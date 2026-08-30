#!/usr/bin/env python3
"""One-off live validation of X11Backend's input half, driven through XTest.

`docs/validation.md` listed "Input injection through XTest" under *Not run
live* until this script's first run against a real GNOME/Mutter session.
That run scored 2 of 21 checks, and the reason why is now a documented
finding in its own right (see validation.md's entry under "Run live on
GNOME Shell 50.4"), not a passing grade for this script: `probe.
set_input_focus()` sets X-level focus only, and Mutter routes injected
XTest events by *compositor*-level focus, which that call never touches.
So the assertions below -- written assuming the probe window actually
receives what is injected -- systematically fail on Mutter; every event
this script sends instead lands wherever the compositor already considers
focused (in that first run, the terminal running the script). Kept here
because the design is still sound as an instrument on any compositor
where an X window can genuinely take input focus (a real X11 session, or
possibly KWin/wlroots under XWayland -- unmeasured), and because the
in-script checks below are the fastest way to notice if that has changed.
On Mutter, expect it to fail exactly as described, not to indicate a
regression.

For input, this script's approach is: create its own X11 window with
python-xlib, select the key, button and motion masks on it, take input
focus, and read back the event stream the server actually delivered.
Every assertion is exact -- this keycode, this keysym, this button, in
this order -- rather than a human deciding whether an xterm looked right.
It also needs nothing installed beyond python-xlib.

`move_mouse` is checked against `X11Backend.pointer_position()`, which is
sound here for a reason established only recently: under XWayland that
readback is exact while the pointer is over an X surface and silently stale
elsewhere (see docs/validation.md). The probe window is an X surface and the
pointer stays inside it, which is the regime where the readback can be
trusted.

WHAT THIS DOES TO YOUR SESSION, while it runs:

  * really moves your pointer, and really injects key and button events;
  * on Mutter, those events go wherever compositor focus already is --
    not necessarily the probe window, see above -- so this may type into
    whatever else you have focused; a terminal is the safest thing to
    have focused when you run it, for the same reason the first live run
    was legible at all;
  * uses no key any compositor grabs globally -- no Super, no Alt-Tab;
  * releases every key and button in a finally, including on Ctrl-C, since
    a modifier left held is the one way an input test leaves a desktop
    feeling broken;
  * restores the pointer to where it found it.

It changes nothing on the system: no packages, no groups, no udev rules, no
files outside this repository. XTest is an ordinary X protocol request, not
a device node -- unlike the uinput backend, it needs no permissions setup.

Do not touch the mouse or keyboard while it runs.

    python3 examples/_xtest_input_validate.py
    python3 examples/_xtest_input_validate.py --dry-run

`--dry-run` opens the probe window, connects the backend and reports what it
would exercise, then stops before injecting anything. It never takes focus.
Use it to confirm the setup is sound without handing over your keyboard.

On a real X11 session, where compositor focus and X focus are the same
thing, this validates injection outright. Under XWayland on Mutter it
does not validate what it was built to (see above); what it demonstrated
instead -- that a raw XTest event reaches whatever holds compositor
focus, native Wayland windows included -- is recorded in validation.md
rather than asserted here, since this script has no way to arrange for
a chosen Wayland window to hold that focus and check against it.
"""

import contextlib
import sys
import time

import pyguitest
from pyguitest import Capability

try:
    from Xlib import XK, X, display
except ImportError:
    sys.exit(
        "python-xlib is not installed -- this script needs it both for the "
        "backend under test and for the probe window. Install it in a "
        "throwaway venv rather than system-wide:\n"
        "  python3 -m venv --system-site-packages /tmp/xtest-venv\n"
        "  /tmp/xtest-venv/bin/pip install python-xlib\n"
        "  PYTHONPATH=src /tmp/xtest-venv/bin/python "
        "examples/_xtest_input_validate.py"
    )

WIDTH, HEIGHT = 600, 400
TYPED_TEXT = "pyguitest"

results = []


def check(label, passed, detail=""):
    """Record one assertion and print it as it happens."""
    results.append((label, passed))
    mark = "PASS" if passed else "FAIL"
    print(f"  [{mark}] {label}{'  -- ' + detail if detail else ''}")


def drain(screen_display):
    """Every event the connection has queued, drained.

    Sync first: XTest requests and the events they generate are separate
    round trips, and reading the queue without flushing races the server.
    """
    screen_display.sync()
    events = []
    while screen_display.pending_events():
        event = screen_display.next_event()
        events.append(event)
    return events


