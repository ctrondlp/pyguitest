"""Session detection: display server, compositor, desktop, and input prefs.

Everything downstream is decided from this, so the tests build the
environment they mean to describe rather than reading the one they run on --
the suite has to give the same answers on an X11 box and a Wayland one.
"""

import ctypes
import dataclasses
import subprocess
import unittest
from unittest import mock

from pyguitest.session import Compositor, SessionType, detect


def linux_detect(variables):
    """`detect()` as it would answer on Linux, from any machine.

    `_classify` asks `_platform()` first and, on Windows, asks nothing else --
    so without this pin every fixture here became a WIN32 session when the
    suite ran on Windows, and the classification, compositor and input-
    preference tests all asserted against the wrong platform. `detect()`'s own
    docstring names patching `_platform` as the way to reach a platform you are
    not on, and `TestWindowsSession` does exactly that in the other direction.
    """
    with mock.patch("pyguitest.session._platform", return_value="linux"):
        return detect(variables)


def env(**kw):
    """A bare fake environment; linux_detect() reads only what is passed."""
    return kw


class _Function:
    """A callable that also accepts restype/argtypes, as a ctypes function does.

    Every probe in `session.py` declares both before calling, exactly as it
    must against a real DLL. A plain bound method refuses those assignments,
    and the probe then reads the refusal as a failure -- which is how the
    first version of the window-station fake passed one test and failed
    another.
    """

    def __init__(self, function):
        self.function = function

    def __call__(self, *args):
        return self.function(*args)


def token_label(*subauthorities):
    """A stand-in TOKEN_MANDATORY_LABEL: one SID_AND_ATTRIBUTES plus a SID.

    Built to the layout the documentation describes rather than to anything
    measured, because what the tests using it pin is this package's own
    arithmetic over that layout: `_integrity_rid` reads SubAuthorityCount as
    one byte at offset 1 and takes the last 32-bit subauthority from offset 8,
    and this is what writes both.
    """
    sid = ctypes.create_string_buffer(8 + 4 * len(subauthorities))
    sid[0] = b"\x01"  # SID revision
    sid[1] = bytes([len(subauthorities)])  # SubAuthorityCount
    for index, value in enumerate(subauthorities):
        where = ctypes.cast(
            ctypes.byref(sid, 8 + 4 * index), ctypes.POINTER(ctypes.c_uint32)
        )
        where[0] = value
    label = ctypes.create_string_buffer(ctypes.sizeof(ctypes.c_void_p) + 4)
    pointer = ctypes.cast(label, ctypes.POINTER(ctypes.c_void_p))
    pointer[0] = ctypes.cast(sid, ctypes.c_void_p).value
    return label


class TestSessionClassification(unittest.TestCase):
    def test_pure_wayland(self):
        e = linux_detect(env(WAYLAND_DISPLAY="wayland-0", XDG_CURRENT_DESKTOP="GNOME"))
        self.assertIs(e.session_type, SessionType.WAYLAND)
        self.assertIs(e.compositor, Compositor.MUTTER)

    def test_xwayland_is_distinguished_from_both(self):
        # Both variables set means an X11 connection inside a Wayland session,
        # where XTest reaches X11 clients but never native Wayland ones.
        e = linux_detect(env(WAYLAND_DISPLAY="wayland-0", DISPLAY=":0"))
        self.assertIs(e.session_type, SessionType.XWAYLAND)
        self.assertTrue(any("XWayland" in n for n in e.notes))

    def test_pure_x11(self):
        e = linux_detect(env(DISPLAY=":0", XDG_SESSION_TYPE="x11"))
        self.assertIs(e.session_type, SessionType.X11)

    def test_display_wins_over_a_misdeclared_xdg_session_type(self):
        # Regression: observed live on a machine offering several session
        # types at login (Plasma X11, Plasma Wayland, GNOME). Logind
        # reported XDG_SESSION_TYPE=wayland for a session the user had
        # explicitly chosen as plasmax11 (DESKTOP_SESSION=plasmax11),
        # DISPLAY=:0 set, WAYLAND_DISPLAY entirely absent -- no Wayland
        # socket existed at all. The declared type used to be checked
        # before DISPLAY, so this reported WAYLAND on a real X11 session,
        # and every X11Backend-only capability read [ no] as a result.
        e = linux_detect(
            env(
                DISPLAY=":0",
                XDG_SESSION_TYPE="wayland",
                DESKTOP_SESSION="plasmax11",
                XDG_SESSION_DESKTOP="plasmax11",
                XDG_CURRENT_DESKTOP="KDE",
            )
        )
        self.assertIs(e.session_type, SessionType.X11)

    def test_headless(self):
        e = linux_detect(env())
        self.assertIs(e.session_type, SessionType.HEADLESS)
        self.assertIs(e.compositor, Compositor.NONE)


