#!/usr/bin/env python3
"""Regenerate docs/api.md from the source.

The reference is generated rather than written because two of its columns
cannot be kept accurate by hand: the capability a call needs comes from
CompositeBackend._DISPATCH, and the backends providing that capability come
from each backend's own `capabilities` property. Both move whenever a
backend is added, and a hand-maintained table would quietly go stale --
the same failure the composite-dispatch invariant guards against in code.

Run from the repository root:  python3 scripts/gen-api-docs.py
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
import re
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import pyguitest  # noqa: E402
import pyguitest.backends as backends_pkg  # noqa: E402
from pyguitest.backends.composite import _DISPATCH  # noqa: E402
from pyguitest.capabilities import Capability, Tier  # noqa: E402

OUT = ROOT / "docs" / "api.md"

# Backend calls _DISPATCH does not route. Capture is the only one:
# CompositeBackend picks a member for it by hand, because a capture that
# fails must fall back to the next member rather than raise, so it never
# went in the dispatch table.
EXTRA_DISPATCH = {"capture": Capability.SCREEN_CAPTURE}

# Backends that say yes to everything or nothing carry no information.
UNINFORMATIVE = {"CompositeBackend", "NullBackend", "GUIBackend"}

TYPES = ("Element", "Window", "Screen", "ImageMatch", "Application", "Environment")

# Sections of the Session API, in the order a reader meets them. Anything
# unlisted lands in "Other", so a newly added method shows up rather than
# being silently dropped.
GROUPS: list[tuple[str, str, list[str]]] = [
    (
        "Session control",
        "",
        ["supports", "require", "capabilities", "report", "close"],
    ),
    (
        "Elements",
        "The recommended way to drive an application: match on what a widget "
        "is and what it is called, not where it happens to be.",
        [
            "button",
            "text_field",
            "dropdown",
            "checkbox",
            "link",
            "menu_item",
            "element",
            "elements",
            "root_element",
            "window_element",
            "focused",
            "focus_tracking_works",
        ],
    ),
    (
        "Pointer",
        "",
        [
            "move_mouse",
            "glide",
            "click",
            "press_button",
            "release_button",
            "drag",
            "scroll",
        ],
    ),
    (
        "Keyboard",
        "",
        [
            "type_text",
            "send_keys",
            "press_key",
            "release_key",
            "tap_key",
            "press_tab",
        ],
    ),
    (
        "Windows",
        "",
        [
            "windows",
            "find_window",
            "find_windows",
            "active_window",
            "window_at",
            "geometry",
            "move_window",
            "resize_window",
            "activate_window",
            "minimize_window",
            "is_window_open",
            "is_window_viewable",
            "refresh_window",
            "window_events",
        ],
    ),
    (
        "Screen and capture",
        "",
        ["screens", "screenshot", "capture_on_failure", "locate", "locate_image"],
    ),
    ("Clipboard", "", ["get_clipboard", "set_clipboard"]),
    ("Processes", "", ["start_app", "run_app"]),
    (
        "Waiting",
        "Every `wait_*` call polls on an interval unless the backend can do "
        "better; `timeout=None` uses the session default.",
        [
            "wait",
            "sync",
            "wait_until",
            "wait_for_element",
            "wait_until_gone",
            "wait_for_window",
            "wait_window_close",
            "wait_for_process",
            "wait_for_idle",
            "wait_for_file",
        ],
    ),
    (
        "Assertions",
        "Raise on failure with a message naming what was actually found, for "
        "use directly in a test.",
        [
            "assert_focused",
            "assert_clipboard",
            "assert_tab_order",
            "assert_accessible",
            "assert_no_missing_accessible_names",
            "assert_no_duplicate_accessible_names",
        ],
    ),
]


def source_of(obj) -> str:
    """Source text of a function, property or method; '' when unavailable."""
    if isinstance(obj, property):
        obj = obj.fget
    try:
        return inspect.getsource(obj)
    except (TypeError, OSError):
        return ""


def cell(text: str) -> str:
    """Escape a value for a markdown table cell.

    Union annotations are full of `|`, which ends a cell where it stands.
    """
    return text.replace("|", "\\|")


def first_line(obj) -> str:
    """First paragraph of a docstring, collapsed to one line."""
    target = obj.fget if isinstance(obj, property) else obj
    doc = inspect.getdoc(target) or ""
    return re.sub(r"\s+", " ", doc.split("\n\n")[0].strip())


def signature(cls, name: str) -> str:
    """`name(params)` for a method, or bare `name` for a property."""
    static = inspect.getattr_static(cls, name, None)
    if isinstance(static, property):
        return name
    try:
        sig = inspect.signature(getattr(cls, name))
    except (TypeError, ValueError):
        return f"{name}(...)"
    params = [str(p) for n, p in sig.parameters.items() if n != "self"]
    rendered = re.sub(r"'([^']*)'", r"\1", ", ".join(params))
    return f"{name}({rendered})"


def function_signature(fn) -> str:
    """A module-level function's signature, quotes stripped."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return "(...)"
    return re.sub(r"'([^']*)'", r"\1", str(sig))


