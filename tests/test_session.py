import dataclasses
import unittest
from unittest import mock

from pyguitest.session import Compositor, SessionType, detect


def env(**kw):
    """A bare fake environment; detect() reads only what is passed."""
    return kw


class TestSessionClassification(unittest.TestCase):
    def test_pure_wayland(self):
        e = detect(env(WAYLAND_DISPLAY="wayland-0", XDG_CURRENT_DESKTOP="GNOME"))
        self.assertIs(e.session_type, SessionType.WAYLAND)
        self.assertIs(e.compositor, Compositor.MUTTER)

    def test_xwayland_is_distinguished_from_both(self):
        # Both variables set means an X11 connection inside a Wayland session,
        # where XTest reaches X11 clients but never native Wayland ones.
        e = detect(env(WAYLAND_DISPLAY="wayland-0", DISPLAY=":0"))
        self.assertIs(e.session_type, SessionType.XWAYLAND)
        self.assertTrue(any("XWayland" in n for n in e.notes))

    def test_pure_x11(self):
        e = detect(env(DISPLAY=":0", XDG_SESSION_TYPE="x11"))
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
        e = detect(
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
        e = detect(env())
        self.assertIs(e.session_type, SessionType.HEADLESS)
        self.assertIs(e.compositor, Compositor.NONE)


class TestCompositorDetection(unittest.TestCase):
    def test_kde(self):
        e = detect(env(WAYLAND_DISPLAY="wayland-0", XDG_CURRENT_DESKTOP="KDE"))
        self.assertIs(e.compositor, Compositor.KWIN)

    def test_wlroots_by_desktop_name(self):
        for desktop in ("sway", "Hyprland", "river", "niri"):
            with self.subTest(desktop=desktop):
                e = detect(
                    env(WAYLAND_DISPLAY="wayland-0", XDG_CURRENT_DESKTOP=desktop)
                )
                self.assertIs(e.compositor, Compositor.WLROOTS)

    def test_wlroots_by_socket(self):
        e = detect(env(WAYLAND_DISPLAY="wayland-0", SWAYSOCK="/run/sway.sock"))
        self.assertIs(e.compositor, Compositor.WLROOTS)

    def test_mutter_warns_about_foreign_toplevel(self):
        e = detect(env(WAYLAND_DISPLAY="wayland-0", XDG_CURRENT_DESKTOP="GNOME"))
        self.assertTrue(any("foreign-toplevel" in n for n in e.notes))

    def test_unnamed_wayland_compositor_is_other_not_none(self):
        # Regression: NONE means "no compositor", which contradicts
        # WAYLAND_DISPLAY being set at all -- a Wayland session cannot exist
        # without one, even when nothing names which one.
        e = detect(env(WAYLAND_DISPLAY="wayland-0"))
        self.assertIs(e.compositor, Compositor.OTHER)


class TestInputPreference(unittest.TestCase):
    def setUp(self):
        self.wayland = detect(env(WAYLAND_DISPLAY="wayland-0"))

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
    """What `detect()` lists has to be what a backend could actually use.

    `input_tools` is not decoration: `preferred_input` reads it, `doctor`
    prints it, and `_input_factory` makes the same discovery call to pick
    a real backend. A tool listed here that cannot work on this session is
    a wrong answer in all three places.
    """

    def _tools(self, environment):
        with mock.patch(
            "pyguitest.tools.shutil.which", lambda name: f"/usr/bin/{name}"
        ):
            return detect(environment).input_tools

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
        e = detect(env(WAYLAND_DISPLAY="wayland-0"))
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
        e = detect(env(WAYLAND_DISPLAY="wayland-0"))
        no_dogtail = dataclasses.replace(
            e, has_atspi=True, has_pygobject=True, has_dogtail=False
        )
        self.assertFalse(no_dogtail.can_use_atspi)


class TestPortalDetectionHonoursTheFakeEnvironment(unittest.TestCase):
    """has_portal must read the injected env, not the real process one.

    Regression: it used to read os.environ directly, so a fake env passed
    to detect() for a test silently inherited the real host's D-Bus session
    instead of the one being simulated.
    """

    def test_no_dbus_session_in_the_fake_env_means_no_portal(self):
        # Patch the real process environment to have a bus address, so the
        # only way this could pass is by actually reading the fake `env`.
        with mock.patch.dict(
            "os.environ", {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/bus"}
        ):
            e = detect(env(WAYLAND_DISPLAY="wayland-0"))
        self.assertFalse(e.has_portal)

    def test_dbus_session_in_the_fake_env_is_read(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch("shutil.which", return_value="/usr/bin/xdg-desktop-portal"):
                e = detect(
                    env(
                        WAYLAND_DISPLAY="wayland-0",
                        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/fake-bus",
                    )
                )
        self.assertTrue(e.has_portal)


if __name__ == "__main__":
    unittest.main()


class TestCaptureDetection(unittest.TestCase):
    """can_capture: a CLI tool is only one of three routes to pixels.

    Reporting "no screen capture" purely on an empty capture_tools sends
    someone off to install gnome-screenshot on a session that can already
    capture -- an X11 login with python-xlib, or any desktop with the
    Screenshot portal.
    """

    def _env(self, **overrides):
        base = detect(env(WAYLAND_DISPLAY="wayland-0"))
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
