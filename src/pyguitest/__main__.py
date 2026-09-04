"""Diagnostics and migration scanning.

    pyguitest                    what this desktop can actually do
    pyguitest doctor             what to install to unlock more
    pyguitest debug              everything needed to diagnose a bug report
    pyguitest inspect            the accessible tree of every open window
    pyguitest migrate script.pl  what porting that script involves

Also runnable as `python -m pyguitest` when the package is on the path but the
console script is not installed.

The migration scan is a lexical pass over Perl sources: it finds X11::GUITest
calls and reports the tier each lands in, so the cost of a port can be read off
before any code is written.
"""

import argparse
import dataclasses
import json
import os
import pathlib
import platform
import re
import sys
from collections import Counter
from enum import Enum

from . import Capability, __version__, connect, tools
from .backends.atspi import a11y_bus_probe
from .capabilities import TIERS, Tier
from .compat import LEGACY
from .hints import advice, detect_distro, hints_for
from .inspect import format_tree, tree_data
from .session import toolkit_accessibility

_CALL = re.compile(r"\b(" + "|".join(sorted(LEGACY, key=len, reverse=True)) + r")\b")

# Env vars the desktop-detection logic in session.py actually reads
# (session_type/compositor classification, plus the sway/Hyprland IPC
# sockets). Printed as-is: none of these carry secrets, unlike
# DBUS_SESSION_BUS_ADDRESS, which is deliberately not on this list.
_ENV_VARS_OF_INTEREST = (
    "XDG_SESSION_TYPE",
    "XDG_CURRENT_DESKTOP",
    "XDG_SESSION_DESKTOP",
    "DESKTOP_SESSION",
    "WAYLAND_DISPLAY",
    "DISPLAY",
    "SWAYSOCK",
    "HYPRLAND_INSTANCE_SIGNATURE",
)

_OS_RELEASE = pathlib.Path("/etc/os-release")
_HOST_OS_RELEASE = pathlib.Path("/run/host/os-release")
"""Where Flatpak exposes the host's own os-release from inside the sandbox.

Every other probe in `debug` -- PATH, /etc/os-release, /dev/uinput -- sees
the sandbox's view, not the host's, so this is the one place a report from
inside a Flatpak can still say what the host actually is."""


def _report() -> int:
    """Print what the current desktop can do."""
    with connect() as gui:
        print(gui.report())
        # capabilities.missing is non-empty on virtually every desktop --
        # tier 6 is unreachable on Wayland by design, not by a missing
        # package -- so gating on it printed a table full of [ no] followed
        # by advice() truthfully saying nothing installable is missing.
        # hints_for() is what actually reasons about installed components.
        bridge = toolkit_accessibility()
        if list(
            hints_for(
                gui.environment,
                capabilities=gui.capabilities,
                toolkit_accessibility=bridge,
            )
        ):
            print()
            print(
                advice(
                    gui.environment,
                    capabilities=gui.capabilities,
                    toolkit_accessibility=bridge,
                )
            )
    return 0


def _doctor() -> int:
    """Print only what is missing and how to install it."""
    with connect() as gui:
        print(gui.environment.summary())
        print()
        print(
            advice(
                gui.environment,
                capabilities=gui.capabilities,
                toolkit_accessibility=toolkit_accessibility(),
            )
        )
    return 0


def _sandbox_kind() -> str | None:
    """Which container/sandbox this process is running in, if any.

    Best-effort against the marker each mechanism documents for itself, not
    a guarantee no others exist. Worth knowing here specifically because
    everything else `debug` reports -- PATH, /etc/os-release, /dev/uinput --
    reflects the sandbox's view rather than the host's, and a bug report
    that does not say so describes a machine the maintainer cannot
    reproduce.
    """
    if os.path.exists("/.flatpak-info"):
        return "flatpak"
    if os.path.exists("/run/.containerenv"):
        return "toolbox/podman"
    if os.path.exists("/.dockerenv"):
        return "docker"
    return None


def _os_release_pretty(path: pathlib.Path) -> str | None:
    """PRETTY_NAME (or NAME plus VERSION_ID) from an os-release file.

    Separate from hints.detect_distro(), which buckets the same file into a
    package-manager family for install advice -- a coarser answer than a
    bug report needs. This is for display: the literal distro string.
    """
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    fields = {}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key in ("PRETTY_NAME", "NAME", "VERSION_ID"):
            fields[key] = value.strip().strip('"')
    if "PRETTY_NAME" in fields:
        return fields["PRETTY_NAME"]
    if "NAME" in fields:
        version = fields.get("VERSION_ID")
        return f"{fields['NAME']} {version}" if version else fields["NAME"]
    return None


