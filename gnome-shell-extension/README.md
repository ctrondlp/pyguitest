# pyguitest-window-control

A GNOME Shell extension with no UI of its own. It gives pyguitest's
`GnomeShellBackend` a way to list, move, resize, activate and minimize
windows on GNOME, which nothing else can do on a pure Wayland session (no
XWayland), because Mutter implements no foreign-toplevel protocol. It also
captures a single window's pixels without a consent prompt, and reports
window open, close and title-change events.

It is optional: without it, pyguitest on GNOME still has elements, input,
and whole-screen capture through the Screenshot portal, but less window
control. Read
[What installing this means](#what-installing-this-means) before enabling it.

**Requirements:** GNOME Shell 45 or newer (the `shell-version` list in
`metadata.json`), and a copy of the pyguitest source tree — the extension is
not included in the pip package.

**Status:** window control, window capture and window events have been
validated live with `scripts/validate-gnome-extension.sh` on GNOME Shell 50.4,
and window capture again on 51.beta, where the capture code path changed
(see [Window capture](#window-capture)). [docs/validation.md](../docs/validation.md)
has the runs.

## Install

From the root of a pyguitest checkout:

```sh
UUID=pyguitest-window-control@pyguitest.local
mkdir -p ~/.local/share/gnome-shell/extensions/$UUID
cp gnome-shell-extension/$UUID/* ~/.local/share/gnome-shell/extensions/$UUID/
```

GNOME Shell only picks up a new extension when it starts. On Wayland, log
out and back in; on X11, `Alt+F2`, type `r`, Enter. Then enable it:

```sh
gnome-extensions enable pyguitest-window-control@pyguitest.local
```

Confirm it is running:

```sh
gnome-extensions info pyguitest-window-control@pyguitest.local
```

It should report `State: ACTIVE`. If it reports `ERROR`, GNOME Shell's log
says why: `journalctl -f /usr/bin/gnome-shell`, or Looking Glass
(`Alt+F2`, then `lg`).

**After updating the extension, log out and back in again.** On Wayland the
shell keeps running the code it loaded at login, so a copy you have just
overwritten still reports `ACTIVE` while behaving like the old one. The usual
symptom is `UnknownMethod: No such method "CaptureWindow"` from pyguitest,
and that error suggests logging out for this reason. `metadata.json`'s
`version-name` (currently `0.4.0-appid`) tells builds apart, but
`gnome-extensions info` may report the installed copy rather than the loaded
one.

To check from Python that pyguitest can reach it:

```sh
python3 -c "
import pyguitest
from pyguitest import Capability
gui = pyguitest.connect(backend='gnomeshell')
print('window capture:', gui.supports(Capability.WINDOW_CAPTURE))
"
```

`connect(backend="gnomeshell")` raises `BackendUnavailable` if the extension
is not reachable; `WINDOW_CAPTURE` is `False` when the extension is running
but cannot capture on this shell.

## Uninstall

```sh
gnome-extensions disable pyguitest-window-control@pyguitest.local
rm -rf ~/.local/share/gnome-shell/extensions/pyguitest-window-control@pyguitest.local
```

## What it exposes

A D-Bus interface on GNOME Shell's own existing connection — no separate bus
name to own, since this runs inside the shell process, which already owns
`org.gnome.Shell`:

| | |
|---|---|
| Bus name | `org.gnome.Shell` |
| Object path | `/org/gnome/Shell/Extensions/Pyguitest` |
| Interface | `org.gnome.Shell.Extensions.Pyguitest` |

`GnomeShellBackend` talks to this over ordinary PyGObject (`gi.repository.Gio`)
— the same dependency the `atspi` extra already needs, so there is nothing
new to install on the Python side once the extension itself is enabled.

## Window capture

`CaptureWindow(id, path) → (ok, error)` writes a PNG of one window and is
the only prompt-free way to screenshot on GNOME under Wayland. Every other
route is closed there: `gnome-screenshot` has not been on the allowlist for
the Shell's own screenshot interface since GNOME 42, XWayland refuses
`GetImage` on the root window, and the Screenshot portal raises a consent
dialog. This code runs *inside* gnome-shell, so it needs none of them.

It reads the window's own actor, so the image is the window's content
rather than whatever is stacked over those screen coordinates — an
occluded window still comes back whole. Two code paths get there depending
on the shell: `Meta.WindowActor.get_image` on Shell ≤50, or
`paint_to_content()` + `Shell.Screenshot.composite_to_stream()` (cropped
against `get_frame_rect()` to exclude the actor's shadow margin) on Shell
≥51, which removed `get_image`. Both are live-validated; see the file
header and `docs/validation.md`.

Two details worth knowing. The path **must be absolute**: gnome-shell's
working directory is not the caller's, and a relative path is refused
rather than written somewhere neither of them intended (pyguitest makes it
absolute for you). And `id` **0 is a capability probe**, not a window — 0
is never a real `stable_sequence`, so pyguitest calls `CaptureWindow(0, "")`
once at construction to find out whether this extension and this shell can
capture at all, and only then declares `Capability.WINDOW_CAPTURE`.

There is no whole-screen method. Capturing the whole stage needs the async
`Shell.Screenshot` API, which is a larger and less certain piece of work;
for the desktop, `connect(backend="portalcapture")` already works and
prompts only once.

### What installing this means

The Shell's
sender allowlist exists precisely so that an arbitrary application cannot
screenshot your session without asking. This extension deliberately routes
around that: while it is enabled, **anything that can talk to your session
bus can capture the contents of any window, with no prompt and no record.**

That is the same trust boundary the extension already crossed for window
control — it could move and minimize your windows before this — but pixels
are more sensitive than geometry, and a screenshot can contain passwords,
messages and documents. Enable it on a machine you use for automated
testing. Think harder about a machine you also use for anything else, and
disable it when you are not running tests:

```sh
gnome-extensions disable pyguitest-window-control@pyguitest.local
```

## Window events

`WindowEvent(change: s, id: u, title: s)` is a D-Bus signal, not a method:
the extension emits it off Meta.Display's `window-created` and Meta.Window's
`unmanaging`/`notify::title`, and pyguitest subscribes rather than polling.
`change` is `"new"`, `"close"`, or `"title"` -- the same vocabulary the
sway and niri backends already use, so `Session.wait_for_window` and
`Session.wait_window_close` work identically on GNOME with no change on
the Python side beyond `GnomeShellBackend` declaring
`Capability.WINDOW_EVENTS`.

`title` travels with every event, `"close"` included, because by the time
a close signal reaches a subscriber the window is already gone from
`ListWindows` -- there is nothing left to look its title up against by
then, unlike geometry or viewability, which can always ask fresh.

Without the extension, `wait_for_window` and `wait_window_close` on GNOME
fall back to polling every `interval` seconds.

If `window-created`/`unmanaging`/`notify::title` ever fail to connect on
some future Mutter (`startWatching()` in `extension.js`), the extension
logs the error and keeps exporting everything else -- window listing,
move, resize, capture -- rather than refusing to load entirely. There is
currently no way for pyguitest to detect that from the Python side and
withdraw `Capability.WINDOW_EVENTS`; check the shell's own log if events
stop arriving but everything else still works.
