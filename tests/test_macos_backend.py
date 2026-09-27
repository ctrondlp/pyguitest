"""The macOS backend, driven from a machine that is not a Mac.

Fake-driven for the same reason `tests/test_macos.py` is: what this backend can do is
decided by a TCC grant and by frameworks that only exist on a Mac, so a test that read
the real ones would pin the developer's own System Settings into the suite.
`ApplicationServices` and `Quartz` are stand-ins installed in `sys.modules` -- the
shape `tests/test_x11.py` established for `Xlib` and `tests/test_macos.py` for
`Quartz` -- and the four `_macapi` preflights are patched.

What a fake cannot answer lives in `backends/macos.py`'s own docstring as measurements
and in `docs/validation.md` as a record: the live shape of the window server's lists,
what AX answers for a real application, and the cost of a walk. The two bugs the first
live run found -- a constant PyObjC does not publish, and a stale join after a window
write -- are pinned here, because both reproduce without a Mac once the fake is shaped
like the real binding.
"""

import os
import re
import sys
import tempfile
import types
import unittest
from unittest import mock

from pyguitest import backends, session, tools
from pyguitest.backends import capture as capture_backend
from pyguitest.backends import macos
from pyguitest.backends.capture import ToolCaptureBackend
from pyguitest.backends.composite import CompositeBackend
from pyguitest.capabilities import Capability
from pyguitest.errors import (
    BackendUnavailable,
    CapabilityUnsupported,
    ElementNotActionable,
    ElementNotFound,
    PermissionRequired,
    PyGUITestError,
    WindowNotFound,
)
from pyguitest.roles import Role


def darwin_environment(**fields):
    """An `Environment` that looks like a Mac, with `fields` overridden."""
    every = {
        "session_type": session.SessionType.DARWIN,
        "compositor": session.Compositor.QUARTZ,
        "has_pyobjc_quartz": True,
        "has_pyobjc_application_services": True,
        "has_ax": True,
        "has_screen_recording": True,
        "has_post_event": True,
        "has_listen_event": True,
        "macos_version": "26.7",
    }
    every.update(fields)
    return session.Environment(**every)


class Constant:
    """One fake `kAX*` constant, remembered by name.

    An object rather than a string so a test can tell two apart, and with a `name` so
    the fake module can translate one back to the attribute it stands for -- the trick
    `tests/test_macos.py`'s FakeQuartz plays with its `kCG*` names.
    """

    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return self.name


class FakeValue:
    """What the fake's `AXValueCreate` hands back, and `AXValueGetValue` unwraps."""

    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


class FakeElement:
    """One fake AX element: attributes, children, and a record of what was done to it.

    `attrs` is keyed by the *name* of the AX attribute, which is what the fake module
    translates a `Constant` into. A missing name reads back as `-25205`
    (`kAXErrorAttributeUnsupported`) and a name whose value is None as `-25212`
    (`kAXErrorNoValue`) -- both measured on the live machine, and both answers this
    backend has to treat the same way.
    """

    def __init__(
        self, role=None, attrs=None, children=(), pid=None, actions=(), windows=None
    ):
        self.attrs = dict(attrs or {})
        if role is not None:
            self.attrs["kAXRoleAttribute"] = role
        if pid is not None:
            self.pid = pid
        self.children = list(children)
        # An application publishes its windows as both its children and its
        # `kAXWindowsAttribute`; a window element has children and no windows attribute.
        # Defaulting one to the other keeps the fixtures from having to say it twice.
        self.windows = list(children if windows is None else windows)
        self.actions = list(actions)
        self.written = []
        self.performed = []
        # An element whose every read fails: measured, -25202 for an element that has
        # gone and -25204 for a process that will not be interviewed.
        self.error = None


class FakeApplicationServices:
    """Stands in for PyObjC's `ApplicationServices`.

    Only the entry points `macos.py` calls are here, and `available()` is exactly
    "all of these exist", so a test that wants an older binding deletes one.
    """

    def __init__(self):
        self.calls = []
        self.applications = {}
        self.constants = {}
        self.system_wide = FakeElement(role="AXSystemWide")
        self.at_position = None
        self.prompted = []
        self.action_error = 0
        self.set_error = 0

    def __getattr__(self, name):
        if name.startswith("kAX"):
            # Cached, because the real binding's constants are stable module attributes
            # --
            # and a fresh object per read would make `Element._read`'s cache miss every
            # time, which is how a fake quietly stops testing what it is there to test.
            if name not in self.constants:
                self.constants[name] = Constant(name)
            return self.constants[name]
        raise AttributeError(name)

    def _record(self, name, *args):
        self.calls.append((name, args))
        return args

    def AXUIElementCreateApplication(self, pid):
        """The application element for `pid`, or one that will not be interviewed.

        Measured: every pid the window server named answered `AXApplication` except the
        Window Server's own, which answered -25204 for everything. So a pid no fixture
        registered is modelled as that rather than as a healthy anonymous application --
        otherwise `_applications` would report AX trees that do not exist.
        """
        if pid in self.applications:
            return self.applications[pid]
        unreachable = FakeElement(role="AXApplication", pid=pid)
        unreachable.error = -25204
        return unreachable

    def AXUIElementCreateSystemWide(self):
        return self.system_wide

    def AXUIElementCopyAttributeValue(self, element, attribute, _out):
        if element.error:
            return (element.error, None)
        name = attribute.name
        if name == "kAXChildrenAttribute":
            return (0, list(element.children))
        if name == "kAXWindowsAttribute":
            return (0, list(element.windows))
        if name not in element.attrs:
            return (-25205, None)
        value = element.attrs[name]
        return (-25212, None) if value is None else (0, value)

    def AXUIElementCopyActionNames(self, element, _out):
        if element.error:
            return (element.error, None)
        return (0, list(element.actions))

    def AXUIElementCopyElementAtPosition(self, element, x, y, _out):
        if self.at_position is None:
            return (-25204, None)
        return (0, self.at_position)

    def AXUIElementPerformAction(self, element, action):
        element.performed.append(action)
        return self.action_error

    def AXUIElementSetAttributeValue(self, element, attribute, value):
        element.written.append((attribute.name, value))
        return self.set_error

    def AXUIElementGetPid(self, element, _out):
        # The pair PyObjC returns, measured as `(0, 690)` for Terminal.
        return (0, getattr(element, "pid", None))

    def AXValueCreate(self, value_type, value):
        self._record("AXValueCreate", value_type.name)
        return FakeValue(value)

    def AXValueGetValue(self, value, value_type, _out):
        self._record("AXValueGetValue", value_type.name)
        return (True, value.value)

    def AXIsProcessTrusted(self):
        """The non-prompting preflight.

        Not what this backend reads -- the constructor asks `_macapi`'s ctypes one so a
        machine without the binding still gets an answer -- but it is one of the entry
        points `available()` checks, so the fake has to publish it.
        """
        return True

    def AXIsProcessTrustedWithOptions(self, options):
        self.prompted.append(options)
        return False


class FakeQuartz:
    """Stands in for PyObjC's `Quartz`, with the two window lists a real Mac has.

    Both lists are settable, because the join's whole design turns on the difference
    between them: the on-screen list is ordered front to back and carries only visible
    windows, and the all-windows list still lists a minimized one. Measured live, that
    difference is why `IsOnscreen` and not the layer is the filter.
    """

    def __init__(self, onscreen=(), everything=(), displays=()):
        self.onscreen = list(onscreen)
        self.everything = list(everything)
        self.displays = list(displays)
        self.calls = []
        self.point = types.SimpleNamespace(x=11.0, y=22.0)

    def __getattr__(self, name):
        # Three option names are integers, because the backend bitwise-ORs them and
        # the fake has to decide which list that asks for.
        if name == "kCGWindowListOptionOnScreenOnly":
            return 1
        if name == "kCGWindowListOptionAll":
            return 2
        if name == "kCGWindowListExcludeDesktopElements":
            return 4
        if name.startswith("kCG"):
            return Constant(name)
        raise AttributeError(name)

    def CGWindowListCopyWindowInfo(self, options, _window):
        self.calls.append(("CGWindowListCopyWindowInfo", options))
        return self.onscreen if options & 1 else self.everything

    def CGGetActiveDisplayList(self, maximum, _out, _count):
        """The real signature: a maximum to fill, then the used count.

        CoreGraphics publishes no count-only call -- see `backend.screens` -- so the
        fake models this one exactly as the binding does, including the fact that
        `maximum` bounds what comes back. `CGGetActiveDisplayCount`, which this fake
        used to provide, is a function that does not exist; providing it is what let
        `screens()` pass here and raise on the first real Mac.
        """
        self.calls.append(("CGGetActiveDisplayList", maximum))
        return (0, self.displays[:maximum], min(len(self.displays), maximum))

    def CGDisplayBounds(self, display):
        return types.SimpleNamespace(
            origin=types.SimpleNamespace(x=0.0, y=0.0),
            size=types.SimpleNamespace(
                width=display.points[0], height=display.points[1]
            ),
        )

    def CGDisplayScreenSize(self, display):
        return types.SimpleNamespace(width=display.millimetres[0], height=0.0)

    def CGDisplayPixelsWide(self, display):
        return display.pixels[0]

    def CGDisplayPixelsHigh(self, display):
        return display.pixels[1]

    def CGDisplayIsMain(self, display):
        return display.main

    def CGEventCreate(self, source):
        return "event"

    def CGEventGetLocation(self, event):
        return self.point

    def CGPointMake(self, x, y):
        return types.SimpleNamespace(x=float(x), y=float(y))

    def CGSizeMake(self, width, height):
        return types.SimpleNamespace(width=float(width), height=float(height))


def display(
    pixels=(1280, 800), points=(1280.0, 800.0), millimetres=(452.0, 283.0), main=True
):
    """One fake display: pixels, points, and its physical size in millimetres."""
    return types.SimpleNamespace(
        pixels=pixels, points=points, millimetres=millimetres, main=main
    )


def window(number, pid, owner, bounds, layer=0, onscreen=True, name=None, alpha=1.0):
    """One fake `CGWindowListCopyWindowInfo` entry.

    `onscreen=None` omits the key entirely, which is what the real all-windows list
    does for a window that is not showing: measured, only 15 of its 50 entries carried
    the key at all.
    """
    entry = {
        "kCGWindowNumber": number,
        "kCGWindowOwnerPID": pid,
        "kCGWindowOwnerName": owner,
        "kCGWindowLayer": layer,
        "kCGWindowAlpha": alpha,
        "kCGWindowBounds": {
            "X": float(bounds[0]),
            "Y": float(bounds[1]),
            "Width": float(bounds[2]),
            "Height": float(bounds[3]),
        },
    }
    if onscreen is not None:
        entry["kCGWindowIsOnscreen"] = onscreen
    if name is not None:
        entry["kCGWindowName"] = name
    return entry


def point(x, y):
    """An `AXValue`-shaped position, which is what an AX window publishes."""
    return FakeValue(types.SimpleNamespace(x=float(x), y=float(y)))


