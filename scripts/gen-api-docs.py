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
        "A `Window` stays usable as a handle, but its `title`, `app_id` and `pid` "
        "are read when it is created and do not update themselves -- call "
        "`refresh_window` for current values rather than comparing against a title "
        "the application has since changed. Finding one by title is the usual way "
        "in, and a window the list already carries may still not be placed on "
        "screen: check `is_window_viewable` before acting on a position.",
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
        "down, and is what a GUI test wants. `run_app` **blocks until the program "
        "exits** -- it is `subprocess.run` underneath, so pointing it at an "
        "application hangs the script until someone closes the window. Launching "
        "the application under test from the script is what makes a run "
        "repeatable.",
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
    # `rstrip`, never `strip`: `getdoc` has already dedented the docstring
    # uniformly, so a paragraph that is *still* indented is an indented code
    # example and markdown renders it as a code block. Stripping removed the
    # leading whitespace of the paragraph's first line only, which left
    # `with gui.capture_on_failure("artifacts"):` flush against the prose
    # while the lines under it stayed indented -- the opening line rendered
    # as a sentence and the body as a code block. `dedent` could not undo it
    # either: with the first line already at column 0 the common prefix is
    # empty, so it had nothing to remove.
    paragraphs = [p.rstrip() for p in doc.split("\n\n")[1:] if p.strip()]
    lines: list[str] = []
    # Consecutive code paragraphs become one fence, not one each. A docstring
    # that shows three variations of a call separates them with blank lines,
    # which are paragraph breaks here -- fencing each separately turned one
    # worked example into three stacked blocks with nothing between them.
    # Merging keeps the blank lines *inside* the fence, which is how the
    # example was written and how code is normally read.
    # Only blocks carrying the same fence tag merge: a Python example and a
    # non-Python legend are both fenced but must not end up in one block.
    run: list[str] = []
    run_tag = ""
    for paragraph in paragraphs:
        fenced = code_fence(paragraph)
        if fenced:
            tag = fenced[0]
            if run and tag == run_tag:
                run.append("")
                run += fenced[1:-2]
                continue
            if run:
                lines += [run_tag, *run, "```", ""]
            run = list(fenced[1:-2])
            run_tag = tag
            continue
        if run:
            lines += [run_tag, *run, "```", ""]
            run, run_tag = [], ""
        lines += [paragraph, ""]
    if run:
        lines += [run_tag, *run, "```", ""]
    return lines


