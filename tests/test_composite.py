import unittest
import warnings

from pyguitest.backends.base import GUIBackend, Window
from pyguitest.backends.composite import CaptureFallbackWarning, CompositeBackend
from pyguitest.capabilities import Capability, CapabilitySet
from pyguitest.errors import CapabilityUnsupported, PyGUITestError


class Fake(GUIBackend):
    def __init__(self, name, caps, marker=None):
        self._name = name
        self._caps = CapabilitySet(caps)
        self.marker = marker
        self.closed = False

    name = property(lambda self: self._name)
    capabilities = property(lambda self: self._caps)

    def windows(self):
        return [self.marker]

    def geometry(self, window):
        return self.marker

    def type_text(self, text):
        return (self.marker, text)

    def locate(self, haystack, template, **kwargs):
        return (self.marker, haystack, template)

    def close(self):
        self.closed = True


class TestComposite(unittest.TestCase):
    def setUp(self):
        self.elements = Fake(
            "atspi",
            {
                Capability.ELEMENT_TREE,
                Capability.WINDOW_LIST,
                Capability.WINDOW_GEOMETRY,
            },
            marker="atspi",
        )
        self.input = Fake("input:wdotool", {Capability.TEXT_ENTRY}, marker="tool")
        self.composite = CompositeBackend([self.elements, self.input])

    def test_capabilities_are_the_union(self):
        self.assertIn(Capability.ELEMENT_TREE, self.composite.capabilities)
        self.assertIn(Capability.TEXT_ENTRY, self.composite.capabilities)
        self.assertEqual(len(self.composite.capabilities), 4)

    def test_calls_route_to_the_providing_member(self):
        self.assertEqual(self.composite.windows(), ["atspi"])
        self.assertEqual(self.composite.type_text("x"), ("tool", "x"))

    def test_locate_routes_to_the_providing_member(self):
        imagesearch = Fake(
            "imagesearch:compare", {Capability.IMAGE_LOCATE}, marker="compare"
        )
        composite = CompositeBackend([self.elements, self.input, imagesearch])
        self.assertEqual(
            composite.locate("a.png", "b.png"), ("compare", "a.png", "b.png")
        )

    def test_registration_order_decides_a_contested_capability(self):
        # A compositor IPC backend registered first should win WINDOW_GEOMETRY
        # from AT-SPI, whose coordinates are unreliable under Wayland.
        ipc = Fake("swaymsg", {Capability.WINDOW_GEOMETRY}, marker="ipc")
        first = CompositeBackend([ipc, self.elements])
        self.assertEqual(first.geometry(None), "ipc")
        second = CompositeBackend([self.elements, ipc])
        self.assertEqual(second.geometry(None), "atspi")

    def test_unprovided_capability_raises_with_the_capability_named(self):
        with self.assertRaises(CapabilityUnsupported) as ctx:
            self.composite.capture()
        self.assertIs(ctx.exception.capability, Capability.SCREEN_CAPTURE)

    def test_unprovided_image_locate_raises_with_the_capability_named(self):
        with self.assertRaises(CapabilityUnsupported) as ctx:
            self.composite.locate("haystack.png", "template.png")
        self.assertIs(ctx.exception.capability, Capability.IMAGE_LOCATE)

    def test_unknown_attribute_is_an_attribute_error(self):
        with self.assertRaises(AttributeError):
            self.composite.no_such_method

    def test_close_propagates_to_every_member(self):
        self.composite.close()
        self.assertTrue(self.elements.closed)
        self.assertTrue(self.input.closed)

    def test_providers_map_is_diagnostic(self):
        providers = self.composite.providers()
        self.assertEqual(providers["TEXT_ENTRY"], "input:wdotool")
        self.assertEqual(providers["WINDOW_LIST"], "atspi")
        self.assertNotIn("SCREEN_CAPTURE", providers)

    def test_empty_composite_is_rejected(self):
        with self.assertRaises(ValueError):
            CompositeBackend([])

    def test_a_member_declaring_but_not_implementing_a_method_raises_typed(self):
        # window_events has no GUIBackend override on Fake -- a member that
        # declares the capability without implementing the method used to
        # surface as a bare AttributeError (back when window_events was not
        # itself a GUIBackend method) instead of the typed
        # CapabilityUnsupported every other unsupported operation raises.
        claims_but_lacks = Fake("half-sway", {Capability.WINDOW_EVENTS}, marker="half")
        composite = CompositeBackend([claims_but_lacks])
        with self.assertRaises(CapabilityUnsupported) as ctx:
            composite.window_events()
        self.assertIs(ctx.exception.capability, Capability.WINDOW_EVENTS)

    def test_declaring_a_capability_whose_method_is_only_the_stub_also_raises_typed(
        self,
    ):
        # The same failure shape, but for a method window_events/
        # wait_for_window/find_element don't have to themselves: every
        # method dispatched here is *also* a real GUIBackend method with a
        # raising default body, so hasattr() alone can't tell "overridden"
        # from "just inherited the stub" -- Fake declares SCREEN_CAPTURE
        # without overriding capture(), which is exactly that case.
        claims_but_lacks = Fake("half-tool", {Capability.SCREEN_CAPTURE}, marker="half")
        composite = CompositeBackend([claims_but_lacks])
        with self.assertRaises(CapabilityUnsupported) as ctx:
            composite.capture()
        self.assertIs(ctx.exception.capability, Capability.SCREEN_CAPTURE)


