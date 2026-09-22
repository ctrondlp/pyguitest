"""The user-facing convenience layer on Session."""

import ctypes
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

import pyguitest
from pyguitest import (
    AccessibilityViolation,
    Capability,
    ClipboardMismatch,
    ElementNotActionable,
    ElementNotFound,
    FocusMismatch,
    ImageMatch,
    ImageNotFound,
    PortalTimeout,
    PyGUITestError,
    Role,
    WindowNotFound,
)
from pyguitest.backends.base import GUIBackend, Window
from pyguitest.capabilities import CapabilitySet


class FakeElement:
    def __init__(
        self,
        role,
        name,
        *,
        enabled=True,
        visible=True,
        description="",
        checkable=False,
        selectable=False,
        focused=False,
        actions=(),
        parent=None,
        children=(),
    ):
        self.role = role
        self.name = name
        self.clicked = False
        self.text = None
        self.chosen = None
        self.checked = False
        self.enabled = enabled
        self.visible = visible
        self.description = description
        self.checkable = checkable
        self.selected = False
        self.selectable = selectable
        self.focused = focused
        self.actions = list(actions)
        self.parent = parent
        self.children = list(children)

    def click(self):
        self.clicked = True

    def focus(self):
        self.focused = True

    def set_text(self, text):
        self.text = text

    def choose(self, option):
        self.chosen = option

    def _bind_session(self, session):
        """Mirror atspi.Element: record the session that handed this out."""
        self._session = session

    def double_click(self):
        """Mirror atspi.Element: the session owns the gesture, not the element."""
        self._session.double_click_element(self)


class FakeBackend(GUIBackend):
    name = "fake"

    def __init__(self):
        self.elements = [
            FakeElement(Role.PUSH_BUTTON, "OK"),
            FakeElement(Role.PUSH_BUTTON, "Cancel", enabled=False),
            FakeElement(Role.ENTRY, "Name"),
            FakeElement(Role.COMBO_BOX, "Country"),
            FakeElement(Role.CHECK_BOX, "Remember me"),
            FakeElement(
                Role.PUSH_BUTTON, "Save", visible=False, description="Save the file"
            ),
            FakeElement(Role.FRAME, "Preferences"),
        ]
        self._windows = [
            Window("a", self, title="Document - Editor", app_id="org.editor.Editor"),
            Window("b", self, title="Firefox", app_id="firefox"),
        ]

    @property
    def capabilities(self):
        return CapabilitySet({Capability.ELEMENT_TREE, Capability.WINDOW_LIST})

    def find_elements(
        self,
        role=None,
        name=None,
        within=None,
        enabled=None,
        visible=None,
        description=None,
        predicate=None,
    ):
        def matches_text(value, wanted):
            if hasattr(wanted, "search"):
                return wanted.search(value or "") is not None
            return value == wanted

        return [
            e
            for e in self.elements
            if (role is None or e.role == role)
            and (name is None or matches_text(e.name, name))
            and (enabled is None or e.enabled == enabled)
            and (visible is None or e.visible == visible)
            and (description is None or matches_text(e.description, description))
            and (predicate is None or predicate(e))
        ]

    def windows(self):
        return self._windows


def session():
    return pyguitest.Session(FakeBackend(), pyguitest.detect())


class FakeClipboardBackend(GUIBackend):
    """Two independent selections in memory: clipboard and PRIMARY."""

    name = "fake-clipboard"

    def __init__(self, clipboard="", primary=""):
        self._store = {False: clipboard, True: primary}

    @property
    def capabilities(self):
        return CapabilitySet({Capability.CLIPBOARD})

    def get_clipboard(self, primary=False):
        return self._store[primary]

    def set_clipboard(self, text, primary=False):
        self._store[primary] = text


class TestClipboardAssertion(unittest.TestCase):
    def test_a_matching_clipboard_passes(self):
        gui = pyguitest.Session(FakeClipboardBackend("hello"), pyguitest.detect())
        gui.assert_clipboard("hello")

    def test_a_mismatch_raises_naming_both_values(self):
        gui = pyguitest.Session(FakeClipboardBackend("hello"), pyguitest.detect())
        with self.assertRaises(ClipboardMismatch) as caught:
            gui.assert_clipboard("goodbye")
        message = str(caught.exception)
        self.assertIn("goodbye", message)
        self.assertIn("hello", message)

    def test_it_checks_primary_when_asked(self):
        # The clipboard and PRIMARY are independent selections -- checking
        # one must not accidentally read the other.
        gui = pyguitest.Session(
            FakeClipboardBackend(clipboard="clip", primary="middle-click"),
            pyguitest.detect(),
        )
        gui.assert_clipboard("middle-click", primary=True)
        with self.assertRaises(ClipboardMismatch):
            gui.assert_clipboard("clip", primary=True)

    def test_a_partial_match_is_not_good_enough(self):
        # set_clipboard() replaces rather than appends, so a caller checking
        # the round trip wants an exact match, not "contains".
        gui = pyguitest.Session(FakeClipboardBackend("hello world"), pyguitest.detect())
        with self.assertRaises(ClipboardMismatch):
            gui.assert_clipboard("hello")

    def test_the_mismatch_message_names_which_selection(self):
        gui = pyguitest.Session(FakeClipboardBackend(primary="x"), pyguitest.detect())
        with self.assertRaises(ClipboardMismatch) as caught:
            gui.assert_clipboard("y", primary=True)
        self.assertIn("PRIMARY", str(caught.exception))


class TestAccessibilityAssertions(unittest.TestCase):
    """Missing and ambiguous accessible names.

    The same defect shows up twice: a control with no name is read out by a
    screen reader as just "button", and cannot be found by
    `gui.button("Save")` either -- so it is worth catching in a GUI test
    rather than only in an audit.
    """

    def _gui(self, *elements):
        gui = session()
        gui.backend.elements = list(elements)
        return gui

    def test_an_unnamed_button_is_reported(self):
        gui = self._gui(FakeElement(Role.PUSH_BUTTON, ""))
        with self.assertRaises(AccessibilityViolation) as caught:
            gui.assert_no_missing_accessible_names()
        self.assertIn("push button", str(caught.exception))

    def test_named_controls_pass(self):
        gui = self._gui(
            FakeElement(Role.PUSH_BUTTON, "Save"),
            FakeElement(Role.ENTRY, "Name"),
        )
        gui.assert_no_missing_accessible_names()

    def test_content_and_structural_roles_are_not_required_to_have_names(self):
        # The line NAMED_ROLES draws. A label carries its text as content
        # and a panel is furniture; demanding names of these would make the
        # assertion unusable on any real application.
        gui = self._gui(
            FakeElement(Role.LABEL, ""),
            FakeElement(Role.PANEL, ""),
            FakeElement(Role.SEPARATOR, ""),
            FakeElement(Role.ICON, ""),
            FakeElement(Role.TABLE_CELL, ""),
        )
        gui.assert_accessible()

    def test_an_invisible_unnamed_control_is_not_reported(self):
        # Nobody can reach it, so it is not a labelling problem -- and a
        # hidden dialog's worth of them would bury the real findings.
        gui = self._gui(FakeElement(Role.PUSH_BUTTON, "", visible=False))
        gui.assert_no_missing_accessible_names()

    def test_two_buttons_with_one_name_are_ambiguous(self):
        gui = self._gui(
            FakeElement(Role.PUSH_BUTTON, "Delete"),
            FakeElement(Role.PUSH_BUTTON, "Delete"),
        )
        with self.assertRaises(AccessibilityViolation) as caught:
            gui.assert_no_duplicate_accessible_names()
        self.assertIn("Delete", str(caught.exception))

    def test_one_name_across_two_roles_is_ordinary(self):
        # A "Save" button beside a "Save" menu item is normal, and flagging
        # it would be noise -- so duplicates are counted per role.
        gui = self._gui(
            FakeElement(Role.PUSH_BUTTON, "Save"),
            FakeElement(Role.MENU_ITEM, "Save"),
        )
        gui.assert_no_duplicate_accessible_names()

    def test_unnamed_controls_are_not_counted_as_duplicates(self):
        # Two unnamed buttons are one problem, not two: the missing-name
        # check owns them, and reporting "'' used twice" as well would be
        # the same finding wearing a worse message.
        gui = self._gui(
            FakeElement(Role.PUSH_BUTTON, ""),
            FakeElement(Role.PUSH_BUTTON, ""),
        )
        gui.assert_no_duplicate_accessible_names()

    def test_assert_accessible_reports_missing_names_first(self):
        gui = self._gui(
            FakeElement(Role.PUSH_BUTTON, ""),
            FakeElement(Role.ENTRY, "Name"),
            FakeElement(Role.ENTRY, "Name"),
        )
        with self.assertRaises(AccessibilityViolation) as caught:
            gui.assert_accessible()
        self.assertIn("no accessible name", str(caught.exception))

    def test_the_role_set_can_be_narrowed_or_widened(self):
        gui = self._gui(FakeElement(Role.LABEL, ""))
        gui.assert_no_missing_accessible_names()  # not in NAMED_ROLES
        with self.assertRaises(AccessibilityViolation):
            gui.assert_no_missing_accessible_names(roles=[Role.LABEL])


class TestWindowIdentity(unittest.TestCase):
    """A Window is a snapshot; these are the ways to ask about *now*.

    The three failures behind this, all from one live run: a handle that
    went stale mid-script ("no window with id 106" from geometry()), a
    title that changed underneath a captured Window because the editor
    renamed itself once it had content, and no way to ask whether two
    Windows were the same one -- which is what pushed that script into
    comparing titles, the thing that produced the first two problems.
    """

    def test_two_objects_for_one_window_are_equal(self):
        # Built by hand rather than by looking twice: the fake backend hands
        # back the same object both times, where a real one constructs a new
        # Window per call -- which is the case that has to compare equal.
        gui = session()
        first = gui.find_window("Firefox")
        again = Window(first.handle, first.backend, title=first.title)
        self.assertIsNot(first, again)
        self.assertEqual(first, again)

    def test_a_different_window_is_not_equal(self):
        gui = session()
        self.assertNotEqual(gui.find_window("Firefox"), gui.find_window("Editor"))

    def test_equal_handles_from_different_backends_are_not_the_same_window(self):
        # Two members of one composite can both hand out "a" as a handle.
        # Comparing handles alone would call those the same window.
        one, two = FakeBackend(), FakeBackend()
        self.assertNotEqual(
            Window("a", one, title="Document - Editor"),
            Window("a", two, title="Document - Editor"),
        )

    def test_a_window_survives_a_set(self):
        gui = session()
        windows = set(gui.windows())
        self.assertIn(gui.find_window("Firefox"), windows)
        self.assertEqual(len(windows | set(gui.windows())), len(windows))

    def test_comparing_against_a_non_window_is_not_an_error(self):
        self.assertNotEqual(session().find_window("Firefox"), "Firefox")

    def test_refresh_window_reports_the_title_as_it_is_now(self):
        # The editor-renamed-itself case. The handle is unchanged, so it is
        # still the same window -- but the snapshot's title is stale.
        gui = session()
        stale = gui.find_window("Firefox")
        gui.backend._windows[1] = Window("b", gui.backend, title="Firefox - Private")

        fresh = gui.refresh_window(stale)
        self.assertEqual(fresh, stale)
        self.assertEqual(fresh.title, "Firefox - Private")
        self.assertEqual(stale.title, "Firefox")

    def test_refresh_window_reports_a_closed_window_as_gone(self):
        gui = session()
        window = gui.find_window("Firefox")
        gui.backend._windows = [w for w in gui.backend._windows if w != window]
        self.assertIsNone(gui.refresh_window(window))

    def test_is_window_open_follows_the_list(self):
        gui = session()
        window = gui.find_window("Firefox")
        self.assertTrue(gui.is_window_open(window))
        gui.backend._windows = [w for w in gui.backend._windows if w != window]
        self.assertFalse(gui.is_window_open(window))


