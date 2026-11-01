"""Metrics of the process: CPU, memory, threads, file descriptors, uptime and garbage
collector (OpenTelemetry semantic conventions), and the memory limits of Odoo.

Observable instruments registered in each process once its SDK is set up: the
callbacks run in the thread of the metric reader, at each collection, and read the
process they were registered in. Odoo checks the virtual memory of a worker
(``process.memory.virtual``, the resident one on macOS) against
``odoo.worker.memory.limit``.
"""

import gc
import time

import psutil
from opentelemetry.metrics import Observation

from odoo.tools import config as odoo_config

CPU_MODES = ("user", "system")
CONTEXT_SWITCH_TYPES = ("voluntary", "involuntary")
# Odoo option of each memory limit (gevent ones excluded: no SDK in that process)
MEMORY_LIMITS = (("soft", "limit_memory_soft"), ("hard", "limit_memory_hard"))

COUNTER = "create_observable_counter"
UP_DOWN_COUNTER = "create_observable_up_down_counter"
GAUGE = "create_observable_gauge"
# name, kind, callback (method of _Callbacks), unit, description
INSTRUMENTS = (
    (
        "process.cpu.time",
        COUNTER,
        "cpu_time",
        "s",
        "Total CPU seconds broken down by different CPU modes.",
    ),
    (
        "process.memory.usage",
        UP_DOWN_COUNTER,
        "memory_usage",
        "By",
        "The amount of physical memory in use.",
    ),
    (
        "process.memory.virtual",
        UP_DOWN_COUNTER,
        "memory_virtual",
        "By",
        "The amount of committed virtual memory.",
    ),
    (
        "process.thread.count",
        UP_DOWN_COUNTER,
        "thread_count",
        "{thread}",
        "Process threads count.",
    ),
    (
        "process.unix.file_descriptor.count",
        UP_DOWN_COUNTER,
        "file_descriptor_count",
        "{file_descriptor}",
        "Number of unix file descriptors in use by the process.",
    ),
    (
        "process.context_switches",
        COUNTER,
        "context_switches",
        "{context_switch}",
        "Number of times the process has been context switched.",
    ),
    (
        "process.uptime",
        GAUGE,
        "uptime",
        "s",
        "The time the process has been running.",
    ),
    (
        "cpython.gc.collections",
        COUNTER,
        "gc_collections",
        "{collection}",
        "The number of times a generation was collected since interpreter start.",
    ),
    (
        "cpython.gc.collected_objects",
        COUNTER,
        "gc_collected_objects",
        "{object}",
        "The total number of objects collected since interpreter start.",
    ),
    (
        "cpython.gc.uncollectable_objects",
        COUNTER,
        "gc_uncollectable_objects",
        "{object}",
        "The total number of uncollectable objects found since interpreter start.",
    ),
    (
        "odoo.worker.memory.limit",
        UP_DOWN_COUNTER,
        "memory_limit",
        "By",
        (
            "The memory limits of Odoo: soft, the worker is recycled after its "
            "request; hard, the allocation fails (Linux)."
        ),
    ),
)


def register(meter):
    """Create the observable instruments of the current process on ``meter``."""
    callbacks = _Callbacks(psutil.Process())
    for name, kind, callback, unit, description in INSTRUMENTS:
        if callback == "file_descriptor_count" and not hasattr(
            callbacks.process, "num_fds"
        ):
            continue  # not on Windows
        getattr(meter, kind)(
            name, [getattr(callbacks, callback)], unit=unit, description=description
        )


class _Callbacks:
    """The callbacks of the instruments, reading ``process``."""

    def __init__(self, process):
        self.process = process

    def cpu_time(self, _options):
        times = self.process.cpu_times()
        for mode in CPU_MODES:
            yield Observation(getattr(times, mode), {"cpu.mode": mode})

    def memory_usage(self, _options):
        yield Observation(self.process.memory_info().rss)

    def memory_virtual(self, _options):
        yield Observation(self.process.memory_info().vms)

    def thread_count(self, _options):
        yield Observation(self.process.num_threads())

    def file_descriptor_count(self, _options):
        yield Observation(self.process.num_fds())

    def context_switches(self, _options):
        switches = self.process.num_ctx_switches()
        for switch_type in CONTEXT_SWITCH_TYPES:
            yield Observation(
                getattr(switches, switch_type),
                {"process.context_switch.type": switch_type},
            )

    def uptime(self, _options):
        yield Observation(time.time() - self.process.create_time())

    def gc_collections(self, _options):
        return _gc_stat("collections")

    def gc_collected_objects(self, _options):
        return _gc_stat("collected")

    def gc_uncollectable_objects(self, _options):
        return _gc_stat("uncollectable")

    def memory_limit(self, _options):
        for limit_type, option in MEMORY_LIMITS:
            if limit := odoo_config[option]:
                yield Observation(limit, {"odoo.limit.type": limit_type})


def _gc_stat(key):
    return [
        Observation(stats[key], {"cpython.gc.generation": generation})
        for generation, stats in enumerate(gc.get_stats())
    ]
