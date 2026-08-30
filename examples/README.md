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
| `06_a_real_test.py` | **The one to copy.** pyguitest inside a `unittest` suite: skip on a missing capability, screenshot the failure while it is still on screen, wait on conditions instead of sleeping. |
| `07_keys_and_pointer.py` | `send_keys` in X11::GUITest's own notation, escaping with `quote_for_type`, and the raw pointer calls. |
| `08_find_by_image.py` | Find a control by a picture of it, for anything AT-SPI cannot see. |
| `09_gui_spy.py` | Point at a screen coordinate, get back the `role=`/`name=` to script against — an element inspector. |
| `_x11_validate.py` | Not numbered: a live-validation script for `X11Backend`'s window control (move/resize/minimize/hit-test/lower/title-set), forced rather than composited. Candidate for removal or promotion to a real numbered example. |

`06_a_real_test.py` is a `unittest` file rather than a top-to-bottom script,
because that is how the library is actually used. Run it directly:

```sh
PYTHONPATH=src python3 examples/06_a_real_test.py -v
```

`unittest discover` cannot import it — discovery turns the filename into a
module name, and `06_a_real_test` starts with a digit, so it is not a valid
identifier. Discovery reports "NO TESTS RAN" rather than an error. That is
a quirk of the numbering used here; your own test files will not have it.

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
