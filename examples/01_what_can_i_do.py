#!/usr/bin/env python3
"""Show what this desktop actually supports, and how to unlock more.

Start here. Every other example depends on capabilities this one reports, and
what is available differs by compositor -- that is the whole point of the
capability model. Whatever is missing gets install advice tailored to the
session type (X11/Wayland), compositor, and distribution actually detected.

    python3 examples/01_what_can_i_do.py
"""

import pyguitest
from pyguitest import Capability
from pyguitest.hints import advice, hints_for

gui = pyguitest.connect()

print(gui.report())
print()

# Ask before you depend. This is the intended pattern for a test suite: skip
# what the desktop cannot do rather than failing halfway through a run.
for capability in (
    Capability.WINDOW_LIST,
    Capability.ELEMENT_TREE,
    Capability.POINTER_MOVE,
    Capability.SCREEN_CAPTURE,
):
    mark = "yes" if gui.supports(capability) else "no "
    print(f"[{mark}] {capability.name:16} {capability.description}")

# The gaps above are usually one install away. What to install depends on
# the session type (X11 vs. Wayland), the compositor (GNOME, wlroots, KDE),
# and the distribution -- hints_for() reasons about all three, which is why
# the same "no" can carry a different fix on different desktops. This is
# also what `pyguitest doctor` prints on its own.
# capabilities= is passed through so the one hint that needs a live D-Bus
# check -- whether the pyguitest-window-control GNOME Shell extension is
# actually installed and enabled -- can be included too; environment alone
# can't tell (see hints.hints_for).
if list(hints_for(gui.environment, capabilities=gui.capabilities)):
    print()
    print(advice(gui.environment, capabilities=gui.capabilities))