class TestWindowFinders(unittest.TestCase):
    def test_find_windows_matches_a_regex(self):
        self.assertEqual(len(session().find_windows("Editor")), 1)
        self.assertEqual(len(session().find_windows(re.compile("."))), 2)

    def test_find_windows_treats_a_plain_string_literally(self):
        # A plain string is escaped before matching (as elements()/element()
        # already do for name/description), so a title containing regex
        # metacharacters can be round-tripped back in and still match
        # itself. Found live: GNOME Text Editor's own default title, "New
        # Document (Draft) - Text Editor", does not match itself unescaped,
        # since "(Draft)" reads as a capture group. "." has no special
        # meaning here unless the caller opts in with re.compile.
        self.assertEqual(session().find_windows("."), [])
        gui = session()
        gui.backend._windows.append(Window("c", gui.backend, title="Q&A (Draft)"))
        self.assertEqual(
            [w.title for w in gui.find_windows("Q&A (Draft)")], ["Q&A (Draft)"]
        )

    def test_find_window_returns_the_first_match(self):
        self.assertEqual(session().find_window("Fire").title, "Firefox")

    def test_missing_window_raises_rather_than_returning_none(self):
        with self.assertRaises(WindowNotFound):
            session().find_window("NoSuchApp")

    def test_find_window_by_app_id_alone(self):
        self.assertEqual(session().find_window(app_id="firefox").title, "Firefox")

    def test_app_id_is_an_exact_match_not_a_substring(self):
        # "fire" is a substring, not the whole app_id -- unlike a plain
        # string title, which matches as a literal substring (or pass a
        # compiled regex for full pattern power).
        self.assertEqual(session().find_windows(app_id="fire"), [])

    def test_title_and_app_id_together_both_must_match(self):
        gui = session()
        self.assertEqual(
            gui.find_windows("Firefox", app_id="firefox")[0].title, "Firefox"
        )
        self.assertEqual(gui.find_windows("Firefox", app_id="org.editor.Editor"), [])

    def test_neither_title_nor_app_id_is_an_error(self):
        with self.assertRaises(ValueError):
            session().find_windows()
        with self.assertRaises(ValueError):
            session().find_window()

    def test_empty_app_id_matches_nothing_rather_than_every_unset_window(self):
        # A window whose backend never fills app_id defaults to "" -- that
        # is "unset", not a real identifier, so it must not be findable by
        # asking for app_id="".
        gui = session()
        gui.backend._windows.append(Window("c", gui.backend, title="Untitled"))
        self.assertEqual(gui.find_windows(app_id=""), [])

    def test_app_id_may_be_several_ids_and_matches_any_of_them(self):
        gui = session()
        both = gui.find_windows(app_id=("firefox", "org.editor.Editor"))
        self.assertEqual([w.title for w in both], ["Document - Editor", "Firefox"])
        self.assertEqual(gui.find_window(app_id=("nope", "firefox")).title, "Firefox")

    def test_the_other_protocols_spelling_is_what_finds_the_window(self):
        # One window, two possible ids, neither derivable from the other:
        # the class half of WM_CLASS here, the Wayland app id a replay on
        # that protocol would report. Asking for the wrong one alone finds
        # nothing; naming both finds it.
        gui = session()
        gui.backend._windows.append(
            Window("c", gui.backend, title="Draft", app_id="gnome-text-editor")
        )
        self.assertEqual(gui.find_windows(app_id="org.gnome.TextEditor"), [])
        found = gui.find_windows(app_id=("org.gnome.TextEditor", "gnome-text-editor"))
        self.assertEqual([w.title for w in found], ["Draft"])

    def test_an_empty_sequence_of_app_ids_matches_nothing(self):
        # Same reading as app_id="": nothing named, nothing found -- rather
        # than the filter being dropped and every window coming back.
        self.assertEqual(session().find_windows(app_id=()), [])

    def test_expect_window_takes_several_app_ids_too(self):
        found = session().expect_window(app_id=("nope", "firefox"), timeout=0.1)
        self.assertEqual(found.title, "Firefox")


class TestWaitForWindow(unittest.TestCase):
    """The generic fallback: polling find_windows when there is no event feed.

    FakeBackend declares WINDOW_LIST but not WINDOW_EVENTS, so every case
    here exercises the poll loop, not backend delegation -- that path is
    covered separately below.
    """

    def test_returns_immediately_when_already_open(self):
        gui = session()
        window = gui.wait_for_window("Editor", timeout=1, interval=0.01)
        self.assertEqual(window.title, "Document - Editor")

    def test_returns_none_on_timeout_rather_than_raising(self):
        gui = session()
        self.assertIsNone(
            gui.wait_for_window("NoSuchWindow", timeout=0.05, interval=0.01)
        )

    def test_keeps_polling_until_the_window_actually_appears(self):
        gui = session()
        all_windows = gui.backend._windows
        gui.backend._windows = []
        calls = []

        def flaky_windows():
            calls.append(None)
            if len(calls) >= 3:
                gui.backend._windows = all_windows
            return gui.backend._windows

        gui.backend.windows = flaky_windows
        window = gui.wait_for_window("Editor", timeout=2, interval=0.01)
        self.assertEqual(window.title, "Document - Editor")
        self.assertGreaterEqual(len(calls), 3)

    def test_delegates_to_the_backend_when_window_events_is_supported(self):
        class EventBackend(FakeBackend):
            def __init__(self):
                super().__init__()
                self.asked = None

            @property
            def capabilities(self):
                return CapabilitySet(
                    set(FakeBackend.capabilities.fget(self))
                    | {Capability.WINDOW_EVENTS}
                )

            def wait_for_window(self, title, timeout):
                self.asked = (title, timeout)
                return self.windows()[0]

        gui = pyguitest.Session(EventBackend(), pyguitest.detect())
        window = gui.wait_for_window("Editor", timeout=5)
        self.assertEqual(gui.backend.asked, ("Editor", 5))
        self.assertEqual(window.title, "Document - Editor")

    def test_app_id_always_polls_even_when_window_events_is_supported(self):
        # The event-driven backends only ever learned to match a title, so
        # naming app_id -- with or without a title -- must not reach them.
        class EventBackend(FakeBackend):
            @property
            def capabilities(self):
                return CapabilitySet(
                    set(FakeBackend.capabilities.fget(self))
                    | {Capability.WINDOW_EVENTS}
                )

            def wait_for_window(self, title, timeout):
                raise AssertionError("app_id must not delegate to the backend")

        gui = pyguitest.Session(EventBackend(), pyguitest.detect())
        window = gui.wait_for_window(app_id="firefox", timeout=1, interval=0.01)
        self.assertEqual(window.title, "Firefox")

    def test_by_app_id_alone(self):
        gui = session()
        window = gui.wait_for_window(app_id="firefox", timeout=1, interval=0.01)
        self.assertEqual(window.title, "Firefox")

    def test_title_and_app_id_together_both_must_match(self):
        gui = session()
        self.assertIsNone(
            gui.wait_for_window(
                "Firefox", app_id="org.editor.Editor", timeout=0.05, interval=0.01
            )
        )

    def test_neither_title_nor_app_id_is_an_error(self):
        with self.assertRaises(ValueError):
            session().wait_for_window()


class FocusBackend(FakeBackend):
    """A FakeBackend that can also answer `active_window`.

    A controllable, poppable sequence of answers -- WINDOW_STATE is
    otherwise undeclared on FakeBackend, so `wait_window_focus` has nothing
    to poll.
    """

    def __init__(self, focus_sequence=(None,)):
        super().__init__()
        self._focus_sequence = list(focus_sequence)
        self.calls = 0

    @property
    def capabilities(self):
        return CapabilitySet(
            set(FakeBackend.capabilities.fget(self)) | {Capability.WINDOW_STATE}
        )

    def active_window(self):
        self.calls += 1
        if len(self._focus_sequence) > 1:
            return self._focus_sequence.pop(0)
        return self._focus_sequence[0]


class TestWaitWindowFocus(unittest.TestCase):
    def test_returns_true_immediately_when_already_focused(self):
        backend = FocusBackend()
        gui = pyguitest.Session(backend, pyguitest.detect())
        target = backend._windows[0]
        backend._focus_sequence = [target]
        self.assertTrue(gui.wait_window_focus(target, timeout=1, interval=0.01))
        self.assertEqual(backend.calls, 1)

    def test_polls_until_the_window_actually_gains_focus(self):
        backend = FocusBackend(focus_sequence=[None, None, None])
        gui = pyguitest.Session(backend, pyguitest.detect())
        target = backend._windows[0]
        backend._focus_sequence = [None, None, target]
        self.assertTrue(gui.wait_window_focus(target, timeout=2, interval=0.01))
        self.assertGreaterEqual(backend.calls, 3)

    def test_returns_false_on_timeout_rather_than_raising(self):
        backend = FocusBackend(focus_sequence=[None])
        gui = pyguitest.Session(backend, pyguitest.detect())
        target = backend._windows[0]
        self.assertFalse(gui.wait_window_focus(target, timeout=0.05, interval=0.01))

    def test_a_different_window_holding_focus_does_not_count(self):
        backend = FocusBackend()
        gui = pyguitest.Session(backend, pyguitest.detect())
        target, other = backend._windows
        backend._focus_sequence = [other]
        self.assertFalse(gui.wait_window_focus(target, timeout=0.05, interval=0.01))


class SettlingBackend(FocusBackend):
    """A FocusBackend that also answers geometry() and activate_window().

    Geometry is fixed (already settled); activate_window() records each
    call and the *next* active_window() answer only changes once enough
    calls have been made, so a test can assert the request was retried
    rather than trusted the first time.
    """

    def __init__(self, activations_needed=1):
        super().__init__(focus_sequence=[None])
        self.activations_needed = activations_needed
        self.activate_calls = 0

    @property
    def capabilities(self):
        return CapabilitySet(
            set(FocusBackend.capabilities.fget(self))
            | {Capability.WINDOW_GEOMETRY, Capability.WINDOW_ACTIVATE}
        )

    def geometry(self, window):
        return (0, 0, 100, 100)

    def activate_window(self, window):
        self.activate_calls += 1
        if self.activate_calls >= self.activations_needed:
            self._focus_sequence = [window]


class TestExpectWindow(unittest.TestCase):
    """expect_window: wait_for_window's raising sibling, and nothing more."""

    def test_returns_the_window_once_found(self):
        gui = session()
        window = gui.expect_window("Editor", timeout=1)
        self.assertEqual(window.title, "Document - Editor")

    def test_raises_window_not_found_instead_of_returning_none(self):
        gui = session()
        with self.assertRaises(WindowNotFound):
            gui.expect_window("NoSuchWindow", timeout=0.05)

    def test_raises_by_app_id_too(self):
        gui = session()
        with self.assertRaises(WindowNotFound):
            gui.expect_window(app_id="no.such.app", timeout=0.05)

    def test_does_not_activate_the_window_it_finds(self):
        # Regression, found live on MATE: expect_window used to activate
        # every window it found. Binding the desktop while an application
        # menu was open raised the desktop, dismissed the menu, and left
        # every click that followed landing on nothing. A lookup must stay
        # a lookup.
        backend = SettlingBackend()
        gui = pyguitest.Session(backend, pyguitest.detect())
        gui.expect_window("Editor", timeout=1)
        self.assertEqual(backend.activate_calls, 0)


class TestFocusWindow(unittest.TestCase):
    """focus_window: the activation expect_window deliberately does not do."""

    def test_retries_activation_until_it_actually_takes(self):
        backend = SettlingBackend(activations_needed=3)
        gui = pyguitest.Session(backend, pyguitest.detect())
        target = backend._windows[0]
        self.assertTrue(gui.focus_window(target))
        self.assertGreaterEqual(backend.activate_calls, 3)

    def test_returns_false_when_activation_never_takes(self):
        backend = SettlingBackend(activations_needed=99)
        gui = pyguitest.Session(backend, pyguitest.detect())
        self.assertFalse(gui.focus_window(backend._windows[0], attempts=2))
        self.assertEqual(backend.activate_calls, 2)

    def test_reports_false_rather_than_raising_without_the_capabilities(self):
        # FakeBackend declares neither WINDOW_GEOMETRY nor WINDOW_ACTIVATE:
        # not being able to ask is not a different answer from "it did not
        # take", and must not become an exception out of a best-effort call.
        gui = session()
        self.assertFalse(gui.focus_window(gui.find_window("Editor")))