def _env_field_value(value):
    """A JSON-safe form of one Environment field's value."""
    return value.value if isinstance(value, Enum) else value


def _tool_reports(group):
    """present/path/version for every tool in a tools.py group."""
    return [
        {
            "name": tool.name,
            "present": tool.present,
            "path": tool.path(),
            "version": tool.version() if tool.present else None,
        }
        for tool in group
    ]


def _debug_data(gui) -> dict:
    """Everything `debug` reports, as one JSON-serializable structure.

    The single source both output formats draw from, so the text report and
    --json can never say two different things about the same machine.
    """
    environment = gui.environment
    sandbox = _sandbox_kind()
    host_release = _HOST_OS_RELEASE if sandbox else None
    backend_report = getattr(gui.backend, "report", None)
    return {
        "pyguitest_version": __version__,
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "platform": platform.platform(),
        "sandbox": sandbox,
        "distro": {
            "family": detect_distro(),
            "pretty": _os_release_pretty(_OS_RELEASE),
            "host_family": detect_distro(host_release.read_text(errors="replace"))
            if host_release is not None and host_release.exists()
            else None,
            "host_pretty": _os_release_pretty(host_release)
            if host_release is not None
            else None,
        },
        "environment": {
            f.name: _env_field_value(getattr(environment, f.name))
            for f in dataclasses.fields(environment)
        },
        "tools": {
            "input": _tool_reports(tools.INPUT_TOOLS),
            "capture": _tool_reports(tools.CAPTURE_TOOLS),
            "window": _tool_reports(tools.WINDOW_TOOLS),
            "image": _tool_reports(tools.IMAGE_TOOLS),
            # Listed like the rest even though the answer on Mutter is
            # always "none": that a desktop has no clipboard tool is
            # exactly what a pasted bug report about the clipboard needs
            # to say, and `environment.clipboard_tools` a few lines above
            # already reports it -- these two disagreeing was the bug.
            "clipboard": _tool_reports(tools.CLIPBOARD_TOOLS),
        },
        "env_vars": {name: os.environ.get(name) for name in _ENV_VARS_OF_INTEREST},
        "backend": gui.backend.name,
        "capabilities": sorted(c.name for c in gui.capabilities),
        "capabilities_report": gui.capabilities.report(),
        "backend_report": backend_report() if callable(backend_report) else None,
        "focus_tracking": _focus_tracking(gui),
        # Reported on every desktop, not only where a hint fires for it.
        # `doctor` warns about this on KDE alone, because that is the only
        # place it was observed to break anything (see hints.py) -- but the
        # value is a fact, and a fact is what a pasted diagnostic is for. If
        # it turns out to matter on some other desktop, this line is what
        # will show it.
        "toolkit_accessibility": toolkit_accessibility(),
        # The other half of the AT-SPI story, and the one that used to end
        # the process rather than a line of output: see
        # backends.atspi.a11y_bus_reachable. `has_atspi` above says the
        # library is installed, which is a different claim from the bus
        # answering, and a bug report about "no elements" needs both.
        "a11y_bus": a11y_bus_probe(),
    }


def _focus_tracking(gui) -> bool | None:
    """Whether per-widget focus is readable here, or None if unknowable.

    A live probe rather than a declared capability -- see
    Session.focus_tracking_works. None means the question could not be
    asked at all (no element tree on this desktop), which is a different
    answer from "asked, and focus is not published".
    """
    try:
        return gui.focus_tracking_works()
    except Exception:  # noqa: BLE001 -- diagnostics never fail the report
        return None


