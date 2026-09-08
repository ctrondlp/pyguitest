# Why the API is not a port

pyguitest is the successor to X11::GUITest, not a translation of it. The reason
is in the audit: [wayland-audit.md](wayland-audit.md) classifies all 50
X11::GUITest 0.29 exports by what it would cost to implement each one on
Wayland, and the distribution is the finding.

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

The tier scale is a *Wayland ceiling*, not an absolute one. On Wayland every
achievable capability is served and the tier-6 ones cannot exist; the X11
backend serves those too, so an X11 session gets a strictly larger capability
set through the same API. Discovering which one you are on is what
`supports()` is for.

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
  See [ADR 001](adr-001-dependencies.md) and [ADR 002](adr-002-transports.md).
- **Input tools are ranked by keymap safety.** `wdotool` and `wtype` let the
  client supply a keymap; `ydotool` injects scancodes *below* the compositor, so
  `type_text("Hello")` produces different characters on an AZERTY session and no
  protocol reports the active layout. That ranking is encoded in `tools.py`, and
  detection warns when only keymap-unsafe tools are present.
- **AT-SPI leads.** It answers what the window-tree walk was really used for,
  needs neither geometry nor injection permission, and behaves identically under
  X11 and Wayland — the one layer needing no backend matrix.
- **The X11 backend is a peer, not a legacy path.** It is the surest route to
  the BSDs and Solaris — python-xlib speaks the wire protocol in pure Python and
  assumes no kernel — and the only backend serving tier-6 capabilities at all.
  Less is Linux-only than it looks: `/dev/uinput` and libei are kernel
  interfaces, but compositor IPC is a unix socket and JSON, and sway, grim and
  wtype are all in FreeBSD ports. Nothing here gates on the platform; nothing
  off Linux is tested either.
- **Compositor IPC fills the geometry hole.** sway, Hyprland and niri report
  window rectangles, which no Wayland protocol exposes — and once every
  rectangle is known, hit-testing a coordinate is arithmetic rather than a
  compositor query.
- **No hard dependencies.** Every mechanism is probed at runtime and degrades to
  an unsupported capability rather than an import error. Extras are per-backend,
  so a real install is the package plus one extra.

## Further reading

- [wayland-audit.md](wayland-audit.md) — the full 50-export classification this
  page summarises
- [ADR 001](adr-001-dependencies.md) — why these libraries
- [ADR 002](adr-002-transports.md) — why sockets replaced CLI tools
- [structure.md](structure.md) — how a call flows through the layers
