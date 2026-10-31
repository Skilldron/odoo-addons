import os
from unittest.mock import patch

import odoo
from odoo.service import server
from odoo.service.server import Worker, WorkerCron, WorkerHTTP
from odoo.tests import BaseCase, tagged
from odoo.tools import config as odoo_config

from .. import hooks, sdk
from ..config import Config
from ..instrumentation import _patch
from ..instrumentation import server as server_instrumentation
from .common import ENDPOINT, keep_sdk_state

HOOKS_LOGGER = "odoo.addons.monitoring_opentelemetry.hooks"


@tagged("post_install", "-at_install")
class TestHooks(BaseCase):
    def setUp(self):
        super().setUp()
        keep_sdk_state(self)
        self.addCleanup(hooks.uninstall)
        patcher = patch.object(server, "server", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def post_load(self, environ, workers=0, stop_after_init=False, evented=False):
        options = {"workers": workers, "stop_after_init": stop_after_init}
        with (
            patch.dict(os.environ, environ, clear=True),
            patch.dict(odoo_config.options, options),
            patch.object(odoo, "evented", evented),
            patch.object(hooks.atexit, "register") as register,
            patch.object(hooks.sdk, "setup", wraps=sdk.setup) as setup,
            self.assertLogs(HOOKS_LOGGER, "INFO") as logs,
        ):
            hooks.post_load()
        return setup, register, logs.output

    def is_installed(self):
        return _patch.is_patched(Worker, "start")

    def test_not_server_wide(self):
        with (
            patch.dict(os.environ, ENDPOINT, clear=True),
            patch.object(server, "server", object()),
            self.assertLogs(HOOKS_LOGGER, "WARNING"),
        ):
            hooks.post_load()
        self.assertFalse(self.is_installed())

    def test_disabled(self):
        setup, _register, output = self.post_load({})
        setup.assert_not_called()
        self.assertFalse(self.is_installed())
        self.assertIn("disabled", output[0])

    def test_gevent(self):
        setup, _register, output = self.post_load(ENDPOINT, evented=True)
        setup.assert_not_called()
        self.assertFalse(self.is_installed())
        self.assertIn("gevent", output[0])

    def test_threaded(self):
        setup, register, _output = self.post_load(ENDPOINT)
        self.assertTrue(self.is_installed())
        setup.assert_called_once_with(Config(enabled=True), "threaded")
        self.assertTrue(sdk.is_active())
        # the providers register their own exit handlers too
        register.assert_any_call(hooks.deactivate)

    def test_stop_after_init(self):
        setup, _register, _output = self.post_load(
            ENDPOINT, workers=2, stop_after_init=True
        )
        setup.assert_called_once_with(Config(enabled=True), "threaded")
        self.assertTrue(sdk.is_active())

    def test_prefork_master(self):
        setup, register, _output = self.post_load(ENDPOINT, workers=2)
        self.assertTrue(self.is_installed())
        setup.assert_not_called()
        register.assert_not_called()
        self.assertFalse(sdk.is_active())

    def test_post_load_never_raises(self):
        with (
            patch.dict(os.environ, ENDPOINT, clear=True),
            patch.object(hooks, "install", side_effect=RuntimeError("boom")),
            self.assertLogs(HOOKS_LOGGER, "ERROR"),
        ):
            hooks.post_load()
        self.assertFalse(sdk.is_active())

    def test_activate_never_raises(self):
        with (
            patch.object(hooks.sdk, "setup", side_effect=RuntimeError("boom")),
            self.assertLogs(HOOKS_LOGGER, "ERROR"),
        ):
            hooks.activate(Config(enabled=True), "http")
        self.assertFalse(sdk.is_active())

    def test_deactivate_never_raises(self):
        with (
            patch.object(hooks.sdk, "shutdown", side_effect=RuntimeError("boom")),
            self.assertLogs(HOOKS_LOGGER, "ERROR"),
        ):
            hooks.deactivate()


@tagged("post_install", "-at_install")
class TestWorker(BaseCase):
    """Worker.start / Worker.stop, without forking: the original methods (signals,
    pipes) are replaced by mocks before the instrumentation wraps them."""

    def setUp(self):
        super().setUp()
        self.originals = {}
        for name in ("start", "stop"):
            patcher = patch.object(Worker, name, autospec=True)
            self.originals[name] = patcher.start()
            self.addCleanup(patcher.stop)
        self.events = []
        self.deactivate_error = None
        server_instrumentation.install(self.activate, self.deactivate)
        self.addCleanup(server_instrumentation.uninstall)

    def activate(self, process_type):
        self.events.append(("activate", process_type))

    def deactivate(self):
        self.events.append(("deactivate",))
        if self.deactivate_error:
            raise self.deactivate_error

    def test_process_type(self):
        Worker.start(object.__new__(WorkerHTTP))
        Worker.start(object.__new__(WorkerCron))
        self.assertEqual(self.events, [("activate", "http"), ("activate", "cron")])

    def test_start_then_activate(self):
        worker = object.__new__(WorkerHTTP)
        self.originals["start"].side_effect = lambda w: self.events.append(("start",))
        Worker.start(worker)
        self.originals["start"].assert_called_once_with(worker)
        self.assertEqual(self.events, [("start",), ("activate", "http")])

    def test_stop_deactivates_first(self):
        self.originals["stop"].side_effect = lambda w: self.events.append(("stop",))
        Worker.stop(object.__new__(WorkerCron))
        self.assertEqual(self.events, [("deactivate",), ("stop",)])

    def test_stop_runs_even_if_deactivate_fails(self):
        self.deactivate_error = RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            Worker.stop(object.__new__(WorkerHTTP))
        self.originals["stop"].assert_called_once()

    def test_install_once(self):
        wrapper = Worker.start
        server_instrumentation.install(None, None)
        self.assertIs(Worker.start, wrapper)
