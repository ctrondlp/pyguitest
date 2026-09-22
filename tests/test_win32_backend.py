"""The Windows backend against fakes: no Windows machine, no DLLs loaded.

What is worth testing from Linux is everything the platform's own documentation
specifies -- the flags and union member of a message, the decisions a window
filter makes, the rectangle a blit is taken from, the retry a held clipboard
gets -- and all of it is reachable by replacing `_winapi`'s four accessors with
objects that record what they were asked. The structures' layout is pinned
separately in `tests/test_win32.py`, so a mistake in a declaration and a mistake
in the backend are two different failures rather than one.

The fakes are deliberately literal: a monitor is a rectangle and a DPI, a window
is a dict of the fields the filter reads, and the global heap really allocates,
so a clipboard round trip here reads back the bytes it wrote.
"""

import ctypes
import os
import struct
import tempfile
import threading
import types
import unittest
import zlib
from unittest import mock

from pyguitest.backends import _winapi
from pyguitest.backends.base import Window
from pyguitest.backends.win32 import (
    _CURSOR_SHAPES,
    _DIAGONAL_SHAPES,
    _WINDOW_EVENT_RANGES,
    VK,
    Win32Backend,
    _absolute_input,
)
from pyguitest.capabilities import Capability
from pyguitest.errors import (
    BackendUnavailable,
    CapabilityUnsupported,
    PermissionRequired,
    PyGUITestError,
    WindowNotFound,
)


class FakeLibrary:
    """A DLL stand-in where every attribute is a recorded callable.

    Behaviour is per name: a value is returned as it is, a callable is called
    with the same arguments -- which is how the fakes that write through an
    out-parameter (`GetWindowRect`) or call back into the caller (`EnumWindows`)
    are built. Anything unconfigured returns 0, which is what a Windows API
    returns when it fails, so a test that forgets to set something sees a
    refusal rather than an unrelated exception.
    """

    def __init__(self, **behaviour):
        self._behaviour = behaviour
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args):
            self.calls.append((name, args))
            answer = self._behaviour.get(name, 0)
            return answer(*args) if callable(answer) else answer

        return call

    def args_for(self, name):
        """Every argument tuple `name` was called with."""
        return [args for called, args in self.calls if called == name]

    def arg_for(self, name):
        """The one argument tuple `name` was called with."""
        (args,) = self.args_for(name)
        return args

    def was_called(self, name):
        """Whether `name` was called at all."""
        return bool(self.args_for(name))


class FakeGlobalMemory:
    """kernel32's global heap: real buffers, so a round trip is one."""

    def __init__(self):
        self.blocks = {}
        self._next = 1

    def GlobalAlloc(self, flags, size):
        """A movable block of `size` bytes, as a handle."""
        handle = self._next
        self._next += 1
        self.blocks[handle] = ctypes.create_string_buffer(size)
        return handle

    def GlobalFree(self, handle):
        """Drop a block."""
        self.blocks.pop(handle, None)
        return 0

    def GlobalLock(self, handle):
        """A pointer into the block, which `blocks` keeps alive."""
        return ctypes.cast(self.blocks[handle], ctypes.c_void_p)

    def GlobalUnlock(self, handle):
        """Succeed; nothing here needs unlocking."""
        return 1

    def GlobalSize(self, handle):
        """The block's byte length, as Windows reports it."""
        return ctypes.sizeof(self.blocks[handle])

    def GetCurrentThreadId(self):
        """A fixed id: one pump thread runs at a time in any of these tests."""
        return 4321


