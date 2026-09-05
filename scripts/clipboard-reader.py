#!/usr/bin/env python3
"""Read the clipboard as a native Wayland client, and print what it got.

The witness `_portal_clipboard_validate.py` could not have. On GNOME the
only outside reader available to that script is `xclip`, through XWayland,
because Mutter implements no wlr-data-control and `wl-paste` cannot read
that desktop's clipboard at all. So every clipboard result recorded so far
is really "as seen from X11" -- which leaves the question that matters for
a Wayland-first package unanswered: can a native Wayland client read a
selection the portal backend owns?

This answers it, and it has to open a window to do so. That is not
decoration: a Wayland client is offered the selection through
`wl_data_device`, and the compositor sends that offer only to the client
holding keyboard focus. A headless or windowless reader is handed nothing
and would report an empty clipboard whether or not the write worked --
exactly the false negative this exists to rule out.

Prints one line, meant to be parsed by whoever spawned it:

    CLIPBOARD <repr of the text>
    ERROR <message>

    python3 clipboard-reader.py [--timeout SECONDS]
"""

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, GLib, Gtk  # noqa: E402 -- follows require_version

TITLE = "pyguitest clipboard witness"
APP_ID = "local.pyguitest.ClipboardWitness"
RETRY_MS = 700
"""Milliseconds between read attempts.

Reading once does not work, and the reason is the whole difficulty here.
A Wayland client is offered the selection through `wl_data_device` only
while it holds keyboard focus -- and GNOME denies focus to a window whose
process was spawned from a script rather than launched by the user, so a
freshly presented window can sit there unfocused and be offered nothing.
Measured: with an empty clipboard the read fails *immediately* ("Cannot
read from empty clipboard", no offer required), while with real content
it hangs indefinitely, which is the same symptom as a broken write.

So this keeps asking until the answer arrives or the timeout runs out,
which also means clicking the window part-way through makes it work.
"""


def read_clipboard(app, timeout):
    """Keep reading the clipboard until it answers, or the timeout ends it."""
    clipboard = Gdk.Display.get_default().get_clipboard()
    state = {"done": False, "attempts": 0, "last": ""}

    def finish(line):
        if state["done"]:
            return
        state["done"] = True
        print(line)
        app.quit()

    def finished(source, result):
        try:
            text = source.read_text_finish(result)
        except GLib.Error as error:
            # Kept, not printed: an early attempt failing is expected
            # while the window is still unfocused. Only the last one is
            # worth reporting, and only if nothing ever succeeds.
            state["last"] = error.message
            return
        if text is None:
            state["last"] = "the clipboard offered no text"
            return
        finish(f"CLIPBOARD {text!r}")

    def attempt():
        if state["done"]:
            return GLib.SOURCE_REMOVE
        state["attempts"] += 1
        if state["attempts"] == 4:
            print(
                "still unfocused -- click the window if it is not in front",
                file=sys.stderr,
            )
        clipboard.read_text_async(None, finished)
        return GLib.SOURCE_CONTINUE

    GLib.timeout_add(RETRY_MS, attempt)
    GLib.timeout_add_seconds(
        timeout,
        lambda: finish(f"ERROR {state['last'] or 'timed out'}"),
    )


def on_activate(app, timeout):
    """Show the window, then read once it can actually be offered anything."""
    window = Gtk.ApplicationWindow(application=app, title=TITLE)
    window.set_default_size(360, 120)
    window.set_child(Gtk.Label(label="reading the clipboard…"))
    window.present()
    read_clipboard(app, timeout)


def main(argv):
    """Read the clipboard and exit. Prints exactly one line."""
    timeout = 10
    if len(argv) >= 2 and argv[0] == "--timeout":
        timeout = int(argv[1])
    app = Gtk.Application(application_id=APP_ID)
    app.connect("activate", on_activate, timeout)
    return app.run([])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
