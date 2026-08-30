#!/usr/bin/env python3
"""One-off live validation of Capability.CLIPBOARD, forced rather than composited.

New capability, never run against a real desktop before this script.
Backed by a CLI tool (see tools.CLIPBOARD_TOOLS): wl-copy/wl-paste on
KWin and the wlroots compositors, xclip/xsel on X11. Confirmed by hand
first, ad hoc, on KDE Plasma 6/KWin -- `echo -n text | wl-copy` followed
by `wl-paste` round-tripped correctly, and the wl-copy process was still
resident in `ps` afterwards (it forks into the background to keep serving
the selection). This script exercises the same round trip through
pyguitest's own get_clipboard()/set_clipboard(), not the raw tools.

Also checks that a second, unrelated round trip does not see the first
value -- i.e. that set_clipboard() actually replaces the previous content
rather than appending, and that nothing here is reading a stale value
cached from construction.

    python3 _clipboard_validate.py
"""

import sys
import time

import pyguitest
from pyguitest import Capability
from pyguitest.errors import CapabilityUnsupported

gui = pyguitest.connect(backend="clipboard")
print(f"forced backend: {gui.backend.name}")

if not gui.supports(Capability.CLIPBOARD):
    sys.exit(
        "CLIPBOARD is unsupported on this desktop -- no clipboard tool was "
        "found, or (on GNOME) none exists yet; see tools.CLIPBOARD_TOOLS "
        "and clipboard.py's module docstring. Run `pyguitest doctor`."
    )

marker_1 = f"pyguitest-clipboard-spike-{int(time.time())}"
print(f"writing {marker_1!r}...")
gui.set_clipboard(marker_1)

read_back = gui.get_clipboard()
print(f"read back: {read_back!r}")
print("round trip matched?", read_back == marker_1)

print("\nwaiting 1s to confirm the value persists after the write call returns...")
time.sleep(1)
still_there = gui.get_clipboard()
print("still there after 1s?", still_there == marker_1)

marker_2 = f"pyguitest-clipboard-second-{int(time.time())}"
print(f"\nwriting a second value {marker_2!r}...")
gui.set_clipboard(marker_2)
second_read = gui.get_clipboard()
print(f"read back: {second_read!r}")
print("second write replaced the first, not appended?", second_read == marker_2)

try:
    gui.get_clipboard()
    print("\nall calls completed without raising")
except CapabilityUnsupported as exc:
    sys.exit(f"unexpected refusal after supports() said yes: {exc}")

print("\nPaste somewhere by hand now to confirm a real application sees it too.")
input("Press Enter once you have checked (or just to exit): ")
