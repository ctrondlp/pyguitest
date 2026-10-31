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
from pyguitest.capabilities import TIERS, Capability, Tier  # noqa: E402

OUT = ROOT / "docs" / "api.md"

# Backend calls _DISPATCH does not route. Capture is the only one:
# CompositeBackend picks a member for it by hand, because a capture that
# fails must fall back to the next member rather than raise, so it never
# went in the dispatch table.
EXTRA_DISPATCH = {"capture": Capability.SCREEN_CAPTURE}

# Helpers a Session method hands the work to, which the analysis below cannot
# see through: it follows `self.<name>(...)` calls and backend attributes, and
# a method whose whole body constructs a helper does neither.
#
# `Session.send_keys` is exactly that -- `KeySender(self).send(keys)` -- so it
# derived *no* capabilities at all, and neither did `press_tab`, which is
# sugar over it, nor the `assert_tab_order` that presses Tab through both.
# Three rows on a page headed "every public name in pyguitest" therefore said
# a call that injects keystrokes needs nothing. Matched on the constructor
# rather than on `.send(`, which is a name too common to key a table on.
HELPER_DISPATCH = {"KeySender": Capability.KEY_EVENT}

# Backends that say yes to everything or nothing carry no information.
UNINFORMATIVE = {"CompositeBackend", "NullBackend", "GUIBackend"}

TYPES = ("Element", "Window", "Screen", "ImageMatch", "Application", "Environment")