class TestCompositorDetection(unittest.TestCase):
    def test_kde(self):
        e = linux_detect(env(WAYLAND_DISPLAY="wayland-0", XDG_CURRENT_DESKTOP="KDE"))
        self.assertIs(e.compositor, Compositor.KWIN)

    def test_wlroots_by_desktop_name(self):
        for desktop in ("sway", "Hyprland", "river", "niri"):
            with self.subTest(desktop=desktop):
                e = linux_detect(
                    env(WAYLAND_DISPLAY="wayland-0", XDG_CURRENT_DESKTOP=desktop)
                )
                self.assertIs(e.compositor, Compositor.WLROOTS)

    def test_wlroots_by_socket(self):
        e = linux_detect(env(WAYLAND_DISPLAY="wayland-0", SWAYSOCK="/run/sway.sock"))
        self.assertIs(e.compositor, Compositor.WLROOTS)

    def test_mutter_warns_about_foreign_toplevel(self):
        e = linux_detect(env(WAYLAND_DISPLAY="wayland-0", XDG_CURRENT_DESKTOP="GNOME"))
        self.assertTrue(any("foreign-toplevel" in n for n in e.notes))

    def test_unnamed_wayland_compositor_is_other_not_none(self):
        # Regression: NONE means "no compositor", which contradicts
        # WAYLAND_DISPLAY being set at all -- a Wayland session cannot exist
        # without one, even when nothing names which one.
        e = linux_detect(env(WAYLAND_DISPLAY="wayland-0"))
        self.assertIs(e.compositor, Compositor.OTHER)


class TestDesktopName(unittest.TestCase):
    def test_xdg_current_desktop_wins(self):
        e = linux_detect(
            env(
                DISPLAY=":0",
                XDG_CURRENT_DESKTOP="XFCE",
                XDG_SESSION_DESKTOP="xfce",
                DESKTOP_SESSION="xfce",
            )
        )
        self.assertEqual(e.desktop, "XFCE")

    def test_falls_back_to_xdg_session_desktop(self):
        e = linux_detect(env(DISPLAY=":0", XDG_SESSION_DESKTOP="xfce"))
        self.assertEqual(e.desktop, "xfce")

    def test_falls_back_to_desktop_session(self):
        # A bare startx/~/.xinitrc launch: no display manager sets either
        # XDG variable, only the legacy one some window managers export.
        e = linux_detect(env(DISPLAY=":0", DESKTOP_SESSION="i3"))
        self.assertEqual(e.desktop, "i3")

    def test_empty_when_nothing_names_it(self):
        e = linux_detect(env(DISPLAY=":0"))
        self.assertEqual(e.desktop, "")


class TestAssistiveTechnologyProbe(unittest.TestCase):
    """Why a Chromium window can be listed but have no elements at all."""

    def _answer(self, **kwargs):
        from pyguitest import session

        with mock.patch.object(session.subprocess, "run", **kwargs):
            return session.assistive_technology_enabled()

    def test_true_and_false_are_read_out_of_the_variant(self):
        # gdbus prints a property as a variant: "(<true>,)".
        self.assertIs(
            self._answer(return_value=mock.Mock(returncode=0, stdout="(<true>,)\n")),
            True,
        )
        self.assertIs(
            self._answer(return_value=mock.Mock(returncode=0, stdout="(<false>,)\n")),
            False,
        )

    def test_no_accessibility_bus_answers_none_not_false(self):
        # "Could not ask" and "an AT is not running" are different facts,
        # and only the second one explains an empty element tree.
        self.assertIsNone(self._answer(return_value=mock.Mock(returncode=1, stdout="")))

    def test_a_missing_gdbus_answers_none(self):
        self.assertIsNone(self._answer(side_effect=OSError))

    def test_a_hang_answers_none_rather_than_raising(self):
        self.assertIsNone(
            self._answer(side_effect=subprocess.TimeoutExpired(cmd="gdbus", timeout=5))
        )

    def test_unexpected_output_answers_none(self):
        self.assertIsNone(
            self._answer(return_value=mock.Mock(returncode=0, stdout="who knows"))
        )


