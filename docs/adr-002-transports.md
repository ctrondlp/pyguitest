# ADR 002 — Speak protocols directly; demote CLI tools to fallback

Status: accepted · 2026-08-22 · amends [ADR 001](adr-001-dependencies.md)

## Context

ADR 001 chose to adapt maintained command-line tools rather than bind libraries,
to satisfy "prefer maintained libraries" and "keep dependencies low" at once.
That was right for capture and for KDE, and wrong for the two layers where it
was leaned on hardest. Reviewing it surfaced costs the original decision
under-weighted:

- **pip cannot manage a CLI.** `pip install pyguitest` could yield a package
  that does nothing until tools are installed through the distribution.
- **A process spawn per operation**, roughly 5–15 ms. Tolerable for typing a
  whole string in one call; poor for drags or per-key work in a large suite.
- **CLI output is not an API.** kdotool's text parsing has no compile-time
  contract; a format change breaks it silently.
- **Session lifetime.** A tool that negotiates a fresh RemoteDesktop portal
  session per invocation may prompt for consent on every call. Unverified for
  `wdotool`, and a real risk if it holds.

## Decision

**Speak the documented protocol wherever one exists; keep tools as fallback.**

| Layer | Primary | Fallback |
|---|---|---|
| sway / i3 windows | unix socket, i3-ipc protocol (`ipc.SwaySocket`) | `swaymsg` |
| Hyprland windows | unix socket (`ipc.HyprlandSocket`) | `hyprctl` |
| niri windows | unix socket, line-delimited JSON (`ipc.NiriSocket`) | `niri msg` |
| KDE windows | `kdotool` | — |
| Input | keymap-safe tools, then python-evdev, then the rest | — |
| Capture | desktop screenshot tool | — |
| X11 | python-xlib | — |
| Elements | dogtail | — |

The sway, Hyprland and niri transports are **stdlib only** — `socket`, `struct`,
`json`. No tool needs to be installed, no process is spawned per query, the wire
format is versioned rather than scraped, and the connection stays open to stream
events. This removes `swaymsg`, `hyprctl` and `niri msg` from the runtime requirements.

niri was added later, on 2026-08-25, for a reason worth recording: `for_compositor`
returned `None` for any wlroots-family session that was neither sway nor Hyprland,
so river, wayfire, labwc and niri had no window backend at all. The alternative
considered was a native `wlr-foreign-toplevel-management` client — which would
have covered all four at once — but that protocol carries no geometry, no
placement, no pid and no stacking order, so it could not have replaced the socket
transports; it would have been an additional dependency layered under them. niri's
own IPC gives strictly more, at the same zero-dependency cost, and follows the
pattern already here. See [ADR 001](adr-001-dependencies.md) on `pywayland`.

niri's transport differs from the other two in opening a connection per request
rather than holding one. niri documents that requests are processed separately
with time passing between them, so a held connection buys no atomicity, and a
connection that has started an event stream stops answering requests entirely.

`kdotool` stays a tool: KWin exposes no simple socket, and driving its scripting
API over D-Bus is real work kdotool already does.

**Input gains an in-process path.** `python-evdev` is mature and maintained, and
holding one virtual device open beats spawning a process per event. It does not
escape uinput's limitation — events are injected below the compositor, so the
session's active layout is applied — so it is ranked *below* keymap-safe tools
and *above* keymap-unsafe ones:

1. `wdotool`, `wtype`, `xdotool` — client supplies its own keymap
2. python-evdev — keymap-unsafe, but in-process
3. `ydotool` — keymap-unsafe *and* a spawn per event

## Consequences

- A sway, Hyprland or niri session needs nothing installed for window control.
- niri is the first backend that can resize a window but not move one, which is
  what split `WINDOW_RESIZE` out of `WINDOW_PLACEMENT`. Placement was one flag
  for both, so a scrolling tiler could only have declared both or neither: both
  over-promises and fails at call time, neither throws away a working resize.
  Every other backend declares the pair, so nothing changed for them. niri has
  no minimize either, and no scratchpad to stand in for one.
- The i3-ipc framing is hand-written from the specification, so it is covered by
  socketpair tests exercising the real encoding, including split-packet reads
  and bad magic. niri's line framing gets the same treatment: a reply split
  across reads and two events arriving in one.
- Backends now take a *transport* rather than a subprocess runner, which made
  their tests simpler: a fake transport is a plain object, not an argv matcher.
- Typing non-ASCII through uinput fails with a clear error rather than a
  confusing one; only ASCII is reachable by scancode.
- Outstanding when this ADR was accepted: an in-process libei transport would
  make input both keymap-safe and spawn-free, but the Python bindings for libei
  were not mature enough to depend on. Closed since — see the update below.

## Update — 2026-08-29: the libei gap is closed

The `eiinput` backend is that transport. It injects through libei over a
RemoteDesktop portal session negotiated on D-Bus, using
[python-libei](https://pypi.org/project/python-libei/) for both the bindings
and (since its 0.3.0, where this package's own negotiation was upstreamed as
`libei.portal`) the negotiation —
written for this purpose, verified live on 2026-08-26 (GNOME 50.4), and
published to PyPI on 2026-08-28, which is what closes the maturity objection
the original bullet raised. It is the `eiinput` extra; `pip install pyguitest`
still pulls nothing.

It is the first input path that is keymap-safe *and* spawn-free at once, so the
ranking above gains a tier at the top:

0. `eiinput` — client supplies its own keymap, and no process is spawned
1. `wdotool`, `wtype`, `xdotool` — client supplies its own keymap
2. python-evdev — keymap-unsafe, but in-process
3. `ydotool` — keymap-unsafe *and* a spawn per event

Keymap safety here is not the same trick as the tools in tier 1. libei's
`ei_device_keyboard_key()` takes a raw evdev keycode, and the compositor
interprets it with *the keymap it handed this client* — so `xkb.py` compiles
that keymap and turns "which key produces this character" into a lookup rather
than a guess. On AZERTY, `type_text("a")` presses the physical Q key. Two
deliberate consequences: TEXT_ENTRY and KEY_EVENT are offered only when a
keymap was actually obtained (a keyboard without one is reported as no keyboard
at all, rather than an unsafe one), and a character the active layout cannot
produce raises instead of pressing something approximate.

**It does not displace the fallback ladder, and is deliberately not autoselected.**
`eiinput` is registered `opt_in=True`, like `portal`, so a plain `connect()`
never reaches it: constructing it can raise an interactive consent dialog, and
a library that prompts as a side effect of being imported into a test suite
would be worse than one that is slower. It has to be asked for —
`connect(backend="eiinput")`. That leaves tiers 1–3 as the path for every
session where no human can click Allow, headless CI included, so ADR 001's
tools stay exactly where they are.

The `python-evdev` reasoning in the decision above is unchanged: it remains the
in-process option that needs no portal session, and the keymap-unsafe ceiling
that motivated wanting libei in the first place is a property of uinput, not of
the binding.