def size(width, height):
    """An `AXValue`-shaped size, which is what an AX window publishes."""
    return FakeValue(types.SimpleNamespace(width=float(width), height=float(height)))


class BackendTestCase(unittest.TestCase):
    """A backend built on the fakes, with the grant in place unless a test says no.

    `_macapi.accessibility_trusted` is patched rather than the backend's own
    attribute, so the real path is what is under test: the constructor reads the
    preflight, and the capability set is computed from what it answered.
    """

    def make(
        self,
        *,
        trusted=True,
        request=False,
        onscreen=(),
        everything=(),
        displays=(),
        applications=None,
    ):
        """Build a backend over fresh fakes, and hand it back."""
        self.ax = FakeApplicationServices()
        self.quartz = FakeQuartz(
            onscreen=onscreen, everything=everything, displays=displays
        )
        for pid, element in (applications or {}).items():
            self.ax.applications[pid] = element
        modules = mock.patch.dict(
            sys.modules, {"ApplicationServices": self.ax, "Quartz": self.quartz}
        )
        modules.start()
        self.addCleanup(modules.stop)
        granted = mock.patch(
            "pyguitest.backends._macapi.accessibility_trusted", return_value=trusted
        )
        granted.start()
        self.addCleanup(granted.stop)
        return macos.MacosBackend(darwin_environment(), request=request)

    @staticmethod
    def onscreen_windows():
        """The six windows a person could see on the live machine.

        Terminal's window and TextEdit's four, in the front-to-back order the
        on-screen list reported them in -- Terminal first, which is what
        `active_window` reads back.
        """
        return [
            window(48, 690, "Terminal", (153, 79, 877, 499)),
            window(268, 2829, "TextEdit", (205, 145, 673, 439)),
            window(265, 2829, "TextEdit", (176, 116, 673, 439)),
            window(263, 2829, "TextEdit", (147, 87, 673, 439)),
            window(256, 2829, "TextEdit", (118, 58, 673, 439)),
        ]

    @classmethod
    def everything_windows(cls):
        """The same six, plus what the live all-windows list added.

        Three shapes of noise, all measured: a layer-0 helper surface that is not on
        screen (a 1280x30 menu-bar strip), an on-screen window at another layer (the
        Dock), and an on-screen Window Server entry carrying the one name
        CoreGraphics was willing to give up. A minimized TextEdit window is in here
        too -- on screen false -- because that is the case `geometry` and
        `is_window_viewable` exist for.
        """
        return cls.onscreen_windows() + [
            window(44, 690, "Terminal", (0, 0, 1280, 30), onscreen=None),
            window(269, 606, "CursorUIViewService", (0, 736, 64, 64), onscreen=None),
            window(12, 416, "Dock", (0, 0, 1280, 800), layer=20),
            window(
                21, 199, "Window Server", (0, 0, 1280, 30), layer=24, name="Menubar"
            ),
            window(30, 417, "Control Center", (1057, 0, 31, 30), layer=25),
            window(255, 2829, "TextEdit", (0, 300, 500, 500), onscreen=None),
        ]

    @staticmethod
    def applications():
        """The AX side of the join, with the titles CoreGraphics withheld."""
        textedit = [
            FakeElement(
                role="AXWindow",
                attrs={
                    "kAXTitleAttribute": "live_doc4.txt",
                    "kAXPositionAttribute": point(205, 145),
                    "kAXSizeAttribute": size(673, 439),
                },
            ),
            FakeElement(
                role="AXWindow",
                attrs={
                    "kAXTitleAttribute": "live_doc3.txt",
                    "kAXPositionAttribute": point(176, 116),
                    "kAXSizeAttribute": size(673, 439),
                },
            ),
            FakeElement(
                role="AXWindow",
                attrs={
                    "kAXTitleAttribute": "live_doc2.txt",
                    "kAXPositionAttribute": point(147, 87),
                    "kAXSizeAttribute": size(673, 439),
                },
            ),
            FakeElement(
                role="AXWindow",
                attrs={
                    "kAXTitleAttribute": "live_doc.txt",
                    "kAXPositionAttribute": point(118, 58),
                    "kAXSizeAttribute": size(673, 439),
                },
            ),
        ]
        terminal_window = FakeElement(
            role="AXWindow",
            attrs={
                "kAXTitleAttribute": "pyguitest - caffeinate",
                "kAXPositionAttribute": point(153, 79),
                "kAXSizeAttribute": size(877, 499),
                "kAXMinimizedAttribute": False,
            },
            actions=("AXRaise",),
        )
        return {
            2829: FakeElement(
                role="AXApplication",
                attrs={"kAXTitleAttribute": "TextEdit"},
                children=textedit,
                pid=2829,
            ),
            690: FakeElement(
                role="AXApplication",
                attrs={"kAXTitleAttribute": "Terminal"},
                children=[terminal_window],
                pid=690,
            ),
            416: FakeElement(
                role="AXApplication",
                attrs={"kAXTitleAttribute": "Dock"},
                children=[],
                pid=416,
            ),
        }


class TestTheRoleTable(unittest.TestCase):
    """The data half: AX's role names, mapped to what this package reports."""

    def test_every_mapped_role_is_an_atspi_name(self):
        # The values are at-spi's, which is what makes a script portable: a role
        # asked for with `Role.PUSH_BUTTON` has to match on a Mac as it does on Linux.
        # Each value is either a `Role` constant or one of the names at-spi reports
        # with no constant beside it -- the distinction uia.py's
        # `_ATSPI_NAMES_WITHOUT_CONSTANTS` draws for the same reason.
        constants = {
            value
            for name, value in vars(Role).items()
            if not name.startswith("_") and isinstance(value, str)
        }
        nameless = {"application", "split pane", "menu bar", "thumb", "unknown"}
        for ax_name, reported in macos._AX_ROLES.items():
            self.assertTrue(ax_name.startswith("AX"), ax_name)
            self.assertIn(reported, constants | nameless, f"{ax_name} -> {reported}")

    def test_a_role_the_table_does_not_know_is_spelled_out(self):
        # The honest answer for a role this package has never heard of: AX
        # camel-cases its names, so the words are in the string already. Reporting
        # `unknown` would claim the element never answered.
        self.assertEqual(macos._ax_role("AXColorWell"), "color well")
        self.assertEqual(macos._ax_role("AXRowSpanGrid"), "row span grid")

    def test_a_role_nothing_answered_is_unknown(self):
        self.assertEqual(macos._ax_role(""), "unknown")
        self.assertEqual(macos._ax_role(None), "unknown")

    def test_the_thumb_is_not_a_scroll_bar(self):
        # uia.py's argument, applying identically here: an AXValueIndicator is the
        # *child* of the bar, so calling it one makes a scroll-bar search report two
        # elements per bar on a Mac and one on Linux. Measured live: Terminal's scroll
        # area walks as AXScrollArea -> AXScrollBar and AXValueIndicator, one each.
        self.assertEqual(macos._ax_role("AXValueIndicator"), "thumb")
        self.assertEqual(macos._ax_role("AXScrollBar"), Role.SCROLL_BAR)

    def test_a_search_accepts_every_spelling_of_a_role(self):
        # `roles.py`'s alias table is what keeps `button` matching a desktop that says
        # `push button`; it has to work the same way through this table.
        self.assertIn("AXButton", macos._ax_roles(Role.PUSH_BUTTON))
        self.assertIn("AXButton", macos._ax_roles("button"))
        self.assertIsNone(macos._ax_roles(None))

    def test_a_raw_ax_role_can_be_searched_for(self):
        # A caller who read `AXTextField` off a live tree should be able to ask for it
        # by the name AX uses.
        self.assertEqual(macos._ax_roles("AXTextField"), {"AXTextField"})

    def test_a_role_nothing_reports_matches_nothing(self):
        # `{role}` rather than an empty set: a search for a role this backend never
        # produces should come back empty because no element has it, not because the
        # filter was silently dropped.
        self.assertEqual(macos._ax_roles("calendar"), {"calendar"})


class TestTheActionTable(BackendTestCase):
    """AX action names, as this package publishes and accepts them."""

    def test_the_press_is_published_as_a_click(self):
        # `actions` is what a caller prints and hands back to `do_action`, and a script
        # written against at-spi says `click`. Measured live: TextEdit's title-bar
        # button reports `['click']` where AX publishes `AXPress`.
        self.assertEqual(macos._AX_ACTIONS["AXPress"], "click")

    def test_every_published_name_resolves_back_to_its_action(self):
        for ax_name, published in macos._AX_ACTIONS.items():
            self.assertEqual(macos._ax_action(published), ax_name)

    def test_atspi_spellings_are_accepted(self):
        # `activate` is at-spi's word for a press, and uia.py accepts the same two
        # spellings for the same reason.
        for spelling in ("click", "press", "activate"):
            self.assertEqual(macos._ax_action(spelling), "AXPress")

    def test_a_raw_ax_name_is_accepted(self):
        self.assertEqual(macos._ax_action("AXShowMenu"), "AXShowMenu")

    def test_an_unknown_name_is_not_an_action(self):
        # None rather than a guess: `do_action` turns this into a ValueError naming
        # what the element does offer, which is what `press_key` does with a key it
        # does not know.
        self.assertIsNone(macos._ax_action("frobnicate"))

    def test_an_elements_actions_are_published_in_ax_order(self):
        # AX lists an element's actions in the order it prefers them, and the first is
        # what a press should use, so the translation is a map and not a sort -- and
        # this is also where `AXUIElementCopyActionNames` is exercised, the call that
        # replaced a constant PyObjC does not publish. The first live run of this
        # backend raised `AttributeError` here instead.
        backend = self.make()
        node = FakeElement(role="AXButton", actions=("AXShowMenu", "AXPress"))
        self.assertEqual(macos.Element(node, backend).actions, ["show menu", "click"])

    def test_an_element_offering_nothing_reports_no_actions(self):
        # An empty list is a truthful answer about a text area, measured live: the one
        # at the centre of Terminal's window reported `[]` while the title-bar button
        # beside it reported `['click']`.
        backend = self.make()
        node = FakeElement(role="AXTextArea")
        self.assertEqual(macos.Element(node, backend).actions, [])

    def test_an_element_that_will_not_answer_reports_no_actions(self):
        # A process that will not be interviewed answers -25204 for everything,
        # including this call: measured on the Window Server's own pid.
        backend = self.make()
        node = FakeElement(role="AXButton", actions=("AXPress",))
        node.error = -25204
        self.assertEqual(macos.Element(node, backend).actions, [])


