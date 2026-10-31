"""Configuration read from environment variables.

The standard ``OTEL_*`` variables (endpoint, headers, sampler, resource, batch
processors) are read by the OpenTelemetry SDK itself; this module only reads the
switch variables and the ``ODOO_OTEL_*`` variables specific to Odoo.
"""

import logging
import math
import os
from dataclasses import dataclass

_logger = logging.getLogger(__name__)

ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
TRACES_ENDPOINT = "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
LOGS_ENDPOINT = "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"
SQL_MODES = ("off", "slow", "all")
# Never exported: the exporter logs through them, exporting them would loop
MANDATORY_EXCLUDED_LOGGERS = ("opentelemetry", "urllib3", "requests")
DEFAULT_EXCLUDED_LOGGERS = ("werkzeug",)
DEFAULT_EXCLUDED_PATHS = ("/websocket",)


@dataclass(frozen=True)
class Config:
    enabled: bool = False
    traces: bool = True
    logs: bool = True
    log_level: int = logging.INFO
    excluded_loggers: tuple[str, ...] = (
        MANDATORY_EXCLUDED_LOGGERS + DEFAULT_EXCLUDED_LOGGERS
    )
    excluded_paths: tuple[str, ...] = DEFAULT_EXCLUDED_PATHS
    sql: str = "slow"
    sql_slow_ms: float = 100.0
    disabled_reason: str = ""


def _is_set(environ, name):
    return bool(environ.get(name, "").strip())


def _bool(environ, name, default):
    value = environ.get(name, "").strip().lower()
    if not value:
        return default
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    _logger.warning("%s: invalid boolean %r, using %s", name, value, default)
    return default


def _list(environ, name, default):
    value = environ.get(name)
    if value is None:
        return default
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _log_level(environ, name, default):
    value = environ.get(name, "").strip().upper()
    if not value:
        return default
    if value.isdigit():
        return int(value)
    level = logging.getLevelName(value)
    if isinstance(level, int):
        return level
    _logger.warning("%s: invalid log level %r, using %s", name, value, default)
    return default


def _sql_mode(environ, name, default):
    value = environ.get(name, "").strip().lower()
    if not value:
        return default
    if value in SQL_MODES:
        return value
    _logger.warning("%s: invalid value %r (expected %s)", name, value, SQL_MODES)
    return default


def _non_negative_float(environ, name, default):
    value = environ.get(name, "").strip()
    if not value:
        return default
    try:
        number = float(value)
    except ValueError:
        number = math.nan
    if math.isfinite(number) and number >= 0:
        return number
    _logger.warning("%s: invalid number %r, using %s", name, value, default)
    return default


def load(environ=None):
    """Return the :class:`Config` from ``environ`` (default ``os.environ``).

    A signal is enabled only when an endpoint is set for it: the generic one, or
    the one of the signal.
    """
    environ = os.environ if environ is None else environ
    # OTEL_SDK_DISABLED: only "true" (case-insensitive) disables, as in the spec
    if environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return Config(disabled_reason="OTEL_SDK_DISABLED is true")
    generic = _is_set(environ, ENDPOINT)
    traces = (generic or _is_set(environ, TRACES_ENDPOINT)) and _bool(
        environ, "ODOO_OTEL_TRACES", True
    )
    logs = (generic or _is_set(environ, LOGS_ENDPOINT)) and _bool(
        environ, "ODOO_OTEL_LOGS", True
    )
    if not (traces or logs):
        return Config(
            disabled_reason=f"no signal enabled with an endpoint ({ENDPOINT})"
        )
    excluded_loggers = _list(
        environ, "ODOO_OTEL_EXCLUDED_LOGGERS", DEFAULT_EXCLUDED_LOGGERS
    )
    return Config(
        enabled=True,
        traces=traces,
        logs=logs,
        log_level=_log_level(environ, "ODOO_OTEL_LOG_LEVEL", logging.INFO),
        excluded_loggers=MANDATORY_EXCLUDED_LOGGERS
        + tuple(
            name for name in excluded_loggers if name not in MANDATORY_EXCLUDED_LOGGERS
        ),
        excluded_paths=_list(
            environ, "ODOO_OTEL_EXCLUDED_PATHS", DEFAULT_EXCLUDED_PATHS
        ),
        sql=_sql_mode(environ, "ODOO_OTEL_SQL", "slow"),
        sql_slow_ms=_non_negative_float(environ, "ODOO_OTEL_SQL_SLOW_MS", 100.0),
    )