def _format_debug(data: dict) -> str:
    """Render `_debug_data`'s output as the pasteable text report."""
    lines = [
        f"pyguitest    {data['pyguitest_version']}",
        f"python       {data['python']['version']} "
        f"({data['python']['implementation']})",
        f"platform     {data['platform']}",
    ]
    if data["sandbox"]:
        lines.append(f"sandbox      {data['sandbox']}")
    distro = data["distro"]
    lines.append(f"distro       {distro['pretty'] or distro['family'] or 'unknown'}")
    if distro["host_pretty"] or distro["host_family"]:
        lines.append(
            "host distro  "
            f"{distro['host_pretty'] or distro['host_family']} "
            "(outside the sandbox)"
        )

    lines.append("")
    lines.append("environment")
    for name, value in data["environment"].items():
        lines.append(f"  {name:<16} {value!r}")

    for group in ("input", "capture", "window", "image", "clipboard"):
        lines.append("")
        lines.append(f"{group} tools")
        for tool in data["tools"][group]:
            if not tool["present"]:
                lines.append(f"  {tool['name']:<18} not found")
                continue
            version = tool["version"] or "version unknown"
            lines.append(f"  {tool['name']:<18} {tool['path']}  ({version})")

    lines.append("")
    lines.append("environment variables")
    for name, value in data["env_vars"].items():
        lines.append(f"  {name:<26} {value if value is not None else 'unset'}")

    lines.append("")
    lines.append(f"backend      {data['backend']}")
    focus = data["focus_tracking"]
    lines.append(
        "focus        "
        + {
            True: "per-widget focus is published and readable",
            False: "NOT published on this desktop -- assert_focused/"
            "assert_tab_order cannot match a widget here",
            None: "not probed (no element tree on this desktop)",
        }[focus]
    )
    lines.append(
        "a11y bus     "
        + {
            True: "org.a11y.Bus answers",
            False: "org.a11y.Bus did NOT answer -- AT-SPI is unavailable "
            "here; install at-spi2-core or start at-spi-bus-launcher",
            None: "not asked (no gdbus) -- AT-SPI is attempted anyway",
        }[data["a11y_bus"]]
    )
    lines.append(
        "toolkit      "
        + {
            True: "toolkit-accessibility on",
            False: "toolkit-accessibility OFF -- harmless on GNOME, but on "
            "KDE no GTK application publishes elements",
            None: "toolkit-accessibility not readable (no PyGObject, or no "
            "GNOME schemas installed)",
        }[data["toolkit_accessibility"]]
    )
    if data["backend_report"]:
        lines.append("")
        lines.append(data["backend_report"])

    lines.append("")
    lines.append(data["capabilities_report"])

    return "\n".join(lines)


def _debug(json_output: bool = False) -> int:
    """Print everything needed to diagnose a bug report.

    Deliberately wider than `doctor`, which answers "what should I
    install" -- this answers "what does this machine actually look like":
    versions, every Environment probe (true and false, not only what
    summary() shows), each detected tool's own --version, whether the
    process is sandboxed, and the raw capability table. Paste the output
    into a bug report rather than describing the setup in prose.
    """
    with connect() as gui:
        data = _debug_data(gui)
    print(json.dumps(data, indent=2) if json_output else _format_debug(data))
    return 0


def _inspect(json_output: bool = False, window: str | None = None) -> int:
    """Print the accessible tree of every open window.

    Needs Capability.ELEMENT_TREE -- printed as install advice rather than
    a traceback when it is missing, the same way every other capability gap
    in this file is reported.
    """
    with connect() as gui:
        if not gui.supports(Capability.ELEMENT_TREE):
            # Threaded here too so every advice() in this file reports
            # the same set of hints; an unthreaded call silently drops
            # the accessibility-bridge one.
            print(
                advice(
                    gui.environment,
                    capabilities=gui.capabilities,
                    toolkit_accessibility=toolkit_accessibility(),
                )
            )
            return 1
        data = tree_data(gui, window=window)
    print(json.dumps(data, indent=2) if json_output else format_tree(data))
    return 0


def _scan(paths):
    """Report the migration cost of each X11::GUITest call in `paths`."""
    hits: Counter[str] = Counter()
    per_file: dict[str, Counter[str]] = {}

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
    by_tier: Counter[Tier] = Counter()
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
    debug = sub.add_parser(
        "debug", help="everything needed to diagnose a bug report, pasteable as-is"
    )
    debug.add_argument("--json", action="store_true", help="machine-readable output")
    inspect_parser = sub.add_parser(
        "inspect", help="the accessible tree of every open window"
    )
    inspect_parser.add_argument(
        "--json", action="store_true", help="machine-readable output"
    )
    inspect_parser.add_argument(
        "--window", help="only windows whose title matches this regex"
    )
    migrate = sub.add_parser("migrate", help="scan Perl sources for X11::GUITest calls")
    migrate.add_argument("paths", nargs="+")

    args = parser.parse_args(argv)
    if args.command == "migrate":
        return _scan(args.paths)
    if args.command == "doctor":
        return _doctor()
    if args.command == "debug":
        return _debug(json_output=args.json)
    if args.command == "inspect":
        return _inspect(json_output=args.json, window=args.window)
    return _report()


if __name__ == "__main__":
    sys.exit(main())