def keysym_of(screen_display, keycode, state=0):
    """The keysym a delivered keycode stands for, under the live keymap."""
    index = 1 if state & X.ShiftMask else 0
    return screen_display.keycode_to_keysym(keycode, index)


def main(dry_run=False):
    screen_display = display.Display()
    screen = screen_display.screen()

    left = (screen.width_in_pixels - WIDTH) // 2
    top = (screen.height_in_pixels - HEIGHT) // 2
    probe = screen.root.create_window(
        left,
        top,
        WIDTH,
        HEIGHT,
        2,
        screen.root_depth,
        X.InputOutput,
        X.CopyFromParent,
        background_pixel=screen.white_pixel,
        event_mask=(
            X.KeyPressMask
            | X.KeyReleaseMask
            | X.ButtonPressMask
            | X.ButtonReleaseMask
            | X.PointerMotionMask
            | X.StructureNotifyMask
        ),
    )
    probe.set_wm_name("pyguitest-xtest-probe")
    probe.map()
    screen_display.sync()

    centre = (left + WIDTH // 2, top + HEIGHT // 2)
    print(f"probe window at ({left}, {top}) {WIDTH}x{HEIGHT}, centre {centre}")

    gui = pyguitest.connect(backend="x11")
    print(f"forced backend: {gui.backend.name}")
    for capability in (
        Capability.POINTER_MOVE,
        Capability.POINTER_BUTTON,
        Capability.KEY_EVENT,
        Capability.TEXT_ENTRY,
    ):
        if not gui.supports(capability):
            sys.exit(f"{capability.name} is unavailable; nothing to validate")

    restore_to = None
    if gui.supports(Capability.POINTER_QUERY):
        restore_to = gui.pointer_position()
        print(f"pointer starts at {restore_to}; it will be put back")

    if dry_run:
        print(
            "\ndry run: the probe window opened, the backend connected and "
            "every capability needed is present.\nNothing was injected and "
            "focus was left alone. Re-run without --dry-run to validate."
        )
        with contextlib.suppress(Exception):
            probe.destroy()
            screen_display.sync()
            screen_display.close()
        return 0

    print("\nStarting in 3 seconds -- do not touch the mouse or keyboard.")
    time.sleep(3)

    held_keys = []
    held_buttons = []
    try:
        # Focus must be ours before a single key goes out, or the events
        # land wherever the pointer happened to leave it.
        probe.set_input_focus(X.RevertToParent, X.CurrentTime)
        gui.move_mouse(*centre)
        screen_display.sync()
        time.sleep(0.2)
        drain(screen_display)

        focus = screen_display.get_input_focus().focus
        print(f"\ninput focus is the probe window: {focus.id == probe.id}")

        # -- POINTER_MOVE ---------------------------------------------------
        print("\nmove_mouse:")
        for dx, dy in ((-120, -80), (120, 80), (0, 0)):
            target = (centre[0] + dx, centre[1] + dy)
            gui.move_mouse(*target)
            time.sleep(0.15)
            motion = [e for e in drain(screen_display) if e.type == X.MotionNotify]
            check(
                f"motion delivered for {target}",
                bool(motion),
                f"{len(motion)} MotionNotify",
            )
            if gui.supports(Capability.POINTER_QUERY):
                got = gui.pointer_position()
                check(
                    f"pointer_position reads back {target}",
                    got == target,
                    f"read {got}",
                )

        # -- POINTER_BUTTON -------------------------------------------------
        print("\npress_button / release_button, and click:")
        gui.move_mouse(*centre)
        time.sleep(0.15)
        drain(screen_display)

        held_buttons.append(1)
        gui.press_button(1)
        time.sleep(0.15)
        pressed = [e for e in drain(screen_display) if e.type == X.ButtonPress]
        check("ButtonPress delivered", bool(pressed))
        check(
            "it is button 1",
            bool(pressed) and pressed[0].detail == 1,
            f"detail {pressed[0].detail}" if pressed else "",
        )
        if gui.supports(Capability.INPUT_STATE_QUERY):
            check("is_button_pressed sees it held", gui.is_button_pressed(1))

        gui.release_button(1)
        held_buttons.remove(1)
        time.sleep(0.15)
        released = [e for e in drain(screen_display) if e.type == X.ButtonRelease]
        check("ButtonRelease delivered", bool(released))
        if gui.supports(Capability.INPUT_STATE_QUERY):
            check("is_button_pressed sees it let go", not gui.is_button_pressed(1))

        gui.click()
        time.sleep(0.15)
        events = drain(screen_display)
        check(
            "click() is a press and a release, in that order",
            [e.type for e in events if e.type in (X.ButtonPress, X.ButtonRelease)]
            == [X.ButtonPress, X.ButtonRelease],
        )

        # -- KEY_EVENT ------------------------------------------------------
        print("\npress_key / release_key:")
        held_keys.append("a")
        gui.press_key("a")
        time.sleep(0.15)
        key_presses = [e for e in drain(screen_display) if e.type == X.KeyPress]
        check("KeyPress delivered", bool(key_presses))
        if key_presses:
            got = keysym_of(screen_display, key_presses[0].detail)
            check(
                "the keysym delivered is 'a'",
                got == XK.string_to_keysym("a"),
                f"got {screen_display.lookup_string(got)!r}",
            )
        if gui.supports(Capability.INPUT_STATE_QUERY):
            check("is_key_pressed sees it held", gui.is_key_pressed("a"))

        gui.release_key("a")
        held_keys.remove("a")
        time.sleep(0.15)
        check(
            "KeyRelease delivered",
            any(e.type == X.KeyRelease for e in drain(screen_display)),
        )
        if gui.supports(Capability.INPUT_STATE_QUERY):
            check("is_key_pressed sees it let go", not gui.is_key_pressed("a"))

        # -- TEXT_ENTRY -----------------------------------------------------
        print(f"\ntype_text({TYPED_TEXT!r}):")
        drain(screen_display)
        gui.type_text(TYPED_TEXT)
        time.sleep(0.3)
        typed = [e for e in drain(screen_display) if e.type == X.KeyPress]
        received = "".join(
            chr(keysym_of(screen_display, e.detail, e.state) & 0xFF) for e in typed
        )
        check(
            "every character arrived, in order",
            received == TYPED_TEXT,
            f"received {received!r}",
        )

        # The shift path is the half most likely to be wrong: an uppercase
        # character needs a modifier pressed around the keycode, and getting
        # it backwards still delivers a key, just the wrong one.
        print("\ntype_text('X') -- the shift path:")
        drain(screen_display)
        gui.type_text("X")
        time.sleep(0.2)
        shifted = [e for e in drain(screen_display) if e.type == X.KeyPress]
        letters = [
            e
            for e in shifted
            if keysym_of(screen_display, e.detail, e.state) == XK.XK_X
        ]
        check("an uppercase X was delivered", bool(letters))
        check(
            "it arrived with Shift in its modifier state",
            bool(letters) and bool(letters[0].state & X.ShiftMask),
            f"state {letters[0].state:#x}" if letters else "",
        )

        # -- send_keys ------------------------------------------------------
        print("\nsend_keys('^(a)') -- modifier notation:")
        drain(screen_display)
        gui.send_keys("^(a)")
        time.sleep(0.2)
        combo = [e for e in drain(screen_display) if e.type == X.KeyPress]
        target = [
            e
            for e in combo
            if keysym_of(screen_display, e.detail) == XK.string_to_keysym("a")
        ]
        check("the 'a' of ^(a) was delivered", bool(target))
        check(
            "it arrived with Control in its modifier state",
            bool(target) and bool(target[0].state & X.ControlMask),
            f"state {target[0].state:#x}" if target else "",
        )

    finally:
        # Unconditional: anything still held here outlives the script and
        # makes the whole desktop feel broken.
        for button in list(held_buttons):
            with contextlib.suppress(Exception):
                gui.release_button(button)
        for key in list(held_keys):
            with contextlib.suppress(Exception):
                gui.release_key(key)
        for modifier in ("Shift_L", "Control_L", "Alt_L", "Super_L"):
            with contextlib.suppress(Exception):
                gui.release_key(modifier)
        if restore_to is not None:
            with contextlib.suppress(Exception):
                gui.move_mouse(*restore_to)
        with contextlib.suppress(Exception):
            probe.destroy()
            screen_display.sync()
            screen_display.close()

    failed = [label for label, passed in results if not passed]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("failed:")
        for label in failed:
            print(f"  - {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(dry_run="--dry-run" in sys.argv))
