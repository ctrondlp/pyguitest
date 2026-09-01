#!/usr/bin/env python3
"""GUI Spy: point at a screen coordinate and see what pyguitest would call it.

An element inspector: given a point on screen, report the window and
accessible element there, so a script can be written against `role=` and
`name=` instead of guessed at.

Four ways to give it a point. Two NEED AN X CONNECTION: reading where the
pointer is, even once, is `Capability.POINTER_QUERY` -- deliberately absent
on every Wayland compositor, since a process that could poll it freely
would be handing out exactly what a keylogger reads. XWayland carries a
real X connection, so a Wayland session running it (`DISPLAY` set alongside
`WAYLAND_DISPLAY`) has these too, with python-xlib installed -- but only
for what XWayland itself tracks. That limit is now measured rather than
suspected: over an X surface the read is right, and over a native Wayland
window X keeps returning the last position it knew, silently and with no
error (docs/validation.md). So `--here` under XWayland is trustworthy
pointing at an X11 client and quietly wrong pointing anywhere else -- which
is the worse failure, since a stale coordinate still names *some* element.
`--watch` has the same shape: it sees the button only while an X client
holds focus. On a real X11 session there is no such doubt.

    python3 examples/09_gui_spy.py 842 612            # works on any desktop
    python3 examples/09_gui_spy.py --find button.png  # works on any desktop
    python3 examples/09_gui_spy.py --here             # X11/XWayland, one read
    python3 examples/09_gui_spy.py --watch            # X11/XWayland, per click

On a Wayland session with no XWayland, and on GNOME, KDE, sway, Hyprland or
niri without python-xlib: use the first or second form. Read a coordinate
off a screenshot (example 05) or your desktop's own pointer-position
display, or point `--find` at a cropped picture of the control the way
example 08 does -- `--here`/`--watch` will tell you plainly, and
immediately, that the capability is missing rather than doing something
degraded.

`--watch` additionally needs `Capability.INPUT_STATE_QUERY` (same X
connection, same reason) to notice a click at all, since it does not
capture input itself -- it polls whether the left button is currently held,
the same readback `--here` uses once, just repeated. Every point it reports
is one you visibly clicked while this was running in your own foreground
terminal; nothing here runs unattended, hooks input, or writes anything
down.

Any of the four forms also takes:

    --tree   list every accessible element containing the point, not just
             the smallest -- useful when two controls overlap and the
             smallest-area match is not the one you meant.
    --json   one line of machine-readable JSON instead of the formatted
             report, for feeding into another tool rather than eyeballing.
"""

import json
import sys
import time

import pyguitest
from pyguitest import Capability, ImageNotFound, Role

_POLL_INTERVAL = 0.02

_USAGE = (
    "Usage:\n"
    "  python3 examples/09_gui_spy.py X Y            (works on any desktop)\n"
    "  python3 examples/09_gui_spy.py --find IMG.png (works on any desktop)\n"
    "  python3 examples/09_gui_spy.py --here          (X11/XWayland)\n"
    "  python3 examples/09_gui_spy.py --watch         (X11/XWayland)\n"
    "\n"
    "Any form also takes --tree (list every element containing the point,\n"
    "not just the smallest) and --json (machine-readable output).\n"
)

# Reversed once, from the Role class itself, so this never drifts out of
# sync with roles.py: {"push button": "Role.PUSH_BUTTON", ...}.
_ROLE_NAMES = {
    value: f"Role.{name}"
    for name, value in vars(Role).items()
    if isinstance(value, str) and not name.startswith("_")
}

# The sugar methods Session offers for the roles they cover -- when a
# matched element's role is one of these, the suggested snippet uses the
# named finder instead of the general element(role=, name=) form, the same
# way examples/03_widgets.py recommends.
_SUGAR = {
    Role.PUSH_BUTTON: "button",
    Role.ENTRY: "text_field",
    Role.TEXT: "text_field",
    Role.PASSWORD_TEXT: "text_field",
    Role.COMBO_BOX: "dropdown",
    Role.CHECK_BOX: "checkbox",
    Role.MENU_ITEM: "menu_item",
    Role.LINK: "link",
}


def _contains(geometry, x, y):
    gx, gy, gw, gh = geometry
    return gx <= x < gx + gw and gy <= y < gy + gh


def _walk(element):
    """Every element in this one's subtree, itself included."""
    yield element
    try:
        children = element.children
    except Exception:
        return
    for child in children:
        yield from _walk(child)


