"""Metrics of the PostgreSQL connection pools of the process (semantic conventions).

Odoo keeps two pools per process, created on first use (``odoo.sql_db``): the
read/write one and the read-only one (``db_replica_host``), of ``db_maxconn``
connections each. ``db.client.connection.count`` (by state, ``idle`` or ``used``) and
``db.client.connection.max`` are read at each collection; ``odoo.db.pool.full`` counts
the connections refused because the pool is full (``PoolError``, raised at once
rather than after a timeout). The counts are read without the lock of the pool: a
connection being given back may be counted in neither state.
"""

from opentelemetry.metrics import Observation
from psycopg2.pool import PoolError

from odoo import sql_db

from .. import sdk
from ._patch import patch, unpatch


def pool_attributes(pool):
    return {
        "db.system.name": "postgresql",
        "db.client.connection.pool.name": "read_only"
        if pool.readonly
        else "read_write",
    }


def _pools():
    return [pool for pool in (sql_db._Pool, sql_db._Pool_readonly) if pool is not None]


def _count(_options):
    for pool in _pools():
        attributes = pool_attributes(pool)
        for state, connections in (
            ("idle", pool._free_connections),
            ("used", pool._used_connections),
        ):
            yield Observation(
                len(connections), {**attributes, "db.client.connection.state": state}
            )


def _max(_options):
    for pool in _pools():
        yield Observation(pool._maxconn, pool_attributes(pool))


def register(meter):
    """Create the observable instruments of the pools of the process on ``meter``."""
    meter.create_observable_up_down_counter(
        "db.client.connection.count",
        [_count],
        unit="{connection}",
        description=(
            "The number of connections that are currently in state described by "
            "the state attribute."
        ),
    )
    meter.create_observable_up_down_counter(
        "db.client.connection.max",
        [_max],
        unit="{connection}",
        description="The maximum number of open connections allowed.",
    )


def _full(meter):
    return meter.create_counter(
        "odoo.db.pool.full",
        unit="{connection}",
        description="The connections refused because the pool is full.",
    )


def _make_borrow(original):
    def borrow(self, *args, **kwargs):
        try:
            return original(self, *args, **kwargs)
        except PoolError:
            # borrow raises it only when the pool is full
            if counter := sdk.instrument(_full):
                counter.add(1, pool_attributes(self))
            raise

    return borrow


def install():
    patch(sql_db.ConnectionPool, "borrow", _make_borrow)


def uninstall():
    unpatch(sql_db.ConnectionPool, "borrow")
