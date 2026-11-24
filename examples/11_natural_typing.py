#!/usr/bin/env python3
"""Type a passage to a human rhythm, and measure the rhythm that came out.

`type_text` sends every character with the same gap after it.
`type_text_naturally` shapes the time instead: a rate in words per minute, a
rhythm that follows the text and the fingers, a slow drift that makes the rate
wax and wane across the passage, lognormal scatter on each individual gap, and
optionally a typo that notices itself and presses Backspace.

The part worth watching for is the drift. The *mean* rate stays the `--wpm`
asked for -- the gaps are scaled to it after being shaped, so no amount of
shaping can drag the overall speed off -- while the instantaneous rate moves
around it. This measures both, from the keystrokes themselves rather than from
the model: the overall WPM, and the WPM over each window of `--window`
characters, which is where the fluctuation shows.

How it measures is worth reading too, because it is a useful trick in its own
right and uses nothing private: `gui.backend.type_text` is wrapped, so every
character the method sends is timestamped on its way through. A prediction
would have been easier and would have proved nothing about what the backend
actually did with the schedule.

**This types into whatever holds keyboard focus.** It focuses nothing itself,
because nothing it could focus would be the window you meant. Open a scratch
editor, give it focus, and use `--countdown` to buy yourself time to do that;
`--dry-run` connects to nothing and sends no keystrokes at all.

    python3 examples/11_natural_typing.py --dry-run
    python3 examples/11_natural_typing.py --countdown 5
    python3 examples/11_natural_typing.py --wpm 35 --drift 0.4
    python3 examples/11_natural_typing.py --typos 0.05 --seed 7
    python3 examples/11_natural_typing.py --rhythm 0 --drift 0 --jitter 0

That last one is worth running once beside the defaults: it is an even stream
at the same average rate, and the difference between the two window tables is
the whole of what this method does.

`--text` takes a passage of your own, and `--seed` fixes the shape. Left alone
the seed comes from the text itself, so the same passage replays identically --
the property a recorded take depends on, and the reason a test on the rhythm
cannot flake.

Needs `Capability.TEXT_ENTRY`, and `KEY_EVENT` as well with `--typos`.
`pyguitest doctor` names whatever is missing.
"""

from __future__ import annotations

import argparse
import sys
import time

import pyguitest
from pyguitest import Capability

PASSAGE = (
    "The quick brown fox jumps over the lazy dog. "
    "Pack my box with five dozen liquor jugs. "
    "How vexingly quick daft zebras jump!"
)
"""Default passage: pangrams, which exercise every finger and both hands.

Prose rather than a word list on purpose -- the rhythm reacts to word, clause
and sentence boundaries, and a passage with none of those would hide most of
what there is to see.
"""

WORD_CHARS = 5
"""Characters to a word, the convention every WPM figure is quoted in."""

BACKSPACE = "\b"
"""What a correction is recorded as, so the report can tell it from a space."""


def shown(what):
    """A keystroke as something readable in a report line."""
    return "<BS>" if what == BACKSPACE else what


def bail(message):
    """Explain what is missing, and point at the thing that lists it."""
    print(
        f"{message}\nRun `pyguitest doctor` for what this desktop supports and "
        "what to install, or example 01 for the same list in code."
    )
    return 2


