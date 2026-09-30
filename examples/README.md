# Examples

Run them from the project root with the package installed:

```sh
pip install .                        # or, without installing: export PYTHONPATH=src
python3 examples/01_what_can_i_do.py
```

Use `python` instead of `python3` on Windows. Install the extra for your
platform too (`.[atspi]`, `.[windows]` or `.[macos]`) for the examples that
work with elements, and run `pyguitest doctor` for anything else your desktop
needs.

| | |
|---|---|
| `01_what_can_i_do.py` | **Start here.** What this desktop supports, and what to install for more. |
| `02_find_windows.py` | List and match windows by title. |
| `03_widgets.py` | Buttons, text boxes, dropdowns — the recommended approach. |
| `04_drive_an_editor.py` | Launch an app, wait for it, type into it. |
| `05_screenshot.py` | Capture the screen, a window, or a rectangle. |
| `06_a_real_test.py` | **The one to copy.** pyguitest inside a `unittest` suite: skip on a missing capability, screenshot the failure while it is still on screen, wait on conditions instead of sleeping. |
| `07_keys_and_pointer.py` | `send_keys` in X11::GUITest's notation, escaping with `quote_for_type`, and the raw pointer calls. |
| `08_find_by_image.py` | Find a control by a picture of it, for anything the accessibility tree cannot see. `--demo` generates a synthetic pair into `images/` and searches one for the other — no desktop needed, so it is the quickest check that template matching works at all. |
| `09_gui_spy.py` | Point at a screen coordinate (or `--find` a picture of a control) and get back the `role=`/`name=` to script against. `--tree` lists every element at that point; `--json` gives machine-readable output. |
| `10_natural_mouse.py` | Tour the screen with `move_mouse_naturally`, showing the arc, speed ramp, drift and overshoot. **Moves your real pointer** for about ten seconds; `--dry-run` prints the waypoints instead. `--duration`, `--latency`, `--pause`, `--arc`, `--wobble` and `--overshoot` shape the motion (`--arc 0 --wobble 0` is a straight line), and the default seed makes each tour repeatable. |

Every script checks `gui.supports(...)` before it acts and exits with an
explanation if the desktop cannot do it. That is the intended pattern: what
is available differs by desktop, and a script should say so rather than fail
obscurely. On macOS, the examples that inject input need `macquartz`, which a
plain `connect()` does not select — see
[install.md](../docs/install.md#connecting-with-input).

`06_a_real_test.py` is a `unittest` file rather than a top-to-bottom script,
because that is how the library is normally used. Run it directly:

```sh
python3 examples/06_a_real_test.py -v
```

`unittest discover` cannot import it: its filename starts with a digit, so
it is not a valid module name, and discovery reports "NO TESTS RAN" rather
than an error. Your own test files will not have this problem.

What each desktop supports is summarised in the project
[README](../README.md#what-works-where); setup for elements, input and
capture is in [install.md](../docs/install.md) and
[input.md](../docs/input.md).

## Live-validation scripts

The scripts whose names start with `_` are for maintainers checking a
backend against a real desktop, and are recorded in
[validation.md](../docs/validation.md). Most move the real pointer, type real
keys, or raise a consent dialog; read each script's docstring before running
it.

| | |
|---|---|
| `_x11_validate.py` | `X11Backend` window control: move, resize, minimize, hit-test, lower, set title. |
| `_xtest_input_validate.py` | `X11Backend` input through XTest: pointer, buttons, keys, `type_text` and `send_keys`. Works on a real X11 session; under GNOME's XWayland it fails by design (see validation.md). `--dry-run` checks the setup without injecting. Needs `python-xlib`. |
| `_cursor_validate.py` | `X11Backend.is_window_cursor()` (`WINDOW_CURSOR_QUERY`). Needs `python-xlib`. |
| `_kdotool_validate.py` | `KdotoolBackend` window control on KWin, including the documented `is_window_viewable` refusal. |
| `_kwin_events_validate.py` | `KWinEventsBackend`: `new`, `title` and `close` window events on KDE. |
| `_sway_validate.py` | `SwayBackend` window control and events. Run it under `scripts/headless-sway-session.sh`; needs no real desktop. |
| `_eiinput_validate.py` | `LibeiBackend` (`eiinput`): pointer, click, scroll and typed text. Raises a consent dialog. |
| `_eiinput_portal_validate.py` | `eiinput` through python-libei's `libei.portal`: one consent dialog, typed text read back through AT-SPI, then a restore-token reconnection that must raise **no** dialog. `--preflight` checks without any D-Bus traffic; `--rehearse` runs the rest first. |
| `_clipboard_validate.py` | `Capability.CLIPBOARD`: round trip, persistence, and replacement on a second write. |
| `_portal_clipboard_validate.py` | The portal clipboard (the only clipboard path on GNOME): a separate process pastes what was written, and the selection's lifetime is checked. Raises a consent dialog. |
| `_sync_under_load_validate.py` | What `Capability.INPUT_SYNC` is worth on a busy machine: round-trip latency idle versus with every core saturated. Raises a consent dialog. |
| `_inputcapture_validate.py` | `Capability.INPUT_CAPTURE` / `wait_for_pointer_activation()`. **Approving its dialog diverts your own pointer** until the capture ends, so run it yourself, attended, never unattended or from an agent session. |
| `_natural_motion_validate.py` | `move_mouse_naturally()` against a real display server: motion event counts, path shape, speed ramp, and Enter/motion/Leave on a probe window. Forces `x11`; run it under `scripts/headless-session.sh`. |
