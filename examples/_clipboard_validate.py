#!/usr/bin/env python3
"""One-off live validation of Capability.CLIPBOARD, forced rather than composited.

New capability, never run against a real desktop before this script.
Backed by a CLI tool (see tools.CLIPBOARD_TOOLS): wl-copy/wl-paste on
KWin and the wlroots compositors, xclip/xsel on X11, and pbcopy/pbpaste
on macOS, which ship with the OS. Confirmed by hand first, ad hoc, on
KDE Plasma 6/KWin -- `echo -n text | wl-copy` followed by `wl-paste`
round-tripped correctly, and the wl-copy process was still resident in
`ps` afterwards (it forks into the background to keep serving the
selection). This script exercises the same round trip through
pyguitest's own get_clipboard()/set_clipboard(), not the raw tools.

The cases are the ones a clipboard can go wrong on rather than one
happy path: a second write must *replace*, not append or be ignored;
non-ASCII must survive unchanged; empty text must stay empty rather than
becoming a newline; the value must outlive the session object that set
it; and the whole cycle is repeated, because one successful round trip
is weak evidence for anything driven through a subprocess. Every
comparison is tallied and a mismatch exits 1, so this is an acceptance
test and not only a report -- see docs/validation.md.

The macOS tool is the one that does not fork, so `del gui` there proves a
different thing than it does on Linux: the pasteboard is held by the
pasteboard server, not by a daemon this package started. It is also the
one platform with no PRIMARY, which is why the primary section below
reports a refusal on a Mac and a round trip everywhere else.

    python3 _clipboard_validate.py

Runs unattended: the closing prompt treats EOF as "done".
"""

import gc
import sys
import time

import pyguitest
from pyguitest import Capability
from pyguitest.errors import CapabilityUnsupported

gui = pyguitest.connect(backend="clipboard")
print(f"forced backend: {gui.backend.name}")
print(f"tool: {gui.backend.tool.name}")

if not gui.supports(Capability.CLIPBOARD):
    sys.exit(
        "CLIPBOARD is unsupported on this desktop -- no clipboard tool was "
        "found, or (on GNOME) none exists yet; see tools.CLIPBOARD_TOOLS "
        "and clipboard.py's module docstring. Run `pyguitest doctor`."
    )

failures = []


def check(label, actual, expected):
    """Tally one comparison and say what it saw either way."""
    ok = actual == expected
    if not ok:
        failures.append(label)
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {actual!r}")
    if not ok:
        print(f"     expected: {expected!r}")
    return ok


marker_1 = f"pyguitest-clipboard-spike-{int(time.time())}"
print(f"\nwriting {marker_1!r}...")
gui.set_clipboard(marker_1)
check("round trip", gui.get_clipboard(), marker_1)

print("\nwaiting 1s to confirm the value persists after the write call returns...")
time.sleep(1)
check("still there after 1s", gui.get_clipboard(), marker_1)

marker_2 = f"pyguitest-clipboard-second-{int(time.time())}"
print(f"\nwriting a second value {marker_2!r}...")
gui.set_clipboard(marker_2)
check("second write replaced the first", gui.get_clipboard(), marker_2)

unicode_text = "h\u00e9llo \u2014 \u65e5\u672c\u8a9e \u2705 \U0001f389"
print(f"\nwriting non-ASCII {unicode_text!r} (bytes: {len(unicode_text.encode())})...")
gui.set_clipboard(unicode_text)
check("non-ASCII survived byte for byte", gui.get_clipboard(), unicode_text)

print("\nwriting empty text...")
gui.set_clipboard("")
check("empty stayed empty", gui.get_clipboard(), "")

print("\nwriting text back after the empty write...")
gui.set_clipboard(marker_2)
check("recovered after empty", gui.get_clipboard(), marker_2)

print("\nPRIMARY, the second selection...")
try:
    gui.set_clipboard("primary-marker", primary=True)
    check("PRIMARY round trip", gui.get_clipboard(primary=True), "primary-marker")
    check("PRIMARY did not disturb the clipboard", gui.get_clipboard(), marker_2)
except CapabilityUnsupported as exc:
    print(f"refused, as a single-selection platform should: {exc}")

print("\ndropping the session object and reading back through a fresh one...")
del gui
gc.collect()
gui = pyguitest.connect(backend="clipboard")
check("value outlived the session that set it", gui.get_clipboard(), marker_2)

rounds = 10
print(f"\n{rounds} write/read cycles, each with its own value...")
mismatched = 0
for round_number in range(rounds):
    text = f"pyguitest-clipboard-round-{round_number}-{int(time.time() * 1000)}"
    gui.set_clipboard(text)
    if gui.get_clipboard() != text:
        mismatched += 1
check(f"all {rounds} cycles matched", mismatched, 0)

print(f"\n{len(failures)} failed check(s)")
for label in failures:
    print(f"  FAIL {label}")
if failures:
    sys.exit(1)

print("\nPaste somewhere by hand now to confirm a real application sees it too.")
try:
    input("Press Enter once you have checked (or just to exit): ")
except EOFError:
    print("(no terminal; exiting)")
