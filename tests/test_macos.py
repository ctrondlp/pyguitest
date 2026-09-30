"""macOS support, driven from a machine that is not a Mac.

Everything here is fake-driven for one reason: what macOS support can do is
decided by TCC grants and by frameworks that only exist on a Mac, so a test
that read the real ones would pin the developer's own System Settings into the
suite. `_platform` is patched to put a Darwin session in front of `detect()`,
`backends._macapi`'s four questions are replaced, and PyObjC is a stand-in
installed in `sys.modules` -- the shape `tests/test_x11.py` established for
`Xlib`.

What cannot be tested this way lives on `docs/validation.md`: the AX-to-
CGWindowList join, deep-tree cost, and whether the TCC attribution rules hold
for a process launched from a terminal.
"""

import importlib
import os
import struct
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

import pyguitest
from pyguitest import backends, hints, session, tools
from pyguitest.backends import capture
from pyguitest.capabilities import Capability, CapabilitySet
from pyguitest.errors import CapabilityUnsupported, PermissionRequired


class FakeQuartz:
    """Stands in for PyObjC's `Quartz`, recording what was posted.

    `__getattr__` answers any `kCG*` name with a stable unique integer, so the
    fake needs no table of constants while a test can still tell one from
    another. That is why `macquartz` reads its constants off the module instead
    of transcribing them: the Python name is its own documentation, and a fake
    can be complete without duplicating it.
    """

    def __init__(self):
        self.calls = []
        self.constants = {}
        self.posted = []

    def __getattr__(self, name):
        if name.startswith("kCG"):
            if name not in self.constants:
                self.constants[name] = len(self.constants) + 1
            return self.constants[name]
        raise AttributeError(name)

    def _record(self, name, *args):
        self.calls.append((name, args))
        return f"{name}-{len(self.calls)}"

    def CGEventSourceCreate(self, state):
        return self._record("CGEventSourceCreate", state)

    def CGEventCreate(self, source):
        return self._record("CGEventCreate", source)

    def CGEventGetLocation(self, event):
        return types.SimpleNamespace(x=11.0, y=22.0)

    def CGEventCreateMouseEvent(self, source, kind, point, button):
        return self._record("CGEventCreateMouseEvent", kind, point, button)

    def CGEventCreateScrollWheelEvent(self, source, units, wheels, *values):
        return self._record("CGEventCreateScrollWheelEvent", units, wheels, *values)

    def CGEventCreateKeyboardEvent(self, source, code, down):
        return self._record("CGEventCreateKeyboardEvent", code, down)

    def CGEventKeyboardSetUnicodeString(self, event, length, text):
        return self._record("CGEventKeyboardSetUnicodeString", event, length, text)

    def CGEventSetFlags(self, event, flags):
        return self._record("CGEventSetFlags", event, flags)

    def CGEventSetIntegerValueField(self, event, field, value):
        return self._record("CGEventSetIntegerValueField", event, field, value)

    def CGEventPost(self, tap, event):
        self.posted.append((tap, event))

    def CGRequestPostEventAccess(self):
        return self._record("CGRequestPostEventAccess")


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


class TestVocabularyAndDetection(unittest.TestCase):
    def test_the_two_new_members_are_spelled_like_sys_platform(self):
        # The rule WIN32 and DWM follow: one spelling, so the platform string
        # and the enum value connect without a table.
        self.assertEqual(session.SessionType.DARWIN.value, "darwin")
        self.assertEqual(session.Compositor.QUARTZ.value, "quartz")

    def test_darwin_is_classified_before_any_variable_is_read(self):
        # The XQuartz case: a Mac with DISPLAY set is still not an X11
        # session, and reading the display first would hand X11Backend a
        # fraction of a desktop and call it full support.
        with mock.patch.object(session, "_platform", return_value="darwin"):
            found = session._classify({"DISPLAY": ":0"})
        self.assertIs(found, session.SessionType.DARWIN)

    def test_a_darwin_session_is_composited_by_quartz(self):
        self.assertIs(
            session._compositor({}, session.SessionType.DARWIN),
            session.Compositor.QUARTZ,
        )

    def test_detect_fills_the_macos_fields_and_the_grant_notes(self):
        with (
            mock.patch.object(session, "_platform", return_value="darwin"),
            mock.patch.object(session, "_module", return_value=False),
            mock.patch.object(
                session, "_darwin_version", return_value="26.7"
            ) as version,
            mock.patch(
                "pyguitest.backends._macapi.accessibility_trusted", return_value=False
            ),
            mock.patch(
                "pyguitest.backends._macapi.screen_recording_allowed",
                return_value=False,
            ),
            mock.patch(
                "pyguitest.backends._macapi.post_event_allowed", return_value=False
            ),
            mock.patch(
                "pyguitest.backends._macapi.listen_event_allowed", return_value=True
            ),
        ):
            environment = session.detect({})

        version.assert_called()
        self.assertIs(environment.session_type, session.SessionType.DARWIN)
        self.assertEqual(environment.macos_version, "26.7")
        self.assertFalse(environment.has_pyobjc_quartz)
        self.assertFalse(environment.has_ax)
        self.assertTrue(environment.has_listen_event)
        # The input question is answered by PostEvent and not Accessibility:
        # a Mac can hold one and not the other, and conflating them refuses
        # input on a machine that granted exactly what input needs.
        self.assertFalse(environment.can_inject_input)
        # `screencapture` is always on PATH, so the grant is the whole
        # question -- and answering from `capture_tools` would call a Mac
        # whose screenshots are black "able to capture".
        self.assertFalse(environment.can_capture)
        for missing in ("PyObjC", "Accessibility", "Screen Recording", "PostEvent"):
            with self.subTest(missing=missing):
                self.assertTrue(
                    any(missing in note for note in environment.notes),
                    f"no note mentions {missing}: {environment.notes}",
                )

    def test_the_summary_says_what_the_session_can_reach(self):
        written = darwin_environment().summary()
        self.assertIn("darwin (macOS 26.7)", written)
        self.assertIn("quartz", written)
        self.assertIn("CGEventPost", written)
        # The mechanisms line must not contradict the input line: that
        # disagreement is why the line exists at all.
        self.assertIn("post-event", written)
        # And the input line must not read as "this Mac injects": `macquartz`
        # is `opt_in`, so a plain `connect()` here injects nothing, and the
        # label says so the way libei's does.
        self.assertIn("opt-in", written)
        self.assertIn('connect(backend="macquartz")', written)

    def test_a_hand_built_environment_keeps_the_answers_it_always_had(self):
        # Every macOS field is defaulted, so nothing off a Mac changes.
        plain = session.Environment(
            session_type=session.SessionType.WAYLAND,
            compositor=session.Compositor.WLROOTS,
        )
        self.assertEqual(plain.macos_version, "")
        self.assertFalse(plain.has_ax)
        self.assertFalse(plain.can_inject_input)
        self.assertNotIn("quartz", plain.summary())


