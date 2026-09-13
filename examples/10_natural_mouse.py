#!/usr/bin/env python3
"""Walk the pointer around the whole screen through 50 waypoints.

`move_mouse` teleports, and `glide` draws a straight line at a constant speed.
`move_mouse_naturally` shapes the path instead: a minimum-jerk speed ramp out
of the start and into each target, an arch bowing every leg off the straight
line, a little seeded drift across the run, and an overshoot that aims past the
end of each leg before correcting back onto it.

This walks a rosette -- fifty points around the screen, alternating between an
outer ring and an inner one -- so all four of those are visible at once. The
zig-zag between the rings is what gives every leg a long diagonal run with a
visible arc over it; a plain circuit of fifty points would be fifty short hops.

`via` takes waypoints exactly as `glide` does, with one difference worth
watching for: **this lands on each waypoint**, where a glide passes through at
whatever distance-spacing gives. That is deliberate, and it is why the pointer
touches every point of the rosette rather than cutting the corners.

**This moves your real pointer** for as long as it runs -- about ten seconds by
default. It touches no keys and no buttons. `--dry-run` prints the waypoints
and moves nothing, and `--seed N` picks a different shape: left alone the seed
is derived from the move itself, so the same tour replays identically every
time it is run, which is the property a recorded take depends on.

    python3 examples/10_natural_mouse.py
    python3 examples/10_natural_mouse.py --dry-run
    python3 examples/10_natural_mouse.py --waypoints 12 --duration 4 --seed 7
    python3 examples/10_natural_mouse.py --arc 0 --wobble 0     # a straight line
    python3 examples/10_natural_mouse.py --duration 25 --pause 1.5 --latency 0.4

`--duration` is the wall-clock time the *motion* is spread over, and on a session
that injects directly -- `/dev/uinput`, libei, XTest -- that is what you get. It
is a floor rather than a promise where the input backend spawns a process per
event, which `xdotool`, `wtype` and `ydotool` all do: the default tour is about
1200 events, and 1200 subprocesses is a good deal more than ten seconds. The
run prints the backend it ended up with; `pyguitest doctor` says why.

Every knob the method has is on the command line: `--duration` for speed,
`--latency` for the pause before the pointer starts, `--pause` for one
hesitation part-way through, and `--arc`/`--wobble`/`--overshoot` for the shape.
`--arc 0 --wobble 0` is worth running once beside the default, because it is
what shows what the shaping is actually doing.

`pace` is deliberately not here. It derives a duration from distance when no
duration is given, and it clamps that to a plausible range -- so on a fifty-leg
tour the clamp decides the answer and the number you pass would not matter. It
earns its keep on a single move, which is what the method's own default is for.

Needs pointer injection -- `pyguitest doctor` names whatever is missing -- and,
unless `--size` is given, `Capability.SCREEN_INFO` to know how big the screen is.
"""

from __future__ import annotations

import argparse
import math
import sys

import pyguitest
from pyguitest import Capability

MARGIN = 0.06
"""How much of each edge to keep away from, as a fraction of the screen.

Screens have panels, docks and hot corners along their edges, and a
demonstration whose pointer grazes one goes somewhere unexpected instead.
Six percent of 1920x1080 is about 65px off the short edge, and about 115px
off the long one.
"""

INNER = 0.45
"""The inner ring's radius, as a fraction of the outer one."""


def bail(message):
    """Explain what is missing, and point at the thing that lists it."""
    print(
        f"{message}\nRun `pyguitest doctor` for what this desktop supports and "
        "what to install, or example 01 for the same list in code."
    )
    return 2


