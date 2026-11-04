import threading
from datetime import timedelta
from unittest.mock import patch

import psycopg2
from opentelemetry.trace import SpanKind, StatusCode
from psycopg2 import sql as psql

from odoo import fields
from odoo.tests import TransactionCase, tagged
from odoo.tools import SQL, mute_logger

from odoo.addons.base.models import ir_cron as ir_cron_module
from odoo.addons.base.models.ir_cron import CONSECUTIVE_TIMEOUT_FOR_FAILURE, IrCron

from .. import sdk
from ..config import Config
from ..instrumentation import cron as cron_instrumentation
from ..instrumentation import errors
from ..instrumentation import sql as sql_instrumentation
from ..instrumentation._patch import is_patched
from .common import OtelMixin

QUERY = "SELECT id FROM res_partner WHERE name = 'secret' AND id = %s"


@tagged("post_install", "-at_install")
class TestSql(OtelMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.setup_otel()

    def run_query(self, mode, slow_ms=100.0, query=QUERY, params=(424242,)):
        config = Config(enabled=True, sql=mode, sql_slow_ms=slow_ms)
        tracer = sdk.tracer()
        with (
            tracer.start_as_current_span("unit") as unit,
            sql_instrumentation.track(unit, config) as tracker,
        ):
            self.env.cr.execute(query, params)
        spans = self.finished_spans()
        unit = next(span for span in spans if span.name == "unit")
        queries = [span for span in spans if span.kind == SpanKind.CLIENT]
        return unit, queries, tracker

    def test_all(self):
        unit, queries, _tracker = self.run_query("all")
        (span,) = [span for span in queries if span.name == "SELECT res_partner"]
        self.assertEqual(span.parent.span_id, unit.context.span_id)
        attributes = span.attributes
        self.assertEqual(attributes["db.system.name"], "postgresql")
        self.assertEqual(attributes["db.operation.name"], "SELECT")
        self.assertEqual(attributes["db.collection.name"], "res_partner")
        self.assertEqual(attributes["db.namespace"], self.env.cr.dbname)
        self.assertEqual(
            attributes["db.query.text"],
            "SELECT id FROM res_partner WHERE name = ? AND id = %s",
        )
        self.assertNotIn("424242", str(dict(attributes)))
        self.assertGreater(span.end_time, span.start_time)
        self.assertGreaterEqual(unit.attributes["odoo.sql.count"], 1)
        self.assertGreater(unit.attributes["odoo.sql.duration_ms"], 0)

    def test_slow(self):
        _unit, queries, tracker = self.run_query("slow", slow_ms=60_000)
        self.assertEqual(queries, [])
        self.assertGreaterEqual(tracker.count, 1)
        _unit, queries, _tracker = self.run_query("slow", slow_ms=0)
        self.assertTrue([span for span in queries if span.name == "SELECT res_partner"])

    def test_off(self):
        unit, queries, _tracker = self.run_query("off")
        self.assertEqual(queries, [])
        self.assertGreaterEqual(unit.attributes["odoo.sql.count"], 1)

    def test_sql_object(self):
        _unit, queries, _tracker = self.run_query(
            "all",
            query=SQL("SELECT id FROM res_partner WHERE id = %s", 424242),
            params=None,
        )
        (span,) = [span for span in queries if span.name == "SELECT res_partner"]
        self.assertEqual(
            span.attributes["db.query.text"], "SELECT id FROM res_partner WHERE id = %s"
        )

    def test_failed_query(self):
        config = Config(enabled=True, sql="all")
        with (
            sdk.tracer().start_as_current_span("unit") as unit,
            sql_instrumentation.track(unit, config) as tracker,
            self.assertRaises(psycopg2.Error),
            mute_logger("odoo.sql_db"),
            self.env.cr.savepoint(),
        ):
            self.env.cr.execute("SELECT id FROM monitoring_no_such_table")
        self.assertGreaterEqual(tracker.count, 1)
        self.assertTrue(
            [
                span
                for span in self.finished_spans()
                if span.name == "SELECT monitoring_no_such_table"
            ]
        )

    def test_hooks_restored(self):
        thread = threading.current_thread()
        hooks = vars(thread).setdefault("query_hooks", [])
        before = list(hooks)
        config = Config(enabled=True, sql="off")
        calls = []

        def other_hook(*args):  # another hook (the profiler), added meanwhile
            calls.append(args)

        with (
            sdk.tracer().start_as_current_span("outer") as outer,
            sql_instrumentation.track(outer, config) as outer_tracker,
        ):
            hooks.append(other_hook)
            with sql_instrumentation.track(outer, config) as inner_tracker:
                self.env.cr.execute("SELECT 1")
            self.env.cr.execute("SELECT 1")
        hooks.remove(other_hook)
        self.assertEqual(hooks, before)
        self.assertEqual(inner_tracker.count, 1)
        self.assertEqual(outer_tracker.count, 2)
        self.assertEqual(len(calls), 2)

    def test_query_text(self):
        query_text = sql_instrumentation.query_text
        self.assertEqual(query_text("SELECT 'it''s', %s"), "SELECT ?, %s")
        self.assertEqual(
            query_text(
                "SELECT x FROM res_partner_2 WHERE id IN (1, 2.5, 3e10) -- 'a\n"
                "AND b = E'c\\'d' /* 'e */ AND f = $t$ g $t$ AND h = $$ i $$"
            ),
            "SELECT x FROM res_partner_2 WHERE id IN (?, ?, ?) \n"
            "AND b = ?  AND f = ? AND h = ?",
        )
        # a literal cut by the truncation of a large query is masked too
        cut = query_text("INSERT INTO t VALUES " + "(1, 'secret'), " * 2000)
        self.assertNotIn("secret", cut)
        self.assertNotIn("1", cut)
        self.assertEqual(query_text(b"SELECT x"), "SELECT x")
        self.assertIsNone(query_text(psql.SQL("SELECT 1")))
        self.assertEqual(
            len(query_text("SELECT " + "x" * 5000)),
            sql_instrumentation.QUERY_TEXT_MAX_LENGTH,
        )


@tagged("post_install", "-at_install")
class TestCron(OtelMixin, TransactionCase):
    def setUp(self):
        super().setUp()
        self.setup_otel()
        cron_instrumentation.install()
        self.addCleanup(cron_instrumentation.uninstall)

    def process_job(self, code, **job_values):
        cron = self.env["ir.cron"].create(
            {
                "name": "Monitoring OpenTelemetry test cron",
                "state": "code",
                "code": code,
                "model_id": self.env.ref("base.model_res_partner").id,
                "user_id": self.env.uid,
                "interval_number": 1,
                "interval_type": "days",
            }
        )
        job = {
            **cron.read(load=None)[0],
            "done": 0,
            "remaining": 0,
            "timed_out_counter": 0,
            "progress_id": 0,
            **job_values,
        }
        self.env.flush_all()
        with self.enter_registry_test_mode(), self.registry.cursor() as cr:
            self.registry["ir.cron"]._process_job(cr, job)
        return cron, [
            span for span in self.finished_spans() if span.name.startswith("cron ")
        ]

    def test_job(self):
        cron, (span,) = self.process_job("model.search([], limit=1)")
        self.assertNotIn("odoo.cron.timed_out", span.attributes)
        self.assertEqual(span.name, "cron Monitoring OpenTelemetry test cron")
        self.assertEqual(span.kind, SpanKind.INTERNAL)
        self.assertIsNone(span.parent)
        attributes = span.attributes
        self.assertEqual(attributes["odoo.cron.id"], cron.id)
        self.assertEqual(attributes["odoo.cron.status"], "fully done")
        self.assertEqual(attributes["odoo.db"], self.env.cr.dbname)
        self.assertGreater(attributes["odoo.sql.count"], 0)
        self.assertEqual(span.status.status_code, StatusCode.UNSET)

    @mute_logger("odoo.addons.base.models.ir_cron")
    def test_failed_job(self):
        _cron, (span,) = self.process_job("1 / 0")
        self.assertEqual(span.attributes["odoo.cron.status"], "failed")
        self.assertEqual(span.status.status_code, StatusCode.ERROR)
        # the exception logged by Odoo without raising it is on the span
        (event,) = [event for event in span.events if event.name == "exception"]
        self.assertEqual(
            event.attributes["exception.type"], "builtins.ZeroDivisionError"
        )

    @mute_logger("odoo.addons.base.models.ir_cron")
    def test_user_error_job(self):
        # seen by nobody in a job: a failure, with its exception
        _cron, (span,) = self.process_job("raise UserError('boom')")
        self.assertEqual(
            span.attributes["odoo.exception.type"], "odoo.exceptions.UserError"
        )
        self.assertEqual(span.status.status_code, StatusCode.ERROR)
        self.assertEqual(
            len([event for event in span.events if event.name == "exception"]), 1
        )

    @mute_logger("odoo.addons.base.models.ir_cron")
    def test_partially_done_job(self):
        # failed after committing progress: an exception, not a failure; Odoo runs
        # such a job again for MIN_TIME_PER_JOB seconds
        self.patch(ir_cron_module, "MIN_TIME_PER_JOB", 0)
        _cron, (span,) = self.process_job(
            "env['ir.cron']._commit_progress(processed=1, remaining=1)\n1 / 0"
        )
        self.assertEqual(span.attributes["odoo.cron.status"], "partially done")
        self.assertNotEqual(span.status.status_code, StatusCode.ERROR)
        self.assertTrue([event for event in span.events if event.name == "exception"])

    def test_callback_outside_job(self):
        # no span of a job: the exception is raised, unchanged
        cron, spans = self.process_job("model.search([], limit=1)")
        cron.ir_actions_server_id.code = "1 / 0"
        self.env.flush_all()
        with (
            self.enter_registry_test_mode(),
            self.registry.cursor() as cr,
            self.assertRaises(ZeroDivisionError),
        ):
            cron.with_env(cron.env(cr=cr))._callback(
                cron.name, cron.ir_actions_server_id.id
            )
        self.assertEqual(len(self.finished_spans()), len(spans))

    @mute_logger("odoo.addons.base.models.ir_cron")
    def test_instrumentation_failure(self):
        # a failure of the instrumentation never replaces the exception of the job
        with patch.object(
            errors, "add_event", side_effect=RuntimeError("instrumentation")
        ):
            _cron, (span,) = self.process_job("1 / 0")
        self.assertEqual(span.attributes["odoo.cron.status"], "failed")

    def test_uninstall(self):
        self.assertTrue(is_patched(IrCron, "_callback"))
        cron_instrumentation.uninstall()
        self.assertFalse(is_patched(IrCron, "_callback"))
        self.assertFalse(is_patched(IrCron, "_process_job"))
        cron_instrumentation.install()

    @mute_logger("odoo.addons.base.models.ir_cron")
    def test_timed_out_job(self):
        _cron, (span,) = self.process_job(
            "model.search([], limit=1)",
            timed_out_counter=CONSECUTIVE_TIMEOUT_FOR_FAILURE,
        )
        self.assertEqual(span.attributes["odoo.cron.status"], "failed")
        self.assertIs(span.attributes["odoo.cron.timed_out"], True)
        self.assertEqual(span.status.status_code, StatusCode.ERROR)

    def test_manual_run(self):
        # method_direct_trigger runs the job in the request: a child span
        with sdk.tracer().start_as_current_span("request") as request:
            _cron, (span,) = self.process_job("model.search([], limit=1)")
        self.assertEqual(span.parent.span_id, request.get_span_context().span_id)
        self.assertEqual(span.context.trace_id, request.get_span_context().trace_id)

    def test_inactive(self):
        sdk.shutdown()
        _cron, spans = self.process_job("model.search([], limit=1)")
        self.assertEqual(spans, [])

    def cron_points(self, name):
        return {
            tuple(sorted(point.attributes.items())): point
            for point in self.metric_points(name)
        }

    def test_duration(self):
        self.process_job("model.search([], limit=1)")
        ((attributes, point),) = self.cron_points("odoo.cron.duration").items()
        self.assertEqual(
            dict(attributes),
            {
                "odoo.cron.name": "Monitoring OpenTelemetry test cron",
                "odoo.cron.status": "fully done",
            },
        )
        self.assertEqual(point.count, 1)
        self.assertEqual(
            tuple(point.explicit_bounds), cron_instrumentation.DURATION_BUCKETS
        )

    @mute_logger("odoo.addons.base.models.ir_cron")
    def test_failed_duration(self):
        self.process_job("1 / 0")
        ((attributes, _point),) = self.cron_points("odoo.cron.duration").items()
        self.assertEqual(dict(attributes)["odoo.cron.status"], "failed")

    def test_lag(self):
        nextcall = fields.Datetime.now() - timedelta(seconds=100)
        self.process_job("model.search([], limit=1)", nextcall=nextcall)
        ((attributes, point),) = self.cron_points("odoo.cron.lag").items()
        self.assertEqual(
            dict(attributes), {"odoo.cron.name": "Monitoring OpenTelemetry test cron"}
        )
        self.assertGreaterEqual(point.sum, 100)
        self.assertLess(point.sum, 600)
        self.assertEqual(tuple(point.explicit_bounds), cron_instrumentation.LAG_BUCKETS)
        # a lag of a few minutes is known to the half minute: in (90, 120]
        self.assertEqual(point.count, 1)
        self.assertEqual(point.bucket_counts[point.explicit_bounds.index(120)], 1)

    def test_no_lag_before_nextcall(self):
        # run by a trigger before its nextcall: no known due time
        nextcall = fields.Datetime.now() + timedelta(hours=1)
        self.process_job("model.search([], limit=1)", nextcall=nextcall)
        self.assertEqual(self.cron_points("odoo.cron.lag"), {})
        self.assertEqual(len(self.cron_points("odoo.cron.duration")), 1)

    def test_no_lag_when_run_manually(self):
        # with or without the traces: the HTTP span may be a no-op one
        self.setup_otel(Config(enabled=True, traces=False))
        nextcall = fields.Datetime.now() - timedelta(days=2)
        token = cron_instrumentation._manual.set(True)
        try:
            self.process_job("model.search([], limit=1)", nextcall=nextcall)
        finally:
            cron_instrumentation._manual.reset(token)
        self.assertEqual(self.cron_points("odoo.cron.lag"), {})
        self.assertEqual(len(self.cron_points("odoo.cron.duration")), 1)

    def test_method_direct_trigger_is_manual(self):
        seen = []
        trigger = cron_instrumentation._make_method_direct_trigger(
            lambda cron: seen.append(cron_instrumentation._manual.get())
        )
        trigger(self.env["ir.cron"])
        self.assertEqual(seen, [True])
        self.assertFalse(cron_instrumentation._manual.get())

    def test_no_lag_after_partially_done(self):
        # rescheduled at once by a trigger: the nextcall is the original one
        nextcall = fields.Datetime.now() - timedelta(minutes=30)
        self.process_job("model.search([], limit=1)", nextcall=nextcall, remaining=5)
        self.assertEqual(self.cron_points("odoo.cron.lag"), {})

    def test_metrics_without_traces(self):
        self.setup_otel(Config(enabled=True, traces=False))
        _cron, spans = self.process_job("model.search([], limit=1)")
        self.assertEqual(spans, [])
        self.assertEqual(len(self.cron_points("odoo.cron.duration")), 1)