def _role_const(role):
    """The Role.X constant name for `role`, or the raw AT-SPI string."""
    return _ROLE_NAMES.get(role, role)


def _label(element):
    """A short 'role name' label for one element, for breadcrumbs and trees."""
    return f"{element.role} {element.name!r}" if element.name else element.role


def find_elements_at(gui, x, y):
    """Every accessible element whose geometry contains (x, y), largest first.

    The same per-element geometry test find_element_at uses, kept as a list
    rather than collapsed to the smallest match. Two elements can each
    report geometry that contains (x, y) without one being an ancestor of
    the other -- an overlapping popup, or plain stacking order a single
    "best" match cannot show -- which is what --tree is for. A geometry()
    call that fails (no Component interface, or the coordinate readback
    itself failing -- see AtspiBackend.geometry's own docstring) just drops
    that one element from consideration rather than aborting the walk; most
    of a typical tree is not visible at all.
    """
    matches = []
    for element in _walk(gui.root_element()):
        try:
            geometry = gui.geometry(element)
        except Exception:
            continue
        if not _contains(geometry, x, y):
            continue
        area = geometry[2] * geometry[3]
        if area <= 0:
            continue
        matches.append((element, geometry, area))
    matches.sort(key=lambda match: match[2], reverse=True)
    return [(element, geometry) for element, geometry, _area in matches]


def find_element_at(gui, x, y):
    """The smallest of find_elements_at(gui, x, y)'s matches.

    Smallest area wins rather than last-found or first-found: AT-SPI's tree
    order is not necessarily paint or z-order, so it says nothing reliable
    about which of several overlapping matches is "on top". Area does say
    something reliable -- a button sitting inside a panel sitting inside a
    window is smaller than either ancestor, so the smallest positive-area
    match is the most specific one, regardless of traversal order.

    Returns (element, geometry) or (None, None) if nothing at (x, y) could
    report usable geometry.
    """
    matches = find_elements_at(gui, x, y)
    if not matches:
        return None, None
    return matches[-1]


def ancestors(element):
    """The chain of containing elements: immediate parent first, root last."""
    chain = []
    node = element.parent
    while node is not None:
        chain.append(node)
        node = node.parent
    return chain


def describe(element, geometry, chain=()):
    """Everything about `element` worth printing, one line per field."""
    lines = [
        f"  role         {_role_const(element.role)}",
        f"  name         {element.name!r}",
        f"  geometry     {geometry}",
        f"  enabled      {element.enabled}",
        f"  visible      {element.visible}",
    ]
    if element.description:
        lines.append(f"  description  {element.description!r}")
    if element.text is not None:
        lines.append(f"  text         {element.text!r}")
    if element.value is not None:
        lines.append(f"  value        {element.value!r}")
    if element.checked is not None:
        lines.append(f"  checked      {element.checked}")
    if element.actions:
        lines.append(f"  actions      {', '.join(element.actions)}")
    if chain:
        breadcrumb = " > ".join(_label(ancestor) for ancestor in reversed(chain))
        lines.append(f"  ancestors    {breadcrumb}")
    return "\n".join(lines)


def suggest_snippet(element):
    """A pyguitest call that would find `element` again, as a string."""
    sugar = _SUGAR.get(element.role)
    if sugar and element.name:
        return f"gui.{sugar}({element.name!r}).click()"
    role_const = _role_const(element.role)
    if element.name:
        return f"gui.element(role={role_const}, name={element.name!r})"
    return f"gui.element(role={role_const})  # no name to match on"


def gather(gui, x, y):
    """Everything known about the point (x, y), as plain objects.

    Shared by print_report and json_report so the two forms of output
    cannot drift apart on what counts as "known" about a point.

    Returns a dict with two keys:
      "window":  a Window, None, or a string explaining why WINDOW_AT_POINT
                 is unavailable.
      "matches": find_elements_at's list, or a string explaining why
                 ELEMENT_TREE or WINDOW_GEOMETRY is unavailable.
    """
    result = {}

    if gui.supports(Capability.WINDOW_AT_POINT):
        result["window"] = gui.window_at(x, y)
    else:
        result["window"] = "WINDOW_AT_POINT unsupported on this desktop"

    if not gui.supports(Capability.ELEMENT_TREE):
        result["matches"] = (
            "ELEMENT_TREE unsupported -- element automation needs AT-SPI: "
            "sudo dnf install python3-gobject python3-pyatspi at-spi2-core; "
            "pip install -e '.[atspi]'"
        )
    elif not gui.supports(Capability.WINDOW_GEOMETRY):
        result["matches"] = (
            "WINDOW_GEOMETRY unsupported, so AT-SPI's own coordinates cannot "
            "be trusted here (see AtspiBackend.geometry's docstring) -- there "
            "is no reliable way to find an element by point on this desktop. "
            "gui.elements(role=..., name=...) still works without "
            "coordinates; see examples/03_widgets.py."
        )
    else:
        result["matches"] = find_elements_at(gui, x, y)

    return result


