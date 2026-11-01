from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from .. import sdk
from ..config import Config
from ..instrumentation import logs as logs_instrumentation

ENDPOINT = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.invalid:4318"}


def keep_sdk_state(case):
    """Restore the SDK state of the test process (and its log handler, which drops
    the records meanwhile) at the end of ``case``."""
    saved = sdk._active, logs_instrumentation._handler

    def restore():
        logs_instrumentation.detach()
        sdk.shutdown()
        sdk._active, logs_instrumentation._handler = saved

    case.addCleanup(restore)
    sdk._active = logs_instrumentation._handler = None


class OtelMixin:
    """Set up the SDK of the test process with in-memory exporters."""

    otel_config = Config(enabled=True)

    def setUp(self):
        super().setUp()
        keep_sdk_state(self)

    def setup_otel(self, config=None, process_type="threaded"):
        sdk.shutdown()
        self.span_exporter = InMemorySpanExporter()
        self.log_exporter = InMemoryLogRecordExporter()
        self.metric_reader = InMemoryMetricReader()
        sdk.setup(
            config or self.otel_config,
            process_type,
            span_processor=SimpleSpanProcessor(self.span_exporter),
            log_processor=SimpleLogRecordProcessor(self.log_exporter),
            metric_reader=self.metric_reader,
        )

    def finished_spans(self):
        return self.span_exporter.get_finished_spans()

    def collect_metrics(self):
        """The metrics collected now, by name, and the resource of the metrics."""
        data = self.metric_reader.get_metrics_data()
        metrics, resource = {}, None
        for resource_metrics in data.resource_metrics if data else ():
            resource = resource_metrics.resource
            for scope_metrics in resource_metrics.scope_metrics:
                for metric in scope_metrics.metrics:
                    metrics[metric.name] = metric
        return metrics, resource
