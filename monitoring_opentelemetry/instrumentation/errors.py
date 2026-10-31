"""Exceptions on spans: errors of the server versus errors of the user.

The exception event carries the type, the first line of the message (truncated) and
the stack trace (code locations only): the next lines of a message often hold record
values, like the ``DETAIL`` of a PostgreSQL error. An exception is recorded as an
event once, on the innermost span; the outer spans only get its type and status.
"""

import contextlib
import traceback

from opentelemetry.trace import Status, StatusCode
from werkzeug.exceptions import HTTPException

from odoo.exceptions import RedirectWarning, UserError
from odoo.http.session import SessionExpiredException

# Expected outcomes of a request or a call, shown to the user: not server errors
USER_ERRORS = (UserError, RedirectWarning, SessionExpiredException)
MESSAGE_MAX_LENGTH = 300
_RECORDED = "_otel_recorded"


def is_user_error(exc):
    if isinstance(exc, HTTPException):
        return (exc.code or 0) < 500
    return isinstance(exc, USER_ERRORS)


def qualified_name(exc):
    cls = type(exc)
    return f"{cls.__module__}.{cls.__qualname__}"


def safe_message(exc):
    """First line of the message of ``exc``, truncated."""
    lines = str(exc).strip().splitlines()
    message = lines[0] if lines else ""
    if len(message) > MESSAGE_MAX_LENGTH:
        message = message[:MESSAGE_MAX_LENGTH] + "…"
    return message


def _stacktrace(exc, name, message):
    frames = "".join(traceback.format_tb(exc.__traceback__))
    return f"Traceback (most recent call last):\n{frames}{name}: {message}"


def _first_recording(exc):
    """Whether ``exc`` is recorded for the first time (marks it)."""
    if getattr(exc, _RECORDED, False):
        return False
    # exceptions without __dict__ cannot be marked: recorded each time
    with contextlib.suppress(AttributeError):
        setattr(exc, _RECORDED, True)
    return True


def record(span, exc):
    """Record ``exc`` on ``span``: a server error sets the span in error (with an
    exception event the first time), an error of the user is only named in
    ``odoo.exception.type``.
    """
    if not span.is_recording():
        return
    name = qualified_name(exc)
    span.set_attribute("odoo.exception.type", name)
    if is_user_error(exc):
        return
    span.set_attribute("error.type", name)
    span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
    if _first_recording(exc):
        message = safe_message(exc)
        span.add_event(
            "exception",
            {
                "exception.type": name,
                "exception.message": message,
                "exception.stacktrace": _stacktrace(exc, name, message),
            },
        )