class TestWhatTheGrantBuys(BackendTestCase):
    """ADR 004 §6: the capability set follows the permission, and nothing more."""

    def test_with_the_grant_everything_is_declared(self):
        backend = self.make(trusted=True)
        for capability in (
            Capability.ELEMENT_TREE,
            Capability.ELEMENT_ACTION,
            Capability.ELEMENT_GEOMETRY,
            Capability.WINDOW_LIST,
            Capability.WINDOW_STATE,
            Capability.WINDOW_GEOMETRY,
            Capability.WINDOW_ACTIVATE,
            Capability.WINDOW_PID,
            Capability.WINDOW_AT_POINT,
            Capability.WINDOW_PLACEMENT,
            Capability.WINDOW_RESIZE,
            Capability.WINDOW_MINIMIZE,
            Capability.SCREEN_INFO,
            Capability.POINTER_QUERY,
        ):
            self.assertIn(capability, backend.capabilities)

    def test_without_it_the_element_family_is_withdrawn(self):
        # What Accessibility actually gates: the three element capabilities and the
        # AX-backed window writes. Claiming them anyway would hand a caller an empty
        # tree and a placement call that raises, with `supports()` saying both were
        # fine.
        backend = self.make(trusted=False)
        for capability in (
            Capability.ELEMENT_TREE,
            Capability.ELEMENT_ACTION,
            Capability.ELEMENT_GEOMETRY,
            Capability.WINDOW_PLACEMENT,
            Capability.WINDOW_RESIZE,
            Capability.WINDOW_MINIMIZE,
        ):
            self.assertNotIn(capability, backend.capabilities)

    def test_the_grant_does_not_take_away_what_it_does_not_gate(self):
        # Screen Recording, PostEvent and the window server's own list are separate
        # services, and withdrawing these would report the machine as less able than it
        # is -- the same dishonesty in the other direction.
        backend = self.make(trusted=False)
        for capability in (
            Capability.SCREEN_INFO,
            Capability.POINTER_QUERY,
            Capability.WINDOW_LIST,
            Capability.WINDOW_STATE,
            Capability.WINDOW_GEOMETRY,
            Capability.WINDOW_ACTIVATE,
            Capability.WINDOW_PID,
            Capability.WINDOW_AT_POINT,
        ):
            self.assertIn(capability, backend.capabilities)

    def test_the_permanent_refusals_are_absent_either_way(self):
        # ADR 004 §7: a capability rather than a raise, so a caller finds out from
        # supports() instead of from a failure.
        for trusted in (True, False):
            backend = self.make(trusted=trusted)
            for capability in (
                Capability.WINDOW_TITLE_SET,
                Capability.WINDOW_LOWER,
                Capability.WINDOW_CURSOR_QUERY,
                Capability.INPUT_SYNC,
                Capability.WINDOW_EVENTS,
                Capability.SCREEN_CAPTURE,
                Capability.CLIPBOARD,
                Capability.INPUT_STATE_QUERY,
            ):
                self.assertNotIn(capability, backend.capabilities, capability.name)

    def test_the_window_list_still_answers_without_the_grant(self):
        # The capability claim and the code agreeing: `windows` is CoreGraphics plus
        # the join, and what the grant adds is the titles.
        backend = self.make(trusted=False, onscreen=self.onscreen_windows())
        windows = backend.windows()
        self.assertEqual(len(windows), 5)
        self.assertEqual([w.title for w in windows], [""] * 5)
        self.assertEqual([w.app_id for w in windows[1:]], ["TextEdit"] * 4)


class TestConstructionAndRegistration(BackendTestCase):
    """Reaching the frameworks, asking only when asked, and the registry entry."""

    def test_available_is_false_without_the_binding(self):
        with mock.patch.dict(sys.modules, {"ApplicationServices": None}):
            self.assertFalse(macos.available())

    def test_available_needs_every_entry_point_it_calls(self):
        # A binding can be importable and older than the functions this module calls,
        # which
        # is a different problem with a different fix -- and the reason `_NEEDED` is
        # checked
        # rather than assumed.
        complete = types.ModuleType("ApplicationServices")
        for name in macos._NEEDED:
            setattr(complete, name, lambda *args: None)
        with mock.patch.dict(sys.modules, {"ApplicationServices": complete}):
            self.assertTrue(macos.available())
            for name in macos._NEEDED:
                with self.subTest(missing=name):
                    delattr(complete, name)
                    self.assertFalse(macos.available())
                    setattr(complete, name, lambda *args: None)

    def test_a_backend_that_cannot_be_built_says_which_binding_is_missing(self):
        with mock.patch.dict(sys.modules, {"ApplicationServices": None}):
            with self.assertRaises(BackendUnavailable) as caught:
                macos.MacosBackend(None)
            self.assertIn("ApplicationServices", str(caught.exception))

    def test_constructing_does_not_ask_by_default(self):
        # The rule ADR 004 §4 keeps: a plain `connect()` must not put a dialog on
        # somebody's
        # screen, so the *requesting* form is not called unless a caller says so --
        # which is
        # why this backend is not registered `opt_in` the way `macquartz` is.
        self.make(trusted=False)
        self.assertEqual(self.ax.prompted, [])

    def test_requesting_is_what_asks(self):
        self.make(trusted=False, request=True)
        self.assertEqual(len(self.ax.prompted), 1)

    def test_a_grant_already_held_is_not_asked_for_again(self):
        self.make(trusted=True, request=True)
        self.assertEqual(self.ax.prompted, [])

    def test_constructing_records_what_the_preflight_answered(self):
        self.assertTrue(self.make(trusted=True).trusted)
        self.assertFalse(self.make(trusted=False).trusted)

    def test_the_registry_builds_it_without_being_named(self):
        # A plain `connect()` on a Mac has to get elements, so this backend is in
        # automatic
        # composition rather than `opt_in`. The registry is replaced rather than added
        # to,
        # so this is a test of `register` + `select` for this backend rather than a test
        # of
        # which other factories happen to work on the machine running it.
        registry = mock.patch.object(backends, "_REGISTRY", [])
        registry.start()
        self.addCleanup(registry.stop)
        backends.register(backends._macos_factory, "macos", priority=90)
        self.make(trusted=False)
        self.assertIn("macos", backends.available())
        self.assertIsInstance(backends.select(darwin_environment()), macos.MacosBackend)

    def test_naming_it_forwards_the_request_option(self):
        # `connect(backend="macos", backend_options={"request": True})` is the caller
        # asking for the grant, which is `opt_in`'s rule spelled as an option.
        registry = mock.patch.object(backends, "_REGISTRY", [])
        registry.start()
        self.addCleanup(registry.stop)
        backends.register(backends._macos_factory, "macos", priority=90)
        self.make(trusted=False)
        selected = backends.select(darwin_environment(), "macos", {"request": True})
        self.assertTrue(selected.requested)
        self.assertEqual(len(self.ax.prompted), 1)


class TestTheWindowJoin(BackendTestCase):
    """CoreGraphics is the source of truth, Accessibility the enrichment."""

    def setUp(self):
        self.backend = self.make(
            onscreen=self.onscreen_windows(),
            everything=self.everything_windows(),
            applications=self.applications(),
        )

    def test_only_the_windows_a_person_can_see_are_listed(self):
        # The measurement the filter comes from: the live all-windows list names 50
        # windows with 36 at layer 0, and only 15 carry the on-screen key. The *pair* is
        # exact, and each field alone over-reports -- a layer-0 helper strip is not on
        # screen, and the Dock is on screen at layer 20.
        self.assertEqual(
            [w.title for w in self.backend.windows()],
            [
                "pyguitest - caffeinate",
                "live_doc4.txt",
                "live_doc3.txt",
                "live_doc2.txt",
                "live_doc.txt",
            ],
        )

    def test_a_coregraphics_name_is_the_fallback_where_ax_has_none(self):
        # The one name CoreGraphics gave up on the live machine was the Window Server's
        # own menu bar, so the fallback is real rather than theoretical: it is used
        # where
        # a window has a name and no AX counterpart to enrich it from.
        onscreen = [
            window(300, 999, "SomeApp", (0, 0, 100, 100), name="From CoreGraphics")
        ]
        backend = self.make(onscreen=onscreen)
        self.assertEqual([w.title for w in backend.windows()], ["From CoreGraphics"])

    def test_the_join_matches_by_process_and_rectangle(self):
        # TextEdit's four windows differ in both, and the AX side answers each with its
        # own title -- so a title landing against the right handle is the frame match
        # working, not a lookup by order.
        titles = {w.handle: w.title for w in self.backend.windows()}
        self.assertEqual(titles[268], "live_doc4.txt")
        self.assertEqual(titles[256], "live_doc.txt")
        self.assertEqual(titles[48], "pyguitest - caffeinate")

    def test_a_malformed_ax_window_list_degrades_rather_than_mismatches(self):
        # Measured: Finder answers `kAXWindowsAttribute` with an `AXScrollArea` rather
        # than with its windows, and that element's rect matches nothing on screen. A
        # wrong AX list has to leave the title empty rather than pick a wrong one.
        onscreen = self.onscreen_windows() + [
            window(400, 421, "Finder", (10, 10, 200, 200))
        ]
        applications = self.applications()
        applications[421] = FakeElement(
            role="AXApplication",
            attrs={},
            children=[
                FakeElement(
                    role="AXScrollArea",
                    attrs={
                        "kAXTitleAttribute": None,
                        "kAXPositionAttribute": point(500, 300),
                        "kAXSizeAttribute": size(500, 500),
                    },
                )
            ],
        )
        backend = self.make(onscreen=onscreen, applications=applications)
        self.assertEqual({w.handle: w.title for w in backend.windows()}[400], "")

    def test_a_minimized_window_leaves_the_list_but_keeps_answering(self):
        # Measured: a minimized window keeps its CoreGraphics entry and loses the
        # on-screen field, and nothing grant-free tells it apart from the helper
        # surfaces -- so it is not listed, and `geometry`/`is_window_viewable` are how a
        # caller that already holds the handle asks about it.
        self.assertNotIn(255, [w.handle for w in self.backend.windows()])
        held = macos.Window(255, self.backend, pid=2829)
        self.assertEqual(self.backend.geometry(held), (0, 300, 500, 500))
        self.assertFalse(self.backend.is_window_viewable(held))

    def test_the_windows_are_in_the_servers_own_order(self):
        # The on-screen list is front to back and the full enumeration is not: measured,
        # the same Terminal window was in front of TextEdit's four on screen and behind
        # them in the all-windows one. `active_window` is the first entry of this list.
        self.assertEqual(self.backend.active_window().handle, 48)

    def test_active_window_does_not_ask_which_application_has_focus(self):
        # `kAXFocusedApplicationAttribute` measured flaky -- err=0 with Terminal's pid
        # on one run and -25204 on the next, same shell, same desktop -- so the ordering
        # comes from the window server and this must not touch the system-wide element.
        self.assertEqual(self.backend.active_window().title, "pyguitest - caffeinate")
        self.assertNotIn(
            "AXUIElementCreateSystemWide", [name for name, _ in self.ax.calls]
        )

    def test_no_windows_at_all_answers_none(self):
        self.assertIsNone(self.make(onscreen=[]).active_window())

    def test_a_point_inside_a_window_finds_it(self):
        self.assertEqual(self.backend.window_at(600, 400).handle, 48)

    def test_a_point_on_the_dock_or_the_menu_bar_finds_nothing(self):
        # The Dock's fake entry covers 0,0-1280x800 at layer 20 and the menu bar is at
        # layer 24, so both contain this point and neither is a window.
        self.assertIsNone(self.backend.window_at(0, 10))

    def test_geometry_of_a_window_that_is_gone_is_a_typed_refusal(self):
        # Window numbers are recycled the way X11 ids are, so a stale handle is an
        # ordinary case rather than an exceptional one, and the refusal names it.
        gone = macos.Window(9999, self.backend, pid=2829)
        with self.assertRaises(WindowNotFound):
            self.backend.geometry(gone)

    def test_one_listing_is_one_enumeration(self):
        # The cache's whole purpose: one search must not pay one enumeration per node.
        def enumerations():
            return len(
                [
                    call
                    for call in self.quartz.calls
                    if call[0] == "CGWindowListCopyWindowInfo"
                ]
            )

        before = enumerations()
        self.backend.windows()
        self.backend.windows()
        self.assertEqual(enumerations() - before, 1)


