import gc
import os
from unittest.mock import patch

import psutil

from odoo.tests import BaseCase, tagged
from odoo.tools import config as odoo_config

from .. import hooks, sdk
from ..config import Config
from ..instrumentation import pool as pool_instrumentation
from ..instrumentation import process as process_instrumentation
from .common import OtelMixin, in_memory_reader

GIB = 1024**3
MIB = 1024**2


@tagged("post_install", "-at_install")
class TestProcess(OtelMixin, BaseCase):
    def setUp(self):
        super().setUp()
        self.setup_otel()

    def collect(self, limits=None):
        options = limits or {"limit_memory_soft": 2 * GIB, "limit_memory_hard": 3 * GIB}
        with patch.dict(odoo_config.options, options):
            process_instrumentation.register(sdk.meter())
            metrics, _resource = self.collect_metrics()
        return metrics

    def points(self, metric):
        return {
            tuple(sorted(point.attributes.items())): point.value
            for point in metric.data.data_points
        }

    def test_names(self):
        metrics = self.collect()
        self.assertEqual(
            {
                name: (metric.unit, type(metric.data).__name__)
                for name, metric in metrics.items()
            },
            {
                "process.cpu.time": ("s", "Sum"),
                "process.memory.usage": ("By", "Sum"),
                "process.memory.virtual": ("By", "Sum"),
                "process.thread.count": ("{thread}", "Sum"),
                "process.unix.file_descriptor.count": ("{file_descriptor}", "Sum"),
                "process.context_switches": ("{context_switch}", "Sum"),
                "process.uptime": ("s", "Gauge"),
                "cpython.gc.collections": ("{collection}", "Sum"),
                "cpython.gc.collected_objects": ("{object}", "Sum"),
                "cpython.gc.uncollectable_objects": ("{object}", "Sum"),
                "odoo.worker.memory.limit": ("By", "Sum"),
            },
        )
        for name in ("process.cpu.time", "process.context_switches"):
            self.assertTrue(metrics[name].data.is_monotonic, name)
        for name in ("process.memory.usage", "odoo.worker.memory.limit"):
            self.assertFalse(metrics[name].data.is_monotonic, name)

    def test_values(self):
        metrics = self.collect()
        memory = psutil.Process(os.getpid()).memory_info()
        (rss,) = self.points(metrics["process.memory.usage"]).values()
        self.assertAlmostEqual(rss, memory.rss, delta=64 * MIB)
        (vms,) = self.points(metrics["process.memory.virtual"]).values()
        self.assertAlmostEqual(vms, memory.vms, delta=64 * MIB)
        cpu = self.points(metrics["process.cpu.time"])
        self.assertEqual(set(cpu), {(("cpu.mode", "user"),), (("cpu.mode", "system"),)})
        self.assertGreater(cpu[(("cpu.mode", "user"),)], 0)
        switches = self.points(metrics["process.context_switches"])
        self.assertEqual(
            set(switches),
            {
                (("process.context_switch.type", "voluntary"),),
                (("process.context_switch.type", "involuntary"),),
            },
        )
        (threads,) = self.points(metrics["process.thread.count"]).values()
        self.assertGreaterEqual(threads, 1)
        (fds,) = self.points(metrics["process.unix.file_descriptor.count"]).values()
        self.assertGreater(fds, 0)
        (uptime,) = self.points(metrics["process.uptime"]).values()
        self.assertIsInstance(uptime, float)
        self.assertGreater(uptime, 0)
        collections = self.points(metrics["cpython.gc.collections"])
        self.assertEqual(
            set(collections),
            {(("cpython.gc.generation", generation),) for generation in range(3)},
        )
        self.assertGreaterEqual(
            gc.get_stats()[0]["collections"],
            collections[(("cpython.gc.generation", 0),)],
        )

    def test_memory_limits(self):
        limits = self.points(self.collect()["odoo.worker.memory.limit"])
        self.assertEqual(
            limits,
            {
                (("odoo.limit.type", "soft"),): 2 * GIB,
                (("odoo.limit.type", "hard"),): 3 * GIB,
            },
        )

    def test_no_memory_limit(self):
        metrics = self.collect({"limit_memory_soft": 0, "limit_memory_hard": 0})
        self.assertNotIn("odoo.worker.memory.limit", metrics)


@tagged("post_install", "-at_install")
class TestHooksProcess(OtelMixin, BaseCase):
    def activate(self, config):
        with (
            patch.object(sdk, "PeriodicExportingMetricReader", in_memory_reader),
            patch.object(sdk, "OTLPLogExporter"),
            patch.object(process_instrumentation, "register") as register,
        ):
            hooks.activate(config, "http")
        self.addCleanup(hooks.deactivate)
        return register

    def test_activate(self):
        with patch.object(pool_instrumentation, "register") as pool_register:
            register = self.activate(Config(enabled=True, traces=False, logs=False))
        register.assert_called_once_with(sdk.meter())
        pool_register.assert_called_once_with(sdk.meter())

    def test_activate_without_metrics(self):
        register = self.activate(Config(enabled=True, traces=False, metrics=False))
        register.assert_not_called()

    def test_failure_keeps_the_sdk(self):
        with (
            patch.object(sdk, "PeriodicExportingMetricReader", in_memory_reader),
            patch.object(
                process_instrumentation, "register", side_effect=RuntimeError("boom")
            ),
            self.assertLogs(hooks._logger.name, "ERROR"),
        ):
            hooks.activate(Config(enabled=True, traces=False, logs=False), "http")
        self.addCleanup(hooks.deactivate)
        self.assertTrue(sdk.is_active())
