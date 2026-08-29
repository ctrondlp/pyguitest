#!/usr/bin/env python3
"""Find and use individual widgets: buttons, text boxes, dropdowns.

This is the recommended way to automate a GUI, and the thing X11::GUITest
could never do. It walked X windows, but GTK and Qt draw their widgets
themselves, so a button is not a window and has no id to find. The
accessibility tree is the only place widgets are individually visible.

Because it matches on what a widget *is* and what it is *called*, automation
survives the application being moved or resized -- unlike clicking at (842, 612).

    python3 examples/03_widgets.py
"""

import sys

import pyguitest
from pyguitest import Capability, ElementNotFound, Role

gui = pyguitest.connect()

if not gui.supports(Capability.ELEMENT_TREE):
    sys.exit(
        "Element automation needs AT-SPI.\n"
        "  sudo dnf install python3-gobject python3-pyatspi at-spi2-core\n"
        "  pip install -e '.[atspi]'"
    )

# --- what is on screen, by kind -------------------------------------------

for role in (Role.PUSH_BUTTON, Role.ENTRY, Role.COMBO_BOX, Role.CHECK_BOX):
    found = gui.elements(role=role)
    print(f"{role:14} {len(found)} found")
    for element in found[:5]:
        print(f"    {element.name!r}  enabled={element.enabled}")

# --- using them -----------------------------------------------------------
#
# Each finder raises ElementNotFound if nothing matches, so a script stops
# where the mistake is rather than several lines later.

try:
    gui.button("OK").click()

    field = gui.text_field("Name")
    field.set_text("Ada Lovelace")
    print("field now reads:", field.text)

    gui.dropdown("Country").choose("Norway")

    box = gui.checkbox("Remember me")
    if not box.checked:
        box.click()

except ElementNotFound as exc:
    print(f"\n(nothing to drive here: {exc})")

# --- walking the tree yourself --------------------------------------------
#
# Element.children replaces GetChildWindows. Elements carry a role and a name
# rather than an anonymous window id.

root = gui.root_element()
for application in root.children[:3]:
    print(f"\n{application.name} ({application.role})")
    for child in application.children[:5]:
        print(f"    {child.role:16} {child.name!r}")
