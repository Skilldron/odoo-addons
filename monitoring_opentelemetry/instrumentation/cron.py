"""Cron jobs: one ``INTERNAL`` span ``cron {name}`` per job.

``IrCron._process_job`` runs one job in the cron workers (prefork) and the cron
threads (threaded server), where the span is the root of its own trace; its SQL
queries are tracked on the span. A job run manually (``method_direct_trigger``) runs
in its HTTP request: its span is a child of the request span, whose SQL totals
include the job's queries.

The exception raised by a run of the job is added to the span in ``_callback``, before
``_run_job`` logs it without raising it, whatever its class: in a job, a user error is
seen by nobody. It does not set the span in error: a run that failed after committing
progress may end ``partially done`` or ``fully done``. The completion status of the job
is known in ``_update_failure_count``, also called when the job failed by timeout
without running (``odoo.cron.timed_out``): only ``failed`` sets the span in error.

Metrics, with or without the traces: ``odoo.cron.duration`` (by job name and
completion status, ``failed`` when the run raised) and ``odoo.cron.lag``, the delay
between the ``nextcall`` of a job and the start of its run by the scheduler, both
read from the database clock. Not measured: the lag of a run by a trigger before its
``nextcall`` (no known due time), of a job run again after a ``partially done`` run
(its ``nextcall`` is unchanged) and of a manual run. A job failed by timeout without
running has a duration near zero.
"""

import contextvars
import logging
import time
from dataclasses import dataclass

from opentelemetry import context, trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from odoo.addons.base.models.ir_cron import (
    CONSECUTIVE_TIMEOUT_FOR_FAILURE,
    CompletionStatus,
    IrCron,
)

from .. import sdk
from . import errors
from . import sql as sql_instrumentation
from ._patch import patch, unpatch

_logger = logging.getLogger(__name__)
_current = contextvars.ContextVar("monitoring_opentelemetry_cron", default=None)
# in method_direct_trigger: a job run manually, in its HTTP request
_manual = contextvars.ContextVar("monitoring_opentelemetry_cron_manual", default=False)
DURATION_BUCKETS = (0.1, 0.5, 1.0, 5.0, 10.0, 30.0, 60.0, 300.0, 600.0, 1800.0, 3600.0)
LAG_BUCKETS = (1.0, 5.0, 10.0, 30.0, 60.0, 300.0, 600.0, 1800.0, 3600.0)


def _duration(meter):
    return meter.create_histogram(
        "odoo.cron.duration",
        unit="s",
        description="Duration of the runs of the cron jobs.",
        explicit_bucket_boundaries_advisory=DURATION_BUCKETS,
    )


def _lag(meter):
    return meter.create_histogram(
        "odoo.cron.lag",
        unit="s",
        description="Delay between the next call of a cron job and its run.",
        explicit_bucket_boundaries_advisory=LAG_BUCKETS,
    )


@dataclass
class _Job:
    """The span of the current job, and its completion status."""

    span: trace.Span
    status: str | None = None


def _safely(function, *args):
    try:
        function(*args)
    except Exception:
        _logger.debug("OpenTelemetry instrumentation failed", exc_info=True)


def _in_request():
    return trace.get_current_span().get_span_context().is_valid


def _start(cron_cr, job):
    in_request = _in_request()
    return sdk.tracer().start_span(
        f"cron {job['cron_name']}",
        context=None if in_request else context.Context(),
        kind=SpanKind.INTERNAL,
        attributes={
            "odoo.cron.id": job["id"],
            "odoo.cron.name": job["cron_name"],
            "odoo.db": cron_cr.dbname,
        },
    )


def _make_process_job(original):
    def _process_job(cls, cron_cr, job):
        config = sdk.config()
        if config is None or not (config.traces or config.metrics):
            return original(cls, cron_cr, job)
        start = time.perf_counter()
        try:
            if not _manual.get():
                _safely(_record_lag, cron_cr, job)
            span = _start(cron_cr, job)
        except Exception:
            _logger.debug("OpenTelemetry instrumentation failed", exc_info=True)
            return original(cls, cron_cr, job)
        current = _Job(span)
        context_token = context.attach(trace.set_span_in_context(span))
        cron_token = _current.set(current)
        try:
            with sql_instrumentation.track(span, config):
                return original(cls, cron_cr, job)
        except BaseException as exc:
            _safely(errors.record, span, exc)
            raise
        finally:
            _current.reset(cron_token)
            # in the context of the span: the exemplars of its trace
            _safely(_record_duration, job, current, start)
            context.detach(context_token)
            _safely(span.end)

    return _process_job


def _record_lag(cron_cr, job):
    if job.get("remaining"):  # run again after a partially done run
        return
    # the time that made the job ready (cached by the query that acquired it)
    lag = (cron_cr.now() - job["nextcall"]).total_seconds()
    if lag >= 0 and (histogram := sdk.instrument(_lag)):
        histogram.record(lag, {"odoo.cron.name": job["cron_name"]})


def _record_duration(job, current, start):
    if histogram := sdk.instrument(_duration):
        attributes = {
            "odoo.cron.name": job["cron_name"],
            "odoo.cron.status": current.status or str(CompletionStatus.FAILED),
        }
        histogram.record(time.perf_counter() - start, attributes)


def _add_exception(span, exc):
    span.set_attribute("odoo.exception.type", errors.qualified_name(exc))
    errors.add_event(span, exc)


def _make_callback(original):
    def _callback(self, *args, **kwargs):
        try:
            return original(self, *args, **kwargs)
        except Exception as exc:
            if (current := _current.get()) is not None:
                _safely(_add_exception, current.span, exc)
            raise

    return _callback


def _set_status(current, job, status):
    current.status = str(status)
    span = current.span
    span.set_attribute("odoo.cron.status", str(status))
    if status != CompletionStatus.FAILED:
        return
    timed_out = (
        job["timed_out_counter"] >= CONSECUTIVE_TIMEOUT_FOR_FAILURE and not job["done"]
    )
    if timed_out:
        span.set_attribute("odoo.cron.timed_out", True)
    span.set_status(Status(StatusCode.ERROR, "timed out" if timed_out else "failed"))


def _make_update_failure_count(original):
    def _update_failure_count(self, job, status):
        if (current := _current.get()) is not None:
            _safely(_set_status, current, job, status)
        return original(self, job, status)

    return _update_failure_count


def _make_method_direct_trigger(original):
    def method_direct_trigger(self, *args, **kwargs):
        token = _manual.set(True)
        try:
            return original(self, *args, **kwargs)
        finally:
            _manual.reset(token)

    return method_direct_trigger


def install():
    patch(IrCron, "_process_job", _make_process_job)
    patch(IrCron, "method_direct_trigger", _make_method_direct_trigger)
    patch(IrCron, "_callback", _make_callback)
    patch(IrCron, "_update_failure_count", _make_update_failure_count)


def uninstall():
    unpatch(IrCron, "method_direct_trigger")
    unpatch(IrCron, "_update_failure_count")
    unpatch(IrCron, "_callback")
    unpatch(IrCron, "_process_job")