class TestWindowWrites(BackendTestCase):
    """The AX-backed window writes, which is what the Accessibility grant buys."""

    def setUp(self):
        self.backend = self.make(
            onscreen=self.onscreen_windows(),
            everything=self.everything_windows(),
            applications=self.applications(),
        )
        self.terminal = [w for w in self.backend.windows() if w.handle == 48][0]
        self.node = self.ax.applications[690].windows[0]

    def test_moving_a_window_writes_its_ax_position(self):
        self.backend.move_window(self.terminal, 200, 120)
        name, value = self.node.written[-1]
        self.assertEqual(name, "kAXPositionAttribute")
        self.assertEqual((value.value.x, value.value.y), (200.0, 120.0))

    def test_resizing_a_window_writes_its_ax_size(self):
        self.backend.resize_window(self.terminal, 800, 600)
        name, value = self.node.written[-1]
        self.assertEqual(name, "kAXSizeAttribute")
        self.assertEqual((value.value.width, value.value.height), (800.0, 600.0))

    def test_the_two_writes_use_the_types_ax_expects(self):
        # `kAXValueCGPointType` for a position and `kAXValueCGSizeType` for a size:
        # measured, a mismatched type raises rather than answering, so this is not
        # cosmetic.
        self.backend.move_window(self.terminal, 1, 2)
        self.backend.resize_window(self.terminal, 3, 4)
        created = [args[0] for name, args in self.ax.calls if name == "AXValueCreate"]
        self.assertEqual(created, ["kAXValueCGPointType", "kAXValueCGSizeType"])

    def test_a_write_drops_the_cached_enumerations(self):
        # The bug the first live run found: the join resolves an AX window by matching
        # it
        # against a CoreGraphics rectangle, so a rectangle cached before a move makes
        # the
        # *next* write fail with "no AX window matches its handle" -- which is what
        # happened on a move-then-resize round trip.
        self.backend.windows()
        self.assertIsNotNone(self.backend._cache_onscreen)
        self.backend.move_window(self.terminal, 200, 120)
        self.assertIsNone(self.backend._cache_onscreen)
        self.assertIsNone(self.backend._cache_all)

    def test_a_second_write_after_a_move_still_finds_the_window(self):
        # The same bug as a caller meets it, rather than as the cache shows it: the
        # window's
        # rectangle has moved, so the join has to be re-read to find it. Both sides move
        # here -- the AX element and the window server's entry -- because that is what
        # the
        # live machine did: `geometry()` read back the new rectangle after a settle.
        self.backend.move_window(self.terminal, 200, 120)
        self.node.attrs["kAXPositionAttribute"] = point(200, 120)
        for entry in self.quartz.onscreen + self.quartz.everything:
            if entry["kCGWindowNumber"] == 48:
                entry["kCGWindowBounds"].update({"X": 200.0, "Y": 120.0})
        self.backend.resize_window(self.terminal, 800, 600)
        self.assertEqual(self.node.written[-1][0], "kAXSizeAttribute")

    def test_a_moved_window_is_found_again_within_a_point_or_two(self):
        # Between an AX write landing and the window server reporting it, the two
        # rectangles can differ by a rounding step. Two real windows of one process are
        # never that close without overlapping, so the tolerance cannot pick the wrong
        # window -- and without it, a write after a move is the one that fails.
        self.node.attrs["kAXPositionAttribute"] = point(154, 80)
        self.backend.resize_window(self.terminal, 800, 600)
        self.assertEqual(self.node.written[-1][0], "kAXSizeAttribute")

    def test_a_write_to_a_window_whose_ax_element_is_missing_is_refused(self):
        # A handle the AX side cannot match -- stale, or an application whose window
        # list
        # is malformed -- is a typed refusal rather than a write aimed at whatever came
        # first in the list.
        gone = macos.Window(9999, self.backend, pid=2829)
        with self.assertRaises(CapabilityUnsupported) as caught:
            self.backend.move_window(gone, 1, 1)
        self.assertIn("no AX window matches its handle", str(caught.exception))

    def test_a_refused_write_names_the_capability_it_needed(self):
        # Placement and resize are separate capabilities, so a caller reading the error
        # can tell which one to stop depending on.
        gone = macos.Window(9999, self.backend, pid=2829)
        for method, capability in (
            (self.backend.move_window, Capability.WINDOW_PLACEMENT),
            (self.backend.resize_window, Capability.WINDOW_RESIZE),
        ):
            with self.assertRaises(CapabilityUnsupported) as caught:
                method(gone, 1, 1)
            self.assertIs(caught.exception.capability, capability)

    def test_a_refusal_says_which_write_it_was(self):
        # Measured, and the reason `_write_frame` takes a verb: the message used to say
        # "could not move" for a resize, which is worse than saying nothing.
        self.ax.set_error = -25205
        with self.assertRaises(CapabilityUnsupported) as caught:
            self.backend.resize_window(self.terminal, 1, 1)
        self.assertIn("could not resize", str(caught.exception))
        self.assertIn("-25205", str(caught.exception))

    def test_minimizing_writes_the_attribute(self):
        self.backend.minimize_window(self.terminal)
        self.assertEqual(self.node.written[-1], ("kAXMinimizedAttribute", True))
        self.backend.minimize_window(self.terminal, minimized=False)
        self.assertEqual(self.node.written[-1], ("kAXMinimizedAttribute", False))

    def test_every_write_needs_the_grant(self):
        # The capability set and the code agreeing: without Accessibility these refuse
        # before touching AX, which is what `_AX_ONLY` promises.
        backend = self.make(trusted=False, onscreen=self.onscreen_windows())
        window = macos.Window(48, backend, pid=690)
        for call in (
            lambda: backend.move_window(window, 1, 1),
            lambda: backend.resize_window(window, 1, 1),
            lambda: backend.minimize_window(window),
        ):
            with self.assertRaises(CapabilityUnsupported):
                call()

    def test_raising_a_window_uses_the_action_ax_publishes(self):
        # Measured live: Terminal's window reports `['raise']` and accepts it.
        self.backend.activate_window(self.terminal)
        self.assertEqual(self.node.performed, ["AXRaise"])
        self.assertIsNone(self.backend._cache_onscreen)

    def test_activating_a_window_the_server_no_longer_lists_is_a_refusal(self):
        gone = macos.Window(9999, self.backend, pid=2829)
        with self.assertRaises(WindowNotFound):
            self.backend.activate_window(gone)

    def test_activation_without_appkit_writes_the_frontmost_attribute(self):
        # `WINDOW_ACTIVATE` is in `_UNGATED` because this route needs nothing: no
        # grant, so no `AXRaise`, and no AppKit, so the application element is asked
        # to come forward instead.
        #
        # AppKit is patched out rather than left to the machine, which is how this test
        # was written wrong: on a Mac `NSRunningApplication` answers -- pid 690 is a
        # live Terminal on the machine this was measured on -- so the route below never
        # ran and the assertion failed on the one platform the code is written for,
        # while passing on a CI runner where the import cannot succeed. Found by
        # running the suite on the macOS 26.7 VM.
        backend = self.make(
            trusted=False,
            onscreen=self.onscreen_windows(),
            applications=self.applications(),
        )
        window = macos.Window(48, backend, pid=690)
        with mock.patch.object(macos, "_import", return_value=None):
            backend.activate_window(window)
        app = self.ax.applications[690]
        self.assertEqual(app.written[-1], ("kAXFrontmostAttribute", True))

    def test_activation_prefers_appkit_over_any_ax_write(self):
        # The other half of the same branch, and the one a Mac takes: bringing an
        # application forward through AppKit is the documented way and needs no grant
        # at all, so it is tried first and AX is not written to when it answers.
        backend = self.make(
            trusted=False,
            onscreen=self.onscreen_windows(),
            applications=self.applications(),
        )
        window = macos.Window(48, backend, pid=690)
        running = mock.Mock()
        running.activateWithOptions_.return_value = True
        appkit = types.SimpleNamespace(
            NSApplicationActivateAllWindows=1 << 9,
            NSRunningApplication=mock.Mock(
                runningApplicationWithProcessIdentifier_=mock.Mock(return_value=running)
            ),
        )
        with mock.patch.dict(sys.modules, {"AppKit": appkit}):
            backend.activate_window(window)
        running.activateWithOptions_.assert_called_once_with(1 << 9)
        self.assertEqual(self.ax.applications[690].written, [])


class TestScreensAndThePointer(BackendTestCase):
    """The two capabilities that need no grant at all."""

    def test_a_display_is_reported_in_pixels_with_its_dpi_scale(self):
        # `base.Screen` defines width/height as physical pixels and scale as DPI/96. A
        # display whose physical size works out to 96 DPI is a scale of exactly 1.0.
        backend = self.make(
            displays=[
                display(
                    pixels=(1280, 800),
                    points=(1280.0, 800.0),
                    millimetres=(338.67, 211.67),
                )
            ]
        )
        screen = backend.screens()[0]
        self.assertEqual((screen.index, screen.width, screen.height), (0, 1280, 800))
        self.assertEqual((screen.name, screen.scale), ("main", 1.0))

    def test_the_scale_is_dpi_over_ninety_six(self):
        # 300 mm wide at 1280 pixels is 108.4 DPI, which is 1.129 of 96 -- rounded to
        # three
        # places by `_scale` rather than to a convenient-looking number.
        backend = self.make(
            displays=[display(pixels=(1280, 800), millimetres=(300.0, 190.0))]
        )
        self.assertEqual(backend.screens()[0].scale, 1.129)

    def test_a_display_with_no_physical_size_falls_back_to_its_ratio(self):
        # A virtual display reports no physical size, and the fallback is the
        # pixel-per-point ratio: 1.0 at 1x and 2.0 at 2x, which is also what the DPI
        # route gives for a typical Retina panel.
        retina = display(
            pixels=(2880, 1800), points=(1440.0, 900.0), millimetres=(0.0, 0.0)
        )
        backend = self.make(displays=[retina])
        self.assertEqual(backend.screens()[0].scale, 2.0)

    def test_only_the_main_display_is_called_main(self):
        backend = self.make(
            displays=[display(main=True), display(main=False), display(main=False)]
        )
        self.assertEqual(
            [s.name for s in backend.screens()], ["main", "display1", "display2"]
        )

    def test_the_display_list_is_asked_for_with_a_maximum(self):
        # There is no count-only call in CoreGraphics, so the maximum is how the
        # count is obtained: `CGGetActiveDisplayList` fills up to it and answers how
        # many it used. Asking a non-existent `CGGetActiveDisplayCount` first is what
        # this replaced -- it raised `AttributeError` on the first real Mac.
        backend = self.make(displays=[display()])
        self.assertEqual(len(backend.screens()), 1)
        self.assertEqual(
            self.quartz.calls, [("CGGetActiveDisplayList", macos._MAX_ACTIVE_DISPLAYS)]
        )

    def test_no_quartz_entry_point_is_invented(self):
        """Every `self._quartz.<name>` in the module must be one it checked for.

        `available()` tests `_QUARTZ_NEEDED`, so a name outside that tuple is either a
        typo or a function the binding does not publish -- and the fake in this file
        is what decides which of the two a green suite can see.
        `CGGetActiveDisplayCount` was not in the tuple and not in Quartz, and the fake
        provided it anyway, so `screens()` passed on CI for as long as no Mac ran it.
        Reading the module's own text is the only check that scales to the next
        invented name.
        """
        import pathlib

        source = pathlib.Path(macos.__file__).read_text(encoding="utf-8")
        called = set(re.findall(r"self\._quartz\.([A-Za-z_][A-Za-z0-9_]*)", source))
        self.assertEqual(sorted(called - set(macos._QUARTZ_NEEDED)), [])

    def test_the_pointer_read_needs_no_grant(self):
        # ADR 004 §1 marks it "no grant at all", and it is the gap
        # `tests/test_macos.py` records: `macquartz` deliberately does not declare
        # `POINTER_QUERY`, and this backend is the one that does.
        backend = self.make(trusted=False)
        self.assertEqual(backend.pointer_position(), (11, 22))
        self.assertIn(Capability.POINTER_QUERY, backend.capabilities)