def providers() -> dict[str, set[str]]:
    """Capability name -> the backend classes declaring it.

    Read from each backend's `capabilities` property rather than a list
    kept here, so a new backend appears without editing this script.
    """
    found: dict[str, set[str]] = {}
    for info in pkgutil.iter_modules(backends_pkg.__path__):
        try:
            module = importlib.import_module(f"pyguitest.backends.{info.name}")
        except Exception:  # a backend whose optional dependency is absent
            continue
        for name, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != module.__name__ or not name.endswith("Backend"):
                continue
            src = source_of(inspect.getattr_static(cls, "capabilities", None))
            for cap in re.findall(r"Capability\.([A-Z_]+)", src):
                found.setdefault(cap, set()).add(name)
    return found


def strip_docs(src: str) -> str:
    """Source with docstrings and comments removed.

    Both mention capabilities freely -- `screenshot`'s docstring explains
    what WINDOW_CAPTURE changes about the image -- and a scan counting
    those as requirements marks calls unavailable that work everywhere.
    """
    try:
        tree = ast.parse(textwrap.dedent(src))
    except SyntaxError:
        return src
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            body.pop(0)
            if not body:
                body.append(ast.Pass())
    return ast.unparse(tree)


def method_capabilities(
    cls, name: str, seen: frozenset[str] = frozenset()
) -> tuple[set[str], set[str]]:
    """(required, optional) capabilities for a Session method.

    A method rarely names its own capability: `button()` defers to
    `element()`, which defers to the backend's `find_element`, and only
    that last hop is in _DISPATCH. Recursing over `self.<name>(...)` calls
    -- private helpers included, since `glide` reaches the pointer through
    `self._walk` -- recovers what the wrapper a caller writes actually
    needs.

    Anything guarded by `supports(...)` is optional rather than required:
    `glide` reads the pointer's start position when POINTER_QUERY exists
    and computes one otherwise, so it is not an X11-only call even though
    it names an X11-only capability.
    """
    if name in seen:
        return set(), set()
    raw = source_of(inspect.getattr_static(cls, name, None))
    if not raw:
        return set(), set()
    src = strip_docs(raw)

    routed = {**_DISPATCH, **EXTRA_DISPATCH}
    required = {
        cap.name for attr, cap in routed.items() if re.search(rf"\.{attr}\s*\(", src)
    }
    optional = set(re.findall(r"supports\(\s*Capability\.([A-Z_]+)", src))
    required |= set(re.findall(r"Capability\.([A-Z_]+)", src)) - optional

    for callee in set(re.findall(r"self\.([a-z_][a-z0-9_]*)\s*\(", src)):
        sub_required, sub_optional = method_capabilities(cls, callee, seen | {name})
        required |= sub_required
        optional |= sub_optional

    known = set(Capability.__members__)
    optional &= known
    return (required & known) - optional, optional


def field_types(cls) -> dict[str, str]:
    """Attribute -> declared type, for classes that carry data.

    `Window` and `Screen` use __slots__, so their attributes are slot
    descriptors with no signature and no docstring; the types exist only
    on __init__. Dataclasses keep theirs in __annotations__.
    """
    types: dict[str, str] = {}
    for name, ann in getattr(cls, "__annotations__", {}).items():
        types[name] = (
            ann if isinstance(ann, str) else getattr(ann, "__forward_arg__", str(ann))
        )
    try:
        sig = inspect.signature(cls.__init__)
    except (TypeError, ValueError):
        return types
    for name, param in sig.parameters.items():
        if name == "self" or param.annotation is inspect.Parameter.empty:
            continue
        ann = param.annotation
        types[name] = (
            ann if isinstance(ann, str) else getattr(ann, "__name__", str(ann))
        )
    return types