class Win32TestCase(unittest.TestCase):
    """A backend wired to a small fake desktop.

    The desktop has two monitors, a window per test, and an empty clipboard.
    The primary is first in the fake's own enumeration order only so that the
    sorting test has to put it second on purpose.
    """

    MONITORS = (
        # x, y, width, height, primary, device name, dpi
        (0, 0, 1920, 1080, True, r"\\.\DISPLAY1", 96),
        (-1280, 0, 1280, 1024, False, r"\\.\DISPLAY2", 144),
    )
    METRICS = {
        _winapi.SM_XVIRTUALSCREEN: -1280,
        _winapi.SM_YVIRTUALSCREEN: 0,
        _winapi.SM_CXVIRTUALSCREEN: 3200,
        _winapi.SM_CYVIRTUALSCREEN: 1080,
    }

    def setUp(self):
        """Build the desktop, patch it into `_winapi`, and construct the backend."""
        self.windows = {}
        self.monitor_info = {}
        self.metrics = dict(self.METRICS)
        self.clipboard = {}
        self.open_refusals = 0
        self.foreground = 0
        self.point_window = 0
        self.send_count = None
        self.sent = None
        self.cursor = (0, 0)
        self.cursor_handle = 0
        self.cursor_showing = 1
        self.keys_down = set()
        self.pixels = b""
        self.show_window_honoured = True
        self.title_honoured = True
        self.message_timeout_honoured = False
        self.set_clipboard_ok = True
        self.event_hooks = []
        self.unhooked = []
        self.failing_hook_ranges = frozenset()
        self.hooks_installed = threading.Event()
        self.quit_posted = threading.Event()
        self._next_hook_handle = 1
        self.dpi_of = {
            index + 100: monitor[6] for index, monitor in enumerate(self.MONITORS)
        }
        self.memory = FakeGlobalMemory()
        self.user32 = self._fake_user32()
        self.gdi32 = FakeLibrary(
            CreateCompatibleDC=lambda hdc: 1,
            CreateCompatibleBitmap=lambda hdc, width, height: 2,
            SelectObject=lambda hdc, obj: 3,
            BitBlt=lambda *args: 1,
            GetDIBits=self._get_dib_bits,
            DeleteObject=lambda obj: 1,
            DeleteDC=lambda hdc: 1,
        )
        self.shcore = FakeLibrary(GetDpiForMonitor=self._get_dpi_for_monitor)
        self.dwmapi = FakeLibrary(DwmGetWindowAttribute=self._get_dwm_attribute)
        for name, library in (
            ("user32", self.user32),
            ("gdi32", self.gdi32),
            ("shcore", self.shcore),
            ("dwmapi", self.dwmapi),
            ("kernel32", self.memory),
        ):
            patcher = mock.patch.object(_winapi, name, lambda lib=library: lib)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.gui = Win32Backend()

    def _fake_user32(self):
        """The recorded user32 the whole case shares."""
        return FakeLibrary(
            EnumWindows=self._enum_windows,
            EnumDisplayMonitors=self._enum_monitors,
            GetMonitorInfoW=self._get_monitor_info,
            GetSystemMetrics=self._get_system_metric,
            IsWindow=lambda hwnd: hwnd in self.windows,
            IsWindowVisible=lambda hwnd: self.windows[hwnd]["visible"],
            IsIconic=lambda hwnd: self.windows[hwnd]["iconic"],
            GetWindowLongPtrW=lambda hwnd, index: self.windows[hwnd]["style"],
            GetWindow=lambda hwnd, index: self.windows[hwnd]["owner"],
            GetAncestor=lambda hwnd, flag: self.windows[hwnd]["root"],
            GetWindowTextLengthW=lambda hwnd: len(self.windows[hwnd]["title"]),
            GetWindowTextW=self._get_window_text,
            GetClassNameW=self._get_class_name,
            GetWindowThreadProcessId=self._get_window_pid,
            GetWindowRect=self._get_window_rect,
            GetForegroundWindow=lambda: self.foreground,
            SetForegroundWindow=self._set_foreground,
            SetWindowPos=self._set_window_pos,
            ShowWindow=self._show_window,
            SetWindowTextW=self._set_window_text,
            SendMessageTimeoutW=self._send_message_timeout,
            WindowFromPoint=lambda point: self.point_window,
            GetCursorPos=self._get_cursor_pos,
            GetCursorInfo=self._get_cursor_info,
            GetAsyncKeyState=lambda vk: 0x8000 if vk in self.keys_down else 0,
            LoadCursorW=self._load_cursor,
            SendInput=self._send_input,
            GetDC=lambda hwnd: 99,
            ReleaseDC=lambda hwnd, hdc: 1,
            OpenClipboard=self._open_clipboard,
            CloseClipboard=lambda: 1,
            EmptyClipboard=self._empty_clipboard,
            IsClipboardFormatAvailable=lambda fmt: fmt in self.clipboard,
            GetClipboardData=lambda fmt: self.clipboard.get(fmt, 0),
            SetClipboardData=self._set_clipboard_data,
            SetWinEventHook=self._set_win_event_hook,
            UnhookWinEvent=self._unhook_win_event,
            PeekMessageW=lambda *args: 0,
            GetMessageW=self._get_message,
            TranslateMessage=lambda *args: 1,
            DispatchMessageW=lambda *args: 0,
            PostThreadMessageW=self._post_thread_message,
        )

    # -- the desktop's own behaviour ---------------------------------------

    def add_window(self, handle, **fields):
        """Add a window and return its handle.

        The defaults describe the ordinary case -- visible, titled, unowned,
        not a tool window, not cloaked, activatable -- so each test states only
        the field it is actually about.
        """
        window = {
            "title": f"window {handle}",
            "class_name": "SomeClass",
            "pid": 1000 + handle,
            "visible": True,
            "iconic": False,
            "style": 0,
            "owner": 0,
            "root": handle,
            "cloaked": 0,
            "rect": (10, 20, 810, 620),
            "activated": True,
            "needs_input": False,
        }
        window.update(fields)
        self.windows[handle] = window
        return handle

    def listed(self):
        """The titles of every window `windows()` reports."""
        return [window.title for window in self.gui.windows()]

    def _enum_windows(self, callback, param):
        for handle in list(self.windows):
            callback(handle, param)
        return 1

    def _enum_monitors(self, hdc, rect, callback, param):
        for index, monitor in enumerate(self.MONITORS):
            handle = index + 100
            self.monitor_info[handle] = monitor
            callback(handle, None, None, param)
        return 1

    def _get_monitor_info(self, hmonitor, info_ptr):
        x, y, width, height, primary, device, _dpi = self.monitor_info[hmonitor]
        info = info_ptr._obj
        info.cbSize = ctypes.sizeof(_winapi.MONITORINFOEXW)
        info.rcMonitor.left = x
        info.rcMonitor.top = y
        info.rcMonitor.right = x + width
        info.rcMonitor.bottom = y + height
        info.rcWork.left = x
        info.rcWork.top = y
        info.rcWork.right = x + width
        info.rcWork.bottom = y + height - 40
        info.dwFlags = _winapi.MONITORINFOF_PRIMARY if primary else 0
        for position, char in enumerate(device):
            info.szDevice[position] = ord(char)
        return 1

    def _get_system_metric(self, index):
        return self.metrics[index]

    def _get_window_text(self, hwnd, buffer, count):
        title = self.windows[hwnd]["title"]
        buffer.value = title
        return len(title)

    def _get_class_name(self, hwnd, buffer, count):
        name = self.windows[hwnd]["class_name"]
        buffer.value = name
        return len(name)

    def _get_window_pid(self, hwnd, pid_ptr):
        pid_ptr._obj.value = self.windows[hwnd]["pid"]
        return 7  # the owning thread's id, which nothing here reads

    def _get_window_rect(self, hwnd, rect_ptr):
        left, top, right, bottom = self.windows[hwnd]["rect"]
        rect_ptr._obj.left = left
        rect_ptr._obj.top = top
        rect_ptr._obj.right = right
        rect_ptr._obj.bottom = bottom
        return 1

    def _set_foreground(self, hwnd):
        # `needs_input` is Windows' foreground lock: refused until this process
        # has sent input of its own, whatever it is.
        granted = self.windows[hwnd]["activated"] and (
            not self.windows[hwnd]["needs_input"] or self.sent is not None
        )
        if granted:
            self.foreground = hwnd
            return 1
        return 0

    def _set_window_pos(self, hwnd, insert_after, x, y, width, height, flags):
        self.windows[hwnd]["position"] = (insert_after, x, y, width, height, flags)
        return 1

    def _show_window(self, hwnd, command):
        if self.show_window_honoured:
            self.windows[hwnd]["iconic"] = command == _winapi.SW_MINIMIZE
        return 1

    def _set_window_text(self, hwnd, text):
        if self.title_honoured:
            self.windows[hwnd]["title"] = text
        return 1

    def _send_message_timeout(
        self, hwnd, message, wparam, lparam, flags, timeout, result
    ):
        """`WM_SETTEXT`, as the target application would handle it.

        The message's lParam is the address of the caller's buffer, and this
        fake is in the same process, so the text can be read back out of it --
        which is the only way to show that the fallback route really carried
        the string rather than merely being called.
        """
        if not self.message_timeout_honoured:
            return 0
        self.windows[hwnd]["title"] = ctypes.c_wchar_p(lparam).value
        return 1

    def _get_cursor_pos(self, point_ptr):
        point_ptr._obj.x, point_ptr._obj.y = self.cursor
        return 1

    def _get_cursor_info(self, info_ptr):
        info = info_ptr._obj
        info.cbSize = ctypes.sizeof(_winapi.CURSORINFO)
        info.flags = self.cursor_showing
        info.hCursor = self.cursor_handle
        info.ptScreenPos.x, info.ptScreenPos.y = self.cursor
        return 1

    def _load_cursor(self, instance, name):
        # A resource id travels as a pointer, so its address *is* the handle
        # the system reports for that cursor: 32512 for an arrow, and so on.
        return ctypes.cast(name, ctypes.c_void_p).value

    def _send_input(self, count, events, size):
        self.sent = (count, events, size)
        return count if self.send_count is None else self.send_count

    def _get_dpi_for_monitor(self, hmonitor, dpi_type, x_ptr, y_ptr):
        x_ptr._obj.value = self.dpi_of[hmonitor]
        y_ptr._obj.value = self.dpi_of[hmonitor]
        return 0

    def _get_dwm_attribute(self, hwnd, attribute, value_ptr, size):
        value_ptr._obj.value = self.windows[hwnd]["cloaked"]
        return 0

    def _open_clipboard(self, hwnd):
        if self.open_refusals:
            self.open_refusals -= 1
            return 0
        return 1

    def _empty_clipboard(self):
        self.clipboard.clear()
        return 1

    def _set_clipboard_data(self, fmt, handle):
        if not self.set_clipboard_ok:
            return 0
        self.clipboard[fmt] = handle
        return handle

    # -- the WinEvent hook and its pump thread's message loop ---------------
    #
    # `GetMessageW` really does block here, on a `threading.Event` rather
    # than the real OS message queue: `window_events()` runs its own pump
    # thread for real, and this is what lets that thread behave the way
    # `PostThreadMessageW(WM_QUIT, ...)` documents without a live desktop
    # under it. The 5 second cap is a safety net for a test that gets the
    # synchronization wrong, not a value production code should ever wait
    # out.

    def _set_win_event_hook(
        self, low, high, hmod, callback, idprocess, idthread, flags
    ):
        self.event_hooks.append((low, high, callback, idprocess, idthread, flags))
        if (low, high) in self.failing_hook_ranges:
            return 0
        handle = self._next_hook_handle
        self._next_hook_handle += 1
        if len(self.event_hooks) == len(_WINDOW_EVENT_RANGES):
            self.hooks_installed.set()
        return handle

    def _unhook_win_event(self, handle):
        self.unhooked.append(handle)
        return 1

    def _get_message(self, msg_ptr, hwnd, wmsg_min, wmsg_max):
        self.quit_posted.wait(timeout=5.0)
        return 0

    def _post_thread_message(self, thread_id, message, wparam, lparam):
        if message == _winapi.WM_QUIT:
            self.quit_posted.set()
        return 1

    def fire_window_event(self, event, hwnd):
        """Call the registered `WinEventProc` as Windows would for one event.

        Only through a hook whose range actually covers `event`, the same
        restriction a real `SetWinEventHook` caller relies on -- a test that
        asks for an event outside every registered range is asking a question
        this backend never subscribed to.
        """
        for low, high, callback, _idprocess, _idthread, _flags in self.event_hooks:
            if low <= event <= high:
                callback(
                    0, event, hwnd, _winapi.OBJID_WINDOW, _winapi.CHILDID_SELF, 0, 0
                )
                return
        raise AssertionError(f"no WinEvent hook covers event {event:#06x}")

    def run_generator(self, generator, after_start=None):
        """Advance `generator` on its own thread and return what it produced.

        `window_events()` blocks -- on `ready.wait()` while its pump thread
        starts, then on `queue.Queue.get()` for the next event -- so it
        cannot be driven from the same thread a test fires events from
        without deadlocking. `after_start`, if given, runs on the calling
        thread right after the generator's own thread starts: it is where a
        test waits for `hooks_installed` and then calls `fire_window_event`,
        while `next(generator)` proceeds concurrently on the other thread.
        Waits up to 5 seconds, the same safety margin `_get_message` gives the
        pump thread, before failing the test rather than hanging the suite.
        """
        outcome: list = []

        def advance():
            try:
                outcome.append(next(generator))
            except StopIteration:
                outcome.append(None)
            except Exception as exc:  # noqa: BLE001 -- reported by the test
                outcome.append(exc)

        thread = threading.Thread(target=advance, daemon=True)
        thread.start()
        if after_start is not None:
            after_start()
        thread.join(timeout=5.0)
        if thread.is_alive():
            raise AssertionError("window_events() did not produce anything in time")
        (result,) = outcome
        if isinstance(result, Exception):
            raise result
        return result

    # -- pixels and files --------------------------------------------------

    def set_pixels(self, rows):
        """Hand `GetDIBits` these top-down RGB rows, as Windows really would.

        Converted to what the call actually returns -- four bytes per pixel,
        blue first, bottom row first -- so a capture test asserts that the
        backend undoes both of those rather than asserting its own arithmetic
        back at itself.
        """
        width = len(rows[0]) // 3
        data = bytearray()
        for row in reversed(rows):
            for x in range(width):
                red, green, blue = row[x * 3 : x * 3 + 3]
                data += bytes((blue, green, red, 0))
        self.pixels = bytes(data)

    def _get_dib_bits(self, hdc, bitmap, start, lines, buffer, header_ptr, usage):
        ctypes.memmove(buffer, self.pixels, len(self.pixels))
        return lines

    def decode_png(self, path):
        """Read back a PNG this backend wrote, as (width, height, rows)."""
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

    def open_window_events(self, timeout=5.0):
        """`self.gui.window_events(timeout=timeout)`, closed at test teardown.

        Closing runs the generator's own `finally` -- posting `WM_QUIT` and
        joining the pump thread -- which is near-instant against these fakes,
        rather than leaving each test to wait out its own `timeout`.
        """
        generator = self.gui.window_events(timeout=timeout)
        self.addCleanup(generator.close)
        return generator

    def temporary_path(self):
        """A path under the temp directory that the test cleans up.

        `tempfile.gettempdir()` rather than `TMPDIR` or `/tmp`: this is the one
        backend test file whose subject *is* Windows, so it is also the one
        likeliest to be run there -- and Windows sets `TEMP`, not `TMPDIR`, so
        the old default put every capture test's output in a literal `/tmp`
        that does not exist. Found on the first real run on Windows 11, where
        it failed all six capture tests with a `FileNotFoundError` that said
        nothing about the cause.
        """
        path = os.path.join(tempfile.gettempdir(), f"pyguitest-win32-{id(self)}.png")
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path


