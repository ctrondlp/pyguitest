"""Install advice: distribution detection and what it recommends."""

import dataclasses
import unittest

from pyguitest.hints import advice, detect_distro, hints_for
from pyguitest.session import SessionType, detect


def environment(**overrides):
    base = detect({"WAYLAND_DISPLAY": "wayland-0", "XDG_CURRENT_DESKTOP": "GNOME"})
    return dataclasses.replace(base, **overrides)


class TestDistroDetection(unittest.TestCase):
    def test_direct_id(self):
        self.assertEqual(detect_distro("ID=fedora\nVERSION_ID=44"), "fedora")
        self.assertEqual(detect_distro("ID=arch"), "arch")

    def test_id_like_is_used_when_the_id_is_unknown(self):
        # Derivatives name their parent, so Mint resolves through Ubuntu.
        self.assertEqual(
            detect_distro('ID=linuxmint\nID_LIKE="ubuntu debian"'), "debian"
        )

    def test_quotes_are_stripped(self):
        self.assertEqual(detect_distro('ID="opensuse-leap"\nID_LIKE="suse"'), "suse")

    def test_unknown_distribution_is_none_not_an_error(self):
        self.assertIsNone(detect_distro("ID=plan9"))
        self.assertIsNone(detect_distro(""))

    def test_specific_id_wins_regardless_of_line_order(self):
        # Regression: ID and ID_LIKE were merged in file order, so a file
        # listing ID_LIKE before ID let the generic family beat the
        # specific one. Both are recognised families here but disagree, so
        # the wrong answer ("arch") is distinguishable from the right one.
        self.assertEqual(detect_distro('ID_LIKE="arch"\nID=fedora'), "fedora")

        # A derivative whose own ID is unrecognised still falls through to
        # ID_LIKE, in either line order.
        self.assertEqual(
            detect_distro('ID_LIKE="ubuntu debian"\nID=linuxmint'), "debian"
        )


class TestHints(unittest.TestCase):
    def test_missing_atspi_is_reported_first(self):
        found = list(hints_for(environment(has_pygobject=False), distro="fedora"))
        self.assertEqual(found[0].component, "AT-SPI")
        self.assertIn("dnf install", found[0].command)
        self.assertIn("python3-gobject", found[0].command)

    def test_package_names_differ_by_distribution(self):
        env = environment(has_pygobject=False)
        debian = next(iter(hints_for(env, distro="debian")))
        self.assertIn("apt install", debian.command)
        self.assertIn("python3-gi", debian.command)
        arch = next(iter(hints_for(env, distro="arch")))
        self.assertIn("pacman", arch.command)

    def test_missing_imagemagick_is_hinted_with_the_right_package_name(self):
        env = environment(
            has_atspi=True,
            has_pygobject=True,
            capture_tools=("grim",),
            input_tools=("wdotool",),
            image_tools=(),
        )
        fedora = next(
            h
            for h in hints_for(env, distro="fedora")
            if h.component == "template matching"
        )
        self.assertIn("dnf install", fedora.command)
        self.assertIn("ImageMagick", fedora.command)
        debian = next(
            h
            for h in hints_for(env, distro="debian")
            if h.component == "template matching"
        )
        self.assertIn("apt install", debian.command)
        self.assertIn("imagemagick", debian.command)

    def test_unknown_distribution_still_names_the_component(self):
        hint = next(iter(hints_for(environment(has_pygobject=False), distro="plan9")))
        self.assertEqual(hint.component, "AT-SPI")
        self.assertIsNone(hint.command)
        self.assertIn(
            "install it through your distribution",
            advice(environment(has_pygobject=False), distro="plan9"),
        )

    def test_nothing_missing_says_so(self):
        complete = environment(
            has_atspi=True,
            has_pygobject=True,
            capture_tools=("grim",),
            input_tools=("wdotool",),
            image_tools=("compare",),
        )
        self.assertEqual(list(hints_for(complete)), [])
        self.assertIn("Nothing missing", advice(complete))

    def test_advice_mentions_the_extra_only_when_atspi_is_missing(self):
        with_atspi = environment(
            has_atspi=True,
            has_pygobject=True,
            capture_tools=("grim",),
            input_tools=("wdotool",),
            image_tools=("compare",),
        )
        self.assertNotIn("[atspi]", advice(with_atspi))
        self.assertIn("[atspi]", advice(environment(has_pygobject=False)))

    def test_end_user_advice_never_suggests_an_editable_install(self):
        # `-e` is for developing this package, not for using it. Match the
        # whole flag: package names like python3-evdev contain "-e".
        text = advice(environment(has_pygobject=False))
        self.assertNotIn("pip install -e", text)
        self.assertIn("pip install 'pyguitest[atspi]'", text)


