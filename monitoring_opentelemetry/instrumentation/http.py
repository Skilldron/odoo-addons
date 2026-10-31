"""HTTP requests: one ``SERVER`` span per request, named ``{method} {route}``.

The span wraps ``Application.__call__``, the WSGI entry point of Odoo. The route
(werkzeug rule template) is known once Odoo has matched it, in
``_set_request_dispatcher``. Odoo turns exceptions into responses inside
``__call__``: they are recorded where they leave ``serve_db`` / ``serve_nodb``. The
span ends when ``__call__`` returns: sending a streamed body is not measured.

Never recorded: the query string (portal tokens), headers other than the user agent,
cookies, bodies, and the path of a route with arguments (``/survey/start/<token>``):
its template is in ``http.route``.

The telemetry code never breaks a request: its failures are logged at debug level.
"""

import contextvars
import logging
from dataclasses import dataclass

from opentelemetry import context, propagate, trace
from opentelemetry.propagators.textmap import Getter
from opentelemetry.trace import SpanKind, Status, StatusCode

from odoo.http import router

from .. import sdk
from . import errors
from . import sql as sql_instrumentation
from ._patch import patch, unpatch

_logger = logging.getLogger(__name__)

KNOWN_METHODS = frozenset(
    ("CONNECT", "DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE")
)


class _EnvironGetter(Getter):
    """Read the propagation headers (``traceparent``...) from a WSGI environ."""

    def get(self, carrier, key):
        value = carrier.get("HTTP_" + key.upper().replace("-", "_"))
        return None if value is None else [value]

    def keys(self, carrier):
        return [
            key[5:].lower().replace("_", "-")
            for key in carrier
            if key.startswith("HTTP_")
        ]


_GETTER = _EnvironGetter()
USER_AGENT_MAX_LENGTH = 512


@dataclass
class _Request:
    """The span of the current request, and what is known about it."""

    span: trace.Span
    method: str  # method in span names: HTTP for an unknown method (semconv)
    path: str
    # url.path only for a route without arguments: no record value nor unknown path
    path_is_route: bool = False


_current = contextvars.ContextVar("monitoring_opentelemetry_request", default=None)


def _safely(function, *args):
    try:
        function(*args)
    except Exception:
        _logger.debug("OpenTelemetry instrumentation failed", exc_info=True)


def _decode_path(environ):
    path = environ.get("PATH_INFO") or "/"
    try:  # WSGI decodes the path as latin-1
        return path.encode("latin-1").decode("utf-8", "replace")
    except UnicodeEncodeError:
        return path


def _split_host(host):
    """``(address, port)`` of a Host header, IPv6 literals included."""
    if host.startswith("["):
        end = host.find("]") + 1
        address, rest = host[:end], host[end:]
        port = rest[1:] if rest.startswith(":") else ""
    else:
        address, _sep, port = host.partition(":")
    return address, int(port) if port.isdigit() else None


def _is_excluded(app, path, config):
    if config.excluded_paths and path.startswith(config.excluded_paths):
        return True
    return bool(app.get_static_file(path))


def _start(app, environ, config):
    """Start the span of the request, ``None`` when it is not traced."""
    path = _decode_path(environ)
    if _is_excluded(app, path, config):
        return None
    raw_method = environ.get("REQUEST_METHOD") or "GET"
    method = raw_method.upper()
    known = method in KNOWN_METHODS
    attributes = {"http.request.method": method if known else "_OTHER"}
    if not known:
        attributes["http.request.method_original"] = raw_method
    if user_agent := environ.get("HTTP_USER_AGENT"):
        attributes["user_agent.original"] = user_agent[:USER_AGENT_MAX_LENGTH]
    protocol = environ.get("SERVER_PROTOCOL", "")
    if protocol.startswith("HTTP/"):
        attributes["network.protocol.version"] = protocol[5:]
    span_method = method if known else "HTTP"
    span = sdk.tracer().start_span(
        span_method,
        context=propagate.extract(environ, getter=_GETTER),
        kind=SpanKind.SERVER,
        attributes=attributes,
    )
    return _Request(span, span_method, path)


def _finish(request, environ, status_line):
    span = request.span
    if request.path_is_route:
        span.set_attribute("url.path", request.path)
    # read after Odoo applied ProxyFix (proxy_mode), which updates the environ
    span.set_attribute("url.scheme", environ.get("wsgi.url_scheme", "http"))
    if host := environ.get("HTTP_HOST") or environ.get("SERVER_NAME"):
        address, port = _split_host(host)
        span.set_attribute("server.address", address)
        if port:
            span.set_attribute("server.port", port)
    if client := environ.get("REMOTE_ADDR"):
        span.set_attribute("client.address", client)
    if not status_line:
        return
    code = int(status_line.split(None, 1)[0])
    span.set_attribute("http.response.status_code", code)
    if code >= 500:
        if "error.type" not in (getattr(span, "attributes", None) or {}):
            span.set_attribute("error.type", str(code))
        span.set_status(Status(StatusCode.ERROR))


def _make_call(original):
    def __call__(self, environ, start_response):
        config = sdk.config()
        if config is None or not config.traces:
            return original(self, environ, start_response)
        try:
            request = _start(self, environ, config)
        except Exception:
            _logger.debug("OpenTelemetry instrumentation failed", exc_info=True)
            request = None
        if request is None:
            return original(self, environ, start_response)
        status = []

        def _start_response(status_line, headers, exc_info=None):
            status.append(status_line)
            if exc_info is None:
                return start_response(status_line, headers)
            return start_response(status_line, headers, exc_info)

        context_token = context.attach(trace.set_span_in_context(request.span))
        request_token = _current.set(request)
        try:
            with sql_instrumentation.track(request.span, config):
                return original(self, environ, _start_response)
        except BaseException as exc:
            _safely(errors.record, request.span, exc)
            raise
        finally:
            _current.reset(request_token)
            context.detach(context_token)
            _safely(_finish, request, environ, status[-1] if status else None)
            _safely(request.span.end)

    return __call__


def _set_route(request, rule):
    request.path_is_route = not rule.arguments
    request.span.update_name(f"{request.method} {rule.rule}")
    request.span.set_attribute("http.route", rule.rule)
    request.span.set_attribute("odoo.route_type", rule.endpoint.routing["type"])


def _make_set_request_dispatcher(original):
    def _set_request_dispatcher(request, rule):
        if (current := _current.get()) is not None:
            _safely(_set_route, current, rule)
        return original(request, rule)

    return _set_request_dispatcher


def _make_serve(original):
    def serve(request, *args, **kwargs):
        current = _current.get()
        if current is None:
            return original(request, *args, **kwargs)
        if request.db:
            _safely(current.span.set_attribute, "odoo.db", request.db)
        try:
            return original(request, *args, **kwargs)
        except router.RegistryError as exc:
            # Odoo serves the request without the database: not an error yet
            name = errors.qualified_name(exc)
            _safely(current.span.set_attribute, "odoo.exception.type", name)
            raise
        except Exception as exc:
            _safely(errors.record, current.span, exc)
            raise
        finally:
            if uid := getattr(request.session, "uid", None):
                _safely(current.span.set_attribute, "odoo.uid", uid)

    return serve


def install():
    patch(router.Application, "__call__", _make_call)
    patch(router, "_set_request_dispatcher", _make_set_request_dispatcher)
    patch(router, "serve_db", _make_serve)
    patch(router, "serve_nodb", _make_serve)


def uninstall():
    unpatch(router.Application, "__call__")
    unpatch(router, "_set_request_dispatcher")
    unpatch(router, "serve_db")
    unpatch(router, "serve_nodb")