class TestTheCoordinateHelper(unittest.TestCase):
    """The one piece of arithmetic here that runs on any machine.

    It is also where the two silent mistakes in an absolute pointer move live:
    forgetting the virtual desktop's origin, and truncating instead of rounding.
    """

    def test_the_corners_of_the_desktop_are_the_ends_of_the_range(self):
        box = (0, 0, 1920, 1080)
        self.assertEqual(_absolute_input(0, 0, box), (0, 0))
        self.assertEqual(_absolute_input(1919, 1079, box), (65535, 65535))

    def test_a_monitor_left_of_the_primary_starts_at_zero(self):
        # The virtual desktop's origin is negative on any such machine, and its
        # left-most pixel is the range's zero rather than a clamped negative.
        box = (-1280, 0, 3200, 1080)
        self.assertEqual(_absolute_input(-1280, 0, box)[0], 0)

    def test_coordinates_outside_the_desktop_are_clamped_to_an_edge(self):
        box = (-1280, 0, 3200, 1080)
        self.assertEqual(_absolute_input(-9000, -9000, box), (0, 0))
        self.assertEqual(_absolute_input(9000, 9000, box), (65535, 65535))

    def test_a_fractional_step_rounds_to_the_nearest_one(self):
        # Pixel 960 of a 1920-wide desktop is 32784.57 steps: truncation would
        # answer 32784 and bias every coordinate inward from the far corner.
        self.assertEqual(_absolute_input(960, 0, (0, 0, 1920, 1080))[0], 32785)

    def test_a_one_pixel_axis_has_no_second_edge(self):
        self.assertEqual(_absolute_input(0, 0, (0, 0, 1, 1)), (0, 0))


class TestTheCapabilitySet(Win32TestCase):
    """What this backend claims, and what it deliberately does not."""

    def test_the_in_process_capabilities_are_claimed(self):
        for capability in (
            Capability.SCREEN_INFO,
            Capability.SCREEN_CAPTURE,
            Capability.POINTER_MOVE,
            Capability.POINTER_BUTTON,
            Capability.POINTER_SCROLL,
            Capability.KEY_EVENT,
            Capability.TEXT_ENTRY,
            Capability.WINDOW_LIST,
            Capability.WINDOW_EVENTS,
            Capability.WINDOW_STATE,
            Capability.WINDOW_ACTIVATE,
            Capability.WINDOW_GEOMETRY,
            Capability.WINDOW_PLACEMENT,
            Capability.WINDOW_RESIZE,
            Capability.WINDOW_MINIMIZE,
            Capability.WINDOW_PID,
            Capability.WINDOW_AT_POINT,
            Capability.CLIPBOARD,
        ):
            with self.subTest(capability=capability.name):
                self.assertIn(capability, self.gui.capabilities)

    def test_the_tier_six_block_is_claimed_because_it_is_ordinary_here(self):
        # The tier is a Wayland ceiling: a Windows `report()` shows these as
        # available under a heading whose own text says "deliberately
        # prevented", which is the difference the capability model exists to
        # express.
        for capability in (
            Capability.POINTER_QUERY,
            Capability.INPUT_STATE_QUERY,
            Capability.WINDOW_TITLE_SET,
            Capability.WINDOW_LOWER,
            Capability.WINDOW_CURSOR_QUERY,
        ):
            with self.subTest(capability=capability.name):
                self.assertIn(capability, self.gui.capabilities)

    def test_elements_are_not_claimed(self):
        # They are UI Automation's, in a backend of their own.
        for capability in (
            Capability.ELEMENT_TREE,
            Capability.ELEMENT_ACTION,
            Capability.ELEMENT_GEOMETRY,
        ):
            with self.subTest(capability=capability.name):
                self.assertNotIn(capability, self.gui.capabilities)

    def test_one_window_un_occluded_is_not_claimed(self):
        # PrintWindow is a later phase, so `screenshot(window=...)` must not
        # silently return a crop that includes an occluding window.
        self.assertNotIn(Capability.WINDOW_CAPTURE, self.gui.capabilities)
        window = Window(1, self.gui, title="anything")
        with self.assertRaises(CapabilityUnsupported) as raised:
            self.gui.capture(window=window)
        self.assertIs(raised.exception.capability, Capability.WINDOW_CAPTURE)

    def test_the_event_feed_is_claimed(self):
        # SetWinEventHook, unlike PrintWindow-based WINDOW_CAPTURE, needs
        # nothing this backend does not already have -- see TestWindowEvents.
        self.assertIn(Capability.WINDOW_EVENTS, self.gui.capabilities)

    def test_the_thread_dpi_context_is_set_when_the_backend_is_built(self):
        # Per thread, deliberately: the process-wide call cannot be undone and
        # would re-lay-out the host application's own windows.
        (context,) = self.user32.arg_for("SetThreadDpiAwarenessContext")
        self.assertEqual(context, _winapi.DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)

    def test_sync_refuses_and_says_why(self):
        # Not a bare "unsupported": the whole reason this is refused is worth
        # carrying to the caller, since the alternative looks like it works.
        with self.assertRaises(CapabilityUnsupported) as raised:
            self.gui.sync()
        self.assertIs(raised.exception.capability, Capability.INPUT_SYNC)
        self.assertIn("SendInput", raised.exception.reason)

    def test_the_backend_refuses_to_build_without_the_api(self):
        with mock.patch.object(_winapi, "user32", lambda: None):
            with self.assertRaises(BackendUnavailable) as raised:
                Win32Backend()
        self.assertIn("user32.dll", str(raised.exception))


class TestScreens(Win32TestCase):
    """Monitor enumeration: the order, the size, and the scale it reports."""

    def test_the_primary_monitor_is_index_zero_wherever_it_is_found(self):
        primary, secondary = self.MONITORS
        # Enumerated secondary-first, which is what a real desktop does when a
        # monitor sits to the left of the primary.
        self.MONITORS = (secondary, primary)
        self.assertEqual(
            [screen.name for screen in self.gui.screens()],
            [r"\\.\DISPLAY1", r"\\.\DISPLAY2"],
        )

    def test_scale_is_the_monitors_dpi_over_96(self):
        # 144/96 is the 150% a Windows display setting shows, and 1.0 is both
        # an unscaled monitor and the answer everywhere without shcore.
        self.assertEqual([screen.scale for screen in self.gui.screens()], [1.0, 1.5])

    def test_size_is_the_monitors_own_rectangle(self):
        sizes = [screen.size for screen in self.gui.screens()]
        self.assertEqual(sizes, [(1920, 1080), (1280, 1024)])

    def test_monitors_are_numbered_from_zero_in_reporting_order(self):
        self.assertEqual([screen.index for screen in self.gui.screens()], [0, 1])

    def test_a_machine_without_shcore_reports_no_scaling(self):
        with mock.patch.object(_winapi, "shcore", lambda: None):
            self.assertEqual(
                [screen.scale for screen in self.gui.screens()], [1.0, 1.0]
            )

    def test_a_failed_enumeration_raises_rather_than_reporting_no_monitors(self):
        # The two are the same empty list and mean opposite things: a session
        # always has a monitor, so an empty answer sends a caller looking at
        # their display configuration for a fault that is in the call.
        self.user32.EnumDisplayMonitors = lambda hdc, rect, callback, param: 0
        with self.assertRaises(PyGUITestError):
            self.gui.screens()