def counting(original, log):
    """A fake attribute read that records the name of the attribute it was asked for."""

    def read(element, attribute, out):
        log.append(attribute.name)
        return original(element, attribute, out)

    return read


class TestWindowCapture(BackendTestCase):
    """`screencapture -l`: the one route where this backend owns the pixels.

    Driven through `capture.py`'s own `_run_tool`, patched, so what is under test
    is the wiring -- which number goes to which command line, and that the grant is
    checked before the tool is spawned. The runner itself, its timeout and its two
    failure messages have their own tests in `tests/test_capture.py`.

    The question a fake cannot answer is the one this route still carries: what
    `-l` hands back for a real window -- whether the drop shadow comes with it, and
    whether the image is `geometry()`'s rectangle or the window's frame around
    that. Both are live-Mac measurements, and until one exists the image's content
    is trusted while its dimensions are not.
    """

    def make_capturing(self, granted=True, **kwargs):
        """A backend, and the argv of every tool invocation it makes.

        `granted` patches the Screen Recording preflight, for the same reason the
        AX one is patched by `make`: the real answer on anything but a Mac is no,
        and the gate is deliberately checked before the tool would run.
        """
        backend = self.make(**kwargs)
        calls = []

        def run_tool(argv):
            calls.append(argv)
            with open(argv[-1], "w", encoding="utf-8") as handle:
                handle.write("png-ish")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        patched = mock.patch.object(capture_backend, "_run_tool", run_tool)
        patched.start()
        self.addCleanup(patched.stop)
        allowed = mock.patch(
            "pyguitest.backends._macapi.screen_recording_allowed",
            return_value=granted,
        )
        allowed.start()
        self.addCleanup(allowed.stop)
        return backend, calls

    def destination(self):
        """An empty file to write a fake screenshot into, cleaned up after."""
        descriptor, path = tempfile.mkstemp(suffix=".png")
        os.close(descriptor)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path

    def test_the_capability_is_declared_with_and_without_the_ax_grant(self):
        # Screen Recording gates this route and Accessibility does not, so the AX
        # preflight has nothing to say about it. Declared either way, and refused at
        # the call -- ADR 004 §6 is about what AX gates and no more.
        self.assertIn(Capability.WINDOW_CAPTURE, self.make().capabilities)
        self.assertIn(Capability.WINDOW_CAPTURE, self.make(trusted=False).capabilities)

    def test_a_window_is_captured_by_the_number_it_carries(self):
        backend, calls = self.make_capturing(onscreen=self.onscreen_windows())
        window = backend.windows()[0]  # Terminal, whose number is 48.
        path = self.destination()
        self.assertEqual(backend.capture(window=window, path=path), path)
        self.assertEqual(calls, [["screencapture", "-x", "-o", "-l", "48", path]])

    def test_a_raw_handle_is_taken_as_itself(self):
        # `_issuer` allows a caller who passed a handle rather than a Window, and
        # this is the member a composite hands such a handle to.
        backend, calls = self.make_capturing(onscreen=self.onscreen_windows())
        path = self.destination()
        backend.capture(window=265, path=path)
        self.assertEqual(calls, [["screencapture", "-x", "-o", "-l", "265", path]])

    def test_a_denied_grant_refuses_before_the_tool_is_spawned(self):
        # A denied Screen Recording grant is exit 0 and a black image, so the gate
        # has to come first: nothing after it could tell that the tool had failed.
        backend, calls = self.make_capturing(
            granted=False, onscreen=self.onscreen_windows()
        )
        path = self.destination()
        with self.assertRaises(PermissionRequired) as raised:
            backend.capture(window=backend.windows()[0], path=path)
        self.assertIn("Screen Recording", str(raised.exception))
        self.assertEqual(calls, [])
        self.assertEqual(os.path.getsize(path), 0)

    def test_a_tool_that_writes_nothing_is_caught(self):
        # Exit 0 alone has already been measured to mean nothing on a Linux desktop;
        # this route shares that check rather than re-deriving it.
        backend = self.make(onscreen=self.onscreen_windows())
        path = self.destination()
        allowed = mock.patch(
            "pyguitest.backends._macapi.screen_recording_allowed", return_value=True
        )
        allowed.start()
        self.addCleanup(allowed.stop)
        patched = mock.patch.object(
            capture_backend,
            "_run_tool",
            lambda argv: types.SimpleNamespace(returncode=0, stdout="", stderr=""),
        )
        patched.start()
        self.addCleanup(patched.stop)
        with self.assertRaises(PyGUITestError) as raised:
            backend.capture(window=backend.windows()[0], path=path)
        self.assertIn("exited successfully but left", str(raised.exception))

    def test_whole_screen_capture_names_the_backend_that_serves_it(self):
        # Refused naming the route that does serve it, rather than reported as a
        # capability this backend does not declare: on a Mac a missing grant and a
        # missing route are different problems, and the tool that ships with the OS
        # needs nothing from here.
        backend, calls = self.make_capturing()
        with self.assertRaises(CapabilityUnsupported) as raised:
            backend.capture(path=self.destination())
        self.assertIn("capture:screencapture", str(raised.exception))
        self.assertEqual(calls, [])

    def test_a_region_beside_a_window_is_refused(self):
        backend, calls = self.make_capturing(onscreen=self.onscreen_windows())
        with self.assertRaises(ValueError):
            backend.capture(
                window=backend.windows()[0],
                region=(0, 0, 5, 5),
                path=self.destination(),
            )
        self.assertEqual(calls, [])

    def test_a_composite_routes_a_macos_window_here_rather_than_to_the_crop(self):
        # The reason the capability is declared on the backend that issues the
        # handles: route one of `CompositeBackend.capture` passes a window back only
        # to the member that issued it, so with the provider anywhere else this window
        # would be resolved to a rectangle and cropped out of a screen shot instead --
        # occluder and all.
        backend, calls = self.make_capturing(onscreen=self.onscreen_windows())
        tool = next(t for t in tools.CAPTURE_TOOLS if t.name == "screencapture")
        cropped = []
        composite = CompositeBackend(
            [backend, ToolCaptureBackend(tool, runner=cropped.append)]
        )
        path = self.destination()
        self.assertEqual(
            composite.capture(window=backend.windows()[0], path=path), path
        )
        self.assertEqual(calls, [["screencapture", "-x", "-o", "-l", "48", path]])
        self.assertEqual(cropped, [])


