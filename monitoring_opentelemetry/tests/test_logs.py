import logging
import threading
from unittest.mock import patch

from opentelemetry import context
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.sdk._logs import LoggerProvider

from odoo.tests import BaseCase, tagged

from .. import hooks, sdk
from ..config import Config
from ..instrumentation import logs as logs_instrumentation
from .common import OtelMixin

LOGGER = "odoo.addons.monitoring_opentelemetry.tests.sample"


@tagged("post_install", "-at_install")
class TestLogs(OtelMixin, BaseCase):
    """The handler is attached to a dedicated logger that does not propagate: the
    records of these tests only go to the in-memory exporter."""

    def setUp(self):
        super().setUp()
        self.setup_otel()
        self.logger = logging.getLogger(LOGGER)
        for name, value in (("propagate", False), ("level", logging.DEBUG)):
            self.addCleanup(setattr, self.logger, name, getattr(self.logger, name))
            setattr(self.logger, name, value)
        self.attach()

    def attach(self, provider=None):
        logs_instrumentation.attach(
            self.otel_config, provider or sdk.logger_provider(), logger=self.logger
        )

    def exported(self, body=None):
        records = [log.log_record for log in self.log_exporter.get_finished_logs()]
        if body is not None:
            records = [record for record in records if record.body == body]
        return records

    def test_correlation(self):
        with sdk.tracer().start_as_current_span("request") as span:
            self.logger.info("inside %s", "a span")
        (record,) = self.exported("inside a span")
        self.assertEqual(record.trace_id, span.get_span_context().trace_id)
        self.assertEqual(record.span_id, span.get_span_context().span_id)
        self.assertEqual(record.severity_text, "INFO")
        self.assertIn("code.function.name", record.attributes)

    def test_odoo_attributes(self):
        thread = threading.current_thread()
        with patch.object(thread, "dbname", "otel_db", create=True):
            record = self.logger.makeRecord(
                LOGGER, logging.INFO, __file__, 1, "odoo record", (), None
            )
        record.colored_message = "\x1b[1modoo record"  # set by ColoredFormatter
        self.logger.handle(record)
        (record,) = self.exported("odoo record")
        self.assertEqual(record.attributes["odoo.db"], "otel_db")
        for key in logs_instrumentation.ODOO_RECORD_ATTRIBUTES:
            self.assertNotIn(key, record.attributes)

    def test_level(self):
        self.logger.debug("debug record")
        self.logger.warning("warning record")
        self.assertEqual(self.exported("debug record"), [])
        (record,) = self.exported("warning record")
        self.assertEqual(record.severity_text, "WARN")

    def test_excluded_loggers(self):
        excluded = logs_instrumentation.ExcludedLoggers(("urllib3", "odoo.http.server"))

        def accepted(name):
            record = logging.LogRecord(name, logging.INFO, "", 1, "m", (), None)
            return excluded.filter(record)

        self.assertFalse(accepted("urllib3"))
        self.assertFalse(accepted("urllib3.connectionpool"))
        self.assertFalse(accepted("odoo.http.server"))
        self.assertTrue(accepted("odoo.http.serverless"))
        self.assertTrue(accepted("odoo.http"))

    def test_failed_query_dropped(self):
        """Odoo logs a failed query with the values of its parameters."""
        excluded = logs_instrumentation.ExcludedLoggers(())

        def accepted(msg):
            record = logging.LogRecord(
                "odoo.sql_db", logging.ERROR, "", 1, msg, ("q", "e"), None
            )
            return excluded.filter(record)

        self.assertFalse(accepted("bad query: %s\nERROR: %s"))
        self.assertTrue(accepted("Connection to the database failed"))

    def test_records_of_the_export_are_dropped(self):
        """Records emitted while exporting (any logger) would loop."""
        token = context.attach(context.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
        try:
            self.logger.warning("emitted by the exporter")
        finally:
            context.detach(token)
        self.assertEqual(self.exported("emitted by the exporter"), [])

    def test_attributes(self):
        """Scalars of ``extra`` are exported; other values and the sensitive
        attributes of the HTTP access log are not."""
        self.logger.info(
            "extra record",
            extra={
                "count": 3,
                "names": ["a", "b"],
                "ident": "session-id",
                "http_headers": [("Cookie", "session_id=x")],
                "obj": object(),
            },
        )
        (record,) = self.exported("extra record")
        self.assertEqual(record.attributes["count"], 3)
        self.assertEqual(tuple(record.attributes["names"]), ("a", "b"))
        for key in ("ident", "http_headers", "obj"):
            self.assertNotIn(key, record.attributes)

    def test_exception(self):
        try:
            raise ValueError("boom")
        except ValueError:
            self.logger.exception("failed")
        (record,) = self.exported("failed")
        self.assertEqual(record.attributes["exception.type"], "ValueError")
        self.assertEqual(record.severity_text, "ERROR")

    def test_emit_never_raises(self):
        with patch.object(logging, "raiseExceptions", False):
            # a malformed call, on purpose
            self.logger.info("%s %s", "one argument missing")  # noqa: PLE1206
        self.assertEqual(self.exported(), [])

    def test_other_provider(self):
        """A handler bound to another provider (a parent process, or shut down)
        drops the records."""
        self.attach(LoggerProvider())
        self.logger.info("other provider")
        self.assertEqual(self.exported("other provider"), [])
        self.attach()
        sdk.shutdown()
        self.logger.info("after shutdown")
        self.assertEqual(self.exported("after shutdown"), [])

    def test_detach(self):
        logs_instrumentation.detach()
        self.logger.info("detached record")
        self.assertEqual(self.exported("detached record"), [])
        self.assertEqual(self.logger.handlers, [])
        logs_instrumentation.detach()

    def test_attach_replaces(self):
        self.attach()
        self.logger.info("once")
        self.assertEqual(len(self.exported("once")), 1)
        self.assertEqual(len(self.logger.handlers), 1)


@tagged("post_install", "-at_install")
class TestHooksLogs(OtelMixin, BaseCase):
    def test_activate_deactivate(self):
        config = Config(enabled=True, traces=False)
        with patch.object(sdk, "OTLPLogExporter"):
            hooks.activate(config, "http")
        handler = logs_instrumentation._handler
        self.assertIsNotNone(handler)
        self.assertIn(handler, logging.getLogger().handlers)
        hooks.deactivate()
        self.assertIsNone(logs_instrumentation._handler)
        self.assertNotIn(handler, logging.getLogger().handlers)
        self.assertFalse(sdk.is_active())