class TestReportReachesTheBackend(unittest.TestCase):
    def test_composite_report_is_reachable_from_a_session(self):
        # Regression: Session.report() called self.capabilities.report()
        # and never consulted the backend, so CompositeBackend's own
        # provider-by-capability breakdown was unreachable from the CLI.
        import pyguitest

        elements = Fake("atspi", {Capability.ELEMENT_TREE}, marker="atspi")
        composite = CompositeBackend([elements])
        session = pyguitest.Session(composite, pyguitest.detect())
        report = session.report()
        self.assertIn("composite of 1 backend(s)", report)
        self.assertIn("atspi", report)

    def test_a_backend_with_no_report_method_is_not_required_to_have_one(self):
        import pyguitest

        plain = Fake("plain", {Capability.ELEMENT_TREE}, marker="x")
        session = pyguitest.Session(plain, pyguitest.detect())
        session.report()  # must not raise


if __name__ == "__main__":
    unittest.main()


class TestCaptureJoinsTwoMembers(unittest.TestCase):
    """capture(window=...) is the one operation needing two members.

    The regression this pins: `capture` used to be dispatched like every
    other operation, to the single SCREEN_CAPTURE provider -- which then
    refused a window, because a screenshot tool cannot resolve a Window to
    a rectangle. So per-window capture was unavailable on every session,
    including the ones that had both halves installed and only needed them
    introduced to each other.
    """

    class Geometry(GUIBackend):
        """Knows where windows are, and nothing about pixels."""

        name = "geometry"
        capabilities = CapabilitySet(
            {Capability.WINDOW_GEOMETRY, Capability.WINDOW_LIST}
        )

        def geometry(self, window):
            return (10, 20, 300, 200)

    class Pixels(GUIBackend):
        """Knows how to shoot the screen, and nothing about windows."""

        name = "pixels"
        capabilities = CapabilitySet({Capability.SCREEN_CAPTURE})

        def __init__(self):
            self.calls = []

        def capture(self, window=None, path=None, region=None):
            self.calls.append({"window": window, "path": path, "region": region})
            return path or "/tmp/whole.png"

    class Native(GUIBackend):
        """Captures a window directly, the way X11 can."""

        name = "native"
        capabilities = CapabilitySet(
            {Capability.SCREEN_CAPTURE, Capability.WINDOW_CAPTURE}
        )

        def __init__(self):
            self.calls = []

        def capture(self, window=None, path=None, region=None):
            self.calls.append({"window": window, "path": path, "region": region})
            return "/tmp/native.png"

    def test_a_window_is_resolved_to_a_region_by_the_geometry_member(self):
        pixels = self.Pixels()
        composite = CompositeBackend([self.Geometry(), pixels])
        composite.capture(window="w", path="/tmp/out.png")
        self.assertEqual(
            pixels.calls,
            [{"window": None, "path": "/tmp/out.png", "region": (10, 20, 300, 200)}],
        )

    def test_a_native_window_capturer_wins_over_geometry_plus_crop(self):
        # X11 can read the window's own drawable, so nothing stacked on top
        # of it lands in the image. That is a better screenshot than the
        # cropped one, so it is preferred whenever a member offers it.
        pixels = self.Pixels()
        native = self.Native()
        composite = CompositeBackend([self.Geometry(), native, pixels])
        self.assertEqual(composite.capture(window="w"), "/tmp/native.png")
        self.assertEqual(native.calls, [{"window": "w", "path": None, "region": None}])
        self.assertEqual(pixels.calls, [])

    def test_no_window_still_routes_straight_to_the_capture_member(self):
        pixels = self.Pixels()
        composite = CompositeBackend([self.Geometry(), pixels])
        composite.capture(region=(0, 0, 5, 5))
        self.assertEqual(
            pixels.calls,
            [{"window": None, "path": None, "region": (0, 0, 5, 5)}],
        )

    def test_window_and_region_together_are_refused(self):
        composite = CompositeBackend([self.Geometry(), self.Pixels()])
        with self.assertRaises(ValueError):
            composite.capture(window="w", region=(0, 0, 5, 5))

    def test_capture_with_no_pixel_source_names_the_missing_capability(self):
        composite = CompositeBackend([self.Geometry()])
        with self.assertRaises(CapabilityUnsupported) as caught:
            composite.capture()
        self.assertIn("SCREEN_CAPTURE", str(caught.exception))

    def test_a_window_with_no_geometry_source_says_which_half_is_missing(self):
        # The generic "no member provides it" would be actively misleading
        # here: a capture member *is* present, and the message has to point
        # at the half that is not.
        pixels = self.Pixels()
        composite = CompositeBackend([pixels])
        with self.assertRaises(CapabilityUnsupported) as caught:
            composite.capture(window="w")
        message = str(caught.exception)
        self.assertIn("WINDOW_GEOMETRY", message)
        self.assertIn("pixels", message)
        self.assertEqual(pixels.calls, [])

    def test_a_member_declaring_capture_without_implementing_it_raises_typed(self):
        class Liar(GUIBackend):
            name = "liar"
            capabilities = CapabilitySet({Capability.SCREEN_CAPTURE})

        composite = CompositeBackend([Liar()])
        with self.assertRaises(CapabilityUnsupported):
            composite.capture()


