"""Prefork workers: set up the SDK in each worker, after ``os.fork()``.

Odoo has no post-fork hook: ``Worker.start`` runs first in the child process (HTTP
and cron workers), ``Worker.stop`` last when the worker exits cleanly. The providers
also flush at exit (``atexit``), but flushing in ``Worker.stop`` does it while the
worker is still whole (before the cron worker closes its connection) and logged as
part of it. A worker killed (SIGKILL on timeout or memory limit, SIGTERM on a
non-graceful stop) loses the spans and logs not exported yet.
"""

from odoo.service.server import Worker, WorkerCron

from ._patch import patch, unpatch


def process_type(worker):
    return "cron" if isinstance(worker, WorkerCron) else "http"


def install(activate, deactivate):
    """Call ``activate(process_type)`` when a worker starts and ``deactivate()``
    when it stops."""

    def make_start(original):
        def start(self, *args, **kwargs):
            original(self, *args, **kwargs)
            activate(process_type(self))

        return start

    def make_stop(original):
        def stop(self, *args, **kwargs):
            try:
                deactivate()
            finally:
                original(self, *args, **kwargs)

        return stop

    patch(Worker, "start", make_start)
    patch(Worker, "stop", make_stop)


def uninstall():
    unpatch(Worker, "start")
    unpatch(Worker, "stop")