class TestInputPreference(unittest.TestCase):
    def setUp(self):
        self.wayland = linux_detect(env(WAYLAND_DISPLAY="wayland-0"))

    def test_preferred_input_is_the_highest_ranked_present_tool(self):
        e = dataclasses.replace(self.wayland, input_tools=("wdotool", "ydotool"))
        self.assertEqual(e.preferred_input, "wdotool")
        self.assertTrue(e.can_inject_input)

    def test_no_tools_means_no_input(self):
        bare = dataclasses.replace(
            self.wayland,
            input_tools=(),
            has_libei=False,
            has_portal=False,
            uinput_writable=False,
        )
        self.assertIsNone(bare.preferred_input)
        self.assertFalse(bare.can_inject_input)

    def test_libei_without_a_tool_still_counts_as_injectable(self):
        # libei is usable through a backend even with no CLI on PATH; it
        # just has no adapter yet.
        e = dataclasses.replace(self.wayland, input_tools=(), has_libei=True)
        self.assertTrue(e.can_inject_input)
        self.assertIsNone(e.preferred_input)

    def test_transport_names_the_tool_when_a_keymap_safe_one_is_present(self):
        e = dataclasses.replace(self.wayland, input_tools=("wdotool", "ydotool"))
        self.assertEqual(e.input_transport, "wdotool")

    def test_transport_names_uinput_where_preferred_input_says_nothing(self):
        # The reported bug: `summary()` printed "input none available" on a
        # box injecting perfectly well through in-process uinput, while the
        # `mechanisms` line two rows below it said `uinput`.
        e = dataclasses.replace(
            self.wayland, input_tools=(), uinput_writable=True, has_evdev=True
        )
        self.assertIsNone(e.preferred_input)
        self.assertEqual(e.input_transport, "uinput (in-process)")
        self.assertIn("uinput", e.summary())
        self.assertNotIn("none available", e.summary())

    def test_uinput_outranks_a_keymap_unsafe_tool(self):
        # Mirrors backends._input_factory, which prefers in-process uinput
        # to ydotool: same keymap limitation, without a process per event.
        e = dataclasses.replace(
            self.wayland,
            input_tools=("ydotool",),
            uinput_writable=True,
            has_evdev=True,
        )
        self.assertEqual(e.preferred_input, "ydotool")
        self.assertEqual(e.input_transport, "uinput (in-process)")

    def test_a_keymap_unsafe_tool_still_beats_nothing(self):
        e = dataclasses.replace(
            self.wayland,
            input_tools=("ydotool",),
            uinput_writable=False,
            has_evdev=False,
        )
        self.assertEqual(e.input_transport, "ydotool")

    def test_libei_is_named_last_and_marked_opt_in(self):
        # It is never chosen by automatic composition -- naming it without
        # the caveat would describe a session nobody gets by default.
        e = dataclasses.replace(
            self.wayland,
            input_tools=(),
            has_libei=True,
            uinput_writable=False,
        )
        self.assertIn("libei", e.input_transport)
        self.assertIn("opt-in", e.input_transport)

    def test_no_transport_at_all_is_still_none(self):
        bare = dataclasses.replace(
            self.wayland,
            input_tools=(),
            has_libei=False,
            has_portal=False,
            uinput_writable=False,
            has_evdev=False,
        )
        self.assertIsNone(bare.input_transport)
        self.assertIn("input        none available", bare.summary())

    def test_portal_alone_does_not_count_as_injectable(self):
        # Regression: has_portal used to count on its own, but nothing in
        # this package can actually drive a portal transport -- there is no
        # libei binding here, only external CLI tools and in-process
        # uinput. Reporting "injectable" here suppressed the "install an
        # input tool" hint on a machine that cannot inject at all.
        e = dataclasses.replace(
            self.wayland,
            input_tools=(),
            has_libei=False,
            has_portal=True,
            uinput_writable=False,
        )
        self.assertFalse(e.can_inject_input)


class TestToolDiscoveryMatchesTheSession(unittest.TestCase):
    """What `linux_detect()` lists has to be what a backend could actually use.

    `input_tools` is not decoration: `preferred_input` reads it, `doctor`
    prints it, and `_input_factory` makes the same discovery call to pick
    a real backend. A tool listed here that cannot work on this session is
    a wrong answer in all three places.
    """

    def _tools(self, environment):
        with mock.patch(
            "pyguitest.tools.shutil.which", lambda name: f"/usr/bin/{name}"
        ):
            return linux_detect(environment).input_tools

    def test_a_wlroots_only_tool_is_not_listed_for_a_plain_x11_session(self):
        # Regression: the wlroots gate was `compositor is WLROOTS or
        # x11_session`, so wtype was listed wherever an X display existed
        # -- including a session with no Wayland compositor at all.
        tools_found = self._tools(env(DISPLAY=":0", XDG_SESSION_TYPE="x11"))
        self.assertNotIn("wtype", tools_found)
        self.assertIn("xdotool", tools_found)

    def test_a_wlroots_only_tool_is_not_listed_for_gnome_xwayland(self):
        tools_found = self._tools(
            env(WAYLAND_DISPLAY="wayland-0", DISPLAY=":0", XDG_CURRENT_DESKTOP="GNOME")
        )
        self.assertNotIn("wtype", tools_found)

    def test_a_wlroots_session_still_lists_its_own_tools(self):
        tools_found = self._tools(
            env(WAYLAND_DISPLAY="wayland-0", XDG_CURRENT_DESKTOP="sway")
        )
        self.assertIn("wtype", tools_found)

    def test_an_x11_only_tool_is_not_listed_for_a_pure_wayland_session(self):
        tools_found = self._tools(
            env(WAYLAND_DISPLAY="wayland-0", XDG_CURRENT_DESKTOP="sway")
        )
        self.assertNotIn("xdotool", tools_found)


