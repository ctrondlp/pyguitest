#!/usr/bin/env python3
"""Launch a text editor, type into it, and close it.

The equivalent of X11::GUITest's TextEditor_1.pl, written the way this package
prefers: wait on an event rather than polling, and act on widgets rather than
coordinates.

    python3 examples/04_drive_an_editor.py gedit
"""

import sys

import pyguitest
from pyguitest import Capability, WindowNotFound

editor = sys.argv[1] if len(sys.argv) > 1 else "gedit"
STARTUP_TIMEOUT = 30  # seconds to allow a cold app launch to appear

gui = pyguitest.connect(key_delay=0.02)

if not gui.supports(Capability.WINDOW_LIST):
    sys.exit("This desktop cannot list windows; see example 01.")

print(f"starting {editor}")

# `with` rather than try/finally: start_app returns an Application, and
# leaving the block terminates the editor -- killing it if it will not go,
# which matters here because a document with unsaved text answers SIGTERM
# with a "Save changes?" dialog rather than exiting. Confirmed live.
with gui.start_app([editor]) as app:
    # wait_for_window is event-driven where the compositor supports it
    # (sway today), and polls find_windows everywhere else -- either way, a
    # single fixed sleep would just be a race against however long this
    # particular launch takes, cold start vs. warm.
    window = gui.wait_for_window(editor, timeout=STARTUP_TIMEOUT)
    if window is None:
        raise WindowNotFound(
            f"{editor} did not open a window within {STARTUP_TIMEOUT}s"
        )

    print(f"found window {window.title!r}")

    if gui.supports(Capability.WINDOW_ACTIVATE):
        gui.activate_window(window)

    if gui.supports(Capability.TEXT_ENTRY):
        gui.type_text("Hello from pyguitest.\n")
    else:
        print("(no input transport; nothing typed)")

    gui.wait(1)

print("done")
