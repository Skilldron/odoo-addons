import logging

from odoo.tests import BaseCase, tagged

from .. import config
from .common import ENDPOINT

CONFIG_LOGGER = "odoo.addons.monitoring_opentelemetry.config"


@tagged("post_install", "-at_install")
class TestConfig(BaseCase):
    def test_disabled_without_endpoint(self):
        result = config.load({})
        self.assertFalse(result.enabled)
        self.assertIn("OTEL_EXPORTER_OTLP_ENDPOINT", result.disabled_reason)

    def test_signal_endpoint(self):
        """A signal is enabled only with an endpoint, generic or its own."""
        result = config.load({config.TRACES_ENDPOINT: "http://c:4318/v1/traces"})
        self.assertEqual((result.enabled, result.traces, result.logs), (1, 1, 0))
        result = config.load({config.LOGS_ENDPOINT: "http://c:4318/v1/logs"})
        self.assertEqual((result.enabled, result.traces, result.logs), (1, 0, 1))

    def test_sdk_disabled(self):
        result = config.load({**ENDPOINT, "OTEL_SDK_DISABLED": "TRUE"})
        self.assertFalse(result.enabled)
        self.assertIn("OTEL_SDK_DISABLED", result.disabled_reason)
        # only "true" disables the SDK (specification)
        self.assertTrue(config.load({**ENDPOINT, "OTEL_SDK_DISABLED": "1"}).enabled)

    def test_defaults(self):
        result = config.load(ENDPOINT)
        self.assertEqual(result, config.Config(enabled=True))
        self.assertEqual((result.traces, result.logs), (True, True))
        self.assertEqual(result.log_level, logging.INFO)
        self.assertEqual(result.excluded_paths, ("/websocket",))
        self.assertEqual(
            result.excluded_loggers,
            ("opentelemetry", "urllib3", "requests", "werkzeug"),
        )
        self.assertEqual((result.sql, result.sql_slow_ms), ("slow", 100.0))

    def test_values(self):
        result = config.load(
            {
                **ENDPOINT,
                "ODOO_OTEL_LOGS": "false",
                "ODOO_OTEL_LOG_LEVEL": "warning",
                "ODOO_OTEL_EXCLUDED_LOGGERS": "a, urllib3,,",
                "ODOO_OTEL_EXCLUDED_PATHS": "",
                "ODOO_OTEL_SQL": "ALL",
                "ODOO_OTEL_SQL_SLOW_MS": "2.5",
            }
        )
        self.assertTrue(result.traces)
        self.assertFalse(result.logs)
        self.assertEqual(result.log_level, logging.WARNING)
        # the loggers of the exporter are always excluded
        self.assertEqual(
            result.excluded_loggers, ("opentelemetry", "urllib3", "requests", "a")
        )
        self.assertEqual(result.excluded_paths, ())
        self.assertEqual((result.sql, result.sql_slow_ms), ("all", 2.5))

    def test_numeric_log_level(self):
        result = config.load({**ENDPOINT, "ODOO_OTEL_LOG_LEVEL": "25"})
        self.assertEqual(result.log_level, 25)

    def test_invalid_values_keep_defaults(self):
        with self.assertLogs(CONFIG_LOGGER, "WARNING") as logs:
            result = config.load(
                {
                    **ENDPOINT,
                    "ODOO_OTEL_TRACES": "maybe",
                    "ODOO_OTEL_LOG_LEVEL": "LOUD",
                    "ODOO_OTEL_SQL": "some",
                    "ODOO_OTEL_SQL_SLOW_MS": "inf",
                }
            )
            config.load({**ENDPOINT, "ODOO_OTEL_SQL_SLOW_MS": "-1"})
        self.assertEqual(len(logs.records), 5)
        self.assertEqual(result, config.Config(enabled=True))

    def test_no_signal(self):
        result = config.load(
            {**ENDPOINT, "ODOO_OTEL_TRACES": "0", "ODOO_OTEL_LOGS": "no"}
        )
        self.assertFalse(result.enabled)