class TestWindowHandlesStayWithTheirOwnBackend(unittest.TestCase):
    """A Window from one member must never be handed to another.

    The live failure this pins, found on GNOME 50 the first time capture ran
    against a real desktop: the composite listed windows through the GNOME
    Shell extension, whose handle is a stable_sequence integer, then handed
    that Window to X11Backend because X11 is the member declaring
    WINDOW_CAPTURE. X11 expects an Xlib drawable, so it died with
    "'int' object has no attribute 'get_geometry'" -- reported, worse, as
    WindowNotFound, which reads like the window had closed.

    Window.handle is documented backend-private; this is the composite
    honouring that.
    """

    class Shell(GUIBackend):
        """Lists windows, knows their geometry; handles are integers."""

        name = "shell"
        capabilities = CapabilitySet(
            {
                Capability.WINDOW_LIST,
                Capability.WINDOW_GEOMETRY,
            }
        )

        def __init__(self):
            self.geometry_calls = []

        def windows(self):
            return [Window(handle=42, backend=self, title="Editor")]

        def geometry(self, window):
            self.geometry_calls.append(window)
            if not isinstance(getattr(window, "handle", window), int):
                raise AssertionError("got a handle from another backend")
            return (10, 20, 300, 200)

    class Xorg(GUIBackend):
        """Captures natively; handles must be Xlib drawable objects."""

        name = "x11"
        capabilities = CapabilitySet(
            {Capability.SCREEN_CAPTURE, Capability.WINDOW_CAPTURE}
        )

        def __init__(self):
            self.calls = []

        def windows(self):
            return [Window(handle=object(), backend=self, title="Editor")]

        def capture(self, window=None, path=None, region=None):
            if window is not None and isinstance(getattr(window, "handle", None), int):
                # Exactly what the real X11Backend did: treat the foreign
                # integer as a drawable and blow up on attribute access.
                raise AssertionError("got a handle from another backend")
            self.calls.append({"window": window, "region": region})
            return "/tmp/out.png"

    def setUp(self):
        self.shell = self.Shell()
        self.x11 = self.Xorg()
        self.composite = CompositeBackend([self.shell, self.x11])

    def test_a_foreign_window_takes_the_geometry_route_instead(self):
        window = self.composite.windows()[0]
        self.assertIs(window.backend, self.shell)
        self.composite.capture(window=window, path="/tmp/out.png")
        # Resolved by its own backend, captured as a region.
        self.assertEqual(self.shell.geometry_calls, [window])
        self.assertEqual(self.x11.calls[0]["region"], (10, 20, 300, 200))
        self.assertIsNone(self.x11.calls[0]["window"])

    def test_a_window_the_native_member_issued_still_takes_the_fast_path(self):
        # The optimisation must not be lost fixing the bug: a window that
        # really is X11's gets X11's own per-window grab.
        window = self.x11.windows()[0]
        self.composite.capture(window=window)
        self.assertIs(self.x11.calls[0]["window"], window)
        self.assertEqual(self.shell.geometry_calls, [])

    def test_a_raw_handle_keeps_the_historic_behaviour(self):
        # Only the caller knows whose namespace a bare handle is from, so
        # it goes to the capability's provider as it always did.
        self.composite.capture(window="raw-handle")
        self.assertEqual(self.x11.calls[0]["window"], "raw-handle")

    def test_geometry_prefers_the_owning_member_over_the_provider(self):
        # Same hazard in the other route: if the WINDOW_GEOMETRY provider
        # were a different backend, it would be handed a foreign handle.
        other = self.Shell()
        composite = CompositeBackend([other, self.shell, self.x11])
        window = Window(handle=42, backend=self.shell, title="Editor")
        composite.capture(window=window)
        self.assertEqual(self.shell.geometry_calls, [window])
        self.assertEqual(other.geometry_calls, [], "asked the wrong member")

    def test_a_window_from_a_backend_outside_the_composite_is_not_trusted(self):
        # A Window built by a backend that is not a member at all: its
        # handle is from nobody's namespace here, so the native path must
        # not claim it.
        stranger = self.Shell()
        window = Window(handle=99, backend=stranger, title="Elsewhere")
        composite = CompositeBackend([self.shell, self.x11])
        composite.capture(window=window)
        # Positively foreign is not the same as "unknown, so probably
        # fine": it must not reach the native grab.
        self.assertIsNone(composite._owner(window))
        self.assertIsNone(self.x11.calls[0]["window"])
        self.assertEqual(self.x11.calls[0]["region"], (10, 20, 300, 200))


