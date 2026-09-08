# Notes for AI assistants writing pyguitest code

Rules for a coding assistant generating or reviewing pyguitest scripts. They
are written to be pasted into a `CLAUDE.md`, a Copilot instructions file, or a
system prompt. Humans get more use out of
[getting-started.md](getting-started.md) and [recipes.md](recipes.md).

The short version: **pyguitest is a capability-negotiated library, and code
that assumes a fixed desktop is the characteristic failure.** Most of the
rules below are one shape of that mistake.

## Rules

1. **Never `time.sleep()`.** Use `wait_for_window`, `wait_for_element`,
   `wait_until_gone`, `wait_window_close`, `wait_for_file`,
   `wait_for_process`, `wait_for_idle`, or `wait_until` with a predicate.
   A sleep is the single most common cause of a flaky GUI test. `gui.wait()`
   exists but is a last resort, not a default.

2. **Prefer a named element to a coordinate.** `gui.button("Save").click()`
   over `gui.move_mouse(x, y); gui.click()`. The ladder, best first:
   `gui.button(...)` / `gui.text_field(...)` → `gui.element(role=, name=,
   ...)` → `within=gui.window_element(title)` → `gui.locate_image(...)` →
   raw coordinates. Only descend when the step above genuinely cannot match.

3. **Check a capability before depending on it.** Anything beyond elements
   and process control varies by desktop:

   ```python
   if gui.supports(Capability.WINDOW_GEOMETRY):
       x, y, w, h = gui.geometry(window)
   ```

   In a test suite, decide at setup: `gui.require(...)` when the environment
   is guaranteed, `supports()` plus a skip when the suite must run on more
   than one kind of desktop. Never wrap a capability failure in a bare
   `except`.

4. **A typo in a method name is not a type error.** `Session` forwards
   unknown attributes to the backend, so `gui.pointer_postion()` type-checks
   clean and raises at runtime. Do not rely on a type checker to catch an
   invented method — check the name against [api.md](api.md).

5. **Do not invent methods, capabilities or roles.** Every public name is in
   [api.md](api.md), which is generated from the source. If a plausible
   method is not there, it does not exist — say so rather than emitting it.

6. **Window titles are regexes, not literals.** `find_window`,
   `find_windows` and `wait_for_window` all match a regex. Titles carry
   document names and modification markers, so anchor loosely and escape
   anything literal.

7. **Set text through the element, not the keyboard.**
   `gui.text_field("Name").set_text("Ada")` needs no focus and no keyboard
   layout. Use `type_text()` only when the application must observe
   individual keystrokes.

8. **Re-query elements; do not cache them across UI changes.** An `Element`
   wraps a live accessibility node with no staleness policy — if the
   application redraws and destroys the node, every access raises. Look it up
   again after anything that changes the screen. `Window` is different: it
   has `gui.refresh_window()` and `gui.is_window_open()`.

9. **`scroll()` takes whole wheel detents, and `dy > 0` is up.** The same on
   every backend. Do not pass pixel deltas.

10. **Capture failures with `capture_on_failure`, not a hand-written
    `except`.** By the time an `except` block runs the application is
    usually gone:

    ```python
    with gui.capture_on_failure("artifacts"):
        ...
    ```

11. **Close the session.** Use `with pyguitest.connect() as gui:` or call
    `gui.close()`. Same for `gui.start_app(...)`, which returns an
    `Application` usable as a context manager.

12. **Do not assume X11.** `pointer_position()`, `is_key_pressed()`,
    `is_button_pressed()`, `set_window_title()`, `lower_window()` and
    `is_window_cursor()` are X11 and XWayland only — no Wayland compositor
    will ever serve them, by design. If a task needs one of these, say that
    it constrains the script to X11 rather than emitting it silently.

13. **Do not hardcode screen dimensions.** Ask: `gui.screens()`, and use
    `Screen.size`. Multi-monitor layouts and fractional scaling make any
    literal wrong somewhere.

14. **`send_keys()` has a grammar.** `^` Ctrl, `%` Alt, `+` Shift, `#` Meta,
    `{TAB}` and friends. For text that contains those characters literally,
    use `quote_for_type()` or `type_text()`.

15. **Prefer the accessibility layer over image matching.** `locate_image()`
    is real and useful, but it breaks on theme, font and scaling changes.
    Reach for it only when there is nothing accessible to match.

## A correct script, end to end

```python
import unittest

import pyguitest
from pyguitest import Capability


class SaveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gui = pyguitest.connect()
        if not cls.gui.supports(Capability.ELEMENT_ACTION):
            raise unittest.SkipTest("no accessible elements on this session")

    @classmethod
    def tearDownClass(cls):
        cls.gui.close()

    def test_saves_the_document(self):
        gui = self.gui
        with gui.start_app(["gnome-text-editor"]) as app:
            window = gui.wait_for_window(r"Text Editor", timeout=10)

            with gui.capture_on_failure("artifacts"):
                gui.text_field("Document").set_text("hello")
                gui.button("Save").click()

                saved = gui.wait_for_element(name="Saved", timeout=10)
                self.assertIsNotNone(saved)

            gui.wait_for_idle(app.pid, timeout=10)


if __name__ == "__main__":
    unittest.main()
```

Every rule above is visible in that file: no sleeps, named elements, a
capability check at setup, a regex window title, a context-managed session
and application, and failure evidence captured while the failure is still on
screen.

## What to tell the user when the desktop is the problem

If a script cannot be written robustly because the application publishes no
accessible names, the fix belongs in that application, not in more clever
locators. Point at
[testable-guis.md](https://github.com/ctrondlp/pyguitest-recorder/blob/main/docs/testable-guis.md),
which is written to be handed to its developers, and fall back to coordinates
in the meantime — saying explicitly that you have done so and why.