class TestWindows(Win32TestCase):
    """`windows()` and the filter that decides what it reports.

    Every case here is one line of 3.2's filter list, because the failures are
    indistinguishable from each other in a running suite and only separable
    here.
    """

    def test_windows_are_reported_bottommost_first(self):
        # EnumWindows yields the stack top down; this package's order is bottom
        # up, because Session.find_window takes found[-1] as "the topmost".
        # Reported in enumeration order instead, a title two windows share
        # would resolve to the one at the *bottom* of the stack -- the X11
        # window-title-collision bug, arriving again on a second platform.
        self.add_window(1)
        self.add_window(2)
        self.add_window(3)
        self.assertEqual(self.listed(), ["window 3", "window 2", "window 1"])

    def test_the_last_window_listed_is_the_topmost_one(self):
        # The invariant the Session layer actually depends on, asserted as
        # itself rather than as a consequence of the order above.
        self.add_window(1)
        self.add_window(2)
        topmost = self.gui.windows()[-1]
        self.assertEqual(topmost.title, "window 1")

    def test_an_invisible_window_is_dropped(self):
        self.add_window(1)
        self.add_window(2, visible=False)
        self.assertEqual(self.listed(), ["window 1"])

    def test_a_tool_window_is_dropped(self):
        # A palette or floating toolbar: enumerable, visible, and never what a
        # caller meant.
        self.add_window(1)
        self.add_window(2, style=_winapi.WS_EX_TOOLWINDOW)
        self.assertEqual(self.listed(), ["window 1"])

    def test_an_owned_window_is_kept(self):
        # A dialog opened with an owner, which is what nearly every
        # Find/Replace, About and confirmation dialog is. Alt-Tab hides these
        # because they travel with their owner, but a test cannot Alt-Tab: with
        # this dropped, wait_for_window() returned None forever for the one
        # window a caller was waiting on while FindWindowW answered with the
        # handle immediately.
        self.add_window(1)
        self.add_window(2, owner=1)
        self.assertEqual(sorted(self.listed()), ["window 1", "window 2"])

    def test_an_owned_tool_window_is_dropped(self):
        # Owning a window no longer decides, but WS_EX_TOOLWINDOW still does --
        # an owned palette is a palette. This is the pair that shows the two
        # rules are independent rather than one standing in for the other.
        self.add_window(1)
        self.add_window(2, owner=1, style=_winapi.WS_EX_TOOLWINDOW)
        self.assertEqual(self.listed(), ["window 1"])

    def test_a_tool_window_that_asks_to_be_listed_is_kept(self):
        # Sorted, because this case is about WS_EX_APPWINDOW overriding
        # WS_EX_TOOLWINDOW -- which of the pair is on top is the ordering
        # tests' subject, and asserting it here as well would make this one
        # fail for a reason it is not about. Owned as well, because that is
        # the shape the override exists for: a utility window with a real
        # taskbar entry keeps one.
        self.add_window(1)
        self.add_window(
            2, owner=1, style=_winapi.WS_EX_TOOLWINDOW | _winapi.WS_EX_APPWINDOW
        )
        self.assertEqual(sorted(self.listed()), ["window 1", "window 2"])

    def test_a_cloaked_window_is_dropped(self):
        # A suspended Store application: enumerable, style-visible, and nowhere
        # on screen. Leaving this out is what makes wait_for_window match a
        # ghost.
        self.add_window(1)
        self.add_window(2, cloaked=1)
        self.assertEqual(self.listed(), ["window 1"])

    def test_a_minimized_window_is_kept(self):
        # WINDOW_STATE exists to report it, so dropping it would remove the
        # answer to the question the capability was added for.
        self.add_window(1, iconic=True)
        self.assertEqual(self.listed(), ["window 1"])

    def test_a_missing_dwmapi_means_nothing_is_cloaked(self):
        # The attribute arrived with the behaviour, so a machine without the
        # library is one where the condition does not exist.
        self.add_window(1, cloaked=1)
        with mock.patch.object(_winapi, "dwmapi", lambda: None):
            self.assertEqual(self.listed(), ["window 1"])

    def test_each_window_carries_its_own_title_class_and_pid(self):
        self.add_window(4, title="Notepad", class_name="Notepad", pid=4242)
        (window,) = self.gui.windows()
        self.assertEqual(window.title, "Notepad")
        self.assertEqual(window.app_id, "Notepad")
        self.assertEqual(window.pid, 4242)

    def test_the_foreground_window_is_the_active_one(self):
        self.add_window(1)
        self.add_window(2)
        self.foreground = 2
        self.assertEqual(self.gui.active_window().title, "window 2")

    def test_nothing_in_the_foreground_is_no_active_window(self):
        self.add_window(1)
        self.assertIsNone(self.gui.active_window())

    def test_a_foreground_window_the_filter_would_drop_is_no_active_window(self):
        # Better an honest "no window I can name" than a handle nothing else
        # here will act on.
        self.add_window(1, style=_winapi.WS_EX_TOOLWINDOW)
        self.foreground = 1
        self.assertIsNone(self.gui.active_window())


class TestWindowOperations(Win32TestCase):
    """Geometry, placement, activation, minimization and the title."""

    def only_window(self, handle=1, **fields):
        """Add one window, list it, and return the `Window`."""
        self.add_window(handle, **fields)
        return self.gui.windows()[0]

    def position(self, handle=1):
        """Whatever the last SetWindowPos recorded, destructured."""
        return self.windows[handle]["position"]

    def test_geometry_is_the_window_rectangle(self):
        window = self.only_window(rect=(10, 20, 810, 620))
        self.assertEqual(self.gui.geometry(window), (10, 20, 800, 600))

    def test_a_move_neither_resizes_restacks_nor_activates(self):
        window = self.only_window()
        self.gui.move_window(window, 100, 200)
        assert_after, x, y, width, height, flags = self.position()
        self.assertEqual((assert_after, x, y, width, height), (None, 100, 200, 0, 0))
        self.assertTrue(flags & _winapi.SWP_NOSIZE)
        self.assertTrue(flags & _winapi.SWP_NOZORDER)
        self.assertTrue(flags & _winapi.SWP_NOACTIVATE)
        self.assertFalse(flags & _winapi.SWP_NOMOVE)

    def test_a_resize_neither_moves_restacks_nor_activates(self):
        window = self.only_window()
        self.gui.resize_window(window, 640, 480)
        _after, x, y, width, height, flags = self.position()
        self.assertEqual((x, y, width, height), (0, 0, 640, 480))
        self.assertTrue(flags & _winapi.SWP_NOMOVE)
        self.assertTrue(flags & _winapi.SWP_NOZORDER)
        self.assertTrue(flags & _winapi.SWP_NOACTIVATE)
        self.assertFalse(flags & _winapi.SWP_NOSIZE)

    def test_lowering_puts_the_window_at_the_bottom_without_activating_it(self):
        window = self.only_window()
        self.gui.lower_window(window)
        assert_after, _x, _y, _w, _h, flags = self.position()
        self.assertEqual(assert_after, _winapi.HWND_BOTTOM)
        self.assertTrue(flags & _winapi.SWP_NOACTIVATE)
        self.assertTrue(flags & _winapi.SWP_NOMOVE)
        self.assertTrue(flags & _winapi.SWP_NOSIZE)

    def test_activation_is_verified_rather_than_assumed(self):
        # The case the analysis singles out: SetForegroundWindow reports
        # success and the foreground stays where it was, so trusting it would
        # leave every later keystroke going to the wrong window.
        window = self.only_window(activated=False)
        with self.assertRaises(PyGUITestError) as raised:
            self.gui.activate_window(window)
        self.assertIn("foreground", str(raised.exception))

    def test_activation_succeeds_when_the_foreground_is_granted(self):
        window = self.only_window()
        self.gui.activate_window(window)
        self.assertEqual(self.foreground, 1)

    def test_a_foreground_refused_for_want_of_input_is_retried_after_a_nudge(self):
        # Measured live: with another process in front and no input of its own
        # lately, SetForegroundWindow is refused, and a zero-distance mouse
        # move is what makes the retry take. Before this, activating a
        # background window raised every time.
        window = self.only_window(needs_input=True)
        self.gui.activate_window(window)
        self.assertEqual(self.foreground, 1)

    def test_the_nudge_moves_nothing_and_clicks_nothing(self):
        window = self.only_window(needs_input=True)
        self.gui.activate_window(window)
        count, events, _size = self.sent
        self.assertEqual(count, 1)
        mouse = events[0].mi
        self.assertEqual(events[0].type, _winapi.INPUT_MOUSE)
        self.assertEqual((mouse.dx, mouse.dy), (0, 0))
        # Relative, and a move only: not ABSOLUTE, not a button, not a wheel.
        self.assertEqual(mouse.dwFlags, _winapi.MOUSEEVENTF_MOVE)

    def test_no_input_is_sent_when_the_first_attempt_took(self):
        window = self.only_window()
        self.gui.activate_window(window)
        self.assertIsNone(self.sent)

    def test_a_window_that_still_refuses_after_the_nudge_raises(self):
        window = self.only_window(activated=False)
        with self.assertRaises(PyGUITestError) as raised:
            self.gui.activate_window(window)
        self.assertIn("foreground", str(raised.exception))
        self.assertIsNotNone(self.sent)

    def test_a_minimized_window_is_not_nudged(self):
        window = self.only_window(activated=False, iconic=True)
        with self.assertRaises(PyGUITestError):
            self.gui.activate_window(window)
        self.assertIsNone(self.sent)

    def test_a_minimized_window_is_told_apart_from_a_held_foreground(self):
        # SetForegroundWindow does not restore a minimized window, so the
        # failure has nothing to do with who is holding the foreground -- and
        # the generic message would send a caller looking for a process that
        # is not there.
        window = self.only_window(activated=False, iconic=True)
        with self.assertRaises(PyGUITestError) as raised:
            self.gui.activate_window(window)
        self.assertIn("minimized", str(raised.exception))

    def test_minimizing_is_verified_with_is_iconic(self):
        # ShowWindow's own return value is "was it hidden before", so it cannot
        # answer this question; a silent refusal must not read as success.
        window = self.only_window()
        self.gui.minimize_window(window)
        self.assertTrue(self.windows[1]["iconic"])
        self.gui.minimize_window(window, minimized=False)
        self.assertFalse(self.windows[1]["iconic"])

    def test_a_minimize_that_did_not_take_raises(self):
        window = self.only_window()
        self.show_window_honoured = False
        with self.assertRaises(PyGUITestError) as raised:
            self.gui.minimize_window(window)
        self.assertIn("did not minimize", str(raised.exception))

    def test_the_title_is_set_through_set_window_text(self):
        window = self.only_window()
        self.gui.set_window_title(window, "renamed")
        self.assertEqual(self.windows[1]["title"], "renamed")
        self.assertFalse(self.user32.was_called("SendMessageTimeoutW"))

    def test_a_window_that_ignores_set_window_text_gets_the_message(self):
        window = self.only_window()
        self.title_honoured = False
        self.message_timeout_honoured = True
        self.gui.set_window_title(window, "renamed")
        self.assertEqual(self.windows[1]["title"], "renamed")
        _hwnd, message, wparam, _lparam, flags, timeout, _result = self.user32.arg_for(
            "SendMessageTimeoutW"
        )
        self.assertEqual(message, _winapi.WM_SETTEXT)
        self.assertEqual(wparam, 0)
        self.assertTrue(flags & _winapi.SMTO_ABORTIFHUNG)
        self.assertEqual(timeout, 1000)

    def test_a_title_neither_route_accepted_raises(self):
        window = self.only_window()
        self.title_honoured = False
        with self.assertRaises(PyGUITestError) as raised:
            self.gui.set_window_title(window, "renamed")
        self.assertIn("did not accept the title", str(raised.exception))

    def test_window_at_walks_up_to_the_toplevel(self):
        window = self.only_window()
        self.point_window = 1
        self.assertEqual(self.gui.window_at(5, 5), window)

    def test_window_at_a_point_over_nothing_is_none(self):
        self.only_window()
        self.point_window = 0
        self.assertIsNone(self.gui.window_at(5, 5))

    def test_a_point_over_a_window_the_filter_drops_is_none(self):
        # The handle is added but never listed: taking a `Window` from
        # `windows()` here would be asking the test to fail a step earlier.
        self.add_window(1, style=_winapi.WS_EX_TOOLWINDOW)
        self.point_window = 1
        self.assertIsNone(self.gui.window_at(5, 5))

    def test_a_window_that_has_closed_is_refused_by_handle(self):
        window = self.only_window()
        del self.windows[1]
        with self.assertRaises(WindowNotFound):
            self.gui.geometry(window)

    def test_a_closed_window_is_simply_not_viewable(self):
        window = self.only_window()
        self.assertTrue(self.gui.is_window_viewable(window))
        del self.windows[1]
        self.assertFalse(self.gui.is_window_viewable(window))

    def test_a_window_from_another_backend_is_refused(self):
        window = self.only_window()
        other = Win32Backend()
        with self.assertRaises(WindowNotFound):
            self.gui.geometry(Window(window.handle, other, title=window.title))


