#!/usr/bin/env python3
"""Keystrokes and pointer input, including X11::GUITest's send_keys syntax.

The compatibility story, and the one thing a script being ported from Perl
will reach for first. `send_keys` takes the same notation X11::GUITest used
-- modifiers as prefixes, key names in braces -- so quoted strings from an
old script keep working:

    ^(a)        Ctrl-A            %(f)     Alt-F
    +(abc)      Shift held for abc    #(r) Meta/Super-R
    {TAB}{ENT}  named keys        {{}      a literal brace

Also shows the raw pointer calls, which are deliberately a last resort:
clicking at (842, 612) breaks the moment the window moves, whereas
`gui.button("OK").click()` does not. Use these when there is no accessible
element to aim at -- a canvas, a game, a custom-drawn control.

    python3 examples/07_keys_and_pointer.py
"""

import sys

import pyguitest
from pyguitest import Capability, quote_for_type

gui = pyguitest.connect()

# -- typing ----------------------------------------------------------------

if not gui.supports(Capability.KEY_EVENT):
    sys.exit(
        "No key injection on this session. See example 01 for what is "
        "missing, or `pyguitest doctor` for what to install."
    )

print("send_keys understands the X11::GUITest notation:")
gui.send_keys("hello{SPACE}world")
gui.send_keys("^(a)")  # select all
gui.send_keys("{BKSP}")

# Text from a user or a file may contain the characters send_keys treats as
# syntax. quote_for_type escapes them, which is what QuoteStringForSendKeys
# did -- with one fix: the original missed '#', so a literal # slipped
# through as a Meta modifier.
awkward = "50% off (today) ^ tomorrow #1"
print(f"  escaping {awkward!r}")
gui.send_keys(quote_for_type(awkward))

# type_text is the plainer route when no syntax is wanted at all. On
# eiinput it is keymap-safe: it presses whichever physical key produces the
# character on the *active* layout, so this is correct on AZERTY too.
if gui.supports(Capability.TEXT_ENTRY):
    gui.type_text("no escaping needed here: 50% ^ #1")

# -- the pointer -----------------------------------------------------------

if not gui.supports(Capability.POINTER_MOVE):
    print("\nNo pointer injection here; skipping the pointer half.")
    sys.exit(0)

print("\nmoving the pointer")
gui.move_mouse(400, 300)
gui.click()  # button 1 by default; click(3) for the context menu

if gui.supports(Capability.POINTER_SCROLL):
    gui.scroll(dy=-3)  # negative is down, matching a wheel

# Reading the pointer back is tier 6: possible under X11, impossible for an
# ordinary client on any Wayland compositor. This is the asymmetry the tier
# scale exists to express -- injection works, readback does not.
if gui.supports(Capability.POINTER_QUERY):
    print(f"pointer is at {gui.pointer_position()}")
else:
    print(
        "POINTER_QUERY is unavailable, which is normal on Wayland: a client\n"
        "may move the pointer but may not ask where it is."
    )
