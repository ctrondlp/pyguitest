#!/usr/bin/env python3
"""Use pyguitest inside an actual test suite.

The gap the other examples leave. This is a GUI *testing* library, and
every other script here is a demonstration that runs top to bottom and
prints things -- which is not how anyone will actually use it.

Three things are worth copying from this file:

1. **Skip, do not fail, on a missing capability.** What a desktop can do
   varies by compositor, so a suite that runs on more than one machine has
   to treat "this session cannot do that" as different from "the
   application is broken". `supports()` is the whole reason the capability
   surface is public.

2. **Screenshot the failure while it is still on screen.** By the time an
   `except:` block or a `tearDown` runs, the app under test is usually
   gone. `capture_on_failure` shoots during the exception's propagation and
   attaches the path to the exception, so the runner still reports the
   original failure and there is an image beside it.

3. **Wait for a condition, never sleep.** A fixed sleep is a bet on how
   slow the machine is today. Every wait_* method here polls a predicate
   with a deadline instead.

Run it directly:

    PYTHONPATH=src python3 examples/06_a_real_test.py -v

Not through `unittest discover`, which cannot import it: discovery derives
a module name from the filename, and `06_a_real_test` is not a valid Python
identifier because it starts with a digit. It reports "NO TESTS RAN" rather
than an error, which is worth knowing before you conclude the file is
broken. Your own tests will not be numbered, so this only affects the
examples.

It drives a text editor, so it needs a desktop; on a session that cannot
list windows it skips rather than failing.
"""

import os
import sys
import unittest

import pyguitest
from pyguitest import Capability

EDITOR = os.environ.get("PYGUITEST_EDITOR", "gedit")
ARTIFACTS = os.environ.get("PYGUITEST_SCREENSHOT_DIR", "artifacts")
STARTUP_TIMEOUT = 30


class EditorTest(unittest.TestCase):
    """A small suite against a real text editor."""

    @classmethod
    def setUpClass(cls):
        """Connect once, and refuse early if this desktop cannot help."""
        cls.gui = pyguitest.connect(key_delay=0.02)
        # Checked here rather than in each test: if the session cannot do
        # this at all, every test below is meaningless and should say so
        # once instead of failing four times for the same reason.
        if not cls.gui.supports(Capability.WINDOW_LIST):
            raise unittest.SkipTest(
                "this desktop cannot list windows; run examples/01 to see why"
            )

    @classmethod
    def tearDownClass(cls):
        """Release the backend's resources."""
        cls.gui.close()

    def setUp(self):
        """Start the editor and wait for its window."""
        self.process = self.gui.start_app([EDITOR])
        self.addCleanup(self.process.terminate)
        window = self.gui.wait_for_window(EDITOR, timeout=STARTUP_TIMEOUT)
        if window is None:
            self.fail(f"{EDITOR} opened no window within {STARTUP_TIMEOUT}s")
        self.window = window

    def test_the_window_is_actually_showing(self):
        """A window that exists is not necessarily on screen."""
        if not self.gui.supports(Capability.WINDOW_STATE):
            self.skipTest("no WINDOW_STATE on this session")
        self.assertTrue(self.gui.is_window_viewable(self.window))

    def test_typing_reaches_the_document(self):
        """Type, then assert on the accessible tree rather than a sleep."""
        if not self.gui.supports(Capability.ELEMENT_TREE):
            self.skipTest("no AT-SPI here; see the atspi extra in the README")

        # The capture is armed around the part that can fail. Nothing is
        # written when this passes.
        with self.gui.capture_on_failure(ARTIFACTS, name="typing"):
            if self.gui.supports(Capability.WINDOW_ACTIVATE):
                self.gui.activate_window(self.window)
            self.gui.type_text("hello from pyguitest")

            # Not `gui.wait(1)`. The editor may be slow to render on a
            # loaded machine and instant on an idle one; a deadline covers
            # both, and returns as soon as the condition holds.
            found = self.gui.wait_until(
                lambda: any(
                    "hello from pyguitest" in (getattr(e, "text", "") or "")
                    for e in self.gui.elements(role="text")
                ),
                timeout=10,
            )
            self.assertTrue(found, "typed text never appeared in the document")

    def test_the_window_can_be_resized(self):
        """Geometry round-trips, where the compositor allows it at all."""
        for capability in (Capability.WINDOW_RESIZE, Capability.WINDOW_GEOMETRY):
            if not self.gui.supports(capability):
                self.skipTest(f"no {capability.name} on this session")

        with self.gui.capture_on_failure(ARTIFACTS, name="resize"):
            self.gui.resize_window(self.window, 800, 600)
            # Resizing is asynchronous on Wayland: the compositor proposes
            # the size and the client acks it on its own schedule, so the
            # read-back needs a deadline rather than an immediate assert.
            settled = self.gui.wait_until(
                lambda: self.gui.geometry(self.window)[2:] == (800, 600),
                timeout=5,
            )
            self.assertTrue(settled, f"stayed at {self.gui.geometry(self.window)[2:]}")


if __name__ == "__main__":
    # Also runnable directly, which unittest.main() handles.
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
