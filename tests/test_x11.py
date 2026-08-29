"""X11 backend tests against a stand-in python-xlib.

The architectural point under test: X11 serves the tier-6 capabilities that no
Wayland compositor can. The tier scale is a Wayland ceiling, not an absolute
one, and the capability set is how a caller discovers the difference.
"""

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

from pyguitest.capabilities import Capability, Tier
from pyguitest.errors import (
    BackendUnavailable,
    CapabilityUnsupported,
    WindowNotFound,
)


class _FakeRoot:
    """Stands in for the root window's identity.

    Also carries send_event, so _NET_MOVERESIZE_WINDOW's ClientMessage can be
    captured and asserted on. Reset by FakeDisplay.__init__, since it is
    otherwise a module-level singleton shared across every test in this file.
    """

    def __init__(self):
        self.sent_events = []

    def send_event(self, event, event_mask=0):
        self.sent_events.append((event, event_mask))


_ROOT = _FakeRoot()


class FakeClientMessage:
    """Stands in for Xlib.protocol.event.ClientMessage: a plain data holder."""

    def __init__(self, window=None, client_type=None, data=None):
        self.window = window
        self.client_type = client_type
        self.data = data


class FakeWindow:
    def __init__(self, name="", children=(), geom=(0, 0, 100, 100), root_pos=None):
        self._name = name
        self._children = list(children)
        self._geom = geom
        # Absolute, root-relative position. Defaults to the geometry's own
        # x/y (no reparenting simulated); a test passes root_pos explicitly
        # to simulate a decorating WM where the two differ.
        self._root_pos = root_pos if root_pos is not None else geom[:2]
        self.configured = {}
        self.focused = False
        self.mapped = True
        # GetImage bookkeeping, so a test can assert the rectangle actually
        # asked for and simulate a window that has gone away.
        self.image_requests = []
        self.image_fails = False
        # _NET_CLIENT_LIST_STACKING support, on the root window only. None (the
        # default) means an EWMH-unaware WM -- windows() falls back to a
        # tree walk. A test sets this to the ids create_resource_object
        # should resolve, to exercise the EWMH path instead.
        self.client_list_ids = None

    def query_tree(self):
        return types.SimpleNamespace(children=self._children)

    def get_wm_name(self):
        return self._name

    def set_wm_name(self, name):
        self._name = name

    def get_geometry(self):
        x, y, w, h = self._geom
        return types.SimpleNamespace(x=x, y=y, width=w, height=h, root=_ROOT)

    def translate_coords(self, dst, x, y):
        rx, ry = self._root_pos
        return types.SimpleNamespace(x=rx + x, y=ry + y)

    def configure(self, **kw):
        self.configured.update(kw)

    def set_input_focus(self, revert, time):
        self.focused = True

    def query_pointer(self):
        return types.SimpleNamespace(root_x=640, root_y=480, mask=0x100)

    def get_full_property(self, atom, kind):
        if atom == "_NET_CLIENT_LIST_STACKING":
            # None means "unsupported", the fallback-to-tree-walk case;
            # a test opts into EWMH support by setting client_list_ids.
            if self.client_list_ids is None:
                return None
            return types.SimpleNamespace(value=self.client_list_ids)
        return types.SimpleNamespace(value=[4242])  # _NET_WM_PID

    def xtest_compare_cursor(self, cursor):
        return cursor == ("cursorfont", 68)  # XC_LEFT_PTR

    def unmap(self):
        self.mapped = False

    def map(self):
        self.mapped = True

    def get_attributes(self):
        # 2 == Xlib.X.IsViewable, 0 == IsUnmapped -- matches the real
        # values so a test could compare against the fake X module's
        # constants directly instead of the magic numbers.
        return types.SimpleNamespace(map_state=2 if self.mapped else 0)

    def get_image(self, x, y, width, height, format, plane_mask):
        """Stand in for GetImage: 32bpp BGRX, the usual x86 TrueColor case.

        Each pixel encodes its own position so a test can tell a correctly
        decoded, correctly ordered image from a plausible-looking wrong one:
        red carries the column, green the row, blue a constant.
        """
        self.image_requests.append((x, y, width, height))
        if self.image_fails:
            raise RuntimeError("BadDrawable")
        buffer = bytearray()
        for row in range(height):
            for column in range(width):
                red = (x + column) % 256
                green = (y + row) % 256
                buffer += bytes([0x40, green, red, 0])  # B, G, R, unused
        return types.SimpleNamespace(data=bytes(buffer))