class TestClassifyWindowEvent(Win32TestCase):
    """`_classify_window_event`'s decision table, with no thread in sight.

    This is where "new" versus "title" versus "close" versus "focus" is
    actually decided; `TestWindowEvents` below only has to show that a real
    WinEvent hook and pump thread deliver *something* to it correctly, not
    re-litigate every branch under real threading too.
    """

    def test_a_create_for_a_listable_window_not_yet_known_is_new(self):
        handle = self.add_window(1)
        known: set = set()
        change = self.gui._classify_window_event(
            _winapi.EVENT_OBJECT_CREATE, handle, known
        )
        self.assertEqual(change, "new")
        self.assertIn(handle, known)

    def test_a_create_for_an_unlistable_window_is_nothing(self):
        # A tool window or a cloaked one: neither is a window this package
        # would list, so their own creation is not "new" any more than
        # EnumWindows would report them. An owned window is deliberately not
        # in this list -- see test_an_owned_window_is_kept.
        handle = self.add_window(1, style=_winapi.WS_EX_TOOLWINDOW)
        known: set = set()
        change = self.gui._classify_window_event(
            _winapi.EVENT_OBJECT_CREATE, handle, known
        )
        self.assertIsNone(change)
        self.assertNotIn(handle, known)

    def test_a_namechange_for_a_known_window_is_title(self):
        handle = self.add_window(1)
        known = {handle}
        change = self.gui._classify_window_event(
            _winapi.EVENT_OBJECT_NAMECHANGE, handle, known
        )
        self.assertEqual(change, "title")

    def test_a_namechange_for_an_unknown_listable_window_is_new(self):
        # The create event for it was missed (or predates this call), but a
        # namechange is still evidence it exists and qualifies -- the same
        # "first time seen" rule niri's own window_events() uses.
        handle = self.add_window(1)
        known: set = set()
        change = self.gui._classify_window_event(
            _winapi.EVENT_OBJECT_NAMECHANGE, handle, known
        )
        self.assertEqual(change, "new")
        self.assertIn(handle, known)

    def test_a_destroy_for_a_known_window_is_close_and_forgets_it(self):
        handle = self.add_window(1)
        known = {handle}
        change = self.gui._classify_window_event(
            _winapi.EVENT_OBJECT_DESTROY, handle, known
        )
        self.assertEqual(change, "close")
        self.assertNotIn(handle, known)

    def test_a_destroy_for_an_unknown_handle_is_nothing(self):
        # The ordinary case: a child control's own destroy, which was never
        # added to `known` because it never passed `_is_listable`. Windows
        # fires EVENT_OBJECT_DESTROY for every window-class object, not only
        # the toplevels this package reports.
        known: set = set()
        change = self.gui._classify_window_event(
            _winapi.EVENT_OBJECT_DESTROY, 99, known
        )
        self.assertIsNone(change)

    def test_a_foreground_change_for_a_known_window_is_focus(self):
        handle = self.add_window(1)
        known = {handle}
        change = self.gui._classify_window_event(
            _winapi.EVENT_SYSTEM_FOREGROUND, handle, known
        )
        self.assertEqual(change, "focus")
        self.assertIn(handle, known)

    def test_a_foreground_change_for_an_unknown_listable_window_is_new(self):
        handle = self.add_window(1)
        known: set = set()
        change = self.gui._classify_window_event(
            _winapi.EVENT_SYSTEM_FOREGROUND, handle, known
        )
        self.assertEqual(change, "new")

    def test_a_foreground_change_for_an_unlistable_window_is_nothing(self):
        # A tool window rather than an owned one: owning a window no longer
        # makes it unlistable, but WS_EX_TOOLWINDOW still does.
        handle = self.add_window(1, style=_winapi.WS_EX_TOOLWINDOW)
        known: set = set()
        change = self.gui._classify_window_event(
            _winapi.EVENT_SYSTEM_FOREGROUND, handle, known
        )
        self.assertIsNone(change)

    def test_an_unrelated_event_is_nothing(self):
        # EVENT_OBJECT_SHOW, say -- deliberately not hooked; see the module
        # docstring's "extras" note. Reachable here even though no hook
        # delivers it in practice, since this method has no idea which
        # events a caller's hooks actually cover.
        change = self.gui._classify_window_event(0x8002, 1, set())
        self.assertIsNone(change)


