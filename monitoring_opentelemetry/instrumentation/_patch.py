"""Reversible patching of Odoo attributes (functions, methods, classmethods).

The patches are kept in a registry and identified by identity: an attribute wrapped
again by someone else is neither patched twice nor restored over the other wrapper.
"""

import functools
import inspect
import logging

_logger = logging.getLogger(__name__)

# (owner, name) -> (installed attribute, original attribute, owned by owner)
_patches = {}


def patch(owner, name, make_wrapper):
    """Replace ``owner.name`` by ``make_wrapper(function)``, once.

    For a classmethod or a staticmethod, ``make_wrapper`` receives the underlying
    function and the wrapper is wrapped again in the same descriptor.

    :return: whether the attribute was patched by this call
    """
    key = (owner, name)
    raw = inspect.getattr_static(owner, name)
    if key in _patches and _patches[key][0] is raw:
        return False
    descriptor = type(raw) if isinstance(raw, classmethod | staticmethod) else None
    function = raw.__func__ if descriptor else raw
    wrapper = functools.wraps(function)(make_wrapper(function))
    installed = descriptor(wrapper) if descriptor else wrapper
    owned = name in vars(owner)
    setattr(owner, name, installed)
    _patches[key] = (installed, raw, owned)
    return True


def unpatch(owner, name):
    """Restore ``owner.name`` patched by :func:`patch`."""
    entry = _patches.pop((owner, name), None)
    if entry is None:
        return
    installed, raw, owned = entry
    if vars(owner).get(name) is not installed:
        _logger.warning("%s.%s was patched again, not restored", owner, name)
    elif owned:
        setattr(owner, name, raw)
    else:
        delattr(owner, name)


def is_patched(owner, name):
    entry = _patches.get((owner, name))
    return entry is not None and vars(owner).get(name) is entry[0]


def original(owner, name):
    """The function wrapped by the patch of ``owner.name``."""
    raw = _patches[(owner, name)][1]
    return raw.__func__ if isinstance(raw, classmethod | staticmethod) else raw
