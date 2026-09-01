# Project structure

No hard dependencies; test code is roughly as large as the source it covers.

## Layout

```
pyguitest/
├── LICENSE                     GPL-2.0-or-later
├── pyproject.toml              packaging, ruff and pytest config
├── .pre-commit-config.yaml     ruff lint + format on commit
├── .github/workflows/ci.yml    tests on 3.10-3.14, lint, types, D-Bus
├── README.md                   what this is, install, usage
├── CONTRIBUTING.md             tests, lint, types, CI
├── examples/                   runnable scripts, simplest first
├── gnome-shell-extension/       pyguitest-window-control; opt-in, live-verified
├── docs/
│   ├── README.md               index into this folder, for anyone landing here directly
│   ├── wayland-audit.html      the audit all of this derives from
│   ├── install.md              what each backend needs, per distribution
│   ├── input.md                injecting input: permissions, keymaps, libei
│   ├── validation.md           what has been run against a real desktop
│   ├── adr-001-dependencies.md why libraries were chosen as they were
│   ├── adr-002-transports.md   why sockets replaced CLI tools
│   └── structure.md            this file
├── src/pyguitest/
│   ├── capabilities.py         the tier scale and capabilities
│   ├── xkb.py                  keymap lookup: which key types which character
│   ├── roles.py                accessible role names (button, entry, ...)
│   ├── hints.py                what to install, per distribution
│   ├── py.typed                PEP 561 marker
│   ├── compat.py               the X11::GUITest exports, as data
│   ├── errors.py               typed failures
│   ├── session.py              runtime environment detection
│   ├── app.py                  a launched program, and stopping it again
│   ├── tools.py                external CLI registry, ranked
│   ├── ipc.py                  sway, Hyprland and niri socket protocols
│   ├── png.py                  writing a PNG from raw pixels, stdlib only
│   ├── __init__.py             public API: connect(), Session, send_keys()
│   ├── __main__.py             the `pyguitest` command: report, doctor, debug, migrate
│   └── backends/
│       ├── base.py             the backend interface; send_keys()'s key tables
│       ├── __init__.py         registry, selection, composition, opt-in gating
│       ├── composite.py        merges backends, routes by capability
│       ├── null.py             tier-1 only, for CI and dead sessions
│       ├── windows.py          sway, Hyprland, niri, KDE window control
│       ├── gnomeshell.py       window control via a GNOME Shell extension
│       ├── portalrequest.py    the XDG portal request/response dance, shared
│       ├── portal.py           input via the RemoteDesktop XDG portal
│       ├── portalcapture.py    screenshots via the Screenshot XDG portal
│       ├── eiinput.py          input via libei, keymap-safe, over that portal
│       ├── atspi.py            element automation via dogtail
│       ├── input.py            injection via CLI tools; ydotool's evdev codes
│       ├── uinput.py           injection in-process via python-evdev
│       ├── capture.py          screenshots via desktop tools
│       ├── crop.py             cutting a rectangle out, via ImageMagick
│       ├── imagesearch.py      locate a template image via ImageMagick's compare
│       └── x11.py              X11 via python-xlib
└── tests/                      unit tests, no display server required
```

## Standards

| | |
|---|---|
| PEP 8 | `ruff check` — `E W F I D B UP C4 SIM RET ARG` |
| PEP 257 | every public class and function has a docstring |
| PEP 484 | the public API is annotated; `mypy` is clean |
| PEP 561 | `py.typed` ships, so editors see the annotations |
| Formatting | `ruff format` |

All three checks pass on the whole tree. Python 3.10 or newer; 3.9 reached
end-of-life in October 2025.

## Comment conventions

Three forms, each with one job:

| Form | Used for |
|---|---|
| `# -- label ------` | A section divider grouping related definitions. Padded to column 76 so they line up down the file. |
| A string after an assignment | Documents a public attribute or constant (PEP 258). A real object, so `help()` and documentation tools see it. |
| `# text` | Ordinary explanation, and anything private. |