class TestWindowEvents(Win32TestCase):
    """`window_events()` end to end: a real hook, a real pump thread.

    `TestClassifyWindowEvent` covers the new/title/close/focus decision
    itself; what is left to show here is that `SetWinEventHook` is asked for
    the right ranges and flags, that a fired event really does travel through
    the queue and the thread boundary and come out as a `WindowEvent`, and
    that a failed install or a stopped generator clean up the hooks and the
    thread rather than leaking either.

    Every scenario below adds its window with `add_window` *before* creating
    the generator, so it is unambiguously part of the `known` set seeded
    from `windows()` before the loop starts -- regardless of exactly when the
    consumer thread gets around to that seeding relative to the pump thread
    installing hooks. `TestClassifyWindowEvent` is where "new" (a handle that
    is *not* part of that seed) is exercised, precisely because that
    ordering is not observable from outside and not worth racing against in
    a threaded test.
    """

    def test_the_hooks_cover_the_documented_ranges_with_the_right_flags(self):
        result = self.run_generator(self.gui.window_events(timeout=0.2))
        self.assertIsNone(result)  # timed out with nothing to report
        ranges = [(low, high) for low, high, *_rest in self.event_hooks]
        self.assertEqual(ranges, list(_WINDOW_EVENT_RANGES))
        for _low, _high, _callback, idprocess, idthread, flags in self.event_hooks:
            self.assertEqual(idprocess, 0)
            self.assertEqual(idthread, 0)
            self.assertEqual(
                flags, _winapi.WINEVENT_OUTOFCONTEXT | _winapi.WINEVENT_SKIPOWNPROCESS
            )

    def fire_once_hooked(self, event, handle):
        """Wait for the hooks, then fire one event -- an `after_start` body."""

        def fire():
            self.assertTrue(self.hooks_installed.wait(timeout=2.0))
            self.fire_window_event(event, handle)

        return fire

    def test_a_namechange_round_trips_as_a_title_event(self):
        handle = self.add_window(1, title="renamed already")
        event = self.run_generator(
            self.open_window_events(),
            after_start=self.fire_once_hooked(_winapi.EVENT_OBJECT_NAMECHANGE, handle),
        )
        self.assertEqual(event.change, "title")
        self.assertEqual(event.window.handle, handle)
        self.assertEqual(event.window.title, "renamed already")

    def test_a_destroy_round_trips_as_a_close_event_with_a_bare_window(self):
        handle = self.add_window(1)
        event = self.run_generator(
            self.open_window_events(),
            after_start=self.fire_once_hooked(_winapi.EVENT_OBJECT_DESTROY, handle),
        )
        self.assertEqual(event.change, "close")
        self.assertEqual(event.window.handle, handle)
        # No title lookup against a handle that may already be gone -- the
        # same bare Window() the compositor-IPC backends yield for "close".
        self.assertEqual(event.window.title, "")

    def test_a_foreground_change_round_trips_as_a_focus_event(self):
        handle = self.add_window(1)
        event = self.run_generator(
            self.open_window_events(),
            after_start=self.fire_once_hooked(_winapi.EVENT_SYSTEM_FOREGROUND, handle),
        )
        self.assertEqual(event.change, "focus")
        self.assertEqual(event.window.handle, handle)

    def test_a_failed_hook_install_raises_and_unhooks_what_succeeded(self):
        self.failing_hook_ranges = {_WINDOW_EVENT_RANGES[1]}
        with self.assertRaises(PyGUITestError) as raised:
            self.run_generator(self.gui.window_events())
        self.assertIn("SetWinEventHook", str(raised.exception))
        # Stopped at the failing range rather than trying the third.
        self.assertEqual(len(self.event_hooks), 2)
        # Only the one hook that actually succeeded needed unhooking.
        self.assertEqual(self.unhooked, [1])

    def test_closing_the_generator_posts_quit_and_joins_the_pump(self):
        handle = self.add_window(1)
        generator = self.gui.window_events(timeout=5.0)
        self.run_generator(
            generator,
            after_start=self.fire_once_hooked(_winapi.EVENT_SYSTEM_FOREGROUND, handle),
        )
        self.assertFalse(self.quit_posted.is_set())
        generator.close()
        self.assertTrue(self.quit_posted.is_set())
        self.assertEqual(len(self.unhooked), len(self.event_hooks))

    def test_wait_for_window_finds_an_already_open_window_without_a_hook(self):
        self.add_window(1, title="Preferences")
        found = self.gui.wait_for_window("Prefer")
        self.assertEqual(found.handle, 1)
        # No SetWinEventHook at all: the existing-windows check short-circuits
        # before window_events() is ever called.
        self.assertEqual(self.event_hooks, [])

    def test_wait_for_window_matches_a_live_new_event(self):
        matched: list = []

        def fire_and_create():
            self.assertTrue(self.hooks_installed.wait(timeout=2.0))
            handle = self.add_window(1, title="Save changes?")
            self.fire_window_event(_winapi.EVENT_OBJECT_CREATE, handle)

        def call():
            matched.append(self.gui.wait_for_window("Save changes"))

        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        fire_and_create()
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())
        (found,) = matched
        self.assertEqual(found.title, "Save changes?")


