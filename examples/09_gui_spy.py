#!/usr/bin/env python3
"""GUI Spy: point at a screen coordinate and see what pyguitest would call it.

An element inspector: given a point on screen, report the window and
accessible element there, so a script can be written against `role=` and
`name=` instead of guessed at.

Two of the three ways to give it a point are X11-ONLY. Reading where the
pointer is, even once, is `Capability.POINTER_QUERY` -- deliberately
absent on every Wayland compositor, since a process that could poll it
freely would be handing out exactly what a keylogger reads. Passing a
coordinate you already have is the one way that works everywhere:

    python3 examples/09_gui_spy.py 842 612    # works on any desktop
    python3 examples/09_gui_spy.py --here     # X11 only -- reads the pointer once
    python3 examples/09_gui_spy.py --watch    # X11 only -- reports on every click

On GNOME, KDE, sway, Hyprland, niri: use the first form. Read the
coordinate off a screenshot (example 05) or your desktop's own
pointer-position display, then pass it directly -- `--here`/`--watch` will
tell you plainly, and immediately, that the capability is missing rather
than doing something degraded.

`--watch` additionally needs `Capability.INPUT_STATE_QUERY` (X11-only, same
reason) to notice a click at all, since it does not capture input itself --
it polls whether the left button is currently held, the same readback
`--here` uses once, just repeated. Every point it reports is one you
visibly clicked while this was running in your own foreground terminal;
nothing here runs unattended, hooks input, or writes anything down.
"""

import sys
import time

import pyguitest
from pyguitest import Capability, Role

_POLL_INTERVAL = 0.02

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


def find_element_at(gui, x, y):
    """The smallest accessible element whose geometry contains (x, y).

    Walks the whole desktop's accessible tree from root_element() down,
    since AT-SPI exposes no "element at point" query of its own, only
    per-element geometry to test by hand.

    Smallest area wins rather than last-found or first-found: AT-SPI's
    tree order is not necessarily paint or z-order, so it says nothing
    reliable about which of several overlapping matches is "on top". Area
    does say something reliable -- a button sitting inside a panel sitting
    inside a window is smaller than either ancestor, so the smallest
    positive-area match is the most specific one, regardless of traversal
    order. A geometry() call that fails (no Component interface, or the
    coordinate readback itself failing -- see AtspiBackend.geometry's own
    docstring) just drops that one element from consideration rather than
    aborting the walk; most of a typical tree is not visible at all.

    Returns (element, geometry) or (None, None) if nothing at (x, y) could
    report usable geometry.
    """
    best = best_geometry = None
    best_area = None
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
        if best_area is None or area < best_area:
            best, best_geometry, best_area = element, geometry, area
    return best, best_geometry


