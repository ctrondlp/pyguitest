"""Diagnostics and migration scanning.

    pyguitest                    what this desktop can actually do
    pyguitest doctor             what to install to unlock more
    pyguitest debug              everything needed to diagnose a bug report
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

from . import __version__, connect, tools
from .capabilities import TIERS, Tier
from .compat import LEGACY
from .hints import advice, detect_distro, hints_for

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
        },
        "env_vars": {name: os.environ.get(name) for name in _ENV_VARS_OF_INTEREST},
        "backend": gui.backend.name,
        "capabilities": sorted(c.name for c in gui.capabilities),
        "capabilities_report": gui.capabilities.report(),
        "backend_report": backend_report() if callable(backend_report) else None,
    }


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

    for group in ("input", "capture", "window", "image"):
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
    migrate = sub.add_parser("migrate", help="scan Perl sources for X11::GUITest calls")
    migrate.add_argument("paths", nargs="+")

    args = parser.parse_args(argv)
    if args.command == "migrate":
        return _scan(args.paths)
    if args.command == "doctor":
        return _doctor()
    if args.command == "debug":
        return _debug(json_output=args.json)
    return _report()


if __name__ == "__main__":
    sys.exit(main())