def print_report(gui, x, y, tree=False):
    """Print everything known about the point (x, y), human-readable.

    Never raises for a missing optional capability -- reports that it is
    missing and moves on, rather than dying. That matters most for
    --watch: one click that lands where AT-SPI cannot help should not end
    a loop that is otherwise working fine.
    """
    info = gather(gui, x, y)

    window = info["window"]
    if isinstance(window, str):
        print(f"window       ({window}; see pyguitest doctor)")
    elif window is None:
        print("window       (none at that point)")
    else:
        print(
            f"window       {window.title!r}  app_id={window.app_id!r}  pid={window.pid}"
        )
        if gui.supports(Capability.WINDOW_GEOMETRY):
            print(f"  geometry     {gui.geometry(window)}")
    print()

    matches = info["matches"]
    if isinstance(matches, str):
        print(f"element      {matches}")
        return
    if not matches:
        print("element      (nothing at that point reported usable geometry)")
        return

    element, geometry = matches[-1]
    chain = ancestors(element)

    print("element:")
    print(describe(element, geometry, chain))
    print()
    print("snippet:")
    print(f"  {suggest_snippet(element)}")

    if tree:
        print()
        print(f"tree ({len(matches)} element(s) containing this point, largest first):")
        for candidate_element, candidate_geometry in matches:
            marker = "*" if candidate_element is element else " "
            print(f"  {marker} {_label(candidate_element)}  {candidate_geometry}")


def json_report(gui, x, y, tree=False):
    """Print everything known about the point (x, y), as one line of JSON."""
    info = gather(gui, x, y)
    report = {"point": {"x": x, "y": y}}

    window = info["window"]
    if isinstance(window, str):
        report["window"] = {"error": window}
    elif window is None:
        report["window"] = None
    else:
        report["window"] = {
            "title": window.title,
            "app_id": window.app_id,
            "pid": window.pid,
            "geometry": (
                list(gui.geometry(window))
                if gui.supports(Capability.WINDOW_GEOMETRY)
                else None
            ),
        }

    matches = info["matches"]
    if isinstance(matches, str):
        report["element"] = {"error": matches}
    elif not matches:
        report["element"] = None
    else:
        element, geometry = matches[-1]
        chain = ancestors(element)
        report["element"] = {
            "role": element.role,
            "role_const": _role_const(element.role),
            "name": element.name,
            "geometry": list(geometry),
            "enabled": element.enabled,
            "visible": element.visible,
            "description": element.description or None,
            "text": element.text,
            "value": element.value,
            "checked": element.checked,
            "actions": element.actions,
            "ancestors": [_label(ancestor) for ancestor in reversed(chain)],
            "snippet": suggest_snippet(element),
        }
        if tree:
            report["tree"] = [
                {
                    "role": candidate.role,
                    "role_const": _role_const(candidate.role),
                    "name": candidate.name,
                    "geometry": list(candidate_geometry),
                }
                for candidate, candidate_geometry in matches
            ]

    print(json.dumps(report))


def report_at(gui, x, y, tree=False, as_json=False):
    """Report everything known about the point (x, y), in either form."""
    if as_json:
        json_report(gui, x, y, tree=tree)
    else:
        print_report(gui, x, y, tree=tree)


def watch(gui, tree=False, as_json=False):
    """Report on every left click, until Ctrl+C.

    Needs an X connection -- X11 or XWayland; see the module docstring for
    why a desktop without one has no path for this at all. Polls rather
    than captures:
    `is_button_pressed`/`pointer_position` are the same readback any
    script could already call once, just repeated here to notice a
    press-then-release. Debounced on the transition from not-pressed to
    pressed so one physical click reports once, not once per poll for as
    long as the button stays down. With --json, only the report lines
    themselves go to stdout, one per click, so the stream stays valid
    JSONL; the banners below go to stderr instead.
    """
    print("watching for clicks -- click anywhere, Ctrl+C to stop\n", file=sys.stderr)
    was_pressed = False
    try:
        while True:
            pressed = gui.is_button_pressed(1)
            if pressed and not was_pressed:
                x, y = gui.pointer_position()
                if not as_json:
                    print(f"=== click at ({x}, {y}) " + "=" * 40)
                report_at(gui, x, y, tree=tree, as_json=as_json)
                if not as_json:
                    print()
            was_pressed = pressed
            time.sleep(_POLL_INTERVAL)
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)