class TestHints(unittest.TestCase):
    def test_the_release_takes_the_place_of_a_distribution(self):
        with mock.patch("pyguitest.hints._darwin_version", return_value="26.7"):
            self.assertEqual(hints.detect_distro(platform="darwin"), "macOS 26.7")

    def test_an_unreadable_release_still_names_the_platform(self):
        # None reads as "unrecognised distribution" and sends the reader
        # looking for a package manager; "macOS" does not.
        with mock.patch("pyguitest.hints._darwin_version", return_value=""):
            self.assertEqual(hints.detect_distro(platform="darwin"), "macOS")

    def test_each_missing_grant_gets_a_row(self):
        environment = darwin_environment(
            has_ax=False,
            has_screen_recording=False,
            has_post_event=False,
            has_listen_event=False,
            has_pyobjc_quartz=False,
            has_pyobjc_application_services=False,
            image_tools=("compare",),
        )
        found = {hint.component for hint in hints.hints_for(environment)}
        self.assertEqual(
            found,
            {
                "the PyObjC bindings",
                "Accessibility",
                "Screen Recording",
                "PostEvent",
                "Input Monitoring",
            },
        )

    def test_a_granted_mac_is_told_nothing_is_missing(self):
        environment = darwin_environment(image_tools=("compare",))
        self.assertEqual(list(hints.hints_for(environment)), [])

    def test_no_macos_row_reuses_a_linux_component_name(self):
        # `advice()` decides what to append by matching on component names:
        # "AT-SPI" pulls in the extra line and "membership of the 'input'
        # group" the whole /dev/uinput walkthrough. Either would print a page
        # of Linux instructions under a Mac that has neither.
        environment = darwin_environment(
            has_ax=False, has_screen_recording=False, has_post_event=False
        )
        written = hints.advice(environment)
        self.assertNotIn("/dev/uinput", written)
        self.assertNotIn("pyguitest[atspi]", written)

    def test_every_row_names_the_binary_the_grant_is_recorded_against(self):
        # TCC records consent against a binary, so a reader who granted it to
        # their system Python -- or to the virtualenv from before an upgrade
        # -- has to be able to see which one this is.
        environment = darwin_environment(has_ax=False, image_tools=("compare",))
        found = list(hints.hints_for(environment))
        self.assertEqual(len(found), 1)
        self.assertIn(sys.executable, found[0].why)
        self.assertIsNone(found[0].command)
        self.assertFalse(found[0].installable)

    def test_a_mac_that_holds_the_grant_is_told_the_backend_is_opt_in(self):
        # The environment alone cannot answer this one: the PostEvent grant is
        # held and the binding is installed, so every grant row is silent --
        # and yet a plain connect() here injects nothing, because `macquartz`
        # is `opt_in` and composition never selects it. Only the live
        # capability set can say so.
        environment = darwin_environment(image_tools=("compare",))
        found = list(hints.hints_for(environment, capabilities=CapabilitySet()))
        self.assertEqual([hint.component for hint in found], ["macquartz"])
        self.assertIn('connect(backend="macquartz")', found[0].why)
        self.assertFalse(found[0].installable)
        self.assertIsNone(found[0].command)

    def test_a_session_that_named_macquartz_is_not_told_to_name_it(self):
        environment = darwin_environment(image_tools=("compare",))
        named = CapabilitySet({Capability.POINTER_MOVE, Capability.KEY_EVENT})
        self.assertEqual(list(hints.hints_for(environment, capabilities=named)), [])
        # None means "not asked" rather than "an empty session": without a live
        # set there is no way to tell a Mac that named the backend from one
        # that did not, and a hint invented from nothing is worse than none.
        self.assertEqual(list(hints.hints_for(environment)), [])

    def test_the_binding_alone_does_not_make_the_clipboard_reachable(self):
        # No member of this package serves CLIPBOARD on Darwin yet -- the
        # element backend that will is ADR 004's next phase -- so answering
        # from `has_pyobjc_quartz` would report a capability nothing can
        # honour, the read `can_capture` documents in the other direction.
        self.assertFalse(darwin_environment().can_use_clipboard)