class TestAtspiDetection(unittest.TestCase):
    def test_c_library_alone_is_not_reported_as_usable(self):
        # The bug this guards: find_library("atspi") succeeds on most desktops
        # while the Python binding is missing, and an empty namespace directory
        # makes find_spec("gi") succeed too.
        e = linux_detect(env(WAYLAND_DISPLAY="wayland-0"))
        both = dataclasses.replace(
            e, has_atspi=True, has_pygobject=True, has_dogtail=True
        )
        self.assertTrue(both.can_use_atspi)
        c_only = dataclasses.replace(
            e, has_atspi=True, has_pygobject=False, has_dogtail=True
        )
        self.assertFalse(c_only.can_use_atspi)

    def test_distro_halves_alone_are_not_reported_as_usable(self):
        # Regression: libatspi and PyGObject both come from the distro and
        # can be present with dogtail -- the one part pip actually
        # installs -- still missing. AtspiBackend cannot construct without
        # it, so reporting can_use_atspi True here silences the hint that
        # says to `pip install pyguitest[atspi]`.
        e = linux_detect(env(WAYLAND_DISPLAY="wayland-0"))
        no_dogtail = dataclasses.replace(
            e, has_atspi=True, has_pygobject=True, has_dogtail=False
        )
        self.assertFalse(no_dogtail.can_use_atspi)


