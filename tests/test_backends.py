import dataclasses
import unittest
from types import SimpleNamespace
from unittest import mock

from pyguitest import Capability, backends, connect, tools
from pyguitest.backends import NullBackend, available, select
from pyguitest.errors import (
    BackendUnavailable,
    CapabilityUnsupported,
    PyGUITestError,
)
from pyguitest.session import Compositor, SessionType, detect


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


class TestNamedComposition(unittest.TestCase):
    """`select(env, ["a", "b"])` -- composing exactly the backends named.

    The case this exists for: pairing an opt-in input backend with the
    element and window access a plain connect() would have given you.
    Automatic composition skips opt-in factories so a consent dialog is
    never raised by surprise, which used to leave callers opening two
    sessions and remembering which one answered what.
    """

    def setUp(self):
        from pyguitest import backends

        self.backends = backends
        original = list(backends._REGISTRY)
        backends._REGISTRY.clear()
        self.addCleanup(backends._REGISTRY.extend, original)
        self.addCleanup(backends._REGISTRY.clear)
        self.built: dict = {}

    def _fake(self, name, caps=(), builds=True, reports=None):
        """Register a fake backend, and hand back the instances it builds.

        `reports` is the name the backend gives for itself when that differs
        from the name it is registered under -- the `imagesearch:compare`
        convention four real backends follow.
        """
        from pyguitest.backends.base import GUIBackend
        from pyguitest.capabilities import CapabilitySet

        class Fake(GUIBackend):
            def __init__(self, **options):
                self._name = reports or name
                self.options = options
                self.closed = False

            name = property(lambda self: self._name)
            capabilities = property(lambda self: CapabilitySet(set(caps)))

            def close(self):
                self.closed = True

        def factory(environment, **options):
            if not builds:
                return None
            self.built[name] = Fake(**options)
            return self.built[name]

        return factory

    def _register(self, *entries):
        """Register (name, priority, kwargs-for-_fake) in the given order."""
        for name, priority, kwargs in entries:
            self.backends.register(self._fake(name, **kwargs), name, priority=priority)

    def test_a_named_list_composes_exactly_those_backends(self):
        self._register(("a", 90, {}), ("b", 80, {}), ("c", 70, {}))
        backend = select(detect(), ["a", "c"])
        self.assertEqual([m.name for m in backend.members], ["a", "c"])

    def test_the_callers_order_is_the_precedence_not_the_registry_priority(self):
        # The whole point of naming an order: "b" is registered lower than
        # "a" and would lose this capability under automatic composition.
        self._register(
            ("a", 90, {"caps": (Capability.WINDOW_LIST,)}),
            ("b", 10, {"caps": (Capability.WINDOW_LIST,)}),
        )
        first = select(detect(), ["b", "a"])
        self.assertEqual(first.provider(Capability.WINDOW_LIST).name, "b")
        second = select(detect(), ["a", "b"])
        self.assertEqual(second.provider(Capability.WINDOW_LIST).name, "a")

    def test_automatic_composition_still_orders_by_priority(self):
        self._register(
            ("a", 90, {"caps": (Capability.WINDOW_LIST,)}),
            ("b", 10, {"caps": (Capability.WINDOW_LIST,)}),
        )
        self.assertEqual(select(detect()).provider(Capability.WINDOW_LIST).name, "a")

    def test_one_name_in_a_list_is_the_backend_itself_not_a_composite(self):
        # Same object select(env, "a") hands back: a composite of one only
        # adds a layer of dispatch to reach the only member there is.
        self._register(("a", 90, {}))
        self.assertIs(type(select(detect(), ["a"])), type(select(detect(), "a")))

    def test_a_named_backend_that_cannot_build_raises(self):
        # Where automatic composition reads None as "not applicable here"
        # and moves on, a name is a request: dropping it would hand back a
        # session missing the capability that motivated asking for it.
        self._register(("a", 90, {}), ("gone", 80, {"builds": False}))
        with self.assertRaises(BackendUnavailable) as ctx:
            select(detect(), ["a", "gone"])
        self.assertIn("gone", str(ctx.exception))
        self.assertIsInstance(select(detect()), object)  # automatic still fine

    def test_an_earlier_member_is_closed_when_a_later_one_fails(self):
        self._register(("a", 90, {}), ("gone", 80, {"builds": False}))
        with self.assertRaises(BackendUnavailable):
            select(detect(), ["a", "gone"])
        self.assertTrue(self.built["a"].closed)

    def test_an_unknown_name_in_a_list_is_an_error(self):
        self._register(("a", 90, {}))
        with self.assertRaises(BackendUnavailable) as ctx:
            select(detect(), ["a", "no-such-backend"])
        self.assertIn("no-such-backend", str(ctx.exception))

    def test_a_repeated_name_is_an_error(self):
        self._register(("a", 90, {}))
        with self.assertRaises(ValueError) as ctx:
            select(detect(), ["a", "a"])
        self.assertIn("repeat", str(ctx.exception))

    def test_an_empty_sequence_is_an_error_rather_than_automatic_detection(self):
        with self.assertRaises(ValueError) as ctx:
            select(detect(), [])
        self.assertIn("empty", str(ctx.exception))

    def test_options_are_keyed_by_backend_name(self):
        self._register(("a", 90, {}), ("b", 80, {}))
        select(detect(), ["a", "b"], {"b": {"persist_mode": 2}})
        self.assertEqual(self.built["a"].options, {})
        self.assertEqual(self.built["b"].options, {"persist_mode": 2})

    def test_a_flat_options_dict_with_a_list_is_refused_not_guessed(self):
        # Guessing would hand persist_mode to whichever factory happened to
        # accept it, which is the wrong backend half the time and silent.
        self._register(("a", 90, {}), ("b", 80, {}))
        with self.assertRaises(ValueError) as ctx:
            select(detect(), ["a", "b"], {"persist_mode": 2})
        self.assertIn("keyed by backend name", str(ctx.exception))

    def test_a_single_name_still_takes_a_flat_dict_even_of_dicts(self):
        # Regression guard: the keyed form must not be inferred from the
        # contents, or an option whose value is itself a dict would be read
        # as a per-backend mapping and rejected.
        self._register(("a", 90, {}))
        select(detect(), "a", {"config": {"nested": True}})
        self.assertEqual(self.built["a"].options, {"config": {"nested": True}})

    def test_member_reaches_one_backends_own_extras(self):
        self._register(("a", 90, {}), ("b", 80, {}))
        backend = select(detect(), ["a", "b"])
        self.assertIs(backend.member("b"), self.built["b"])

    def test_member_takes_the_registry_name_of_a_tool_backed_backend(self):
        # Four backends report the tool they found as part of their name --
        # imagesearch:compare, input:wtype, capture:grim, clipboard:wl-paste
        # -- so asking for them by the name you selected them with must work
        # without knowing which tool the machine happened to have.
        self._register(
            ("a", 90, {}),
            ("imagesearch", 80, {"reports": "imagesearch:compare"}),
        )
        backend = select(detect(), ["a", "imagesearch"])
        found = self.built["imagesearch"]
        self.assertIs(backend.member("imagesearch"), found)
        self.assertIs(backend.member("imagesearch:compare"), found)

    def test_member_names_what_is_actually_there_when_it_misses(self):
        self._register(("a", 90, {}), ("b", 80, {}))
        backend = select(detect(), ["a", "b"])
        with self.assertRaises(PyGUITestError) as ctx:
            backend.member("eiinput")
        self.assertIn("a, b", str(ctx.exception))

    def test_connect_accepts_a_list_end_to_end(self):
        self._register(("a", 90, {"caps": (Capability.WINDOW_LIST,)}), ("b", 80, {}))
        with connect(backend=["b", "a"]) as gui:
            self.assertEqual(gui.backend.name, "b+a")
            self.assertTrue(gui.supports(Capability.WINDOW_LIST))


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