class TestScreenRecordingGate(unittest.TestCase):
    """`screencapture` fails *successfully* when the grant is missing."""

    def test_the_argv_is_the_documented_one(self):
        self.assertEqual(
            capture.screencapture_argv("/tmp/a.png"),
            ["screencapture", "-x", "/tmp/a.png"],
        )
        self.assertEqual(
            capture.screencapture_argv("/tmp/a.png", (1, 2, 30, 40)),
            ["screencapture", "-x", "-R", "1,2,30,40", "/tmp/a.png"],
        )
        self.assertEqual(
            capture.screencapture_argv("/tmp/a.png", window_id=99),
            ["screencapture", "-x", "-o", "-l", "99", "/tmp/a.png"],
        )

    def test_a_denied_grant_raises_before_the_tool_runs(self):
        with mock.patch(
            "pyguitest.backends._macapi.screen_recording_allowed", return_value=False
        ):
            with self.assertRaises(PermissionRequired) as raised:
                capture.require_screen_recording()
        self.assertIn("Screen Recording", str(raised.exception))

    def test_the_backend_refuses_the_same_way(self):
        tool = next(t for t in tools.CAPTURE_TOOLS if t.name == "screencapture")
        spawned = []
        backend = capture.ToolCaptureBackend(tool, runner=spawned.append)
        with mock.patch(
            "pyguitest.backends._macapi.screen_recording_allowed", return_value=False
        ):
            with self.assertRaises(PermissionRequired):
                backend.capture(path="/tmp/never.png")
        # The point of the gate: a black image is never produced, so the tool
        # is never spawned and nothing is written.
        self.assertEqual(spawned, [])

    def test_a_granted_mac_runs_the_tool_as_usual(self):
        def runner(argv):
            with open(argv[-1], "w", encoding="utf-8") as handle:
                handle.write("png-ish")

        tool = next(t for t in tools.CAPTURE_TOOLS if t.name == "screencapture")
        backend = capture.ToolCaptureBackend(tool, runner=runner)
        descriptor, path = tempfile.mkstemp(suffix=".png")
        os.close(descriptor)
        try:
            with mock.patch(
                "pyguitest.backends._macapi.screen_recording_allowed",
                return_value=True,
            ):
                written = backend.capture(path=path, region=(0, 0, 10, 10))
            self.assertEqual(written, path)
            self.assertGreater(os.path.getsize(path), 0)
        finally:
            os.unlink(path)