def parse_args(argv):
    """Command line: every knob the method has, and nothing else."""
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--text", default=PASSAGE, help="the passage to type")
    parser.add_argument(
        "--wpm",
        type=float,
        default=52.0,
        help="mean rate in words per minute, five characters to the word "
        "(52, the published population mean)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        metavar="S",
        help="seconds for the whole passage; overrides --wpm",
    )
    parser.add_argument(
        "--rhythm",
        type=float,
        default=1.0,
        help="how strongly the text's own structure stretches the gaps -- "
        "word and sentence boundaries, same-finger reaches (1.0)",
    )
    parser.add_argument(
        "--drift",
        type=float,
        default=0.2,
        help="how far the rate wanders above and below the mean, in slow "
        "swells across the passage (0.2, about a fifth either way)",
    )
    parser.add_argument(
        "--jitter",
        type=float,
        default=0.35,
        help="lognormal scatter on each individual gap (0.35)",
    )
    parser.add_argument(
        "--typos",
        type=float,
        default=0.0,
        metavar="P",
        help="probability per character of hitting a neighbouring key, "
        "noticing, and correcting it (0); it dirties the field, so it is "
        "off unless asked for",
    )
    parser.add_argument(
        "--latency",
        type=float,
        default=None,
        metavar="S",
        help="pause before the first keystroke; default is the session's "
        "own event_delay, so one setting paces every script",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=0.0,
        metavar="S",
        help="one hesitation part-way through, at a word boundary (0)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="fix the rhythm; default derives it from the text, which makes "
        "every run of the same passage identical",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=20,
        metavar="N",
        help="characters per window in the measured rate table (20)",
    )
    parser.add_argument(
        "--countdown",
        type=int,
        default=0,
        metavar="N",
        help="seconds to wait before typing, to go and focus a window (0)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="describe the run and send no keystrokes",
    )
    args = parser.parse_args(argv)
    if not args.text:
        parser.error("--text needs something to type")
    if args.wpm <= 0:
        parser.error("--wpm must be positive")
    if args.window < 2:
        parser.error("--window needs at least 2 characters to average over")
    for name in ("duration", "rhythm", "drift", "jitter", "typos", "latency", "pause"):
        value = getattr(args, name)
        if value is not None and value < 0:
            parser.error(f"--{name} must not be negative")
    if args.typos > 1:
        parser.error("--typos is a probability, so at most 1")
    if args.countdown < 0:
        parser.error("--countdown must not be negative")
    return args


class Observer:
    """Timestamps every keystroke on its way to the backend.

    Wraps the two calls `type_text_naturally` makes -- `type_text` for a
    character and `press_key` for the Backspace of a correction -- and leaves
    them doing their job afterwards. Nothing private is touched: a backend is
    a public object with public methods, and this only notes when they were
    called.
    """

    def __init__(self, backend):
        """Wrap `backend`'s typing calls, keeping the originals to call on."""
        self.backend = backend
        self.strokes = []
        """(seconds since the first keystroke, what was sent)."""
        self.began = 0.0
        self._typed = backend.type_text
        self._pressed = backend.press_key
        backend.type_text = self._on_type
        backend.press_key = self._on_press

    def _note(self, what):
        now = time.monotonic()
        if not self.strokes:
            self.began = now
        self.strokes.append((now - self.began, what))

    def _on_type(self, text, **kwargs):
        self._note(text)
        self._typed(text, **kwargs)

    def _on_press(self, key):
        self._note(BACKSPACE)
        self._pressed(key)

    def restore(self):
        """Put the backend's own methods back."""
        self.backend.type_text = self._typed
        self.backend.press_key = self._pressed

    @property
    def gaps(self):
        """The measured gap before each keystroke after the first."""
        return [
            self.strokes[i][0] - self.strokes[i - 1][0]
            for i in range(1, len(self.strokes))
        ]


def rate_table(strokes, window):
    """Lines of "WPM over these N keystrokes", which is where drift shows.

    Converted back to words per minute at the same five-characters-to-the-word
    convention the rate itself uses. The overall figure comes out at whatever
    was asked for; these do not, and are not meant to.
    """
    lines = []
    # N keystrokes hold N-1 intervals, so a window's span runs from its
    # first stroke to its last -- not to the stroke after it, which borrowed
    # an interval from the next window and left a run of exactly `window`
    # strokes printing no row at all.
    for start in range(0, len(strokes) - window + 1, window):
        span = strokes[start + window - 1][0] - strokes[start][0]
        rate = (window - 1) / span * 60 / WORD_CHARS if span else float("inf")
        window_text = "".join(
            shown(what) for _, what in strokes[start : start + window]
        )
        lines.append(f"  {rate:6.1f} wpm   {window_text.replace(chr(10), ' ')!r}")
    return lines