class TestCaptureSurvivesABrokenBackend(unittest.TestCase):
    """One installed-but-broken tool must not take capture down.

    Not hypothetical. On GNOME Shell 50.4, `gnome-screenshot -f` cannot
    reach the Shell's screenshot interface (restricted to an allowlist of
    senders since 42), falls back to X11, which grabs nothing on a Wayland
    session, and then hangs until the 15s timeout -- while python-xlib sat
    in the same composite perfectly able to capture. The README documents
    the same shape for ImageMagick's `import` on Fedora 43.
    """

    class Broken(GUIBackend):
        """Installed, selected, and does not work."""

        name = "capture:gnome-screenshot"
        capabilities = CapabilitySet({Capability.SCREEN_CAPTURE})

        def __init__(self):
            self.calls = 0

        def capture(self, window=None, path=None, region=None):
            self.calls += 1
            raise PyGUITestError("gnome-screenshot did not finish within 15s")

    class Working(GUIBackend):
        """Captures natively, with no tool at all."""

        name = "x11"
        capabilities = CapabilitySet({Capability.SCREEN_CAPTURE})

        def __init__(self):
            self.calls = []

        def capture(self, window=None, path=None, region=None):
            self.calls.append(region)
            return path or "/tmp/x11.png"

    def setUp(self):
        self.broken = self.Broken()
        self.working = self.Working()
        self.composite = CompositeBackend([self.broken, self.working])

    def test_a_failing_member_falls_through_to_the_next(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self.assertEqual(self.composite.capture(), "/tmp/x11.png")
        self.assertEqual(self.broken.calls, 1)
        self.assertEqual(self.working.calls, [None])

    def test_falling_back_warns_rather_than_hiding_the_breakage(self):
        # Nothing else would report it: `pyguitest doctor` sees the tool as
        # present, and the capture the fallback rescued did succeed.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.composite.capture()
        self.assertEqual(len(caught), 1)
        self.assertIs(caught[0].category, CaptureFallbackWarning)
        self.assertIn("gnome-screenshot", str(caught[0].message))

    def test_a_broken_member_is_not_retried(self):
        # It hung for the full timeout; paying that on every capture would
        # make capture_on_failure unusable in a suite with many failures.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            for _ in range(4):
                self.composite.capture()
        self.assertEqual(self.broken.calls, 1)
        self.assertEqual(len(self.working.calls), 4)

    def test_the_region_route_falls_back_too(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self.composite.capture(region=(1, 2, 3, 4))
        self.assertEqual(self.working.calls, [(1, 2, 3, 4)])

    def test_a_window_resolved_to_a_region_also_falls_back(self):
        shell = TestWindowHandlesStayWithTheirOwnBackend.Shell()
        composite = CompositeBackend([shell, self.broken, self.working])
        window = shell.windows()[0]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            composite.capture(window=window)
        self.assertEqual(self.working.calls, [(10, 20, 300, 200)])

    def test_a_lone_failing_member_raises_its_own_error_unchanged(self):
        # No fallback happened, so nothing should be wrapped or renamed --
        # the tool's own message is the most useful thing to show.
        composite = CompositeBackend([self.Broken()])
        with self.assertRaises(PyGUITestError) as caught:
            composite.capture()
        self.assertEqual(
            str(caught.exception), "gnome-screenshot did not finish within 15s"
        )

    def test_when_every_member_fails_the_error_names_them_all(self):
        composite = CompositeBackend([self.Broken(), self.Broken()])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with self.assertRaises(PyGUITestError) as caught:
                composite.capture()
        message = str(caught.exception)
        self.assertIn("every capture backend failed", message)
        # Both members named, each with its own error beside it.
        self.assertEqual(message.count("capture:gnome-screenshot:"), 2)
        self.assertEqual(message.count("did not finish within 15s"), 2)

    def test_a_caller_mistake_is_not_retried_against_every_member(self):
        # A malformed region would fail identically everywhere, so it must
        # propagate rather than being mistaken for a broken backend.
        with self.assertRaises(ValueError):
            self.composite.capture(region=(0, 0, 0, 0))
        self.assertEqual(self.broken.calls, 0)
        self.assertEqual(self.working.calls, [])

    def test_the_final_error_names_only_a_path_that_was_not_tried(self):
        # Recommending a member the same sentence just reported broken is
        # worse than saying nothing. portalcapture is registered opt_in, so
        # automatic composition never includes it -- it is the one path a
        # reader has genuinely not exhausted.
        composite = CompositeBackend([self.Broken(), self.Broken()])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with self.assertRaises(PyGUITestError) as caught:
                composite.capture()
        message = str(caught.exception)
        self.assertIn('backend="portalcapture"', message)
        self.assertNotIn('backend="x11"', message)

    def test_the_fallback_warning_stays_short_enough_to_read(self):
        # It quotes the member's own error, so a long error message there
        # becomes an unreadable warning here -- which is exactly what
        # happened live before the tool messages were trimmed.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.composite.capture()
        self.assertLess(len(str(caught[0].message)), 240)

    def test_no_capture_member_at_all_points_at_the_opt_in_path(self):
        # The normal state of a GNOME Wayland session once broken tools stop
        # being selected: nothing is composed automatically, and the error
        # arrives exactly when someone wanted a screenshot -- which is when
        # they most need to know what to try next.
        composite = CompositeBackend([TestWindowHandlesStayWithTheirOwnBackend.Shell()])
        with self.assertRaises(CapabilityUnsupported) as caught:
            composite.capture()
        message = str(caught.exception)
        self.assertIn("SCREEN_CAPTURE", message)
        self.assertIn('backend="portalcapture"', message)

    def test_the_same_advice_appears_for_a_window_capture(self):
        # Two code paths report this; they must not drift apart.
        composite = CompositeBackend([TestWindowHandlesStayWithTheirOwnBackend.Shell()])
        window = composite.windows()[0]
        with self.assertRaises(CapabilityUnsupported) as caught:
            composite.capture(window=window)
        self.assertIn('backend="portalcapture"', str(caught.exception))

    def test_native_window_capture_works_with_no_screen_capture_member(self):
        # The exact shape of a GNOME Wayland session, measured: X11Backend
        # provides WINDOW_CAPTURE, because GetImage on an X11 client's own
        # drawable succeeds under XWayland, while SCREEN_CAPTURE has no
        # provider at all -- the root is unreadable and no screenshot tool
        # can run there. Requiring a pixel-grabbing member up front refused
        # a capture that would have worked.
        class WindowOnly(GUIBackend):
            """Captures a window it issued, and nothing else."""

            name = "x11"
            capabilities = CapabilitySet({Capability.WINDOW_CAPTURE})

            def __init__(self):
                self.calls = []

            def windows(self):
                return [Window(handle=object(), backend=self, title="Editor")]

            def capture(self, window=None, path=None, region=None):
                self.calls.append(window)
                return "/tmp/window.png"

        member = WindowOnly()
        composite = CompositeBackend([member])
        window = member.windows()[0]
        self.assertEqual(composite.capture(window=window), "/tmp/window.png")
        self.assertIs(member.calls[0], window)

    def test_a_whole_screen_capture_still_needs_a_screen_capture_member(self):
        # The same composite cannot shoot the desktop, and must say so
        # rather than quietly handing back one window.
        class WindowOnly(GUIBackend):
            """Captures a window it issued, and nothing else."""

            name = "x11"
            capabilities = CapabilitySet({Capability.WINDOW_CAPTURE})

            def capture(self, window=None, path=None, region=None):
                return "/tmp/window.png"

        composite = CompositeBackend([WindowOnly()])
        with self.assertRaises(CapabilityUnsupported):
            composite.capture()
        with self.assertRaises(CapabilityUnsupported):
            composite.capture(region=(0, 0, 10, 10))
