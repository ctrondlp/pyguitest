# Injecting pointer and keyboard input

Input is the one area where the mechanism you get decides what your typing
actually produces, so it is worth knowing which one you are on. In rough
order of preference: `eiinput` (keymap-safe, needs libei), the `wdotool` /
`wtype` CLI tools (keymap-safe, wlroots or GNOME), `uinput` and `ydotool`
(keymap-*unsafe*), `xdotool` and XTest (needs an X connection, so X11 or
XWayland -- but under XWayland it reaches only that session's X clients,
never its native Wayland ones).

What has and has not been exercised against a real desktop is recorded in
[validation.md](validation.md).

## uinput: `/dev/uinput` permissions

Injection through uinput needs one more step than the rest, because
`/dev/uinput` is root-only by default. Install the input packages for your
distribution (`pyguitest doctor` names them, or see
[install.md](install.md)), then:

```sh
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

`static_node=uinput` matters: `/dev/uinput` is created when the module loads
rather than by a hotplug event, so permissions have to be applied at
module-load time. The rule takes effect on the next boot; to fix the running
node without rebooting, `sudo chgrp input /dev/uinput && sudo chmod g+rw
/dev/uinput` (reloading the module usually fails with "Module uinput is in
use").

Group membership is established at **login** and inherited by every child
process, so opening a new terminal is *not* enough — it inherits the old
set. Either:

```sh
newgrp input        # this shell only, takes effect immediately
```

or log out and back in, which fixes the whole session.

Verify with `id -nG` — **no username**. `id -nG $USER` reads the group
database and will show `input` the moment `usermod` runs, whether or not
your current processes have it, which is misleading. The check that actually
matters is whether Python can open the device:

```sh
python3 -c "import os; print(os.access('/dev/uinput', os.W_OK))"
```

`pyguitest doctor` performs exactly that test.

## ydotool: the daemon and its socket

`ydotool` is the alternative to python3-evdev and also packaged, but it
needs a `ydotoold` daemon running; python3-evdev needs no daemon.

Fedora's `ydotool` package ships `ydotool.service`, which runs `ydotoold` as
**root** with no arguments. ydotoold defaults to a fixed socket at
`/tmp/.ydotool_socket` (deliberately outside any one user's runtime dir, so
a root daemon and non-root clients *can* share it) — but it creates that
socket `root:root` mode `0600`, so nothing but root can actually open it
yet:

```sh
sudo systemctl enable --now ydotool   # starts the daemon; socket is still root-only
ls -la /tmp/.ydotool_socket           # confirm: srw-------. 1 root root
```

Running the client under `sudo` "works" but is not the fix — it just runs
*pyguitest itself* as root, which loses `DISPLAY`/`WAYLAND_DISPLAY`/
`DBUS_SESSION_BUS_ADDRESS`/`XDG_RUNTIME_DIR` (your desktop session lives on
those) unless painstakingly reconstructed with `sudo -E`. The actual fix is
to have the daemon create the socket group-owned by `input` and
group-writable, the same group already used for `/dev/uinput` above:

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

One more mismatch: the `ydotool` *client* does not default to
`/tmp/.ydotool_socket` at all — it looks in `$XDG_RUNTIME_DIR/.ydotool_socket`
(`/run/user/<uid>/.ydotool_socket`), which is a different default from the
daemon's, on the ydotool build Fedora ships. Point the client at the socket
the daemon above actually created:

```sh
echo 'export YDOTOOL_SOCKET=/tmp/.ydotool_socket' >> ~/.bashrc
```

(open a new shell, or `source ~/.bashrc`, for it to take effect). With both
of those in place — group membership on the socket, and the client pointed
at where the daemon actually listens — `ydotool` works with no `sudo`.

## Keymap safety

uinput and ydotool both go through `/dev/uinput`, which injects *below* the
compositor, so the session's active keyboard layout is applied to whatever
you type. On a US layout that is invisible; on AZERTY or Dvorak,
`type_text("Hello")` produces different characters. `type_text` warns, and
`type_text(..., allow_keymap_unsafe=False)` refuses outright — use that in
any suite that asserts on typed content.

Among the CLI tools, the only keymap-safe option on GNOME is
[wdotool](https://github.com/cushycush/wdotool), which speaks libei through
the RemoteDesktop portal and needs no group membership. No distribution
packages it yet, so it has to be built from source. On sway and Hyprland,
`wtype` is keymap-safe and packaged; it does **not** work on GNOME or KDE,
whose compositors lack the protocol it needs.

`pyguitest doctor` prints whichever of these apply to your machine.

## `eiinput`: keymap-safe input over libei

`connect(backend="eiinput")` — opt-in — covers what `portal` deliberately
cannot: absolute pointer motion, via
[libei](https://libinput.pages.freedesktop.org/libei/) rather than the
portal's own D-Bus methods, using
[python-libei](https://github.com/ctrondlp/python-libei) for the bindings.
Unlike `portal`'s `NotifyPointerMotionAbsolute`, this needs no PipeWire
stream and no ScreenCast consent — verified live (2026-08-26, GNOME 50) by
enumerating every device the seat offers, with and without a ScreenCast
source selected: identical either way, absolute-pointer region included.

Installing what it needs: the native `libei` and `libeis` libraries from
your distribution (`libei libeis` on Fedora and openSUSE, `libei1 libeis1`
on Debian/Ubuntu, `libei` on Arch), then the bindings:

```sh
pip install '.[eiinput]'     # or: pip install 'python-libei[portal]'
```

The two halves are separate on purpose — `python-libei` is pure ctypes and
its wheel carries no `.so`, so pip installs the bindings and the
distribution supplies the library they `dlopen`. `liboeffis` is not needed
and is not used: the RemoteDesktop session is negotiated over D-Bus by
python-libei's own `libei.portal` module, because `oeffis_create_session()`
takes only a device-type bitmask and so cannot express `persist_mode` or
`restore_token` (see `backends/eiinput.py`'s module docstring, and
upstream's own note that liboeffis is "intentionally kept simple"). That
negotiation lived in this package until it was upstreamed in python-libei
0.3.0; the `eiinput` extra requires 0.4.0, which is where a negotiation
that fails part-way stopped leaving its portal session open behind it, and
where the timeout below came to bound every leg of a round trip rather than
only the wait for the portal's reply. The extra also pulls in PyGObject,
which that module needs. `libeis` is only needed to run
`tests/test_eiinput_libei.py`, not at runtime.

This is the only backend here that is **keymap-safe by construction**.
`Device.keyboard_key()` takes a raw Linux keycode and the compositor
interprets it through the very keymap it handed the client, so `xkb.py`
compiles that keymap and looks the answer up rather than guessing: on a
French AZERTY layout `type_text("a")` presses the physical Q key, where
`uinput`'s hardcoded US table types "q". Typing is refused outright if no
keymap could be read (guessing is exactly the failure being avoided), and a
character the active layout cannot produce raises rather than pressing
something approximate.

### The two-pointer trap

Worth knowing about, since it cost a long debugging session: **one seat
resumes two pointer devices**, `virtual pointer` (relative) and `shared
virtual absolute pointer` (absolute, with a region), as separate
`DEVICE_RESUMED` events — the relative one first, every time observed.
Taking the first device to resume, which is the obvious implementation,
yields a device whose `pointer_motion_absolute()` logs a libei-internal
warning and silently does nothing: no exception, no movement. That presented
as maddening flakiness — byte-identical code working, then not — and was
initially misdiagnosed as a missing ScreenCast/PipeWire linkage, with a
whole combined-session negotiation built on the misreading before a `busctl
--user monitor` comparison caught the reference script failing the same way.
`_wait_for_device` now waits for the device it actually needs.

## `portal`: input through xdg-desktop-portal

`connect(backend="portal")` — deliberately not part of automatic detection
at all; see [Backend registry](structure.md#backend-registry) — talks to
`org.freedesktop.portal.RemoteDesktop` for keyboard and pointer
button/scroll injection, with every method transcribed from the actual
portal XML rather than assumed. It is the furthest out of the backends; see
[validation.md](validation.md) for exactly how far it has been driven.

### Avoiding repeat consent dialogs

There is no way to skip the dialog the *first* time — that first click is
the portal's actual security boundary, not something pyguitest sits in front
of. What the protocol *does* support is not asking again: `SelectDevices`
takes a `persist_mode` option (`2` = "permissions persist until explicitly
revoked"), and once a session is approved under that mode, `Start()`'s reply
carries a `restore_token`. Passing that token back into a later
`SelectDevices` call lets the portal recognize the previously-approved
session and skip straight to `Start()` — the same mechanism every
screen-sharing and remote-desktop app uses to avoid re-prompting on every
launch, not a bypass of it. Each successful restore returns a token in its
own `Start()` reply, and the caller must save that one in place of the old:
the portal is free to answer with a different token, and code that keeps
reusing the original would eventually present a stale one. (Measured
2026-09-01 on GNOME: the same token comes back on every restore. That is one
portal's behaviour, not a guarantee to rely on.)

This is implemented, on both `portal` and `eiinput`, and is opt-in: a plain
`connect(backend="portal")` sends neither option and prompts every time, so
nothing starts persisting behind your back. Ask for it explicitly, and save
the token that comes back:

```python
gui = connect(
    backend="portal",
    backend_options={
        "persist_mode": 2,  # PERSIST_UNTIL_REVOKED
        "restore_token": saved,  # None on the first run
    },
)
save_it_yourself(gui.backend.restore_token)  # save every run; replaces `saved`
```

Forcing an input backend by name gives you *only* that backend, and neither
`portal` nor `eiinput` can find an element or list a window. To get both in
one session, name both — the options are then keyed by backend, and the
token is read off that member:

```python
gui = connect(
    backend=["eiinput", "atspi"],
    backend_options={"eiinput": {"persist_mode": 2, "restore_token": saved}},
)
save_it_yourself(gui.backend.member("eiinput").restore_token)
```

pyguitest deliberately never writes the token anywhere itself — see the
caution below for why that is your decision rather than the library's.

**Caution:** a `restore_token` is a standing grant of keyboard and pointer
injection to whatever presents it — treat it as a credential, not a
convenience flag. It is scoped to the requesting app and kept in the
portal's own permission store (`$XDG_DATA_HOME/flatpak/db`, per the
[xdg-desktop-portal wiki](https://github.com/flatpak/xdg-desktop-portal/wiki/The-Permission-Store)),
where the user can revoke it independently of your code (KDE Plasma 6.5+ has
a dedicated Application Permissions settings page for this; on other
desktops it is the portal-permission-store contents or, for a
Flatpak-packaged caller, `flatpak permission-reset`). Don't request
`persist_mode=2` on a shared or multi-tenant machine without telling whoever
clicks Allow what they are persisting, and don't build automation that
clicks the dialog *for* the user — that would defeat the reason it exists.
`persist_mode=1` (app-lifetime only) or `0` (default, no persistence, one
dialog per run) are the lower-privilege choices when repeat-prompt annoyance
is not worth the standing grant.

For CI or other headless runs where no human can click anything at all,
`xdg-desktop-portal` ships fake portal backends for its own test suite that
answer `Start()` with no UI — appropriate only inside a sandbox you control,
never pointed at a real user's session. That is the same boundary
python-libei's `eis.Eis.create_for_fd()` already draws for testing without a
portal at all, and the boundary `tests/test_portal_dbusmock.py` works
inside.

## Motion between the two points

`move_mouse()` teleports. One event goes out and the pointer is simply
somewhere else, which is all a click needs and not enough for anything that
watches the pointer on the way:

- **Drag-and-drop.** GTK and Qt both arm on the press and begin the drag only
  once motion afterwards crosses a threshold. Press, teleport, release is not
  a drag — it is a click at the destination.
- **Hover.** Enter/leave crossings, tooltips, hover-reveal buttons,
  drop-target highlighting. A teleport *into* a widget does cross into it; a
  teleport *through* one never happens.
- **Velocity.** Kinetic scrolling, flick gestures and gesture recognisers all
  derive speed from event timestamps.
- **Approach.** Hot corners and pointer barriers fire before arrival.

`glide()` sends the same move as a stream of events on a wall-clock schedule,
and `drag()` wraps a press and a release around one:

```python
gui.move_mouse(120, 400)
gui.glide(600, 400, duration=0.3)        # ~36 events over 300ms