def _env(**overrides):
    """A detected Environment with fields overridden."""
    base = detect({"WAYLAND_DISPLAY": "wayland-0", "XDG_CURRENT_DESKTOP": "GNOME"})
    return dataclasses.replace(base, **overrides)


class TestCaptureFactory(unittest.TestCase):
    """Which screenshot tool a session is given, if any.

    Selection is where the capture bugs lived. A tool being installed was
    treated as a tool being usable, and gnome-screenshot on GNOME Wayland
    is installed, selected, and hangs for the full subprocess timeout.
    """

    def _factory(self, environment):
        return backends._capture_factory(environment)

    def test_a_real_x11_session_may_use_the_root_reading_tools(self):
        with mock.patch.object(tools.ExternalTool, "present", True):
            backend = self._factory(_env(session_type=SessionType.X11))
        self.assertIsNotNone(backend)

    def test_xwayland_never_gets_a_root_reading_tool(self):
        # The X root is unreadable there, and gnome-screenshot does not
        # fail quickly -- it hangs. Both must be excluded.
        with mock.patch.object(tools.ExternalTool, "present", True):
            backend = self._factory(_env(session_type=SessionType.XWAYLAND))
        if backend is not None:
            self.assertNotIn(backend.tool.name, ("gnome-screenshot", "import"))

    def test_pure_wayland_never_gets_one_either(self):
        with mock.patch.object(tools.ExternalTool, "present", True):
            backend = self._factory(_env(session_type=SessionType.WAYLAND))
        if backend is not None:
            self.assertNotIn(backend.tool.name, ("gnome-screenshot", "import"))

    def test_no_tool_installed_yields_no_backend(self):
        with mock.patch.object(tools.ExternalTool, "present", False):
            self.assertIsNone(self._factory(_env(session_type=SessionType.X11)))