# Sections of the Session API, in the order a reader meets them. Anything
# unlisted lands in "Other", so a newly added method shows up rather than
# being silently dropped.
GROUPS: list[tuple[str, str, list[str]]] = [
    (
        "Session control",
        "A session is the handle on one desktop. `supports` and `require` are how a "
        "script asks what this desktop allows before depending on it, rather than "
        "discovering it from a traceback halfway through a run.",
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
        "Coordinates are absolute screen pixels, and `screen=` selects the monitor. "
        "Prefer driving a named element over clicking a point where you can: a "
        "coordinate goes stale the moment a window moves. `glide` and "
        "`move_mouse_naturally` exist for applications that only react to real "
        "movement, such as hover menus.",
        [
            "move_mouse",
            "glide",
            "move_mouse_naturally",
            "click",
            "press_button",
            "release_button",
            "drag",
            "scroll",
        ],
    ),
    (
        "Keyboard",
        "`type_text` is for literal text and `send_keys` for chords and named keys. "
        "Both go to whatever holds focus, so focus a window or element first -- see "
        "`focus_window` -- or the keystrokes land somewhere else. Typed text depends "
        "on the session keymap; [input.md](input.md) covers when that matters.",
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
        "A `Window` is a handle, not a snapshot: keep it and re-read through it. "
        "Finding one by title is the usual way in, and a window that the list already "
        "carries may still not be placed on screen -- check `is_window_viewable` "
        "before acting on a position.",
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
        "Capture is the one area where desktops differ most in what they permit; "
        "`screenshot` reports rather than guesses when a session will not serve it. "
        "`capture_on_failure` is the one to reach for in a test suite, and `locate` "
        "finds a known image on screen when nothing publishes an accessible element.",
        ["screens", "screenshot", "capture_on_failure", "locate", "locate_image"],
    ),
    (
        "Clipboard",
        "Text only, and the selection a desktop calls the clipboard rather than the "
        "X11 primary selection.",
        ["get_clipboard", "set_clipboard"],
    ),
    (
        "Processes",
        "`start_app` launches and hands back an `Application` to wait on and tear "
        "down; `run_app` is the fire-and-forget form. Launching the application under "
        "test from the script is what makes a run repeatable.",
        ["start_app", "run_app"],
    ),
    (
        "Waiting",
        "Every `wait_*` call polls on an interval unless the backend can do "
        "better: a window wait is answered by real notification — "
        "`SetWinEventHook` on Windows, a compositor event stream on GNOME "
        "Shell, KWin, sway or niri — but only when it matches a `title`. On "
        "macOS, which has no event feed here yet, on every X11 session, and "
        "on any wait that names `app_id`, that same call polls every "
        "`interval` seconds instead. `timeout` bounds the wait in seconds, "
        "and `timeout` left `None` — which every `wait_*` here defaults to, "
        "`sync` excepted — waits indefinitely; there is no session-level "
        "default to fall back on.",
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
    """First paragraph of a docstring, collapsed to one line.

    Empty when the object has no docstring *of its own*. A plain value
    borrows its type's, and `getattr_static` on a dataclass field hands
    back its default value -- so `has_dogtail: bool = False` documented
    itself with `bool`'s docstring. Useless on any one version, and worse
    across them: that text is "bool(x) -> bool" up to Python 3.11 and
    "Returns True when the argument is true..." from 3.12, so a committed
    `docs/api.md` could not match on every supported version at once, and
    CI failed on 3.10 against a file generated on a newer interpreter.

    Compared by equality, not identity: a C type builds `__doc__` fresh on
    each access, so `False.__doc__ is bool.__doc__` is False even though
    the text is exactly the borrowed one. Equality still keeps every
    descriptor that carries its own line -- a NamedTuple's `_tuplegetter`
    ("Alias for field number 0"), `tuple.count`, a property's getter --
    since none of those match their own type's docstring.
    """
    target = obj.fget if isinstance(obj, property) else obj
    own = getattr(target, "__doc__", None)
    if own is None or own == getattr(type(target), "__doc__", None):
        return ""
    doc = inspect.getdoc(target) or ""
    return re.sub(r"\s+", " ", doc.split("\n\n")[0].strip())


def body(obj) -> list[str]:
    """Everything after a docstring's summary line, as markdown paragraphs.

    The reason this page grew past a table. `first_line` publishes the
    summary and nothing else, and ruff's D205 holds that summary to a single
    line -- so every call in the reference was described in a median of eight
    words while the docstring under it carried the part a reader actually
    needs: that `button()` is push buttons *only* and a toolbar item
    publishes a different role, that `scroll()` counts wheel detents rather
    than pixels, that `wait_for_window` can return a window the window
    manager has not placed yet. Measured before writing this: 92 of 92
    documented `Session` members had more docstring than reached the page,
    1170 lines of it, against a 476-line reference.

    Borrowed docstrings are excluded for the same reason `first_line`
    excludes them -- a value with no `__doc__` of its own reports its type's,
    which is stdlib text that is reworded between interpreters, and this file
    is byte-compared on every version CI runs.

    Paragraph wrapping is left exactly as written. Markdown reflows a
    paragraph anyway, and rewrapping here would churn the whole page every
    time a docstring was rewrapped by a formatter.
    """
    target = obj.fget if isinstance(obj, property) else obj
    if borrowed_docstring(target):
        return []
    doc = inspect.getdoc(target) or ""
    paragraphs = [p.strip() for p in doc.split("\n\n")[1:] if p.strip()]
    lines: list[str] = []
    for paragraph in paragraphs:
        lines += [textwrap.dedent(paragraph), ""]
    return lines


def borrowed_docstring(obj) -> bool:
    """Whether `obj`'s docstring is really its type's, not its own.

    `getattr_static` on a dataclass field hands back the default *value*, and
    a value reports its type's `__doc__` -- so `has_dogtail: bool = False`
    documented itself with `bool`'s docstring. Compared by equality rather
    than identity because a C type rebuilds `__doc__` on each access.
    """
    doc = getattr(obj, "__doc__", None)
    return bool(doc) and doc == getattr(type(obj), "__doc__", None)


def attribute_note(source: str, name: str) -> str:
    """First paragraph of the attribute docstring on `name = ...`, or ''.

    PEP 257's form: a string literal directly after the assignment. That is
    what this project writes for a value that cannot document itself -- a dict
    has no `__doc__` of its own to put one in -- and tests/test_style.py pins
    the choice by rejecting the Sphinx `#:` alternative.

    Collapsed the way `first_line` collapses a docstring, so the reference
    gets one line either way.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ""
    for index, node in enumerate(tree.body):
        if not (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
        ):
            continue
        following = tree.body[index + 1] if index + 1 < len(tree.body) else None
        if not (
            isinstance(following, ast.Expr)
            and isinstance(following.value, ast.Constant)
            and isinstance(following.value.value, str)
        ):
            return ""
        return re.sub(r"\s+", " ", following.value.value.split("\n\n")[0].strip())
    return ""


def note_for(name: str, obj) -> str:
    """The attribute docstring of a name whose object cannot carry one.

    `first_line` stays the first choice: a class or a function says what it is
    in its own docstring. A plain value has none to give -- it borrows its
    type's, which `first_line` refuses on purpose, since that text changes
    between interpreters and this file is byte-compared on every version CI
    runs. `TIERS` is what that left: a blank cell for a name in `__all__`, on
    a page headed "every public name in `pyguitest`", with the six meanings it
    holds unreadable anywhere else in the docs.

    The note is read from the source rather than kept in a table here, so the
    sentence sits next to the value it describes and no list in this script can
    drift from the package.

    Several submodules hold the same object -- `__main__` imports `TIERS` from
    `capabilities` too -- so the search takes the first candidate that *has* a
    note, not the first that has the name. Matching on the binding alone made
    the result depend on which modules the interpreter had already imported,
    which the byte-comparison in tests/test_api_docs.py reported as the
    committed file being out of date.
    """
    for module_name in sorted(sys.modules):
        if not module_name.startswith("pyguitest."):
            continue
        module = sys.modules[module_name]
        if module is None or vars(module).get(name) is not obj:
            continue
        path = getattr(module, "__file__", None)
        if not path:
            continue
        try:
            source = Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
        note = attribute_note(source, name)
        if note:
            return note
    return ""


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

    Two shapes are understood, because backends legitimately use both. A property
    that builds its set from `Capability.` members inline is read directly. A
    property that composes named module-level sets -- `macos.py` returns
    `_UNGATED | _AX_ONLY` so that each half can carry its own argument in its own
    docstring -- is followed through those names, which is the difference between
    a table that lists that backend and one that silently omits it. Measured:
    before this, `MacosBackend` appeared nowhere in docs/api.md.
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
            for constant in re.findall(r"\b(_[A-Z][A-Z_]*)\b", src):
                values = getattr(module, constant, None)
                if not isinstance(values, (set, frozenset)):
                    continue
                for value in values:
                    if isinstance(value, Capability):
                        found.setdefault(value.name, set()).add(name)
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
    required |= {
        cap.name
        for helper, cap in HELPER_DISPATCH.items()
        if re.search(rf"\b{helper}\s*\(", src)
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


def inherited_from_builtin(cls, name: str) -> bool:
    """Whether `name` comes from a builtin base rather than from this class.

    `ImageMatch` is a NamedTuple, so `getmembers` finds `tuple.count` and
    `tuple.index` on it -- neither part of this package's API, and both
    documented by a C type. Same for the `_tuplegetter` descriptors behind
    the fields, whose "Alias for field number 0" says nothing the label
    `x: int` does not already say.

    Excluding them is a correctness fix as much as a tidiness one: a C
    type's docstring is free to change between interpreters, and
    `docs/api.md` is committed and byte-compared on every supported
    version. One such string (`bool`'s, reached through a dataclass
    default) already broke CI on 3.10 against a file generated on a newer
    Python; leaving a handful more in place would be waiting for the same
    failure.
    """
    for base in type.mro(cls):
        if name in vars(base):
            return getattr(base, "__module__", "") == "builtins"
    return False


def member_row(cls, name: str, types: dict[str, str]) -> str:
    """One row of a type's member table."""
    static = inspect.getattr_static(cls, name, None)
    if isinstance(static, property) or inspect.isfunction(static):
        return f"| `{cell(signature(cls, name))}` | {cell(first_line(static))} |"
    declared = types.get(name)
    label = f"{name}: {declared}" if declared else name
    # No description for a data member. What reaches here is only ever a
    # field -- `count`/`index` and the rest of the builtin surface are
    # filtered out above -- and a field carries no documentation of its
    # own: a dataclass default borrows its *type's* docstring, and a
    # NamedTuple's `_tuplegetter` says "Alias for field number 0", which
    # tells a reader nothing the label `x: int` has not already told them.
    # Both are also stdlib text that may be reworded between interpreters,
    # and this file is byte-compared on every version CI runs.
    return f"| `{cell(label)}` |  |"


def needs_cell(required, optional, x11_only: set[str]) -> str:
    """The *Needs* cell: the capabilities, and the caveats on reaching them.

    The X11-only mark lives here rather than in a column of its own. It
    applies to nothing today -- every tier-6 capability is served by Windows
    too -- so as a column it was 95 blank cells across fifteen tables, and a
    reader scanning for the call they want paid for it in every one. Inline,
    it costs nothing until it applies and appears next to the capability it
    qualifies when it does. It is also how the Capabilities table has always
    rendered the same fact, so the three tables now agree.
    """
    cell_text = ", ".join(f"`{c}`" for c in sorted(required))
    notes = []
    if optional:
        extra = ", ".join(f"`{c}`" for c in sorted(optional))
        notes.append(f"uses {extra} if present")
    if any(c in x11_only for c in required):
        notes.append("**X11 only**")
    if notes:
        cell_text += (" — " if cell_text else "") + "; ".join(notes)
    return cell_text


def session_row(name: str, x11_only: set[str]) -> str:
    """One Session method as a table row."""
    required, optional = method_capabilities(pyguitest.Session, name)
    needs = needs_cell(required, optional, x11_only)
    doc = first_line(inspect.getattr_static(pyguitest.Session, name))
    sig = cell(signature(pyguitest.Session, name))
    return f"| `{sig}` | {needs} | {cell(doc)} |"


HEADER = """# API reference

Every public name in `pyguitest`, with the capability each one needs.
Generated by `scripts/gen-api-docs.py` — edit the docstrings, not this page.

New to the library? [getting-started.md](getting-started.md) goes from an
empty file to a working script; this page is the lookup you want once you
know roughly what to reach for. [recipes.md](recipes.md) answers the
task-shaped questions — waiting properly, filling a form, screenshots, CI —
and [troubleshooting.md](troubleshooting.md) starts from the symptom when a
call finds nothing, types nothing or captures nothing.

**Reading the tables.** *Needs* is the `Capability` a call requires; ask
`gui.supports(...)` before depending on it, or let the call raise
`CapabilityUnsupported`. A blank means the call works on any session, and
"uses X if present" means the call adapts rather than fails when X is
missing. A few calls can also be marked **X11 only** — a capability nothing
but `X11Backend` provides. Nothing carries that mark today, because Windows
provides every tier-6 capability and macOS serves `pointer_position()`: the
capability model working, not an annotation gone missing. See
[wayland-audit.md](developers/wayland-audit.md) for why.

## Getting a session

Most scripts have the same shape — open a session, find the window, work
through named elements, and assert on what the application did:

```python
import pyguitest

gui = pyguitest.connect()
window = gui.wait_for_window("Preferences", timeout=10)
gui.focus_window(window)

gui.text_field("Name").set_text("Ada")
gui.checkbox("Subscribe").click()
gui.button("Save").click()

gui.expect_text(name="Status", equals="Saved")
gui.close()
```

`connect()` picks the backends for the session it finds; name them yourself
only when you need a specific one. The call raises if nothing can drive this
desktop at all, so there is no "did it work" check to write here.

Two forks a first script meets. `Element.set_text` writes through the
accessibility interface and sends no key events, which is what you want when
the value is the point; `Session.type_text` sends real keystrokes, which is
what you want when the application's own key handling is under test. And
`expect_*` raises on failure for use directly in a test, where `wait_*`
returns a value for you to branch on.
"""

TABLE_HEAD = [
    "| Call | Needs | What it does |",
    "|------|-------|--------------|",
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
        out += session_details(present)
    leftover = sorted(members - {n for _, _, names in GROUPS for n in names})
    if leftover:
        out += ["## Other", ""] + TABLE_HEAD
        out += [session_row(n, x11_only) for n in leftover]
        out.append("")
        out += session_details(leftover)


def session_details(names: list[str]) -> list[str]:
    """The per-call detail that follows a group's index table.

    The table stays the way in: signature, capability and the summary line,
    so "which call do I want" is still one scan. What follows is the rest of
    each docstring, which is where the caveat that saves an afternoon lives.
    The summary is deliberately not repeated here -- the table above has
    already given it, and a reference that says the same sentence twice per
    call reads like filler.
    """
    out: list[str] = []
    for name in names:
        static = inspect.getattr_static(pyguitest.Session, name)
        detail = body(static)
        if not detail:
            continue
        # The name alone, not the signature. The table above carries the
        # full one, and several run past 300 characters -- `elements()` takes
        # seven keyword filters -- which makes an unreadable heading and an
        # unusable anchor. A bare name also gives every call a stable
        # `#name` link that survives a signature change.
        out += [f"### `{name}`", ""]
        out += detail
    return out


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
        "| Call | Needs | What it does |",
        "|------|-------|--------------|",
    ]
    for attr in missing:
        cap = _DISPATCH[attr]
        owner, obj = forwarded_definition(attr)
        doc = first_line(obj) if obj is not None else ""
        sig = cell(signature(owner, attr)) if owner is not None else f"{attr}(...)"
        needs = needs_cell([cap.name], (), x11_only)
        out.append(f"| `{sig}` | {needs} | {cell(doc)} |")
    out.append("")


def render_types(out: list[str]) -> None:
    """The objects Session hands back."""
    out += ["## Types", ""]
    for name in TYPES:
        cls = getattr(pyguitest, name, None)
        if cls is None:
            continue
        out += [f"### {name}", "", first_line(cls), ""]
        members = [
            n
            for n, _ in inspect.getmembers(cls)
            if not n.startswith("_") and not inherited_from_builtin(cls, n)
        ]
        if not members:
            continue
        types = field_types(cls)
        out += ["| Member | What it does |", "|--------|--------------|"]
        out += [member_row(cls, m, types) for m in members]
        out.append("")
        # `Element` is the type most end-user code actually touches --
        # `gui.button("Save").click()` is an Element call -- and its members
        # carried the same eight-word summaries the Session tables did, while
        # the docstrings held the per-platform behaviour that decides whether
        # a script works: what `value` answers for a check box on each
        # backend, what `click(via=...)` falls back to, which toolkits
        # publish `expanded` at all.
        for member in members:
            static = inspect.getattr_static(cls, member, None)
            if static is None:
                continue
            detail = body(static)
            if not detail:
                continue
            out += [f"#### `{member}`", ""]
            out += detail


def render_capabilities(out: list[str], prov, x11_only: set[str]) -> None:
    """The capability set, its tiers, and who provides each one."""
    out += [
        "## Capabilities",
        "",
        "The full set, with the tier each sits in and the backends providing",
        "it. Tiers come from the audit and are ordered by cost: tier 6 is what",
        "Wayland prevents by design, which is why a Wayland session cannot ",
        "serve them. On X11, XWayland, Windows or macOS they are ordinary ",
        "calls, and the X11-only mark appears only where no other backend ",
        "provides the capability at all -- which today is nowhere.",
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
    # TIERS, not the member's docstring: an enum member inherits its
    # class's, so every row read "What it costs to implement a capability
    # on Wayland" -- the same sentence six times, where TIERS carries what
    # each tier actually means.
    for tier in Tier:
        out.append(f"| T{tier.value} | {tier.name} | {cell(TIERS[tier])} |")
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
            described = first_line(obj) or note_for(name, obj)
            out.append(f"| `{name}` | {cell(described)} |")
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
    # `newline="\n"` deliberately. The checked-in file is LF, and `write_text`
    # otherwise translates to the platform's line ending: on Windows every run
    # rewrote all of it as CRLF, so regenerating after a docstring change showed
    # a whole-file diff, and the real change had to be found inside it. This is
    # a formatter, and a formatter that churns the file it formats is not one.
    # Nothing about the text changes -- `test_api_docs` reads it back with
    # universal newlines either way -- only what the diff after a run shows.
    OUT.write_text(render(), encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