class TestMacquartz(unittest.TestCase):
    """The input backend, against a stand-in `Quartz` in `sys.modules`."""

    def setUp(self):
        self.quartz = FakeQuartz()
        patcher = mock.patch.dict(sys.modules, {"Quartz": self.quartz})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.granted = mock.patch(
            "pyguitest.backends._macapi.post_event_allowed", return_value=True
        )
        self.granted.start()
        self.addCleanup(self.granted.stop)
        from pyguitest.backends import macquartz

        self.macquartz = macquartz

    def build(self):
        return self.macquartz.MacquartzBackend(None)

    def test_available_follows_the_binding_not_the_grant(self):
        # Two questions, two fixes: "install the extra" and "grant the
        # permission" must not be folded into one answer.
        self.assertTrue(self.macquartz.available())
        with mock.patch.dict(sys.modules, {"Quartz": None}):
            self.assertFalse(self.macquartz.available())

    def test_constructing_asks_for_the_post_event_grant(self):
        # The request is made, and that is all it is: measured live, the call
        # showed no dialog on macOS 26.7, so this pins the call rather than a
        # prompt that anything could answer.
        self.build()
        self.assertIn(
            "CGRequestPostEventAccess", [name for name, _ in self.quartz.calls]
        )

    def test_a_denied_grant_withdraws_every_input_capability(self):
        backend = self.build()
        self.assertIn(Capability.TEXT_ENTRY, backend.capabilities)
        with mock.patch(
            "pyguitest.backends._macapi.post_event_allowed", return_value=False
        ):
            self.assertEqual(len(backend.capabilities), 0)

    def test_the_pointer_moves_in_display_space_and_refuses_a_screen(self):
        backend = self.build()
        backend.move_mouse(10, 20)
        kind, point = next(
            (args[0], args[1])
            for name, args in self.quartz.calls
            if name == "CGEventCreateMouseEvent"
        )
        self.assertEqual(point, (10, 20))
        self.assertEqual(kind, self.quartz.kCGEventMouseMoved)
        with self.assertRaises(CapabilityUnsupported):
            backend.move_mouse(1, 2, screen=1)

    def click_counts(self):
        """The click count stamped on each button event, in order."""
        return [
            args[2]
            for name, args in self.quartz.calls
            if name == "CGEventSetIntegerValueField"
            and args[1] == self.quartz.kCGMouseEventClickState
        ]

    def test_a_second_press_in_time_is_the_second_click_of_a_double(self):
        # AppKit reads a double-click from the event's click count, not from
        # timing: measured on macOS 26.7, two presses that all said 1 left a
        # double-clicked word in TextEdit unselected.
        backend = self.build()
        for _ in range(2):
            backend.press_button(1)
            backend.release_button(1)
        self.assertEqual(self.click_counts(), [1, 1, 2, 2])

    def test_a_late_or_different_press_starts_counting_again(self):
        backend = self.build()
        backend.press_button(1)
        backend.release_button(1)
        backend.press_button(3)
        backend.release_button(3)
        with mock.patch(
            "pyguitest.backends.macquartz._double_click_seconds",
            lambda _quartz: 0.0,
        ):
            backend.press_button(3)
            backend.release_button(3)
        self.assertEqual(self.click_counts(), [1, 1, 1, 1, 1, 1])

    def test_a_move_with_a_button_held_is_a_drag(self):
        # A move posted under a held button is not delivered as a drag: measured
        # on macOS 26.7, a canvas saw the press and the release and nothing in
        # between.
        backend = self.build()
        backend.press_button(1)
        backend.move_mouse(30, 40)
        backend.release_button(1)
        backend.move_mouse(50, 60)
        kinds = [
            args[0]
            for name, args in self.quartz.calls
            if name == "CGEventCreateMouseEvent"
        ]
        self.assertEqual(
            kinds,
            [
                self.quartz.kCGEventLeftMouseDown,
                self.quartz.kCGEventLeftMouseDragged,
                self.quartz.kCGEventLeftMouseUp,
                self.quartz.kCGEventMouseMoved,
            ],
        )

    def test_scroll_sends_both_axes_in_this_packages_sign_convention(self):
        self.build().scroll(dx=2, dy=-3)
        _name, args = next(
            call
            for call in self.quartz.calls
            if call[0].startswith("CGEventCreateScroll")
        )
        self.assertEqual(args, (self.quartz.kCGScrollEventUnitLine, 2, -3, 2))

    def test_a_modifier_is_recorded_and_stamped_on_what_follows(self):
        backend = self.build()
        backend.press_key("command")
        backend.press_key("a")
        backend.release_key("command")
        backend.press_key("b")
        flags = [
            args[1] for name, args in self.quartz.calls if name == "CGEventSetFlags"
        ]
        # Four events. The modifier's own press carries its own flag, and that is not
        # a detail: a listener is handed a *flags-changed* event, which says press or
        # release through its flag set and nothing else, so a press posted with no
        # flag is read as a release -- measured live against a real tap, which is why
        # the flag is now recorded before the press event is built rather than after.
        # The key posted between the modifier going down and coming up carries it too,
        # and the release drops it *before* building its own event, so nothing left
        # over shifts a later keystroke.
        self.assertEqual(
            flags,
            [
                self.quartz.kCGEventFlagMaskCommand,
                self.quartz.kCGEventFlagMaskCommand,
                0,
                0,
            ],
        )

    def test_a_recorded_keysym_resolves_to_the_key_it_names(self):
        # What a *recording* writes for a key. pyguitest-recorder's macOS
        # backend names every key it can name by its X11 keysym -- that is the
        # vocabulary `RawEvent.keysym` is in and the one `send_keys` takes -- so
        # a replayed `press_key("Alt_L")` or `tap_key("apostrophe")` has to
        # land on the key this backend's legend name lands on. All fifteen
        # raised before the keysym table existed: a recorded Command+S died on
        # its own modifier, and a recorded `'` died on the key that types it.
        backend = self.build()
        for keysym, legend in (
            ("Super_L", "command"),
            ("Super_R", "rightcommand"),
            ("Shift_L", "shift"),
            ("Shift_R", "rightshift"),
            ("Alt_L", "option"),
            ("Alt_R", "rightoption"),
            ("Control_L", "control"),
            ("Control_R", "rightcontrol"),
            ("Caps_Lock", "capslock"),
            ("BackSpace", "delete"),
            ("Page_Up", "pageup"),
            ("Page_Down", "pagedown"),
            ("bracketleft", "leftbracket"),
            ("bracketright", "rightbracket"),
            ("apostrophe", "quote"),
        ):
            with self.subTest(keysym=keysym):
                self.assertEqual(backend._keycode(keysym), backend._keycode(legend))
                backend.press_key(keysym)
                backend.release_key(keysym)
        # And the case-insensitive half of the same promise, for the spellings
        # that do not collide with a legend name.
        self.assertEqual(backend._keycode("BACKSPACE"), backend._keycode("delete"))
        self.assertEqual(backend._keycode("alt_r"), backend._keycode("rightoption"))

    def test_the_x11_delete_is_forward_delete_where_the_legend_is_backspace(self):
        # The one pair the two vocabularies disagree on rather than merely spell
        # differently: X11's `Delete` is the forward-delete key (117) while the
        # key a Mac prints "delete" on is backspace (51). Folded they are one
        # string, so the keysym spelling has to win before the fold -- and this
        # is the case a case-insensitive alias table cannot express at all.
        backend = self.build()
        self.assertEqual(backend._keycode("Delete"), 0x75)  # forwarddelete
        self.assertEqual(backend._keycode("delete"), 0x33)  # backspace
        self.assertEqual(backend._keycode("BackSpace"), 0x33)
        self.assertEqual(backend._keycode("forwarddelete"), 0x75)
        # Upper-casing a legend name does not turn it into the keysym: `{DEL}`
        # is the abbreviation for forward delete and resolves through
        # KEY_ALIASES, not through the folded name table.
        self.assertEqual(backend._keycode("DELETE"), 0x33)
        self.assertEqual(
            self.macquartz.MacquartzBackend.KEY_ALIASES["DEL"], "forwarddelete"
        )

    def test_a_recorded_chord_stamps_the_modifier_it_was_recorded_with(self):
        # A chord reaches a replay as its parts: hold the modifier, press the
        # key, let the modifier go. The stamping is read off `_held`, which is
        # keyed by this backend's own names, so the recorded spelling has to
        # resolve *there* too -- resolving only the keycode posts the Command
        # key and then sends a plain `s`, which is a chord that reads as a
        # shortcut and does not act as one.
        backend = self.build()
        backend.press_key("Super_L")
        backend.press_key("s")
        backend.release_key("Super_L")
        backend.press_key("s")
        flags = [
            args[1] for name, args in self.quartz.calls if name == "CGEventSetFlags"
        ]
        self.assertEqual(
            flags,
            [
                self.quartz.kCGEventFlagMaskCommand,
                self.quartz.kCGEventFlagMaskCommand,
                0,
                0,
            ],
        )

    def test_type_text_posts_unicode_rather_than_keycodes(self):
        # Layout-independent: this is what lets an accented letter arrive as
        # itself on a machine with any keyboard layout.
        self.build().type_text("é")
        written = [
            args[1:]
            for name, args in self.quartz.calls
            if name.startswith("CGEventKeyboard")
        ]
        self.assertEqual(written, [(1, "é")])

    def test_type_text_counts_utf16_units_rather_than_characters(self):
        # `CGEventKeyboardSetUnicodeString` takes a `UniChar *` count, one unit
        # per 16 bits, so a character outside the BMP is two units. `len()`
        # there told CGEvent to read half of a surrogate pair, and a wrong
        # character arrives rather than a missing one.
        self.assertEqual(self.macquartz._utf16_units("a"), 1)
        self.assertEqual(self.macquartz._utf16_units("\u00e9"), 1)
        self.assertEqual(self.macquartz._utf16_units("\U0001f600"), 2)
        self.build().type_text("\U0001f600")
        written = [
            args[1:]
            for name, args in self.quartz.calls
            if name.startswith("CGEventKeyboard")
        ]
        self.assertEqual(written, [(2, "\U0001f600")])

    def test_characters_resolve_to_macos_key_names(self):
        backend = self.build()
        self.assertEqual(backend.resolve_char_key("?"), ("slash", True))
        self.assertEqual(backend.resolve_char_key("A"), ("a", True))
        self.assertEqual(backend.resolve_char_key("7"), ("7", False))
        with self.assertRaises(ValueError):
            backend.resolve_char_key("\u00e9")

    def test_an_unknown_key_names_the_vocabulary(self):
        backend = self.build()
        with self.assertRaises(CapabilityUnsupported) as raised:
            backend.press_key("Scroll_Lock")
        self.assertIn("_KEYCODES", str(raised.exception))

    def test_alt_gr_is_refused_rather_than_aliased_to_option(self):
        # Option is a real key on a Mac and level 3 is not; aliasing would
        # make `{&x}` read as one thing and post another.
        self.assertNotIn("&", self.macquartz.MacquartzBackend.MODIFIER_KEYS)
        self.assertEqual(self.macquartz.MacquartzBackend.MODIFIER_KEYS["#"], "command")

    def test_input_sync_is_absent_rather_than_a_no_op(self):
        backend = self.build()
        self.assertNotIn(Capability.INPUT_SYNC, backend.capabilities)


