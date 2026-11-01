from odoo.exceptions import UserError
from odoo.tests import HttpCase, JsonRpcException, TransactionCase, tagged
from odoo.tools import SQL, mute_logger

from .. import sdk
from ..config import Config
from ..instrumentation import rpc as rpc_instrumentation
from ..instrumentation import sql as sql_instrumentation
from .common import OtelMixin
from .test_http import InstrumentationMixin


@tagged("post_install", "-at_install")
class TestRpcMetrics(InstrumentationMixin, HttpCase):
    def setUp(self):
        super().setUp()
        self.authenticate("admin", "admin")

    def calls(self):
        return {
            tuple(sorted(point.attributes.items())): point
            for point in self.metric_points("odoo.rpc.duration")
        }

    def test_duration(self):
        self.call_kw("res.partner", "search_read", kwargs={"limit": 1})
        ((attributes, point),) = self.calls().items()
        self.assertEqual(
            dict(attributes),
            {"odoo.model": "res.partner", "odoo.method": "search_read"},
        )
        self.assertEqual(point.count, 1)
        self.assertEqual(
            tuple(point.explicit_bounds), rpc_instrumentation.DURATION_BUCKETS
        )

    @mute_logger("odoo.http", "odoo.http.router")
    def test_server_error(self):
        with (
            self.patch_search_read(ValueError("boom")),
            self.assertRaises(JsonRpcException),
        ):
            self.call_kw("res.partner", "search_read")
        ((attributes, _point),) = self.calls().items()
        self.assertEqual(
            dict(attributes),
            {
                "odoo.model": "res.partner",
                "odoo.method": "_OTHER",
                "error.type": "builtins.ValueError",
            },
        )

    @mute_logger("odoo.http", "odoo.http.router")
    def test_user_error_is_not_an_error(self):
        with (
            self.patch_search_read(UserError("no")),
            self.assertRaises(JsonRpcException),
        ):
            self.call_kw("res.partner", "search_read")
        ((attributes, _point),) = self.calls().items()
        self.assertEqual(
            dict(attributes), {"odoo.model": "res.partner", "odoo.method": "_OTHER"}
        )

    def test_metrics_without_traces(self):
        self.setup_otel(Config(enabled=True, traces=False))
        self.call_kw("res.partner", "search_read", kwargs={"limit": 1})
        self.assertEqual(self.finished_spans(), ())
        self.assertEqual(len(self.calls()), 1)


@tagged("post_install", "-at_install")
class TestSqlMetrics(OtelMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.setup_otel()

    def execute(self, query, config=None):
        config = config or Config(enabled=True, sql="off")
        with (
            sdk.tracer().start_as_current_span("unit") as unit,
            sql_instrumentation.track(unit, config),
        ):
            self.env.cr.execute(query)
        return {
            tuple(sorted(point.attributes.items())): point
            for point in self.metric_points("db.client.operation.duration")
        }

    def test_duration(self):
        points = self.execute("SELECT id FROM res_partner LIMIT 1")
        point = points[
            (
                ("db.namespace", self.env.cr.dbname),
                ("db.operation.name", "SELECT"),
                ("db.system.name", "postgresql"),
            )
        ]
        self.assertGreaterEqual(point.count, 1)
        self.assertGreater(point.sum, 0)
        self.assertEqual(
            tuple(point.explicit_bounds), sql_instrumentation.DURATION_BUCKETS
        )

    def test_sql_object(self):
        points = self.execute(
            SQL("UPDATE res_partner SET active = active WHERE id = 0")
        )
        self.assertIn(
            "UPDATE",
            [dict(attributes).get("db.operation.name") for attributes in points],
        )

    def test_without_metrics(self):
        self.setup_otel(Config(enabled=True, metrics=False))
        tracker = sql_instrumentation._Tracker(sdk.config())
        self.assertIsNone(tracker.histogram)

    def test_nested(self):
        # a job run manually: its queries are tracked in the request too
        config = Config(enabled=True, sql="off")
        with (
            sdk.tracer().start_as_current_span("request") as request,
            sql_instrumentation.track(request, config),
            sdk.tracer().start_as_current_span("job") as job,
            sql_instrumentation.track(job, config) as inner,
        ):
            self.env.cr.execute("SELECT 1")
        self.assertIsNone(inner.histogram)
        (point,) = self.metric_points("db.client.operation.duration")
        self.assertEqual(point.count, 1)

    def test_no_query_span_without_traces(self):
        self.setup_otel(Config(enabled=True, traces=False))
        tracker = sql_instrumentation._Tracker(Config(traces=False, sql="all"))
        self.assertEqual(tracker.mode, "off")

    def test_operation(self):
        operation = sql_instrumentation.operation_name
        self.assertEqual(operation("  select 1"), "SELECT")
        self.assertEqual(operation(b"INSERT INTO t VALUES (1)"), "INSERT")
        self.assertIsNone(operation(object()))
        self.assertIsNone(operation("-- comment"))
