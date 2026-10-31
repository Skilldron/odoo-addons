"""Logs: the log records of the process exported over OTLP.

A handler on the root logger, attached in each process once its SDK is set up. The
trace and span ids of the current span are set on each record (correlation with the
traces), the database of the Odoo record becomes ``odoo.db``. The level of the
handler (``ODOO_OTEL_LOG_LEVEL``) filters on top of the levels of the loggers set by
Odoo (``--log-level``, ``--log-handler``).

Not exported: the loggers excluded by the configuration (the exporter's own, which
would loop, and the HTTP access logs, which the traces replace), the records emitted
while exporting, the queries logged by Odoo with their parameters (failed queries,
``odoo.sql_db`` debug records), the attributes of a record that are not scalars, and
the attributes of the HTTP access log that hold headers, cookies, the session id or
the client address. The message and the exception of a record are exported as
logged, like on the standard output.

A record is exported only while the provider of the handler is the one of the SDK of
the current process: never after its shutdown, nor in a forked child.
"""

import logging
import os

from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY, get_value
from opentelemetry.instrumentation.logging.handler import LoggingHandler

from .. import sdk

# Attributes of the Odoo log records (odoo.netsvc.LogRecord, and colored_message set
# by odoo.logging.ColoredFormatter when the output handler ran first): not exported
ODOO_RECORD_ATTRIBUTES = frozenset(
    ("dbname", "thread_native", "test", "faked_created", "colored_message")
)
# Extra of the HTTP access log (odoo.http.server_log), should a record carry them
SENSITIVE_ATTRIBUTES = frozenset(
    ("ident", "http_auth", "http_headers", "http_request_line", "remote_addr")
)
_SCALARS = (str, bool, int, float)

_handler = None


def _is_query(record):
    """A record with a query: Odoo logs the values of its parameters, for a failed
    query and in debug (``--log-level=debug_sql``)."""
    if record.name != "odoo.sql_db":
        return False
    if record.levelno <= logging.DEBUG:
        return True
    return isinstance(record.msg, str) and record.msg.startswith("bad query:")


class ExcludedLoggers(logging.Filter):
    """Reject the records of the given loggers and of their children, the records
    emitted while exporting (whatever their logger), and the records of the queries
    (the exception of a failed one reaches the span of the request or the cron job)."""

    def __init__(self, names):
        super().__init__()
        self.names = frozenset(names)
        self.prefixes = tuple(f"{name}." for name in names)

    def filter(self, record):
        if get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return False
        name = record.name
        if name in self.names or name.startswith(self.prefixes):
            return False
        return not _is_query(record)


def _is_exportable(value):
    if isinstance(value, _SCALARS):
        return True
    return isinstance(value, list | tuple) and all(
        isinstance(item, _SCALARS) for item in value
    )


class OdooLoggingHandler(LoggingHandler):
    def emit(self, record):
        # the provider of another process (fork) or shut down: drop the record
        if sdk.logger_provider() is not self._logger_provider:
            return
        try:
            super().emit(record)
        except Exception:  # noqa: BLE001 - never raise into the code that logs
            self.handleError(record)

    # Overrides a private method of the pinned opentelemetry-instrumentation-logging:
    # tests/test_logs.py fails if it is renamed by a new version
    def _get_attributes(self, record):
        attributes, event_name = super()._get_attributes(record)
        attributes = {
            key: value
            for key, value in attributes.items()
            if key not in ODOO_RECORD_ATTRIBUTES
            and key not in SENSITIVE_ATTRIBUTES
            and _is_exportable(value)
        }
        dbname = getattr(record, "dbname", None)
        if dbname and dbname != "?":
            attributes["odoo.db"] = dbname
        return attributes, event_name


def attach(config, logger_provider, logger=None):
    """Export the logs of the process (of ``logger``, default the root logger) to
    ``logger_provider``."""
    global _handler
    detach()
    handler = OdooLoggingHandler(
        level=config.log_level,
        logger_provider=logger_provider,
        log_code_attributes=True,
    )
    handler.addFilter(ExcludedLoggers(config.excluded_loggers))
    handler.otel_logger = logger or logging.getLogger()
    handler.otel_logger.addHandler(handler)
    _handler = handler


def detach():
    """Stop exporting the logs of the process."""
    global _handler
    handler, _handler = _handler, None
    if handler is not None:
        handler.otel_logger.removeHandler(handler)
        handler.close()


# A forked child must not keep the handler of its parent
os.register_at_fork(after_in_child=detach)