class TestThePointerReadIsDeliberatelyUndeclared(unittest.TestCase):
    """`pointer_position()` works; `POINTER_QUERY` is not this backend's.

    The module-level function is ungated on purpose -- a button press has to
    place itself, and asking through the backend would refuse on the one
    machine whose grant a click does not need -- so a click works while
    `gui.pointer_position()` raises. Measured live on macOS 26.7 with
    Accessibility and PostEvent granted: `CGEventGetLocation` answered
    `(640.0, 450.0)` for a `move_mouse(640, 450)` exactly, and the capability
    stayed `[ no]` under the tier whose description says "deliberately
    prevented". ADR 004 §6 assigns the capability to `macos` instead, which now
    serves it: a plain `connect()` on a Mac declares `POINTER_QUERY` and answers, and
    `tests/test_macos_backend.py` pins that side. What *this* class pins is the other
    half, which is still true and is why the gap was visible at all -- a session built
    from `macquartz` alone has no pointer read, so the function beneath it stays
    deliberately ungated.
    """

    def setUp(self):
        quartz = FakeQuartz()
        modules = mock.patch.dict(sys.modules, {"Quartz": quartz})
        modules.start()
        self.addCleanup(modules.stop)
        granted = mock.patch(
            "pyguitest.backends._macapi.post_event_allowed", return_value=True
        )
        granted.start()
        self.addCleanup(granted.stop)
        from pyguitest.backends import macquartz

        self.macquartz = macquartz
        self.quartz = quartz

    def test_the_ungated_read_needs_no_capability(self):
        self.assertEqual(self.macquartz.pointer_position(self.quartz), (11, 22))

    def test_the_backend_does_not_declare_the_capability(self):
        backend = self.macquartz.MacquartzBackend(None)
        self.assertNotIn(Capability.POINTER_QUERY, backend.capabilities)

    def test_a_session_refuses_the_read_the_call_beneath_it_can_do(self):
        backend = self.macquartz.MacquartzBackend(None)
        gui = pyguitest.Session(backend, darwin_environment())
        with self.assertRaises(CapabilityUnsupported):
            gui.pointer_position()