class FakeDisplay:
    def __init__(self, *a, **kw):
        _ROOT.sent_events = []
        self.editor = FakeWindow("Editor", geom=(0, 0, 800, 600))
        self.browser = FakeWindow("Browser", geom=(800, 0, 400, 600))
        # Sized to match screen() deliberately: a root whose live
        # geometry disagrees with the connection-setup screen record is
        # the *bug* case, exercised explicitly in TestCapture.
        self.root = FakeWindow("", [self.editor, self.browser], geom=(0, 0, 1920, 1080))
        self._by_id = {100: self.editor, 101: self.browser}
        self.synced = 0
        self.events = []
        self.closed = False
        self.image_byte_order = 0

    def query_extension(self, name):
        return True

    def screen_count(self):
        return 1

    # A 32-bit TrueColor visual with the usual x86 channel masks, plus a
    # decoy visual before it so a backend that grabs the first one it sees
    # rather than matching root_visual is caught.
    _VISUAL = types.SimpleNamespace(
        visual_id=0x21, red_mask=0xFF0000, green_mask=0xFF00, blue_mask=0xFF
    )
    _DECOY = types.SimpleNamespace(
        visual_id=0x99, red_mask=0xFF, green_mask=0xFF00, blue_mask=0xFF0000
    )

    def screen(self, n=0):
        return types.SimpleNamespace(
            width_in_pixels=1920,
            height_in_pixels=1080,
            root=self.root,
            root_visual=self._VISUAL.visual_id,
            allowed_depths=[
                types.SimpleNamespace(depth=24, visuals=[self._DECOY, self._VISUAL])
            ],
        )

    @property
    def display(self):
        """python-xlib's protocol-level display, for image_byte_order.

        0 is LSBFirst, the x86 answer; a test sets image_byte_order to 1
        (MSBFirst) to check the other branch.
        """
        return types.SimpleNamespace(
            info=types.SimpleNamespace(image_byte_order=self.image_byte_order)
        )

    def sync(self):
        self.synced += 1

    def close(self):
        self.closed = True

    # A small, deliberately asymmetric keymap: keycode 38 carries both
    # levels of 'a'/'A' (an ordinary shifted key), the rest are single-level.
    # Keysyms outside this table have no keycode, exercising the "unmapped"
    # rejection path.
    _KEYCODE_LEVELS = {
        38: [0x61, 0x41],  # a, A
        56: [0x62, 0x42],  # b, B
        50: [0xFFE1],  # Shift_L
        36: [0xFF0D],  # Return
        23: [0xFF09],  # Tab
    }
    _KEYSYM_TO_KEYCODE = {
        keysym: keycode
        for keycode, levels in _KEYCODE_LEVELS.items()
        for keysym in levels
    }

    def keysym_to_keycode(self, keysym):
        return self._KEYSYM_TO_KEYCODE.get(keysym, 0)

    def get_keyboard_mapping(self, first_keycode, count):
        return [self._KEYCODE_LEVELS.get(first_keycode + i, []) for i in range(count)]

    def intern_atom(self, name):
        # Identity is enough here: the fake doesn't need real X atom
        # numbers, only something get_full_property can switch on.
        return name

    def create_resource_object(self, cls, wid):
        return self._by_id[wid]

    def query_keymap(self):
        keymap = bytearray(32)
        keymap[38 // 8] |= 1 << (38 % 8)  # keycode 38 held down
        return bytes(keymap)

    def get_input_focus(self):
        return types.SimpleNamespace(focus=self.editor)

    def xtest_fake_input(self, event_type, detail=0, **kw):
        self.events.append((event_type, detail, kw))

    def open_font(self, name):
        return types.SimpleNamespace(
            create_glyph_cursor=lambda mask, src, msk, fg, bg: (name + "font", src)
        )


# Keysym names that are not single printable characters, and so are not
# covered by the "keysym equals codepoint" rule below.
_NAMED_KEYSYMS = {
    "Shift_L": 0xFFE1,
    "Return": 0xFF0D,
    "Tab": 0xFF09,
    "Escape": 0xFF1B,
    "BackSpace": 0xFF08,
}


def _fake_string_to_keysym(name):
    """Stand-in for Xlib.XK.string_to_keysym.

    Named keysyms come from the table above; a single printable Latin-1
    character resolves to its own codepoint, matching real X11 semantics for
    that range. Anything else -- notably an empty string, and any character
    string_to_keysym would not actually recognise -- is 0 (unknown), the
    signal press_key and type_text both check for.
    """
    if name in _NAMED_KEYSYMS:
        return _NAMED_KEYSYMS[name]
    if len(name) == 1 and 0x20 <= ord(name) <= 0xFF:
        return ord(name)
    return 0


def install_fake_xlib():
    xlib = types.ModuleType("Xlib")
    X = types.ModuleType("Xlib.X")
    for i, name in enumerate(
        [
            "MotionNotify",
            "ButtonPress",
            "ButtonRelease",
            "KeyPress",
            "KeyRelease",
            "Above",
            "Below",
            "RevertToParent",
            "CurrentTime",
            "AnyPropertyType",
        ]
    ):
        setattr(X, name, i + 1)
    # Real values, not sequential like the above -- IsUnmapped=0 is
    # otherwise indistinguishable from a stub returning 0 by omission.
    X.IsUnmapped, X.IsUnviewable, X.IsViewable = 0, 1, 2
    # Also real: matches include/uapi and the X protocol spec, in case a
    # test ever needs to tell these apart from the sequential ones above.
    X.SubstructureNotifyMask, X.SubstructureRedirectMask = 1 << 19, 1 << 20
    # Real GetImage format values: XYBitmap=0, XYPixmap=1, ZPixmap=2.
    X.XYBitmap, X.XYPixmap, X.ZPixmap = 0, 1, 2
    XK = types.ModuleType("Xlib.XK")
    XK.string_to_keysym = _fake_string_to_keysym
    display = types.ModuleType("Xlib.display")
    display.Display = FakeDisplay
    ext = types.ModuleType("Xlib.ext")
    xtest = types.ModuleType("Xlib.ext.xtest")
    protocol = types.ModuleType("Xlib.protocol")
    event = types.ModuleType("Xlib.protocol.event")
    event.ClientMessage = FakeClientMessage

    xtest.fake_input = lambda disp, et, detail=0, **kw: disp.xtest_fake_input(
        et, detail, **kw
    )
    xlib.X, xlib.XK, xlib.display, xlib.ext, xlib.protocol = (
        X,
        XK,
        display,
        ext,
        protocol,
    )
    ext.xtest = xtest
    protocol.event = event
    return mock.patch.dict(
        sys.modules,
        {
            "Xlib": xlib,
            "Xlib.X": X,
            "Xlib.XK": XK,
            "Xlib.display": display,
            "Xlib.ext": ext,
            "Xlib.ext.xtest": xtest,
            "Xlib.protocol": protocol,
            "Xlib.protocol.event": event,
        },
    )


class X11TestCase(unittest.TestCase):
    def setUp(self):
        patcher = install_fake_xlib()
        patcher.start()
        self.addCleanup(patcher.stop)
        from pyguitest.backends import x11

        self.module = x11
        self.gui = x11.X11Backend()


class TestTierSixOnX11(X11TestCase):
    def test_x11_serves_capabilities_no_compositor_can(self):
        # The whole reason the tier is documented as a Wayland ceiling.
        for cap in (
            Capability.POINTER_QUERY,
            Capability.INPUT_STATE_QUERY,
            Capability.WINDOW_TITLE_SET,
            Capability.WINDOW_LOWER,
            Capability.WINDOW_CURSOR_QUERY,
        ):
            with self.subTest(cap=cap.name):
                self.assertEqual(cap.tier, Tier.NO_PATH)
                self.assertIn(cap, self.gui.capabilities)

    def test_cursor_query_is_served_too(self):
        # python-xlib does bind XTestCompareCursorWithWindow, as
        # window.xtest_compare_cursor -- so all five tier-6 caps are available.
        self.assertIn(Capability.WINDOW_CURSOR_QUERY, self.gui.capabilities)

    def test_cursor_shape_is_compared_against_the_cursor_font(self):
        window = self.gui.windows()[0]
        self.assertTrue(self.gui.is_window_cursor(window, 68))  # XC_LEFT_PTR
        self.assertFalse(self.gui.is_window_cursor(window, 152))  # XC_XTERM

    def test_cursor_is_cached_per_shape_rather_than_rebuilt_each_call(self):
        # Regression: every call built a brand new server-side cursor object
        # via create_glyph_cursor and freed none of them.
        first = self.gui._font_cursor(68)
        second = self.gui._font_cursor(68)
        self.assertIs(first, second)

    def test_pointer_position_reads_back(self):
        self.assertEqual(self.gui.pointer_position(), (640, 480))

    def test_key_state_reads_the_keymap_vector(self):
        self.assertTrue(self.gui.is_key_pressed("a"))

    def test_title_can_be_rewritten(self):
        window = self.gui.windows()[0]
        self.gui.set_window_title(window, "Renamed")
        self.assertEqual(window.handle.get_wm_name(), "Renamed")

    def test_lower_restacks_below(self):
        window = self.gui.windows()[0]
        self.gui.lower_window(window)
        self.assertIn("stack_mode", window.handle.configured)


class TestActiveWindow(X11TestCase):
    def test_returns_the_focused_window(self):
        self.assertEqual(self.gui.active_window().title, "Editor")

    def test_returns_none_for_pointer_root_rather_than_a_bogus_window(self):
        # get_input_focus().focus can be the PointerRoot or None X
        # constants -- plain integers, not window objects -- when nothing
        # has taken focus explicitly. Wrapping either as a Window used to
        # silently produce a Window('') with no indication anything was
        # wrong: _title()'s bare except swallowed the resulting error.
        PointerRoot = 1
        self.gui._display.get_input_focus = lambda: types.SimpleNamespace(
            focus=PointerRoot
        )
        self.assertIsNone(self.gui.active_window())


class TestWindows(X11TestCase):
    def test_only_named_windows_are_listed(self):
        # The unnamed root is walked but not reported.
        self.assertEqual([w.title for w in self.gui.windows()], ["Editor", "Browser"])

    def test_pid_comes_from_net_wm_pid(self):
        self.assertEqual(self.gui.windows()[0].pid, 4242)

    def test_geometry(self):
        self.assertEqual(self.gui.geometry(self.gui.windows()[1]), (800, 0, 400, 600))

    def test_geometry_is_translated_to_root_not_the_parent(self):
        # get_geometry() alone would report (10, 10) here -- the offset
        # inside the decoration frame a reparenting WM inserted. geometry()
        # must translate that to the window's real screen position.
        reparented = FakeWindow(
            "Decorated", geom=(10, 10, 300, 200), root_pos=(500, 400)
        )
        self.assertEqual(self.gui.geometry(reparented), (500, 400, 300, 200))

    def test_hit_test(self):
        self.assertEqual(self.gui.window_at(50, 50).title, "Editor")
        self.assertEqual(self.gui.window_at(900, 50).title, "Browser")
        self.assertIsNone(self.gui.window_at(5000, 5000))

    def test_move_sends_net_moveresize_window_not_a_raw_configure(self):
        # Regression: a raw ConfigureWindow request sets position relative
        # to whatever the window manager reparented the client under, not
        # the screen -- the same mismatch geometry() already has to correct
        # for. move_window must go through the EWMH message instead, which
        # window managers are required to honour in screen coordinates.
        window = self.gui.windows()[0]
        self.gui.move_window(window, 50, 60)

        sent = _ROOT.sent_events
        self.assertEqual(len(sent), 1)
        message, mask = sent[0]
        self.assertEqual(message.window, window.handle)
        self.assertEqual(message.client_type, "_NET_MOVERESIZE_WINDOW")
        fmt, values = message.data
        self.assertEqual(fmt, 32)
        flags, x, y, width, height = values
        self.assertEqual((x, y), (50, 60))
        # Only x and y were requested -- width/height's presence bits (10, 11)
        # must stay clear so the window manager leaves the size alone.
        self.assertTrue(flags & (1 << 8))  # x present
        self.assertTrue(flags & (1 << 9))  # y present
        self.assertFalse(flags & (1 << 10))  # width absent
        self.assertFalse(flags & (1 << 11))  # height absent
        self.assertEqual(
            mask,
            self.gui._X.SubstructureRedirectMask | self.gui._X.SubstructureNotifyMask,
        )

    def test_resize_sets_only_the_size_presence_bits(self):
        window = self.gui.windows()[0]
        self.gui.resize_window(window, 500, 400)

        message, _mask = _ROOT.sent_events[0]
        flags, x, y, width, height = message.data[1]
        self.assertEqual((width, height), (500, 400))
        self.assertFalse(flags & (1 << 8))  # x absent
        self.assertFalse(flags & (1 << 9))  # y absent
        self.assertTrue(flags & (1 << 10))  # width present
        self.assertTrue(flags & (1 << 11))  # height present

    def test_moveresize_uses_static_gravity(self):
        # StaticGravity (10) is what lets the request position the window's
        # own corner exactly, without the window manager needing to know
        # the size of its own decorations.
        window = self.gui.windows()[0]
        self.gui.move_window(window, 0, 0)
        flags = _ROOT.sent_events[0][0].data[1][0]
        self.assertEqual(flags & 0xFF, 10)

    def test_net_client_list_is_used_when_the_wm_maintains_it(self):
        # Regression: the tree walk sees a WM's decoration frames and any
        # client sub-windows alongside the toplevel, so one application can
        # be reported two or three times. _NET_CLIENT_LIST_STACKING is exactly the
        # toplevel set an EWMH window manager maintains -- no duplicates.
        self.gui._display.root.client_list_ids = [100, 101]
        self.assertEqual([w.title for w in self.gui.windows()], ["Editor", "Browser"])

    def test_uses_the_stacking_property_not_the_unordered_one(self):
        # Regression: _NET_CLIENT_LIST (no ordering guarantee) is easy to
        # reach for instead of _NET_CLIENT_LIST_STACKING (guaranteed
        # bottom-to-top). window_at()'s "last match wins" hit-test depends
        # on that ordering; the wrong property would make it pick an
        # arbitrary overlapping window instead of the topmost one.
        root = self.gui._display.root
        original_get_full_property = root.get_full_property

        def strict_get_full_property(atom, kind):
            assert atom != "_NET_CLIENT_LIST", (
                "must query _NET_CLIENT_LIST_STACKING, not the "
                "unordered _NET_CLIENT_LIST"
            )
            return original_get_full_property(atom, kind)

        root.get_full_property = strict_get_full_property
        root.client_list_ids = [100, 101]
        self.gui.windows()  # raises via the assert above if this regresses

    def test_hit_test_honours_stacking_order_via_net_client_list(self):
        # Two overlapping windows: stacking order alone decides which one
        # window_at() reports, since the tree-walk fallback isn't involved.
        overlap_a = FakeWindow("Back", geom=(0, 0, 200, 200))
        overlap_b = FakeWindow("Front", geom=(0, 0, 200, 200))
        self.gui._display._by_id[200] = overlap_a
        self.gui._display._by_id[201] = overlap_b
        # Listed back-to-front: "Front" is last, i.e. topmost.
        self.gui._display.root.client_list_ids = [200, 201]
        self.assertEqual(self.gui.window_at(100, 100).title, "Front")

    def test_falls_back_to_the_tree_walk_without_net_client_list(self):
        # client_list_ids defaults to None -- an EWMH-unaware WM.
        self.assertIsNone(self.gui._display.root.client_list_ids)
        self.assertEqual([w.title for w in self.gui.windows()], ["Editor", "Browser"])

    def test_tree_walk_skips_a_window_destroyed_mid_scan(self):
        # query_tree() on a destroyed window raises BadWindow; the walk must
        # skip that branch rather than aborting the whole scan.
        class Destroyed:
            def query_tree(self):
                raise Exception("BadWindow")

            def get_wm_name(self):
                raise Exception("BadWindow")

        gone = Destroyed()
        self.gui._display.root._children.append(gone)
        titles = [w.title for w in self.gui.windows()]
        self.assertEqual(titles, ["Editor", "Browser"])

    def test_minimize_unmaps(self):
        window = self.gui.windows()[0]
        self.gui.minimize_window(window)
        self.assertFalse(window.handle.mapped)
        self.gui.minimize_window(window, minimized=False)
        self.assertTrue(window.handle.mapped)

    def test_is_window_viewable_reads_map_state(self):
        window = self.gui.windows()[0]
        self.assertTrue(self.gui.is_window_viewable(window))
        self.gui.minimize_window(window)
        self.assertFalse(self.gui.is_window_viewable(window))

    def test_is_window_viewable_for_a_destroyed_window(self):
        class Destroyed:
            def get_attributes(self):
                raise Exception("BadWindow")

        with self.assertRaises(WindowNotFound):
            self.gui.is_window_viewable(Destroyed())


class TestInput(X11TestCase):
    def test_typing_resolves_each_character_through_the_server_keymap(self):
        # Keymap-correct by construction -- what raw uinput cannot do.
        self.gui.type_text("ab")
        presses = list(self.gui._display.events)
        self.assertEqual(len(presses), 4)  # two chars, press + release each

    def test_capital_letters_are_shifted(self):
        # "Hello" typed lowercase was the bug: no shift meant every capital
        # arrived as its unshifted (lowercase) neighbour.
        self.gui.type_text("aB")
        events = list(self.gui._display.events)
        self.assertEqual(len(events), 6)
        keycodes = [detail for _etype, detail, _kw in events]
        shift, b = self.gui._keycode("Shift_L"), self.gui._keycode("B")
        # a is unshifted; B is bracketed by a Shift_L press and release around
        # its own keycode, rather than shift being held for the whole string.
        self.assertEqual(keycodes[:2], [self.gui._keycode("a")] * 2)
        self.assertEqual(keycodes[2:], [shift, b, b, shift])
        shift_presses = [etype for etype, detail, _kw in events if detail == shift]
        self.assertEqual(shift_presses, [self.gui._X.KeyPress, self.gui._X.KeyRelease])

    def test_control_characters_resolve_by_name_not_codepoint(self):
        # string_to_keysym("\n") finds no keysym named "\n" and returns
        # NoSymbol; the fix routes control characters through their proper
        # keysym name ("Return") before falling through to that lookup.
        self.assertEqual(self.gui._char_keysym("\n"), 0xFF0D)
        self.gui.type_text("\n")
        keycodes = [detail for _etype, detail, _kw in self.gui._display.events]
        self.assertEqual(keycodes, [self.gui._keycode("Return")] * 2)

    def test_non_latin1_character_uses_the_unicode_keysym_form(self):
        # Codepoints above U+00FF need 0x01000000 | codepoint; the bare
        # ordinal (what `ord(char)` alone produces) is not a valid keysym.
        codepoint = ord("€")
        self.assertGreater(codepoint, 0xFF)
        self.assertEqual(self.gui._char_keysym("€"), 0x01000000 | codepoint)

    def test_latin1_character_uses_its_codepoint_directly(self):
        self.assertEqual(self.gui._char_keysym("a"), ord("a"))

    def test_unmapped_keysym_raises_rather_than_pressing_keycode_zero(self):
        # 'z' has a keysym (0x7a) but no entry in the fake keymap, so
        # keysym_to_keycode returns 0 -- press_key already refuses this;
        # type_text must too, rather than silently pressing keycode 0.
        from pyguitest.errors import CapabilityUnsupported

        with self.assertRaises(CapabilityUnsupported):
            self.gui.type_text("z")

    def test_scroll_uses_buttons_four_and_five(self):
        self.gui.scroll(dy=1)
        details = [e[1] for e in self.gui._display.events]
        self.assertIn(4, details)

    def test_scroll_honours_magnitude_on_both_axes(self):
        dx, dy = 2, 3
        self.gui.scroll(dx=dx, dy=dy)
        details = [e[1] for e in self.gui._display.events]
        # Each step is a press + release pair sharing the button's detail, so
        # the count is double the requested magnitude on each axis.
        self.assertEqual(details.count(4), 2 * dy)
        self.assertEqual(details.count(7), 2 * dx)
        self.assertNotIn(5, details)  # no downward clicks for a dy>0 request
        self.assertNotIn(6, details)  # no leftward clicks for a dx>0 request

    def test_pure_horizontal_scroll_touches_no_vertical_button(self):
        self.gui.scroll(dx=-1)
        details = [e[1] for e in self.gui._display.events]
        self.assertTrue(details and set(details) == {6})  # left button only
        self.assertNotIn(4, details)
        self.assertNotIn(5, details)

    def test_unknown_key_name_is_rejected(self):
        with self.assertRaises(ValueError):
            self.gui.press_key("")


class TestLifecycle(X11TestCase):
    def test_close_closes_the_display(self):
        self.gui.close()
        self.assertTrue(self.gui._display.closed)


class TestUnavailable(unittest.TestCase):
    def test_missing_python_xlib_refuses_with_an_install_hint(self):
        from pyguitest.backends import x11

        with mock.patch.object(x11, "_xlib", return_value=None):
            self.assertFalse(x11.available())
            with self.assertRaises(BackendUnavailable) as ctx:
                x11.X11Backend()
            self.assertIn("pyguitest[x11]", str(ctx.exception))

    def test_display_is_closed_when_xtest_is_missing(self):
        # Regression: the display connection was left open when the XTEST
        # check failed -- the one branch after a successful Display() call
        # that raised without calling close().
        patcher = install_fake_xlib()
        patcher.start()
        self.addCleanup(patcher.stop)
        from pyguitest.backends import x11

        created = []
        real_init = FakeDisplay.__init__

        def recording_init(self, *a, **kw):
            real_init(self, *a, **kw)
            created.append(self)

        with mock.patch.object(FakeDisplay, "__init__", recording_init):
            with mock.patch.object(FakeDisplay, "query_extension", return_value=False):
                with self.assertRaises(BackendUnavailable):
                    x11.X11Backend()

        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].closed)