def member_row(cls, name: str, types: dict[str, str]) -> str:
    """One row of a type's member table."""
    static = inspect.getattr_static(cls, name, None)
    doc = first_line(static)
    if isinstance(static, property) or inspect.isfunction(static):
        return f"| `{cell(signature(cls, name))}` | {cell(doc)} |"
    declared = types.get(name)
    label = f"{name}: {declared}" if declared else name
    return f"| `{cell(label)}` | {cell(doc)} |"


def session_row(name: str, x11_only: set[str]) -> str:
    """One Session method as a table row."""
    required, optional = method_capabilities(pyguitest.Session, name)
    needs = ", ".join(f"`{c}`" for c in sorted(required))
    if optional:
        extra = ", ".join(f"`{c}`" for c in sorted(optional))
        needs += (" — " if needs else "") + f"uses {extra} if present"
    mark = "X11" if any(c in x11_only for c in required) else ""
    doc = first_line(inspect.getattr_static(pyguitest.Session, name))
    sig = cell(signature(pyguitest.Session, name))
    return f"| `{sig}` | {needs} | {mark} | {cell(doc)} |"


HEADER = """# API reference

Every public name in `pyguitest`, with the capability each one needs.
Generated by `scripts/gen-api-docs.py` — edit the docstrings, not this page.

**Reading the tables.** *Needs* is the `Capability` a call requires; ask
`gui.supports(...)` before depending on it, or let the call raise
`CapabilityUnsupported`. A blank means the call works on any session, and
"uses X if present" means the call adapts rather than fails when X is
missing. **X11** marks a call needing a capability no Wayland compositor
can serve: it works on an X11 or XWayland session and raises everywhere
else. See [wayland-audit.md](wayland-audit.md) for why.

## Getting a session

```python
import pyguitest

gui = pyguitest.connect()
```
"""

TABLE_HEAD = [
    "| Call | Needs | | What it does |",
    "|------|-------|---|--------------|",
]


def render_session(out: list[str], x11_only: set[str]) -> None:
    """Every Session method, grouped."""
    members = {
        n for n, _ in inspect.getmembers(pyguitest.Session) if not n.startswith("_")
    }
    for title, blurb, names in GROUPS:
        present = [n for n in names if n in members]
        if not present:
            continue
        out += [f"## {title}", ""]
        if blurb:
            out += [blurb, ""]
        out += TABLE_HEAD
        out += [session_row(n, x11_only) for n in present]
        out.append("")
    leftover = sorted(members - {n for _, _, names in GROUPS for n in names})
    if leftover:
        out += ["## Other", ""] + TABLE_HEAD
        out += [session_row(n, x11_only) for n in leftover]
        out.append("")


def forwarded_definition(attr: str):
    """Where a backend-forwarded operation is actually defined.

    Prefer GUIBackend's stub, which documents the operation for every
    backend. The tier-6 ones have no stub -- they exist only on the
    backend that can serve them -- so fall back to searching the
    backends for whichever class defines it.
    """
    stub = inspect.getattr_static(pyguitest.GUIBackend, attr, None)
    if stub is not None:
        return pyguitest.GUIBackend, stub
    for info in pkgutil.iter_modules(backends_pkg.__path__):
        try:
            module = importlib.import_module(f"pyguitest.backends.{info.name}")
        except Exception:
            continue
        for name, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != module.__name__ or not name.endswith("Backend"):
                continue
            if name in UNINFORMATIVE:
                # CompositeBackend gets a generated forwarder for every
                # _DISPATCH entry, whose signature is (*args, **kwargs) and
                # whose docstring says only that it delegates. The real
                # definition is on the backend that serves the capability.
                continue
            found = cls.__dict__.get(attr)
            if found is not None:
                return cls, found
    return None, None


def render_forwarded(out: list[str], x11_only: set[str]) -> None:
    """Operations reachable on a Session only through __getattr__.

    Session writes out the interface it means to offer and forwards the
    rest to the backend, so these are callable as `gui.<name>(...)` while
    being invisible to an editor and to inspect.getmembers. They are also
    where every X11-only operation lives, which makes leaving them out of
    the reference the one omission a reader would most notice.
    """
    members = {
        n for n, _ in inspect.getmembers(pyguitest.Session) if not n.startswith("_")
    }
    missing = [attr for attr in _DISPATCH if attr not in members]
    if not missing:
        return
    out += [
        "## Forwarded to the backend",
        "",
        "`Session` writes out the interface above and forwards anything else",
        "to the backend, so these are callable as `gui.<name>(...)` but will",
        "not appear in an editor's completions. Every operation Wayland",
        "prevents by design lives here — on a Wayland session they raise",
        "`CapabilityUnsupported`, and `gui.supports(...)` answers in advance.",
        "",
        "| Call | Needs | | What it does |",
        "|------|-------|---|--------------|",
    ]
    for attr in missing:
        cap = _DISPATCH[attr]
        owner, obj = forwarded_definition(attr)
        doc = first_line(obj) if obj is not None else ""
        sig = cell(signature(owner, attr)) if owner is not None else f"{attr}(...)"
        mark = "X11" if cap.name in x11_only else ""
        out.append(f"| `{sig}` | `{cap.name}` | {mark} | {cell(doc)} |")
    out.append("")