@unittest.skipUnless(
    sys.platform == "darwin",
    "the real Quartz exists only on a Mac; everything above this class is the "
    "fake-driven half that runs everywhere",
)
class TestLiveQuartz(unittest.TestCase):
    """The real `Quartz`, on a Mac, with whatever grant that Mac happens to have.

    These are the checks `tests/` cannot make from any other machine: a
    transcription error in a framework path or an `argtypes` list is invisible
    to a fake module, because a fake never sees one.

    Two separate conditions skip from `setUp`, per test rather than per class,
    and the binding is the first of them because the grant cannot stand in for
    it. The `macos` extra is what puts PyObjC's `Quartz` on the path, and the
    `macos` job runs this suite *before* installing it -- deliberately, so that
    `detect()` is measured on a machine without it -- so there an absent
    binding is the arrangement rather than a fault. That runner also reports
    PostEvent as granted (its probe step prints all four preflights), which is
    exactly why the grant check below cannot be the only gate: it lets every
    test through on a machine with no `Quartz` to import.
    """

    def setUp(self):
        from pyguitest.backends import _macapi

        try:
            importlib.import_module("Quartz")
        except ImportError:
            self.skipTest(
                "PyObjC's Quartz is not installed, so there is no real binding "
                "here to check; the 'macos' extra is what puts one there"
            )
        if not _macapi.post_event_allowed():
            self.skipTest("PostEvent is not granted, so every post is a no-op")

    def test_every_entry_point_the_backend_calls_is_there(self):
        from pyguitest.backends import macquartz

        quartz = macquartz.client()
        for name in macquartz._NEEDED:
            self.assertTrue(callable(getattr(quartz, name, None)), name)

    def test_a_synthetic_move_reads_back_where_it_was_put(self):
        """The coordinate space, measured -- unless a hand is fighting for it.

        `move_mouse` posts a warp, and a machine that is *also* being driven by
        a physical pointer (a VM whose host pointer is live, a developer moving
        their mouse) re-asserts its own position immediately, so the read
        reports the hand rather than the warp. That is a property of the room
        rather than of this package, so it retries and then skips with that
        sentence instead of failing: the same assertion held on the first run
        against this VM, with nothing driving the pointer.
        """
        from pyguitest.backends import macquartz

        backend = macquartz.MacquartzBackend(None)
        where = macquartz.pointer_position(backend._q)
        target = (where[0] + 7, where[1] + 7)
        self.addCleanup(backend.move_mouse, *where)
        for _ in range(10):
            backend.move_mouse(*target)
            time.sleep(0.05)
            if macquartz.pointer_position(backend._q) == target:
                return
        self.skipTest("a physical pointer is overriding synthetic warps here")

    def test_the_pointer_read_answers_with_no_grant_at_all(self):
        # The half of that which no physical pointer can spoil, and the claim
        # ADR 004 §6 makes: the read needs nothing, whatever the grant is.
        from pyguitest.backends import macquartz

        backend = macquartz.MacquartzBackend(None)
        x, y = macquartz.pointer_position(backend._q)
        self.assertIsInstance(x, int)
        self.assertIsInstance(y, int)

    def test_the_capability_set_follows_the_grant_this_machine_gave(self):
        from pyguitest.backends import macquartz

        capabilities = macquartz.MacquartzBackend(None).capabilities
        for capability in (
            Capability.POINTER_MOVE,
            Capability.POINTER_BUTTON,
            Capability.POINTER_SCROLL,
            Capability.KEY_EVENT,
            Capability.TEXT_ENTRY,
        ):
            self.assertIn(capability, capabilities)
        # PostEvent's own permanent refusal, absent with the grant in place.
        self.assertNotIn(Capability.INPUT_SYNC, capabilities)


