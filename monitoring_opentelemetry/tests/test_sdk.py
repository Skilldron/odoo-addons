import os
import warnings
from unittest.mock import patch

from opentelemetry import metrics, trace
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from odoo import release
from odoo.tests import BaseCase, tagged

from .. import sdk
from ..config import Config
from .common import OtelMixin


@tagged("post_install", "-at_install")
class TestSdk(OtelMixin, BaseCase):
    def test_inactive(self):
        self.assertFalse(sdk.is_active())
        self.assertIsInstance(sdk.tracer(), trace.NoOpTracer)
        self.assertIsNone(sdk.logger_provider())
        self.assertIsNone(sdk.config())
        self.assertIsInstance(sdk.meter(), metrics.NoOpMeter)

    def test_setup_once(self):
        self.setup_otel()
        self.assertTrue(sdk.is_active())
        self.assertFalse(sdk.setup(self.otel_config, "threaded"))
        with sdk.tracer().start_as_current_span("test"):
            pass
        (span,) = self.finished_spans()
        self.assertEqual(span.name, "test")
        self.assertIs(sdk.config(), self.otel_config)
        self.assertIsNotNone(sdk.logger_provider())

    def test_resource(self):
        self.setup_otel(process_type="cron")
        with sdk.tracer().start_as_current_span("test"):
            pass
        attributes = self.finished_spans()[0].resource.attributes
        self.assertEqual(attributes["service.name"], "odoo")
        self.assertEqual(attributes["service.version"], release.version)
        self.assertEqual(attributes["odoo.process.type"], "cron")
        self.assertEqual(attributes["process.pid"], os.getpid())

    def test_metrics(self):
        self.setup_otel(process_type="cron")
        sdk.meter().create_counter("test.count").add(2, {"key": "value"})
        collected, resource = self.collect_metrics()
        (point,) = collected["test.count"].data.data_points
        self.assertEqual((point.value, dict(point.attributes)), (2, {"key": "value"}))
        # the workers of a host share its service.instance.id, their metrics do not
        with sdk.tracer().start_as_current_span("test"):
            pass
        traces_resource = self.finished_spans()[0].resource.attributes
        self.assertEqual(
            resource.attributes["service.instance.id"],
            f"{traces_resource['service.instance.id']}-{os.getpid()}",
        )
        self.assertEqual(
            {**resource.attributes, "service.instance.id": None},
            {**traces_resource, "service.instance.id": None},
        )

    def test_metrics_instance_id_from_environment(self):
        environ = {"OTEL_RESOURCE_ATTRIBUTES": "service.instance.id=erp-1"}
        with patch.dict(os.environ, environ):
            self.setup_otel()
        sdk.meter().create_counter("test.count").add(1)
        _collected, resource = self.collect_metrics()
        self.assertEqual(
            resource.attributes["service.instance.id"], f"erp-1-{os.getpid()}"
        )

    def test_instrument(self):
        def create(meter):
            return meter.create_counter("test.count")

        self.assertIsNone(sdk.instrument(create))  # inactive
        self.setup_otel()
        counter = sdk.instrument(create)
        self.assertIs(sdk.instrument(create), counter)  # created once per process
        counter.add(3)
        collected, _resource = self.collect_metrics()
        self.assertEqual(collected["test.count"].data.data_points[0].value, 3)
        with patch.object(sdk.os, "getpid", return_value=os.getpid() + 1):
            self.assertIsNone(sdk.instrument(create))
        self.setup_otel()  # a new provider, new instruments
        self.assertIsNot(sdk.instrument(create), counter)
        self.setup_otel(Config(enabled=True, metrics=False))
        self.assertIsNone(sdk.instrument(create))

    def test_resource_environment_overrides(self):
        environ = {
            "OTEL_SERVICE_NAME": "erp",
            "OTEL_RESOURCE_ATTRIBUTES": (
                "service.version=1.2,deployment.environment.name=staging"
            ),
        }
        with patch.dict(os.environ, environ):
            attributes = sdk.build_resource("http").attributes
        self.assertEqual(attributes["service.name"], "erp")
        self.assertEqual(attributes["service.version"], "1.2")
        self.assertEqual(attributes["deployment.environment.name"], "staging")
        self.assertEqual(attributes["odoo.process.type"], "http")

    def test_other_process(self):
        """The state of another process (the parent) is not used."""
        self.setup_otel()
        with patch.object(sdk.os, "getpid", return_value=os.getpid() + 1):
            self.assertFalse(sdk.is_active())
            self.assertIsInstance(sdk.tracer(), trace.NoOpTracer)
            self.assertIsNone(sdk.logger_provider())
            self.assertIsInstance(sdk.meter(), metrics.NoOpMeter)

    def test_fork(self):
        """In a real forked child, the SDK of the parent is inactive and the child
        sets up its own."""
        self.setup_otel()
        read_fd, write_fd = os.pipe()
        # the test process has threads: the child only reports and exits
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            pid = os.fork()
        if pid == 0:  # child: report through the pipe, never return
            try:
                result = [sdk.is_active()]
                processor = SimpleSpanProcessor(InMemorySpanExporter())
                reader = InMemoryMetricReader()
                result.append(
                    sdk.setup(
                        Config(enabled=True), "http", processor, metric_reader=reader
                    )
                )
                result.append(sdk.is_active())
                sdk.meter().create_counter("test.count").add(1)
                (resource_metrics,) = reader.get_metrics_data().resource_metrics
                attributes = resource_metrics.resource.attributes
                result.append(
                    attributes["service.instance.id"].endswith(f"-{os.getpid()}")
                )
                os.write(write_fd, repr(result).encode())
            finally:
                os._exit(0)
        os.close(write_fd)
        with os.fdopen(read_fd) as pipe:
            result = pipe.read()
        os.waitpid(pid, 0)
        self.assertEqual(result, "[False, True, True, True]")
        self.assertTrue(sdk.is_active())

    def test_setup_failure_is_atomic(self):
        """A failure after the tracer provider was built shuts it down."""
        with (
            patch.object(sdk, "OTLPLogExporter", side_effect=RuntimeError("boom")),
            patch.object(
                sdk.TracerProvider, "shutdown", autospec=True
            ) as tracer_shutdown,
            self.assertRaises(RuntimeError),
        ):
            sdk.setup(
                Config(enabled=True),
                "http",
                span_processor=SimpleSpanProcessor(InMemorySpanExporter()),
            )
        tracer_shutdown.assert_called_once()
        self.assertFalse(sdk.is_active())

    def test_metrics_setup_failure_is_atomic(self):
        """A failure of the meter provider shuts down the other providers and the
        reader it did not take."""
        reader = InMemoryMetricReader()
        with (
            patch.object(sdk, "MeterProvider", side_effect=ValueError("boom")),
            patch.object(sdk.TracerProvider, "shutdown", autospec=True) as tracer,
            patch.object(sdk.LoggerProvider, "shutdown", autospec=True) as logger,
            patch.object(reader, "shutdown") as reader_shutdown,
            self.assertRaises(ValueError),
        ):
            sdk.setup(
                Config(enabled=True),
                "http",
                span_processor=SimpleSpanProcessor(InMemorySpanExporter()),
                log_processor=SimpleLogRecordProcessor(InMemoryLogRecordExporter()),
                metric_reader=reader,
            )
        tracer.assert_called_once()
        logger.assert_called_once()
        reader_shutdown.assert_called_once()
        self.assertFalse(sdk.is_active())

    def test_shutdown(self):
        self.setup_otel()
        sdk.meter().create_counter("test.count").add(1)
        with patch.object(
            self.metric_reader, "shutdown", wraps=self.metric_reader.shutdown
        ) as reader_shutdown:
            sdk.shutdown()
        reader_shutdown.assert_called_once()  # by the meter provider
        self.assertFalse(sdk.is_active())
        self.assertIsInstance(sdk.meter(), metrics.NoOpMeter)
        sdk.shutdown()

    def test_shutdown_failure_is_logged(self):
        self.setup_otel()
        with (
            patch.object(
                sdk.LoggerProvider, "shutdown", side_effect=RuntimeError("boom")
            ),
            self.assertLogs(sdk._logger.name, "WARNING"),
        ):
            sdk.shutdown()
        self.assertFalse(sdk.is_active())

    def test_signals(self):
        self.setup_otel(Config(enabled=True, traces=False))
        self.assertIsInstance(sdk.tracer(), trace.NoOpTracer)
        self.assertIsNotNone(sdk.logger_provider())
        self.setup_otel(Config(enabled=True, logs=False))
        self.assertNotIsInstance(sdk.tracer(), trace.NoOpTracer)
        self.assertIsNone(sdk.logger_provider())
        self.assertNotIsInstance(sdk.meter(), metrics.NoOpMeter)
        self.setup_otel(Config(enabled=True, metrics=False))
        self.assertIsInstance(sdk.meter(), metrics.NoOpMeter)
