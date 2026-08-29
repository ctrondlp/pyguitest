"""Diagnostics and migration scanning.

    pyguitest                    what this desktop can actually do
    pyguitest doctor             what to install to unlock more
    pyguitest migrate script.pl  what porting that script involves

Also runnable as `python -m pyguitest` when the package is on the path but the
console script is not installed.

The migration scan is a lexical pass over Perl sources: it finds X11::GUITest
calls and reports the tier each lands in, so the cost of a port can be read off
before any code is written.
"""

import argparse
import re
import sys
from collections import Counter

from . import __version__, connect
from .capabilities import TIERS, Tier
from .compat import LEGACY
from .hints import advice, hints_for

_CALL = re.compile(r"\b(" + "|".join(sorted(LEGACY, key=len, reverse=True)) + r")\b")


def _report() -> int:
    """Print what the current desktop can do."""
    with connect() as gui:
        print(gui.report())
        # capabilities.missing is non-empty on virtually every desktop --
        # tier 6 is unreachable on Wayland by design, not by a missing
        # package -- so gating on it printed a table full of [ no] followed
        # by advice() truthfully saying nothing installable is missing.
        # hints_for() is what actually reasons about installed components.
        if list(hints_for(gui.environment)):
            print()
            print(advice(gui.environment))
    return 0


def _doctor() -> int:
    """Print only what is missing and how to install it."""
    with connect() as gui:
        print(gui.environment.summary())
        print()
        print(advice(gui.environment))
    return 0


def _scan(paths):
    """Report the migration cost of each X11::GUITest call in `paths`."""
    hits = Counter()
    per_file = {}

    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                source = handle.read()
        except OSError as exc:
            print(f"{path}: {exc}", file=sys.stderr)
            continue
        found = Counter(_CALL.findall(source))
        if found:
            per_file[path] = found
            hits.update(found)

    if not hits:
        print("No X11::GUITest calls found.")
        return 0

    for path, found in per_file.items():
        print(f"\n{path}")
        for name, count in sorted(found.items()):
            fn = LEGACY[name]
            label = name + (f" x{count}" if count > 1 else "")
            arrow = fn.replacement or "(no replacement -- rethink this call)"
            print(f"  T{int(fn.tier)}  {label:<26} -> {arrow}")
            if fn.note:
                print(f"      {fn.note}")

    print("\nSummary")
    by_tier = Counter()
    for name, count in hits.items():
        by_tier[LEGACY[name].tier] += count
    for tier in Tier:
        if by_tier[tier]:
            print(f"  T{int(tier)} {tier.name:11} {by_tier[tier]:3}  {TIERS[tier]}")

    blocked = [n for n in hits if LEGACY[n].tier is Tier.NO_PATH]
    if blocked:
        blocked_calls = sum(hits[n] for n in blocked)
        print(
            f"\n{blocked_calls} call(s) across {len(blocked)} distinct name(s) have "
            "no Wayland path and need rethinking: " + ", ".join(sorted(blocked))
        )
    return 1 if blocked else 0


def main(argv=None):
    """Entry point for `python -m pyguitest`."""
    parser = argparse.ArgumentParser(
        prog="pyguitest", description=__doc__.split("\n")[0]
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("doctor", help="what to install to unlock more capabilities")
    migrate = sub.add_parser("migrate", help="scan Perl sources for X11::GUITest calls")
    migrate.add_argument("paths", nargs="+")

    args = parser.parse_args(argv)
    if args.command == "migrate":
        return _scan(args.paths)
    if args.command == "doctor":
        return _doctor()
    return _report()


if __name__ == "__main__":
    sys.exit(main())