def _backing_scale(quartz, x, y):
    """The backing scale of the display holding the point `(x, y)`.

    Pixels over points, read from the display itself: 1.0 on the machine the
    live run used, whose one display reported 1280x800 bounds *and* 1280x800
    pixels, and 2.0 on a Retina display.

    Deliberately not `Screen.scale`. This backend defines that as DPI/96 --
    0.75 on that same display, because a 72 DPI virtual display is below 96 and
    answering so is the convention -- so the two numbers are different measures,
    and only one of them is about the image `screencapture` writes.

    A point on no display, and a machine with no displays at all, answer 1.0:
    the strict size is the right default for a factor nobody could read.
    """
    from pyguitest.backends import macos

    err, displays, count = quartz.CGGetActiveDisplayList(
        macos._MAX_ACTIVE_DISPLAYS, None, None
    )
    if err or not count:
        return 1.0
    for display in list(displays)[:count]:
        bounds = quartz.CGDisplayBounds(display)
        inside = (
            bounds.origin.x <= x < bounds.origin.x + bounds.size.width
            and bounds.origin.y <= y < bounds.origin.y + bounds.size.height
        )
        if inside:
            points = bounds.size.width
            return quartz.CGDisplayPixelsWide(display) / points if points else 1.0
    return 1.0


def _accepted_sizes(width, height, scale):
    """The PNG sizes a correct capture of this window could have.

    Two of them, because the unit is `screencapture`'s choice rather than this
    package's: the window's rectangle in points, which the live run measured,
    and the same rectangle in backing pixels, which is what a Retina display
    would write and which no run has measured. At a scale of 1 the two are one
    size, so the assertion there is the strict one it always was.

    The shadow padding is a constant number of pixels per side, so it matches
    neither -- which is what keeps this a regression test rather than a test
    that passes either way.
    """
    return {(width, height), (round(width * scale), round(height * scale))}


class TestTheWindowCaptureSizeTolerance(unittest.TestCase):
    """The half of `TestLiveWindowCapture` that needs no Mac.

    What `screencapture` actually writes can only be measured on a Mac, so that
    assertion stays live. What can be checked anywhere is the decision around
    it: which sizes a display's backing scale puts in play, and that the size
    which must never be accepted -- the window plus its shadow padding -- is
    not, at either scale.
    """

    class FakeDisplays:
        """A display list of `((x, y, width, height), (pixels_wide, pixels_high))`.

        The three calls are the ones `macos.screens` makes, in the shapes it
        reads them: a list plus a count, a bounds rectangle, and a pixel width.
        """

        def __init__(self, displays):
            self.displays = displays

        def CGGetActiveDisplayList(self, maximum, _array, _count):
            return 0, self.displays[:maximum], min(len(self.displays), maximum)

        def CGDisplayBounds(self, display):
            x, y, width, height = display[0]
            return types.SimpleNamespace(
                origin=types.SimpleNamespace(x=float(x), y=float(y)),
                size=types.SimpleNamespace(width=float(width), height=float(height)),
            )

        def CGDisplayPixelsWide(self, display):
            return display[1][0]

    def test_a_one_to_one_display_answers_one(self):
        quartz = self.FakeDisplays([((0, 0, 1280, 800), (1280, 800))])
        self.assertEqual(_backing_scale(quartz, 640, 400), 1.0)

    def test_a_retina_display_answers_two(self):
        quartz = self.FakeDisplays([((0, 0, 1440, 900), (2880, 1800))])
        self.assertEqual(_backing_scale(quartz, 700, 400), 2.0)

    def test_a_second_display_is_asked_about_its_own_point(self):
        # The factor belongs to the display the window is on rather than to the
        # widest one in the list: a 1x display beside a Retina one is a real
        # arrangement, and taking the first display for it would rescale an
        # image nothing rescaled.
        quartz = self.FakeDisplays(
            [
                ((0, 0, 1440, 900), (2880, 1800)),
                ((-1280, 0, 1280, 1024), (1280, 1024)),
            ]
        )
        self.assertEqual(_backing_scale(quartz, 700, 400), 2.0)
        self.assertEqual(_backing_scale(quartz, -600, 400), 1.0)

    def test_a_point_on_no_display_falls_back_to_the_strict_size(self):
        quartz = self.FakeDisplays([((0, 0, 1280, 800), (1280, 800))])
        self.assertEqual(_backing_scale(quartz, 9000, 9000), 1.0)

    def test_a_machine_with_no_displays_falls_back_to_the_strict_size(self):
        self.assertEqual(_backing_scale(self.FakeDisplays([]), 10, 10), 1.0)

    def test_a_scale_of_one_accepts_only_the_window_itself(self):
        self.assertEqual(_accepted_sizes(320, 272, 1.0), {(320, 272)})

    def test_a_retina_scale_accepts_both_units(self):
        self.assertEqual(_accepted_sizes(320, 272, 2.0), {(320, 272), (640, 544)})

    def test_the_measured_shadow_padding_is_accepted_at_neither_scale(self):
        # The regression the whole guard exists for, and the reason this is a
        # tolerance rather than a dropped assertion: the live run measured `-l`
        # without `-o` as 432x384 against `geometry()`'s 320x272 for the key
        # window, and 388x340 for the inactive one before it.
        for scale in (1.0, 2.0):
            with self.subTest(scale=scale):
                accepted = _accepted_sizes(320, 272, scale)
                self.assertNotIn((432, 384), accepted)
                self.assertNotIn((388, 340), accepted)