class TestElementReading(BackendTestCase):
    """One AX element, read through the Element interface."""

    def setUp(self):
        self.backend = self.make()

    def element(self, **kwargs):
        """An Element over a fake node built from `kwargs`."""
        return macos.Element(FakeElement(**kwargs), self.backend)

    def test_a_role_is_translated_rather_than_passed_through(self):
        self.assertEqual(self.element(role="AXButton").role, Role.PUSH_BUTTON)
        self.assertEqual(self.element(role="AXTextField").role, Role.ENTRY)
        self.assertEqual(self.element(role="AXWindow").role, Role.WINDOW)

    def test_a_title_is_the_name_and_a_description_is_the_fallback(self):
        # Measured: a control drawn as an image has no title and publishes its label as
        # a
        # description, which is then the only name it has.
        titled = self.element(role="AXButton", attrs={"kAXTitleAttribute": "Save"})
        described = self.element(
            role="AXButton", attrs={"kAXDescriptionAttribute": "Save"}
        )
        self.assertEqual(titled.name, "Save")
        self.assertEqual(described.name, "Save")
        self.assertEqual(self.element(role="AXButton").name, "")

    def test_an_attribute_with_no_value_reads_as_empty(self):
        # -25212, measured on Finder: the attribute is there and has no value, which is
        # the same answer as not publishing it at all for a caller asking for a title.
        node = self.element(role="AXWindow", attrs={"kAXTitleAttribute": None})
        self.assertEqual(node.name, "")

    def test_an_element_that_has_gone_is_not_alive_and_has_no_role(self):
        # Measured: a dead element answers -25202 for everything, and that is the case
        # `alive` exists to turn into a clean answer instead of an exception.
        dead = FakeElement(role="AXButton")
        dead.error = -25202
        element = macos.Element(dead, self.backend)
        self.assertFalse(element.alive)
        self.assertEqual(element.role, "unknown")

    def test_a_process_that_will_not_answer_reports_not_alive(self):
        # -25204, measured on the Window Server's own pid: nothing can be read through
        # it either, so a caller holding a stale widget is better told so.
        silent = FakeElement(role="AXButton")
        silent.error = -25204
        self.assertFalse(macos.Element(silent, self.backend).alive)

    def test_a_live_element_is_alive(self):
        self.assertTrue(self.element(role="AXButton").alive)

    def test_text_is_the_string_that_attribute_holds(self):
        # Measured: a text area answers its whole document, and a static text answers
        # the
        # string it shows -- which is why a label has text here where at-spi's would be
        # None, and what `base.Element.text` asks for.
        holding = self.element(role="AXTextArea", attrs={"kAXValueAttribute": "hello"})
        self.assertEqual(holding.text, "hello")
        self.assertIsNone(self.element(role="AXTextArea").text)
        self.assertIsNone(
            self.element(role="AXSlider", attrs={"kAXValueAttribute": 3}).text
        )

    def test_a_number_is_the_value_and_a_string_is_not(self):
        # One attribute read two ways: AX puts a number in it for a slider and a string
        # for anything holding text, so what a caller gets is decided by what the
        # element
        # published. A bool is excluded even though Python counts it as an int -- a
        # check
        # box's 0/1 is `checked`'s answer.
        self.assertEqual(
            self.element(role="AXSlider", attrs={"kAXValueAttribute": 0.5}).value, 0.5
        )
        self.assertIsNone(
            self.element(role="AXSlider", attrs={"kAXValueAttribute": "0.5"}).value
        )
        self.assertIsNone(
            self.element(role="AXCheckBox", attrs={"kAXValueAttribute": True}).value
        )

    def test_a_checkbox_answers_checked_from_its_value(self):
        off = self.element(role="AXCheckBox", attrs={"kAXValueAttribute": 0})
        on = self.element(role="AXCheckBox", attrs={"kAXValueAttribute": 1})
        self.assertIs(off.checked, False)
        self.assertIs(on.checked, True)
        self.assertTrue(on.checkable)

    def test_a_menu_items_tick_is_its_checked_state(self):
        # AX splits what at-spi calls one state: a menu item carries a mark character
        # rather than a value, and its role is `menu item` either way -- which is
        # exactly
        # why `checkable` asks the attribute as well as the role.
        ticked = self.element(
            role="AXMenuItem", attrs={"kAXMenuItemMarkCharAttribute": "\u2713"}
        )
        clear = self.element(
            role="AXMenuItem", attrs={"kAXMenuItemMarkCharAttribute": ""}
        )
        self.assertIs(ticked.checked, True)
        self.assertIs(clear.checked, False)
        self.assertTrue(ticked.checkable)

    def test_an_element_with_nothing_to_check_answers_none(self):
        self.assertIsNone(self.element(role="AXButton").checked)
        self.assertFalse(self.element(role="AXButton").checkable)

    def test_selection_answers_from_the_attribute_being_published(self):
        # "Is it published" is the signal `select` acts on, so `selectable` and the
        # write
        # underneath it cannot disagree -- a difference from `checkable`, whose
        # role-based
        # half has no such guarantee.
        row = self.element(role="AXRow", attrs={"kAXSelectedAttribute": True})
        self.assertIs(row.selected, True)
        self.assertTrue(row.selectable)
        button = self.element(role="AXButton")
        self.assertIsNone(button.selected)
        self.assertFalse(button.selectable)

    def test_disclosure_state_answers_none_where_nothing_publishes_it(self):
        # Measured, and the reason the interface asks a caller to read `expandable`
        # first: an outline row answers the attribute where the application propagates
        # it
        # and does not publish it at all where it does not.
        open_item = self.element(
            role="AXDisclosureTriangle", attrs={"kAXExpandedAttribute": True}
        )
        self.assertIs(open_item.expanded, True)
        self.assertTrue(open_item.expandable)
        self.assertIsNone(self.element(role="AXRow").expanded)
        self.assertFalse(self.element(role="AXRow").expandable)

    def test_enabled_defaults_to_true_where_nothing_answers(self):
        # The leniency uia.py shows for the same reason: a caller about to act on the
        # element is better served by trying than by a false "it is disabled".
        self.assertTrue(self.element(role="AXButton").enabled)
        disabled = self.element(role="AXButton", attrs={"kAXEnabledAttribute": False})
        self.assertFalse(disabled.enabled)

    def test_the_description_is_the_help_text_first(self):
        # `kAXHelpAttribute` is the tooltip; a Mac also publishes one description string
        # for "what this control is for", which `name` falls back to. The overlap is the
        # platform's rather than this module's.
        both = self.element(
            role="AXButton",
            attrs={"kAXHelpAttribute": "tooltip", "kAXDescriptionAttribute": "label"},
        )
        self.assertEqual(both.description, "tooltip")
        described = self.element(
            role="AXButton", attrs={"kAXDescriptionAttribute": "label"}
        )
        self.assertEqual(described.description, "label")

    def test_the_pid_comes_from_the_element(self):
        self.assertEqual(self.element(role="AXButton", pid=690).pid, 690)
        self.assertIsNone(self.element(role="AXButton").pid)

    def test_identifying_attributes_are_read_once_and_state_every_time(self):
        # The split the measured cost makes necessary: 1.83 ms per read over SSH, and a
        # search asks for an element's role twice. State is the other half of it -- a
        # remembered `checked` would describe the world before the click that changed
        # it.
        node = FakeElement(role="AXCheckBox", attrs={"kAXValueAttribute": 0})
        element = macos.Element(node, self.backend)
        reads = []
        original = self.ax.AXUIElementCopyAttributeValue
        self.ax.AXUIElementCopyAttributeValue = counting(original, reads)
        element.role, element.role
        element.name, element.name
        element.checked, element.checked
        self.assertEqual(reads.count("kAXRoleAttribute"), 1)
        self.assertEqual(reads.count("kAXTitleAttribute"), 1)
        self.assertEqual(reads.count("kAXValueAttribute"), 2)

    def test_the_backend_is_reachable_for_what_the_interface_does_not_cover(self):
        # `node` stays public, as `atspi.Element`'s dogtail Node and `uia.Element`'s COM
        # pointer do: the interface is a subset, and the escape hatch is the reason.
        node = FakeElement(role="AXButton")
        self.assertIs(macos.Element(node, self.backend).node, node)

    def test_repr_reads_as_a_script_writes_it(self):
        self.assertEqual(
            repr(self.element(role="AXButton", attrs={"kAXTitleAttribute": "Save"})),
            "Element('push button', 'Save')",
        )


class TestActionsAndClicks(BackendTestCase):
    """Doing things to an element, and refusing to pretend."""

    def setUp(self):
        self.backend = self.make()

    def element(self, **kwargs):
        """An Element over a fake node built from `kwargs`."""
        return macos.Element(FakeElement(**kwargs), self.backend)

    def test_a_click_is_the_action_ax_publishes(self):
        button = self.element(role="AXButton", actions=("AXPress",))
        button.click()
        self.assertEqual(button.node.performed, ["AXPress"])

    def test_a_click_does_not_fall_through_a_refused_press(self):
        # The choice uia.py makes for the same reason: an element can publish a press
        # and
        # be disabled, and turning "this button is disabled" into a click reported as
        # successful is the one failure this package will not trade for a convenience.
        self.ax.action_error = -25204
        checkbox = self.element(
            role="AXCheckBox", attrs={"kAXValueAttribute": 0}, actions=("AXPress",)
        )
        with self.assertRaises(CapabilityUnsupported) as caught:
            checkbox.click()
        self.assertIn("-25204", str(caught.exception))
        self.assertEqual(checkbox.node.written, [])

    def test_a_checkable_with_no_press_is_toggled_through_its_value(self):
        # What at-spi's `click` does for the same roles, and the route that makes a Mac
        # check box clickable at all: measured, a check box's state is its AXValue.
        checkbox = self.element(role="AXCheckBox", attrs={"kAXValueAttribute": 0})
        checkbox.click()
        self.assertEqual(checkbox.node.written, [("kAXValueAttribute", True)])

    def test_a_selectable_with_no_press_is_selected(self):
        row = self.element(role="AXRow", attrs={"kAXSelectedAttribute": False})
        row.click()
        self.assertEqual(row.node.written, [("kAXSelectedAttribute", True)])

    def test_a_click_on_something_with_no_route_is_a_typed_refusal(self):
        # The same typed answer the other two backends give, with this platform's
        # reason in it -- and, for an element with no session behind it, the way
        # out named as well: the coordinate click is a call the caller can make.
        label = self.element(role="AXStaticText")
        with self.assertRaises(ElementNotActionable) as caught:
            label.click()
        self.assertIn("gui.click_element(element)", str(caught.exception))

    def test_a_click_with_no_route_goes_to_the_pointer_through_the_session(self):
        # Measured shape on a Mac too: AX publishes no action for a static text
        # or a table cell, and the element stays the locator while the gesture
        # falls back to the session's pointer.
        label = self.element(role="AXStaticText")
        session = mock.Mock()
        label._bind_session(session)
        label.click()
        session.click_element.assert_called_once_with(label)

    def test_a_click_with_no_route_and_no_rectangle_stays_not_actionable(self):
        # With a rectangle there is somewhere to aim; without one the typed
        # refusal is the honest answer rather than a click somewhere arbitrary.
        label = self.element(role="AXStaticText")
        session = mock.Mock()
        session.extents.return_value = None
        label._bind_session(session)
        with self.assertRaises(ElementNotActionable) as caught:
            label.click()
        self.assertIn("no rectangle", str(caught.exception))
        session.click_element.assert_not_called()

    def test_a_refused_press_is_not_fallen_through_to_the_pointer(self):
        # The same policy as uia.py: a press that was published and failed is
        # reported, never turned into a coordinate click that would look like a
        # success.
        self.ax.action_error = -25204
        button = self.element(role="AXButton", actions=("AXPress",))
        session = mock.Mock()
        button._bind_session(session)
        with self.assertRaises(PyGUITestError):
            button.click()
        session.click_element.assert_not_called()

    def test_do_action_accepts_the_spellings_the_other_desktops_use(self):
        button = self.element(role="AXButton", actions=("AXPress",))
        for spelling in ("click", "press", "activate", "AXPress"):
            button.node.performed.clear()
            button.do_action(spelling)
            self.assertEqual(button.node.performed, ["AXPress"])

    def test_do_action_on_a_name_it_never_performs_lists_what_is_offered(self):
        # What `press_key` does with an unknown key, and far more useful than doing
        # nothing: the answer is the typo *and* the list to fix it from.
        button = self.element(role="AXButton", actions=("AXPress",))
        with self.assertRaises(ValueError) as caught:
            button.do_action("frobnicate")
        self.assertIn("click", str(caught.exception))

    def test_do_action_on_a_real_action_this_element_lacks_is_a_typed_refusal(self):
        # A caller can tell "typo" from "this widget cannot", which is the whole reason
        # `ElementNotActionable` exists beside the ValueError above.
        label = self.element(role="AXStaticText")
        with self.assertRaises(ElementNotActionable) as caught:
            label.do_action("click")
        self.assertIn("no actions at all", str(caught.exception))

    def test_set_text_writes_the_value(self):
        field = self.element(role="AXTextField")
        field.set_text("hello")
        self.assertEqual(field.node.written, [("kAXValueAttribute", "hello")])

    def test_a_refused_write_says_what_was_attempted(self):
        self.ax.set_error = -25205
        field = self.element(role="AXStaticText")
        with self.assertRaises(CapabilityUnsupported) as caught:
            field.set_text("hello")
        self.assertIn("replace the text of", str(caught.exception))
        self.assertIn("-25205", str(caught.exception))

    def test_focus_writes_the_focused_attribute(self):
        field = self.element(role="AXTextField")
        field.focus()
        self.assertEqual(field.node.written, [("kAXFocusedAttribute", True)])

    def test_selection_refuses_rather_than_doing_nothing(self):
        # A caller who asked for a selection and got nothing has no way to tell that
        # apart from a selection that happened, so the refusal is the useful answer.
        with self.assertRaises(CapabilityUnsupported):
            self.element(role="AXButton").select()
        row = self.element(role="AXRow", attrs={"kAXSelectedAttribute": False})
        row.select()
        self.assertEqual(row.node.written, [("kAXSelectedAttribute", True)])

    def test_expanding_is_a_no_op_where_it_is_already_open(self):
        # AX's disclosure state is written rather than toggled, so this is about the two
        # backends agreeing on the idempotence rather than about a write that would
        # flip.
        # Note the fake does not reflect a write, which is deliberate: the check has to
        # be
        # on the state *read back*, not on the write having happened.
        already_open = self.element(
            role="AXDisclosureTriangle", attrs={"kAXExpandedAttribute": True}
        )
        already_open.expand()
        self.assertEqual(already_open.node.written, [])
        already_closed = self.element(
            role="AXDisclosureTriangle", attrs={"kAXExpandedAttribute": False}
        )
        already_closed.collapse()
        self.assertEqual(already_closed.node.written, [])

    def test_expand_and_collapse_write_the_state_they_asked_for(self):
        closed = self.element(
            role="AXDisclosureTriangle", attrs={"kAXExpandedAttribute": False}
        )
        closed.expand()
        self.assertEqual(closed.node.written, [("kAXExpandedAttribute", True)])
        opened = self.element(
            role="AXDisclosureTriangle", attrs={"kAXExpandedAttribute": True}
        )
        opened.collapse()
        self.assertEqual(opened.node.written, [("kAXExpandedAttribute", False)])

    def test_expanding_something_with_no_disclosure_state_refuses(self):
        with self.assertRaises(CapabilityUnsupported):
            self.element(role="AXRow").expand()
        with self.assertRaises(CapabilityUnsupported):
            self.element(role="AXRow").collapse()

    def test_double_click_needs_a_session_to_delegate_to(self):
        # An AX reference knows nothing of the session above it, and the pointer is the
        # session's, so an element taken straight from a backend says so rather than
        # doing
        # nothing at all.
        element = self.element(role="AXButton")
        with self.assertRaises(PyGUITestError) as caught:
            element.double_click()
        self.assertIn("gui.double_click_element", str(caught.exception))

    def test_double_click_delegates_to_the_session_holding_the_pointer(self):
        element = self.element(role="AXButton")
        session = mock.Mock()
        element._bind_session(session)
        element.double_click()
        session.double_click_element.assert_called_once_with(element)

    def test_choosing_presses_the_control_then_selects_the_option(self):
        # Two real steps, because AX has no value setter for a popup button: press the
        # control -- its menu usually does not exist in the tree until it opens -- then
        # select or click the item. The press is allowed to fail silently, because a
        # popup
        # that is already open treats a second press as the close it looks like.
        item = FakeElement(
            role="AXMenuItem",
            attrs={"kAXTitleAttribute": "Large", "kAXSelectedAttribute": False},
        )
        menu = FakeElement(role="AXMenu", children=[item])
        popup = FakeElement(role="AXPopUpButton", children=[menu], actions=("AXPress",))
        macos.Element(popup, self.backend).choose("Large")
        self.assertEqual(popup.performed, ["AXPress"])
        self.assertEqual(item.written, [("kAXSelectedAttribute", True)])

    def test_choosing_something_that_is_not_there_is_element_not_found(self):
        popup = FakeElement(role="AXPopUpButton", actions=("AXPress",))
        with self.assertRaises(ElementNotFound):
            macos.Element(popup, self.backend).choose("Nonexistent")

    def test_options_prefers_menu_items_over_list_items(self):
        # Matching `atspi.Element.options`, and an empty list is a truthful answer about
        # a
        # popup whose menu has not been opened.
        menu_item = FakeElement(role="AXMenuItem", attrs={"kAXTitleAttribute": "a"})
        list_item = FakeElement(role="AXRow", attrs={"kAXTitleAttribute": "b"})
        popup = FakeElement(role="AXPopUpButton", children=[list_item, menu_item])
        found = macos.Element(popup, self.backend).options()
        self.assertEqual([element.role for element in found], [Role.MENU_ITEM])
        empty = macos.Element(FakeElement(role="AXPopUpButton"), self.backend)
        self.assertEqual(empty.options(), [])


