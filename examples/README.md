# Examples

Run them from the project root, with the package importable:

```sh
pip install -e .                     # or: export PYTHONPATH=src
python3 examples/01_what_can_i_do.py
```

| | |
|---|---|
| `01_what_can_i_do.py` | **Start here.** What this desktop supports. |
| `02_find_windows.py` | List and match windows by title. |
| `03_widgets.py` | Buttons, text boxes, dropdowns — the recommended approach. |
| `04_drive_an_editor.py` | Launch an app, wait for it, type into it. |
| `05_screenshot.py` | Capture the screen, a window, or a rectangle. |
| `_x11_validate.py` | Not numbered: a live-validation script for `X11Backend`'s window control (move/resize/minimize/hit-test/lower/title-set), forced rather than composited. Candidate for removal or promotion to a real numbered example. |

Every script checks `gui.supports(...)` before it acts and exits with an
explanation if the desktop cannot do it. That is the intended pattern: what is
available differs by compositor, and a script should say so rather than fail
obscurely.

## What works where

| | GNOME (Mutter) | KDE (KWin) | sway / Hyprland | X11 |
|---|---|---|---|---|
| Windows | via AT-SPI | `kdotool` | built in | built in |
| Widgets | AT-SPI | AT-SPI | AT-SPI | AT-SPI |
| Input | portal tool | portal tool | built in | built in |
| Geometry | — | `kdotool` | built in | built in |

Two setup steps are easy to miss, and `pyguitest doctor` will name whichever
you need:

- **Elements** need `pip install '.[atspi]'` plus the distribution's
  `python3-gobject python3-pyatspi at-spi2-core`.
- **Input** needs `python3-evdev` (or `ydotool`) *and* membership of the
  `input` group — `/dev/uinput` is root-only by default:

  ```sh
  sudo usermod -aG input $USER   # then log out and back in
  ```

Note `wtype` works only on sway and Hyprland; Mutter and KWin lack the protocol
it needs, so it installs, runs, and silently types nothing. pyguitest will not
select it on those desktops.
