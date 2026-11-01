"""Per-process OpenTelemetry SDK.

The SDK is set up once in each process that serves requests or runs crons, never in
the prefork master: its exporters, batch threads and this module's state must not be
shared with forked workers. The state of the process is an immutable snapshot tagged
with its PID and replaced in one assignment, so a reader never sees it half set up,
and a forked child never uses the one of its parent: in any process without its own
SDK, :func:`tracer` and :func:`meter` return no-op ones and :func:`logger_provider`
``None``.

The global OpenTelemetry providers are not set: the instrumentation of this module
uses the providers below explicitly.
"""

import logging
import os
import socket
import threading
from dataclasses import dataclass, field

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import OTELResourceDetector, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from odoo import release

_logger = logging.getLogger(__name__)

INSTRUMENTATION_NAME = "odoo.addons.monitoring_opentelemetry"
_NOOP_TRACER = trace.NoOpTracer()
_NOOP_METER = metrics.NoOpMeter(INSTRUMENTATION_NAME)


@dataclass(frozen=True)
class _Active:
    pid: int
    config: object
    tracer_provider: TracerProvider | None
    tracer: trace.Tracer | None
    logger_provider: LoggerProvider | None
    meter_provider: MeterProvider | None
    meter: metrics.Meter | None
    # instruments created by instrument(), by factory
    instruments: dict[object, object] = field(
        default_factory=dict, compare=False, repr=False
    )
    instruments_lock: threading.Lock = field(
        default_factory=threading.Lock, compare=False, repr=False
    )


_active = None
_lock = threading.Lock()


def _reset_lock():
    global _lock
    _lock = threading.Lock()


# A fork while another thread holds the lock would leave it locked in the child
os.register_at_fork(after_in_child=_reset_lock)


def _current():
    active = _active
    if active is not None and active.pid == os.getpid():
        return active
    return None


def is_active():
    """Whether the SDK is set up in the current process."""
    return _current() is not None


def config():
    """The :class:`~.config.Config` of the current process, ``None`` if inactive."""
    active = _current()
    return active.config if active else None


def tracer():
    """The tracer of the current process, a no-op tracer if inactive."""
    active = _current()
    if active and active.tracer is not None:
        return active.tracer
    return _NOOP_TRACER


def meter():
    """The meter of the current process, a no-op meter if inactive."""
    active = _current()
    if active and active.meter is not None:
        return active.meter
    return _NOOP_METER


def instrument(create):
    """The instrument ``create(meter)`` of the current process, created once; ``None``
    if its metrics are inactive.

    The instruments of a process belong to its meter provider: created on first use,
    after the setup of the SDK in the process, never shared with a forked child.
    ``create`` is a module-level function (the key of the cache), which neither
    raises nor calls this function.
    """
    active = _current()
    if active is None or active.meter is None:
        return None
    if (found := active.instruments.get(create)) is not None:
        return found
    with active.instruments_lock:
        if create not in active.instruments:
            active.instruments[create] = create(active.meter)
        return active.instruments[create]


def logger_provider():
    """The logger provider of the current process, ``None`` if inactive."""
    active = _current()
    return active.logger_provider if active else None


def build_resource(process_type):
    """Resource of the process.

    ``Resource.create()`` gives its attributes priority over the environment; the
    second merge gives ``OTEL_RESOURCE_ATTRIBUTES`` and ``OTEL_SERVICE_NAME``
    priority over these defaults.
    """
    defaults = {
        "service.name": "odoo",
        "service.version": release.version,
        "service.instance.id": socket.gethostname(),
        "odoo.version": release.version,
        "odoo.process.type": process_type,
        "process.pid": os.getpid(),
    }
    return Resource.create(defaults).merge(OTELResourceDetector().detect())


def build_metrics_resource(resource):
    """Resource of the metrics of the process: ``resource`` with the PID appended to
    ``service.instance.id``.

    The workers of a host share its ``service.instance.id``; each one exports its own
    cumulative series, which must not be mixed up with the ones of the others.
    """
    instance_id = resource.attributes["service.instance.id"]
    return resource.merge(
        Resource({"service.instance.id": f"{instance_id}-{os.getpid()}"})
    )


def setup(
    config,
    process_type,
    span_processor=None,
    log_processor=None,
    metric_reader=None,
):
    """Set up the SDK in the current process, once.

    :param config: the :class:`~.config.Config`, enabled
    :param process_type: ``http``, ``cron`` or ``threaded``
    :param span_processor: processor replacing the OTLP batch one (tests)
    :param log_processor: processor replacing the OTLP batch one (tests)
    :param metric_reader: reader replacing the OTLP periodic one (tests)
    :return: whether the SDK was set up by this call
    :raise: on failure, after shutting down what was built
    """
    global _active
    with _lock:
        if _current():
            return False
        tracer_provider = logger_provider_ = meter_provider = reader = None
        try:
            resource = build_resource(process_type)
            if config.traces:
                tracer_provider = TracerProvider(resource=resource)
                tracer_provider.add_span_processor(
                    span_processor or BatchSpanProcessor(OTLPSpanExporter())
                )
            if config.logs:
                logger_provider_ = LoggerProvider(resource=resource)
                logger_provider_.add_log_record_processor(
                    log_processor or BatchLogRecordProcessor(OTLPLogExporter())
                )
            if config.metrics:
                reader = metric_reader or PeriodicExportingMetricReader(
                    OTLPMetricExporter()
                )
                meter_provider = MeterProvider(
                    metric_readers=[reader], resource=build_metrics_resource(resource)
                )
        except Exception:
            # a periodic reader runs its own thread until its provider, or itself,
            # is shut down
            _shutdown_providers(
                tracer_provider, logger_provider_, meter_provider or reader
            )
            raise
        _active = _Active(
            pid=os.getpid(),
            config=config,
            tracer_provider=tracer_provider,
            tracer=(
                tracer_provider.get_tracer(INSTRUMENTATION_NAME, release.version)
                if tracer_provider
                else None
            ),
            logger_provider=logger_provider_,
            meter_provider=meter_provider,
            meter=(
                meter_provider.get_meter(INSTRUMENTATION_NAME, release.version)
                if meter_provider
                else None
            ),
        )
    _logger.debug("OpenTelemetry SDK set up (%s process)", process_type)
    return True


def shutdown():
    """Flush and shut down the SDK of the current process, if set up."""
    global _active
    with _lock:
        active = _current()
        if active is None:
            return
        _active = None
    _shutdown_providers(
        active.tracer_provider, active.logger_provider, active.meter_provider
    )


def _shutdown_providers(*providers):
    for provider in providers:
        if provider is None:
            continue
        try:
            provider.shutdown()
        except Exception:
            _logger.warning("OpenTelemetry provider shutdown failed", exc_info=True)
