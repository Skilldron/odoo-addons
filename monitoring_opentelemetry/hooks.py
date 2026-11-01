"""Entry point: the ``post_load`` hook of the server-wide module.

``post_load`` runs once per process, when ``odoo.service.server.start()`` loads the
server-wide modules, before the server is created and before any fork:

* prefork master (``workers`` > 0): the instrumentation is installed, the SDK is set
  up later in each worker (see :mod:`.instrumentation.server`);
* single process (``workers`` = 0, or ``--stop-after-init``): the SDK is set up now;
* gevent process: nothing, in this version.

Telemetry must never break Odoo: every failure is logged and Odoo runs without it.
"""

import atexit
import logging

import odoo
from odoo.service import server
from odoo.tools import config as odoo_config

from . import config as otel_config
from . import sdk
from .instrumentation import cron as cron_instrumentation
from .instrumentation import http as http_instrumentation
from .instrumentation import logs as logs_instrumentation
from .instrumentation import process as process_instrumentation
from .instrumentation import rpc as rpc_instrumentation
from .instrumentation import server as server_instrumentation

_logger = logging.getLogger(__name__)


def post_load():
    try:
        _post_load()
    except Exception:
        _logger.exception("OpenTelemetry could not be enabled, running without it")


def _post_load():
    config = otel_config.load()
    if not config.enabled:
        _logger.info("OpenTelemetry is disabled: %s", config.disabled_reason)
        return
    if server.server is not None:
        _logger.warning(
            "OpenTelemetry is inactive: load monitoring_opentelemetry server-wide "
            "(server_wide_modules or --load)"
        )
        return
    if odoo.evented:
        _logger.info("OpenTelemetry is not enabled in the gevent process")
        return
    install(config)
    if odoo_config["workers"] and not odoo_config["stop_after_init"]:
        _logger.info("OpenTelemetry is enabled in each worker")
    else:
        activate(config, "threaded")
        atexit.register(deactivate)
        _logger.info("OpenTelemetry is enabled")


def install(config):
    """Install the instrumentation of the Odoo server, for ``config``."""
    server_instrumentation.install(
        lambda process_type: activate(config, process_type), deactivate
    )
    if config.traces or config.metrics:
        http_instrumentation.install()
    if config.traces:
        rpc_instrumentation.install()
        cron_instrumentation.install()


def uninstall():
    """Remove the instrumentation installed by :func:`install`."""
    cron_instrumentation.uninstall()
    rpc_instrumentation.uninstall()
    http_instrumentation.uninstall()
    server_instrumentation.uninstall()


def activate(config, process_type):
    """Set up the SDK in the current process, export its logs and its metrics; never
    raise."""
    try:
        if not sdk.setup(config, process_type):
            return
        if provider := sdk.logger_provider():
            logs_instrumentation.attach(config, provider)
    except Exception:
        _logger.exception("OpenTelemetry setup failed, running without it")
        return
    if config.metrics:
        try:
            process_instrumentation.register(sdk.meter())
        except Exception:
            _logger.exception("OpenTelemetry process metrics failed")


def deactivate():
    """Flush and shut down the SDK of the current process; never raise."""
    try:
        logs_instrumentation.detach()
        sdk.shutdown()
    except Exception:
        _logger.exception("OpenTelemetry shutdown failed")
