from unittest.mock import patch

import psycopg2
from psycopg2.pool import PoolError

from odoo import sql_db
from odoo.tests import BaseCase, tagged
from odoo.tools import OrderedSet

from .. import sdk
from ..instrumentation import pool as pool_instrumentation
from .common import OtelMixin

READ_WRITE = {
    "db.client.connection.pool.name": "read_write",
    "db.system.name": "postgresql",
}


def make_pool(maxconn, free=0, used=0, readonly=False):
    pool = sql_db.ConnectionPool(maxconn, readonly=readonly)
    pool._free_connections = [object() for _i in range(free)]
    pool._used_connections = OrderedSet(object() for _i in range(used))
    return pool


def key(attributes):
    return tuple(sorted(attributes.items()))


@tagged("post_install", "-at_install")
class TestPool(OtelMixin, BaseCase):
    def setUp(self):
        super().setUp()
        self.setup_otel()

    def points(self, name):
        return {
            key(point.attributes): point.value for point in self.metric_points(name)
        }

    def test_connections(self):
        with (
            patch.object(sql_db, "_Pool", make_pool(8, free=2, used=3)),
            patch.object(sql_db, "_Pool_readonly", None),
        ):
            pool_instrumentation.register(sdk.meter())
            count = self.points("db.client.connection.count")
            maximum = self.points("db.client.connection.max")
        self.assertEqual(
            count,
            {
                key({**READ_WRITE, "db.client.connection.state": "idle"}): 2,
                key({**READ_WRITE, "db.client.connection.state": "used"}): 3,
            },
        )
        self.assertEqual(maximum, {key(READ_WRITE): 8})

    def test_read_only_pool(self):
        with (
            patch.object(sql_db, "_Pool", None),
            patch.object(sql_db, "_Pool_readonly", make_pool(4, used=1, readonly=True)),
        ):
            pool_instrumentation.register(sdk.meter())
            count = self.points("db.client.connection.count")
            maximum = self.points("db.client.connection.max")
        read_only = {**READ_WRITE, "db.client.connection.pool.name": "read_only"}
        self.assertEqual(maximum, {key(read_only): 4})
        self.assertEqual(
            count[key({**read_only, "db.client.connection.state": "used"})], 1
        )

    def test_full(self):
        pool_instrumentation.install()
        self.addCleanup(pool_instrumentation.uninstall)
        with self.assertRaises(PoolError):
            make_pool(1, used=1).borrow({"dbname": "db"})
        self.assertEqual(self.points("odoo.db.pool.full"), {key(READ_WRITE): 1})

    def test_connection_error_is_not_full(self):
        pool_instrumentation.install()
        self.addCleanup(pool_instrumentation.uninstall)
        error = psycopg2.OperationalError("no server")
        with (
            patch.object(sql_db.psycopg2, "connect", side_effect=error),
            self.assertRaises(psycopg2.OperationalError),
        ):
            make_pool(2).borrow({"dbname": "db"})
        self.assertEqual(self.points("odoo.db.pool.full"), {})

    def test_uninstall(self):
        borrow = sql_db.ConnectionPool.borrow
        pool_instrumentation.install()
        self.assertIsNot(sql_db.ConnectionPool.borrow, borrow)
        pool_instrumentation.uninstall()
        self.assertIs(sql_db.ConnectionPool.borrow, borrow)