`#:` — the Sphinx attribute-comment form — is deliberately **not** used. It
means something only to Sphinx, which this project does not run, and having two
ways to document an attribute is exactly how the source drifted in the first
place. `tests/test_style.py` enforces both rules, since no linter does.

## The three ideas

**1. Capabilities, not functions.** `capabilities.py` defines a set of operations,
each carrying the *tier* that operation sits in on Wayland — how much it costs to
implement, from `PORTABLE` (no display server involved) to `NO_PATH` (Wayland
forbids it). Backends declare what they provide; callers ask before depending.

The tier is a **Wayland ceiling, not an absolute**. The X11 backend serves every
`NO_PATH` capability, because X11 never restricted them. Same API, larger
capability set — which is precisely what `supports()` exists to reveal.

**2. Backends are partial by default.** `base.GUIBackend` defines every
operation, and each raises `CapabilityUnsupported` unless the subclass overrides
it. A backend that implements three things is a valid backend. This replaces
X11::GUITest's convention of returning zero on failure, which could not
distinguish "the click missed" from "this desktop cannot click".

**3. Sessions compose several backends.** No single mechanism covers a desktop.
`CompositeBackend` merges members and routes each call to whichever provides the
capability, so a GNOME session can take elements from AT-SPI and injection from
a tool adapter while the caller sees one object.

## How a call flows

```
    pyguitest.connect()
            │
            ▼
    session.detect()                  probes env vars, libraries,
            │                         /dev/uinput, PATH, portals
            ▼
    backends.select(environment)      asks each registered factory,
            │                         highest priority first
            ▼
    CompositeBackend([...])           every factory that returned a backend
            │
            ▼
    Session(backend, environment)     adds tier-1 ops and the delays
            │
            ▼
    gui.type_text("hello")
            │
            ├── capability lookup ──▶ TEXT_ENTRY
            ├── provider lookup ────▶ input:wdotool
            └── transport ──────────▶ wdotool type --delay 50 -- hello
```

## Backend registry

Factories register with a priority; `select()` tries all of them and composes
whatever answers. Priority decides who wins a contested capability.

| Priority | Name | Serves |
|---|---|---|
| 95 | `windows` | window control; wins `WINDOW_GEOMETRY` over AT-SPI |
| 93 | `gnomeshell` | window control on Mutter, and prompt-free per-window capture, via the `pyguitest-window-control` extension |
| 90 | `atspi` | elements, and window listing where Mutter offers nothing |
| 80 | `portal` *(opt-in)* | keyboard and pointer buttons/scroll, via the RemoteDesktop portal |
| 80 | `eiinput` *(opt-in)* | absolute pointer move/buttons/scroll, plus keymap-safe keyboard where the compositor's keymap is readable, via libei over the same portal |
| 70 | `input` | pointer and keyboard |
| 60 | `capture` | screenshots |
| 58 | `portalcapture` *(opt-in)* | screenshots via the Screenshot portal, needing no tool installed |
| 55 | `imagesearch` | locating a template image, via ImageMagick's `compare` |
| 40 | `x11` | everything, on X11 and XWayland sessions only |
| — | `null` | fallback when nothing else answers |

