from types import SimpleNamespace
from unittest.mock import Mock, patch

from opentelemetry.trace import SpanKind, StatusCode

from odoo import api
from odoo.exceptions import ConcurrencyError, UserError
from odoo.http import retrying as retrying_module
from odoo.http import router
from odoo.tests import BaseCase, HttpCase, JsonRpcException, TransactionCase, tagged
from odoo.tools import mute_logger

from odoo.addons.web.controllers import dataset

from .. import sdk
from ..config import Config
from ..instrumentation import _patch, errors
from ..instrumentation import http as http_instrumentation
from ..instrumentation import rpc as rpc_instrumentation
from .common import OtelMixin

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
PARENT_ID = "b7ad6b7169203331"


class InstrumentationMixin(OtelMixin):
    def setUp(self):
        super().setUp()
        self.setup_otel()
        http_instrumentation.install()
        rpc_instrumentation.install()
        self.addCleanup(http_instrumentation.uninstall)
        self.addCleanup(rpc_instrumentation.uninstall)

    def patch_search_read(self, exception):
        @api.model
        def search_read(self, *args, **kwargs):
            raise exception

        Partner = type(self.env["res.partner"])
        return patch.object(Partner, "search_read", search_read)

    def call_kw(self, model, method, args=(), kwargs=None):
        return self.make_jsonrpc_request(
            f"/web/dataset/call_kw/{model}/{method}",
            {
                "model": model,
                "method": method,
                "args": list(args),
                "kwargs": kwargs or {},
            },
        )


