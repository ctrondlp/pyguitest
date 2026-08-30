# Changelog

All notable changes to pyguitest are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[semantic versioning](https://semver.org/spec/v2.0.0.html) — with the usual
0.x caveat that the API may still change between minor versions.

## [Unreleased]

### Added

- `examples/09_gui_spy.py` gained three ways to inspect a point beyond a
  bare `X Y`: `--find IMG.png` locates a control by picture (reusing
  example 08's `IMAGE_LOCATE`) and inspects its centre, so a script can be
  written against `role=`/`name=` even where AT-SPI cannot see a control's
  location on its own; `--tree` lists every accessible element containing
  the point instead of collapsing to the single smallest-area match,
  for when two controls overlap and that heuristic picks the wrong one;
  and `--json` gives one line of machine-readable output instead of the
  formatted report, with `--watch --json` streaming one JSON object per
  click as valid JSONL. The formatted report also gained an `ancestors`
  breadcrumb — the element's containing chain up to the window — for
  when the matched element itself has no name to match on but a parent
  does.
- `examples/_eiinput_validate.py`: a live-validation script for
  `LibeiBackend` (`eiinput`), forced rather than composited, pairing it
  with a separately forced `windows` session for window discovery since
  `eiinput` is input-only. Live-validated on KDE/KWin — see
  `docs/validation.md`. `eiinput` had previously only been run against
  GNOME Shell; this confirms KDE's `xdg-desktop-portal-kde` negotiates the
  same `RemoteDesktop`+libei path.

## [0.1.1] — 2026-08-30

### Added

- `pyguitest debug` command (plus `--json`): a pasteable diagnostic dump
  for bug reports — package and Python versions, every environment probe
  (not only the ones that came back true), each detected tool's own
  `--version`, and whether the process is running inside a Flatpak,
  toolbox, or other container.
- `Capability.WINDOW_EVENTS` on the `gnomeshell` backend: window
  create/close/title-change, pushed by the Shell extension over a
  `WindowEvent` D-Bus signal rather than polled. `wait_for_window` and
  `wait_window_close` now work the same way on GNOME as they already did
  on sway and niri. Live-validated on GNOME Shell 50.4 — `new`, `title`
  and `close` all confirmed over a real D-Bus connection.
- `examples/09_gui_spy.py`: an element inspector — point at a screen
  coordinate and get back the `role=`/`name=` to script against, plus a
  ready-to-paste snippet. Works on any desktop by passing a coordinate
  directly; `--here` and `--watch` (report on every click, until Ctrl+C)
  are X11-only conveniences, since reading the pointer position or button
  state is a capability no Wayland compositor exposes.
- `examples/_kdotool_validate.py`: a live-validation script for
  `KdotoolBackend` on real KWin, forced rather than composited.

### Fixed

- `KdotoolBackend.geometry()` raised `ValueError` on a window reported
  mid-animation, because KWin can give `getwindowgeometry` a fractional
  position (confirmed live: `545,274.5403238932292`) and the parser assumed
  a plain integer pair. Since a hit-test reads every open window's
  geometry, this could crash `window_at()` over a window unrelated to the
  one under test. Now parsed as `float` and rounded. Live-validated on KDE
  Plasma 6 / KWin (XWayland) — see `docs/validation.md`.
- `CompositeBackend` never overrode `MODIFIER_KEYS`/`KEY_ALIASES`/
  `resolve_char_key`, so `send_keys()` built key names in the base class's
  inherited X11-keysym vocabulary regardless of which member actually
  presses them — e.g. `"Control_L"` for `^`, even when the composite's
  `KEY_EVENT` provider is `UinputBackend`, which only knows evdev names
  like `"LEFTCTRL"`. `send_keys("^(a)")` raised `ValueError: unknown key
  name 'Control_L'` the moment this ran through the default composite
  rather than a single forced backend — confirmed live on KDE/KWin, where
  uinput is the only `KEY_EVENT` provider, but not KWin-specific: the same
  crash can occur on any desktop where uinput ends up providing key
  events. Fixed by routing all three to the `KEY_EVENT` provider, the same
  way every other composite operation dispatches.
- `examples/07_keys_and_pointer.py` used `{BKSP}` for backspace, which is
  not a recognized `KEY_ALIASES` abbreviation (`BAC`/`BS`/`BKS`, or the
  unabbreviated `BackSpace`) and was rejected by every backend. Fixed to
  `{BAC}`.
- `ToolCaptureBackend.capture()` trusted a screenshot tool's exit code
  alone. On KDE/KWin, `spectacle` intermittently exits 0 while leaving the
  output file empty — confirmed with pyguitest entirely out of the loop:
  plain `spectacle -b -n -f -o path` reproduced it directly, printing
  `KWin screenshot request failed: The process is not authorized to take a
  screenshot` and still exiting 0. That is a KWin/spectacle bug, not a
  pyguitest one, and left open in `docs/validation.md` — but trusting exit
  code 0 meant callers got a path to a corrupt image under a name that
  claimed success. Fixed by checking the destination is non-empty before
  returning it (both stages of the capture-then-crop path too), so this
  now raises a clear, actionable error instead.
- `examples/06_a_real_test.py`'s `test_the_window_is_actually_showing`
  guarded `is_window_viewable()` with `gui.supports(Capability.WINDOW_STATE)`,
  but that capability is also declared for `active_window()`'s sake on
  `KdotoolBackend`, which still refuses `is_window_viewable()` itself
  (see above) — `supports()` cannot predict a per-verb refusal like that,
  so the test errored on KDE/KWin instead of skipping. Fixed by catching
  `CapabilityUnsupported` around the call and skipping on it.
- `examples/04_drive_an_editor.py` and `examples/06_a_real_test.py` both
  type text into the editor and then clean up with a bare
  `process.terminate()`. Once the document has unsaved text, gedit's
  response to SIGTERM is its own "Save changes?" dialog rather than
  exiting — confirmed live: the process was still running when the
  interpreter exited, reported as a `ResourceWarning` rather than anything
  pyguitest raised. Fixed both to `wait(timeout=5)` after `terminate()`,
  falling back to `kill()` so cleanup cannot hang on that dialog.

## [0.1.0] — 2026-08-29

First public release.

### Added

- Capability negotiation as public API: `connect()`, `supports()`,
  `Capability`, `CapabilitySet` and the T1–T6 tier scale from the
  X11::GUITest audit in `docs/wayland-audit.html`.
- Element automation over AT-SPI, the one layer that behaves identically
  under X11 and Wayland.
- Input injection backends: X11, XDG desktop portal (RemoteDesktop),
  GNOME Shell extension, libei (opt-in), uinput, and the `wdotool`/`wtype`
  command-line tools.
- Screen capture and image search: portal capture, `grim`, X11, and
  template matching through ImageMagick.
- Window management per compositor, including the GNOME Shell extension
  path, since Mutter implements no foreign-toplevel protocol.
- A `pyguitest` command-line entry point.
- PEP 561 type information (`py.typed`); no hard runtime dependencies.

[Unreleased]: https://github.com/ctrondlp/pyguitest/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ctrondlp/pyguitest/tree/v0.1.0