`windows` outranks `atspi` deliberately: both can list windows, but only
compositor IPC reports geometry that is trustworthy under Wayland. `gnomeshell`
outranks `atspi` for the same reason, and sees every window Mutter manages
regardless of whether the client is native Wayland or XWayland-backed --
something neither AT-SPI nor `x11` can claim on their own. It still needs the
extension in `gnome-shell-extension/` installed and enabled by hand (see that
directory's README) to actually answer, so on a fresh install `atspi` leads
on GNOME as before -- but its *factory* is safe to try automatically: probing
whether the extension is running is a plain D-Bus call with no side effect.

`portal` is a different kind of opt-in, and `register()` enforces it rather
than leaving it to convention: its priority (80) is never consulted by a
plain `connect()` at all, because *constructing* it can raise gnome's own
interactive consent dialog and block until a human answers it -- a side
effect no caller should hit from ordinary automatic detection. Passing
`register(..., opt_in=True)` excludes a factory from the composition loop
entirely while leaving it reachable by name, `connect(backend="portal")`, the
only way to reach it deliberately.

### Naming several backends

`backend` also takes a sequence, which composes exactly those and nothing
else — the way to pair an opt-in backend with the element and window access a
plain `connect()` would have given you, in one session rather than two:

```python
gui = connect(
    backend=["eiinput", "atspi"],
    backend_options={"eiinput": {"persist_mode": 2}},
)
save_somewhere(gui.backend.member("eiinput").restore_token)
```

**There are two precedence rules, and which applies depends on who chose the
order.** Automatic composition orders by registry priority, because nobody
expressed a preference — that is what the table above is for. A named
sequence is in the caller's order, so `["x11", "atspi"]` and `["atspi",
"x11"]` are different requests and the first member holding a capability
serves it.

Two more differences, both because naming a backend is a *request* rather
than a survey of what happens to be installed: a named backend that cannot
build raises `BackendUnavailable` instead of being quietly skipped, and
`backend_options` is keyed by backend name, since a flat dict cannot say
which backend an option was meant for. `session.backend` is then the
composite, so one member's own extras are reached through
`member("eiinput")` — which takes the registry name even for the backends
that report the tool they found (`imagesearch:compare`, `input:wtype`).

`eiinput` shares that priority and that reasoning -- it raises the same
consent dialog -- so it is registered `opt_in=True` too, and the shared 80
is harmless precisely because neither is ever consulted during automatic
composition. It exists alongside `portal` rather than replacing it because
the two cover different halves: `portal` has keyboard and no `move_mouse`
(absolute motion over its D-Bus methods needs a PipeWire stream from a
second ScreenCast dialog), while `eiinput` has absolute motion *and*
keymap-safe typing, the latter by compiling the compositor's own keymap
via `xkb.py`. `eiinput` is also the only one of the two verified working
against a live desktop.

`portalcapture` is opt-in for a narrower reason than `portal` and `eiinput`:
*constructing* it raises no dialog -- there is no session to negotiate, so
nothing is called until the first capture -- but that first capture does
prompt on desktops that gate screenshots. Left in automatic composition it
would sit at 58, above `imagesearch` and below `capture`, which sounds
harmless until a session has a portal and no screenshot tool: a plain
`capture()`, which grim or gnome-screenshot answer silently, would then open
a consent prompt instead. So it is reached deliberately,
`connect(backend="portalcapture")`, and is the one capture path that works
inside a Flatpak sandbox where no tool is on `PATH` at all.

`imagesearch` needs no session-type or compositor awareness at all, unlike
every other factory here: `compare`/`identify`/`magick` operate purely on
already-captured pixel files, so presence on `PATH` is the whole test. Its
priority (55) is not contested by anything else and mostly just keeps the
table reading "most load-bearing first".

## Transports

Window backends take a *transport* rather than shelling out, so the protocol and
the policy stay separate.

| Compositor | Primary | Fallback |
|---|---|---|
| sway / i3 | `ipc.SwaySocket` — unix socket, stdlib only | `ipc.SwayCLI` (`swaymsg`) |
| Hyprland | `ipc.HyprlandSocket` | `ipc.HyprlandCLI` (`hyprctl`) |
| niri | `ipc.NiriSocket` — line-delimited JSON | `ipc.NiriCLI` (`niri msg`) |
| KWin | `kdotool` | — |

Sockets mean no tool need be installed, no process spawn per query, a versioned
wire format rather than scraped output, and a connection that stays open to
stream events. `NiriSocket` is the exception on that last point: it opens a
connection per request, because niri processes requests separately and stops
answering them entirely once an event stream starts. See
[ADR 002](adr-002-transports.md).

niri is also the only backend that resizes without placing -- a scrolling
tiler sizes a window but never lets anyone position it -- which is why
`WINDOW_RESIZE` is a capability apart from `WINDOW_PLACEMENT`. It has no
minimize at all.

## Input ranking

Ordered by correctness first, then cost:

1. **keymap-safe tools** — `wdotool`, `wtype`, `xdotool`. The client supplies its
   own keymap, so typed text arrives as written.
2. **python-evdev** — in-process, one device held open, no spawn per event. But
   keymap-unsafe: events are injected below the compositor, so the session's
   active layout is applied.
3. **keymap-unsafe tools** — `ydotool`. Same limitation, plus a spawn per event.

`type_text` warns on any keymap-unsafe transport, and
`allow_keymap_unsafe=False` refuses outright — the right setting for a suite
that asserts on typed content.

Rank is not the only filter. A tool must also be *carryable* by the session:
`x11_only` tools cannot see native Wayland clients, and `wlroots_only` tools
need protocols Mutter and KWin do not implement. `discover()` applies both
before rank is consulted, because a tool that runs, exits zero and does nothing
is the worst failure available.

Both uinput routes need membership of the `input` group; `/dev/uinput` is
root-only by default.

## The user-facing API

Users should not need the capability model to get started. `Session` offers a
plain layer over it:

| Call | Returns |
|---|---|
| `gui.button(name)` / `text_field` / `dropdown` / `checkbox` | one `Element` |
| `gui.element(role=, name=)` | one `Element`, raising `ElementNotFound` |
| `gui.elements(role=, name=)` | a list, possibly empty |
| `gui.find_window(title)` | one `Window`, raising `WindowNotFound` |
| `gui.wait_for_window(title, timeout=)` | a `Window`, or `None` on timeout -- event-driven where possible |
| `gui.wait_window_close(window, timeout=)` | `bool`: closed before `timeout`? |
| `gui.wait_until(predicate, timeout=)` | `bool`: the general polling primitive, e.g. `gui.wait_until(lambda: button.enabled)` |
| `gui.wait_for_element(role=, name=, timeout=)` | an `Element`, or `None` on timeout -- poll-only, no backend offers element events yet |
| `gui.wait_until_gone(role=, name=, timeout=)` | `bool`: gone before `timeout`? |
| `gui.locate_image(template_path, within=, threshold=)` | an `ImageMatch`, raising `ImageNotFound` -- for a control AT-SPI cannot see |
| `gui.click()` / `type_text()` / `move_mouse()` | input, with the session's delays |
| `gui.send_keys(keys)` | the `SendKeys` `{}` grammar -- modifiers, named keys, `{PAUSE}` |
| `gui.glide(x, y, duration=, via=)` | the same move as a stream of events, for what watches the pointer on the way |
| `gui.drag(start, end)` | press, glide, release -- a drag a toolkit actually recognises |

The finders raise rather than returning `None`, so a script fails where the
mistake is rather than several lines later on an attribute of `None`. Role
strings come from `roles.Role` so a typo fails at import.

## Adding a backend

1. Subclass `GUIBackend`; override `capabilities` and the methods you can honour.
2. Call `self.require(Capability.X)` first in each method, so an unsupported
   call raises rather than half-executing.
3. Guard the import: return `None` from your factory when the library or tool is
   missing. A missing backend must never be an `ImportError` at package import.
4. `register(factory, "name", priority=N)` in `backends/__init__.py`.
5. Add `_DISPATCH` entries in `composite.py` for any new method names.

## Testing

The suite requires no display server, compositor, or optional
dependency. Three techniques:

- **Injected transports and runners.** Backends accept a `runner`, `streamer`, or
  `transport`, so tests assert on the exact commands built. This is where the
  real risk lives: ydotool's evdev button codes, Hyprland's comma syntax.
- **Faked modules.** `dogtail`, `Xlib` and `evdev` are substituted into
  `sys.modules`, so the adapters are exercised without the libraries installed.
- **Real sockets.** `test_ipc.py` drives `SwaySocket` and `NiriSocket` over a
  `socketpair`, verifying the i3-ipc binary framing and niri's line framing,
  including split-packet reassembly in both.

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

**What is not tested:** the sway, Hyprland and niri backends have never run
against a live session -- the sway and Hyprland JSON schemas are
reconstructions from their documentation, and niri's is transcribed from the
`niri-ipc` crate's serde types, all unexercised against a real compositor.
UinputBackend has not been driven live either. KWin's `KdotoolBackend` has
since been run live -- see [validation.md](validation.md).

X11Backend largely has. On a real X11 session: whole-screen capture (the
one capability real X11 has that XWayland does not), window control, and the
tier-6 query capabilities. On XWayland: per-window capture, and
`is_window_cursor` (which works, but reads `False` on a themed desktop --
see validation.md). What remains untested there is XTest input *injection*.

AT-SPI and the CLI-tool input/capture backends *have* now run live, on a real
GNOME/XWayland desktop -- and that surfaced three real bugs no unit test
caught: `AtspiBackend.geometry()` leaking a bare exception from dogtail's
ponytail dependency (two distinct failure modes), and `ToolInputBackend`
dropping stdout from its error message, hiding the real reason a tool failed.
Both are fixed and covered by tests now, but it is why "checked against the
library" and "run live" are called out as different things above: the gap
between them is exactly where these bugs were hiding.

A fourth bug came from neither route: running `mypy` for the first time in a
while (the project's own `.venv` had silently lost its `pip`/`mypy`/`pytest`
installs) found `Session` calling two backend methods `GUIBackend` never
declared. Fixing that surfaced a real, general gap in `CompositeBackend`'s own
dispatch check -- `hasattr()` can't tell "a member overrides this" from "a
member merely inherited GUIBackend's raising stub", so a backend that declared
a capability without actually implementing its method got a bare
`NotImplementedError` instead of the typed `CapabilityUnsupported` promised
everywhere else. A backend-by-backend audit (every real backend's declared
capabilities, cross-checked against which methods it actually overrides) found
one live instance of exactly that: `X11Backend` declared `WINDOW_STATE` without
implementing `is_window_viewable()`. All now fixed and covered by tests -- the
practical lesson being that `mypy` and grep-for-the-pattern audits catch a
different class of bug than either unit tests or live sessions do, and are
worth running deliberately rather than assuming "tests pass" covers them.

A fifth bug needed all three -- static analysis found nothing wrong, and it
takes a live server to see. `X11Backend` went live for the first time in this
project's history (`pip install '.[x11]'` on a real GNOME/XWayland desktop),
and `move_window`/`resize_window` moved windows to visibly wrong positions:
asked to move to (50, 60), the resulting `geometry()` read back (-50, -97).
The cause was `move_window`'s own `configure(x=x, y=y)` -- a raw
`ConfigureWindow` request sets position relative to whatever the window
manager reparented the client under, not the screen, which is exactly the
mismatch `geometry()` already has to correct for via `translate_coords` on
the *read* side. `move_window`/`resize_window` had no equivalent correction
on the *write* side, and had zero test coverage of any kind, static or live,
protecting them -- the bug was invisible until a real window physically moved
to the wrong place on a real screen. Fixed by sending `_NET_MOVERESIZE_WINDOW`
with `StaticGravity` (the EWMH message `wmctrl`/`xdotool` use under the hood
for exactly this reason) instead of a raw `ConfigureWindow`, verified against
the actual EWMH spec text rather than assumed, and now covered by tests
asserting the message's exact fields.

That fix did not fully close the loop, and this last part is **not** fixed,
possibly not fixable, and only a working hypothesis rather than a confirmed
diagnosis. After the move, a screenshot confirmed the window really is at the
requested position on screen -- but `geometry()` read back a position nowhere
close to it. Diagnostic reads of the raw, untranslated values isolated where
the wrong number enters: the root window's own geometry checks out exactly
right (`(0, 0, 1920, 1080)`, single monitor), and the client's offset within
its own decoration frame is a sane small value, but translating the client's
position through its frame to the root produces a result consistent with the
*frame* being recorded, at the X11 level, as sitting far off-screen -- while
Mutter visibly renders it correctly. If that reading is right, Mutter's
XWayland integration is not keeping the frame's X11-visible position synced
to where it actually places the window, which is a data-integrity problem in
what the X server reports rather than a logic error in how `geometry()`
computes from it -- the same `XTranslateCoordinates` call is what `xdotool`
and `wmctrl` rely on too, and there is no other X11 request to fall back on
if the server's own bookkeeping is what is wrong. Not reproduced on any other
window manager; this is one data point on one machine, not a survey.