class TestSearch(BackendTestCase):
    """The breadth-first walk, which is the only way to find anything through AX."""

    def setUp(self):
        self.button = FakeElement(role="AXButton", attrs={"kAXTitleAttribute": "Save"})
        self.entry = FakeElement(
            role="AXTextField", attrs={"kAXTitleAttribute": "Name"}
        )
        self.label = FakeElement(
            role="AXStaticText",
            attrs={"kAXTitleAttribute": "Tip", "kAXDescriptionAttribute": "a tooltip"},
        )
        self.disabled = FakeElement(
            role="AXButton",
            attrs={"kAXTitleAttribute": "Gone", "kAXEnabledAttribute": False},
        )
        self.hidden = FakeElement(
            role="AXButton",
            attrs={"kAXTitleAttribute": "Hidden", "kAXHiddenAttribute": True},
        )
        self.window = FakeElement(
            role="AXWindow",
            attrs={"kAXTitleAttribute": "doc"},
            children=[self.button, self.entry, self.label, self.disabled, self.hidden],
        )
        self.app = FakeElement(
            role="AXApplication",
            attrs={"kAXTitleAttribute": "TextEdit"},
            children=[self.window],
            pid=2829,
        )
        # A window entry so the application is reachable at all: `_applications` asks
        # the
        # window server which processes have AX trees, and a process that owns no window
        # and is not a regular application would be answered wrongly by a fixture that
        # named it in the AX fake alone.
        listed = window(1, 2829, "TextEdit", (0, 0, 400, 300))
        self.backend = self.make(
            applications={2829: self.app}, onscreen=[listed], everything=[listed]
        )

    def elements(self):
        """Every element under the application, whatever its role."""
        return self.backend.find_elements(within=macos.Element(self.app, self.backend))

    def test_a_role_is_translated_before_it_is_compared(self):
        found = self.backend.find_elements(role=Role.PUSH_BUTTON)
        self.assertEqual(
            [element.name for element in found], ["Save", "Gone", "Hidden"]
        )

    def test_the_atspi_spelling_of_a_role_matches_too(self):
        self.assertEqual(len(self.backend.find_elements(role="button")), 3)

    def test_a_name_matches_exactly_or_as_a_regular_expression(self):
        exact = self.backend.find_elements(name="Save")
        pattern = self.backend.find_elements(name=re.compile("^Sa"))
        self.assertEqual([element.name for element in exact], ["Save"])
        self.assertEqual([element.name for element in pattern], ["Save"])
        self.assertEqual(self.backend.find_elements(name="save"), [])

    def test_a_state_filter_selects_on_what_the_element_answers(self):
        found = self.backend.find_elements(role=Role.PUSH_BUTTON, enabled=False)
        self.assertEqual([element.name for element in found], ["Gone"])

    def test_a_hidden_element_is_not_visible(self):
        # `kAXHiddenAttribute` is the one visibility signal AX publishes, and it is what
        # `Element.visible` reads before going looking for a window to join against.
        found = {element.name: element for element in self.elements()}
        self.assertFalse(found["Hidden"].visible)

    def test_a_description_filter_matches_the_help_text(self):
        found = self.backend.find_elements(description="a tooltip")
        self.assertEqual([element.name for element in found], ["Tip"])

    def test_a_predicate_is_the_escape_hatch(self):
        found = self.backend.find_elements(
            role=Role.PUSH_BUTTON,
            predicate=lambda element: element.name.startswith("S"),
        )
        self.assertEqual([element.name for element in found], ["Save"])

    def test_a_search_inside_an_element_starts_below_it(self):
        # `within` is the search root, so the element itself is not a candidate -- which
        # is
        # what makes `gui.find_element(...).find(...)` mean "inside this".
        within = macos.Element(self.window, self.backend)
        found = self.backend.find_elements(role=Role.PUSH_BUTTON, within=within)
        self.assertEqual(len(found), 3)
        self.assertNotIn(Role.WINDOW, [element.role for element in found])

    def test_find_element_answers_the_first_match_or_none(self):
        self.assertEqual(self.backend.find_element(role=Role.PUSH_BUTTON).name, "Save")
        self.assertIsNone(self.backend.find_element(name="Nothing"))

    def test_the_walk_finds_an_application_through_the_synthetic_root(self):
        found = self.backend.find_elements(role="application")
        self.assertEqual([element.name for element in found], ["TextEdit"])

    def test_an_element_found_by_a_search_carries_the_backend_that_found_it(self):
        self.assertIs(
            self.backend.find_element(role=Role.PUSH_BUTTON)._backend, self.backend
        )

    def test_a_search_stops_at_the_node_budget(self):
        # AX has no server-side search, so the walk needs a ceiling: measured, 1.83 ms
        # per
        # node over SSH, which makes 20 000 nodes a budget rather than a limit.
        deep = FakeElement(role="AXGroup")
        node = deep
        for _ in range(5):
            child = FakeElement(role="AXGroup")
            node.children = [child]
            node = child
        node.children = [
            FakeElement(role="AXButton", attrs={"kAXTitleAttribute": "Deep"})
        ]
        within = macos.Element(deep, self.backend)
        self.assertEqual(
            len(self.backend.find_elements(role=Role.PUSH_BUTTON, within=within)), 1
        )
        with mock.patch.object(macos, "_MAX_NODES", 3):
            self.assertEqual(
                self.backend.find_elements(role=Role.PUSH_BUTTON, within=within), []
            )

    def test_a_search_stops_at_the_depth_budget(self):
        # The other ceiling, and the one that keeps a cycle from spinning: a tree 26
        # levels
        # deep is walked to 24 and no further.
        node = FakeElement(role="AXGroup")
        current = node
        for _ in range(macos._MAX_DEPTH + 2):
            child = FakeElement(role="AXGroup")
            current.children = [child]
            current = child
        current.children = [
            FakeElement(role="AXButton", attrs={"kAXTitleAttribute": "TooDeep"})
        ]
        within = macos.Element(node, self.backend)
        self.assertEqual(
            self.backend.find_elements(role=Role.PUSH_BUTTON, within=within), []
        )

    def test_searching_needs_the_grant(self):
        backend = self.make(trusted=False)
        with self.assertRaises(CapabilityUnsupported):
            backend.find_elements(role=Role.PUSH_BUTTON)


