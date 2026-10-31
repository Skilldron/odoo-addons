import functools

from odoo.tests import BaseCase, tagged

from ..instrumentation import _patch


def double(original):
    def wrapper(*args, **kwargs):
        return 2 * original(*args, **kwargs)

    return wrapper


def make_target():
    """A fresh class for each test."""

    class Base:
        def inherited(self):
            return 1

    class Target(Base):
        def method(self):
            return 1

        @classmethod
        def class_method(cls):
            return cls.__name__

    return Target


@tagged("post_install", "-at_install")
class TestPatch(BaseCase):
    def setUp(self):
        super().setUp()
        self.Target = make_target()
        for name in ("method", "class_method", "inherited"):
            self.addCleanup(_patch._patches.pop, (self.Target, name), None)

    def test_patch_once(self):
        self.assertTrue(_patch.patch(self.Target, "method", double))
        self.assertFalse(_patch.patch(self.Target, "method", double))
        self.assertEqual(self.Target().method(), 2)
        self.assertTrue(_patch.is_patched(self.Target, "method"))
        self.assertEqual(self.Target.method.__name__, "method")

    def test_unpatch(self):
        original = self.Target.method
        _patch.patch(self.Target, "method", double)
        self.assertIs(_patch.original(self.Target, "method"), original)
        _patch.unpatch(self.Target, "method")
        self.assertIs(self.Target.method, original)
        self.assertFalse(_patch.is_patched(self.Target, "method"))
        _patch.unpatch(self.Target, "method")

    def test_classmethod(self):
        def make_wrapper(original):
            def wrapper(cls):
                return "wrapped " + original(cls)

            return wrapper

        _patch.patch(self.Target, "class_method", make_wrapper)
        self.assertEqual(self.Target.class_method(), "wrapped Target")
        self.assertEqual(self.Target().class_method(), "wrapped Target")
        _patch.unpatch(self.Target, "class_method")
        self.assertEqual(self.Target.class_method(), "Target")

    def test_inherited(self):
        """Unpatching an attribute inherited by the owner removes it from the
        owner: the one of the parent is visible again."""
        _patch.patch(self.Target, "inherited", double)
        self.assertEqual(self.Target().inherited(), 2)
        _patch.unpatch(self.Target, "inherited")
        self.assertNotIn("inherited", vars(self.Target))
        self.assertEqual(self.Target().inherited(), 1)

    def test_wrapped_again(self):
        """A patch wrapped again by someone else is not restored over the other
        wrapper."""
        _patch.patch(self.Target, "method", double)
        ours = self.Target.method

        @functools.wraps(ours)
        def theirs(self):
            return ours(self) + 10

        self.Target.method = theirs
        self.assertFalse(_patch.is_patched(self.Target, "method"))
        with self.assertLogs(_patch._logger.name, "WARNING"):
            _patch.unpatch(self.Target, "method")
        self.assertIs(self.Target.method, theirs)
        self.assertEqual(self.Target().method(), 12)