def describe(element, geometry):
    """Everything about `element` worth printing, one line per field."""
    role_const = _ROLE_NAMES.get(element.role, repr(element.role))
    lines = [
        f"  role         {role_const}",
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
    return "\n".join(lines)


def suggest_snippet(element):
    """A pyguitest call that would find `element` again, as a string."""
    sugar = _SUGAR.get(element.role)
    if sugar and element.name:
        return f"gui.{sugar}({element.name!r}).click()"
    role_const = _ROLE_NAMES.get(element.role, repr(element.role))
    if element.name:
        return f"gui.element(role={role_const}, name={element.name!r})"
    return f"gui.element(role={role_const})  # no name to match on"


def report_at(gui, x, y):
    """Print everything known about the point (x, y).

    Never raises for a missing optional capability -- reports that it is
    missing and moves on, rather than dying. That matters most for
    --watch: one click that lands where AT-SPI cannot help should not end
    a loop that is otherwise working fine.
    """
    # -- the window at that point ------------------------------------------

    if gui.supports(Capability.WINDOW_AT_POINT):
        window = gui.window_at(x, y)
        if window is None:
            print("window       (none at that point)")
        else:
            print(
                f"window       {window.title!r}  app_id={window.app_id!r}"
                f"  pid={window.pid}"
            )
            if gui.supports(Capability.WINDOW_GEOMETRY):
                print(f"  geometry     {gui.geometry(window)}")
    else:
        print(
            "window       (WINDOW_AT_POINT unsupported on this desktop; "
            "see pyguitest doctor)"
        )
    print()

    # -- the accessible element at that point ------------------------------

    if not gui.supports(Capability.ELEMENT_TREE):
        print(
            "element      ELEMENT_TREE unsupported -- element automation "
            "needs AT-SPI:\n"
            "  sudo dnf install python3-gobject python3-pyatspi at-spi2-core\n"
            "  pip install -e '.[atspi]'"
        )
        return
    if not gui.supports(Capability.WINDOW_GEOMETRY):
        print(
            "element      WINDOW_GEOMETRY unsupported, so AT-SPI's own "
            "coordinates cannot be trusted here (see AtspiBackend.geometry's "
            "docstring) -- there is no reliable way to find an element by "
            "point on this desktop. gui.elements(role=..., name=...) still "
            "works without coordinates; see examples/03_widgets.py."
        )
        return

    element, geometry = find_element_at(gui, x, y)
    if element is None:
        print("element      (nothing at that point reported usable geometry)")
        return

    print("element:")
    print(describe(element, geometry))
    print()
    print("snippet:")
    print(f"  {suggest_snippet(element)}")


def watch(gui):
    """Report on every left click, until Ctrl+C.

    X11 only -- see the module docstring for why every other desktop has
    no path for this at all. Polls rather than captures:
    `is_button_pressed`/`pointer_position` are the same readback any
    script could already call once, just repeated here to notice a
    press-then-release. Debounced on the transition from not-pressed to
    pressed so one physical click reports once, not once per poll for as
    long as the button stays down.
    """
    print("watching for clicks -- click anywhere, Ctrl+C to stop\n")
    was_pressed = False
    try:
        while True:
            pressed = gui.is_button_pressed(1)
            if pressed and not was_pressed:
                x, y = gui.pointer_position()
                print(f"=== click at ({x}, {y}) " + "=" * 40)
                report_at(gui, x, y)
                print()
            was_pressed = pressed
            time.sleep(_POLL_INTERVAL)
    except KeyboardInterrupt:
        print("\nstopped")


def main(argv):
    """Parse `argv`, then report on the point(s) it names."""
    gui = pyguitest.connect()

    if len(argv) == 2 and argv[1] == "--watch":
        if not (
            gui.supports(Capability.POINTER_QUERY)
            and gui.supports(Capability.INPUT_STATE_QUERY)
        ):
            sys.exit(
                "--watch needs Capability.POINTER_QUERY and "
                "Capability.INPUT_STATE_QUERY -- both X11-only, and this "
                "desktop does not have them:\n"
                f"{gui.environment.summary()}\n\n"
                "Pass a coordinate directly instead:\n"
                "  python3 examples/09_gui_spy.py X Y"
            )
        watch(gui)
        return

    if len(argv) == 2 and argv[1] == "--here":
        if not gui.supports(Capability.POINTER_QUERY):
            sys.exit(
                "--here needs Capability.POINTER_QUERY -- X11-only, and "
                "this desktop does not have it:\n"
                f"{gui.environment.summary()}\n\n"
                "Pass a coordinate directly instead:\n"
                "  python3 examples/09_gui_spy.py X Y"
            )
        x, y = gui.pointer_position()
        print(f"pointer is at ({x}, {y})\n")
    elif len(argv) == 3:
        try:
            x, y = int(argv[1]), int(argv[2])
        except ValueError:
            sys.exit(f"X and Y must be integers, got {argv[1]!r} {argv[2]!r}")
    else:
        sys.exit(
            "Usage:\n"
            "  python3 examples/09_gui_spy.py X Y      (works on any desktop)\n"
            "  python3 examples/09_gui_spy.py --here   (X11 only)\n"
            "  python3 examples/09_gui_spy.py --watch  (X11 only)\n"
        )

    report_at(gui, x, y)


if __name__ == "__main__":
    main(sys.argv)