def code_fence(paragraph: str) -> list[str]:
    """A wholly-indented paragraph as a fenced Python block, else [].

    An indented paragraph already rendered as a markdown code block, but as
    an indented one: no language, so no highlighting, and it is invisible to
    anything that looks for fences. Promoting it to a real fence costs
    nothing and upgrades every example already written in a docstring,
    rather than only the ones added after this.

    A bullet list is indented too and must not be caught, so a block
    qualifies only if no line opens with a list marker.

    The `python` tag is only applied to a block that actually parses as
    Python. Several indented blocks in these docstrings are not code at all
    but legends and measured results -- `send_keys` sets out its `{}` grammar
    as one, `^ % + # &   modifiers: Ctrl, Alt, ...` -- and tagging those
    `python` gave them nonsense highlighting and offered them to the
    formatter as source. They still get a fence, so the spacing that makes
    them readable is preserved; they just do not claim to be code.
    """
    raw = paragraph.split("\n")
    if not all(line.startswith("    ") or not line.strip() for line in raw):
        return []
    stripped = [line[4:] if line.startswith("    ") else line for line in raw]
    if any(re.match(r"\s*([*\-+]|\d+\.)\s", line) for line in stripped):
        return []
    # Prose that merely happens to be indented is not an example. Require
    # something that reads as code: an assignment, a call, or a keyword.
    joined = "\n".join(stripped)
    if not re.search(
        r"[=(]|^\s*(import|from|with|for|if|def|assert|return)\b", joined, re.M
    ):
        return []
    try:
        compile(joined, "<docstring>", "exec")
    except SyntaxError:
        return ["```", *stripped, "```", ""]
    return ["```python", *stripped, "```", ""]


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

    The package root is searched too, and not only its submodules. `TIERS`
    hid this: it is defined in `capabilities` and merely imported into
    `__init__`, so a filter of `startswith("pyguitest.")` found it anyway --
    while a constant *defined* in `__init__.py` has no module whose name
    starts with that prefix at all, and came out blank. `NAMED_ROLES` and
    `SESSION_CAPABILITIES` are both that case. Root first, which costs
    nothing: a name only imported there carries no note, and the
    first-with-a-note rule moves on.
    """
    for module_name in sorted(sys.modules):
        if module_name != "pyguitest" and not module_name.startswith("pyguitest."):
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
    """`name(params)` for a method, or bare `name` for a property.

    The bare `*` separator is rendered too. `str(Parameter)` cannot carry it
    -- it is a property of the signature rather than of any one parameter --
    and dropping it printed every keyword-only argument as though it were
    positional: the documented `find_window(title, app_id, pid)` is three
    positional arguments where the real call takes one, and a reader copying
    it got a TypeError. Rebuilt here rather than taken from `str(sig)`,
    which would bring `self` back with it.
    """
    static = inspect.getattr_static(cls, name, None)
    if isinstance(static, property):
        return name
    try:
        sig = inspect.signature(getattr(cls, name))
    except (TypeError, ValueError):
        return f"{name}(...)"
    params: list[str] = []
    starred = False
    for n, p in sig.parameters.items():
        if n == "self":
            continue
        if p.kind is p.VAR_POSITIONAL:
            # `*args` is itself the separator; a bare `*` beside it is a
            # syntax error.
            starred = True
        elif p.kind is p.KEYWORD_ONLY and not starred:
            params.append("*")
            starred = True
        params.append(str(p))
    rendered = re.sub(r"'([^']*)'", r"\1", ", ".join(params))
    return f"{name}({rendered})"


SIG_CELL_LIMIT = 110


def short_signature(cls, name: str) -> str:
    """`signature`, trimmed to the required parameters when it is unreadable.

    `move_mouse_naturally` takes fourteen parameters and renders as 368
    characters, which in a table cell is a wall rather than an answer: two of
    those parameters are required and the other twelve have defaults, and
    nothing about the rendering said so. The index table exists to answer
    "which call do I want", so over the limit it now shows what a caller must
    pass and defers the rest to that call's own Parameters table below, which
    carries every name, type and default in a form that can be read.
    """
    full = signature(cls, name)
    if len(full) <= SIG_CELL_LIMIT:
        return full
    static = inspect.getattr_static(cls, name, None)
    try:
        sig = inspect.signature(getattr(cls, name))
    except (TypeError, ValueError):
        return full
    required = [
        n
        for n, p in sig.parameters.items()
        if n != "self"
        and p.default is inspect.Parameter.empty
        and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
    ]
    del static
    shown = ", ".join(required)
    tail = ", ..." if shown else "..."
    return f"{name}({shown}{tail})"


def signature_fence(sig_text: str) -> list[str]:
    """The full signature as a code block, wrapped if it is long.

    One parameter per line past the limit. A fence does not soft-wrap, so the
    fourteen-parameter calls would otherwise be a single line the reader has
    to scroll sideways through -- the same problem the table cell had, moved.

    The parameter list is found by matching the opening bracket rather than
    by slicing to the last character: a module-level function's signature
    carries its return annotation after the closing bracket, and assuming
    the text ended with `)` truncated `-> Session` to `-> Sessio` and left a
    stray comma behind it.
    """
    if len(sig_text) <= SIG_CELL_LIMIT:
        return ["```python", sig_text, "```", ""]
    open_at = sig_text.find("(")
    if open_at == -1:
        return ["```python", sig_text, "```", ""]
    depth = 0
    close_at = -1
    for i, ch in enumerate(sig_text[open_at:], start=open_at):
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
            if depth == 0:
                close_at = i
                break
    if close_at == -1:
        return ["```python", sig_text, "```", ""]
    head = sig_text[:open_at]
    inner = sig_text[open_at + 1 : close_at]
    suffix = sig_text[close_at + 1 :]
    # Split on commas that separate parameters, not commas inside a type such
    # as `Sequence[tuple[int, int]]` or `dict[str, int]`.
    parts: list[str] = []
    depth = 0
    current = ""
    for ch in inner:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(current.strip())
            current = ""
            continue
        current += ch
    if current.strip():
        parts.append(current.strip())
    body_lines = [f"    {p}," for p in parts]
    return ["```python", f"{head}(", *body_lines, f"){suffix}", "```", ""]


def parameter_rows(fn) -> list[tuple[str, str, str]]:
    """(name, type, default) for one callable, or [] if it takes nothing.

    Generated from the signature rather than parsed out of prose, so it
    cannot fall out of step with the code the way a hand-written parameter
    list does.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return []
    rows: list[tuple[str, str, str]] = []
    for name, p in sig.parameters.items():
        if name == "self":
            continue
        if p.kind is p.VAR_POSITIONAL:
            rows.append((f"*{name}", "", "*any number*"))
            continue
        if p.kind is p.VAR_KEYWORD:
            rows.append((f"**{name}", "", "*any keyword*"))
            continue
        if p.annotation is inspect.Parameter.empty:
            declared = ""
        else:
            declared = re.sub(
                r"'([^']*)'", r"\1", inspect.formatannotation(p.annotation)
            )
        if p.default is inspect.Parameter.empty:
            default = "**required**"
        else:
            default = f"`{p.default!r}`" if p.default != () else "`()`"
        rows.append((name, declared, default))
    return rows