def render_types(out: list[str]) -> None:
    """The objects Session hands back."""
    out += ["## Types", ""]
    for name in TYPES:
        cls = getattr(pyguitest, name, None)
        if cls is None:
            continue
        out += [f"### {name}", "", first_line(cls), ""]
        members = [n for n, _ in inspect.getmembers(cls) if not n.startswith("_")]
        if not members:
            continue
        types = field_types(cls)
        out += ["| Member | What it does |", "|--------|--------------|"]
        out += [member_row(cls, m, types) for m in members]
        out.append("")


def render_capabilities(out: list[str], prov, x11_only: set[str]) -> None:
    """The capability set, its tiers, and who provides each one."""
    out += [
        "## Capabilities",
        "",
        "The full set, with the tier each sits in and the backends providing",
        "it. Tiers come from the audit and are ordered by cost: tier 6 is what",
        "Wayland prevents by design, which is why those rows are X11 only.",
        "",
        "| Capability | Tier | Provided by | What it covers |",
        "|------------|------|-------------|----------------|",
    ]
    for cap in Capability:
        who = sorted(prov.get(cap.name, set()) - UNINFORMATIVE)
        shown = ", ".join(who) if who else "*session itself*"
        mark = " **(X11 only)**" if cap.name in x11_only else ""
        out.append(
            f"| `{cap.name}` | T{cap.tier.value} | {shown}{mark} "
            f"| {cell(cap.description)} |"
        )
    out += [
        "",
        "### Tiers",
        "",
        "| Tier | Name | Meaning |",
        "|------|------|---------|",
    ]
    for tier in Tier:
        out.append(f"| T{tier.value} | {tier.name} | {cell(first_line(tier))} |")
    out.append("")


def render_names(out: list[str]) -> None:
    """Roles, exceptions, and everything else `pyguitest` exports."""
    out += [
        "## Roles",
        "",
        "`Role` holds the AT-SPI role names `element(role=...)` matches on.",
        "",
        "```",
    ]
    line = ""
    for role in (n for n in dir(pyguitest.Role) if not n.startswith("_")):
        if len(line) + len(role) > 70:
            out.append(line.rstrip())
            line = ""
        line += role + "  "
    if line:
        out.append(line.rstrip())
    out += ["```", ""]

    out += [
        "## Exceptions",
        "",
        "| Exception | Raised when |",
        "|-----------|-------------|",
    ]
    for name in pyguitest.__all__:
        obj = getattr(pyguitest, name, None)
        if isinstance(obj, type) and issubclass(obj, BaseException):
            out.append(f"| `{name}` | {cell(first_line(obj))} |")
    out += [
        "",
        "## Module-level names",
        "",
        "| Name | What it is |",
        "|------|------------|",
    ]
    for name in pyguitest.__all__:
        obj = getattr(pyguitest, name, None)
        if obj is not None:
            out.append(f"| `{name}` | {cell(first_line(obj))} |")
    out.append("")


def render() -> str:
    """The whole of docs/api.md as text.

    Separate from main() so tests/test_api_docs.py can compare it against
    the committed file without writing anything.
    """
    prov = providers()
    x11_only = {
        cap for cap, who in prov.items() if who - UNINFORMATIVE == {"X11Backend"}
    }

    out = HEADER.split("\n")
    out += [first_line(pyguitest.connect), ""]
    out += [f"`connect{cell(function_signature(pyguitest.connect))}`", ""]
    render_session(out, x11_only)
    render_forwarded(out, x11_only)
    render_types(out)
    render_capabilities(out, prov, x11_only)
    render_names(out)
    return "\n".join(out).rstrip() + "\n"


def main() -> None:
    """Write docs/api.md."""
    OUT.write_text(render())
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
