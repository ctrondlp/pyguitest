#!/usr/bin/env python3
"""Live validation for `Session.move_mouse_naturally()`.

`docs/validation.md` records this method as the one part of the package with no
live evidence behind it: every claim about the *shape* of its path was, until
this script, checked only by unit tests asserting on the coordinates it emits.
A unit test can show that a path is curved. It cannot show that a display
server delivers that path as many events, in order, arriving where promised,
at the speed a bell profile claims.

What this measures, all of it observed rather than assumed:

* **The stream, not the jump** -- how many motion events the server actually
  delivered, and that the last one lands on the target.
* **The arch** -- the largest distance the path sits off the straight line
  between its endpoints, measured for a shaped move *and* for a straight
  `glide()` over the same endpoints. The first must be well off, the second
  must be on it, which is what makes the number mean anything.
* **The speed ramp** -- median speed over the first, middle and last third of
  the run, from the server's own event timestamps. Minimum-jerk says the middle
  is fastest and both ends are slower; that is a claim about time, so only
  timestamps can check it.
* **The hover case** -- a region that reacts only while the pointer is over it.
  The probe window is shrunk to part of the screen and the pointer moved in
  from outside, so a real X client has to receive Enter, then motion, then
  Leave on the way back out.

Two X connections on purpose. `X11Backend.move_mouse` drives XTest on its own
connection, while this script's connection owns the probe window and reads what
the server delivers to it; python-xlib is not safe to drive concurrently from
one connection, and sharing it between injection and reading would interleave
or deadlock rather than fail cleanly.

Needs `python-xlib`, and a display an X client can open -- X11, or the XWayland
inside `scripts/headless-session.sh`. The `x11` backend is forced: XTest needs
no permissions and no daemon, so this runs where `uinput`/`ydotool` cannot.

    python3 examples/_natural_motion_validate.py
    ./scripts/headless-session.sh python3 examples/_natural_motion_validate.py

Exit status 0 when every check passed, 1 when any failed, 2 for a setup problem
-- no display, no python-xlib, nothing to inject with.
"""

from __future__ import annotations

import math
import sys
import threading
import time

import pyguitest
from pyguitest import Capability

START = (200, 200)
TARGET = (1400, 700)
"""Endpoints for the two comparison runs, in root coordinates.

Both well inside a 1920x1080 monitor, and far enough apart on the diagonal to
give an arch room to show without leaving the screen.
"""

ARC_PX = 40.0
"""How far off the straight line counts as curved, in pixels.

The default `arc` is 0.15 of a leg, so the bow on this ~1243px diagonal peaks
near 90px. A straight `glide()` must come in at effectively zero, which is what
makes 40 a separator rather than an arbitrary pass mark.
"""

SETTLED = 0.3
"""Seconds to wait after a move before reading the events out.

The server delivers asynchronously and the reader polls, so the tail of a
stream can still be in flight when `move_mouse_naturally` returns.
"""


def off_the_line(points, start, end):
    """The largest distance any point sits off the straight line start -> end.

    Zero means a perfectly straight run. This is the whole arch measurement:
    `glide()` and `arc=0` should both be at roughly rounding error, and a shaped
    move should be nowhere near it.
    """
    ax, ay = start
    bx, by = end
    span = math.hypot(bx - ax, by - ay)
    if not span:
        return 0.0
    return max(
        abs((px - ax) * (by - ay) - (py - ay) * (bx - ax)) / span for px, py in points
    )


def speed_bands(events):
    """(first, middle, last) median speed in px/s, over a run's three thirds.

    Median rather than mean because a single coalesced pair of events -- the
    server is free to merge motion, and does -- shows up as one enormous
    interval and would drag a mean anywhere. Deltas come from the server's own
    milliseconds, so this is delivery time rather than emitting time.
    """
    samples = []
    for (t0, x0, y0), (t1, x1, y1) in zip(events, events[1:], strict=False):
        dt = (t1 - t0) & 0xFFFFFFFF  # a 32-bit millisecond counter, so it wraps
        if dt <= 0:
            continue
        samples.append(math.dist((x0, y0), (x1, y1)) * 1000.0 / dt)
    if len(samples) < 6:
        return None
    third = len(samples) // 3
    return tuple(
        sorted(chunk)[len(chunk) // 2]
        for chunk in (samples[:third], samples[third:-third], samples[-third:])
    )


def report(name, ok, detail):
    """One PASS/FAIL line, returning whether it passed."""
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<32} {detail}")
    return bool(ok)