class TestWaitWindowClose(unittest.TestCase):
    def test_returns_true_immediately_if_already_closed(self):
        gui = session()
        ghost = Window("gone", gui.backend, title="Ghost")
        self.assertTrue(gui.wait_window_close(ghost, timeout=1, interval=0.01))

    def test_polls_until_the_window_disappears(self):
        gui = session()
        target = gui.find_window("Editor")
        calls = []

        def flaky_windows():
            calls.append(None)
            if len(calls) >= 3:
                return [w for w in gui.backend._all if w.handle != target.handle]
            return gui.backend._all

        gui.backend._all = gui.backend._windows
        gui.backend.windows = flaky_windows
        self.assertTrue(gui.wait_window_close(target, timeout=2, interval=0.01))
        self.assertGreaterEqual(len(calls), 3)

    def test_returns_false_on_timeout_while_still_open(self):
        gui = session()
        target = gui.find_window("Editor")
        self.assertFalse(gui.wait_window_close(target, timeout=0.05, interval=0.01))

    def test_matches_by_handle_not_by_title(self):
        # Two windows can share a title; a title can also change before the
        # window actually closes. Only the handle identifies it reliably.
        gui = session()
        target = gui.find_window("Editor")
        gui.backend._windows = [w for w in gui.backend._windows if w is not target]
        self.assertTrue(gui.wait_window_close(target, timeout=1, interval=0.01))

    def test_delegates_to_window_events_when_supported(self):
        from pyguitest.backends.windows import WindowEvent

        class EventBackend(FakeBackend):
            @property
            def capabilities(self):
                return CapabilitySet(
                    set(FakeBackend.capabilities.fget(self))
                    | {Capability.WINDOW_EVENTS}
                )

            def window_events(self, timeout=None):
                closing = self._windows[0]
                yield WindowEvent(change="close", window=closing)

        gui = pyguitest.Session(EventBackend(), pyguitest.detect())
        target = gui.find_window("Editor")
        self.assertTrue(gui.wait_window_close(target, timeout=1))


class TestPollUntil(unittest.TestCase):
    """The private primitive every wait_* method below is now built on."""

    def test_returns_the_producers_actual_value_not_just_true(self):
        # wait_until only ever reports True/False -- this is the one place
        # that hands back what was actually found (a Window, an Element, a
        # pid), which is what makes wait_for_window et al. buildable on it.
        gui = session()
        self.assertEqual(gui._poll_until(lambda: "found it", 1, 0.01), "found it")

    def test_returns_none_on_timeout_rather_than_the_last_falsy_value(self):
        gui = session()
        self.assertIsNone(gui._poll_until(lambda: None, 0.05, 0.01))

    def test_stops_polling_the_moment_the_producer_turns_truthy(self):
        gui = session()
        calls = []

        def producer():
            calls.append(None)
            return "done" if len(calls) >= 3 else None

        self.assertEqual(gui._poll_until(producer, 2, 0.01), "done")
        self.assertEqual(len(calls), 3)


class TestWaitUntil(unittest.TestCase):
    def test_returns_true_immediately_when_predicate_is_already_true(self):
        gui = session()
        self.assertTrue(gui.wait_until(lambda: True, timeout=1, interval=0.01))

    def test_returns_false_on_timeout_rather_than_raising(self):
        gui = session()
        self.assertFalse(gui.wait_until(lambda: False, timeout=0.05, interval=0.01))

    def test_keeps_polling_until_element_state_actually_changes(self):
        gui = session()
        button = gui.button("OK")
        button.enabled = False
        calls = []

        def became_enabled():
            calls.append(None)
            if len(calls) >= 3:
                button.enabled = True
            return button.enabled

        self.assertTrue(gui.wait_until(became_enabled, timeout=2, interval=0.01))
        self.assertGreaterEqual(len(calls), 3)


class TestWaitForElement(unittest.TestCase):
    def test_returns_immediately_when_already_present(self):
        gui = session()
        element = gui.wait_for_element(
            role=Role.PUSH_BUTTON, name="OK", timeout=1, interval=0.01
        )
        self.assertEqual(element.name, "OK")

    def test_returns_none_on_timeout_rather_than_raising(self):
        gui = session()
        self.assertIsNone(
            gui.wait_for_element(name="NoSuchElement", timeout=0.05, interval=0.01)
        )

    def test_keeps_polling_until_the_element_actually_appears(self):
        gui = session()
        all_elements = gui.backend.elements
        gui.backend.elements = [e for e in all_elements if e.name != "OK"]
        calls = []

        def flaky_find_elements(role=None, name=None, within=None):
            calls.append(None)
            if len(calls) >= 3:
                gui.backend.elements = all_elements
            return [
                e
                for e in gui.backend.elements
                if (role is None or e.role == role) and (name is None or e.name == name)
            ]

        gui.backend.find_elements = flaky_find_elements
        element = gui.wait_for_element(
            role=Role.PUSH_BUTTON, name="OK", timeout=2, interval=0.01
        )
        self.assertEqual(element.name, "OK")
        self.assertGreaterEqual(len(calls), 3)


class TestExpectElement(unittest.TestCase):
    """expect_element: wait_for_element's raising sibling."""

    def test_returns_the_element_once_found(self):
        gui = session()
        element = gui.expect_element(role=Role.PUSH_BUTTON, name="OK", timeout=1)
        self.assertEqual(element.name, "OK")

    def test_raises_element_not_found_instead_of_returning_none(self):
        gui = session()
        with self.assertRaises(ElementNotFound):
            gui.expect_element(name="NoSuchElement", timeout=0.05)


class TestExpectText(unittest.TestCase):
    def test_passes_when_the_text_already_matches(self):
        gui = session()
        gui.element(name="Name").set_text("hello")
        gui.expect_text(name="Name", equals="hello", timeout=0.2)

    def test_raises_a_readable_assertion_error_otherwise(self):
        gui = session()
        with self.assertRaisesRegex(AssertionError, "expected 'Name' to read 'hello'"):
            gui.expect_text(name="Name", equals="hello", timeout=0.05)


class TestExpectChecked(unittest.TestCase):
    def test_passes_when_already_in_the_wanted_state(self):
        gui = session()
        gui.expect_checked(name="Remember me", checked=False, timeout=0.2)

    def test_raises_a_readable_assertion_error_otherwise(self):
        gui = session()
        with self.assertRaisesRegex(AssertionError, "expected .* to be checked"):
            gui.expect_checked(name="Remember me", checked=True, timeout=0.05)


class TestExpectShowing(unittest.TestCase):
    def test_passes_when_already_visible(self):
        gui = session()
        gui.expect_showing(name="OK", timeout=0.2)

    def test_raises_a_readable_assertion_error_when_not_visible(self):
        gui = session()
        with self.assertRaisesRegex(AssertionError, "expected .* to be showing"):
            gui.expect_showing(role=Role.PUSH_BUTTON, name="Save", timeout=0.05)


class TestWaitUntilGone(unittest.TestCase):
    def test_returns_true_immediately_if_already_absent(self):
        gui = session()
        self.assertTrue(
            gui.wait_until_gone(name="NoSuchElement", timeout=1, interval=0.01)
        )

    def test_returns_false_on_timeout_while_still_present(self):
        gui = session()
        self.assertFalse(
            gui.wait_until_gone(
                role=Role.PUSH_BUTTON, name="OK", timeout=0.05, interval=0.01
            )
        )

    def test_polls_until_the_element_actually_disappears(self):
        gui = session()
        calls = []

        def flaky_find_elements(role=None, name=None, within=None):
            calls.append(None)
            elements = gui.backend.elements
            if len(calls) >= 3:
                elements = [e for e in elements if e.name != "OK"]
            return [
                e
                for e in elements
                if (role is None or e.role == role) and (name is None or e.name == name)
            ]

        gui.backend.find_elements = flaky_find_elements
        self.assertTrue(
            gui.wait_until_gone(
                role=Role.PUSH_BUTTON, name="OK", timeout=2, interval=0.01
            )
        )
        self.assertGreaterEqual(len(calls), 3)


class FakeImageBackend(GUIBackend):
    name = "fake-image"

    def __init__(self, match=None, geometry_result=(10, 20, 100, 50)):
        self.captured_paths = []
        self.capture_calls = []
        self.geometry_calls = []
        self.locate_calls = []
        self._match = match
        self._geometry_result = geometry_result

    @property
    def capabilities(self):
        return CapabilitySet(
            {
                Capability.SCREEN_CAPTURE,
                Capability.WINDOW_GEOMETRY,
                Capability.IMAGE_LOCATE,
            }
        )

    def capture(self, window=None, path=None, region=None):
        if path is None:
            descriptor, path = tempfile.mkstemp(suffix=".png")
            os.close(descriptor)
        else:
            open(path, "wb").close()
        self.captured_paths.append(path)
        self.capture_calls.append({"window": window, "region": region})
        return path

    def geometry(self, window):
        self.geometry_calls.append(window)
        return self._geometry_result

    def locate(self, haystack, template, region=None, **kwargs):
        self.locate_calls.append((haystack, template, region))
        return self._match