def parameter_table(fn) -> list[str]:
    """The Parameters block for a call, or [] when it takes no arguments."""
    rows = parameter_rows(fn)
    if not rows:
        return []
    out = [
        "**Parameters**",
        "",
        "| Parameter | Type | Default |",
        "|-----------|------|---------|",
    ]
    for name, declared, default in rows:
        shown_type = f"`{cell(declared)}`" if declared else ""
        out.append(f"| `{name}` | {shown_type} | {default} |")
    out.append("")
    return out


def return_type_text(fn) -> str:
    """`fn`'s return type, formatted like a parameter's, or "" for nothing worth naming.

    That is: no annotation at all, one that is `None`, or one naming a
    private helper type (`_CaptureOnFailure` for `capture_on_failure()` is
    the one case today) that a reader could never import and verify --
    showing it would read as a typo or a leak of an internal, not as
    documentation. Such a method's docstring is where its return value is
    actually explained, same as before this function existed.

    Generated from the signature for the same reason the Parameters table
    is: every public return type here is annotated (checked live across
    Session, Element, Application and the module-level functions), so this
    cannot drift from `parameter_table`'s own source the way a hand-written
    "Returns" paragraph could.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return ""
    ra = sig.return_annotation
    if ra is inspect.Signature.empty:
        return ""
    text = re.sub(r"'([^']*)'", r"\1", inspect.formatannotation(ra))
    if text == "None" or re.search(r"\b_[A-Za-z]", text):
        return ""
    return text


def returns_table(fn) -> list[str]:
    """The Returns line for a call, or [] for one with nothing to return."""
    text = return_type_text(fn)
    if not text:
        return []
    # Not cell(): that escapes `|` for a markdown *table* cell, and a union
    # return type's `|` is not inside one here -- it would show up as a
    # literal backslash, since backticks render their content verbatim.
    return [f"**Returns** `{text}`", ""]


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


def cap_link(name: str) -> str:
    """A capability name, linked to the row that explains it.

    The names are opaque on first read -- `ELEMENT_TREE` tells a reader
    nothing about what it is or why a desktop might not have it -- and the
    table that defines all of them is 2000 lines below the first call that
    needs one. Linking costs nothing and puts the definition one click from
    every one of the ~95 places a capability is named.
    """
    return f"[`{name}`](#cap-{name.lower()})"


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
    cell_text = ", ".join(cap_link(c) for c in sorted(required))
    notes = []
    if optional:
        extra = ", ".join(cap_link(c) for c in sorted(optional))
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
    sig = cell(short_signature(pyguitest.Session, name))
    return f"| [`{sig}`](#{name}) | {needs} | {cell(doc)} |"


HEADER = """# API reference

Every public name in `pyguitest`, with the capability each one needs.
Generated by `scripts/gen-api-docs.py` — edit the docstrings, not this page.

New to the library? [getting-started.md](getting-started.md) goes from an
empty file to a working script; this page is the lookup you want once you
know roughly what to reach for. [recipes.md](recipes.md) answers the
task-shaped questions — waiting properly, filling a form, screenshots, CI —
and [troubleshooting.md](troubleshooting.md) starts from the symptom when a
call finds nothing, types nothing or captures nothing.

**Reading the tables.** Each group starts with an index — signature, what the
call needs, and what it does in one line — and every signature links down to
that call's own entry, which carries the full signature, a table of every
parameter with its type and default, and the rest of the docstring. A
parameter whose Default reads **required** is one you must pass; anything
else shows the value used when you leave it out.