@tagged("post_install", "-at_install")
class TestHttp(InstrumentationMixin, HttpCase):
    def server_spans(self):
        return [s for s in self.finished_spans() if s.kind == SpanKind.SERVER]

    def test_route(self):
        response = self.url_open("/web/login?redirect=%2Fodoo%3Ftoken%3Dsecret")
        self.assertEqual(response.status_code, 200)
        (span,) = self.server_spans()
        self.assertEqual(span.name, "GET /web/login")
        attributes = span.attributes
        self.assertEqual(attributes["http.request.method"], "GET")
        self.assertEqual(attributes["http.route"], "/web/login")
        self.assertEqual(attributes["url.path"], "/web/login")
        self.assertEqual(attributes["http.response.status_code"], 200)
        self.assertEqual(attributes["odoo.route_type"], "http")
        self.assertEqual(attributes["odoo.db"], self.env.cr.dbname)
        self.assertIn("url.scheme", attributes)
        self.assertIn("server.address", attributes)
        self.assertGreater(attributes["odoo.sql.count"], 0)
        self.assertIn("odoo.sql.duration_ms", attributes)
        self.assertEqual(span.status.status_code, StatusCode.UNSET)
        # never the query string
        self.assertFalse(
            [key for key, value in attributes.items() if "secret" in str(value)]
        )

    def test_route_with_arguments(self):
        """The path of a route with arguments may hold a token: only the template
        is recorded."""
        self.url_open("/odoo/action-secret", allow_redirects=False)
        (span,) = self.server_spans()
        self.assertEqual(span.attributes["http.route"], "/odoo/<path:subpath>")
        self.assertNotIn("url.path", span.attributes)

    def test_not_found(self):
        response = self.url_open("/monitoring-opentelemetry/missing")
        self.assertEqual(response.status_code, 404)
        (span,) = self.server_spans()
        self.assertEqual(span.name, "GET")
        self.assertNotIn("http.route", span.attributes)
        self.assertNotIn("url.path", span.attributes)
        self.assertEqual(span.attributes["http.response.status_code"], 404)
        self.assertEqual(span.status.status_code, StatusCode.UNSET)

    @mute_logger("odoo.http")  # no CSRF token
    def test_unknown_method(self):
        self.url_open("/web/login", method="PROPFIND")
        (span,) = self.server_spans()
        self.assertEqual(span.name, "HTTP /web/login")
        self.assertEqual(span.attributes["http.request.method"], "_OTHER")
        self.assertEqual(span.attributes["http.request.method_original"], "PROPFIND")

    def test_excluded(self):
        self.url_open("/web/static/img/favicon.ico")
        self.url_open("/websocket")
        self.assertEqual(self.server_spans(), [])

    def test_excluded_paths_config(self):
        self.setup_otel(Config(enabled=True, excluded_paths=("/web/login",)))
        self.url_open("/web/login")
        self.assertEqual(self.server_spans(), [])

    def test_inactive(self):
        sdk.shutdown()
        self.assertEqual(self.url_open("/web/login").status_code, 200)
        self.assertEqual(self.span_exporter.get_finished_spans(), ())

    def test_traceparent(self):
        self.url_open(
            "/web/login", headers={"traceparent": f"00-{TRACE_ID}-{PARENT_ID}-01"}
        )
        (span,) = self.server_spans()
        self.assertEqual(span.context.trace_id, int(TRACE_ID, 16))
        self.assertEqual(span.parent.span_id, int(PARENT_ID, 16))
        self.assertTrue(span.parent.is_remote)

    def test_call_kw(self):
        self.authenticate("admin", "admin")
        self.span_exporter.clear()
        self.call_kw("res.partner", "search_read", kwargs={"limit": 1})
        (server_span,) = self.server_spans()
        self.assertEqual(server_span.name, "POST /web/dataset/call_kw/<path:path>")
        self.assertEqual(server_span.attributes["odoo.route_type"], "jsonrpc")
        self.assertEqual(
            server_span.attributes["odoo.uid"], self.env.ref("base.user_admin").id
        )
        (span,) = [s for s in self.finished_spans() if s.kind == SpanKind.INTERNAL]
        self.assertEqual(span.name, "res.partner.search_read")
        self.assertEqual(span.attributes["odoo.model"], "res.partner")
        self.assertEqual(span.attributes["odoo.method"], "search_read")
        self.assertEqual(span.parent.span_id, server_span.context.span_id)

    @mute_logger("odoo.http", "odoo.http.router")
    def test_call_kw_unknown_method(self):
        """A method that does not exist does not name the span."""
        self.authenticate("admin", "admin")
        self.span_exporter.clear()
        with self.assertRaises(JsonRpcException):
            self.call_kw("res.partner", "no_such_method_x")
        (span,) = [s for s in self.finished_spans() if s.kind == SpanKind.INTERNAL]
        self.assertEqual(span.name, "res.partner._OTHER")

    @mute_logger("odoo.http", "odoo.http.router")
    def test_user_error(self):
        """An error of the user is named, but neither an error nor an exception."""
        self.authenticate("admin", "admin")
        self.span_exporter.clear()
        with (
            self.patch_search_read(UserError("wrong")),
            self.assertRaises(JsonRpcException),
        ):
            self.call_kw("res.partner", "search_read")
        spans = self.finished_spans()
        self.assertEqual(len(spans), 2)
        for span in spans:
            self.assertEqual(span.status.status_code, StatusCode.UNSET)
            self.assertEqual(
                span.attributes["odoo.exception.type"], "odoo.exceptions.UserError"
            )
            self.assertNotIn("error.type", span.attributes)
            self.assertEqual(span.events, ())

    @mute_logger("odoo.http", "odoo.http.router")
    def test_server_error(self):
        """A server error sets the spans in error, even with a JSON-RPC 200; the
        exception event is on the innermost span only, with the first line of the
        message."""
        self.authenticate("admin", "admin")
        self.span_exporter.clear()
        with (
            self.patch_search_read(ValueError("boom\nDETAIL: secret value")),
            self.assertRaises(JsonRpcException),
        ):
            self.call_kw("res.partner", "search_read")
        (server_span,) = self.server_spans()
        (call_span,) = [s for s in self.finished_spans() if s.kind == SpanKind.INTERNAL]
        for span in (server_span, call_span):
            self.assertEqual(span.status.status_code, StatusCode.ERROR)
            self.assertEqual(span.attributes["error.type"], "builtins.ValueError")
        self.assertEqual(server_span.events, ())
        (event,) = call_span.events
        self.assertEqual(event.name, "exception")
        self.assertEqual(event.attributes["exception.message"], "boom")
        self.assertNotIn("secret", event.attributes["exception.stacktrace"])
        self.assertIn("search_read", event.attributes["exception.stacktrace"])

    def test_call_kw_imported_by_name(self):
        self.assertTrue(_patch.is_patched(dataset, "call_kw"))


