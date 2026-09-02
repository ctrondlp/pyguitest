# pyguitest-window-control

A GNOME Shell extension with no UI of its own. It exists solely to give
`pyguitest.backends.gnomeshell.GnomeShellBackend` a way to list, move,
resize, activate and minimize windows on GNOME — the one thing nothing else
in pyguitest can do on a pure Wayland session (no XWayland at all), since
Mutter implements no foreign-toplevel protocol.

It also captures a window's pixels, which on GNOME under Wayland nothing
else can do without a consent prompt — see **Window capture** below.

**Status: window control validated live on GNOME Shell 50.4** with
`scripts/validate-gnome-extension.sh` (8 of 8 checks). That first live run
found two real bugs written from the headers alone, both since fixed.
**Window capture validated live on GNOME Shell 50.4** (2026-08-29): a real
window was captured to a PNG through `Meta.WindowActor.get_image`, on a
pure Wayland session where every other capture route is closed.

**Window capture also validated live on GNOME Shell 51.beta** (2026-09-02):
Mutter 51 removed `get_image` with no drop-in replacement, which had made
capture silently unavailable on any Shell ≥51 (the id-0 probe correctly
reported it unsupported, but that is still "no capture"). The extension now
uses `paint_to_content()` + `Shell.Screenshot.composite_to_stream()` on
those shells instead — see **Window capture** below for the version fork,
and `docs/validation.md`'s GNOME Shell 51.beta section for the shadow-margin
crop bug that first live run found and fixed.

**Window events validated live on GNOME Shell 50.4** (2026-08-30): all
three -- `new`, `title`, `close` -- confirmed over the real `WindowEvent`
D-Bus signal. `scripts/validate-gnome-extension.sh` spawns and kills a
throwaway `gnome-text-editor`/`gedit`/`gnome-calculator` at step 6
specifically to exercise `new`/`close` deterministically rather than
depending on a person's timing, then asserts both arrived; a run doing
exactly that passed all 9 checks clean, `new` included. Three real bugs
surfaced getting here, all in the *script itself* rather than the
extension or the backend, each fixed in turn: two stale-window crashes in
the read-only checks after a window closed during the listen, and a race
where killing the launched process happened only after that first
listen's D-Bus subscription had already been torn down -- a `close`
firing in that gap was lost with nothing to blame but timing. Fixed by
using one continuous subscription across spawn, kill, and close instead
of two separate ones. See **Window events** below for the shape of the
signal.

If the extension doesn't load, `journalctl -f /usr/bin/gnome-shell` (or
`looking-glass`, `Alt+F2` then `lg`) is where GNOME Shell logs extension
errors.

## Install

```sh
UUID=pyguitest-window-control@pyguitest.local
mkdir -p ~/.local/share/gnome-shell/extensions/$UUID
cp gnome-shell-extension/$UUID/* ~/.local/share/gnome-shell/extensions/$UUID/
```

On X11, restart the shell to load it: `Alt+F2`, type `r`, Enter. **On
Wayland there is no equivalent — log out and back in.**

Then enable it:

```sh
gnome-extensions enable pyguitest-window-control@pyguitest.local
```

Confirm it's running:

```sh
gnome-extensions info pyguitest-window-control@pyguitest.local
```

should report `State: ACTIVE`. If it reports `ERROR` instead, that's the
debugging round trip mentioned above — check the log locations above for
what GNOME Shell's JS engine rejected.

**`ACTIVE` does not mean your copy is the one running.** On Wayland a
shell keeps serving the code it loaded at login, so an extension you have
just overwritten still reports `ACTIVE` while behaving like the old one.
That is not a failure state, just an un-restarted session — and it
presents as `UnknownMethod: No such method "CaptureWindow"` from
pyguitest, which is why that error tells you to log out and back in.

`metadata.json` carries a `version-name` (`0.3.0-events`) so you can at
least tell the builds apart. Be careful how much you read into it, though:
it is not confirmed whether `gnome-extensions info` reports metadata the
shell cached at load time or re-reads it from disk. If it is the latter,
the version tells you what is *installed*, not what is *loaded*, and the
two differ for exactly as long as it takes you to log out.

The one unambiguous check is to call the method. `pyguitest` does that at
construction and says which of the two possible causes applies:

```sh
python3 -c "
import pyguitest
gui = pyguitest.connect(backend='gnomeshell')
print(gui.backend._can_capture, '|', gui.backend._capture_note)
"
```

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

Worth being explicit, because it is the point of the feature. The Shell's
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

This closes GNOME's biggest remaining gap against sway/niri: without it,
`wait_for_window`/`wait_window_close` on GNOME fell back to
`Session`'s polling loop (a fixed interval, no compositor push) even
though window control here was otherwise fully event-capable elsewhere in
this package.

If `window-created`/`unmanaging`/`notify::title` ever fail to connect on
some future Mutter (`startWatching()` in `extension.js`), the extension
logs the error and keeps exporting everything else -- window listing,
move, resize, capture -- rather than refusing to load entirely. There is
currently no way for pyguitest to detect that from the Python side and
withdraw `Capability.WINDOW_EVENTS`; check the shell's own log if events
stop arriving but everything else still works.