def _watch_mode(gui, args, tree, as_json):
    """--watch: follow the pointer, reporting on each click."""
    if len(args) != 1:
        sys.exit(_USAGE)
    if not (
        gui.supports(Capability.POINTER_QUERY)
        and gui.supports(Capability.INPUT_STATE_QUERY)
    ):
        sys.exit(
            "--watch needs Capability.POINTER_QUERY and "
            "Capability.INPUT_STATE_QUERY -- both need an X connection "
            "(X11 or XWayland, with python-xlib), and this desktop does "
            "not have them:\n"
            f"{gui.environment.summary()}\n\n"
            "Pass a coordinate directly instead:\n"
            "  python3 examples/09_gui_spy.py X Y"
        )
    watch(gui, tree=tree, as_json=as_json)


def _here(gui, args, as_json):
    """--here: the point the pointer is on right now."""
    if len(args) != 1:
        sys.exit(_USAGE)
    if not gui.supports(Capability.POINTER_QUERY):
        sys.exit(
            "--here needs Capability.POINTER_QUERY -- an X connection "
            "(X11 or XWayland, with python-xlib), and this desktop does "
            "not have it:\n"
            f"{gui.environment.summary()}\n\n"
            "Pass a coordinate directly instead:\n"
            "  python3 examples/09_gui_spy.py X Y"
        )
    x, y = gui.pointer_position()
    if not as_json:
        print(f"pointer is at ({x}, {y})\n")
    return x, y


def _find(gui, args, as_json):
    """--find: locate a template image and inspect its centre."""
    if len(args) != 2:
        sys.exit(
            "--find needs exactly one template image:\n"
            "  python3 examples/09_gui_spy.py --find button.png"
        )
    template = args[1]
    for capability in (Capability.SCREEN_CAPTURE, Capability.IMAGE_LOCATE):
        if not gui.supports(capability):
            sys.exit(
                f"{capability.name} is unavailable. Template matching "
                "needs both a way to capture the screen and ImageMagick's "
                "`compare` -- run `pyguitest doctor` for what to install."
            )
    try:
        match = gui.locate_image(template)
    except ImageNotFound:
        sys.exit(
            f"{template} was not found on screen.\n\n"
            "Usually the template, not the search: it must be cropped "
            "from this machine, at this scaling and theme. See example 08."
        )
    # Aim at the middle rather than the corner: the top-left of a match
    # sits on the control's edge, where the click a suggested snippet
    # would make could land on a border or just outside.
    x, y = match.x + match.width // 2, match.y + match.height // 2
    if not as_json:
        print(
            f"found {template!r} at {match.x},{match.y} "
            f"(score {match.score}); inspecting its centre ({x}, {y})\n"
        )
    return x, y


def _coordinates(args):
    """A literal `X Y` pair."""
    try:
        return int(args[0]), int(args[1])
    except ValueError:
        sys.exit(f"X and Y must be integers, got {args[0]!r} {args[1]!r}")


def main(argv):
    """Parse `argv`, then report on the point(s) it names."""
    args = argv[1:]
    as_json = "--json" in args
    tree = "--tree" in args
    args = [arg for arg in args if arg not in ("--json", "--tree")]

    gui = pyguitest.connect()

    # Each mode works out which point to inspect; --watch is the exception,
    # since it inspects many over time and reports as it goes.
    if args[:1] == ["--watch"]:
        _watch_mode(gui, args, tree=tree, as_json=as_json)
        return
    if args[:1] == ["--here"]:
        x, y = _here(gui, args, as_json)
    elif args[:1] == ["--find"]:
        x, y = _find(gui, args, as_json)
    elif len(args) == 2:
        x, y = _coordinates(args)
    else:
        sys.exit(_USAGE)

    report_at(gui, x, y, tree=tree, as_json=as_json)


if __name__ == "__main__":
    main(sys.argv)