@tagged("post_install", "-at_install")
class TestHttpMetrics(InstrumentationMixin, HttpCase):
    def duration(self):
        return {
            tuple(sorted(point.attributes.items())): point
            for point in self.metric_points("http.server.request.duration")
        }

    def test_duration(self):
        self.url_open("/web/login")
        # collected once: the exemplars are reset by a collection
        ((attributes, point),) = self.duration().items()
        self.assertEqual(
            dict(attributes),
            {
                "http.request.method": "GET",
                "http.route": "/web/login",
                "http.response.status_code": 200,
                "network.protocol.version": "1.1",
                "url.scheme": "http",
            },
        )
        self.assertEqual(point.count, 1)
        self.assertGreater(point.sum, 0)
        self.assertEqual(
            tuple(point.explicit_bounds), http_instrumentation.DURATION_BUCKETS
        )
        # the exemplar links the measure to the trace of the request
        (span,) = [s for s in self.finished_spans() if s.kind == SpanKind.SERVER]
        self.assertIn(
            span.context.trace_id,
            [exemplar.trace_id for exemplar in point.exemplars],
        )

    def test_not_found(self):
        self.url_open("/monitoring-opentelemetry/missing")
        (attributes,) = self.duration()
        self.assertNotIn("http.route", dict(attributes))
        self.assertEqual(dict(attributes)["http.response.status_code"], 404)

    def test_excluded(self):
        self.url_open("/web/static/img/favicon.ico")
        self.assertEqual(self.duration(), {})

    def test_metrics_without_traces(self):
        self.setup_otel(Config(enabled=True, traces=False))
        self.url_open("/web/login")
        self.assertEqual(self.finished_spans(), ())
        (attributes,) = self.duration()
        self.assertEqual(dict(attributes)["http.route"], "/web/login")
        (sql,) = self.metric_points("odoo.http.sql.count")
        self.assertGreater(sql.sum, 0)

    def test_traces_without_metrics(self):
        self.setup_otel(Config(enabled=True, metrics=False))
        self.url_open("/web/login")
        self.assertTrue(self.finished_spans())

    def test_sql_count(self):
        self.url_open("/web/login")
        (point,) = self.metric_points("odoo.http.sql.count")
        self.assertEqual(
            dict(point.attributes),
            {"http.request.method": "GET", "http.route": "/web/login"},
        )
        (span,) = [s for s in self.finished_spans() if s.kind == SpanKind.SERVER]
        self.assertEqual(
            (point.count, point.sum), (1, span.attributes["odoo.sql.count"])
        )

    @mute_logger("odoo.http", "odoo.http.router")
    def test_server_error(self):
        # a JSON-RPC error is a 200 response: error.type comes from the exception
        self.authenticate("admin", "admin")
        with (
            self.patch_search_read(ValueError("boom")),
            self.assertRaises(JsonRpcException),
        ):
            self.call_kw("res.partner", "search_read")
        errors_ = [
            dict(attributes)
            for attributes in self.duration()
            if "error.type" in dict(attributes)
        ]
        self.assertEqual(len(errors_), 1)
        self.assertEqual(errors_[0]["error.type"], "builtins.ValueError")
        self.assertEqual(errors_[0]["http.response.status_code"], 200)

    @mute_logger("odoo.http", "odoo.http.router")
    def test_user_error_is_not_an_error(self):
        self.authenticate("admin", "admin")
        with (
            self.patch_search_read(UserError("no")),
            self.assertRaises(JsonRpcException),
        ):
            self.call_kw("res.partner", "search_read")
        self.assertFalse(
            [
                attributes
                for attributes in self.duration()
                if "error.type" in dict(attributes)
            ]
        )