*Needs* is the **capability** a call requires. A capability is one thing a
desktop either lets a program do or does not — read the accessible tree,
move the pointer, capture the screen, list windows — and which ones you get
is decided by the session you are actually running on, not by the install.
That is why it is per-call rather than a footnote: `ELEMENT_TREE`, for
instance, is the one that lets `gui.button("Save")` find a control by name,
and a desktop with no accessibility bridge running does not have it, so
every lookup fails there while typing and clicking still work. Each name
links to [Capabilities](#capabilities), which says what it covers, which
backends provide it, and how hard it is to come by.

Ask `gui.supports(...)` before depending on one, or let the call raise
`CapabilityUnsupported`. A blank *Needs* means the call works on any
session, and "uses X if present" means the call adapts rather than fails
when X is missing. A few calls can also be marked **X11 only** — a
capability nothing but `X11Backend` provides. Nothing carries that mark
today, because Windows provides every tier-6 capability and macOS serves
`pointer_position()`: the capability model working, not an annotation gone
missing. See [wayland-audit.md](developers/wayland-audit.md) for why.

## Getting a session

Most scripts have the same shape — open a session, find the window, work
through named elements, and assert on what the application did:

```python
import pyguitest

gui = pyguitest.connect()
gui.require(pyguitest.Capability.ELEMENT_TREE, pyguitest.Capability.POINTER_BUTTON)

window = gui.wait_for_window("Preferences", timeout=10)
if not gui.focus_window(window):
    raise AssertionError("the window manager would not focus Preferences")

gui.text_field("Name").set_text("Ada")
gui.checkbox("Subscribe").click()
gui.button("Save").click()

gui.expect_text(name="Status", equals="Saved")
gui.close()
```

`connect()` picks the backends for the session it finds; name them yourself
only when you need a specific one. It does **not** raise when nothing can
drive this desktop -- automatic selection falls back to a `NullBackend` and
you still get a session, which then refuses each call individually. That is
why the second line is there: `require` is the check that fails early and
names what is missing, rather than letting a script run on into a refusal.
`focus_window` is checked for the same reason -- it returns False rather
than raising when a window manager declines, and typing into an unfocused
window sends the keystrokes somewhere else.

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
        if isinstance(static, property):
            params: list[str] = []
            returns = returns_table(static.fget) if static.fget else []
        else:
            fn = getattr(pyguitest.Session, name)
            params = parameter_table(fn)
            returns = returns_table(fn)
        # A call with neither prose nor parameters nor a return type has
        # nothing to show here; one with only one of those still does,
        # which is why this is not the old `if not detail: continue`.
        if not detail and not params and not returns:
            continue
        # The name alone, not the signature. The table above carries the
        # full one, and several run past 300 characters -- `elements()` takes
        # seven keyword filters -- which makes an unreadable heading and an
        # unusable anchor. A bare name also gives every call a stable
        # `#name` link that survives a signature change.
        out += [f"### `{name}`", ""]
        # The full signature, in a fence rather than a cell: this is where a
        # reader who followed the index link lands, and the long ones are
        # only readable with room to wrap.
        if not isinstance(static, property):
            out += signature_fence(signature(pyguitest.Session, name))
        out += params
        out += returns
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
            if inspect.isfunction(static):
                params = parameter_table(getattr(cls, member))
                returns = returns_table(getattr(cls, member))
            elif isinstance(static, property) and static.fget is not None:
                params = []
                returns = returns_table(static.fget)
            else:
                params = []
                returns = []
            if not detail and not params and not returns:
                continue
            out += [f"#### `{member}`", ""]
            out += params
            out += returns
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
        # An inline anchor, so every `Needs` cell above can link straight to
        # the row that defines the capability it names. GitHub renders inline
        # HTML in a table cell; a heading per capability would give 32 more
        # entries in the page outline for no gain.
        anchor = f'<a id="cap-{cap.name.lower()}"></a>'
        out.append(
            f"| {anchor}`{cap.name}` | T{cap.tier.value} | {shown}{mark} "
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
            linked = f"[`{name}`](#{name.lower()})" if _has_detail(obj) else f"`{name}`"
            out.append(f"| {linked} | {cell(described)} |")
    out.append("")
    # The module-level *functions* in full. `connect` is the first call in
    # every script and the table above gave it one line and no signature, so
    # the parameters that decide how a whole run behaves -- which backends,
    # the click strategy, the inter-event and inter-key delays -- were
    # documented nowhere on this page. The classes are covered under Types.
    for name in pyguitest.__all__:
        obj = getattr(pyguitest, name, None)
        if not _has_detail(obj):
            continue
        out += [f"### `{name}`", ""]
        out += signature_fence(f"{name}{function_signature(obj)}")
        out += parameter_table(obj)
        out += returns_table(obj)
        out += body(obj)


def _has_detail(obj) -> bool:
    """Whether a module-level export earns a section of its own.

    Functions do: they take arguments. Classes are already rendered under
    Types, and an enum or a constant has nothing a signature would describe.
    """
    return inspect.isfunction(obj)


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