if __name__ == "__main__":
    unittest.main()


class TestInputAdvice(unittest.TestCase):
    """A live Fedora run showed the advice naming an uninstallable package.

    libei and the portal were both present, so the mechanism existed -- but the
    only tool that speaks them, wdotool, is packaged by no distribution. The
    advice now leads with what dnf can actually install.
    """

    def _input_only(self, **overrides):
        return environment(
            has_atspi=True,
            has_pygobject=True,
            capture_tools=("grim",),
            input_tools=(),
            uinput_writable=False,
            **overrides,
        )

    def test_recommends_installable_packages_not_a_source_build(self):
        hints = list(hints_for(self._input_only(has_libei=True), distro="fedora"))
        commands = " ".join(h.command or "" for h in hints)
        self.assertIn("dnf install", commands)
        self.assertIn("python3-evdev", commands)
        self.assertIn("ydotool", commands)

    def test_the_input_group_step_is_stated(self):
        # Installing the package alone is not enough; /dev/uinput is root-only.
        hints = list(hints_for(self._input_only(), distro="fedora"))
        components = [h.component for h in hints]
        self.assertIn("membership of the 'input' group", components)
        group_hint = next(h for h in hints if "input' group" in h.component)
        self.assertIn("usermod -aG input", group_hint.command)

    def test_wdotool_is_mentioned_only_as_a_caveat(self):
        # It is the sole keymap-safe option on GNOME, but no distro ships it,
        # so it must not appear as an install instruction.
        text = advice(self._input_only(has_libei=True), distro="fedora")
        self.assertIn("wdotool", text)
        self.assertIn("no distribution packages", text)
        self.assertNotIn("install wdotool", text)

    def test_writable_uinput_needs_no_input_advice(self):
        env = environment(
            has_atspi=True,
            has_pygobject=True,
            capture_tools=("grim",),
            input_tools=(),
            uinput_writable=True,
        )
        components = [h.component for h in hints_for(env)]
        self.assertNotIn("input injection", components)


