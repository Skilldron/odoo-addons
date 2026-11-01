"""Prefork workers: set up the SDK in each worker, after ``os.fork()``.

Odoo has no post-fork hook: ``Worker.start`` runs first in the child process (HTTP
and cron workers), ``Worker.stop`` last when the worker exits cleanly. The providers
also flush at exit (``atexit``), but flushing in ``Worker.stop`` does it while the
worker is still whole (before the cron worker closes its connection) and logged as
part of it. A worker killed (SIGKILL on timeout or memory limit, SIGTERM on a
non-graceful stop) loses the spans and logs not exported yet.

``odoo.worker.recycles`` counts the workers that stop themselves in
``check_limits``, by reason: ``request_limit`` (``limit_request``), ``memory_soft``
(``limit_memory_soft``), ``max_age`` (``limit_time_worker_cron``, cron workers) and
``parent_changed``; it is exported by ``Worker.stop``. Not counted: the workers killed
by the master (``limit_time_real``, ``limit_memory_hard``) and the graceful stops.
"""

import logging
import os
import time

import psutil

from odoo.service.server import Worker, WorkerCron
from odoo.tools import config as odoo_config
from odoo.tools.osutil import memory_info

from .. import sdk
from ._patch import patch, unpatch

_logger = logging.getLogger(__name__)
# attribute of a worker running its check_limits
_CHECKING = "_otel_checking_limits"


def process_type(worker):
    return "cron" if isinstance(worker, WorkerCron) else "http"


def install(activate, deactivate):
    """Call ``activate(process_type)`` when a worker starts and ``deactivate()``
    when it stops."""

    def make_start(original):
        def start(self, *args, **kwargs):
            original(self, *args, **kwargs)
            activate(process_type(self))
            _start_recycles(self)

        return start

    def make_stop(original):
        def stop(self, *args, **kwargs):
            try:
                deactivate()
            finally:
                original(self, *args, **kwargs)

        return stop

    def make_check_limits(original):
        def check_limits(self, *args, **kwargs):
            # WorkerCron.check_limits calls Worker.check_limits (super): only the
            # outermost wrapper counts
            if vars(self).get(_CHECKING):
                return original(self, *args, **kwargs)
            alive = self.alive
            vars(self)[_CHECKING] = True
            try:
                return original(self, *args, **kwargs)
            finally:
                del vars(self)[_CHECKING]
                if alive and not self.alive:
                    _count_recycle(self)

        return check_limits

    patch(Worker, "start", make_start)
    patch(Worker, "stop", make_stop)
    patch(Worker, "check_limits", make_check_limits)
    patch(WorkerCron, "check_limits", make_check_limits)


def uninstall():
    unpatch(WorkerCron, "check_limits")
    unpatch(Worker, "check_limits")
    unpatch(Worker, "start")
    unpatch(Worker, "stop")


def _recycles(meter):
    return meter.create_counter(
        "odoo.worker.recycles",
        unit="{recycle}",
        description="The workers stopped by themselves to be replaced by the master.",
    )


def recycle_reasons(worker):
    """The reasons why ``worker`` may stop itself in ``check_limits``."""
    reasons = ("parent_changed", "request_limit", "memory_soft")
    return (*reasons, "max_age") if isinstance(worker, WorkerCron) else reasons


def recycle_reason(worker):
    """The reason why ``worker`` stopped in ``check_limits`` (the order of Odoo,
    checked again after it), or ``None`` (a signal)."""
    if worker.ppid != os.getppid():
        return "parent_changed"
    if worker.request_count >= worker.request_max:
        return "request_limit"
    limit = odoo_config["limit_memory_soft"]
    if limit and memory_info(psutil.Process()) > limit:
        return "memory_soft"
    age_limit = odoo_config["limit_time_worker_cron"]
    if (
        isinstance(worker, WorkerCron)
        and age_limit > 0
        and time.monotonic() - worker.alive_time > age_limit
    ):
        return "max_age"
    return None


def _start_recycles(worker):
    """Start the series of the worker at 0: a series first seen at 1, when the
    worker stops, would show no increase on some backends."""
    try:
        if counter := sdk.instrument(_recycles):
            for reason in recycle_reasons(worker):
                counter.add(0, {"odoo.worker.recycle.reason": reason})
    except Exception:
        _logger.warning(
            "OpenTelemetry could not count the worker recycles", exc_info=True
        )


def _count_recycle(worker):
    try:
        if (counter := sdk.instrument(_recycles)) and (
            reason := recycle_reason(worker)
        ):
            counter.add(1, {"odoo.worker.recycle.reason": reason})
    except Exception:
        _logger.warning("OpenTelemetry could not count a worker recycle", exc_info=True)