@unittest.skipUnless(
    sys.platform == "darwin",
    "the real `screencapture` and a real window exist only on a Mac",
)
class TestLiveWindowCapture(unittest.TestCase):
    """The one thing a fake cannot check about `screencapture -l`: its output.

    A fake can pin the argv -- `TestScreenRecordingGate` does, `-o` included --
    but not what the tool does with it, and the live run that measured this
    found a bug a fake was happy to hide: without `-o` the image carries the
    drop shadow as padding, 68 pixels more than the window in each direction, so
    the image and `geometry()` disagree and a caller who measures a window before
    screenshotting it gets a picture of a different rectangle. The assertion
    below is the whole of that regression: the image has to be the window's own
    rectangle, and its size is the only thing here that can say so.

    The unit that rectangle is written in is `screencapture`'s choice rather
    than this package's, so the measurement accepts either unit it could be: the
    rectangle in points, which this live run measured, or the same rectangle in
    backing pixels, which is what a Retina display would write. No Retina display
    has been measured, so the second is a tolerance for an unmeasured case -- and
    the factor comes from the display itself (`_backing_scale`), never from
    `Screen.scale`. `TestTheWindowCaptureSizeTolerance` holds both halves of that
    decision, the shadow padding included, without needing a Mac.

    Skipped wherever there is no grant, which is every CI runner. The `macos` job
    grants nothing and installs no PyObjC before running the suite, so this
    reaches a real measurement on a development Mac and a skip everywhere else,
    which is the same arrangement `TestLiveQuartz` has.
    """

    def setUp(self):
        from pyguitest.backends import _macapi

        try:
            importlib.import_module("Quartz")
            importlib.import_module("ApplicationServices")
        except ImportError:
            self.skipTest("PyObjC is not installed, so there is no real capture here")
        if not _macapi.screen_recording_allowed():
            self.skipTest(
                "Screen Recording is not granted, so `screencapture -l` would "
                "write a black image rather than a measurement"
            )

    def test_the_image_is_the_rectangle_geometry_reports(self):
        quartz = importlib.import_module("Quartz")
        gui = pyguitest.connect(backend="macos")
        candidates = [w for w in gui.windows() if w.title and gui.geometry(w)[2] > 40]
        if not candidates:
            self.skipTest("no titled window on this Mac is wide enough to measure")
        window = candidates[0]
        left, top, width, height = gui.geometry(window)
        descriptor, path = tempfile.mkstemp(suffix=".png")
        os.close(descriptor)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))

        self.assertEqual(gui.screenshot(path, window=window), path)

        with open(path, "rb") as handle:
            header = handle.read(24)
        # A PNG signature, then IHDR: width and height are the two big-endian
        # ints at offset 16, which is all the decoder this needs.
        self.assertEqual(header[:8], b"\x89PNG\r\n\x1a\n")
        # The window's own rectangle, in whichever unit the tool wrote it in:
        # the window's centre is what decides which display's backing scale is
        # the one that could apply, and the shadow's padding belongs to neither
        # unit's answer.
        scale = _backing_scale(quartz, left + width // 2, top + height // 2)
        observed = struct.unpack(">II", header[16:24])
        self.assertIn(observed, _accepted_sizes(width, height, scale))


class TestRegistration(unittest.TestCase):
    """The registry entry, and the one behaviour `opt_in` exists for.

    The registry is process-global and other suites clear it, so this
    class registers what it is about into a registry of its own rather than
    trusting what it inherited -- which also makes it a test of `register` +
    `select` for this backend instead of a test of suite ordering.
    """

    def setUp(self):
        registry = mock.patch.object(backends, "_REGISTRY", [])
        registry.start()
        self.addCleanup(registry.stop)
        backends.register(
            backends._macquartz_factory, "macquartz", priority=70, opt_in=True
        )

    def test_macquartz_is_registered_by_name(self):
        self.assertIn("macquartz", backends.available())

    def test_it_is_opt_in_so_a_plain_connect_never_asks(self):
        quartz = FakeQuartz()
        with (
            mock.patch.dict(sys.modules, {"Quartz": quartz}),
            mock.patch(
                "pyguitest.backends._macapi.post_event_allowed", return_value=True
            ),
        ):
            selected = backends.select(darwin_environment())
            # Automatic composition never even constructs it, so the request is
            # never made: `opt_in`'s whole purpose, and worth asserting now
            # that the request is known to be silent -- a side effect that
            # leaves no window on screen is the one nobody would notice.
            self.assertNotIsInstance(selected, backends.MacquartzBackend)
            self.assertNotIn(
                "CGRequestPostEventAccess", [name for name, _ in quartz.calls]
            )
            # Naming it is the caller asking for the grant.
            named = backends.select(darwin_environment(), "macquartz")
            self.assertIsInstance(named, backends.MacquartzBackend)
        self.assertIn("CGRequestPostEventAccess", [name for name, _ in quartz.calls])


if __name__ == "__main__":
    unittest.main()