class TestInput(Win32TestCase):
    """`SendInput`: the messages, the resolution of key names, and refusals."""

    def sent_events(self):
        """The `INPUT` array from the last call, as a list."""
        count, events, size = self.sent
        self.assertEqual(count, len(events))
        return list(events)

    def sent_one(self):
        """The single `INPUT` from the last call."""
        (event,) = self.sent_events()
        return event

    def test_the_message_size_is_the_declared_structure(self):
        self.gui.press_button(1)
        _count, _events, size = self.sent
        self.assertEqual(size, ctypes.sizeof(_winapi.INPUT))
        self.assertEqual(size, 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)

    def test_a_move_is_absolute_on_the_whole_virtual_desktop(self):
        self.gui.move_mouse(100, 200)
        event = self.sent_one()
        self.assertEqual(event.type, _winapi.INPUT_MOUSE)
        self.assertTrue(event.mi.dwFlags & _winapi.MOUSEEVENTF_MOVE)
        self.assertTrue(event.mi.dwFlags & _winapi.MOUSEEVENTF_ABSOLUTE)
        # Without VIRTUALDESK the range covers the primary monitor only, so
        # every coordinate on a second monitor would land on the primary.
        self.assertTrue(event.mi.dwFlags & _winapi.MOUSEEVENTF_VIRTUALDESK)

    def test_a_move_on_a_second_monitor_uses_the_virtual_desktop_origin(self):
        # The fake desktop runs from x = -1280 to x = 1919.
        self.gui.move_mouse(-1280, 0)
        event = self.sent_one()
        self.assertEqual((event.mi.dx, event.mi.dy), (0, 0))

    def test_buttons_use_the_documented_flag_pairs(self):
        pairs = {
            1: (_winapi.MOUSEEVENTF_LEFTDOWN, _winapi.MOUSEEVENTF_LEFTUP),
            3: (_winapi.MOUSEEVENTF_RIGHTDOWN, _winapi.MOUSEEVENTF_RIGHTUP),
            2: (_winapi.MOUSEEVENTF_MIDDLEDOWN, _winapi.MOUSEEVENTF_MIDDLEUP),
        }
        for button, (down, up) in pairs.items():
            with self.subTest(button=button):
                self.gui.press_button(button)
                self.assertEqual(self.sent_one().mi.dwFlags, down)
                self.gui.release_button(button)
                self.assertEqual(self.sent_one().mi.dwFlags, up)

    def test_a_side_button_says_which_one_it_is(self):
        self.gui.press_button(9)
        event = self.sent_one()
        self.assertEqual(event.mi.dwFlags, _winapi.MOUSEEVENTF_XDOWN)
        self.assertEqual(event.mi.mouseData, _winapi.XBUTTON2)

    def test_an_unknown_button_is_a_value_error(self):
        with self.assertRaises(ValueError):
            self.gui.press_button(7)

    def test_scroll_units_are_detents_and_up_is_positive(self):
        self.gui.scroll(0, 2)
        event = self.sent_one()
        self.assertEqual(event.mi.dwFlags, _winapi.MOUSEEVENTF_WHEEL)
        self.assertEqual(event.mi.mouseData, 2 * _winapi.WHEEL_DELTA)

    def test_a_scroll_backwards_wraps_into_the_unsigned_field(self):
        self.gui.scroll(0, -1)
        self.assertEqual(self.sent_one().mi.mouseData, 0xFFFFFF88)

    def test_both_scroll_axes_go_in_one_call(self):
        self.gui.scroll(1, -1)
        flags = [event.mi.dwFlags for event in self.sent_events()]
        self.assertEqual(flags, [_winapi.MOUSEEVENTF_WHEEL, _winapi.MOUSEEVENTF_HWHEEL])
        self.assertEqual(self.sent[0], 2)

    def test_no_scroll_sends_nothing(self):
        self.gui.scroll()
        self.assertIsNone(self.sent)

    def test_a_scroll_too_large_for_one_wheel_message_is_split(self):
        # 300 detents is 36000, which no longer fits the 16 signed bits every
        # consumer reads the delta back through: as one event it arrives as
        # -29536 and scrolls the other way. Each piece has to stay within the
        # limit, and they still have to add up to what was asked for.
        self.gui.scroll(0, 300)
        events = self.sent_events()
        self.assertGreater(len(events), 1)
        deltas = []
        for event in events:
            self.assertEqual(event.mi.dwFlags, _winapi.MOUSEEVENTF_WHEEL)
            delta = event.mi.mouseData
            if delta > 0x7FFFFFFF:
                delta -= 0x100000000
            self.assertLessEqual(abs(delta), 32767)
            deltas.append(delta)
        self.assertEqual(sum(deltas), 300 * _winapi.WHEEL_DELTA)

    def test_a_large_scroll_the_other_way_is_split_the_same_way(self):
        self.gui.scroll(0, -300)
        deltas = []
        for event in self.sent_events():
            delta = event.mi.mouseData - 0x100000000
            self.assertLessEqual(abs(delta), 32767)
            deltas.append(delta)
        self.assertEqual(sum(deltas), -300 * _winapi.WHEEL_DELTA)

    def test_a_key_name_is_resolved_to_a_virtual_key(self):
        names = {
            "Return": "VK_RETURN",
            "BackSpace": "VK_BACK",
            "F5": "VK_F5",
            "space": "VK_SPACE",
            "a": "VK_A",
            "7": "VK_7",
            "-": "VK_OEM_MINUS",
            "kp_5": "VK_NUMPAD5",
            "VK_RETURN": "VK_RETURN",
            "\n": "VK_RETURN",
        }
        for name, expected in names.items():
            with self.subTest(name=name):
                self.gui.press_key(name)
                self.assertEqual(self.sent_one().ki.wVk, VK[expected])

    def test_a_release_says_so_in_the_flags(self):
        self.gui.release_key("Return")
        event = self.sent_one()
        self.assertEqual(event.type, _winapi.INPUT_KEYBOARD)
        self.assertEqual(event.ki.dwFlags, _winapi.KEYEVENTF_KEYUP)

    def test_an_unknown_key_name_is_a_value_error(self):
        with self.assertRaises(ValueError):
            self.gui.press_key("NoSuchKey")

    def test_a_shifted_character_is_refused_rather_than_downgraded(self):
        # Pressing `1` because the caller wrote `!` types the wrong character,
        # which is the failure mode this package exists to avoid.
        with self.assertRaises(ValueError) as raised:
            self.gui.press_key("!")
        self.assertIn("shifted", str(raised.exception))

    def test_type_text_sends_unicode_units_not_scancodes(self):
        # `wScan` carrying the code unit with `wVk` at zero is what makes the
        # active layout irrelevant; a scancode would be resolved through it.
        self.gui.type_text("a")
        down, up = self.sent_events()
        self.assertEqual(down.ki.wScan, ord("a"))
        self.assertEqual(down.ki.wVk, 0)
        self.assertEqual(down.ki.dwFlags, _winapi.KEYEVENTF_UNICODE)
        self.assertEqual(up.ki.wScan, ord("a"))
        self.assertEqual(
            up.ki.dwFlags, _winapi.KEYEVENTF_UNICODE | _winapi.KEYEVENTF_KEYUP
        )

    def test_each_character_is_its_own_call(self):
        # `delay` has to sit between characters, and a character the system
        # refuses should not take the rest of the string down with it.
        self.gui.type_text("aé")
        calls = self.user32.args_for("SendInput")
        self.assertEqual(len(calls), 2)
        self.assertEqual([count for count, _events, _size in calls], [2, 2])

    def test_a_character_outside_the_bmp_is_one_surrogate_pair(self):
        # Two code units, and both halves in one call: split across calls they
        # would be two broken characters. Asserted on the units themselves --
        # `chr` of one half is not the character, so joining them would only
        # be comparing the two surrogates back to themselves.
        self.gui.type_text("\U0001f600")
        events = self.sent_events()
        self.assertEqual(len(events), 4)
        units = [event.ki.wScan for event in events[::2]]
        self.assertEqual(units, [0xD83D, 0xDE00])
        self.assertEqual(
            "".join(chr(unit) for unit in units).encode("utf-16-le", "surrogatepass"),
            "\U0001f600".encode("utf-16-le"),
        )

    def test_a_control_character_goes_as_a_virtual_key(self):
        # An application expecting Return gets a text character otherwise, so a
        # dialog's default button never fires.
        self.gui.type_text("\n")
        down, up = self.sent_events()
        self.assertEqual(down.ki.wVk, VK["VK_RETURN"])
        self.assertEqual(down.ki.dwFlags, 0)
        self.assertEqual(up.ki.dwFlags, _winapi.KEYEVENTF_KEYUP)

    def test_the_keymap_flag_is_accepted_and_ignored(self):
        # Nothing here injects scancodes, so there is no keymap to be unsafe
        # about -- but the argument must not be a TypeError either.
        self.gui.type_text("a", allow_keymap_unsafe=False)
        down, _up = self.sent_events()
        self.assertEqual(down.ki.dwFlags, _winapi.KEYEVENTF_UNICODE)

    def test_a_blocked_send_is_reported_as_a_permission_problem(self):
        # The documented hole: UIPI returns the full count and delivers
        # nothing. Zero is the case that at least says so, and it is reported
        # with the reason rather than a bare count.
        self.send_count = 0
        with self.assertRaises(PermissionRequired) as raised:
            self.gui.press_button(1)
        self.assertIs(raised.exception.capability, Capability.POINTER_BUTTON)
        self.assertIn("UIPI", raised.exception.reason)

    def test_a_partial_delivery_says_how_many_arrived(self):
        self.send_count = 1
        with self.assertRaises(PermissionRequired) as raised:
            self.gui.type_text("ab")
        self.assertIn("1 of 2", raised.exception.reason)


class TestStateQueries(Win32TestCase):
    """The tier-6 reads: pointer position, buttons, keys, cursor."""

    def test_the_pointer_position_is_where_windows_says_it_is(self):
        self.cursor = (1234, 567)
        self.assertEqual(self.gui.pointer_position(), (1234, 567))

    def test_a_held_button_is_reported_held(self):
        self.keys_down = {_winapi.VK_LBUTTON}
        self.assertTrue(self.gui.is_button_pressed(1))
        self.assertFalse(self.gui.is_button_pressed(3))

    def test_an_unknown_button_is_a_value_error(self):
        with self.assertRaises(ValueError):
            self.gui.is_button_pressed(12)

    def test_the_low_bit_of_the_key_state_is_ignored(self):
        # 0x0001 is "it was pressed since the last call", which is a different
        # question from "is it held" -- and a suite polling twice would get
        # wrong answers from it.
        with mock.patch.object(
            self.user32, "GetAsyncKeyState", lambda vk: 1, create=True
        ):
            self.assertFalse(self.gui.is_key_pressed("Return"))

    def test_a_modifier_is_asked_about_side_agnostically(self):
        # VK_SHIFT is what Windows reports for either Shift, so that is the
        # question a caller asking whether Shift is held is really asking.
        self.keys_down = {_winapi.VK_SHIFT}
        self.assertTrue(self.gui.is_key_pressed("Shift_L"))
        # The left-specific code is not consulted, because a state query that
        # asked about it would say False while Shift is plainly down.
        self.keys_down = {VK["VK_LSHIFT"]}
        self.assertFalse(self.gui.is_key_pressed("Shift_L"))

    def test_a_key_pressed_by_name_is_the_key_that_is_read_back(self):
        self.keys_down = {VK["VK_RETURN"]}
        self.assertTrue(self.gui.is_key_pressed("Return"))

    def cursor_window(self):
        """One window, the pointer inside it, showing an arrow."""
        self.add_window(1, rect=(10, 20, 810, 620))
        window = self.gui.windows()[0]
        self.cursor = (100, 100)
        self.cursor_handle = _winapi.IDC_ARROW
        return window

    def test_the_cursor_a_window_is_showing_is_matched_by_handle(self):
        window = self.cursor_window()
        self.assertTrue(self.gui.is_window_cursor(window, 68))

    def test_a_different_cursor_is_not_the_one_asked_about(self):
        window = self.cursor_window()
        self.cursor_handle = _winapi.IDC_WAIT
        self.assertFalse(self.gui.is_window_cursor(window, 68))

    def test_the_pointer_outside_the_window_answers_false(self):
        # Windows has no per-window cursor to read, so a pointer somewhere else
        # cannot be an answer about this window.
        window = self.cursor_window()
        self.cursor = (5, 5)
        self.assertFalse(self.gui.is_window_cursor(window, 68))

    def test_a_hidden_cursor_is_no_cursor(self):
        window = self.cursor_window()
        self.cursor_showing = 0
        self.assertFalse(self.gui.is_window_cursor(window, 68))

    def test_a_shape_with_no_counterpart_is_refused(self):
        window = self.cursor_window()
        with self.assertRaises(ValueError) as raised:
            self.gui.is_window_cursor(window, 130)
        self.assertIn("no Windows counterpart", str(raised.exception))

    def test_a_corner_resize_shape_is_refused_with_its_own_reason(self):
        # These four are the ones a script written against X11 is most likely
        # to pass, and a bare list of known numbers would not tell its author
        # why theirs is missing -- Windows shows one cursor per diagonal, so
        # this corner and the opposite one are the same handle here. Mapping
        # them would answer True with the pointer on the wrong corner.
        window = self.cursor_window()
        for shape, corner in ((134, "top left"), (14, "bottom right")):
            with self.subTest(shape=shape):
                with self.assertRaises(ValueError) as raised:
                    self.gui.is_window_cursor(window, shape)
                message = str(raised.exception)
                self.assertIn(corner, message)
                self.assertIn("diagonal", message)

    def test_every_corner_shape_is_refused_rather_than_silently_mapped(self):
        window = self.cursor_window()
        for shape in _DIAGONAL_SHAPES:
            with self.subTest(shape=shape):
                self.assertNotIn(shape, _CURSOR_SHAPES)
                with self.assertRaises(ValueError):
                    self.gui.is_window_cursor(window, shape)