def report(observer, text, window):
    """Print what was measured: the overall rate, and how uneven it was."""
    strokes = observer.strokes
    gaps = observer.gaps
    if not gaps:
        print("nothing was typed, so there is no rhythm to report")
        return
    total = strokes[-1][0]
    # Measured from what was recorded, not from what was asked for: a run
    # stopped by Ctrl-C has delivered only part of the passage, and dividing
    # the full request by the short elapsed time inflated the rate. Each
    # correction is a slip, a Backspace and a retype -- three strokes that
    # net one character -- so delivered = strokes - 2 x backspaces.
    backspaces = sum(1 for _, what in strokes if what == BACKSPACE)
    delivered = len(strokes) - 2 * backspaces
    print(
        f"\n{delivered} of {len(text)} characters, {len(strokes)} keystrokes, "
        f"{total:.2f}s -> {delivered / total * 60 / WORD_CHARS:.1f} wpm "
        "measured overall"
    )
    print(
        f"gaps: shortest {min(gaps) * 1000:.0f}ms, longest "
        f"{max(gaps) * 1000:.0f}ms, mean {total / len(gaps) * 1000:.0f}ms"
    )
    if backspaces:
        print(
            f"{backspaces} corrections, so {len(strokes)} keystrokes for "
            f"{delivered} characters -- a slip, a Backspace and a retype each"
        )
    print(f"\nrate over each {window} keystrokes:")
    for line in rate_table(strokes, window):
        print(line)
    print("\nthe five longest gaps, and what they fell in front of:")
    ranked = sorted(zip(gaps, range(1, len(strokes)), strict=True), reverse=True)
    for gap, index in ranked[:5]:
        print(
            f"  {gap * 1000:6.0f}ms before {shown(strokes[index][1])!r} "
            f"(after {shown(strokes[index - 1][1])!r})"
        )


def budget_for(text, args):
    """The schedule's own length, which is what the measurement is against.

    n characters have n-1 gaps: there is nothing to wait for after the last
    keystroke. A correction spends over this, which is why a run with
    `--typos` legitimately overruns it.
    """
    if args.duration is not None:
        return args.duration
    return (len(text) - 1) * 60.0 / (args.wpm * WORD_CHARS)


def main(argv=None):
    """Type the passage, measuring every keystroke -- or describe the run."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    text = args.text
    budget = budget_for(text, args)

    print(
        f"{len(text)} characters at {args.wpm:g} wpm -> a {budget:.2f}s "
        f"schedule, mean gap {budget / max(1, len(text) - 1) * 1000:.0f}ms"
    )
    print(
        f"rhythm {args.rhythm:g}, drift {args.drift:g}, "
        f"jitter {args.jitter:g}, typos {args.typos:g}, "
        + (
            f"seed {args.seed}"
            if args.seed is not None
            else "seeded from the text, so this passage replays identically"
        )
    )

    if args.dry_run:
        print(f"\n--dry-run: nothing was typed, and nothing connected.\n\n{text}")
        print(
            "\nRun it without --dry-run for the measured rate table -- the "
            "fluctuation\nis a property of the keystrokes, so only real ones show it."
        )
        return 0

    gui = pyguitest.connect()
    if not gui.supports(Capability.TEXT_ENTRY):
        return bail("This session cannot type text.")
    if args.typos and not gui.supports(Capability.KEY_EVENT):
        return bail("--typos needs a Backspace, and this session has no key events.")

    print(f"backend {gui.backend.name!r}")
    if args.countdown:
        print("focus the window you want this typed into:")
        for remaining in range(args.countdown, 0, -1):
            print(f"  {remaining}... ", end="\r", flush=True)
            time.sleep(1)
        print("  typing now")
    else:
        print("typing into whatever has focus -- Ctrl-C stops it mid-passage")

    observer = Observer(gui.backend)
    try:
        gui.type_text_naturally(
            text,
            duration=args.duration,
            wpm=args.wpm,
            rhythm=args.rhythm,
            drift=args.drift,
            jitter=args.jitter,
            typos=args.typos,
            latency=args.latency,
            pause=args.pause,
            seed=args.seed,
        )
    except KeyboardInterrupt:
        print("\ninterrupted -- the passage got as far as it got.")
        report(observer, text, args.window)
        return 1
    finally:
        observer.restore()

    report(observer, text, args.window)
    # `--pause` and a correction are both extra over the schedule by design,
    # so neither counts as an overrun. Only the backend's per-character cost
    # can make the run itself miss, which is what this is looking for.
    allowed = budget + args.pause
    overrun = observer.strokes[-1][0] - allowed if observer.strokes else 0.0
    if overrun > allowed * 0.1 and not args.typos:
        print(
            f"\nThe run overran its schedule by {overrun:.2f}s. This backend "
            "costs more per character than the rate allows, so the rhythm was "
            "flattened towards as-fast-as-it-goes; a lower --wpm is the honest "
            "fix. (`xdotool`, `wtype` and `ydotool` spawn a process per call.)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
