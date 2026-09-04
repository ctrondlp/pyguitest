#!/usr/bin/env python3
"""Open one ordinary window and hold it open until killed.

Something for the window-control checks to point at. `validate-gnome-
extension.sh` needs a window it can move, resize, capture and hit-test, and
inside `headless-session.sh` there is nothing open at all -- so it spawns
this rather than exiting early with "open a window and re-run".

Deliberately not one of the editors that script spawns later for its
window-event checks. Those are GApplications: a second launch of one asks
the running instance to open a document instead of creating a process, so
sharing an application id with them would make it ambiguous which window a
"new" event belonged to. This has an id of its own and is a plain window.

The size is fixed and modest so the window is neither maximized nor tiled
-- a maximized window refuses to move, and the geometry battery would then
pass vacuously against a window that never went anywhere.

    python3 probe-window.py [--title TITLE]
"""

import sys

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gio, Gtk  # noqa: E402 -- must follow require_version

TITLE = "pyguitest probe window"
APP_ID = "local.pyguitest.ProbeWindow"


def on_activate(app, title):
    """Show the window once the application is up."""
    window = Gtk.ApplicationWindow(application=app, title=title)
    window.set_default_size(600, 400)
    window.set_child(Gtk.Label(label=title))
    window.present()


def main(argv):
    """Run until the process is killed. Never returns on its own."""
    title = TITLE
    if len(argv) >= 2 and argv[0] == "--title":
        title = argv[1]
    # NON_UNIQUE so each invocation is its own process with its own
    # window. The default single-instance behaviour would hand a second
    # launch to the first process, which then owns the new window -- and
    # the window-event checks spawn one of these precisely so they can
    # kill it and watch the "close" event arrive.
    app = Gtk.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.connect("activate", on_activate, title)
    return app.run([])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
