"""SQL queries of a traced request or cron job, through the query hooks of Odoo.

``odoo.sql_db.Cursor.execute`` calls the ``query_hooks`` of the current thread before
each query; a hook may return a function, called with the duration of the query
after it. While a request or a cron job is traced, a hook counts its queries and
their duration (``odoo.sql.count`` and ``odoo.sql.duration_ms`` on its span) and,
depending on ``ODOO_OTEL_SQL``, creates a ``CLIENT`` span per query after the fact:
``slow`` (default) for the queries lasting at least ``ODOO_OTEL_SQL_SLOW_MS``,
``all`` for every query, ``off`` for none.

The query text is recorded as executed, with its placeholders, truncated: string and
numeric literals are replaced by ``?`` and comments removed (``execute_values`` and
``mogrify`` inline the parameters). The parameters themselves are never recorded.
A failed query is recorded like the others: the hook does not see its exception.

``db.client.operation.duration`` measures each query of a request or a cron job
once (a job run manually tracks its queries inside the tracking of its request), with
or without the traces, by operation (the first word of the query) and database: not
by table, nor with an ``error.type`` (the hook cannot tell a failed query reliably).
"""

import contextlib
import logging
import re
import threading

from opentelemetry.trace import SpanKind

from odoo.sql_db import categorize_query

from .. import sdk

_logger = logging.getLogger(__name__)

QUERY_TEXT_MAX_LENGTH = 2048
# scanned left to right, so that a quote in a comment or a string is not misread; a
# literal or comment cut by the first truncation ends at the end of the text
_LITERAL_OR_COMMENT = re.compile(
    r"""
      (?P<comment> --[^\n]* | /\*.*?(?:\*/|\Z) )
    | \b[Ee]'(?:[^'\\]|\\.|'')*(?:'|\Z)
    | '(?:[^']|'')*(?:'|\Z)
    | \$(\w*)\$.*?(?:\$\2\$|\Z)
    | \b\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?\b
    """,
    re.DOTALL | re.VERBOSE,
)
_FIRST_WORD = re.compile(r"\s*([A-Za-z]+)")
_UPDATE_TABLE = re.compile(r'\s*UPDATE\s+"?(\w+)', re.IGNORECASE)
# advice of the semantic conventions for db.client.operation.duration
DURATION_BUCKETS = (0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0)


def _safely(function, *args):
    try:
        function(*args)
    except Exception:
        _logger.debug("OpenTelemetry instrumentation failed", exc_info=True)


def query_text(query):
    """The text of ``query`` to record, ``None`` if it is not a string."""
    if isinstance(query, bytes):
        query = query.decode("utf-8", "replace")
    if not isinstance(query, str):  # psycopg2 Composable: rendered with a connection
        return None
    # bounded work on a large query (execute_values), then the final truncation
    query = query[: QUERY_TEXT_MAX_LENGTH * 4]
    masked = _LITERAL_OR_COMMENT.sub(_mask, query)
    return masked.strip()[:QUERY_TEXT_MAX_LENGTH]


def operation_name(query):
    """The first word of ``query`` as executed, upper case (``WITH`` for a common
    table expression); ``None`` if it is not a string or starts otherwise (a comment,
    a parenthesis). The spans name the operation from the masked text instead."""
    if isinstance(query, bytes):
        query = query[:64].decode("utf-8", "replace")
    if not isinstance(query, str):
        return None
    match = _FIRST_WORD.match(query)
    return match.group(1).upper() if match else None


def _operation_duration(meter):
    return meter.create_histogram(
        "db.client.operation.duration",
        unit="s",
        description="Duration of database client operations.",
        explicit_bucket_boundaries_advisory=DURATION_BUCKETS,
    )


def _mask(match):
    return "" if match.group("comment") else "?"


def _table(text):
    if match := _UPDATE_TABLE.match(text):
        return match.group(1)
    return categorize_query(text)[1]


def _query_span(cursor, query, start, duration):
    text = query_text(query)
    match = _FIRST_WORD.match(text or "")
    operation = match.group(1).upper() if match else "SQL"
    table = _table(text) if text else None
    attributes = {"db.system.name": "postgresql", "db.operation.name": operation}
    if dbname := getattr(cursor, "dbname", None):
        attributes["db.namespace"] = dbname
    if table:
        attributes["db.collection.name"] = table
    if text:
        attributes["db.query.text"] = text
    start_time = int(start * 1e9)
    span = sdk.tracer().start_span(
        f"{operation} {table}" if table else operation,
        kind=SpanKind.CLIENT,
        attributes=attributes,
        start_time=start_time,
    )
    span.end(end_time=start_time + int(duration * 1e9))


class _Tracker:
    """The query hook of a traced request or cron job."""

    def __init__(self, config):
        # no span to build without the traces (the metrics only)
        self.mode = config.sql if config.traces else "off"
        self.slow = config.sql_slow_ms / 1000
        self.count = 0
        self.duration = 0.0
        self.histogram = sdk.instrument(_operation_duration)

    def hook(self, cursor, query, params, start, *args):
        self.count += 1

        def done(duration):
            self.duration += duration
            if self.histogram is not None:
                _safely(self.record, cursor, query, duration)
            if self.mode == "all" or (self.mode == "slow" and duration >= self.slow):
                _safely(_query_span, cursor, query, start, duration)

        return done

    def record(self, cursor, query, duration):
        attributes = {"db.system.name": "postgresql"}
        if dbname := getattr(cursor, "dbname", None):
            attributes["db.namespace"] = dbname
        if operation := operation_name(query):
            attributes["db.operation.name"] = operation
        self.histogram.record(duration, attributes)


@contextlib.contextmanager
def track(span, config):
    """Track the SQL queries of the current thread on ``span`` meanwhile."""
    tracker = _Tracker(config)
    hooks = None
    try:
        hooks = vars(threading.current_thread()).setdefault("query_hooks", [])
        # an outer tracker already measures the queries
        if any(isinstance(getattr(hook, "__self__", None), _Tracker) for hook in hooks):
            tracker.histogram = None
        hooks.append(tracker.hook)
    except Exception:
        _logger.debug("OpenTelemetry instrumentation failed", exc_info=True)
        hooks = None
    try:
        yield tracker
    finally:
        if hooks is not None:
            with contextlib.suppress(ValueError):
                hooks.remove(tracker.hook)
        _safely(span.set_attribute, "odoo.sql.count", tracker.count)
        _safely(
            span.set_attribute,
            "odoo.sql.duration_ms",
            round(tracker.duration * 1000, 3),
        )