class TestTreesThatLie(BackendTestCase):
    """Trees whose shape and properties are not what they claim to be.

    The adversarial half of the search and read paths -- the same set of shapes
    `tests/test_uia_backend.py::TestTreesThatLie` walks on Windows: a container
    whose children are all at the origin, one name in two windows of one
    process, a title that is not text, a child list that will not answer, an
    element whose application has gone, a tree that reports an ancestor as its
    own child, and the two budgets a merely enormous tree has to stop at.
    Writing them found the cycle case: an element that reported an ancestor as
    its child was reported once per depth level the loop could carry it to, so
    one element came back as a dozen matches. See the CHANGELOG.
    """

    def test_a_cycle_in_the_tree_is_not_walked_into_itself(self):
        # A broken application, or a stale reference: the pane lists the
        # application it belongs to as one of its own children.
        application = FakeElement(
            role="AXApplication", attrs={"kAXTitleAttribute": "Editor"}, pid=2829
        )
        pane = FakeElement(role="AXGroup", attrs={"kAXTitleAttribute": "pane"})
        application.children = [pane]
        pane.children = [application]
        backend = self.make(applications={2829: application})
        found = backend.find_elements(
            role="panel", within=macos.Element(application, backend)
        )
        self.assertEqual([element.name for element in found], ["pane"])

    def test_one_name_in_two_windows_of_one_process_is_found_once_per_window(self):
        # The shape an editor produces with two documents open: one process, two
        # windows, a Save in each. An unscoped search sees both in tree order,
        # and a search scoped to one window sees that one's.
        save_first = FakeElement(role="AXButton", attrs={"kAXTitleAttribute": "Save"})
        save_second = FakeElement(role="AXButton", attrs={"kAXTitleAttribute": "Save"})
        first = FakeElement(role="AXWindow", children=[save_first])
        second = FakeElement(role="AXWindow", children=[save_second])
        application = FakeElement(
            role="AXApplication", children=[first, second], pid=2829
        )
        backend = self.make(applications={2829: application})
        both = backend.find_elements(
            role="button", within=macos.Element(application, backend)
        )
        self.assertEqual([element.name for element in both], ["Save", "Save"])
        scoped = backend.find_elements(
            role="button", name="Save", within=macos.Element(second, backend)
        )
        self.assertEqual([element.node for element in scoped], [save_second])

    def test_every_child_at_the_origin_is_not_a_place_to_click(self):
        # The GTK4 shape, as far as this backend can meet it. The hit test is
        # AX's own rather than a descent over rectangles, so a lying child is
        # never walked into -- and its own rectangle is still refused, which is
        # what keeps a coordinate click from landing on the origin.
        lying = FakeElement(
            role="AXGroup",
            attrs={"kAXPositionAttribute": point(0, 0), "kAXSizeAttribute": size(0, 0)},
        )
        backend = self.make()
        self.ax.at_position = lying
        self.assertEqual(backend.element_at(640, 400).node, lying)
        self.assertIsNone(backend.extents(macos.Element(lying, backend)))

    def test_a_title_that_is_not_text_is_not_a_name(self):
        # AX hands back whatever type an application published, so a title can
        # be a number: it is not a name, and a regex filter over it is "no
        # match" rather than a TypeError raised out of a search.
        liar = FakeElement(role="AXButton", attrs={"kAXTitleAttribute": 17})
        holder = FakeElement(role="AXGroup", children=[liar])
        backend = self.make()
        self.assertEqual(macos.Element(liar, backend).name, "")
        self.assertEqual(
            backend.find_elements(
                name=re.compile("1"), within=macos.Element(holder, backend)
            ),
            [],
        )

    def test_a_child_list_that_will_not_answer_is_no_children(self):
        # A process that will not be interviewed: every read fails, and the
        # element still answers with what can honestly be said about it.
        gone = FakeElement(role="AXApplication", pid=2829)
        gone.error = -25204
        backend = self.make(applications={2829: gone})
        element = macos.Element(gone, backend)
        self.assertEqual(element.children, [])
        self.assertEqual(element.name, "")
        self.assertFalse(element.alive)

    def test_an_element_whose_process_has_gone_is_not_clicked_at_a_stale_place(self):
        # kAXErrorInvalidUIElement: the widget is gone, so there is no action to
        # perform and no rectangle to aim at, and the coordinate fallback
        # refuses rather than clicking where it used to be.
        dead = FakeElement(role="AXButton", attrs={"kAXTitleAttribute": "Save"})
        dead.error = -25202
        backend = self.make()
        element = macos.Element(dead, backend)
        self.assertIsNone(backend.extents(element))
        with self.assertRaises(ElementNotActionable):
            element.click()

    def test_the_node_budget_stops_a_tree_that_is_merely_enormous(self):
        # `_MAX_NODES` is a budget rather than a suggestion: a tree bigger than
        # it is truncated rather than walked forever, and the walk still answers
        # -- patched down here to something a test can build in one go.
        children = [
            FakeElement(role="AXButton", attrs={"kAXTitleAttribute": f"b{index}"})
            for index in range(10)
        ]
        holder = FakeElement(role="AXGroup", children=children)
        backend = self.make()
        with mock.patch.object(macos, "_MAX_NODES", 4):
            found = backend.find_elements(
                role="button", within=macos.Element(holder, backend)
            )
        self.assertEqual([element.name for element in found], ["b0", "b1", "b2"])

    def test_the_depth_limit_stops_a_chain_that_keeps_going(self):
        # `_MAX_DEPTH` is the other budget: a chain deeper than any real window
        # is not followed to the end, and the walk does not raise on the way.
        deep = FakeElement(role="AXButton", attrs={"kAXTitleAttribute": "deep"})
        chain = [deep]
        for _ in range(macos._MAX_DEPTH + 4):
            chain.insert(0, FakeElement(role="AXGroup", children=chain[:1]))
        backend = self.make()
        found = backend.find_elements(
            role="button", within=macos.Element(chain[0], backend)
        )
        self.assertEqual(found, [])


class TestGeometryAndHitTesting(BackendTestCase):
    """`extents` and `element_at`, in one global display space."""

    def test_extents_are_the_elements_rectangle(self):
        backend = self.make()
        node = FakeElement(
            role="AXButton",
            attrs={
                "kAXPositionAttribute": point(10, 20),
                "kAXSizeAttribute": size(30, 40),
            },
        )
        self.assertEqual(
            backend.extents(macos.Element(node, backend)), (10, 20, 30, 40)
        )

    def test_an_element_with_no_rectangle_answers_none(self):
        # An ordinary answer about an element that occupies no screen space, which is
        # what
        # `base.GUIBackend.extents` documents, rather than a missing capability.
        backend = self.make()
        node = FakeElement(role="AXButton")
        self.assertIsNone(backend.extents(macos.Element(node, backend)))

    def test_an_empty_rectangle_is_none_rather_than_a_raise(self):
        backend = self.make()
        node = FakeElement(
            role="AXButton",
            attrs={"kAXPositionAttribute": point(0, 0), "kAXSizeAttribute": size(0, 0)},
        )
        self.assertIsNone(backend.extents(macos.Element(node, backend)))

    def test_a_document_rectangle_is_reported_as_it_is(self):
        # Measured live: the text area at (640, 400) reported its whole document -- 860
        # x
        # 7217 at y = -6638 -- so a rectangle from here can be far larger than the
        # screen
        # and its centre can be off it.
        backend = self.make()
        node = FakeElement(
            role="AXTextArea",
            attrs={
                "kAXPositionAttribute": point(154, -6638),
                "kAXSizeAttribute": size(860, 7217),
            },
        )
        self.assertEqual(
            backend.extents(macos.Element(node, backend)), (154, -6638, 860, 7217)
        )

    def test_a_hit_test_answers_the_deepest_element(self):
        backend = self.make()
        backend._ax.at_position = FakeElement(
            role="AXTextArea", pid=690, attrs={"kAXTitleAttribute": "shell"}
        )
        element = backend.element_at(640, 400)
        self.assertEqual(
            (element.role, element.name, element.pid), (Role.TEXT, "shell", 690)
        )

    def test_a_hit_test_where_ax_declines_answers_none(self):
        backend = self.make()
        self.assertIsNone(backend.element_at(0, 0))

    def test_geometry_and_hit_testing_need_the_grant(self):
        backend = self.make(trusted=False)
        with self.assertRaises(CapabilityUnsupported):
            backend.extents(macos.Element(FakeElement(role="AXButton"), backend))
        with self.assertRaises(CapabilityUnsupported):
            backend.element_at(1, 1)


class TestTheDesktopRoot(BackendTestCase):
    """The synthetic root, which exists because a Mac has no desktop-wide AX tree."""

    def setUp(self):
        self.backend = self.make(
            onscreen=self.onscreen_windows(),
            everything=self.everything_windows(),
            applications=self.applications(),
        )

    def test_the_root_reports_the_atspi_role_for_a_desktop(self):
        # Measured: the system-wide element publishes exactly four attributes and
        # answers
        # `kAXChildrenAttribute` with -25205, so it cannot be searched from. `desktop
        # frame` is at-spi's name for the element that can be.
        root = self.backend.root_element()
        self.assertEqual(root.role, "desktop frame")
        self.assertEqual(str(root), "Element('desktop frame', '')")

    def test_the_roots_children_are_the_applications(self):
        # In the window server's order, so the front-most application comes first -- and
        # including the Dock, which owns an on-screen window and a real AX tree.
        # Measured:
        # the live run's seven applications included Dock and Control Center, and
        # reporting
        # them is honest rather than sloppy.
        names = [child.name for child in self.backend.root_element().children]
        self.assertEqual(names, ["Terminal", "TextEdit", "Dock"])

    def test_an_application_answers_with_its_windows(self):
        terminal = self.backend.root_element().children[0]
        self.assertEqual(terminal.role, "application")
        self.assertEqual(
            [child.name for child in terminal.children], ["pyguitest - caffeinate"]
        )

    def test_the_root_refuses_every_action(self):
        # There is nothing behind it to act on, and a click that silently did nothing
        # would
        # be worse than a refusal -- the argument `atspi.Element.click` makes about an
        # element with no action.
        root = self.backend.root_element()
        for call in (root.click, root.focus, root.select, root.expand, root.collapse):
            with self.assertRaises(CapabilityUnsupported):
                call()

    def test_the_root_answers_the_read_only_members_sensibly(self):
        root = self.backend.root_element()
        self.assertEqual((root.name, root.pid, root.parent), ("", None, None))
        self.assertEqual((root.actions, root.options()), ([], []))
        self.assertTrue(root.visible)
        self.assertTrue(root.enabled)
        self.assertIsNone(root.text)
        self.assertIsNone(root.value)
        self.assertIsNone(root.checked)
        self.assertIsNone(root.expanded)

    def test_anything_else_is_below_the_root(self):
        # Worth being plain about rather than walking: the applications are the root's
        # own
        # children by construction, and no AX element links back to a desktop that does
        # not
        # exist as one.
        root = self.backend.root_element()
        self.assertTrue(root.is_ancestor_of(root.children[0]))
        self.assertFalse(root.is_ancestor_of(root))

    def test_the_root_can_be_searched_from_directly(self):
        found = self.backend.root_element().find(role=Role.WINDOW)
        self.assertEqual(found[0].name, "pyguitest - caffeinate")
        self.assertEqual(len(found), 5)

    def test_a_child_of_the_root_is_the_first_match_or_none(self):
        root = self.backend.root_element()
        self.assertEqual(root.child(role="application").name, "Terminal")
        self.assertIsNone(root.child(role="application", name="Nonexistent"))


if __name__ == "__main__":
    unittest.main()
