import os
import warnings
from unittest.mock import patch

from opentelemetry import trace
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
                result.append(sdk.setup(Config(enabled=True), "http", processor))
                result.append(sdk.is_active())
                os.write(write_fd, repr(result).encode())
            finally:
                os._exit(0)
        os.close(write_fd)
        with os.fdopen(read_fd) as pipe:
            result = pipe.read()
        os.waitpid(pid, 0)
        self.assertEqual(result, "[False, True, True]")
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

    def test_shutdown(self):
        self.setup_otel()
        sdk.shutdown()
        self.assertFalse(sdk.is_active())
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