class TestWaitForFile(unittest.TestCase):
    def test_returns_true_immediately_when_the_file_already_exists(self):
        descriptor, path = tempfile.mkstemp()
        os.close(descriptor)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        self.assertTrue(session().wait_for_file(path, timeout=1, interval=0.01))

    def test_returns_false_on_timeout_when_it_never_appears(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(lambda: os.rmdir(directory))
        path = os.path.join(directory, "never-written")
        self.assertFalse(session().wait_for_file(path, timeout=0.05, interval=0.01))


POSIX_PROCESS_TABLE = unittest.skipIf(
    sys.platform == "win32",
    "/proc and `ps` are the POSIX routes to the process table; Windows reaches "
    "it through CreateToolhelp32Snapshot and never runs this code",
)
"""Skips a test that pins the /proc or `ps` implementation specifically.

Not a blanket Windows skip: `_process_cpu_seconds` and `_process_table` both
have a Windows branch that is tested beside these, and those run everywhere.
What is skipped is the POSIX *route*, which on Windows is unreachable rather
than broken.
"""


class TestWaitForProcess(unittest.TestCase):
    @unittest.skipIf(
        sys.platform == "win32",
        "matches against a token in the command line, which Windows does not "
        "expose: Toolhelp reports the executable filename only, and "
        "wait_for_process documents that cut",
    )
    def test_finds_a_running_process_by_cmdline(self):
        # Three things here are deliberate, and this test taught each the
        # hard way -- it failed with "31880 != 32032", having matched
        # somebody else's process, and it later found nothing at all:
        #
        #   * The pattern is unique to this run. Searching for "sleep"
        #     searches the whole process table for a word that any machine
        #     has several of at any moment, so it found a `sleep` belonging
        #     to an unrelated shell loop and reported its pid.
        #   * The process outlives the search rather than racing it. The
        #     original slept for 0.3s and allowed 1s to be found in, which
        #     is a bet that the machine schedules both promptly.
        #   * The token sits at the *end* of a command line longer than the
        #     width `ps` decides to print, and that is what caught `ps`
        #     truncating `args`: the token was the part cut off, so a
        #     process that was running matched nothing. Do not shorten this
        #     line to make it fit. The width is pinned in
        #     test_the_real_ps_call_finds_a_long_command_line, which runs
        #     the real `ps` rather than trusting whatever window this one
        #     happens to run in.
        # Python rather than `sh -c "sleep 30 # token"`: a shell given a
        # single command execs it, which replaces argv and takes the token
        # with it, leaving nothing to search for.
        gui = session()
        token = f"pyguitest-waitfor-{uuid.uuid4().hex[:8]}"
        code = f"import time; time.sleep(30)  # {token}"
        with gui.start_app([sys.executable, "-c", code]) as app:
            pid = gui.wait_for_process(token, timeout=10, interval=0.01)
            self.assertEqual(pid, app.pid)

    def test_returns_none_on_timeout_when_nothing_matches(self):
        gui = session()
        self.assertIsNone(
            gui.wait_for_process("no-such-process-xyz", timeout=0.05, interval=0.01)
        )


class TestWaitForIdle(unittest.TestCase):
    """Driven entirely against a mocked _process_cpu_seconds.

    Real CPU load would make this slow and flaky, and the streak/threshold
    logic does not need a real process to exercise it.
    """

    def test_a_pid_that_has_already_exited_is_immediately_idle(self):
        with mock.patch("pyguitest._process_cpu_seconds", return_value=None):
            self.assertTrue(session().wait_for_idle(99999, timeout=1))

    def test_becomes_idle_once_consecutive_samples_show_no_cpu_growth(self):
        # Two big jumps (busy), then three readings essentially unchanged
        # (idle) -- idle only once the streak of `samples` (default 3)
        # flat readings is reached.
        readings = [
            (seconds, 0.01)
            for seconds in (0.0, 5.0, 10.0, 10.000001, 10.000002, 10.000003)
        ]
        with mock.patch("pyguitest._process_cpu_seconds", side_effect=readings):
            self.assertTrue(
                session().wait_for_idle(1234, timeout=5, interval=0.01, samples=3)
            )

    def test_times_out_while_still_busy(self):
        # Reporting wall-clock time as "CPU seconds consumed" simulates a
        # process pinned at ~100% CPU -- always well over the 1% threshold.
        with mock.patch(
            "pyguitest._process_cpu_seconds",
            side_effect=lambda pid: (time.monotonic(), 0.01),
        ):
            self.assertFalse(session().wait_for_idle(1234, timeout=0.05, interval=0.01))


class TestIdleIsHonestAboutWhatItCanMeasure(unittest.TestCase):
    """The defect this was written for, and the one it must not reintroduce.

    wait_for_idle used to read "cannot tell" as "idle", so a process pinned
    at 100% on a machine with an unreadable /proc was reported idle -- a
    test that should have failed passing instead. It is the only place in
    the package that failed dishonestly, and not FreeBSD-only: a container
    with hidepid=2 hits the same guard on Linux.
    """

    def test_an_unreadable_cpu_time_raises_rather_than_reporting_idle(self):
        with mock.patch(
            "pyguitest._process_cpu_seconds",
            side_effect=pyguitest.PyGUITestError("cannot read CPU time"),
        ):
            with self.assertRaises(pyguitest.PyGUITestError):
                session().wait_for_idle(1234, timeout=1)

    def test_a_clock_coarser_than_the_interval_raises(self):
        # `ps` on procps reports whole seconds. Sampled 0.2s apart, a process
        # using a whole core accumulates 0.2s of CPU and the clock cannot
        # show it -- so everything reads idle, which is the original defect
        # one layer down.
        with mock.patch("pyguitest._process_cpu_seconds", return_value=(3.0, 1.0)):
            with self.assertRaises(pyguitest.PyGUITestError) as caught:
                session().wait_for_idle(1234, timeout=1)
        message = str(caught.exception)
        self.assertIn("nearest 1s", message)
        self.assertIn("at least 1s", message)  # what would actually work

    def test_the_ordinary_proc_configuration_is_not_refused(self):
        # Regression guard: an earlier version of the check compared the
        # resolution against cpu_threshold * interval, which refused /proc's
        # own 0.01s tick at the shipped defaults -- breaking plain Linux.
        readings = [(10.0, 0.01)] * 6
        with (
            mock.patch("pyguitest._process_cpu_seconds", side_effect=readings),
            mock.patch("pyguitest.time.sleep"),
        ):
            self.assertTrue(session().wait_for_idle(1234, timeout=5))

    def test_a_coarse_resolution_is_fine_when_the_question_is_coarse_too(self):
        # Same 1s resolution, but asking about 50% of a core over 5s, which
        # it can genuinely answer.
        readings = [(10.0, 1.0)] * 4
        with (
            mock.patch("pyguitest._process_cpu_seconds", side_effect=readings),
            mock.patch("pyguitest.time.sleep"),  # the logic, not the waiting
        ):
            self.assertTrue(
                session().wait_for_idle(
                    1234, timeout=60, interval=5.0, samples=2, cpu_threshold=0.5
                )
            )


class TestCpuTimeParsing(unittest.TestCase):
    """Both `ps` formats, with the resolution derived rather than assumed."""

    def test_procps_whole_seconds(self):
        self.assertEqual(pyguitest._parse_cpu_time("00:00:02"), (2.0, 1.0))
        self.assertEqual(pyguitest._parse_cpu_time("01:02:03"), (3723.0, 1.0))

    def test_freebsd_centiseconds(self):
        seconds, resolution = pyguitest._parse_cpu_time("0:00.03")
        self.assertAlmostEqual(seconds, 0.03)
        self.assertAlmostEqual(resolution, 0.01)

    def test_days_are_carried(self):
        seconds, _ = pyguitest._parse_cpu_time("2-03:00:00")
        self.assertEqual(seconds, 2 * 86400 + 3 * 3600)


def _ps_result(stdout, returncode=0):
    """Build a subprocess result standing in for a real `ps` run."""
    return subprocess.CompletedProcess(["ps"], returncode, stdout, "")


class _RecordingButtons(GUIBackend):
    """A backend that records only the button events sent to it."""

    name = "buttons"

    def __init__(self):
        self.events = []

    @property
    def capabilities(self):
        return CapabilitySet({Capability.POINTER_BUTTON})

    def press_button(self, button):
        self.events.append(("press", button))

    def release_button(self, button):
        self.events.append(("release", button))


class TestDoubleClick(unittest.TestCase):
    """double_click sends four button events with no pause between them."""

    def _session(self, event_delay=0.0):
        backend = _RecordingButtons()
        gui = pyguitest.Session(backend, pyguitest.detect(), event_delay=event_delay)
        return gui, backend

    def test_it_sends_press_and_release_twice(self):
        gui, backend = self._session()
        gui.double_click()
        self.assertEqual(
            backend.events,
            [("press", 1), ("release", 1), ("press", 1), ("release", 1)],
        )

    def test_it_passes_the_button_through(self):
        gui, backend = self._session()
        gui.double_click(button=3)
        self.assertEqual([button for _, button in backend.events], [3, 3, 3, 3])

    def test_event_delay_does_not_separate_the_two_clicks(self):
        # Calling click() twice would pause four times, leaving 2 x
        # event_delay between the presses -- 400ms at the common 0.2s
        # setting, which is exactly the GTK/Qt double-click threshold. One
        # pause, and it comes after the pair.
        gui, _ = self._session(event_delay=0.05)
        with mock.patch("pyguitest.time.sleep") as slept:
            gui.double_click()
        self.assertEqual(slept.call_count, 1)


class _RecordingPointerAndExtents(_RecordingButtons):
    """_RecordingButtons plus extents() and move_mouse(), for double_click_element."""

    def __init__(self, extents_by_name):
        super().__init__()
        self._extents_by_name = extents_by_name
        self.moved_to = []

    @property
    def capabilities(self):
        return CapabilitySet(
            set(_RecordingButtons.capabilities.fget(self))
            | {Capability.ELEMENT_GEOMETRY, Capability.POINTER_MOVE}
        )

    def extents(self, element):
        return self._extents_by_name.get(element.name)

    def move_mouse(self, x, y, screen=0):
        self.moved_to.append((x, y))


class TestDoubleClickElement(unittest.TestCase):
    def test_moves_to_the_elements_center_and_double_clicks(self):
        element = FakeElement(Role.PUSH_BUTTON, "OK")
        backend = _RecordingPointerAndExtents({"OK": (10, 20, 30, 40)})
        gui = pyguitest.Session(backend, pyguitest.detect())
        gui.double_click_element(element)
        self.assertEqual(backend.moved_to, [(10 + 15, 20 + 20)])
        self.assertEqual(
            backend.events,
            [("press", 1), ("release", 1), ("press", 1), ("release", 1)],
        )

    def test_raises_without_extents(self):
        element = FakeElement(Role.PUSH_BUTTON, "Ghost")
        backend = _RecordingPointerAndExtents({})
        gui = pyguitest.Session(backend, pyguitest.detect())
        with self.assertRaises(PyGUITestError):
            gui.double_click_element(element)


class TestElementDoubleClick(unittest.TestCase):
    """`element.double_click()` is double_click_element, reached from the element.

    The gesture itself lives in Session.double_click_element, so what is
    worth pinning here is the two halves of getting to it: that a session
    binds the elements it hands out, and that the method on the element is
    that same call rather than a second implementation of it.
    """

    def test_the_session_binds_every_element_it_hands_out(self):
        gui = session()
        for element in (
            gui.button("OK"),
            gui.element(role=Role.PUSH_BUTTON),
            gui.elements(role=Role.ENTRY)[0],
        ):
            with self.subTest(element=element):
                self.assertIs(element._session, gui)

    def test_a_bound_element_double_clicks_through_its_session(self):
        element = FakeElement(Role.PUSH_BUTTON, "OK")
        backend = _RecordingPointerAndExtents({"OK": (10, 20, 30, 40)})
        gui = pyguitest.Session(backend, pyguitest.detect())
        gui._bind(element)
        element.double_click()
        self.assertEqual(backend.moved_to, [(25, 40)])
        self.assertEqual(
            backend.events,
            [("press", 1), ("release", 1), ("press", 1), ("release", 1)],
        )

    def test_an_element_type_with_no_room_for_a_session_still_works(self):
        # _bind offers the back-reference rather than demanding it, so a
        # backend whose elements cannot carry one does not start raising
        # out of elements() -- only double_click is unavailable there, and
        # that method says so itself.
        gui = session()
        plain = object()
        self.assertIs(gui._bind(plain), plain)


class TestProcessTableFallsBackToPs(unittest.TestCase):
    """FreeBSD has no /proc unless linprocfs is mounted; ps is the way in."""

    @POSIX_PROCESS_TABLE
    def test_it_uses_ps_when_proc_is_unavailable(self):
        with (
            mock.patch("pyguitest._have_proc", return_value=False),
            mock.patch(
                "pyguitest._ps",
                return_value=_ps_result("  1 /sbin/init\n 42 sleep 30\n"),
            ) as ran,
        ):
            table = pyguitest._process_table()
        self.assertEqual(table[42], "sleep 30")
        # Two `-o` flags, not "pid=,args=": FreeBSD's ps reads everything
        # after `=` as the header for the last keyword, collapsing that to
        # one pid column with empty command lines. The `-ww` lifts ps's
        # default width cap, without which a long command line comes back
        # cut short -- see test_the_real_ps_call_finds_a_long_command_line.
        self.assertEqual(ran.call_args.args, ("axo", "pid=", "-o", "args=", "-ww"))

    @POSIX_PROCESS_TABLE
    def test_the_real_ps_call_finds_a_long_command_line(self):
        # The one test here that runs the actual `ps`. Everything else in
        # this class mocks it, so nothing else would notice procps refusing
        # `-ww`, a width cap coming back, or the line parse losing its last
        # argument. Patching `_have_proc` rather than `_ps` is deliberate:
        # /proc exists on CI, so without that patch this route is never
        # entered there and the flags above are only ever checked for
        # spelling.
        #
        # `COLUMNS` is pinned because ps's cap follows the terminal rather
        # than the call -- the same command came back cut at 74 characters
        # under COLUMNS=80 here, and 96 whole with `-ww`, so leaving the
        # width to the ambient window would let a dropped `-ww` pass on a
        # wide one. The token sits at the end of a line that outruns that
        # width, which is the part a cap cuts off. A fresh `ps` per attempt,
        # not one, so an early call cannot land in the window before the
        # child's argv is its own. Nothing is caught: `ps` being present and
        # rejecting the arguments raises PyGUITestError, and a test that
        # skipped on that would turn the defect it is looking for green.
        #
        # Guarded like the class's other two, and this is the test where the
        # guard earns its keep: `_process_table` answers from Toolhelp before
        # it ever reaches `_ps`, so on Windows this drove the Windows branch
        # rather than the one it names -- and the `which("ps")` below did not
        # skip it, because Git for Windows puts an MSYS `ps.exe` on the
        # runner's PATH for `which` to find. The first `windows-latest` run
        # got `'python.exe'` back for a child started as
        # `python -c "...  # <token>"`: Toolhelp's `szExeFile`, which is the
        # narrower answer that branch documents, not a defect in the flags
        # this test exists to pin.
        if shutil.which("ps") is None:
            self.skipTest("no `ps` on PATH")
        token = f"pyguitest-psroute-{uuid.uuid4().hex[:8]}"
        code = f"import time; time.sleep(30)  # {token}"
        child = subprocess.Popen([sys.executable, "-c", code])

        def stop():
            child.kill()
            child.wait()

        self.addCleanup(stop)
        found = ""
        deadline = time.monotonic() + 5.0
        with mock.patch.dict(os.environ, {"COLUMNS": "80"}):
            while token not in found and time.monotonic() < deadline:
                with mock.patch("pyguitest._have_proc", return_value=False):
                    found = pyguitest._process_table().get(child.pid, "")
                if token not in found:
                    time.sleep(0.05)
        self.assertIn(
            token,
            found,
            f"the whole command line for {child.pid} should come back, got {found!r}",
        )

    @POSIX_PROCESS_TABLE
    def test_no_proc_and_no_ps_raises_rather_than_reporting_nothing(self):
        # An empty table would read as "your process is not running", which
        # is a different and wrong answer from "I cannot tell".
        with (
            mock.patch("pyguitest._have_proc", return_value=False),
            mock.patch("pyguitest._ps", return_value=None),
        ):
            with self.assertRaises(pyguitest.PyGUITestError):
                pyguitest._process_table()


class _RecordingPath(GUIBackend):
    """Every move_mouse call, in order, for the shaped-motion tests."""

    name = "fake-path"

    def __init__(self):
        self.path = []

    @property
    def capabilities(self):
        return CapabilitySet({Capability.POINTER_MOVE})

    def move_mouse(self, x, y, screen=0):
        self.path.append((x, y))


class TestMoveMouseNaturally(unittest.TestCase):
    """A shaped path -- and the same one every time, which is the point.

    Asserted on the emitted coordinates rather than on wall-clock timing,
    because all four shaping terms (ramp, arch, drift, overshoot) are
    visible in the path itself. Timing is pinned separately, where the two
    delays are.
    """

    def gui(self, **kwargs):
        backend = _RecordingPath()
        return pyguitest.Session(backend, pyguitest.detect(), **kwargs), backend

    def test_it_ends_exactly_on_the_target(self):
        # A pixel short here is a click on the wrong thing, so neither the
        # arch nor the drift nor the overshoot may move this one point.
        gui, backend = self.gui()
        gui.move_mouse_naturally(400, 300, start=(0, 0))
        self.assertEqual(backend.path[-1], (400, 300))
        self.assertEqual(gui._pointer, (400, 300))

    def test_it_arrives_as_a_stream_that_starts_near_the_origin(self):
        gui, backend = self.gui()
        gui.move_mouse_naturally(400, 300, start=(0, 0))
        self.assertGreater(len(backend.path), 10)
        self.assertLess(math.dist(backend.path[0], (0, 0)), 20)

    def test_a_waypoint_is_landed_on(self):
        # The deliberate difference from glide(), which passes through them.
        gui, backend = self.gui()
        gui.move_mouse_naturally(400, 0, via=[(200, 120)], start=(0, 0))
        self.assertIn((200, 120), backend.path)
        self.assertEqual(backend.path[-1], (400, 0))

    def test_the_path_bows_off_the_straight_line(self):
        gui, backend = self.gui()
        gui.move_mouse_naturally(400, 0, start=(0, 0), arc=0.2, wobble=0.0)
        self.assertTrue(any(abs(y) > 5 for _, y in backend.path))

    def test_arc_zero_leaves_only_the_drift(self):
        # The two lateral terms are separable, which is what lets them be
        # argued about one at a time.
        gui, backend = self.gui()
        gui.move_mouse_naturally(400, 0, start=(0, 0), arc=0.0, wobble=6.0)
        self.assertTrue(any(abs(y) > 1 for _, y in backend.path))

    def test_an_overshoot_goes_past_the_target_and_returns_to_it(self):
        gui, backend = self.gui()
        gui.move_mouse_naturally(
            400, 0, start=(0, 0), overshoot=0.2, arc=0.0, wobble=0.0
        )
        self.assertGreater(max(x for x, _ in backend.path), 400)
        self.assertEqual(backend.path[-1], (400, 0))

    def test_overshoot_zero_stops_on_the_target(self):
        gui, backend = self.gui()
        gui.move_mouse_naturally(400, 0, start=(0, 0), overshoot=0.0)
        self.assertLessEqual(max(x for x, _ in backend.path), 400)

    def test_the_same_move_replays_identically(self):
        # The seeded default, and the reason it is seeded: a take has to be
        # shootable twice and a test has to be trustworthy. Unseeded here
        # would reintroduce exactly the flakiness glide() refuses.
        paths = []
        for _ in range(2):
            gui, backend = self.gui()
            gui.move_mouse_naturally(400, 300, start=(0, 0))
            paths.append(backend.path)
        self.assertEqual(paths[0], paths[1])

    def test_a_given_seed_overrides_the_derived_one(self):
        gui, backend = self.gui()
        gui.move_mouse_naturally(400, 300, start=(0, 0), seed=7)
        other, other_backend = self.gui()
        other.move_mouse_naturally(400, 300, start=(0, 0), seed=7)
        self.assertEqual(backend.path, other_backend.path)

    def test_a_different_seed_gives_a_different_shape(self):
        # Otherwise the default seed would be decorative and every move the
        # same length would look like the same move.
        gui, backend = self.gui()
        gui.move_mouse_naturally(400, 300, start=(0, 0), seed=1)
        other, other_backend = self.gui()
        other.move_mouse_naturally(400, 300, start=(0, 0), seed=2)
        self.assertNotEqual(backend.path, other_backend.path)

    def test_event_count_follows_duration_and_rate(self):
        gui, backend = self.gui()
        gui.move_mouse_naturally(400, 300, start=(0, 0), duration=1.0, rate=100)
        # Within the slack of per-leg rounding, the overshoot correction and
        # colinear points collapsing in _dedupe.
        self.assertAlmostEqual(len(backend.path), 100, delta=12)

    def test_a_longer_move_takes_longer_without_scaling_linearly(self):
        gui, backend = self.gui()
        gui.move_mouse_naturally(60, 0, start=(0, 0))
        short = len(backend.path)
        other, other_backend = self.gui()
        other.move_mouse_naturally(2400, 0, start=(0, 0))
        self.assertGreater(len(other_backend.path), short)
        self.assertLess(len(other_backend.path), short * 10)

    def test_latency_defaults_to_the_sessions_event_delay(self):
        gui, _ = self.gui(event_delay=0.05)
        with mock.patch("pyguitest.time.sleep") as slept:
            gui.move_mouse_naturally(400, 300, start=(0, 0))
        # The reaction pause is the first sleep of all; the walk's own
        # schedule follows it, and its trailing event_delay comes last.
        self.assertEqual(slept.call_args_list[0].args, (0.05,))

    def test_latency_can_be_overridden(self):
        gui, _ = self.gui(event_delay=0.05)
        with mock.patch("pyguitest.time.sleep") as slept:
            gui.move_mouse_naturally(400, 300, start=(0, 0), latency=0.2)
        self.assertEqual(slept.call_args_list[0].args, (0.2,))

    def test_pause_adds_one_hesitation(self):
        gui, _ = self.gui()
        with mock.patch("pyguitest.time.sleep") as slept:
            gui.move_mouse_naturally(400, 300, start=(0, 0), pause=0.3, latency=0.0)
        self.assertIn(0.3, [call.args[0] for call in slept.call_args_list])

    def test_pause_still_happens_when_start_and_target_are_the_same(self):
        # Every leg has zero length here, all four shaping terms included --
        # a hover-in-place, one of the two documented reasons this method
        # exists -- so the shaped path collapses to a single point after
        # _dedupe. `pause` must not go missing just because there was
        # nothing to split it out of.
        gui, backend = self.gui()
        with mock.patch("pyguitest.time.sleep") as slept:
            gui.move_mouse_naturally(50, 50, start=(50, 50), pause=0.4, latency=0.0)
        self.assertIn(0.4, [call.args[0] for call in slept.call_args_list])
        self.assertEqual(backend.path[-1], (50, 50))
        self.assertEqual(gui._pointer, (50, 50))

    def test_pause_still_happens_on_a_very_short_move(self):
        # A short enough move with shaping switched off collapses to two
        # points rather than one -- a different shape of the same bug, since
        # the split still has to produce two non-empty halves here instead
        # of one real half and one empty no-op.
        gui, backend = self.gui()
        with mock.patch("pyguitest.time.sleep") as slept:
            gui.move_mouse_naturally(
                1,
                0,
                start=(0, 0),
                arc=0.0,
                wobble=0.0,
                overshoot=0.0,
                pause=0.5,
                latency=0.0,
            )
        self.assertIn(0.5, [call.args[0] for call in slept.call_args_list])
        self.assertEqual(backend.path[-1], (1, 0))

    def test_it_refuses_a_negative_duration(self):
        gui, _ = self.gui()
        with self.assertRaises(ValueError):
            gui.move_mouse_naturally(400, 300, start=(0, 0), duration=-1)

    def test_it_refuses_a_non_positive_rate_or_pace(self):
        gui, _ = self.gui()
        for kwargs in ({"rate": 0}, {"pace": 0}, {"rate": -1}):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    gui.move_mouse_naturally(400, 300, start=(0, 0), **kwargs)


class TestProcHelpers(unittest.TestCase):
    """The /proc readers wait_for_process/wait_for_idle are built on."""

    def test_process_cpu_seconds_reads_the_current_process(self):
        seconds, resolution = pyguitest._process_cpu_seconds(os.getpid())
        self.assertIsInstance(seconds, float)
        self.assertGreater(resolution, 0)

    def test_process_cpu_seconds_is_none_for_a_pid_that_does_not_exist(self):
        self.assertIsNone(pyguitest._process_cpu_seconds(2**30))

    @POSIX_PROCESS_TABLE
    def test_cpu_seconds_is_none_when_ps_shows_no_row_and_the_pid_is_gone(self):
        # `ps -p` exits 1 with no rows for a pid that has gone -- on procps
        # and FreeBSD alike. _ps used to fold that into None, so this
        # reached wait_for_idle as an exception rather than as "idle",
        # against the contract its own docstring states.
        with (
            mock.patch("pyguitest._have_proc", return_value=False),
            mock.patch("pyguitest._ps", return_value=_ps_result("", 1)),
        ):
            self.assertIsNone(pyguitest._process_cpu_seconds(2**30))

    @POSIX_PROCESS_TABLE
    def test_cpu_seconds_raises_when_ps_shows_no_row_for_a_live_pid(self):
        # The same empty output, but the process is running -- so it is `ps`
        # that failed, and reporting "gone" would let wait_for_idle call a
        # busy process idle: the dishonest pass this module exists to avoid.
        with (
            mock.patch("pyguitest._have_proc", return_value=False),
            mock.patch("pyguitest._ps", return_value=_ps_result("", 1)),
        ):
            with self.assertRaises(pyguitest.PyGUITestError):
                pyguitest._process_cpu_seconds(os.getpid())

    @POSIX_PROCESS_TABLE
    def test_cpu_seconds_raises_when_ps_cannot_be_run_at_all(self):
        with (
            mock.patch("pyguitest._have_proc", return_value=False),
            mock.patch("pyguitest._ps", return_value=None),
        ):
            with self.assertRaises(pyguitest.PyGUITestError):
                pyguitest._process_cpu_seconds(os.getpid())

    @POSIX_PROCESS_TABLE
    def test_cpu_seconds_parses_the_freebsd_ps_fallback(self):
        # Centiseconds, as FreeBSD 15 actually prints them.
        with (
            mock.patch("pyguitest._have_proc", return_value=False),
            mock.patch("pyguitest._ps", return_value=_ps_result("0:00.44\n")),
        ):
            seconds, resolution = pyguitest._process_cpu_seconds(os.getpid())
        self.assertAlmostEqual(seconds, 0.44)
        self.assertAlmostEqual(resolution, 0.01)

    @POSIX_PROCESS_TABLE
    def test_process_cmdline_reads_the_current_process(self):
        self.assertIn("python", pyguitest._process_cmdline(os.getpid()).lower())

    @POSIX_PROCESS_TABLE
    def test_process_cmdline_is_empty_for_a_pid_that_does_not_exist(self):
        self.assertEqual(pyguitest._process_cmdline(2**30), "")

    @POSIX_PROCESS_TABLE
    def test_proc_pids_includes_the_current_process(self):
        self.assertIn(os.getpid(), set(pyguitest._proc_pids()))


class _WindowsFunction:
    """A callable that also accepts restype/argtypes, as a ctypes function does.

    `_windows_process_cpu_seconds` declares both before calling, exactly as it
    must against a real kernel32 -- a plain bound method refuses those
    assignments, which is why this exists rather than a method on the fake
    directly. Mirrors `tests/test_session.py`'s own `_Function`, kept as a
    separate copy here rather than a cross-module import: each test file's
    fakes are self-contained, the same way `test_win32_backend.py`'s
    `FakeLibrary` does not reach into `test_win32.py`.
    """

    def __init__(self, function):
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


class TestWindowsProcessCpuSeconds(unittest.TestCase):
    """`_windows_process_cpu_seconds`, driven through a fake kernel32.

    Runs on Linux like every other Windows-facing test here: `_platform` and
    `_win32_lib` are both patched, so `_process_cpu_seconds`'s own dispatch is
    exercised too, not only the Windows half in isolation.
    """

    class _FakeKernel32:
        """Just enough of kernel32 to answer GetProcessTimes for one pid."""

        def __init__(self, pid=4242, kernel_ticks=0, user_ticks=0, exists=True):
            self.pid = pid
            self.kernel_ticks = kernel_ticks
            self.user_ticks = user_ticks
            self.exists = exists
            self.last_error = 87  # ERROR_INVALID_PARAMETER: no such pid
            self.opened = None
            self.closed = []
            self.OpenProcess = _WindowsFunction(self._open_process)
            self.GetProcessTimes = _WindowsFunction(self._get_process_times)
            self.CloseHandle = _WindowsFunction(self._close)

        def _open_process(self, access, _inherit, pid):
            self.opened = (access, pid)
            if not self.exists or pid != self.pid:
                return None
            return 0xABCD

        def _get_process_times(self, _handle, _creation, _exit, kernel, user):
            # `kernel`/`user` arrive as the `CArgObject` `ctypes.byref()`
            # produces, which -- unlike a real `POINTER` argument -- ctypes
            # only converts at a genuine foreign-call boundary; `cast` is
            # what test_session.py's own fakes use for the same reason.
            filetime = ctypes.POINTER(pyguitest._FILETIME)
            kernel_struct = ctypes.cast(kernel, filetime)[0]
            user_struct = ctypes.cast(user, filetime)[0]
            kernel_struct.dwLowDateTime = self.kernel_ticks & 0xFFFFFFFF
            kernel_struct.dwHighDateTime = self.kernel_ticks >> 32
            user_struct.dwLowDateTime = self.user_ticks & 0xFFFFFFFF
            user_struct.dwHighDateTime = self.user_ticks >> 32
            return 1

        def _close(self, handle):
            self.closed.append(handle)
            return 1

    def _reading(self, pid=4242, **kwargs):
        kernel32 = self._FakeKernel32(**kwargs)
        with (
            mock.patch("pyguitest._platform", return_value="win32"),
            mock.patch("pyguitest._win32_lib", lambda _name: kernel32),
            # Patched rather than faked on the library: the error is read
            # through ctypes' own saved copy, which no fake DLL can set --
            # see session._last_error for why it is read that way.
            mock.patch("pyguitest._last_error", lambda: kernel32.last_error),
        ):
            return pyguitest._process_cpu_seconds(pid), kernel32

    def test_ticks_convert_to_seconds_at_the_documented_rate(self):
        # 30_000_000 ticks of each is 3 seconds apiece, 6 total: FILETIME's
        # own unit is 100ns, not the second _process_cpu_seconds returns.
        (seconds, resolution), _kernel32 = self._reading(
            kernel_ticks=30_000_000, user_ticks=30_000_000
        )
        self.assertAlmostEqual(seconds, 6.0)
        self.assertAlmostEqual(resolution, 1e-7)

    def test_a_pid_that_does_not_exist_is_none(self):
        reading, kernel32 = self._reading(pid=99999, exists=True)
        self.assertIsNone(reading)
        self.assertEqual(
            kernel32.opened, (pyguitest._PROCESS_QUERY_LIMITED_INFORMATION, 99999)
        )

    def test_access_denied_raises_rather_than_reading_as_gone(self):
        # The one failure OpenProcess can report for a pid that is very much
        # alive: reading it as "exited" would let wait_for_idle call a busy,
        # merely-protected process idle -- the same dishonest pass this
        # module's Linux half is written to avoid.
        kernel32 = self._FakeKernel32(exists=False)
        kernel32.last_error = 5  # ERROR_ACCESS_DENIED
        with (
            mock.patch("pyguitest._platform", return_value="win32"),
            mock.patch("pyguitest._win32_lib", lambda _name: kernel32),
            mock.patch("pyguitest._last_error", lambda: kernel32.last_error),
        ):
            with self.assertRaises(pyguitest.PyGUITestError):
                pyguitest._process_cpu_seconds(4242)

    def test_the_handle_is_always_closed(self):
        _reading, kernel32 = self._reading()
        self.assertEqual(kernel32.closed, [0xABCD])

    @unittest.skipIf(
        sys.platform == "win32",
        "asserts that the Windows branch is *not* taken without _platform "
        "saying so, which can only be checked from somewhere that is not "
        "Windows -- here the branch is correctly taken every time",
    )
    def test_process_cpu_seconds_dispatches_to_windows_when_the_platform_says_so(self):
        # Proves the seam in _process_cpu_seconds itself, not only the
        # Windows half behind it: a Linux run must never reach this path
        # without _platform saying so.
        with mock.patch(
            "pyguitest._win32_lib", side_effect=AssertionError("not Windows")
        ):
            seconds, resolution = pyguitest._process_cpu_seconds(os.getpid())
        self.assertIsInstance(seconds, float)
        self.assertGreater(resolution, 0)


class TestTheProcessEntryLayout(unittest.TestCase):
    """`PROCESSENTRY32W`, which the API validates through its own `dwSize`.

    A wrong layout here is not a wrong answer but a refusal:
    `Process32FirstW` fails outright when `dwSize` is not the size it expects,
    so the whole process table comes back as a raised error on a machine
    nobody is looking at. `ctypes` computes the size from the declared field
    types, so it can be pinned from Linux -- which is the same argument
    `tests/test_win32.py` makes for the `INPUT` structure.
    """

    POINTER = ctypes.sizeof(ctypes.c_void_p)

    def test_it_is_the_documented_size_for_this_abi(self):
        # 568 bytes on 64-bit Windows and 556 on 32-bit: the difference is
        # th32DefaultHeapID's pointer width, plus the tail padding that
        # follows from the structure's own alignment.
        self.assertEqual(
            ctypes.sizeof(pyguitest._PROCESSENTRY32W), 568 if self.POINTER == 8 else 556
        )

    def test_the_pointer_wide_member_is_where_alignment_puts_it(self):
        # The one field that is not 32 bits. Declaring it as a DWORD would
        # leave every field after it four bytes early on a 64-bit machine,
        # and szExeFile reading from the middle of another member.
        entry = pyguitest._PROCESSENTRY32W
        self.assertEqual(entry.th32ProcessID.offset, 8)
        self.assertEqual(
            entry.th32DefaultHeapID.offset, 16 if self.POINTER == 8 else 12
        )
        self.assertEqual(entry.szExeFile.offset, 44 if self.POINTER == 8 else 36)

    def test_the_name_field_holds_utf16_units_not_wchars(self):
        # c_wchar is four bytes on Linux and two on Windows, so a declaration
        # using it would lay out differently here than on the machine it
        # describes -- and dwSize is computed from that layout.
        self.assertEqual(pyguitest._MAX_PATH, 260)
        entry = pyguitest._PROCESSENTRY32W()
        self.assertEqual(ctypes.sizeof(entry.szExeFile), 2 * pyguitest._MAX_PATH)

    def test_a_name_is_read_up_to_its_nul_and_no_further(self):
        entry = pyguitest._PROCESSENTRY32W()
        for index, unit in enumerate([ord(c) for c in "note.exe"] + [0, 88, 89]):
            entry.szExeFile[index] = unit
        self.assertEqual(pyguitest._wide_field(entry.szExeFile), "note.exe")


class TestWindowsProcessTable(unittest.TestCase):
    """`_windows_process_table`, driven through a fake kernel32 on Linux.

    The snapshot walk is the whole of it: `_platform` and `_win32_lib` are
    both patched so `_process_table`'s own dispatch is exercised too.
    """

    class _FakeKernel32:
        """Enough of kernel32 to hand back one Toolhelp snapshot."""

        SNAPSHOT = 0x5150

        def __init__(self, processes=(), snapshot=SNAPSHOT):
            self.processes = list(processes)
            self.snapshot = snapshot
            self.closed = []
            self.sizes = []
            self._remaining = []
            self.CreateToolhelp32Snapshot = _WindowsFunction(self._create)
            self.Process32FirstW = _WindowsFunction(self._first)
            self.Process32NextW = _WindowsFunction(self._next)
            self.CloseHandle = _WindowsFunction(self._close)

        def _create(self, flags, _pid):
            self.flags = flags
            return self.snapshot

        def _fill(self, entry_ref):
            entry = ctypes.cast(entry_ref, ctypes.POINTER(pyguitest._PROCESSENTRY32W))[
                0
            ]
            if not self._remaining:
                return 0
            pid, name = self._remaining.pop(0)
            entry.th32ProcessID = pid
            for index, char in enumerate(name):
                entry.szExeFile[index] = ord(char)
            entry.szExeFile[len(name)] = 0
            return 1

        def _first(self, _snapshot, entry_ref):
            entry = ctypes.cast(entry_ref, ctypes.POINTER(pyguitest._PROCESSENTRY32W))[
                0
            ]
            # The API validates this, so the test records what it was told.
            self.sizes.append(entry.dwSize)
            self._remaining = list(self.processes)
            return self._fill(entry_ref)

        def _next(self, _snapshot, entry_ref):
            return self._fill(entry_ref)

        def _close(self, handle):
            self.closed.append(handle)
            return 1

    def _table(self, **kwargs):
        kernel32 = self._FakeKernel32(**kwargs)
        with (
            mock.patch("pyguitest._platform", return_value="win32"),
            mock.patch("pyguitest._win32_lib", lambda _name: kernel32),
        ):
            return pyguitest._process_table(), kernel32

    def test_every_process_in_the_snapshot_is_reported(self):
        table, kernel32 = self._table(
            processes=[(4, "System"), (900, "explorer.exe"), (1234, "notepad.exe")]
        )
        self.assertEqual(table, {4: "System", 900: "explorer.exe", 1234: "notepad.exe"})
        self.assertEqual(kernel32.flags, pyguitest._TH32CS_SNAPPROCESS)

    def test_the_entry_is_sized_before_the_first_call_reads_it(self):
        # Process32FirstW fails rather than filling anything in when dwSize is
        # not the size it expects, so this is what stands between the walk and
        # an empty table on a real machine.
        _table, kernel32 = self._table(processes=[(1234, "notepad.exe")])
        self.assertEqual(kernel32.sizes, [ctypes.sizeof(pyguitest._PROCESSENTRY32W)])

    def test_the_snapshot_handle_is_always_closed(self):
        _table, kernel32 = self._table(processes=[(1234, "notepad.exe")])
        self.assertEqual(kernel32.closed, [self._FakeKernel32.SNAPSHOT])

    def test_a_failed_snapshot_raises_rather_than_reporting_no_processes(self):
        # An empty table reads as "your process is not running", which is the
        # one answer that is never true -- the rule the /proc and ps routes
        # follow too.
        with self.assertRaises(pyguitest.PyGUITestError):
            self._table(snapshot=pyguitest._INVALID_HANDLE_VALUE)
        with self.assertRaises(pyguitest.PyGUITestError):
            self._table(snapshot=0)

    def _waiting(self, pattern, processes, **kwargs):
        """`wait_for_process` over a faked snapshot, from a real Session.

        The Session is built *before* the platform is faked, so `detect()`
        classifies this machine as what it is and only the process lookup runs
        as Windows -- the point being to exercise `wait_for_process` itself,
        not a session that thinks it is somewhere else.
        """
        gui = session()
        kernel32 = self._FakeKernel32(processes=processes)
        with (
            mock.patch("pyguitest._platform", return_value="win32"),
            mock.patch("pyguitest._win32_lib", lambda _name: kernel32),
        ):
            return gui.wait_for_process(pattern, interval=0.01, **kwargs)

    def test_wait_for_process_matches_the_executable_name(self):
        pid = self._waiting("notepad", [(1234, "notepad.exe")], timeout=0.05)
        self.assertEqual(pid, 1234)

    def test_a_pattern_that_needs_an_argument_finds_nothing(self):
        # The documented cut: Toolhelp carries no command line, so a script
        # behind its interpreter cannot be told from any other Python.
        pid = self._waiting("manage.py", [(1234, "python.exe")], timeout=0.05)
        self.assertIsNone(pid)


class TestLocateImage(unittest.TestCase):
    def test_raises_when_nothing_matches(self):
        backend = FakeImageBackend(match=None)
        gui = pyguitest.Session(backend, pyguitest.detect())
        with self.assertRaises(ImageNotFound):
            gui.locate_image("/tmp/button.png")

    def test_returns_the_match_when_found(self):
        match = ImageMatch(x=1, y=2, width=3, height=4, score=0.0)
        backend = FakeImageBackend(match=match)
        gui = pyguitest.Session(backend, pyguitest.detect())
        self.assertIs(gui.locate_image("/tmp/button.png"), match)

    def test_looks_up_window_geometry_when_within_is_given(self):
        backend = FakeImageBackend(
            match=ImageMatch(0, 0, 1, 1, 0.0), geometry_result=(5, 6, 7, 8)
        )
        gui = pyguitest.Session(backend, pyguitest.detect())
        window = Window("h", backend, title="Some Window")
        gui.locate_image("/tmp/button.png", within=window)
        self.assertEqual(backend.geometry_calls, [window])
        self.assertEqual(backend.locate_calls[0][2], (5, 6, 7, 8))

    def test_no_geometry_lookup_when_within_is_omitted(self):
        backend = FakeImageBackend(match=ImageMatch(0, 0, 1, 1, 0.0))
        gui = pyguitest.Session(backend, pyguitest.detect())
        gui.locate_image("/tmp/button.png")
        self.assertEqual(backend.geometry_calls, [])
        self.assertIsNone(backend.locate_calls[0][2])

    def test_captured_screenshot_is_cleaned_up(self):
        backend = FakeImageBackend(match=ImageMatch(0, 0, 1, 1, 0.0))
        gui = pyguitest.Session(backend, pyguitest.detect())
        gui.locate_image("/tmp/button.png")
        self.assertFalse(os.path.exists(backend.captured_paths[0]))


class TestWidgetFinders(unittest.TestCase):
    def setUp(self):
        self.gui = session()

    def test_button_by_label(self):
        self.gui.button("OK").click()
        self.assertTrue(self.gui.backend.elements[0].clicked)

    def test_text_field_accepts_any_text_role(self):
        field = self.gui.text_field("Name")
        field.set_text("Ada")
        self.assertEqual(field.text, "Ada")

    def test_dropdown_choose(self):
        self.gui.dropdown("Country").choose("Norway")
        self.assertEqual(self.gui.backend.elements[3].chosen, "Norway")

    def test_checkbox(self):
        self.assertFalse(self.gui.checkbox("Remember me").checked)

    def test_missing_element_names_what_was_wanted(self):
        with self.assertRaises(ElementNotFound) as ctx:
            self.gui.button("Nope")
        self.assertIn("push button", str(ctx.exception))
        self.assertIn("Nope", str(ctx.exception))

    def test_missing_text_field_has_its_own_message(self):
        with self.assertRaises(ElementNotFound):
            self.gui.text_field("Absent")

    def test_elements_returns_a_list_rather_than_raising(self):
        self.assertEqual(len(self.gui.elements(role=Role.PUSH_BUTTON)), 3)
        self.assertEqual(self.gui.elements(role=Role.SLIDER), [])

    def test_a_window_passed_as_within_is_a_type_error_that_says_what_to_pass(self):
        # `find_window` returns a Window and `within=` wants the Element
        # `window_element` returns. Found live: the mix-up surfaced as
        # `AttributeError: 'Window' object has no attribute 'node'` from inside
        # the UIA backend, which names neither the mistake nor the fix.
        window = self.gui.windows()[0]
        with self.assertRaises(TypeError) as ctx:
            self.gui.elements(role=Role.PUSH_BUTTON, within=window)
        self.assertIn("window_element", str(ctx.exception))
        with self.assertRaises(TypeError):
            self.gui.element(role=Role.PUSH_BUTTON, within=window)

    def test_elements_filters_by_enabled(self):
        found = self.gui.elements(role=Role.PUSH_BUTTON, enabled=False)
        self.assertEqual([e.name for e in found], ["Cancel"])

    def test_elements_filters_by_visible(self):
        found = self.gui.elements(role=Role.PUSH_BUTTON, visible=False)
        self.assertEqual([e.name for e in found], ["Save"])

    def test_elements_filters_by_description_exact_and_regex(self):
        self.assertEqual(
            [e.name for e in self.gui.elements(description="Save the file")],
            ["Save"],
        )
        self.assertEqual(
            [e.name for e in self.gui.elements(description=re.compile("^Save"))],
            ["Save"],
        )
        self.assertEqual(self.gui.elements(description="no such text"), [])

    def test_elements_name_accepts_a_compiled_regex(self):
        found = self.gui.elements(name=re.compile("^Ca"))
        self.assertEqual([e.name for e in found], ["Cancel"])

    def test_elements_filters_by_predicate(self):
        found = self.gui.elements(predicate=lambda e: e.role == Role.PUSH_BUTTON)
        self.assertEqual({e.name for e in found}, {"OK", "Cancel", "Save"})

    def test_element_combines_named_filters_and_predicate(self):
        found = self.gui.element(
            role=Role.PUSH_BUTTON, predicate=lambda e: not e.enabled
        )
        self.assertEqual(found.name, "Cancel")


class TestWindowElement(unittest.TestCase):
    def setUp(self):
        self.gui = session()

    def test_finds_the_window_element_by_title_regex(self):
        element = self.gui.window_element("Prefer")
        self.assertEqual(element.name, "Preferences")

    def test_raises_when_nothing_matches(self):
        with self.assertRaises(WindowNotFound):
            self.gui.window_element("No Such Window")


class TestFocused(unittest.TestCase):
    def setUp(self):
        self.gui = session()

    def test_returns_none_when_nothing_has_focus(self):
        self.assertIsNone(self.gui.focused())

    def test_finds_the_one_focused_element(self):
        self.gui.backend.elements[2].focused = True  # "Name"
        found = self.gui.focused()
        self.assertEqual(found.name, "Name")

    def test_a_widget_outranks_a_shell_toplevel_claiming_the_same_state(self):
        """Both are published at once on GNOME, and the shell is found first.

        Measured on GNOME Shell 51.rc: exactly two elements carry FOCUSED
        while an ordinary application is active -- the shell's own `Main
        stage` window and the application's focused widget -- and a walk from
        the tree root reaches the shell's first. Returning that one made every
        GNOME desktop look like one that publishes no per-widget focus at all.
        """
        self.gui.backend.elements[6].focused = True  # "Preferences", a FRAME
        self.gui.backend.elements[2].focused = True  # "Name", an ENTRY
        self.assertEqual(self.gui.focused().name, "Name")

    def test_an_application_that_exits_mid_walk_does_not_take_focused_down(self):
        """Scoping reads a pid off each application node, and nodes go stale.

        An application closing between the tree being listed and its pid being
        read is ordinary on a live desktop, and its dead node raises on the
        read. `element_at` already skips a whole application for this reason
        rather than failing the query; focused() has to do the same, or it
        fails for a reason that has nothing to do with focus. Here the only
        application raises, so the scope comes back empty and the whole-desktop
        walk -- the documented fallback -- still finds the focused widget.
        """

        class _DeadApplication:
            @property
            def pid(self):
                raise RuntimeError("accessible node is gone")

        class _Window:
            pid = 4321

        class _Root:
            children = [_DeadApplication()]

        self.gui.backend.elements[2].focused = True  # "Name"
        self.gui.supports = lambda *capabilities: True
        self.gui.active_window = lambda: _Window()
        self.gui.root_element = lambda: _Root()
        self.assertEqual(self.gui.focused().name, "Name")

    def test_a_raw_backend_error_while_scoping_falls_back_rather_than_escaping(self):
        """Scoping reads straight through to AT-SPI, which raises its own errors.

        `active_window()` reads `window.handle.getState()` and `Element.children`
        reads `self.node.children`, so a node whose process has gone raises a raw
        dogtail or D-Bus error, not a PyGUITestError. Catching only the latter
        left focused() able to fail for a reason having nothing to do with focus,
        in the one place whose whole contract is that failing to scope is free.
        """
        self.gui.backend.elements[2].focused = True  # "Name"
        self.gui.supports = lambda *capabilities: True

        def explode():
            raise RuntimeError("org.freedesktop.DBus.Error.ServiceUnknown")

        self.gui.active_window = explode
        self.assertEqual(self.gui.focused().name, "Name")

    def test_a_toplevel_is_still_the_answer_when_nothing_else_claims_focus(self):
        # The genuinely-unsupported desktop, which focus_tracking_works()
        # exists to recognise: preferring a widget must not invent one.
        self.gui.backend.elements[6].focused = True  # "Preferences", a FRAME
        self.assertEqual(self.gui.focused().name, "Preferences")


class TestFocusTrackingWorks(unittest.TestCase):
    """The live probe for desktops that never publish per-widget focus.

    Measured on GNOME Wayland: the only element carrying AT-SPI's FOCUSED
    state was the shell's own toplevel, so a focus assertion could never
    match a widget there -- see docs/validation.md.
    """

    def setUp(self):
        self.gui = session()

    def test_false_when_nothing_has_focus(self):
        self.assertFalse(self.gui.focus_tracking_works())

    def test_true_when_a_real_widget_has_focus(self):
        self.gui.backend.elements[2].focused = True  # "Name", an ENTRY
        self.assertTrue(self.gui.focus_tracking_works())

    def test_false_when_only_a_toplevel_window_reports_focus(self):
        # The GNOME Wayland shape: a window-role element is focused and no
        # widget is, which is exactly the case the probe exists to catch.
        self.gui.backend.elements[6].focused = True  # "Preferences", a FRAME
        self.assertFalse(self.gui.focus_tracking_works())


class TestAssertFocused(unittest.TestCase):
    def setUp(self):
        self.gui = session()

    def test_passes_and_returns_the_element_when_it_matches(self):
        self.gui.backend.elements[2].focused = True  # "Name"
        found = self.gui.assert_focused(name="Name")
        self.assertEqual(found.name, "Name")

    def test_raises_when_nothing_has_focus(self):
        with self.assertRaises(FocusMismatch) as caught:
            self.gui.assert_focused(name="Name")
        self.assertIn("nothing", str(caught.exception))
        self.assertIn("Name", str(caught.exception))

    def test_raises_when_the_wrong_element_has_focus(self):
        self.gui.backend.elements[0].focused = True  # "OK"
        with self.assertRaises(FocusMismatch) as caught:
            self.gui.assert_focused(name="Name")
        self.assertIn("'OK'", str(caught.exception))
        self.assertIn("Name", str(caught.exception))

    def test_role_filter_applies_alongside_focus(self):
        self.gui.backend.elements[2].focused = True  # "Name", an ENTRY
        self.gui.assert_focused(name="Name", role=Role.ENTRY)
        with self.assertRaises(FocusMismatch):
            self.gui.assert_focused(name="Name", role=Role.PUSH_BUTTON)

    def test_predicate_is_anded_with_the_focus_check(self):
        self.gui.backend.elements[2].focused = True  # "Name", an ENTRY
        self.gui.assert_focused(predicate=lambda e: e.role == Role.ENTRY)
        with self.assertRaises(FocusMismatch):
            self.gui.assert_focused(predicate=lambda e: e.role == Role.COMBO_BOX)


class TestPressTab(unittest.TestCase):
    def setUp(self):
        self.gui = session()

    def test_default_presses_tab(self):
        with mock.patch.object(self.gui, "send_keys") as send_keys:
            self.gui.press_tab()
        send_keys.assert_called_once_with("{TAB}")

    def test_reverse_presses_shift_tab(self):
        with mock.patch.object(self.gui, "send_keys") as send_keys:
            self.gui.press_tab(reverse=True)
        send_keys.assert_called_once_with("+({TAB})")


class TestAssertTabOrder(unittest.TestCase):
    def setUp(self):
        self.gui = session()

    def _advance_focus_along(self, order):
        """Patch press_tab to move .focused from each name to the next.

        Simulates what a real Tab press would do, in `order`.
        """
        by_name = {e.name: e for e in self.gui.backend.elements}
        state = {"index": 0}

        def fake_press_tab(reverse=False):
            by_name[order[state["index"]]].focused = False
            state["index"] += 1
            by_name[order[state["index"]]].focused = True

        return mock.patch.object(self.gui, "press_tab", side_effect=fake_press_tab)

    def test_walks_the_whole_order_without_raising(self):
        order = ["OK", "Name", "Country", "Remember me"]
        with self._advance_focus_along(order):
            self.gui.assert_tab_order(order, timeout=1, interval=0.01)
        self.assertTrue(self.gui.backend.elements[4].focused)  # "Remember me"

    def test_empty_list_is_a_no_op(self):
        self.gui.assert_tab_order([], timeout=1, interval=0.01)

    def test_raises_when_a_stop_lands_on_the_wrong_element(self):
        # press_tab always lands on "OK" instead of advancing -- the second
        # expected name is never actually reached.
        with mock.patch.object(self.gui, "press_tab"):
            with self.assertRaises(FocusMismatch):
                self.gui.assert_tab_order(["OK", "Name"], timeout=0.1, interval=0.01)


class TestRoleVocabulary(unittest.TestCase):
    def test_roles_are_the_atspi_strings(self):
        self.assertEqual(Role.PUSH_BUTTON, "push button")
        self.assertEqual(Role.COMBO_BOX, "combo box")

    def test_grouped_role_tuples(self):
        self.assertIn(Role.DIALOG, Role.WINDOW_ROLES)
        self.assertIn(Role.PASSWORD_TEXT, Role.TEXT_ROLES)
        self.assertIn(Role.COMBO_BOX, Role.CHOICE_ROLES)

    def test_role_is_exported_from_the_package(self):
        self.assertIs(pyguitest.Role, Role)


class TestBackendOptions(unittest.TestCase):
    """connect(backend_options=...) reaches the named backend's constructor."""

    def setUp(self):
        from pyguitest import backends

        self.backends = backends
        self.seen = {}

        def factory(environment, **options):
            self.seen.update(options)
            return FakeBackend()

        backends.register(factory, "optiontest", priority=1, opt_in=True)
        self.addCleanup(
            lambda: backends._REGISTRY.remove(
                next(r for r in backends._REGISTRY if r[1] == "optiontest")
            )
        )

    def test_options_reach_the_named_factory(self):
        self.backends.select(None, "optiontest", {"persist_mode": 2})
        self.assertEqual(self.seen, {"persist_mode": 2})

    def test_no_options_still_calls_the_factory_plainly(self):
        self.backends.select(None, "optiontest")
        self.assertEqual(self.seen, {})


if __name__ == "__main__":
    unittest.main()


class TestTierOneAlwaysAvailable(unittest.TestCase):
    """Regression from a live run: a real backend hid the tier-1 capabilities.

    Session implements process launch and timing itself, so they must be
    reported regardless of which backend was selected -- previously they showed
    as unsupported while run_app() worked perfectly.
    """

    def test_reported_even_when_the_backend_omits_them(self):
        gui = session()
        self.assertNotIn(Capability.PROCESS_LAUNCH, gui.backend.capabilities)
        self.assertTrue(gui.supports(Capability.PROCESS_LAUNCH))
        self.assertTrue(gui.supports(Capability.TIMING))

    def test_and_they_actually_work(self):
        self.assertEqual(session().run_app([sys.executable, "-c", ""]).returncode, 0)

    def test_require_accepts_them(self):
        session().require(Capability.PROCESS_LAUNCH, Capability.TIMING)

    def test_capabilities_is_the_union_not_a_replacement(self):
        gui = session()
        self.assertIn(Capability.ELEMENT_TREE, gui.capabilities)
        self.assertIn(Capability.PROCESS_LAUNCH, gui.capabilities)


class TestPublicExports(unittest.TestCase):
    def test_element_not_found_is_in_all(self):
        # Regression: raised by five Session methods and imported directly
        # in examples/03_widgets.py, but omitted from __all__ while its five
        # sibling exceptions were listed.
        self.assertIn("ElementNotFound", pyguitest.__all__)

    def test_the_two_raised_from_real_paths_are_in_all_too(self):
        # The same omission, found by checking __all__ against the pages
        # that describe it: docs/api.md builds its Exceptions table from
        # __all__, so a public exception left out of it is not only
        # unimportable from the package root, it is undocumentable -- absent
        # from the page headed "every public name in pyguitest".
        #
        # ElementNotActionable is what Element.click() raises through atspi,
        # uia and the coordinate fallback, and is the type a caller catches
        # to tell "here, but it cannot be pressed" apart from
        # ElementNotFound's "not here". PortalTimeout is raised by the portal
        # request path when an accepted call is never answered.
        self.assertIn("ElementNotActionable", pyguitest.__all__)
        self.assertIn("PortalTimeout", pyguitest.__all__)

    def test_both_stay_catchable_as_pyguitesterror(self):
        # Why those two are worth exporting rather than being documented as
        # internal detail: an existing `except PyGUITestError` has to keep
        # catching them, and the reference's Exceptions table has to keep
        # being the complete list a reader can trust.
        for cls in (ElementNotActionable, PortalTimeout):
            with self.subTest(cls=cls.__name__):
                self.assertTrue(issubclass(cls, PyGUITestError))


class TestScreenshot(unittest.TestCase):
    """Session.screenshot: the caller-invoked capture."""

    def setUp(self):
        self.backend = FakeImageBackend()
        self.gui = pyguitest.Session(self.backend, pyguitest.detect())

    def _tempname(self):
        descriptor, path = tempfile.mkstemp(suffix=".png")
        os.close(descriptor)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path

    def test_no_arguments_captures_the_whole_desktop(self):
        path = self.gui.screenshot()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        self.assertEqual(self.backend.capture_calls, [{"window": None, "region": None}])

    def test_a_path_is_passed_through_and_returned(self):
        path = self._tempname()
        self.assertEqual(self.gui.screenshot(path=path), path)

    def test_a_window_is_passed_to_the_backend(self):
        window = Window("a", self.backend, title="Editor")
        self.gui.screenshot(path=self._tempname(), window=window)
        self.assertEqual(self.backend.capture_calls[0]["window"], window)

    def test_a_region_is_passed_to_the_backend(self):
        self.gui.screenshot(path=self._tempname(), region=(1, 2, 3, 4))
        self.assertEqual(self.backend.capture_calls[0]["region"], (1, 2, 3, 4))


class TestCaptureOnFailure(unittest.TestCase):
    """The automatic capture: a screenshot of the moment a test failed.

    Taking it afterwards is too late -- by then the app under test has been
    torn down -- so it has to happen while the exception is still
    propagating, which is the whole reason this is a context manager and not
    a call the caller makes in an except: block.
    """

    def setUp(self):
        self.backend = FakeImageBackend()
        self.gui = pyguitest.Session(self.backend, pyguitest.detect())
        self.directory = tempfile.mkdtemp()

    def tearDown(self):
        for name in os.listdir(self.directory):
            os.unlink(os.path.join(self.directory, name))
        os.rmdir(self.directory)

    def test_nothing_is_captured_when_the_block_succeeds(self):
        with self.gui.capture_on_failure(self.directory):
            pass
        self.assertEqual(self.backend.capture_calls, [])
        self.assertEqual(os.listdir(self.directory), [])

    def test_a_failure_captures_and_re_raises_the_original(self):
        with self.assertRaises(ValueError) as caught:
            with self.gui.capture_on_failure(self.directory):
                raise ValueError("the button was not there")
        self.assertEqual(str(caught.exception), "the button was not there")
        self.assertEqual(len(self.backend.capture_calls), 1)

    def test_the_path_is_attached_to_the_exception(self):
        # So a test runner that prints the exception can lead the reader to
        # the image, rather than the image being written somewhere nobody
        # is told about.
        with self.assertRaises(ValueError) as caught:
            with self.gui.capture_on_failure(self.directory):
                raise ValueError("nope")
        self.assertTrue(os.path.exists(caught.exception.screenshot))
        self.assertTrue(caught.exception.screenshot.endswith(".png"))

    def test_the_file_is_named_after_the_failure_by_default(self):
        with self.assertRaises(KeyError):
            with self.gui.capture_on_failure(self.directory):
                raise KeyError("missing")
        self.assertTrue(os.listdir(self.directory)[0].startswith("KeyError-"))

    def test_an_explicit_name_is_used_instead(self):
        with self.assertRaises(ValueError):
            with self.gui.capture_on_failure(self.directory, name="save-dialog"):
                raise ValueError("nope")
        self.assertTrue(os.listdir(self.directory)[0].startswith("save-dialog-"))

    def test_repeated_failures_do_not_overwrite_each_other(self):
        # A parameterized suite failing the same assertion in several cases
        # would otherwise leave only the last image.
        for _ in range(3):
            with self.assertRaises(ValueError):
                with self.gui.capture_on_failure(self.directory, name="same"):
                    raise ValueError("nope")
        self.assertEqual(len(self.backend.captured_paths), 3)

    def test_the_directory_is_created_if_it_does_not_exist(self):
        nested = os.path.join(self.directory, "artifacts", "run-1")
        with self.assertRaises(ValueError):
            with self.gui.capture_on_failure(nested):
                raise ValueError("nope")
        self.assertTrue(os.path.isdir(nested))
        for name in os.listdir(nested):
            os.unlink(os.path.join(nested, name))
        os.rmdir(nested)
        os.rmdir(os.path.dirname(nested))

    def test_the_environment_variable_is_the_default_directory(self):
        target = os.path.join(self.directory, "from-env")
        with mock.patch.dict(os.environ, {"PYGUITEST_SCREENSHOT_DIR": target}):
            with self.assertRaises(ValueError):
                with self.gui.capture_on_failure():
                    raise ValueError("nope")
        self.assertEqual(len(os.listdir(target)), 1)
        for name in os.listdir(target):
            os.unlink(os.path.join(target, name))
        os.rmdir(target)

    def test_a_window_narrows_the_capture(self):
        window = Window("a", self.backend, title="Editor")
        with self.assertRaises(ValueError):
            with self.gui.capture_on_failure(self.directory, window=window):
                raise ValueError("nope")
        self.assertEqual(self.backend.capture_calls[0]["window"], window)

    def test_a_failing_capture_never_replaces_the_failure_it_documents(self):
        # The important one. A session with no capture backend, an
        # unwritable directory, a display already gone -- none of it is
        # more important than the exception being reported, and all of it
        # happens in the field.
        gui = pyguitest.Session(FakeBackend(), pyguitest.detect())
        with self.assertRaises(ValueError) as caught:
            with gui.capture_on_failure(self.directory):
                raise ValueError("the real problem")
        self.assertEqual(str(caught.exception), "the real problem")
        self.assertIsNone(caught.exception.screenshot)
        self.assertIsInstance(caught.exception.screenshot_error, Exception)

    def test_accessibility_tree_is_captured_when_elements_are_supported(self):
        # FakeBackend has ELEMENT_TREE but no capture backend -- the
        # opposite coverage from FakeImageBackend above, together showing
        # each artifact really is attempted independently of the others.
        gui = pyguitest.Session(FakeBackend(), pyguitest.detect())
        with self.assertRaises(ValueError) as caught:
            with gui.capture_on_failure(self.directory):
                raise ValueError("nope")
        self.assertIsNotNone(caught.exception.accessibility_tree)
        self.assertTrue(os.path.exists(caught.exception.accessibility_tree))
        with open(caught.exception.accessibility_tree) as handle:
            tree = json.load(handle)
        self.assertIsInstance(tree, list)

    def test_active_window_error_is_recorded_when_unsupported(self):
        gui = pyguitest.Session(FakeBackend(), pyguitest.detect())
        with self.assertRaises(ValueError) as caught:
            with gui.capture_on_failure(self.directory):
                raise ValueError("nope")
        self.assertIsNone(caught.exception.active_window)
        self.assertIsInstance(caught.exception.active_window_error, Exception)

    def test_focused_element_is_written_even_when_nothing_is_focused(self):
        gui = pyguitest.Session(FakeBackend(), pyguitest.detect())
        with self.assertRaises(ValueError) as caught:
            with gui.capture_on_failure(self.directory):
                raise ValueError("nope")
        self.assertIsNotNone(caught.exception.focused_element)
        with open(caught.exception.focused_element) as handle:
            self.assertIsNone(json.load(handle))

    def test_focused_element_names_the_focused_element(self):
        backend = FakeBackend()
        backend.elements[2].focused = True  # "Name"
        gui = pyguitest.Session(backend, pyguitest.detect())
        with self.assertRaises(ValueError) as caught:
            with gui.capture_on_failure(self.directory):
                raise ValueError("nope")
        with open(caught.exception.focused_element) as handle:
            self.assertEqual(json.load(handle)["name"], "Name")

    def test_one_failing_artifact_does_not_block_the_others(self):
        # FakeBackend: no capture backend (screenshot fails) but ELEMENT_TREE
        # is present (tree and focused_element succeed).
        gui = pyguitest.Session(FakeBackend(), pyguitest.detect())
        with self.assertRaises(ValueError) as caught:
            with gui.capture_on_failure(self.directory):
                raise ValueError("nope")
        self.assertIsNone(caught.exception.screenshot)
        self.assertIsNotNone(caught.exception.accessibility_tree)
        self.assertIsNotNone(caught.exception.focused_element)