class TestCapture(Win32TestCase):
    """GDI capture: the rectangle, the flags, and the pixels' own order."""

    def small_desktop(self):
        """A two-by-two desktop, so a test writes a two-by-two image."""
        self.metrics[_winapi.SM_XVIRTUALSCREEN] = 0
        self.metrics[_winapi.SM_YVIRTUALSCREEN] = 0
        self.metrics[_winapi.SM_CXVIRTUALSCREEN] = 2
        self.metrics[_winapi.SM_CYVIRTUALSCREEN] = 2
        self.set_pixels([b"\xff\x00\x00\x00\xff\x00", b"\x00\x00\xff\xff\xff\xff"])

    def test_a_whole_capture_is_the_virtual_desktop(self):
        self.small_desktop()
        width, height, _rows = self.decode_png(
            self.gui.capture(path=self.temporary_path())
        )
        self.assertEqual((width, height), (2, 2))

    def test_the_rows_come_out_top_down_and_rgb(self):
        # GetDIBits hands back bottom-up BGR; both have to be undone, and this
        # is the assertion that says so rather than asserting the arithmetic.
        self.small_desktop()
        _width, _height, rows = self.decode_png(
            self.gui.capture(path=self.temporary_path())
        )
        self.assertEqual(
            rows, [b"\xff\x00\x00\x00\xff\x00", b"\x00\x00\xff\xff\xff\xff"]
        )

    def test_layered_windows_are_included(self):
        # Without CAPTUREBLT every window drawn with WS_EX_LAYERED is missing
        # from the result, which is most modern application windows.
        self.small_desktop()
        self.gui.capture(path=self.temporary_path())
        self.assertEqual(
            self.gdi32.arg_for("BitBlt")[-1], _winapi.SRCCOPY | _winapi.CAPTUREBLT
        )

    def test_a_region_is_the_rectangle_asked_for(self):
        self.small_desktop()
        self.gui.capture(region=(0, 0, 2, 1), path=self.temporary_path())
        _dc, x, y, width, height = self.gdi32.arg_for("BitBlt")[:5]
        self.assertEqual((x, y, width, height), (0, 0, 2, 1))

    def test_the_gdi_objects_are_released_afterwards(self):
        self.small_desktop()
        self.gui.capture(path=self.temporary_path())
        self.assertTrue(self.gdi32.was_called("DeleteObject"))
        self.assertTrue(self.gdi32.was_called("DeleteDC"))
        self.assertTrue(self.user32.was_called("ReleaseDC"))

    def test_an_empty_region_is_refused_before_anything_is_blitted(self):
        self.small_desktop()
        with self.assertRaises(ValueError):
            self.gui.capture(region=(0, 0, 0, 5))
        self.assertFalse(self.gdi32.was_called("BitBlt"))

    def test_a_capture_with_no_path_writes_a_temporary_file(self):
        self.small_desktop()
        path = self.gui.capture()
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        self.assertTrue(path.endswith(".png"))
        self.assertEqual(self.decode_png(path)[:2], (2, 2))

    def test_a_machine_without_gdi32_cannot_capture(self):
        self.small_desktop()
        with mock.patch.object(_winapi, "gdi32", lambda: None):
            with self.assertRaises(BackendUnavailable):
                self.gui.capture(path=self.temporary_path())

    def test_a_blit_failure_names_a_non_interactive_window_station(self):
        # Measured on a real Windows 11 box over ssh, where every
        # desktop-bound call failed with nothing in the message about the one
        # thing that explained all of them -- and which detect() had already
        # worked out.
        self.small_desktop()
        self.gui.environment = types.SimpleNamespace(is_interactive_desktop=False)
        self.gdi32.BitBlt = lambda *args: 0
        with self.assertRaises(PyGUITestError) as raised:
            self.gui.capture(path=self.temporary_path())
        self.assertIn("window station", str(raised.exception))

    def test_a_blit_failure_on_a_real_desktop_says_only_what_it_knows(self):
        # The note is a diagnosis, not decoration: appending it to a failure it
        # does not explain would send a caller after the wrong thing.
        self.small_desktop()
        self.gui.environment = types.SimpleNamespace(is_interactive_desktop=True)
        self.gdi32.BitBlt = lambda *args: 0
        with self.assertRaises(PyGUITestError) as raised:
            self.gui.capture(path=self.temporary_path())
        self.assertNotIn("window station", str(raised.exception))

    def test_a_bitmap_that_will_not_select_is_a_failure_not_a_black_image(self):
        # A failed selection leaves the DC's original 1x1 surface in place and
        # BitBlt then succeeds against that, so without this check the capture
        # would be a plausible-looking black PNG.
        self.small_desktop()
        self.gdi32.SelectObject = lambda hdc, obj: 0
        with self.assertRaises(PyGUITestError):
            self.gui.capture(path=self.temporary_path())

    def test_the_bitmap_is_deselected_before_its_pixels_are_read_back(self):
        # GetDIBits documents that the bitmap must not be selected into a
        # device context when it is called, while BitBlt needs exactly that --
        # so the order of the two is a requirement rather than a preference.
        self.small_desktop()
        selected = [None]
        order = []
        real_get_dib_bits = self.gdi32.GetDIBits

        def select(hdc, obj):
            previous, selected[0] = selected[0], obj
            order.append(("select", obj))
            return previous or 3

        def get_dib_bits(*args):
            order.append(("getdibits", selected[0]))
            return real_get_dib_bits(*args)

        self.gdi32.SelectObject = select
        self.gdi32.GetDIBits = get_dib_bits
        self.gui.capture(path=self.temporary_path())
        self.assertIn(("getdibits", 3), order)
        self.assertNotIn(("getdibits", 2), order)


class TestClipboard(Win32TestCase):
    """One clipboard: the round trip, the retry, and the refusal for PRIMARY."""

    def test_text_written_can_be_read_back(self):
        # A real round trip through the fake global heap, so the UTF-16
        # encoding and the terminating NUL are both exercised.
        self.gui.set_clipboard("hello — wörld")
        self.assertEqual(self.gui.get_clipboard(), "hello — wörld")

    def test_a_second_write_replaces_the_first(self):
        self.gui.set_clipboard("first")
        self.gui.set_clipboard("second")
        self.assertEqual(self.gui.get_clipboard(), "second")

    def test_an_empty_clipboard_of_text_is_its_own_failure(self):
        # An owner publishing only CF_TEXT or CF_HTML is the real case, and an
        # empty string would be indistinguishable from an empty clipboard.
        with self.assertRaises(PyGUITestError) as raised:
            self.gui.get_clipboard()
        self.assertIn("CF_UNICODETEXT", str(raised.exception))

    def test_primary_has_no_meaning_and_says_so(self):
        with self.assertRaises(CapabilityUnsupported) as raised:
            self.gui.get_clipboard(primary=True)
        self.assertIs(raised.exception.capability, Capability.CLIPBOARD)
        self.assertIn("PRIMARY", raised.exception.reason)

    def test_primary_on_a_write_is_refused_the_same_way(self):
        with self.assertRaises(CapabilityUnsupported) as raised:
            self.gui.set_clipboard("text", primary=True)
        self.assertIs(raised.exception.capability, Capability.CLIPBOARD)
        self.assertIn("PRIMARY", raised.exception.reason)

    def test_a_held_clipboard_is_retried_before_giving_up(self):
        self.open_refusals = 5
        with mock.patch("pyguitest.backends.win32.time.sleep") as sleep:
            with self.assertRaises(PermissionRequired) as raised:
                self.gui.set_clipboard("text")
        self.assertEqual(len(sleep.call_args_list), 5)
        self.assertIn("holding the clipboard open", raised.exception.reason)

    def test_a_held_clipboard_can_be_retried_successfully(self):
        self.open_refusals = 2
        with mock.patch("pyguitest.backends.win32.time.sleep"):
            self.gui.set_clipboard("text")
        self.assertEqual(self.gui.get_clipboard(), "text")

    def test_a_refused_set_frees_the_block_it_allocated(self):
        # SetClipboardData transfers ownership; a call that fails does not, so
        # the block is this process's to free -- and leaking it would be the
        # invisible half of that mistake.
        self.set_clipboard_ok = False
        with self.assertRaises(PyGUITestError):
            self.gui.set_clipboard("text")
        self.assertEqual(self.memory.blocks, {})

    def test_a_successful_set_leaves_the_block_to_the_system(self):
        self.gui.set_clipboard("text")
        self.assertEqual(len(self.memory.blocks), 1)
