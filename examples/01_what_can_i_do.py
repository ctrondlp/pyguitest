#!/usr/bin/env python3
"""Show what this desktop actually supports.

Start here. Every other example depends on capabilities this one reports, and
what is available differs by compositor -- that is the whole point of the
capability model.

    python3 examples/01_what_can_i_do.py
"""

import pyguitest
from pyguitest import Capability

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