def parse_args(argv):
    """Command line, which is small on purpose."""
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--waypoints",
        type=int,
        default=50,
        help="points on the tour, alternating between the two rings (50)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=10.0,
        help="seconds for the whole tour (10) -- the speed knob for a tour this long",
    )
    parser.add_argument(
        "--latency",
        type=float,
        default=None,
        metavar="S",
        help="pause before the pointer starts moving, in seconds; default is "
        "the session's own event_delay, so one setting paces every script",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=0.0,
        metavar="S",
        help="one hesitation part-way through the tour, in seconds (0); what "
        "breaks a long move into two",
    )
    parser.add_argument(
        "--arc",
        type=float,
        default=0.15,
        help="how far each leg bows off the straight line, as a fraction of "
        "that leg's length (0.15); --arc 0 is a straight line",
    )
    parser.add_argument(
        "--wobble",
        type=float,
        default=1.0,
        help="pixels of slow drift across the path (1.0); --wobble 0 is none",
    )
    parser.add_argument(
        "--overshoot",
        type=float,
        default=0.04,
        metavar="F",
        help="fraction of the last leg to run past the final point before "
        "correcting back onto it (0.04)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="fix the shape; default derives it from the move itself, which "
        "makes every run of the same tour identical",
    )
    parser.add_argument(
        "--size",
        metavar="WxH",
        help="screen size, for a session that cannot report one itself",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the waypoints and move nothing",
    )
    args = parser.parse_args(argv)
    if args.waypoints < 4:
        parser.error("--waypoints needs at least 4 to make a shape")
    if args.duration <= 0:
        parser.error("--duration must be positive")
    for name in ("latency", "pause", "arc", "wobble", "overshoot"):
        value = getattr(args, name)
        if value is not None and value < 0:
            parser.error(f"--{name} must not be negative")
    if args.size:
        try:
            width, height = (int(part) for part in args.size.lower().split("x"))
        except ValueError:
            parser.error(f"--size wants WxH, like 1920x1080, not {args.size!r}")
        args.size = (width, height)
    return args


def rosette(width, height, count):
    """`count` waypoints alternating between an outer and an inner ellipse.

    In pixel coordinates, centred on the screen. Alternating the radius on
    each step is what makes the crossing diagonals; going round one ring at a
    time would be a plain circuit, and the arches -- which are the thing being
    demonstrated -- would have nothing to arch over.
    """
    centre_x, centre_y = width / 2, height / 2
    span_x = width / 2 * (1 - MARGIN * 2)
    span_y = height / 2 * (1 - MARGIN * 2)
    points = []
    for index in range(count):
        angle = 2 * math.pi * index / count
        # Odd steps pull the point in towards the middle: the alternation is
        # what makes the crossing diagonals, and therefore the arches.
        scale = 1.0 if index % 2 == 0 else INNER
        points.append(
            (
                round(centre_x + span_x * scale * math.cos(angle)),
                round(centre_y + span_y * scale * math.sin(angle)),
            )
        )
    return points


def main(argv=None):
    """Build the tour, then walk it -- or print it, with `--dry-run`."""
    args = parse_args(sys.argv[1:] if argv is None else argv)

    gui = pyguitest.connect()
    if not gui.supports(Capability.POINTER_MOVE):
        return bail("This session cannot move the pointer.")

    if args.size:
        width, height = args.size
    elif gui.supports(Capability.SCREEN_INFO):
        width, height = gui.screens()[0].size
    else:
        return bail(
            "This session cannot report the screen size, and the rosette is "
            "built from it. Pass --size WxH and this will run anyway."
        )

    points = rosette(width, height, args.waypoints)
    print(
        f"screen {width}x{height}, backend {gui.backend.name!r} -- "
        f"{len(points)} points over {args.duration:g}s"
    )

    if args.dry_run:
        print("\n--dry-run: the points, in the order they are visited")
        for number, (x, y) in enumerate(points, 1):
            print(f"  {number:>2}. ({x:>4}, {y:>4})")
        print(
            "\nNothing was moved. Every one of those is landed on exactly, "
            "which is\nthe difference from glide(): that one passes through "
            "its waypoints."
        )
        return 0

    start, end = points[0], points[-1]
    # The first point is a plain teleport, deliberately. The tour has to begin
    # somewhere, and gliding in from wherever the pointer happened to be would
    # make the shape depend on where it started rather than on the tour.
    gui.move_mouse(*start)

    print("moving -- Ctrl-C stops it and leaves the pointer where it is\n")
    try:
        # `via` is the middle of the tour and `end` is the target, so all fifty
        # points are visited: the first by the teleport above, the last as the
        # move's own destination, the other forty-eight as waypoints.
        gui.move_mouse_naturally(
            *end,
            via=points[1:-1],
            duration=args.duration,
            latency=args.latency,
            pause=args.pause,
            arc=args.arc,
            wobble=args.wobble,
            overshoot=args.overshoot,
            seed=args.seed,
        )
    except KeyboardInterrupt:
        print("\ninterrupted -- the pointer is wherever it had got to.")
        return 1

    # Park it in the middle rather than leaving it on a corner, where whatever
    # is underneath would carry on reacting after the script has exited.
    gui.move_mouse(width // 2, height // 2)
    print(
        f"\ndone -- {len(points)} points visited, "
        + (
            f"seed {args.seed}."
            if args.seed is not None
            else "seeded from the move itself, so this tour is identical every run."
        )
    )
    print("Run it again with --seed N for a different shape over the same tour.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
