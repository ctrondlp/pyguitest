# Changelog

All notable changes to pyguitest are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[semantic versioning](https://semver.org/spec/v2.0.0.html) — with the usual
0.x caveat that the API may still change between minor versions.

## [Unreleased]

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