class TestInputFactoryRanking(unittest.TestCase):
    """Input transports are ranked by correctness before convenience.

    The ordering is a policy, not an implementation detail: a keymap-unsafe
    tool types the wrong characters on a non-US layout, so it must lose to
    a safe one even though both "work".
    """

    def _pick(self, environment, present):
        with mock.patch.object(
            tools.ExternalTool, "present", property(lambda self: self.name in present)
        ):
            with mock.patch("pyguitest.backends.uinput.available", return_value=False):
                return backends._input_factory(environment)

    def test_a_keymap_safe_tool_wins_over_an_unsafe_one(self):
        backend = self._pick(_env(session_type=SessionType.X11), {"ydotool", "xdotool"})
        # xdotool resolves keysyms against the server's live map; ydotool
        # injects scancodes below the compositor and cannot.
        self.assertEqual(backend.tool.name, "xdotool")

    def test_an_unsafe_tool_is_still_better_than_nothing(self):
        backend = self._pick(_env(session_type=SessionType.X11), {"ydotool"})
        self.assertEqual(backend.tool.name, "ydotool")

    def test_an_x11_only_tool_is_not_offered_to_a_wayland_session(self):
        # xdotool runs there and reaches no native Wayland client.
        backend = self._pick(_env(session_type=SessionType.WAYLAND), {"xdotool"})
        self.assertIsNone(backend)

    def test_uinput_is_preferred_over_a_keymap_unsafe_tool(self):
        # Both are keymap-unsafe, but uinput holds one device open instead
        # of spawning a process per event.
        fake = mock.Mock()
        with mock.patch.object(
            tools.ExternalTool, "present", property(lambda self: self.name == "ydotool")
        ):
            with mock.patch("pyguitest.backends.uinput.available", return_value=True):
                with mock.patch(
                    "pyguitest.backends.uinput.UinputBackend", return_value=fake
                ):
                    backend = backends._input_factory(
                        _env(session_type=SessionType.WAYLAND)
                    )
        self.assertIs(backend, fake)


class TestOtherFactoriesDeclineCleanly(unittest.TestCase):
    """A factory that cannot serve a session returns None, never raises.

    select() builds every registered factory, so one that raised on an
    unsuitable session would take the whole composition down with it.
    """

    def test_x11_declines_a_pure_wayland_session(self):
        self.assertIsNone(backends._x11_factory(_env(session_type=SessionType.WAYLAND)))

    def test_x11_declines_when_python_xlib_is_absent(self):
        with mock.patch("pyguitest.backends.x11.available", return_value=False):
            self.assertIsNone(backends._x11_factory(_env(session_type=SessionType.X11)))

    def test_gnomeshell_declines_off_mutter(self):
        self.assertIsNone(
            backends._gnomeshell_factory(_env(compositor=Compositor.WLROOTS))
        )

    def test_atspi_declines_when_dogtail_is_absent(self):
        with mock.patch("pyguitest.backends.atspi.available", return_value=False):
            self.assertIsNone(backends._atspi_factory(_env()))

    def test_portalcapture_declines_without_pygobject(self):
        with mock.patch(
            "pyguitest.backends.portalcapture.available", return_value=False
        ):
            self.assertIsNone(backends._portalcapture_factory(_env()))

    def test_a_backend_unavailable_during_construction_is_swallowed(self):
        # Probing can fail for reasons detection cannot see -- an extension
        # that is installed but disabled, a bus that is unreachable.
        with mock.patch("pyguitest.backends.x11.available", return_value=True):
            with mock.patch(
                "pyguitest.backends.x11.X11Backend",
                side_effect=BackendUnavailable("no display"),
            ):
                self.assertIsNone(
                    backends._x11_factory(_env(session_type=SessionType.X11))
                )


class TestScreenSize(unittest.TestCase):
    """uinput needs a real screen size at device-creation time.

    Absolute axes are declared once and cannot be changed, so a device
    built for 1920x1080 on a 4K output makes every move_mouse land
    somewhere else. Guessing is the failure being avoided.
    """

    def test_it_falls_back_rather_than_raising_when_nothing_answers(self):
        with mock.patch("shutil.which", return_value=None):
            size = backends._screen_size(_env(session_type=SessionType.WAYLAND))
        self.assertEqual(size, backends._FALLBACK_SCREEN_SIZE)

    def test_a_broken_tool_falls_back_too(self):
        with mock.patch("shutil.which", return_value="/usr/bin/xrandr"):
            with mock.patch("subprocess.run", side_effect=OSError("boom")):
                size = backends._screen_size(_env(session_type=SessionType.X11))
        self.assertEqual(size, backends._FALLBACK_SCREEN_SIZE)

    def test_xrandr_output_is_parsed_when_it_works(self):
        with mock.patch("shutil.which", return_value="/usr/bin/xrandr"):
            with mock.patch(
                "subprocess.run",
                return_value=SimpleNamespace(stdout="Screen 0: current 3840 x 2160"),
            ):
                size = backends._screen_size(_env(session_type=SessionType.X11))
        self.assertEqual(size, (3840, 2160))