class TestToolRecommendationsAreKeyedByCompositor(unittest.TestCase):
    """Recommendations must fit the compositor, not just the distro.

    Regression: input and capture recommendations were keyed only on distro
    family, so a Fedora+sway user was told to install gnome-screenshot
    (captures nothing there) and ydotool (the keymap-unsafe tool tools.py
    ranks last) even though grim and wtype are both ordinarily packaged and
    actually work on that compositor.
    """

    def _wlroots(self, **overrides):
        from pyguitest.session import Compositor, SessionType

        base = detect({"WAYLAND_DISPLAY": "wayland-0", "SWAYSOCK": "/run/sway.sock"})
        self.assertIs(base.compositor, Compositor.WLROOTS)
        self.assertIs(base.session_type, SessionType.WAYLAND)
        return dataclasses.replace(
            base,
            capture_tools=(),
            input_tools=(),
            uinput_writable=False,
            **overrides,
        )

    def _x11(self, **overrides):
        from pyguitest.session import SessionType

        base = detect({"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"})
        self.assertIs(base.session_type, SessionType.X11)
        return dataclasses.replace(
            base,
            capture_tools=(),
            input_tools=(),
            uinput_writable=False,
            **overrides,
        )

    def test_wlroots_capture_recommends_grim_not_gnome_screenshot(self):
        hints = list(hints_for(self._wlroots(), distro="fedora"))
        hint = next(h for h in hints if h.component == "screenshots")
        self.assertIn("grim", hint.command)
        self.assertNotIn("gnome-screenshot", hint.command)

    def test_wlroots_input_recommends_wtype_not_ydotool(self):
        hints = list(hints_for(self._wlroots(), distro="fedora"))
        input_hint = next(h for h in hints if h.component == "input injection")
        self.assertIn("wtype", input_hint.command)
        self.assertNotIn("ydotool", input_hint.command)
        # wtype needs no /dev/uinput access, so the group hint is moot here.
        self.assertNotIn(
            "membership of the 'input' group", [h.component for h in hints]
        )

    def test_x11_input_recommends_xdotool_not_ydotool(self):
        hints = list(hints_for(self._x11(), distro="debian"))
        input_hint = next(h for h in hints if h.component == "input injection")
        self.assertIn("xdotool", input_hint.command)
        self.assertNotIn("ydotool", input_hint.command)

    def test_ydotool_caveat_is_absent_when_xdotool_is_recommended(self):
        text = advice(self._x11(), distro="debian")
        self.assertNotIn("ydotoold", text)

    def test_mutter_still_recommends_ydotool_as_the_true_last_resort(self):
        # Unchanged from before: Mutter implements no wlroots protocol, so
        # ydotool genuinely is the only packaged option there.
        hints = list(
            hints_for(
                environment(capture_tools=(), input_tools=(), uinput_writable=False),
                distro="fedora",
            )
        )
        input_hint = next(h for h in hints if h.component == "input injection")
        self.assertIn("ydotool", input_hint.command)
        self.assertIn("membership of the 'input' group", [h.component for h in hints])


class TestCaptureHintFollowsEveryRoute(unittest.TestCase):
    """The screenshot hint must not fire where capture already works.

    Regression risk introduced with the portal and X11 capture paths: the
    hint keyed on capture_tools alone, so an X11 login with python-xlib --
    which captures and encodes a PNG with no tool at all -- was still told
    to install one.
    """

    def _hint(self, **overrides):
        base = {
            "has_atspi": True,
            "has_pygobject": True,
            "capture_tools": (),
            "input_tools": ("wdotool",),
            "image_tools": ("compare",),
        }
        env = environment(**{**base, **overrides})
        found = hints_for(env, distro="fedora")
        return next((h for h in found if h.component == "screenshots"), None)

    def test_no_route_at_all_is_hinted(self):
        self.assertIsNotNone(self._hint())

    def test_python_xlib_on_an_x_session_silences_it(self):
        self.assertIsNone(self._hint(has_xlib=True, session_type=SessionType.X11))

    def test_the_portal_changes_the_advice_rather_than_silencing_it(self):
        # The portal captures, so "install a screenshot tool" would be
        # wrong -- but staying silent would be worse. Automatic composition
        # never reaches portalcapture (it is opt-in, because its first use
        # prompts), so a plain connect() on such a session cannot
        # screenshot at all, and nothing else would say so.
        hint = self._hint(has_portal=True, has_pygobject=True)
        self.assertIsNotNone(hint)
        self.assertIn("portalcapture", hint.why)
        # Nothing to install: no tool can work on this session.
        self.assertIsNone(hint.command)

    def test_a_working_tool_silences_it_completely(self):
        self.assertIsNone(self._hint(capture_tools=("grim",)))

    def test_native_x11_capture_silences_it_completely(self):
        # python-xlib on a real X session captures with no tool and no
        # portal, so there is nothing to advise.
        self.assertIsNone(
            self._hint(has_xlib=True, session_type=SessionType.X11, has_portal=True)
        )
