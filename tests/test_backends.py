import unittest
from unittest import mock

from pyguitest import Capability, connect
from pyguitest.backends import NullBackend, available, select
from pyguitest.errors import BackendUnavailable, CapabilityUnsupported
from pyguitest.session import detect


class TestNullBackend(unittest.TestCase):
    def setUp(self):
        self.backend = NullBackend()

    def test_supports_only_tier_one(self):
        self.assertTrue(self.backend.supports(Capability.PROCESS_LAUNCH))
        self.assertTrue(self.backend.supports(Capability.TIMING))
        self.assertEqual(len(self.backend.capabilities), 2)

    def test_unsupported_operations_raise_rather_than_return_zero(self):
        # The failure mode the audit singled out in the Perl module: every
        # error was a zero, indistinguishable from "the click missed".
        for call in (
            lambda: self.backend.windows(),
            lambda: self.backend.move_mouse(10, 10),
            lambda: self.backend.geometry(None),
            lambda: self.backend.type_text("x"),
        ):
            with self.subTest(call=call):
                with self.assertRaises(CapabilityUnsupported):
                    call()

    def test_error_carries_capability_and_reason(self):
        with self.assertRaises(CapabilityUnsupported) as ctx:
            self.backend.windows()
        self.assertIs(ctx.exception.capability, Capability.WINDOW_LIST)
        self.assertEqual(ctx.exception.backend, "null")
        self.assertTrue(ctx.exception.reason)


class TestSelection(unittest.TestCase):
    def test_falls_back_to_null_when_nothing_registered(self):
        backend = select(detect())
        self.assertIsInstance(backend, NullBackend)

    def test_unknown_backend_name_is_an_error(self):
        with self.assertRaises(BackendUnavailable):
            select(detect(), "no-such-backend")

    def test_available_is_a_list(self):
        self.assertIsInstance(available(), list)

    def test_opt_in_backend_is_excluded_from_automatic_composition(self):
        # A backend whose construction can raise an interactive consent
        # dialog (the portal, in real use) must never be reached by a plain
        # connect() -- only by naming it explicitly.
        from pyguitest import backends
        from pyguitest.backends.base import GUIBackend
        from pyguitest.capabilities import CapabilitySet

        class FakeOptIn(GUIBackend):
            name = "fake-optin"
            capabilities = property(lambda self: CapabilitySet())

        built = []

        def factory(env):
            built.append(1)
            return FakeOptIn()

        original_registry = list(backends._REGISTRY)
        backends._REGISTRY.clear()
        backends.register(factory, "fake-optin", priority=100, opt_in=True)
        self.addCleanup(backends._REGISTRY.clear)
        self.addCleanup(backends._REGISTRY.extend, original_registry)

        automatic = select(detect())
        self.assertIsInstance(automatic, NullBackend)
        self.assertEqual(built, [])

        named = select(detect(), "fake-optin")
        self.assertIsInstance(named, FakeOptIn)
        self.assertEqual(built, [1])

    def test_already_built_members_are_closed_if_composing_them_fails(self):
        # Regression: select() instantiates every registered backend before
        # deciding how to combine them. If combining them failed, the
        # already-built members -- an open X display, a uinput device, a
        # live IPC socket in the real case -- were dropped without close().
        from pyguitest import backends
        from pyguitest.backends.base import GUIBackend
        from pyguitest.capabilities import CapabilitySet

        class FakeBackend(GUIBackend):
            def __init__(self, name):
                self._name = name
                self.closed = False

            name = property(lambda self: self._name)
            capabilities = property(lambda self: CapabilitySet())

            def close(self):
                self.closed = True

        first, second = FakeBackend("a"), FakeBackend("b")
        original_registry = list(backends._REGISTRY)
        backends._REGISTRY.clear()
        backends.register(lambda env: first, "fake-a", priority=100)
        backends.register(lambda env: second, "fake-b", priority=90)
        self.addCleanup(backends._REGISTRY.clear)
        self.addCleanup(backends._REGISTRY.extend, original_registry)

        with mock.patch.object(
            backends.CompositeBackend, "__init__", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                select(detect())

        self.assertTrue(first.closed)
        self.assertTrue(second.closed)


class TestSessionFacade(unittest.TestCase):
    def test_connect_never_raises_on_a_limited_desktop(self):
        with connect() as gui:
            self.assertTrue(gui.supports(Capability.PROCESS_LAUNCH))
            self.assertIn("backend", gui.report())

    def test_tier_one_operations_work_without_a_display_server(self):
        with connect() as gui:
            result = gui.run_app(["true"])
            self.assertEqual(result.returncode, 0)

    def test_require_raises_for_the_whole_declared_set(self):
        with connect() as gui:
            gui.require(Capability.PROCESS_LAUNCH)  # fine
            with self.assertRaises(CapabilityUnsupported):
                gui.require(Capability.PROCESS_LAUNCH, Capability.WINDOW_LIST)

    def test_private_attributes_still_raise_attribute_error(self):
        with connect() as gui:
            with self.assertRaises(AttributeError):
                gui._nonexistent

    def test_a_missing_backend_attribute_raises_rather_than_recursing(self):
        # Regression: __getattr__ read self.backend, which re-enters
        # __getattr__ when "backend" itself is absent -- unpickling,
        # copy.copy, or a subclass that skipped __init__ all hit this and
        # got RecursionError instead of the real AttributeError.
        import pyguitest

        broken = object.__new__(pyguitest.Session)
        with self.assertRaises(AttributeError):
            broken.windows()


if __name__ == "__main__":
    unittest.main()
