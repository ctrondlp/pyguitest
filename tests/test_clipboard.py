"""ToolClipboardBackend: dispatch to the right read/write argv per tool.

The interesting behaviour is not "does it build a command line" but that
text goes over stdin/stdout rather than argv -- so a caller never needs to
shell-quote clipboard content, and there is no argv length limit -- and
that a plain blocking write is correct despite the underlying tool
forking into the background (see the module docstring in clipboard.py).
"""

import subprocess
import unittest
from unittest import mock

from pyguitest import tools
from pyguitest.backends.clipboard import ToolClipboardBackend
from pyguitest.capabilities import Capability
from pyguitest.errors import CapabilityUnsupported, PyGUITestError

BY_NAME = {t.name: t for t in tools.CLIPBOARD_TOOLS}


class Recorder:
    """Records the argv and stdin of every call, and returns canned stdout."""

    def __init__(self, stdout=""):
        self.calls = []
        self.stdout = stdout

    def __call__(self, argv, input_text=None):
        self.calls.append((argv, input_text))
        return self.stdout


class TestClipboardDispatch(unittest.TestCase):
    def _backend(self, name, stdout=""):
        self.runner = Recorder(stdout=stdout)
        return ToolClipboardBackend(BY_NAME[name], runner=self.runner)

    def test_wl_copy_read(self):
        gui = self._backend("wl-copy", stdout="hello")
        self.assertEqual(gui.get_clipboard(), "hello")
        self.assertEqual(self.runner.calls[0], (["wl-paste", "--no-newline"], None))

    def test_wl_copy_write(self):
        gui = self._backend("wl-copy")
        gui.set_clipboard("hello")
        self.assertEqual(self.runner.calls[0], (["wl-copy"], "hello"))

    def test_xclip_read(self):
        gui = self._backend("xclip", stdout="hello")
        self.assertEqual(gui.get_clipboard(), "hello")
        self.assertEqual(
            self.runner.calls[0],
            (["xclip", "-selection", "clipboard", "-out"], None),
        )

    def test_xclip_write(self):
        gui = self._backend("xclip")
        gui.set_clipboard("hello")
        self.assertEqual(
            self.runner.calls[0], (["xclip", "-selection", "clipboard"], "hello")
        )

    def test_xsel_read(self):
        gui = self._backend("xsel", stdout="hello")
        self.assertEqual(gui.get_clipboard(), "hello")
        self.assertEqual(
            self.runner.calls[0], (["xsel", "--clipboard", "--output"], None)
        )

    def test_xsel_write(self):
        gui = self._backend("xsel")
        gui.set_clipboard("hello")
        self.assertEqual(
            self.runner.calls[0], (["xsel", "--clipboard", "--input"], "hello")
        )

    def test_name_includes_the_tool(self):
        gui = self._backend("wl-copy")
        self.assertEqual(gui.name, "clipboard:wl-copy")

    def test_capabilities_is_clipboard_only(self):
        gui = self._backend("xclip")
        self.assertEqual(gui.capabilities, {Capability.CLIPBOARD})

    def test_an_unmapped_tool_raises_at_construction(self):
        fake = tools.ExternalTool("definitely-not-a-real-clipboard-tool", frozenset())
        with self.assertRaises(PyGUITestError):
            ToolClipboardBackend(fake)


class TestClipboardRequiresTheCapability(unittest.TestCase):
    """Both methods refuse before running anything.

    Mirrors every other backend's guard, on a fresh backend that somehow
    lost the capability.
    """

    def test_get_clipboard_checks_first(self):
        gui = ToolClipboardBackend(BY_NAME["xclip"], runner=Recorder())
        gui.require = mock.Mock(
            side_effect=CapabilityUnsupported(Capability.CLIPBOARD, gui.name)
        )
        with self.assertRaises(CapabilityUnsupported):
            gui.get_clipboard()

    def test_set_clipboard_checks_first(self):
        gui = ToolClipboardBackend(BY_NAME["xclip"], runner=Recorder())
        gui.require = mock.Mock(
            side_effect=CapabilityUnsupported(Capability.CLIPBOARD, gui.name)
        )
        with self.assertRaises(CapabilityUnsupported):
            gui.set_clipboard("hello")


class TestClipboardRunFailures(unittest.TestCase):
    """The real (non-injected) _run: timeout and non-zero exit both raise."""

    def _backend(self):
        return ToolClipboardBackend(BY_NAME["xclip"])

    def test_a_hang_raises_with_the_timeout_named(self):
        gui = self._backend()
        with mock.patch(
            "pyguitest.backends.clipboard.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="xclip", timeout=15),
        ):
            with self.assertRaises(PyGUITestError) as ctx:
                gui.get_clipboard()
            self.assertIn("did not finish", str(ctx.exception))

    def test_a_nonzero_exit_raises_with_stderr(self):
        gui = self._backend()
        with mock.patch(
            "pyguitest.backends.clipboard.subprocess.run",
            return_value=mock.Mock(returncode=1, stdout="", stderr="no display"),
        ):
            with self.assertRaises(PyGUITestError) as ctx:
                gui.get_clipboard()
            self.assertIn("no display", str(ctx.exception))

    def test_text_is_passed_on_stdin_not_argv(self):
        # Arbitrary clipboard content -- including shell metacharacters --
        # must never need quoting, and there is no argv length limit to hit.
        gui = self._backend()
        with mock.patch(
            "pyguitest.backends.clipboard.subprocess.run",
            return_value=mock.Mock(returncode=0, stdout=""),
        ) as run:
            gui.set_clipboard("$(rm -rf /) and a very long string" * 100)
            _args, kwargs = run.call_args
            self.assertNotIn("$(rm -rf /)", run.call_args.args[0])
            self.assertTrue(kwargs["input"].startswith("$(rm -rf /)"))


if __name__ == "__main__":
    unittest.main()