@tagged("post_install", "-at_install")
class TestRetries(OtelMixin, BaseCase):
    def setUp(self):
        super().setUp()
        self.setup_otel()
        http_instrumentation.install()
        self.addCleanup(http_instrumentation.uninstall)
        patcher = patch.object(retrying_module.random, "uniform", return_value=0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fake_env = SimpleNamespace(cr=Mock(closed=False))

    def retries(self):
        return sum(point.value for point in self.metric_points("odoo.http.retries"))

    @mute_logger("odoo.http")
    def test_retries(self):
        failures = [ConcurrencyError("conflict"), ConcurrencyError("conflict")]

        def func():
            if failures:
                raise failures.pop()
            return "done"

        self.assertEqual(retrying_module.retrying(func, self.fake_env), "done")
        self.assertEqual(self.retries(), 2)
        # the router imports it by name
        self.assertTrue(_patch.is_patched(router, "retrying"))

    @mute_logger("odoo.http")
    def test_flush_failure(self):
        # the deferred writes of the attempt fail in the flush done by Odoo
        self.fake_env.cr.flush.side_effect = [ConcurrencyError("conflict"), None]
        self.assertEqual(retrying_module.retrying(lambda: 1, self.fake_env), 1)
        self.assertEqual(self.retries(), 1)

    def test_no_retry(self):
        self.assertEqual(retrying_module.retrying(lambda: 1, self.fake_env), 1)
        self.assertEqual(self.retries(), 0)

    @mute_logger("odoo.http")
    def test_exhausted(self):
        def func():
            raise ConcurrencyError("conflict")

        with self.assertRaises(ConcurrencyError):
            retrying_module.retrying(func, self.fake_env)
        tries = retrying_module.MAX_TRIES_ON_CONCURRENCY_FAILURE
        self.assertEqual(self.retries(), tries - 1)

    def test_without_metrics(self):
        self.setup_otel(Config(enabled=True, metrics=False))
        func = Mock(return_value=1)
        self.assertEqual(retrying_module.retrying(func, self.fake_env), 1)
        func.assert_called_once_with()  # not wrapped

    def test_route(self):
        span = sdk.tracer().start_span("GET")
        request = http_instrumentation._Request(span, "GET", "/p")
        request.attributes["http.route"] = "/web/dataset/call_kw"
        token = http_instrumentation._current.set(request)
        failures = [ConcurrencyError("conflict")]

        def func():
            if failures:
                raise failures.pop()

        try:
            with mute_logger("odoo.http"):
                retrying_module.retrying(func, self.fake_env)
        finally:
            http_instrumentation._current.reset(token)
        (point,) = self.metric_points("odoo.http.retries")
        self.assertEqual(dict(point.attributes), {"http.route": "/web/dataset/call_kw"})


@tagged("post_install", "-at_install")
class TestJson2(InstrumentationMixin, TransactionCase):
    """``/json/2/<model>/<method>``: the span wraps ``serve_ir_http``."""

    def setUp(self):
        super().setUp()
        rpc_instrumentation.uninstall()
        self.serve = Mock(return_value="response")
        patcher = patch.object(router, "serve_ir_http", self.serve)
        patcher.start()
        self.addCleanup(patcher.stop)
        rpc_instrumentation.install()
        # unpatched before the mock is removed
        self.addCleanup(rpc_instrumentation.uninstall)
        self.request = SimpleNamespace(registry=self.env.registry)

    def serve_json2(self, model, method):
        args = {"__model__": model, "__method__": method}
        self.assertEqual(router.serve_ir_http(self.request, None, args), "response")
        return self.finished_spans()

    def test_model_method(self):
        (span,) = self.serve_json2("res.partner", "search_read")
        self.assertEqual(span.name, "res.partner.search_read")
        self.assertEqual(span.kind, SpanKind.INTERNAL)

    def test_unknown_model_or_method(self):
        self.assertEqual(self.serve_json2("no.such.model", "search_read"), ())
        (span,) = self.serve_json2("res.partner", "_private")
        self.assertEqual(span.name, "res.partner._OTHER")

    def test_other_route(self):
        router.serve_ir_http(self.request, None, {"id": 1})
        self.assertEqual(self.finished_spans(), ())
        self.serve.assert_called_once_with(self.request, None, {"id": 1})


@tagged("post_install", "-at_install")
class TestHttpUnits(OtelMixin, BaseCase):
    def setUp(self):
        super().setUp()
        self.setup_otel()

    def finish(self, status_line, environ=None, route_has_arguments=False):
        span = sdk.tracer().start_span("GET")
        request = http_instrumentation._Request(span, "GET", "/p", route_has_arguments)
        http_instrumentation._finish(request, environ or {}, status_line)
        span.end()
        return self.finished_spans()[-1]

    def test_server_error_status(self):
        span = self.finish("500 INTERNAL SERVER ERROR")
        self.assertEqual(span.status.status_code, StatusCode.ERROR)
        self.assertEqual(span.attributes["error.type"], "500")

    def test_host(self):
        span = self.finish("200 OK", {"HTTP_HOST": "[::1]:8069"})
        self.assertEqual(span.attributes["server.address"], "[::1]")
        self.assertEqual(span.attributes["server.port"], 8069)
        self.assertEqual(http_instrumentation._split_host("[::1]"), ("[::1]", None))
        self.assertEqual(http_instrumentation._split_host("odoo"), ("odoo", None))

    def test_registry_error_is_not_an_error(self):
        """Odoo serves the request without the database after a RegistryError."""

        def serve_db(request):
            raise router.RegistryError("no registry")

        serve = http_instrumentation._make_serve(serve_db)
        span = sdk.tracer().start_span("GET")
        token = http_instrumentation._current.set(
            http_instrumentation._Request(span, "GET", "/")
        )
        try:
            with self.assertRaises(router.RegistryError):
                serve(SimpleNamespace(db="db", session=SimpleNamespace(uid=None)))
        finally:
            http_instrumentation._current.reset(token)
            span.end()
        (span,) = self.finished_spans()
        self.assertEqual(span.status.status_code, StatusCode.UNSET)
        self.assertEqual(span.events, ())

    def test_exception_recorded_once(self):
        exc = ValueError("first line\nsecond line")
        outer = sdk.tracer().start_span("outer")
        inner = sdk.tracer().start_span("inner")
        errors.record(inner, exc)
        errors.record(outer, exc)
        self.assertEqual(len(inner.events), 1)
        self.assertEqual(outer.events, ())
        self.assertEqual(errors.safe_message(exc), "first line")
        self.assertEqual(len(errors.safe_message(ValueError("x" * 1000))), 301)

    def test_uninstall(self):
        http_instrumentation.install()
        rpc_instrumentation.install()
        http_instrumentation.uninstall()
        rpc_instrumentation.uninstall()
        self.assertFalse(_patch.is_patched(router.Application, "__call__"))
        self.assertFalse(_patch.is_patched(router, "serve_db"))
        self.assertFalse(_patch.is_patched(dataset, "call_kw"))