class TestPortalDetectionHonoursTheFakeEnvironment(unittest.TestCase):
    """has_portal must read the injected env, not the real process one.

    Regression: it used to read os.environ directly, so a fake env passed
    to linux_detect() for a test silently inherited the real host's D-Bus session
    instead of the one being simulated.
    """

    def test_no_dbus_session_in_the_fake_env_means_no_portal(self):
        # Patch the real process environment to have a bus address, so the
        # only way this could pass is by actually reading the fake `env`.
        with mock.patch.dict(
            "os.environ", {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/bus"}
        ):
            e = linux_detect(env(WAYLAND_DISPLAY="wayland-0"))
        self.assertFalse(e.has_portal)

    def test_dbus_session_in_the_fake_env_is_read(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch("shutil.which", return_value="/usr/bin/xdg-desktop-portal"):
                e = linux_detect(
                    env(
                        WAYLAND_DISPLAY="wayland-0",
                        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/fake-bus",
                    )
                )
        self.assertTrue(e.has_portal)


class TestTheForegroundIntegrityProbe(unittest.TestCase):
    """`_foreground_is_elevated`, driven through a fake set of DLLs.

    Three answers, and all three matter: True for a target that outranks this
    process, False for an ordinary one, and None -- never False -- for every
    way the question can fail to be answerable. The hint that reads this stays
    silent on None, so a protected process declining to be asked about must
    not come back looking like a finding.

    One fake serves as user32, kernel32 and advapi32: no function name appears
    in two of them, and the probe takes them one name at a time through
    `_win32_lib`.
    """

    class _FakeLibraries:
        """What the three DLLs answer, and what the probe then did with them."""

        def __init__(
            self,
            rid=0x2000,
            window=0x10,
            pid=4242,
            process=0xA1,
            token=0xB2,
            read_fails=False,
        ):
            self.rid = rid
            self.window = window
            self.pid = pid
            self.process = process
            self.token = token
            self.read_fails = read_fails
            self.opened = None
            self.closed = []
            self.GetForegroundWindow = _Function(lambda: self.window)
            self.GetWindowThreadProcessId = _Function(self._thread_of)
            self.OpenProcess = _Function(self._open_process)
            self.OpenProcessToken = _Function(self._open_token)
            self.GetTokenInformation = _Function(self._token_information)
            self.CloseHandle = _Function(self._close)

        def _thread_of(self, _window, pid):
            """Write this fake's pid out, the way the real call writes it."""
            ctypes.cast(pid, ctypes.POINTER(ctypes.c_ulong))[0] = self.pid
            return 1

        def _open_process(self, access, _inherit, pid):
            self.opened = (access, pid)
            # A ctypes handle, not a bare integer: the probe hands it back to
            # CloseHandle, exactly as it does with a real OpenProcess result.
            return ctypes.c_void_p(self.process or None)

        def _open_token(self, _process, _access, token):
            if not self.token:
                return 0
            ctypes.cast(token, ctypes.POINTER(ctypes.c_void_p))[0] = self.token
            return 1

        def _token_information(self, _token, _class, buffer, _size, needed):
            """Size on the first call -- buffer None -- then hand the label over."""
            label = token_label(16, self.rid)
            ctypes.cast(needed, ctypes.POINTER(ctypes.c_ulong))[0] = ctypes.sizeof(
                label
            )
            if buffer is None or self.read_fails:
                return 0
            ctypes.memmove(buffer, label, ctypes.sizeof(label))
            return 1

        def _close(self, handle):
            self.closed.append(handle)
            return 1

    def _answer(self, **kwargs):
        from pyguitest import session

        libraries = self._FakeLibraries(**kwargs)
        with mock.patch.object(session, "_win32_lib", lambda _name: libraries):
            return session._foreground_is_elevated(), libraries

    @staticmethod
    def _closed(libraries):
        """The handles handed to CloseHandle, as plain integers.

        The probe passes ctypes objects through, as it must with a real DLL,
        so they are unwrapped here rather than compared as objects.
        """
        return sorted(int(handle.value or 0) for handle in libraries.closed)

    def test_a_target_that_outranks_this_process_reads_true(self):
        answer, _ = self._answer(rid=0x3000)
        self.assertIs(answer, True)

    def test_an_ordinary_target_reads_false(self):
        answer, _ = self._answer(rid=0x2000)
        self.assertIs(answer, False)

    def test_the_query_uses_the_right_that_reaches_upwards(self):
        # PROCESS_QUERY_LIMITED_INFORMATION, not PROCESS_QUERY_INFORMATION:
        # the point of asking about a process that outranks this one is that
        # the older right is refused for exactly that case.
        _, libraries = self._answer()
        self.assertEqual(libraries.opened, (0x1000, 4242))

    def test_no_foreground_window_is_cannot_tell(self):
        answer, libraries = self._answer(window=0)
        self.assertIsNone(answer)
        # Nothing was opened, so there is nothing to have failed to close.
        self.assertIsNone(libraries.opened)
        self.assertEqual(libraries.closed, [])

    def test_a_process_that_will_not_open_is_cannot_tell(self):
        # Access denied is the ordinary answer for a protected process, and it
        # is not the same finding as a target that is elevated.
        answer, _ = self._answer(process=0)
        self.assertIsNone(answer)

    def test_a_token_that_will_not_open_is_cannot_tell(self):
        answer, _ = self._answer(token=0)
        self.assertIsNone(answer)

    def test_a_failed_read_is_cannot_tell_rather_than_not_elevated(self):
        answer, _ = self._answer(read_fails=True)
        self.assertIsNone(answer)

    def test_every_handle_that_was_opened_is_closed_again(self):
        _, libraries = self._answer()
        self.assertEqual(self._closed(libraries), [0xA1, 0xB2])

    def test_the_handles_are_closed_even_when_the_read_fails(self):
        # linux_detect() runs in every process that connects, and a token left open
        # is exactly the side effect this module refuses to have.
        _, libraries = self._answer(read_fails=True)
        self.assertEqual(self._closed(libraries), [0xA1, 0xB2])

    def test_a_dll_that_will_not_load_is_cannot_tell(self):
        from pyguitest import session

        with mock.patch.object(session, "_win32_lib", lambda _name: None):
            self.assertIsNone(session._foreground_is_elevated())


class TestCaptureDetection(unittest.TestCase):
    """can_capture: a CLI tool is only one of three routes to pixels.

    Reporting "no screen capture" purely on an empty capture_tools sends
    someone off to install gnome-screenshot on a session that can already
    capture -- an X11 login with python-xlib, or any desktop with the
    Screenshot portal.
    """

    def _env(self, **overrides):
        base = linux_detect(env(WAYLAND_DISPLAY="wayland-0"))
        blank = {
            "capture_tools": (),
            "has_xlib": False,
            "has_portal": False,
            "has_pygobject": False,
        }
        return dataclasses.replace(base, **{**blank, **overrides})

    def test_a_tool_is_enough(self):
        self.assertTrue(self._env(capture_tools=("grim",)).can_capture)

    def test_python_xlib_on_a_real_x_session_is_enough(self):
        self.assertTrue(
            self._env(has_xlib=True, session_type=SessionType.X11).can_capture
        )

    def test_python_xlib_under_xwayland_is_not_enough(self):
        # X11Backend withdraws SCREEN_CAPTURE under XWayland -- the X root
        # window does not contain the Wayland desktop -- so counting it
        # here would suppress the "install a screenshot tool" hint on
        # precisely the session that needs it.
        self.assertFalse(
            self._env(has_xlib=True, session_type=SessionType.XWAYLAND).can_capture
        )

    def test_python_xlib_without_an_x_session_is_not(self):
        # There is no X connection to make, so X11Backend cannot be built.
        self.assertFalse(
            self._env(has_xlib=True, session_type=SessionType.WAYLAND).can_capture
        )

    def test_the_portal_needs_both_halves(self):
        self.assertTrue(self._env(has_portal=True, has_pygobject=True).can_capture)
        self.assertFalse(self._env(has_portal=True, has_pygobject=False).can_capture)
        self.assertFalse(self._env(has_portal=False, has_pygobject=True).can_capture)

    def test_nothing_at_all_reports_no_capture(self):
        self.assertFalse(self._env().can_capture)


class TestWindowsSession(unittest.TestCase):
    """A Windows session, driven from a Linux machine.

    `_platform` is the seam, and patching it is what takes the Windows branch.
    Everything the probes then report is read from this host, so what is
    asserted is the *shape* of a Windows answer -- the classification order,
    the properties that were Linux-shaped until now, and the facts the summary
    reports -- rather than values only a Windows machine has.
    """

    def _detect(self, **variables):
        from pyguitest import session

        with mock.patch.object(session, "_platform", lambda: "win32"):
            return detect(env(**variables))

    def test_the_platform_wins_over_a_display_that_is_set(self):
        # Xming, VcXsrv and Exceed all set DISPLAY on Windows, and a Cygwin or
        # MSYS2 session sets both it and WAYLAND_DISPLAY. None of them means
        # what it means on Linux, and reading them first handed X11Backend a
        # fraction of the desktop while reporting full support for it.
        e = self._detect(
            DISPLAY=":0",
            WAYLAND_DISPLAY="wayland-0",
            XDG_SESSION_TYPE="x11",
            XDG_CURRENT_DESKTOP="GNOME",
        )
        self.assertIs(e.session_type, SessionType.WIN32)
        self.assertIs(e.compositor, Compositor.DWM)

    def test_the_capability_properties_agree_with_the_backend(self):
        # These three answer the same question the backend answers with a
        # capability, and disagreeing with it is the failure worth pinning:
        # `Win32Backend` declares SCREEN_CAPTURE and CLIPBOARD and reaches
        # both through user32/gdi32 with no tool on PATH, so an empty
        # `capture_tools`/`clipboard_tools` says nothing about Windows.
        #
        # Measured on Windows 11 before the fix: can_capture and
        # can_use_clipboard were both False beside a backend declaring both.
        # The Windows work gave can_inject_input its branch and missed these.
        e = dataclasses.replace(
            self._detect(),
            has_sendinput=True,
            capture_tools=(),
            clipboard_tools=(),
            input_tools=(),
        )
        self.assertTrue(e.can_inject_input)
        self.assertTrue(e.can_capture)
        self.assertTrue(e.can_use_clipboard)

    def test_a_linux_session_still_needs_a_tool_for_those(self):
        # The Windows branch must not leak: on Linux a clipboard genuinely
        # needs a tool, and claiming otherwise would suppress the hint that
        # says so.
        e = detect(env(DISPLAY=":0", XDG_SESSION_TYPE="x11"))
        e = dataclasses.replace(e, capture_tools=(), clipboard_tools=())
        self.assertFalse(e.can_use_clipboard)

    def test_the_compositor_is_not_read_out_of_the_desktop_name(self):
        # A desktop name a Cygwin session leaked is not evidence of a KWin or
        # Mutter session; DWM is the compositor whatever the variable says.
        e = self._detect(XDG_CURRENT_DESKTOP="KDE")
        self.assertIs(e.compositor, Compositor.DWM)

    def test_input_is_claimed_through_sendinput(self):
        # The lying Environment the Windows analysis predicted in the
        # abstract: every mechanism can_inject_input looks at is a Linux one,
        # so a Windows session injected input perfectly well and reported
        # False here -- after which the hints said to install ydotool.
        e = dataclasses.replace(self._detect(), has_sendinput=True, input_tools=())
        self.assertTrue(e.can_inject_input)
        self.assertEqual(e.input_transport, "SendInput")
        self.assertIn("SendInput", e.summary())

    def test_without_sendinput_nothing_is_claimed(self):
        e = dataclasses.replace(self._detect(), has_sendinput=False)
        self.assertFalse(e.can_inject_input)
        self.assertIsNone(e.input_transport)

    def test_atspi_is_unreachable_on_windows_even_with_all_its_parts(self):
        # A hand-built Environment can carry the Linux fields -- a Cygwin
        # session with pyatspi installed, say -- and AT-SPI still does not
        # exist on Windows. What says whether elements are reachable there is
        # has_comtypes, and it is deliberately not folded into this property.
        e = dataclasses.replace(
            self._detect(), has_atspi=True, has_pygobject=True, has_dogtail=True
        )
        self.assertFalse(e.can_use_atspi)

    def test_the_summary_names_the_window_station_and_the_integrity_level(self):
        e = dataclasses.replace(
            self._detect(), is_interactive_desktop=False, is_elevated=True
        )
        text = e.summary()
        self.assertIn("not on an interactive desktop", text)
        self.assertIn("elevated", text)

    def test_the_summary_reports_what_a_bug_report_needs(self):
        e = dataclasses.replace(
            self._detect(),
            windows_build=22621,
            windows_edition="Windows 11 Pro",
            dpi_awareness="per-monitor-v2",
            has_comtypes=True,
            has_sendinput=True,
        )
        text = e.summary()
        self.assertIn("build 22621", text)
        self.assertIn("Windows 11 Pro", text)
        self.assertIn("per-monitor-v2", text)
        # The mechanisms line cannot say "none detected" beside an `input` line
        # that names a transport: that disagreement is why this line exists.
        self.assertIn("sendinput", text)
        self.assertNotIn("none detected", text)

    def test_the_edition_is_corrected_once_and_agrees_everywhere(self):
        # Measured on a real build 26200 box, where the registry says
        # "Windows 10 Pro" and `doctor` printed that beside a `report --json`
        # already saying "Windows 11 Pro (build 26200)" -- one machine, one
        # run, two answers. The correction lives in session so both readers
        # get it; this pins the field, and the hints test pins the other side.
        from pyguitest import hints, session

        self.assertEqual(
            session._windows_edition_name(26200, "Windows 10 Pro"), "Windows 11 Pro"
        )
        # Below the floor, the registry is simply right and is left alone.
        self.assertEqual(
            session._windows_edition_name(19045, "Windows 10 Pro"), "Windows 10 Pro"
        )
        # Unreadable stays unreadable: "" is not an edition to fix up, and
        # each caller says what it wants in its place.
        self.assertEqual(session._windows_edition_name(26200, ""), "")
        with mock.patch.object(
            session, "_windows_version", lambda: (26200, "Windows 10 Pro")
        ):
            self.assertEqual(hints._windows_release(), "Windows 11 Pro (build 26200)")

    def test_the_notes_name_the_failures_that_are_silent(self):
        # Both failures are invisible from inside the process: UIPI drops the
        # input without an error, and a missing comtypes only means every
        # element query finds nothing.
        from pyguitest import session

        # The window station is pinned rather than inherited: this asserts the
        # *absence* of that note, and on a real Windows box reached over ssh
        # the station genuinely is not interactive -- so the note fired and
        # this failed for the one reason that is not a bug. The sibling test
        # below pins it the other way and asserts the note is there.
        # Both halves of the UIPI gate: an unelevated process *and* an
        # elevated window in front of it. Either alone is not the failure.
        with mock.patch.object(session, "_elevated", lambda: False):
            with mock.patch.object(session, "_foreground_is_elevated", lambda: True):
                with mock.patch.object(session, "_module", lambda name: False):
                    with mock.patch.object(
                        session, "_interactive_window_station", lambda: True
                    ):
                        e = self._detect()
        self.assertTrue(any("UIPI" in n for n in e.notes))
        self.assertTrue(any("pyguitest[windows]" in n for n in e.notes))
        self.assertFalse(any("interactive desktop" in n for n in e.notes))

    def test_an_ordinary_unelevated_session_is_not_warned_about_uipi(self):
        # Not elevated is the state of every normal login, so gating the note
        # on that alone put it in every Windows report ever printed -- which
        # is how a reader learns to skip the notes entirely. UIPI only bites
        # where the *target* is the more privileged of the two.
        from pyguitest import session

        for foreground in (False, None):
            with self.subTest(foreground_is_elevated=foreground):
                with mock.patch.object(session, "_elevated", lambda: False):
                    with mock.patch.object(
                        session,
                        "_foreground_is_elevated",
                        lambda value=foreground: value,
                    ):
                        with mock.patch.object(
                            session, "_interactive_window_station", lambda: True
                        ):
                            e = self._detect()
                self.assertFalse(any("UIPI" in n for n in e.notes))

    def test_a_non_interactive_window_station_is_reported_and_noted(self):
        from pyguitest import session

        with mock.patch.object(session, "_interactive_window_station", lambda: False):
            e = self._detect()
        self.assertFalse(e.is_interactive_desktop)
        self.assertTrue(any("no windows are visible" in n for n in e.notes))


class TestTheDpiAwarenessProbe(unittest.TestCase):
    """`_dpi_awareness`, against the opaque handle a real Windows hands back.

    A `DPI_AWARENESS_CONTEXT` is a handle, not one of the pseudo-handle
    constants: on Windows 11 build 26200 the thread context came back as `18`
    and compared equal to `DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE` (-3) only
    through `AreDpiAwarenessContextsEqual`. The first version of this probe
    looked the value up directly and reported "" on that machine -- a gap no
    fake could have exposed, because a fake written from the documentation
    hands back the documented constant. So this one deliberately does not.
    """

    class _FakeUser32:
        """user32 answering an opaque context, as a real one does."""

        def __init__(self, context=18, equal_to=-3, comparer=True):
            self.context = context
            self.equal_to = equal_to
            self.GetThreadDpiAwarenessContext = _Function(lambda: self.context)
            if comparer:
                self.AreDpiAwarenessContextsEqual = _Function(self._equal)

        def _equal(self, left, right):
            return 1 if left == self.context and right == self.equal_to else 0

    def _awareness(self, **kwargs):
        from pyguitest import session

        user32 = self._FakeUser32(**kwargs)
        with mock.patch(
            "pyguitest.session._win32_lib",
            lambda name: user32 if name == "user32" else None,
        ):
            return session._dpi_awareness()

    def test_an_opaque_context_is_resolved_by_comparison(self):
        self.assertEqual(self._awareness(), "per-monitor")

    def test_per_monitor_v2_is_still_told_apart_from_per_monitor(self):
        # The whole reason the thread context is asked before
        # GetProcessDpiAwareness, which cannot express v2 at all.
        self.assertEqual(self._awareness(equal_to=-4), "per-monitor-v2")

    def test_a_context_matching_nothing_is_not_guessed_at(self):
        self.assertEqual(self._awareness(equal_to=-99), "")

    def test_an_older_windows_falls_back_to_the_direct_lookup(self):
        # Before 10 1607 there is no comparison function -- and no context
        # that can be anything but one of the documented values either.
        self.assertEqual(self._awareness(context=-2, comparer=False), "system")


class TestOtherSessionsGainNoWindowsFacts(unittest.TestCase):
    """The new fields default, and the defaults have to be safe ones."""

    def test_the_defaults_cannot_mislead_a_linux_session(self):
        e = linux_detect(env(WAYLAND_DISPLAY="wayland-0"))
        self.assertEqual(e.windows_build, 0)
        self.assertEqual(e.windows_edition, "")
        self.assertEqual(e.dpi_awareness, "")
        self.assertEqual(e.low_level_hooks_timeout_ms, 0)
        self.assertFalse(e.has_sendinput)
        self.assertFalse(e.has_comtypes)
        self.assertFalse(e.has_wgc)
        self.assertFalse(e.is_elevated)
        # None, not False: this is a Windows question, and False would be a
        # finding -- "the window in front is not elevated" -- on a session
        # where there is no such window.
        self.assertIsNone(e.foreground_is_elevated)
        # True, not False: the window-station question is about Windows, and a
        # session that has no window stations is not a more limited one -- so a
        # hint gated on this stays silent everywhere it does not apply.
        self.assertTrue(e.is_interactive_desktop)

    def test_no_windows_note_and_no_windows_line_reach_a_linux_session(self):
        e = linux_detect(env(DISPLAY=":0"))
        self.assertNotIn("windows      build", e.summary())
        for note in e.notes:
            with self.subTest(note=note):
                self.assertNotIn("UIPI", note)
                self.assertNotIn("pyguitest[windows]", note)
                self.assertNotIn("window station", note)


class TestTheUser32FreeProbes(unittest.TestCase):
    """The two Windows probes whose bodies can be driven with a fake DLL.

    Everything else in `_windows_environment` is a ctypes call whose shape is
    read from the documentation and cannot be exercised here; these two can
    be, so they are -- the comparison that decides `is_interactive_desktop`,
    and the arithmetic that reads a token's integrity level.
    """

    class _FakeUser32:
        """Answers the window-station calls, the way a real user32 does."""

        def __init__(self, station_name):
            self.station_name = station_name
            self.GetProcessWindowStation = _Function(lambda: 0x1234)
            self.GetUserObjectInformationW = _Function(self._name_of_station)

        def _name_of_station(self, station, index, buffer, size, needed):
            buffer.value = self.station_name
            return 1

    def _station_with(self, name):
        from pyguitest import session

        with mock.patch.object(
            session, "_win32_lib", lambda _name: self._FakeUser32(name)
        ):
            return session._interactive_window_station()

    def test_winsta0_is_the_interactive_station(self):
        self.assertTrue(self._station_with("WinSta0"))

    def test_the_comparison_does_not_depend_on_the_spelling(self):
        # Windows reports it as "WinSta0"; nothing about that casing is
        # guaranteed by anything, so both sides are folded.
        self.assertTrue(self._station_with("WINSTA0"))

    def test_a_service_station_is_not_interactive(self):
        # Session 0's station: everything enumerates as empty from there, and
        # nothing says why.
        self.assertFalse(self._station_with("Service-0x0-3e7$"))

    def test_a_dll_that_will_not_load_answers_cannot_tell(self):
        # True, not False: a machine with no user32 has no session-0 problem,
        # and a note saying it does would point the reader at the wrong thing.
        from pyguitest import session

        with mock.patch.object(session, "_win32_lib", lambda _name: None):
            self.assertTrue(session._interactive_window_station())


class TestTheIntegrityLevelArithmetic(unittest.TestCase):
    """The SID offsets in `_integrity_rid`, against the documented layout.

    This pins the arithmetic, not the Windows behaviour: the buffer here is
    built to the layout the documentation describes, and the first run on a
    real Windows machine is what confirms that the layout is right. What is
    worth having now is that a later edit cannot move an offset unnoticed.
    """

    def _label(self, *subauthorities):
        """A stand-in TOKEN_MANDATORY_LABEL: one SID_AND_ATTRIBUTES + a SID."""
        return token_label(*subauthorities)

    def _rid(self, *subauthorities):
        from pyguitest import session

        return session._integrity_rid(self._label(*subauthorities))

    def test_a_medium_integrity_sid_reads_as_medium(self):
        # S-1-16-8192, which is what an ordinary logged-in process carries.
        self.assertEqual(self._rid(16, 8192), 8192)

    def test_a_high_integrity_sid_reads_as_high(self):
        # S-1-16-12288, an elevated process: what UIPI is gated on.
        self.assertEqual(self._rid(16, 12288), 12288)

    def test_the_last_subauthority_is_the_one_that_counts(self):
        self.assertEqual(self._rid(16, 8192, 12288), 12288)

    def test_a_sid_with_no_subauthorities_is_zero_rather_than_a_crash(self):
        # Never happens on a real token; reading past the end of a short
        # buffer would be the alternative.
        self.assertEqual(self._rid(), 0)


if __name__ == "__main__":
    unittest.main()