if __name__ == "__main__":
    unittest.main()


class TestCapture(X11TestCase):
    """The only capture path that needs neither a tool nor an image library.

    X11 is also the only backend that can capture a *window* rather than a
    rectangle of screen: GetImage takes any drawable, so pointing it at the
    window asks the server for that window's own pixels instead of whatever
    is stacked over those coordinates.
    """

    def _decode(self, path):
        """Read back the PNG this wrote, as (width, height, rows)."""
        import struct
        import zlib

        with open(path, "rb") as handle:
            data = handle.read()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        offset, found = 8, {}
        while offset < len(data):
            (length,) = struct.unpack(">I", data[offset : offset + 4])
            kind = data[offset + 4 : offset + 8]
            found[kind] = data[offset + 8 : offset + 8 + length]
            offset += 12 + length
        width, height = struct.unpack(">II", found[b"IHDR"][:8])
        raw = zlib.decompress(found[b"IDAT"])
        stride = width * 3 + 1
        rows = [raw[i * stride + 1 : (i + 1) * stride] for i in range(height)]
        return width, height, rows

    def _capture(self, **kwargs):
        path = self.gui.capture(**kwargs)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path

    def test_both_capture_capabilities_are_declared(self):
        self.assertIn(Capability.SCREEN_CAPTURE, self.gui.capabilities)
        self.assertIn(Capability.WINDOW_CAPTURE, self.gui.capabilities)

    def test_whole_screen_uses_the_screen_size(self):
        width, height, _ = self._decode(self._capture())
        self.assertEqual((width, height), (1920, 1080))

    def test_a_region_captures_exactly_that_rectangle(self):
        width, height, rows = self._decode(self._capture(region=(10, 20, 4, 3)))
        self.assertEqual((width, height), (4, 3))
        # The fake encodes the source coordinate into each pixel, so the
        # offset is checked, not just the size: the top-left pixel must be
        # the one at (10, 20), not the one at (0, 0).
        self.assertEqual(rows[0][0:3], bytes([10, 20, 0x40]))
        self.assertEqual(rows[2][9:12], bytes([13, 22, 0x40]))

    def test_a_window_reads_the_window_drawable_not_the_screen(self):
        window = self.gui.windows()[0]
        path = self._capture(window=window)
        width, height, _ = self._decode(path)
        # The editor is 800x600 and the request goes to the window itself
        # at its own origin -- not to the root at the window's screen
        # position, which is what cropping a full-screen shot would do.
        self.assertEqual((width, height), (800, 600))
        self.assertEqual(self.gui._display.editor.image_requests[0][:2], (0, 0))
        self.assertEqual(self.gui._display.root.image_requests, [])

    def test_window_and_region_together_are_refused(self):
        window = self.gui.windows()[0]
        with self.assertRaises(ValueError):
            self.gui.capture(window=window, region=(0, 0, 5, 5))

    def test_a_window_that_has_gone_away_raises_window_not_found(self):
        window = self.gui.windows()[0]
        self.gui._display.editor.image_fails = True
        with self.assertRaises(WindowNotFound):
            self.gui.capture(window=window)

    def test_a_named_path_is_honoured_and_returned(self):
        descriptor, path = tempfile.mkstemp(suffix=".png")
        os.close(descriptor)
        self.addCleanup(os.unlink, path)
        self.assertEqual(self.gui.capture(path=path, region=(0, 0, 2, 2)), path)
        self.assertEqual(self._decode(path)[0], 2)

    def test_the_read_is_banded_so_one_request_cannot_overflow_the_reply(self):
        # A 4K frame is ~33MB, well past a typical X server's maximum reply
        # length; python-xlib raises rather than splitting, so the read has
        # to be banded here.
        self._capture(region=(0, 0, 8, 200))
        requests = self.gui._display.root.image_requests
        self.assertGreater(len(requests), 1)
        self.assertEqual(sum(r[3] for r in requests), 200)
        self.assertEqual([r[1] for r in requests][:2], [0, 64])

    def test_channel_masks_come_from_the_root_visual_not_the_first_one(self):
        # The fake advertises a decoy visual with red and blue swapped
        # ahead of the real one. Taking the first visual on the depth would
        # produce an image that looks fine and has its colours reversed.
        _, _, rows = self._decode(self._capture(region=(1, 0, 1, 1)))
        self.assertEqual(rows[0], bytes([1, 0, 0x40]))

    def test_msb_first_servers_decode_the_other_way_round(self):
        # image_byte_order is read from the connection rather than assumed,
        # because a big-endian server sends the same pixel reversed.
        self.gui._display.image_byte_order = 1
        _, _, rows = self._decode(self._capture(region=(5, 7, 1, 1)))
        # Same four bytes (0x40, green, red, 0), read most-significant
        # first: the channels land in different bits entirely.
        self.assertEqual(rows[0], bytes([7, 5, 0]))

    def test_the_fast_and_general_decoders_agree(self):
        # The whole risk of a special case: it is fast and it is wrong, and
        # nothing notices because the general path never runs on the
        # machine that would catch it. Same buffer through both.
        data = self.gui._display.root.get_image(3, 4, 6, 5, 2, 0xFFFFFFFF).data
        fast = self.gui._to_rgb_rows(data, 6, 5)
        with mock.patch.object(self.module.X11Backend, "_DIRECT_MASKS", (0, 0, 0)):
            general = self.gui._to_rgb_rows(data, 6, 5)
        self.assertEqual(fast, general)
        self.assertEqual(fast[0][0:3], bytes([3, 4, 0x40]))

    def test_a_narrow_channel_is_scaled_up_to_a_full_byte(self):
        # A 16-bit 5-6-5 visual: 5-bit red at full scale must decode to 255,
        # not 248, or every screenshot on such a server comes back dim.
        screen = self.gui._display.screen()
        visual = types.SimpleNamespace(
            visual_id=screen.root_visual,
            red_mask=0xF800,
            green_mask=0x07E0,
            blue_mask=0x001F,
        )
        self.gui._display.screen = lambda n=0: types.SimpleNamespace(
            width_in_pixels=32,
            height_in_pixels=32,
            root=self.gui._display.root,
            root_visual=visual.visual_id,
            allowed_depths=[types.SimpleNamespace(depth=16, visuals=[visual])],
        )
        # One white pixel, 16 bits: all channels at maximum.
        rows = self.gui._to_rgb_rows(b"\xff\xff", 1, 1)
        self.assertEqual(rows[0], bytes([255, 255, 255]))

    def test_an_unsupported_pixel_size_is_refused(self):
        with self.assertRaises(CapabilityUnsupported):
            self.gui._to_rgb_rows(b"\x00" * 8, 1, 1)  # 8 bytes per pixel

    def test_a_visual_the_screen_never_advertised_is_refused(self):
        self.gui._display.screen = lambda n=0: types.SimpleNamespace(
            width_in_pixels=32,
            height_in_pixels=32,
            root=self.gui._display.root,
            root_visual=0xDEAD,
            allowed_depths=[],
        )
        with self.assertRaises(CapabilityUnsupported):
            self.gui.capture(region=(0, 0, 2, 2))

    def test_the_live_root_size_wins_over_the_connection_setup_record(self):
        # The bug this pins, hit the first time X11 capture ran against a
        # real server: screen.width_in_pixels comes from the connection
        # *setup*, which the server sends once and never revises, so after
        # a RandR resize it reports the size the screen used to be. In a
        # VM whose guest resolution follows the host window that is stale
        # immediately -- and asking GetImage for the old, larger rectangle
        # is BadMatch, not a soft failure.
        self.gui._display.root._geom = (0, 0, 1280, 720)
        width, height, _ = self._decode(self._capture())
        self.assertEqual((width, height), (1280, 720))
        # And the read was actually bounded by it.
        self.assertEqual(
            max(r[0] + r[2] for r in self.gui._display.root.image_requests), 1280
        )

    def test_a_region_outside_the_screen_says_so_instead_of_badmatch(self):
        # X answers BadMatch and names none of the numbers involved, so it
        # cannot tell you which side was wrong.
        with self.assertRaises(CapabilityUnsupported) as caught:
            self.gui.capture(region=(1900, 0, 200, 100))
        message = str(caught.exception)
        self.assertIn("200x100+1900+0", message)
        self.assertIn("1920x1080", message)
        self.assertEqual(self.gui._display.root.image_requests, [])

    def test_a_negative_origin_is_refused_too(self):
        with self.assertRaises(CapabilityUnsupported):
            self.gui.capture(region=(-10, 0, 100, 100))

    def test_a_region_exactly_filling_the_screen_is_allowed(self):
        # The boundary must not be off by one.
        width, height, _ = self._decode(self._capture(region=(0, 0, 1920, 1080)))
        self.assertEqual((width, height), (1920, 1080))

    def test_an_unreadable_root_is_not_reported_as_a_missing_window(self):
        # WindowNotFound would be actively misleading with no window in play.
        def boom():
            raise RuntimeError("connection reset")

        self.gui._display.root.get_geometry = boom
        with self.assertRaises(CapabilityUnsupported):
            self.gui.capture()

    def test_screen_capture_is_withdrawn_under_xwayland(self):
        # The trap tools.py already encodes as `x11_only`: the X connection
        # works, and cannot see the Wayland session around it. Native
        # Wayland surfaces are never composited into the X root window, so
        # a root GetImage cannot return the desktop -- seen live on GNOME
        # 50 as BadMatch, but success would have been worse, since an empty
        # X root is a valid image of entirely the wrong thing.
        from pyguitest.session import SessionType

        gui = self.module.X11Backend(
            environment=types.SimpleNamespace(session_type=SessionType.XWAYLAND)
        )
        self.assertNotIn(Capability.SCREEN_CAPTURE, gui.capabilities)

    def test_window_capture_survives_under_xwayland(self):
        # An XWayland-backed X11 client does have its own drawable with its
        # own content, and the composite only ever hands this backend
        # windows it issued itself.
        from pyguitest.session import SessionType

        gui = self.module.X11Backend(
            environment=types.SimpleNamespace(session_type=SessionType.XWAYLAND)
        )
        self.assertIn(Capability.WINDOW_CAPTURE, gui.capabilities)
        # And the rest of the backend is untouched.
        self.assertIn(Capability.POINTER_MOVE, gui.capabilities)
        self.assertIn(Capability.WINDOW_CURSOR_QUERY, gui.capabilities)

    def test_a_real_x_session_keeps_screen_capture(self):
        from pyguitest.session import SessionType

        gui = self.module.X11Backend(
            environment=types.SimpleNamespace(session_type=SessionType.X11)
        )
        self.assertIn(Capability.SCREEN_CAPTURE, gui.capabilities)

    def test_no_environment_is_assumed_to_be_a_real_x_server(self):
        # A directly-constructed backend has always assumed this.
        self.assertIn(Capability.SCREEN_CAPTURE, self.gui.capabilities)

    def test_screens_report_the_live_root_size_not_the_setup_record(self):
        # Same staleness that cost a BadMatch in capture(), but quieter and
        # worse: a caller centring a click on these numbers gets no error,
        # just a click in the wrong place.
        self.gui._display.root._geom = (0, 0, 1280, 720)
        screen = self.gui.screens()[0]
        self.assertEqual((screen.width, screen.height), (1280, 720))

    def test_screens_fall_back_to_the_setup_size_if_the_root_is_unreadable(self):
        # A stale size still beats no answer for a purely informational call.
        def boom():
            raise RuntimeError("connection reset")

        self.gui._display.root.get_geometry = boom
        screen = self.gui.screens()[0]
        self.assertEqual((screen.width, screen.height), (1920, 1080))

    def test_a_window_capture_does_not_require_screen_capture(self):
        # The live failure: under XWayland this backend keeps
        # WINDOW_CAPTURE and drops SCREEN_CAPTURE, and capture(window=...)
        # was refused with "SCREEN_CAPTURE is unsupported on x11" -- a
        # capability the per-window path never uses. Reproduced by
        # capturing with SCREEN_CAPTURE withdrawn exactly as XWayland does.
        from pyguitest.session import SessionType

        self.gui.environment = types.SimpleNamespace(session_type=SessionType.XWAYLAND)
        self.assertNotIn(Capability.SCREEN_CAPTURE, self.gui.capabilities)

        window = self.gui.windows()[0]
        path = self.gui.capture(window=window)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        width, height, _ = self._decode(path)
        self.assertEqual((width, height), (800, 600))

    def test_a_whole_screen_capture_still_requires_screen_capture(self):
        from pyguitest.session import SessionType

        self.gui.environment = types.SimpleNamespace(session_type=SessionType.XWAYLAND)
        with self.assertRaises(CapabilityUnsupported) as caught:
            self.gui.capture()
        self.assertIn("SCREEN_CAPTURE", str(caught.exception))

    def test_a_region_capture_also_requires_screen_capture(self):
        from pyguitest.session import SessionType

        self.gui.environment = types.SimpleNamespace(session_type=SessionType.XWAYLAND)
        with self.assertRaises(CapabilityUnsupported):
            self.gui.capture(region=(0, 0, 10, 10))
