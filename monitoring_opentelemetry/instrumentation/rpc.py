"""ORM calls over RPC: one ``INTERNAL`` span ``{model}.{method}`` per call.

``odoo.service.model.call_kw`` serves ``/web/dataset/call_kw``, ``call_button``,
``/jsonrpc`` and ``/xmlrpc/2/object``. Some modules import it by name
(``web.controllers.dataset``): their global is patched too, and the modules
importing it later get the patched function. ``/json/2/<model>/<method>`` does not
use it: its span wraps ``serve_ir_http`` when the route has these arguments.

Span names come from validated values only: an existing model, and a public method
of it (``_OTHER`` otherwise), so that callers cannot create arbitrary span names.
A call retried by Odoo (serialization failure) gives one span per attempt.

``odoo.rpc.duration`` measures each call (attempt), with or without the traces, by
model and method, and ``error.type`` for an error of the server. A failed call is
measured under the ``_OTHER`` method: any user can call any public method of a model,
which must not fill the series of the metric (spans are not kept in the process).
"""

import logging
import sys
import time

from opentelemetry.trace import SpanKind

from odoo.http import router
from odoo.service import model as service_model

from .. import sdk
from . import errors
from ._patch import patch, unpatch

_logger = logging.getLogger(__name__)
_call_kw_owners = []
# longer than the HTTP requests: imports, reports, postings
DURATION_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    300.0,
)


def _duration(meter):
    return meter.create_histogram(
        "odoo.rpc.duration",
        unit="s",
        description="Duration of the ORM calls over RPC.",
        explicit_bucket_boundaries_advisory=DURATION_BUCKETS,
    )


def method_label(model_class, name):
    """``name`` if it is a public method of ``model_class``, else ``_OTHER``."""
    if (
        isinstance(name, str)
        and not name.startswith("_")
        and callable(getattr(model_class, name, None))
    ):
        return name
    return "_OTHER"


def _span(model, method):
    return sdk.tracer().start_as_current_span(
        f"{model}.{method}",
        kind=SpanKind.INTERNAL,
        attributes={"odoo.model": model, "odoo.method": method},
        record_exception=False,
        set_status_on_exception=False,
    )


def _record_duration(attributes, start):
    if duration := sdk.instrument(_duration):
        duration.record(time.perf_counter() - start, attributes)


def _call(model, method, original, *args):
    attributes = {"odoo.model": model, "odoo.method": method}
    start = time.perf_counter()
    with _span(model, method) as span:
        try:
            return original(*args)
        except BaseException as exc:
            try:
                attributes["odoo.method"] = "_OTHER"
                if not errors.is_user_error(exc):
                    attributes["error.type"] = errors.qualified_name(exc)
                if isinstance(exc, Exception):
                    errors.record(span, exc)
            except Exception:
                _logger.debug("OpenTelemetry instrumentation failed", exc_info=True)
            raise
        finally:
            # in the context of the span: the exemplars of its trace
            try:
                _record_duration(attributes, start)
            except Exception:
                _logger.debug("OpenTelemetry instrumentation failed", exc_info=True)


def _make_call_kw(original):
    def call_kw(model, name, args, kwargs):
        if not sdk.is_active():
            return original(model, name, args, kwargs)
        method = method_label(type(model), name)
        return _call(model._name, method, original, model, name, args, kwargs)

    return call_kw


def _json2_model_method(request, args):
    """``(model, method)`` of a ``/json/2`` call, if the model exists."""
    model, name = args.get("__model__"), args.get("__method__")
    if not (model and name) or request.registry is None:
        return None
    if not isinstance(model, str) or model not in request.registry:
        return None
    return model, method_label(request.registry[model], name)


def _make_serve_ir_http(original):
    def serve_ir_http(request, rule, args):
        if not sdk.is_active():
            return original(request, rule, args)
        try:
            model_method = _json2_model_method(request, args)
        except Exception:
            _logger.debug("OpenTelemetry instrumentation failed", exc_info=True)
            model_method = None
        if model_method is None:
            return original(request, rule, args)
        return _call(*model_method, original, request, rule, args)

    return serve_ir_http


def install():
    original = service_model.call_kw
    patch(service_model, "call_kw", _make_call_kw)
    # vars(): getattr() could trigger the import of lazy modules
    for module in list(sys.modules.values()):
        try:
            imported = vars(module).get("call_kw") is original
        except TypeError:  # not a module object
            imported = False
        if imported and module is not service_model:
            patch(module, "call_kw", _make_call_kw)
            _call_kw_owners.append(module)
    patch(router, "serve_ir_http", _make_serve_ir_http)


def uninstall():
    unpatch(router, "serve_ir_http")
    while _call_kw_owners:
        unpatch(_call_kw_owners.pop(), "call_kw")
    unpatch(service_model, "call_kw")
