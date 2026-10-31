"""Cron jobs: one ``INTERNAL`` span ``cron {name}`` per job.

``IrCron._process_job`` runs one job in the cron workers (prefork) and the cron
threads (threaded server), where the span is the root of its own trace; its SQL
queries are tracked on the span. A job run manually (``method_direct_trigger``) runs
in its HTTP request: its span is a child of the request span, whose SQL totals
include the job's queries.

The completion status of the job is known in ``_update_failure_count``, also called
when the job failed by timeout without running (``odoo.cron.timed_out``): ``failed``
sets the span in error. Odoo logs the exception of a failed job and does not raise
it: the exception is in the logs of the trace, not on the span.
"""

import contextvars
import logging

from opentelemetry import context, trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from odoo.addons.base.models.ir_cron import (
    CONSECUTIVE_TIMEOUT_FOR_FAILURE,
    CompletionStatus,
    IrCron,
)

from .. import sdk
from . import errors
from . import sql as sql_instrumentation
from ._patch import patch, unpatch

_logger = logging.getLogger(__name__)
_current = contextvars.ContextVar("monitoring_opentelemetry_cron", default=None)


def _safely(function, *args):
    try:
        function(*args)
    except Exception:
        _logger.debug("OpenTelemetry instrumentation failed", exc_info=True)


def _start(cron_cr, job):
    in_request = trace.get_current_span().get_span_context().is_valid
    return sdk.tracer().start_span(
        f"cron {job['cron_name']}",
        context=None if in_request else context.Context(),
        kind=SpanKind.INTERNAL,
        attributes={
            "odoo.cron.id": job["id"],
            "odoo.cron.name": job["cron_name"],
            "odoo.db": cron_cr.dbname,
        },
    )


def _make_process_job(original):
    def _process_job(cls, cron_cr, job):
        config = sdk.config()
        if config is None or not config.traces:
            return original(cls, cron_cr, job)
        try:
            span = _start(cron_cr, job)
        except Exception:
            _logger.debug("OpenTelemetry instrumentation failed", exc_info=True)
            return original(cls, cron_cr, job)
        context_token = context.attach(trace.set_span_in_context(span))
        cron_token = _current.set(span)
        try:
            with sql_instrumentation.track(span, config):
                return original(cls, cron_cr, job)
        except BaseException as exc:
            _safely(errors.record, span, exc)
            raise
        finally:
            _current.reset(cron_token)
            context.detach(context_token)
            _safely(span.end)

    return _process_job


def _set_status(span, job, status):
    span.set_attribute("odoo.cron.status", str(status))
    if status != CompletionStatus.FAILED:
        return
    timed_out = (
        job["timed_out_counter"] >= CONSECUTIVE_TIMEOUT_FOR_FAILURE and not job["done"]
    )
    if timed_out:
        span.set_attribute("odoo.cron.timed_out", True)
    span.set_status(Status(StatusCode.ERROR, "timed out" if timed_out else "failed"))


def _make_update_failure_count(original):
    def _update_failure_count(self, job, status):
        if (span := _current.get()) is not None:
            _safely(_set_status, span, job, status)
        return original(self, job, status)

    return _update_failure_count


def install():
    patch(IrCron, "_process_job", _make_process_job)
    patch(IrCron, "_update_failure_count", _make_update_failure_count)


def uninstall():
    unpatch(IrCron, "_update_failure_count")
    unpatch(IrCron, "_process_job")
