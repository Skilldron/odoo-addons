from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from .. import sdk
from ..config import Config

ENDPOINT = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.invalid:4318"}


def keep_sdk_state(case):
    """Restore the SDK state of the test process at the end of ``case``."""
    saved = sdk._active

    def restore():
        sdk.shutdown()
        sdk._active = saved

    case.addCleanup(restore)
    sdk._active = None


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
        sdk.setup(
            config or self.otel_config,
            process_type,
            span_processor=SimpleSpanProcessor(self.span_exporter),
            log_processor=SimpleLogRecordProcessor(self.log_exporter),
        )

    def finished_spans(self):
        return self.span_exporter.get_finished_spans()
