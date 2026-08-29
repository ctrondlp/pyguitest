# pyguitest

Cross-platform GUI automation for Python. Successor to
[X11::GUITest](https://metacpan.org/pod/X11::GUITest).

**Status:** every capability implemented across all backends, covering
**every X11::GUITest export**.

Run live end-to-end on GNOME Shell 50.4: `eiinput` (opt-in); `gnomeshell`,
including its per-window capture through the Shell extension, which is the
only prompt-free way to screenshot on that desktop; `portalcapture`
(opt-in), which captured the whole screen correctly; and `X11Backend`'s
per-window capture under XWayland, which produced a correct image of a real
window through this package's own PNG encoder. `portal` — the input half —
has not been exercised past its consent dialog.

Those runs are the reason several things below are stated as measurements
rather than expectations. They also found bugs no unit test could have:
backends declaring capabilities they could not deliver, a window handle
passed to a backend that could not interpret it, a screenshot tool selected
on a session where it can only hang. Capture had to meet a real compositor
before it was trustworthy.

The distribution is the point. On Wayland, every achievable capability is
served, and the tier-6 ones cannot exist. But the tier scale is a *Wayland
ceiling*, not an absolute one: the X11 backend serves those too, so an X11 session
gets a strictly larger capability set through the same API. Discovering which one
you are on is what `supports()` is for.

Every python-xlib call the X11 backend makes has been checked against the
installed library — names, signatures and return shapes — and its capture
path has now run against a live X server (XWayland on GNOME 50), producing
a correct image of a real window. Its input and window-control calls have
not been exercised live, and neither have the compositor IPC backends
(sway/Hyprland/niri/KWin) or UinputBackend — those tests replay recorded
output and stand-ins, on a sandbox where none of those are available to
test against.

`gnomeshell` needs a GNOME Shell extension
([gnome-shell-extension/](gnome-shell-extension/README.md)) installed by
hand, so it is opt-in and affects no other backend if it is never
installed. It has been loaded by a real gnome-shell: window control passed
8 of 8 checks on GNOME Shell 50.4, and per-window capture produced a real
PNG there. That first live run also found two bugs the header-derived code
had carried from the start, which is why nothing here is claimed from
headers alone any more.

`portal` (`connect(backend="portal")` -- deliberately not part of automatic
detection at all; see below) is the furthest out: it talks to
`org.freedesktop.portal.RemoteDesktop` for keyboard and pointer button/scroll
injection, with every method transcribed from the actual portal XML rather
than assumed. Its CreateSession/SelectDevices/Start negotiation has been
run against a real xdg-desktop-portal (1.22.1) and completes; the
keyboard, pointer and scroll methods past that point have not. `Start()`
raises an interactive consent dialog that blocks until a human clicks
Allow, so every step beyond it needs a person at a real desktop.

`eiinput` (`connect(backend="eiinput")`, also opt-in) covers what `portal`
deliberately cannot: absolute pointer motion, via
[libei](https://libinput.pages.freedesktop.org/libei/) rather than the
portal's own D-Bus methods, using
[python-libei](https://github.com/ctrondlp/python-libei) for the bindings.
Unlike `portal`'s `NotifyPointerMotionAbsolute`, this needs no PipeWire
stream and no ScreenCast consent — verified live (2026-08-26, GNOME 50) by
enumerating every device the seat offers, with and without a ScreenCast
source selected: identical either way, absolute-pointer region included.

The trap worth knowing about, since it cost a long debugging session: **one
seat resumes two pointer devices**, `virtual pointer` (relative) and
`shared virtual absolute pointer` (absolute, with a region), as separate
`DEVICE_RESUMED` events — the relative one first, every time observed.
Taking the first device to resume, which is the obvious implementation,
yields a device whose `pointer_motion_absolute()` logs a libei-internal
warning and silently does nothing: no exception, no movement. That
presented as maddening flakiness — byte-identical code working, then not —
and was initially misdiagnosed as a missing ScreenCast/PipeWire linkage,
with a whole combined-session negotiation built on the misreading before a
`busctl --user monitor` comparison caught the reference script failing the
same way. `_wait_for_device` now waits for the device it actually needs.

Injection is verified live: pointing at GNOME's Activities button and
clicking it opens the overview, reliably, and the cursor moves.

**If injected input appears to do nothing, check your environment before
suspecting the backend.** On a VirtualBox guest, *Mouse Integration* slaves
the guest pointer to the host's mouse via an absolute "VirtualBox USB
Tablet" device, continuously overriding anything injected inside the guest —
libei, uinput and ydotool alike. Symptoms are confusing: clicks land and
hover fires, but the cursor never visibly moves. Turn it off with
**Input → Mouse Integration** (Host+I). Useful checks when input seems
dead: `sudo libinput debug-events --device /dev/input/eventN` (do the events
reach libinput?) and `loginctl seat-status seat0` (is the device on your
seat?).

`eiinput` also types, and is the only backend here that is **keymap-safe**
by construction. `Device.keyboard_key()` takes a raw Linux keycode and the
compositor interprets it through the very keymap it handed the client, so
`xkb.py` compiles that keymap and looks the answer up rather than guessing:
on a French AZERTY layout `type_text("a")` presses the physical Q key,
where `uinput`'s hardcoded US table types "q". Typing is refused outright
if no keymap could be read (guessing is exactly the failure being avoided),
and a character the active layout cannot produce raises rather than
pressing something approximate.

Installing what this backend needs: on Fedora, `sudo dnf install libei
libeis` for the native libraries, then the bindings, which are on PyPI and
have an extra of their own:

```sh
pip install '.[eiinput]'     # or: pip install python-libei
```

The two halves are separate on purpose -- `python-libei` is pure ctypes and
its wheel carries no `.so`, so pip installs the bindings and the distribution
supplies the library they `dlopen`. `liboeffis` is not needed: `eiinput`
negotiates the RemoteDesktop session itself over D-Bus rather than through
that library (see `backends/eiinput.py`'s module docstring for why); `libeis`
is only needed to run `tests/test_eiinput_libei.py`, not at runtime.

#### Avoiding repeat consent dialogs

There is no way to skip the dialog the *first* time -- that first click is
the portal's actual security boundary, not something pyguitest sits in
front of. What the protocol *does* support is not asking again:
`SelectDevices` takes a `persist_mode` option (`2` = "permissions persist
until explicitly revoked"), and once a session is approved under that mode,
`Start()`'s reply carries a `restore_token`. Passing that token back into a
later `SelectDevices` call lets the portal recognize the previously-approved
session and skip straight to `Start()` -- the same mechanism every
screen-sharing and remote-desktop app uses to avoid re-prompting on every
launch, not a bypass of it. The token is single-use: each successful restore
returns a *new* token in its own `Start()` reply, which the caller must save
in place of the old one.

This is implemented, on both `portal` and `eiinput`, and is opt-in: a plain
`connect(backend="portal")` sends neither option and prompts every time, so
nothing starts persisting behind your back. Ask for it explicitly, and save
the token that comes back:

```python
gui = connect(backend="portal", backend_options={
    "persist_mode": 2,        # PERSIST_UNTIL_REVOKED
    "restore_token": saved,   # None on the first run
})
save_it_yourself(gui.backend.restore_token)   # single-use: replaces `saved`
```

pyguitest deliberately never writes the token anywhere itself -- see the
caution below for why that is your decision rather than the library's.

**Caution:** a `restore_token` is a standing grant of keyboard and pointer
injection to whatever presents it -- treat it as a credential, not a
convenience flag. It's scoped to the requesting app and kept in the
portal's own permission store (`$XDG_DATA_HOME/flatpak/db`, per the
[xdg-desktop-portal wiki](https://github.com/flatpak/xdg-desktop-portal/wiki/The-Permission-Store)),
where the user can revoke it independently of your code (KDE Plasma 6.5+
has a dedicated Application Permissions settings page for this; on other
desktops it's the portal-permission-store contents or, for a Flatpak-packaged
caller, `flatpak permission-reset`). Don't request `persist_mode=2` on a
shared or multi-tenant machine without telling whoever clicks Allow what
they're persisting, and don't build automation that clicks the dialog *for*
the user -- that would defeat the reason it exists. `persist_mode=1`
(app-lifetime only) or `0` (default, no persistence, one dialog per run) are
the lower-privilege choices when repeat-prompt annoyance isn't worth the
standing grant.

For CI or other headless runs where no human can click anything at all,
`xdg-desktop-portal` ships fake portal backends for its own test suite that
answer `Start()` with no UI -- appropriate only inside a sandbox you
control, never pointed at a real user's session. That's the same boundary
python-libei's `eis.Eis.create_for_fd()` already draws for testing without
a portal at all.

`tests/test_portal_dbusmock.py` does exactly this for `PortalBackend`,
using [python-dbusmock](https://github.com/martinpitt/python-dbusmock) the
way `xdg-desktop-portal`'s own test suite does: it starts a private
`dbus-daemon` in a throwaway temp directory (never the ambient session
bus -- `$DBUS_SESSION_BUS_ADDRESS` is saved before the test class runs and
restored after, regardless of pass/fail), registers a fake
`org.freedesktop.portal.Desktop` service on *that* bus which answers
`CreateSession`/`SelectDevices`/`Start` the way an already-approved real
portal would, and drives `PortalBackend` against it over a genuine Gio
connection -- real D-Bus method calls and a real `Request`/`Response`
signal round trip, not the pure-Python stand-ins the rest of
`test_portal.py` uses. Verified passing (both the happy path and a
declined-consent path) against a real `dbus-daemon` and real PyGObject.

This proves the D-Bus wire plumbing between `PortalBackend` and a
portal-shaped service is correct. It does **not** prove anything about the
real `xdg-desktop-portal` daemon, GNOME's/KDE's actual backend
implementation, or the dialog itself -- those still fall under "has never
been run against a real xdg-desktop-portal" above.

Everything is unit-tested. The AT-SPI backend and the CLI-tool
input/capture backends *have* now run against a live GNOME/XWayland desktop,
and it found what the unit tests could not: three real bugs where a backend
leaked a bare exception from a dependency instead of a typed one (dogtail's
ponytail helper, twice, and a tool's stdout being dropped from its own error
message). All three are fixed and covered by regression tests. Closing the gap
for the *other* backends — actually running them against a live
sway/Hyprland/niri/KDE session — is next.

## Why the API is not a port

`docs/wayland-audit.html` classifies all 50 X11::GUITest 0.29 exports by what it
would cost to implement each on Wayland. The distribution is the finding:

| Tier | Count | Meaning |
|------|-------|---------|
| T1 Portable   | 9  | No display server involved; ports unchanged |
| T2 Direct     | 4  | Core Wayland protocol gives a real equivalent |
| T3 Compositor | 19 | Needs a separate backend per desktop |
| T4 Privileged | 8  | Input injection; consent or device access |
| T5 Rework     | 4  | Goal survives via AT-SPI; the model does not |
| T6 No path    | 6  | Deliberately prevented; dropped from the API |

Thirteen of fifty carry over unchanged. The largest block is not "impossible"
but "possible once per desktop" — window management is where a portable Wayland
implementation actually fails, and GNOME is the worst case because Mutter
implements neither foreign-toplevel protocol.

## Design decisions that follow

- **Capability negotiation is public API.** With 19 functions varying by
  compositor, X11::GUITest's convention of returning zero on failure is
  untestable — it cannot distinguish "the click missed" from "this desktop
  cannot click". Every failure here is a typed exception, and callers can ask
  first.
- **Speak protocols directly; adapt tools only where no protocol exists.** sway,
  Hyprland and niri window control talks to their unix sockets using nothing but
  the stdlib, so no tool need be installed. dogtail covers elements, python-xlib
  covers X11, python-evdev covers in-process uinput; `kdotool` and the
  screenshot tools stay CLI adapters because they wrap genuinely hard work.
  See [ADR 001](docs/adr-001-dependencies.md) and
  [ADR 002](docs/adr-002-transports.md).
- **Input tools are ranked by keymap safety.** `wdotool` and `wtype` let the
  client supply a keymap; `ydotool` injects scancodes *below* the compositor, so
  `type_text("Hello")` produces different characters on an AZERTY session and no
  protocol reports the active layout. That ranking is encoded in `tools.py`, and
  detection warns when only keymap-unsafe tools are present.
- **AT-SPI leads.** It answers what the window-tree walk was really used for,
  needs neither geometry nor injection permission, and behaves identically under
  X11 and Wayland — the one layer needing no backend matrix.
- **The X11 backend is a peer, not a legacy path.** It is the only route to
  FreeBSD and Solaris — libei, uinput and the compositor IPC tools are all Linux
  interfaces — and the only backend serving tier-6 capabilities at all.
- **Compositor IPC fills the geometry hole.** sway, Hyprland and niri report
  window rectangles, which no Wayland protocol exposes — and once every rectangle is
  known, hit-testing a coordinate is arithmetic rather than a compositor query.
- **No hard dependencies.** Every mechanism is probed at runtime and degrades to
  an unsupported capability rather than an import error. Extras are per-backend,
  so a real install is the package plus one extra.

## Dependencies

**None are required.** `pip install pyguitest` pulls nothing, and every
third-party import in the source is guarded, so the package imports and runs
with no other packages present — you get the tier-1 capabilities and an honest
report of what is missing.

Everything beyond that is opt-in, because no single set of packages spans GNOME,
KDE, wlroots, X11 and the BSDs.

### Python packages (pip)

| Extra | Package | Unlocks |
|---|---|---|
| `atspi` | `dogtail` | Elements — buttons, text boxes, dropdowns. Also windows on GNOME |
| `x11` | `python-xlib` | The X11 backend: everything, including tier-6, and the only route to the BSDs |
| `uinput` | `evdev` | In-process input injection |
| `eiinput` | `python-libei` | The `eiinput` backend: keymap-safe input over libei. Needs `libei` from the distro too |
| `dev` | `pytest`, `ruff`, `mypy` | Tests, lint and format, type checking |

```sh
pip install '.[atspi]'
```

`X11Backend`'s `geometry()`, and by extension anything that reads a position
back after `move_window`, may report a window's location wildly wrong under
GNOME/Mutter specifically (XWayland) — confirmed live: `move_window` visibly
moves the window to the right place, but a `geometry()` call right after
reads back a position nowhere near it. This looks like Mutter's XWayland
integration not keeping the window's decoration-frame position, as reported
over X11, synced to where it actually renders the window — the same
`XTranslateCoordinates` mechanism `xdotool`/`wmctrl` use has no other request
to fall back on if that is what's happening. Working hypothesis from one
diagnostic session on one machine, not an independently confirmed root cause
or a survey of other window managers — worth being skeptical of `geometry()`
results on GNOME's XWayland until someone reproduces or debunks this.

### Distribution packages (pip cannot supply these)

The `atspi` extra is **not self-sufficient**: dogtail declares no dependencies,
and PyGObject does not build from source cleanly. On Fedora:

```sh
sudo dnf install python3-gobject python3-pyatspi at-spi2-core
```

`pyatspi` is used by the AT-SPI backend but appears in no extra, for exactly
this reason.

### External tools (never installed by pip)

Discovered on `PATH` at runtime. Absence degrades a capability; it never raises.

| Group | Tools |
|---|---|
| Input | `wdotool`, `wtype` *(wlroots only)*, `ydotool` *(keymap-unsafe)*, `xdotool` *(X11 only)* |
| Capture | `grim`, `gnome-screenshot` *(real X11 only)*, `spectacle`, `import` *(X11 only, and real X11 only)* |
| Windows | `swaymsg`, `hyprctl`, `niri msg`, `kdotool` |
| Image search | `compare` (ImageMagick) |

**sway, Hyprland and niri need none of these** — window control speaks their
unix sockets using only the standard library.

*Real X11 only* is stricter than *X11 only* and is about capture, not about
which clients a tool can see. These tools screenshot by reading the X root
window, and XWayland refuses that outright — a 1×1 read fails exactly as a
full-screen one does. So they are not selected on a Wayland session at all,
XWayland included; being installed there does not make them usable, and
`gnome-screenshot` in particular hangs for the full timeout before failing.

**On GNOME under Wayland, per-window capture is prompt-free if you install
the [Shell extension](gnome-shell-extension/README.md).** It runs inside
gnome-shell, so it sidesteps the sender allowlist that closes every other
route, and it reads the window's own actor — an occluded window still comes
back whole. It has no whole-screen method. Read that extension's own README
before enabling it: while it is on, anything that can reach your session bus
can screenshot any window with no prompt.

**For the whole desktop, the Screenshot portal is the capture path** —
verified end to end on GNOME Shell 50.4. The other two do not work there
and cannot be made to: `gnome-screenshot` cannot reach the Shell's own
screenshot interface (restricted to an allowlist of senders since GNOME
42), falls back to X11, captures nothing, and hangs; and XWayland refuses
`GetImage` on the root window outright — a 1×1 request is rejected as
firmly as a full-screen one, under every pixmap format and plane mask.

```python
gui = pyguitest.connect(backend="portalcapture")
gui.screenshot("shot.png")
```

The first call raises a consent dialog. The grant then persists — the
desktop's permission store records it, and later calls are silent, so an
unattended suite prompts once on a machine and never again. Inspect or
revoke it with `flatpak permissions screenshot`. Note this is the
*desktop* remembering, not something pyguitest can ask for: unlike
RemoteDesktop and ScreenCast, the Screenshot portal has no `persist_mode`
or `restore_token` in its interface at all.

**X11 screen capture is withdrawn under XWayland**, and only there. An X
connection inside a Wayland session works perfectly well for input, window
control and everything else — but native Wayland surfaces are never
composited into the X root window, so a root-window grab cannot return the
desktop. On GNOME 50 it errors outright; the more dangerous outcome would
have been succeeding, since an empty X root is a perfectly valid image of
entirely the wrong thing. This is the same trap the tool registry already
encodes as *X11 only*. Per-window capture is unaffected: an XWayland-backed
X11 client has its own drawable with its own content. On a true X11 session
nothing changes.

**A tool that is installed but does not work no longer takes capture down
with it.** This is a recurring condition, not an accident: `import` is
broken on Fedora 43 (below), and `gnome-screenshot` cannot capture at all
on GNOME 42+ Wayland — it is not on the allowlist for the Shell's own
screenshot interface, falls back to X11, which sees nothing on a Wayland
session, and then hangs. When the selected backend fails, the next capable
one is tried, the broken one is skipped for the rest of the session rather
than costing another timeout, and a `CaptureFallbackWarning` says which
tool failed and why — silently rescuing the call would hide a real problem
that `pyguitest doctor` cannot see, since the tool *is* installed.

ImageMagick also does the cropping whenever a region is asked of a tool that
has no exact-rectangle mode, so `gnome-screenshot` or `spectacle` alone gives
you whole-screen capture and `magick`/`convert` alongside either gives you
regions and per-window capture too. Under X11 with `python-xlib` installed
neither is needed: `X11Backend` captures and encodes the PNG itself.

`import` (ImageMagick) is the fallback capture tool on GNOME/XWayland, and on
Fedora 43 it is currently broken outright — `import -window root` fails with
`import: missing an image filename` regardless of arguments, an
[upstream ImageMagick bug](https://github.com/ImageMagick/ImageMagick/issues/8459),
not a pyguitest one. `sudo dnf install gnome-screenshot` sidesteps it entirely:
it outranks `import` in tool selection, so once it is on `PATH` pyguitest
picks it automatically.

### In practice

| Desktop | What you install |
|---|---|
| sway / Hyprland / niri | `pip install .` — nothing else |
| GNOME | `pip install '.[atspi]'` + three dnf packages |
| GNOME, pure Wayland (no XWayland) | as GNOME, plus the [pyguitest-window-control extension](gnome-shell-extension/README.md) for window placement/minimize |
| KDE | as GNOME, plus `kdotool` for windows |
| X11 | `pip install '.[x11]'` |
| Any portal-supporting desktop, deliberately | `pip install '.[atspi]'` for PyGObject, then `connect(backend="portal")` and click Allow (once, if you opt into `persist_mode`) |
| Keymap-safe input over libei, deliberately | `pip install '.[atspi,eiinput]'` + `sudo dnf install libei`, then `connect(backend="eiinput")` |
| Screenshots with no tool installed at all, deliberately | `pip install '.[atspi]'` for PyGObject, then `connect(backend="portalcapture")` — the only capture path that works inside a Flatpak sandbox |

`pyguitest doctor` reports which of these you have and names what is missing
-- except the GNOME Shell extension and portal rows, neither of which it can
detect without making a D-Bus call session detection deliberately avoids
(see `session.detect()`'s own docstring); both are opt-in, not autodetected,
and `portal` doubly so -- see [Backend registry](docs/structure.md#backend-registry)
for why `connect()` never reaches it on its own.

## Install

**You do not need `-e`.** That flag is for developing *this package*; it links
to the source tree instead of installing a copy. End users install normally.

Not published yet — `pyproject.toml` carries the `Private :: Do Not Upload`
classifier, which PyPI rejects — so for now installation is from a checkout.

```sh
git clone https://github.com/ctrondlp/pyguitest.git
cd pyguitest
```

### For using pyguitest

```sh
pip install .                # core; no dependencies
pip install '.[atspi]'       # + element automation
```

Once published, that becomes `pip install pyguitest` and
`pip install 'pyguitest[atspi]'`.

Then ask what else your desktop needs:

```sh
pyguitest doctor
```

It detects your distribution and prints the exact commands. The reason it has
to: pip cannot supply the GNOME Python stack (PyGObject does not build from
source cleanly) or the desktop's screenshot and input tools. On Fedora, for
element automation:

```sh
sudo dnf install python3-gobject python3-pyatspi at-spi2-core
```

A virtualenv needs `--system-site-packages` to see those.

### Input: pointer and keyboard

Injection needs one more step than the rest, because `/dev/uinput` is
root-only by default. On Fedora:

```sh
sudo dnf install python3-evdev      # pyguitest drives uinput in-process
sudo usermod -aG input $USER        # put yourself in the 'input' group
```

**On Fedora that is not sufficient on its own**, and the failure is silent:
`/dev/uinput` ships as `root:root` mode `0600`, so it grants the `input`
group nothing and joining that group changes nothing. Confirm with
`ls -l /dev/uinput` — if it is not `root input` / `crw-rw----`, add a udev
rule so the node is group-accessible:

```sh
echo 'KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"' \
    | sudo tee /etc/udev/rules.d/99-uinput.rules
sudo udevadm control --reload-rules
```

`static_node=uinput` matters: `/dev/uinput` is created when the module
loads rather than by a hotplug event, so permissions have to be applied at
module-load time. The rule takes effect on the next boot; to fix the
running node without rebooting, `sudo chgrp input /dev/uinput && sudo chmod
g+rw /dev/uinput` (reloading the module usually fails with "Module uinput is
in use").

Group membership is established at **login** and inherited by every child
process, so opening a new terminal is *not* enough — it inherits the old set.
Either:

```sh
newgrp input        # this shell only, takes effect immediately
```

or log out and back in, which fixes the whole session.

Verify with `id -nG` — **no username**. `id -nG $USER` reads the group database
and will show `input` the moment `usermod` runs, whether or not your current
processes have it, which is misleading. The check that actually matters is
whether Python can open the device:

```sh
python3 -c "import os; print(os.access('/dev/uinput', os.W_OK))"
```

`pyguitest doctor` performs exactly that test.

`ydotool` is the alternative and also packaged, but it needs a `ydotoold`
daemon running; python3-evdev needs no daemon.

Fedora's `ydotool` package ships `ydotool.service`, which runs `ydotoold` as
**root** with no arguments. ydotoold defaults to a fixed socket at
`/tmp/.ydotool_socket` (deliberately outside any one user's runtime dir, so a
root daemon and non-root clients *can* share it) — but it creates that socket
`root:root` mode `0600`, so nothing but root can actually open it yet:

```sh
sudo systemctl enable --now ydotool   # starts the daemon; socket is still root-only
ls -la /tmp/.ydotool_socket           # confirm: srw-------. 1 root root
```

Running the client under `sudo` "works" but is not the fix — it just runs
*pyguitest itself* as root, which loses `DISPLAY`/`WAYLAND_DISPLAY`/
`DBUS_SESSION_BUS_ADDRESS`/`XDG_RUNTIME_DIR` (your desktop session lives on
those) unless painstakingly reconstructed with `sudo -E`. The actual fix is
to have the daemon create the socket group-owned by `input` and group-writable,
the same group already used for `/dev/uinput` above:

```sh
sudo systemctl edit ydotool
```

Paste this and save (empties the packaged `ExecStart=` first, since systemd
appends by default rather than replacing it):

```ini
[Service]
ExecStart=
ExecStart=/bin/sh -c 'exec /usr/bin/ydotoold --socket-own=0:$(getent group input | cut -d: -f3) --socket-perm=0660'
```

```sh
sudo systemctl daemon-reload
sudo systemctl restart ydotool
ls -la /tmp/.ydotool_socket           # now: srw-rw----. 1 root input
```

One more mismatch: the `ydotool` *client* does not default to `/tmp/.ydotool_socket`
at all — it looks in `$XDG_RUNTIME_DIR/.ydotool_socket`
(`/run/user/<uid>/.ydotool_socket`), which is a different default from the
daemon's, on the ydotool build Fedora ships. Point the client at the socket
the daemon above actually created:

```sh
echo 'export YDOTOOL_SOCKET=/tmp/.ydotool_socket' >> ~/.bashrc
```

(open a new shell, or `source ~/.bashrc`, for it to take effect). With both
of those in place — group membership on the socket, and the client pointed at
where the daemon actually listens — `ydotool` works with no `sudo`.

Both go through `/dev/uinput`, which injects *below* the compositor, so the
session's active keyboard layout is applied to whatever you type. On a US
layout that is invisible; on AZERTY or Dvorak, `type_text("Hello")` produces
different characters. `type_text` warns, and
`type_text(..., allow_keymap_unsafe=False)` refuses outright — use that in any
suite that asserts on typed content.

The only keymap-safe option on GNOME is
[wdotool](https://github.com/cushycush/wdotool), which speaks libei through the
RemoteDesktop portal and needs no group membership. No distribution packages it
yet, so it has to be built from source. On sway and Hyprland, `wtype` is
keymap-safe and packaged; it does **not** work on GNOME or KDE, whose
compositors lack the protocol it needs.

`pyguitest doctor` prints whichever of these apply to your machine.

### For working on pyguitest

```sh
pip install -e '.[dev]'      # editable, plus ruff and mypy
pre-commit install
```

## Usage

```python
import pyguitest

gui = pyguitest.connect()

# Widgets by what they are and what they are called -- the recommended way.
gui.button("OK").click()
gui.text_field("Name").set_text("Ada Lovelace")
gui.dropdown("Country").choose("Norway")

# Windows by title regex.
window = gui.find_window("Editor")

# Coordinates and keys, when you need them.
gui.move_mouse(500, 300)
gui.click()
gui.type_text("Hello")
gui.send_keys("^(a)^(c)")  # Ctrl-A, Ctrl-C
```

Matching on role and name survives the application being moved or resized,
unlike clicking at `(842, 612)`. Ask before depending on anything that varies
by desktop:

```python
from pyguitest import Capability

if gui.supports(Capability.WINDOW_GEOMETRY):
    x, y, w, h = gui.geometry(window)
```

`connect()` never raises on a limited desktop — a session with few capabilities
is the normal case, and `supports()` is how you find out.

A session is usually several backends at once: elements from AT-SPI, injection
from a CLI adapter, capture from another. `CompositeBackend` merges their
capabilities and routes each call to whichever member provides it, so callers
see one object. `backend.providers()` shows the routing.

### Screenshots

```python
gui.screenshot("desktop.png")                       # the whole desktop
gui.screenshot("editor.png", window=window)         # one window
gui.screenshot("corner.png", region=(0, 0, 400, 300))
```

`region` is `(x, y, width, height)` in screen coordinates — the same tuple
`gui.geometry(window)` returns, on every backend. You never write grim's
`"x,y WxH"` or ImageMagick's `"WxH+X+Y"`; whichever tool the session picked
gets its own syntax built for it, and the two tools with no exact-rectangle
mode (`gnome-screenshot`, `spectacle`) capture the screen and crop the
rectangle out rather than opening a selector that would hang an unattended
run.

`window` is served two ways, and the difference shows in the image. Under
X11 the window's own pixels are read, so anything stacked on top of it is
absent. Everywhere else the window's rectangle is looked up through
`WINDOW_GEOMETRY` and cut out of a full-screen shot, which does include
whatever is covering it. `gui.supports(Capability.WINDOW_CAPTURE)` is how
you tell which you are getting.

**Automatically, when a test fails.** Nothing captures on its own — a
screenshot has to be taken while the failure is still propagating, because
by the time an `except:` block runs the application under test is usually
gone. Wrap the part you want documented:

```python
with gui.capture_on_failure("artifacts"):
    gui.button("Save").click()
    assert gui.element(name="Saved")
```

Nothing is written when the block succeeds. On failure the image lands in
`artifacts/` (or `$PYGUITEST_SCREENSHOT_DIR`, or the temporary directory),
its path is attached to the exception as `.screenshot`, and the original
exception is re-raised untouched, so the test runner still reports the real
failure. A screenshot that itself fails is recorded on the exception as
`.screenshot_error` and swallowed — it never replaces the failure it was
trying to document.

## Examples

Five runnable scripts in [examples/](examples/), each degrading with an
explanation when the desktop cannot do what it asks:

```sh
python3 examples/01_what_can_i_do.py     # start here
python3 examples/03_widgets.py           # buttons, text boxes, dropdowns
```

## Tools

```sh
pyguitest                     # what this desktop can actually do
pyguitest doctor              # what to install to unlock more
pyguitest migrate script.pl   # what porting a Perl script involves
```

All three also work as `python -m pyguitest …` without installing.

The migration scanner reports the tier of every X11::GUITest call in a source
file and exits non-zero if any call has no Wayland path, so a port can be gated
in CI.

## Layout

Full architecture notes are in [docs/structure.md](docs/structure.md).

```
src/pyguitest/
  capabilities.py   Tier scale and the capabilities
  compat.py         The X11::GUITest migration table, as data
  session.py        Runtime environment and mechanism detection
  ipc.py            sway, Hyprland and niri socket protocols (stdlib only)
  tools.py          External CLI adapters, ranked by keymap safety
  xkb.py            Keymap lookup: which key types which character
  png.py            Writing a PNG from raw pixels, stdlib only
  roles.py          Accessible role names (button, entry, ...)
  hints.py          What to install, per distribution
  errors.py         Typed failures
  __main__.py       The `pyguitest` command: report, doctor, migrate
  backends/
    base.py         The backend interface
    composite.py    Merges backends; routes each call to its provider
    windows.py      Window control and events; sway/Hyprland/niri/KDE
    gnomeshell.py   Window control and prompt-free per-window capture on
                    Mutter, via the shell extension
    atspi.py        Element automation, adapting dogtail
    input.py        Injection, adapting wdotool/wtype/ydotool/xdotool
    uinput.py       In-process injection via python-evdev
    portal.py       Keyboard and buttons/scroll, via the RemoteDesktop portal
    eiinput.py      Absolute pointer via libei, over the same portal
    capture.py      Screenshots, adapting grim/gnome-screenshot/spectacle
    portalcapture.py Screenshots via the Screenshot portal; needs no tool
    portalrequest.py The portal request/response dance, shared by both
    crop.py         Cutting a rectangle out, via ImageMagick
    imagesearch.py  Locating a template image, via ImageMagick's compare
    x11.py          X11 via python-xlib; the only tier-6 and BSD path.
                    Captures a window's own drawable, so nothing stacked
                    over it appears -- but not the screen, under XWayland
    null.py         Tier-1 only; for CI and undetectable sessions
docs/
  wayland-audit.html
  adr-001-dependencies.md
  adr-002-transports.md
  structure.md
```

## Development

```sh
PYTHONPATH=src python3 -m unittest discover -s tests   # unit tests, no deps
```

Really no deps: the suite passes on a machine with nothing installed and no
capture or input tool on `PATH`. That is not a convenience, it is the claim
under test — every display-server mechanism is probed at runtime and stands
in as a fake here, so a stray unconditional `import` in a module that must
stay importable without the optional extras shows up as a failure.

[CI](.github/workflows/ci.yml) runs that suite on Python 3.10 through 3.14,
plus `ruff check`, `ruff format --check` and `mypy` on the 3.10 floor.
Almost nothing in this package can be exercised against the real thing
automatically — there is no compositor, no session bus, no X server, no
consent dialog anyone can click — so the tests drive stand-ins for
python-xlib, Gio and the portal, and running them everywhere is the cheapest
guard against those stand-ins drifting from what they imitate. The one
exception is the portal job, which installs `python3-dbusmock` and
`dbus-daemon` and negotiates against a real private session bus; it fails if
those tests *skip*, since a green job that proved nothing is worse than a
red one.

More live in `tests/test_portal_dbusmock.py` and count separately -- see
[Avoiding repeat consent dialogs](#avoiding-repeat-consent-dialogs) for what
they verify. `pip install '.[dev]'` alone is *not* enough for them:
`dbus-python` needs compiling and `dbus-daemon` is a binary, so both come
from the distribution. On Fedora:

```sh
sudo dnf install python3-dbusmock python3-dbus dbus-daemon
PYTHONPATH=src python3 -m unittest discover -s tests \
    -p test_portal_dbusmock.py -v
```

Use `discover -p`, not `unittest tests.test_portal_dbusmock`: there is no
`tests/__init__.py`, and while Python 3.13 resolves that dotted name as a
namespace package, 3.14 does not -- it fails with `ModuleNotFoundError`,
which reads like a missing file rather than a bad invocation.

**Check they actually ran.** These skip themselves rather than failing when
a prerequisite is missing, and a skipped run still prints `OK` -- so an
incomplete install looks exactly like a passing one. `-v` shows `... ok`
per test, and the skip reason names whichever piece is missing. To assert
it in a script:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests \
    -p test_portal_dbusmock.py -v 2>&1 | grep -cE '\.\.\. ok$'
# 0 means it skipped (or failed to load), not passed
```

That distinction is not pedantry: a permanently-skipped test in this repo
went on hiding a constructor signature that no longer existed.

Linting, formatting and type checking, all configured in `pyproject.toml`:

```sh
pip install -e '.[dev]'
ruff check src tests      # lint  (pycodestyle, pyflakes, pydocstyle, bugbear…)
ruff format src tests     # format
mypy                      # type check
pre-commit install        # run lint + format on every commit
```

The tree is currently clean under all three. The package ships `py.typed`
(PEP 561), so annotations are visible to your editor and type checker.

Requires Python 3.10 or newer — 3.9 reached end-of-life in October 2025.

## License

GPL-2.0-or-later. See [LICENSE](LICENSE).