gui.drag((120, 400), (600, 400))         # press, glide, release

gui.glide(600, 400, via=[(300, 120)])    # routed over the toolbar on the way
```

`duration` is wall-clock seconds and `rate` the points per second, so the two
give `duration * rate` events; 120 Hz by default, which is in the range of a
real mouse without flooding a backend that pays per event. Each pause is
computed against the moment the glide began rather than the end of the last
one, so a backend with real per-event cost — `eiinput` frames and pumps its
socket every time — loses that cost from the pauses instead of adding it to
the total. The session's `event_delay` is charged once for the whole gesture,
not once per point.

### Where the path starts

Wayland has no pointer readback: `POINTER_QUERY` is tier NO_PATH on every
compositor, and only an X connection answers it. So a `Session` remembers
where it last *sent* the pointer and interpolates from there. A hand on the
physical mouse invalidates that; pass `start=(x, y)` where it matters, and
note that `drag()` always takes its start explicitly. With no history and no
readback, `glide()` raises rather than assuming `(0, 0)` — a drag from the
wrong corner does not fail, it succeeds at something else.

Under XWayland the readback deserves the same suspicion as no readback at
all. X answers only for the pointer over an X surface; over a native Wayland
window it returns the last position it knew, with nothing to mark it stale
([validation.md](validation.md)). A glide falling back to that read starts
from the wrong place and runs anyway — the one outcome the raise above
exists to avoid. Where both are live, prefer an explicit `start`.

### On "human-like" paths

The route is straight unless `via` names waypoints. Randomised, human-shaped
wobble — Bézier control points and Gaussian jitter — is a bot-detection
evasion technique, and there is nothing on this side of the compositor looking
for it. In a test suite it buys only flakiness: a path that varies run to run
is a click that occasionally grazes a tooltip and lands somewhere else.

The non-straight path that *is* worth having is a deliberate one, which is
what `via` is for — crossing a particular widget on the way, or holding the
angle a GTK submenu's navigation triangle wants to keep the parent menu open.
Both are reproducible. `ease` is available for the rest of the argument (a
callable from a fraction in [0, 1] to one, clamped) and is off by default,
because constant velocity is what a flick test wants: an ease-out decelerates
into the target, and a flick released at zero speed does not throw.

## When injected input appears to do nothing

**Check your environment before suspecting the backend.** On a VirtualBox
guest, *Mouse Integration* slaves the guest pointer to the host's mouse via
an absolute "VirtualBox USB Tablet" device, continuously overriding anything
injected inside the guest — libei, uinput and ydotool alike. Symptoms are
confusing: clicks land and hover fires, but the cursor never visibly moves.
Turn it off with **Input → Mouse Integration** (Host+I).

Two more checks worth running:

```sh
sudo libinput debug-events --device /dev/input/eventN   # do the events reach libinput?
loginctl seat-status seat0                              # is the device on your seat?
```