class MotionProbe:
    """A mapped X window that records the motion the server delivers to it.

    Owns its own display connection and a reader thread, because the whole
    point is to see what the server sends rather than what the caller believes
    it sent. `override_redirect` keeps a window manager from reparenting or
    decorating it, so the coordinates in those events stay root coordinates.
    """

    def __init__(self):
        from Xlib import X
        from Xlib import display as xdisplay

        self.X = X
        self.display = xdisplay.Display()
        self.screen = self.display.screen()
        self.motions = []
        self.enters = 0
        self.leaves = 0
        self._stop = threading.Event()
        self._thread = None
        self.window = self._make(
            0, 0, self.screen.width_in_pixels, self.screen.height_in_pixels
        )

    def _event_mask(self):
        return (
            self.X.PointerMotionMask | self.X.EnterWindowMask | self.X.LeaveWindowMask
        )

    def _make(self, x, y, width, height):
        window = self.screen.root.create_window(
            x,
            y,
            max(1, width),
            max(1, height),
            0,
            self.screen.root_depth,
            self.X.InputOutput,
            self.X.CopyFromParent,
            background_pixel=self.screen.black_pixel,
            override_redirect=True,
            event_mask=self._event_mask(),
        )
        window.map()
        window.configure(stack_mode=self.X.Above)
        self.display.sync()
        return window

    def shrink_to(self, x, y, width, height):
        """Replace the probe with a window at `x`,`y` of `width` x `height`.

        For the hover phase, where the region has to sit somewhere the pointer
        starts *outside* of. Covering the screen is exactly what makes Enter
        impossible to observe: there is no boundary left to cross, so the
        motion still arrives and the crossing this case is about never does.
        """
        self.window.destroy()
        self.display.sync()
        self.reset()
        self.window = self._make(x, y, width, height)

    def reset(self):
        self.motions.clear()
        self.enters = self.leaves = 0

    def _read(self):
        while not self._stop.is_set():
            while self.display.pending_events():
                event = self.display.next_event()
                kind = event.type
                if kind == self.X.MotionNotify:
                    self.motions.append((event.time, event.root_x, event.root_y))
                elif kind == self.X.EnterNotify:
                    self.enters += 1
                elif kind == self.X.LeaveNotify:
                    self.leaves += 1
            time.sleep(0.002)

    def __enter__(self):
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        try:
            self.window.destroy()
            self.display.close()
        except Exception:  # noqa: BLE001 - teardown is never the finding
            pass
        return False

    def collect(self):
        """The motion recorded so far, oldest first, after letting it settle."""
        time.sleep(SETTLED)
        return list(self.motions)

    def points(self, events):
        """Just the coordinates, for the geometry checks."""
        return [(x, y) for _, x, y in events]


def main():
    """Run every check and report each one."""
    print("move_mouse_naturally -- live validation\n")

    gui = pyguitest.connect(backend="x11")
    if not gui.supports(Capability.POINTER_MOVE):
        print(
            "this session cannot move the pointer, so there is nothing to "
            "measure.\nThe x11 backend needs XTest, which needs an X server: "
            "either a real X11 session, or the XWayland inside\n"
            "  ./scripts/headless-session.sh python3 "
            "examples/_natural_motion_validate.py"
        )
        return 2
    print(f"backend {gui.backend.name!r} (forced), endpoints {START} -> {TARGET}\n")

    passed = []

    with MotionProbe() as probe:
        # -- the control: a straight glide, which must not curve ------------
        gui.move_mouse(*START)
        probe.reset()
        gui.glide(*TARGET, start=START, duration=0.6)
        straight = probe.collect()
        straight_off = off_the_line(probe.points(straight), START, TARGET)

        # -- the subject: the same move, shaped -----------------------------
        gui.move_mouse(*START)
        probe.reset()
        gui.move_mouse_naturally(*TARGET, start=START, duration=0.6)
        shaped = probe.collect()
        shaped_points = probe.points(shaped)
        shaped_off = off_the_line(shaped_points, START, TARGET)
        last = shaped_points[-1] if shaped_points else None
        miss = math.dist(last, TARGET) if last else float("inf")

        print("-- the stream, and where it lands --")
        passed.append(
            report(
                "arrived as a stream",
                len(shaped) >= 20,
                f"{len(shaped)} motion events over 0.6s",
            )
        )
        passed.append(
            report(
                "landed on the target",
                miss <= 2.0,
                f"last event {last}, {miss:.1f}px off",
            )
        )

        print("\n-- the arch --")
        passed.append(
            report(
                "shaped path curves",
                shaped_off >= ARC_PX,
                f"{shaped_off:.1f}px off the straight line (floor {ARC_PX:.0f})",
            )
        )
        passed.append(
            report(
                "glide() does not",
                straight_off < 5.0,
                f"{straight_off:.1f}px off, over the same endpoints",
            )
        )

        print("\n-- the speed ramp (px/s, from the server's timestamps) --")
        bands = speed_bands(shaped)
        if bands is None:
            passed.append(report("speed ramp", False, "too few usable intervals"))
        else:
            first, middle, last_band = bands
            passed.append(
                report(
                    "middle is fastest",
                    middle > first and middle > last_band,
                    f"first {first:.0f}, middle {middle:.0f}, last {last_band:.0f}",
                )
            )

        # -- the hover case: a region that only reacts while over it --------
        probe.shrink_to(
            probe.screen.width_in_pixels // 2,
            0,
            probe.screen.width_in_pixels // 2,
            probe.screen.height_in_pixels,
        )
        gui.move_mouse(*START)  # outside the region
        time.sleep(SETTLED)
        probe.reset()
        gui.move_mouse_naturally(1000, 500, start=START, duration=0.5)
        time.sleep(SETTLED)
        entered, seen = probe.enters, len(probe.motions)
        gui.move_mouse(*START)  # and back out
        time.sleep(SETTLED)
        gone = probe.leaves

        print("\n-- the hover region (right half of the screen) --")
        passed.append(
            report(
                "entered, then tracked",
                entered >= 1 and seen >= 5,
                f"{entered} Enter, {seen} motion inside",
            )
        )
        passed.append(report("left again", gone >= 1, f"{gone} Leave"))

    print(f"\n{sum(passed)}/{len(passed)} checks passed")
    return 0 if all(passed) else 1


if __name__ == "__main__":
    sys.exit(main())
