# Recipes

Task-shaped answers. Each one is a whole working idea, not a signature — for
signatures see [api.md](api.md); for a first script see
[getting-started.md](getting-started.md).

- [Waiting instead of sleeping](#waiting-instead-of-sleeping)
- [Finding and driving a window](#finding-and-driving-a-window)
- [Filling in a form](#filling-in-a-form)
- [Choosing between several similar controls](#choosing-between-several-similar-controls)
- [Starting and stopping the application under test](#starting-and-stopping-the-application-under-test)
- [Screenshots, and evidence when a test fails](#screenshots-and-evidence-when-a-test-fails)
- [Clipboard](#clipboard)
- [Keyboard, focus and tab order](#keyboard-focus-and-tab-order)
- [Scrolling and dragging](#scrolling-and-dragging)
- [Asserting the UI is accessible at all](#asserting-the-ui-is-accessible-at-all)
- [Writing a test suite](#writing-a-test-suite)
- [Running in CI](#running-in-ci)
- [X11::GUITest cheat sheet](#x11guitest-cheat-sheet)

## Waiting instead of sleeping

`time.sleep(2)` is the bug. It is too slow on a fast machine and too short on
a loaded one, and it turns a real failure into an intermittent one. Wait for
the thing you are actually waiting for:

| You are waiting for… | Use |
|---|---|
| A window to appear | `gui.wait_for_window("Save As", timeout=10)` |
| A window to close | `gui.wait_window_close(dialog, timeout=10)` |
| A control to appear | `gui.wait_for_element(name="Saved", timeout=5)` |
| A control to go away (a spinner, say) | `gui.wait_until_gone(role=Role.PROGRESS_BAR, timeout=60)` |
| Any condition you can express | `gui.wait_until(lambda: gui.get_clipboard() == "done", timeout=5)` |
| A file to be written | `gui.wait_for_file("/tmp/export.csv", timeout=30)` |
| A process to start | `gui.wait_for_process(r"gnome-text-editor", timeout=10)` |
| A process to stop working | `gui.wait_for_idle(app.pid, timeout=30)` |
| The compositor to have consumed your input | `gui.sync()` |

Two of these are worth knowing precisely.

**`wait_for_idle(pid)` means CPU-idle, not "the UI stopped changing."** It
samples `/proc/<pid>/stat` (or `ps` where there is no readable `/proc`, or
`GetProcessTimes` on Windows) and returns once the process has stopped burning
CPU. That is the right signal after clicking *Export* and
before checking the output file; it is the wrong signal for an application
idling in an animation loop.

**`sync()` proves delivery to the compositor, not to the application.** It
answers "has my click been consumed", never "has the application repainted".
It needs `INPUT_SYNC`, which only the libei backend provides; where it is
absent, wait on something observable instead.

```python
gui.button("Export").click()
gui.wait_for_file("/tmp/export.csv", timeout=30)  # the observable outcome
```

## Finding and driving a window

A plain string title matches as a **literal substring**, so a title copied
from `Window.title` always finds itself, parentheses and all. Pass a
compiled regex when you need a pattern — titles carry document names,
modification markers and application suffixes that change under you:

```python
import re

editor = gui.expect_window("Untitled Document", timeout=10)
editor = gui.expect_window(re.compile(r"^\*?notes\.txt"), timeout=10)
gui.activate_window(editor)

print(editor.title, editor.app_id, editor.pid)
```

An `app_id` survives title changes altogether:
`gui.find_window(app_id="org.gnome.TextEditor")`.

A `Window` is a snapshot. It stays comparable by identity (`==` and `hash`
are by handle and backend, never by title), but its *title* is whatever it
was when you looked:

```python
editor = gui.refresh_window(editor)  # None if it has closed
if gui.is_window_open(editor):
    x, y, w, h = gui.geometry(editor)
```

Moving and resizing is compositor-dependent — a tiling compositor may size a
window without letting you place it, which is why `WINDOW_PLACEMENT` and
`WINDOW_RESIZE` are separate capabilities:

```python
if gui.supports(Capability.WINDOW_PLACEMENT):
    gui.move_window(editor, 100, 100)
```

## Filling in a form

```python
gui.text_field("Name").set_text("Ada Lovelace")
gui.text_field("Email").set_text("ada@example.com")
gui.dropdown("Country").choose("Norway")
gui.checkbox("Subscribe").click()
gui.button("Submit").click()
```

`set_text()` replaces the field's content through the accessibility layer —
no keystrokes, no focus juggling, and nothing to get wrong under a keyboard
layout that is not yours. Use `type_text()` only when you specifically want
the application to see individual key events (an autocomplete that reacts per
keystroke, for instance).

## Choosing between several similar controls

When a name is ambiguous, add constraints rather than falling back to
coordinates:

```python
import re
from pyguitest import Role

gui.element(role=Role.PUSH_BUTTON, name="Delete", enabled=True).click()
gui.element(name=re.compile(r"^Save\b")).click()
gui.element(role=Role.CHECK_BOX, within=gui.window_element("Preferences"))
gui.element(predicate=lambda e: e.checkable and not e.checked)
```

`within=` scopes a search to one subtree, and `gui.window_element(title)` is
how you get that subtree for a whole window. `predicate=` takes any
`Element -> bool` and is the escape hatch for everything else — an ancestry
check, a value threshold, a combination no keyword expresses.

To see what there actually is to match against, without writing a script:

```sh
pyguitest inspect --window "Preferences"
pyguitest inspect --json > tree.json
```

## Starting and stopping the application under test

`start_app()` returns an `Application`, which is a context manager:

```python
with gui.start_app(["gnome-text-editor", "/tmp/notes.txt"]) as app:
    editor = gui.wait_for_window("notes", timeout=10)
    gui.button("Save").click()
    gui.wait_for_idle(app.pid, timeout=10)
# the application is stopped here, even if the block raised
```

`app.restart()`, `app.is_running()`, `app.stop()` and the `subprocess.Popen`
members you already use (`wait`, `poll`, `returncode`, `terminate`, `kill`)
are all present. `stop()` is the bounded one: it asks politely, then kills.

## Screenshots, and evidence when a test fails

```python
gui.screenshot("desktop.png")
gui.screenshot("editor.png", window=editor)
gui.screenshot("corner.png", region=(0, 0, 400, 300))
```

For failures, do not write your own `except` block — by the time it runs the
application is usually gone:

```python
with gui.capture_on_failure("artifacts"):
    gui.button("Save").click()
    assert gui.element(name="Saved")
```

Nothing is written when the block succeeds. On failure you get a bundle
attached to the exception itself — `.screenshot`, `.accessibility_tree`,
`.active_window`, `.focused_element`, each with a matching `.<name>_error` if
that one artifact could not be taken — and the original exception is re-raised
untouched, so your test runner still reports the real failure.

## Clipboard

```python
gui.set_clipboard("hello")
assert gui.get_clipboard() == "hello"

gui.assert_clipboard("hello")  # raises ClipboardMismatch, with detail
gui.get_clipboard(primary=True)  # the X11 PRIMARY selection
```

Clipboard access is `Capability.CLIPBOARD`: the Win32 clipboard on Windows,
`pbcopy`/`pbpaste` on macOS, and on Linux a CLI tool or, on GNOME, the
`portal` backend. `primary=True` exists only where there is a PRIMARY
selection (X11 and most Wayland desktops) and raises on Windows and macOS.
On Wayland, reading a selection your own process owns is the case most likely
to surprise you — see
[troubleshooting.md](troubleshooting.md#the-clipboard-reads-back-empty).

## Keyboard, focus and tab order

```python
gui.type_text("Hello")  # characters
gui.send_keys("^(a)^(c)")  # Ctrl-A, Ctrl-C
gui.tap_key("Return")
gui.press_key("shift")
gui.release_key("shift")

gui.press_tab()  # advance focus
gui.press_tab(reverse=True)  # Shift+Tab

gui.assert_focused(name="Email")
gui.assert_tab_order(["Name", "Email", "Country", "Submit"])
```

`send_keys()` carries X11::GUITest's grammar (`^` Ctrl, `%` Alt, `+` Shift,
`#` Meta, `{TAB}` and friends). `quote_for_type()` escapes a string that
contains those characters literally.

**Check before you rely on focus.** Some desktops — GNOME Shell among them —
do not publish per-widget keyboard focus to the accessibility bus at all; the
shell holds it for the whole session. The assertions above are then correct
but can never match:

```python
if gui.focus_tracking_works():
    gui.assert_focused(name="Email")
```

## Scrolling and dragging

```python
gui.scroll(dy=3)  # three detents up
gui.scroll(dy=-3)  # three detents down
gui.scroll(dx=1)  # one detent right
```

Units are **whole wheel detents** on every backend, and **`dy > 0` is up** —
one convention everywhere, chosen because it is the one a reader guesses
right. [input.md](input.md#scrolling-detents-and-which-way-is-up) has the
reasoning and the per-backend detail.

Drag-and-drop needs motion the toolkit can actually see, so it is a stream of
events rather than a jump:

```python
gui.drag((120, 400), (600, 400))
gui.glide(600, 400, duration=0.4)  # move without pressing anything
```

## Asserting the UI is accessible at all

These are regression tests for the application's own accessibility, which is
what every element-based locator depends on:

```python
gui.assert_accessible(within=gui.window_element("Preferences"))
gui.assert_no_missing_accessible_names()
gui.assert_no_duplicate_accessible_names()
```

`assert_accessible()` is the two below it run together: it raises
`AccessibilityViolation` if any control in scope has no accessible name, or if
two controls of the same role share one. It checks the roles a user acts on
(buttons, entries, checkboxes and the like), not every node in the tree.

## Writing a test suite

`examples/06_a_real_test.py` is the file to copy. The shape:

```python
import unittest
import pyguitest
from pyguitest import Capability


class EditorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gui = pyguitest.connect()
        if not cls.gui.supports(Capability.ELEMENT_ACTION):
            raise unittest.SkipTest("this session has no accessible elements")

    @classmethod
    def tearDownClass(cls):
        cls.gui.close()

    def test_saving(self):
        with self.gui.capture_on_failure("artifacts"):
            ...
```

**Decide up front whether a missing capability is a skip or a failure**, and
say which at setup time rather than discovering it mid-assertion:

- `supports()` + `SkipTest`, as above, for a suite that runs on more than one
  kind of desktop. "This session cannot do that" is not "the application is
  broken", and conflating them makes the suite useless as a regression signal.
- `gui.require(Capability.ELEMENT_ACTION, ...)` for a suite pinned to one
  known environment (a CI image, say). It raises immediately, naming the
  missing capability, which is the better failure when the environment was
  supposed to be guaranteed.

## Pinning a minimum pyguitest version

A script that uses a method added in a recent release will, on a box with an
older pyguitest, fail *mid-run* with an `AttributeError` deep inside whatever
it was doing — a confusing failure that looks like a bug in the script. This
bites most when a script travels: copied to a test machine, a CI image, or a
colleague's checkout that has a different version installed.

Declare the floor at the top instead, so it fails on the first line with a
message naming both versions:

```python
import pyguitest

pyguitest.require_version("0.18.0")
gui = pyguitest.connect()
# or, equivalently, once connected: gui.require_version("0.18.0")
```

If the installed pyguitest is older, this raises `VersionTooOld` immediately
(`pyguitest.VersionTooOld`), saying what the script needs and what is
installed. The module-level `pyguitest.require_version()` and the session
method `gui.require_version()` do the same check; put the module-level one
first so it fails before `connect()` even runs. It is off by default — a
script that does not call it runs against whatever is present, as before — and
it checks only pyguitest's own version, not the capabilities of the desktop,
which is what `supports()` and `require()` above are for.

## Running in CI

Most of a GUI test suite needs a display server, and a CI runner usually
has none. Three arrangements work:

- **Xvfb** — a private X server. Everything works, including the tier-6
  readback operations, because it is a real X11 session.
- **A headless compositor** — `gnome-shell --headless` and friends. Closer to
  what users run; this repository drives one in
  [CONTRIBUTING.md](../CONTRIBUTING.md#the-headless-gnome-session).
- **Capability-gated tests** — let the suite skip what the runner cannot do:

  ```python
  @unittest.skipUnless(gui.supports(Capability.SCREEN_CAPTURE), "no capture")
  def test_screenshot(self): ...
  ```

Whatever you choose, run `pyguitest debug --json` in the job and keep the
output as a build artifact. When a test fails only in CI, that file is the
difference between a hypothesis and an answer.

## X11::GUITest cheat sheet

The most-used half of the module. `pyguitest migrate script.pl` reports the
tier, replacement and line numbers of every call in a real file — give it a
directory and it walks the Perl sources under it, `--json` for a CI step that
wants more than an exit status. It reads the code as Perl would, so a name in
a comment, POD, a string, a heredoc, a hash key, a method call or another
module's namespace is not a call and is not counted, and a sub of your own
that shares a name with an export (`sub WaitSeconds`) is reported as
shadowing it rather than as a use. It exits non-zero on the two kinds of call
a human has to decide about: the ones Wayland refuses, and the two with no
replacement anywhere (`GetScreenDepth`, `GetWindowsFromPid`). The full
50-export table lives in `pyguitest.compat`, alongside the `:CONST` integers
in `compat.CONSTANTS`, and is explained in
[developers/wayland-audit.md](developers/wayland-audit.md).

Most rows below port by renaming. The ones that do not are the ones where the
call still runs and quietly means something else, and `migrate` prints the
note for each — they are collected under the table.

| X11::GUITest | pyguitest | Note |
|---|---|---|
| `StartApp` / `RunApp` | `start_app` / `run_app` | `start_app` returns an `Application`, not a pid; `run_app` a `CompletedProcess`, not a wait status |
| `WaitSeconds` | `wait` | Prefer a `wait_for_*` |
| `SendKeys` | `send_keys` | Same grammar |
| `QuoteStringForSendKeys` | `quote_for_type` | Also escapes `#` (Meta), which the original missed. Like it, leaves `&` (AltGr) alone: write `{&}` |
| `PressKey` / `ReleaseKey` / `PressReleaseKey` | `press_key` / `release_key` / `tap_key` | |
| `MoveMouseAbs` | `move_mouse` | |
| `ClickMouseButton` | `click` | Buttons 4/5 are now `scroll()` |
| `SetEventSendDelay` / `SetKeySendDelay` | `Session(event_delay=, key_delay=)` | **Seconds, not milliseconds** |
| `FindWindowLike` | `find_windows` | Toplevels only, no recursive descent. A string is a case-sensitive literal substring; the original was a case-insensitive regex, so use `re.compile(..., re.I)` |
| `WaitWindowLike` / `WaitWindowClose` | `wait_for_window` / `wait_window_close` | Event-driven where the compositor allows. `timeout=None` waits forever; the originals gave up after 10 s |
| `WaitWindowViewable` | `wait_for_window`, then `is_window_viewable` | The original waited; `is_window_viewable` reads once |
| `GetWindowName` | `Window.title` | |
| `GetWindowPos` | `geometry` | |
| `MoveWindow` / `ResizeWindow` | `move_window` / `resize_window` | |
| `RaiseWindow` / `SetInputFocus` | `activate_window` | Raising and focusing are one operation now |
| `IconifyWindow` / `UnIconifyWindow` | `minimize_window(w)` / `minimize_window(w, minimized=False)` | |
| `GetWindowFromPoint` | `window_at` | |
| `ClickWindow` | `Element.click` | Prefer `gui.button(...).click()`. The original clicked the window's top-left corner plus an offset, see below |
| `GetRootWindow` / `GetChildWindows` / `GetParentWindow` | `root_element` / `Element.children` / `Element.parent` | The accessible tree, not the window tree |
| `GetScreenRes` / `ScreenCount` | `Screen.size` / `len(gui.screens())` | |
| `IsWindow` / `IsWindowViewable` | `is_window_open` / `is_window_viewable` | The second reads minimized state, not X11's `IsViewable` |
| `GetMousePos` | `pointer_position()` | X11, Windows and macOS; raises on Wayland |
| `IsKeyPressed` / `IsMouseButtonPressed` | `is_key_pressed` / `is_button_pressed` | X11 and Windows only; deliberately prevented on Wayland, where they raise |
| `SetWindowName` / `LowerWindow` / `IsWindowCursor` | `set_window_title` / `lower_window` / `is_window_cursor` | As above. `is_window_cursor` takes the integer the `XC_*` constants carried |
| `DEF_WAIT`, `M_LEFT`, `M_BTN1`…`M_BTN5`, `XC_*` | plain integers | `:CONST` exports nothing needing a pyguitest name: buttons go to `click()`, `M_UP`/`M_DOWN` become `scroll(dy=±1)`, cursor shapes pass through unchanged |

### Where a straight port changes meaning

None of these raise. Each runs and does something slightly different.

- **Delays are seconds.** `SetEventSendDelay(10)` and `SetKeySendDelay(50)`
  took milliseconds, returned the value they replaced, and started at 10 and
  0. `Session(event_delay=0.01, key_delay=0.05)` is the same thing, with both
  starting at `0.0`; leaving the numbers as they were makes every event
  wait ten *seconds* and every key fifty.
- **Window titles are not regexes.** `FindWindowLike('gedit')` matched the
  pattern `/gedit/i` against every title, so `Gedit` and `Untitled - gedit`
  both hit, and `FindWindowLike('foo.*bar')` meant what it looked like.
  `find_windows('gedit')` is a case-sensitive literal substring. Port a
  pattern as `re.compile('gedit', re.I)`; plain strings stay plain.
- **Waits return one window, or nothing.** The originals returned a list (an
  empty one on timeout) and gave up after `DEF_WAIT`, ten seconds.
  `wait_for_window` returns a `Window` or `None`, `wait_window_close` a
  `bool`, and both wait forever unless you pass `timeout=`, so a script that
  relied on the default needs `timeout=10`. `WaitWindowViewable` has no
  one-call spelling: `window = gui.wait_for_window(title, timeout=10)`, then
  poll `gui.is_window_viewable(window)` until it is true.
- **`ClickWindow` is a corner click.** It moved to the window's top-left from
  `GetWindowPos`, added the x/y offsets, clicked `M_LEFT` unless told
  otherwise and returned 1 or 0. The same thing is
  `x, y, _, _ = gui.geometry(window)`, then `gui.move_mouse(x + dx, y + dy)`
  and `gui.click()`. The corner is the decoration's, which on most desktops
  is not content, so where the control has an accessible name
  `gui.button("Save").click()` is both shorter and sturdier.
- **`StartApp` returned a pid or nothing.** It forked, slept one second and
  returned the pid only if the child was still alive. `start_app` returns at
  once with an `Application`; `app.pid` is the pid, and `app.poll() is None`
  after a pause is the survival check. `RunApp` returned `system()`'s wait
  status — the exit code times 256, or -1 when it could not start — so
  `run_app(...).returncode == 0` replaces `RunApp(...) == 0`, and any other
  comparison against a specific status needs the division.
- **`&` is not quoted.** `quote_for_type` keeps the original's omission of
  `&` (AltGr), so a `&` inside quoted text is still read as a modifier. Write
  it as `{&}` yourself where it must reach the screen as itself.

The `IsKeyPressed` row is the one that decides whether a port is possible at
all. If a script depends on reading global input state, it can be ported to
X11, XWayland and Windows — where these are ordinary API calls — but not to
Wayland, and no backend will ever change that, because preventing it is the
point. `GetMousePos` is the exception inside that set: the pointer's position
is readable on macOS too — a plain `CGEventGetLocation` read needing no grant
— which is why `api.md`'s capability table lists three backends for
`POINTER_QUERY` and two for the rest.
