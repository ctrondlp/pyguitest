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
from pyguitest.backends.clipboard import (
    _FORKS_ON_WRITE,
    _READ,
    _WRITE,
    ToolClipboardBackend,
)
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

    def test_pbcopy_read(self):
        # macOS. No flag at all: `pbpaste` writes the pasteboard's text to
        # stdout, and has no --no-newline equivalent because it never adds
        # one -- measured live on macOS 26.7, where `printf abc123 | pbcopy`
        # then `pbpaste` returned exactly "abc123".
        gui = self._backend("pbcopy", stdout="hello")
        self.assertEqual(gui.get_clipboard(), "hello")
        self.assertEqual(self.runner.calls[0], (["pbpaste"], None))

    def test_pbcopy_write(self):
        gui = self._backend("pbcopy")
        gui.set_clipboard("hello")
        self.assertEqual(self.runner.calls[0], (["pbcopy"], "hello"))

    def test_wl_copy_read_primary(self):
        gui = self._backend("wl-copy", stdout="hello")
        self.assertEqual(gui.get_clipboard(primary=True), "hello")
        self.assertEqual(
            self.runner.calls[0],
            (["wl-paste", "--no-newline", "--primary"], None),
        )

    def test_wl_copy_write_primary(self):
        gui = self._backend("wl-copy")
        gui.set_clipboard("hello", primary=True)
        self.assertEqual(self.runner.calls[0], (["wl-copy", "--primary"], "hello"))

    def test_xclip_read_primary(self):
        gui = self._backend("xclip", stdout="hello")
        self.assertEqual(gui.get_clipboard(primary=True), "hello")
        self.assertEqual(
            self.runner.calls[0],
            (["xclip", "-selection", "primary", "-out"], None),
        )

    def test_xclip_write_primary(self):
        gui = self._backend("xclip")
        gui.set_clipboard("hello", primary=True)
        self.assertEqual(
            self.runner.calls[0], (["xclip", "-selection", "primary"], "hello")
        )

    def test_xsel_read_primary(self):
        gui = self._backend("xsel", stdout="hello")
        self.assertEqual(gui.get_clipboard(primary=True), "hello")
        self.assertEqual(
            self.runner.calls[0], (["xsel", "--primary", "--output"], None)
        )

    def test_xsel_write_primary(self):
        gui = self._backend("xsel")
        gui.set_clipboard("hello", primary=True)
        self.assertEqual(
            self.runner.calls[0], (["xsel", "--primary", "--input"], "hello")
        )

    def test_primary_and_clipboard_are_independent_calls(self):
        # Writing PRIMARY must not touch the clipboard argv, or vice versa --
        # the two selections are independent on every real desktop, and a
        # backend that conflated them would silently break that.
        gui = self._backend("xclip")
        gui.set_clipboard("for the clipboard", primary=False)
        gui.set_clipboard("for primary", primary=True)
        self.assertEqual(
            [call[0] for call in self.runner.calls],
            [
                ["xclip", "-selection", "clipboard"],
                ["xclip", "-selection", "primary"],
            ],
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


class TestClipboardPrimaryIsRefusedOnMacos(unittest.TestCase):
    """macOS has one pasteboard, so primary=True is a typed refusal.

    Not an alias to the clipboard: the two selections are independent
    everywhere they do exist, so answering one from the other would be
    answering a different question than the caller asked -- the read
    portal.py makes for the Clipboard interface, which has no PRIMARY either.
    """

    def _backend(self):
        self.runner = Recorder()
        return ToolClipboardBackend(BY_NAME["pbcopy"], runner=self.runner)

    def test_get_clipboard_primary_raises_before_running_anything(self):
        gui = self._backend()
        with self.assertRaises(CapabilityUnsupported) as ctx:
            gui.get_clipboard(primary=True)
        self.assertIn("PRIMARY", str(ctx.exception))
        self.assertEqual(self.runner.calls, [])

    def test_set_clipboard_primary_raises_before_running_anything(self):
        gui = self._backend()
        with self.assertRaises(CapabilityUnsupported) as ctx:
            gui.set_clipboard("hello", primary=True)
        self.assertIn("PRIMARY", str(ctx.exception))
        self.assertEqual(self.runner.calls, [])

    def test_the_refusal_is_about_the_tool_that_cannot_serve_it(self):
        gui = self._backend()
        with self.assertRaises(CapabilityUnsupported) as ctx:
            gui.get_clipboard(primary=True)
        self.assertIn("clipboard:pbcopy", str(ctx.exception))

    def test_the_linux_tools_are_unaffected(self):
        # The refusal is keyed on the tool's own tables, not on the platform
        # this test happens to run on: every Linux tool keeps reaching PRIMARY.
        for name in ("wl-copy", "xclip", "xsel"):
            self.assertIn(True, _READ[name])
            self.assertIn(True, _WRITE[name])
        self.assertEqual(_READ["pbcopy"], {False: ["pbpaste"]})
        self.assertEqual(_WRITE["pbcopy"], {False: ["pbcopy"]})


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


class TestClipboardWriteCaptureFollowsTheFork(unittest.TestCase):
    """_FORKS_ON_WRITE: DEVNULL only where a write really forks.

    The Linux tools daemonize to keep serving the selection, so their writes
    must not be given pipes a fork would hold open -- see the module
    docstring on the 15-second hang that confirmed it. macOS's `pbcopy` is an
    ordinary client talking to the pasteboard server, so it keeps captured
    stderr and a failure there says why.
    """

    def _failure(self, name):
        gui = ToolClipboardBackend(BY_NAME[name])
        with mock.patch(
            "pyguitest.backends.clipboard.subprocess.run",
            return_value=mock.Mock(
                returncode=1, stdout="", stderr="pbs: pasteboard unavailable"
            ),
        ) as run:
            with self.assertRaises(PyGUITestError) as ctx:
                gui.set_clipboard("hello")
            return str(ctx.exception), run.call_args.kwargs

    def test_a_forking_write_is_given_devnull(self):
        message, kwargs = self._failure("wl-copy")
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertIn("stderr not captured", message)

    def test_a_non_forking_write_keeps_stderr(self):
        message, kwargs = self._failure("pbcopy")
        self.assertEqual(kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(kwargs["stderr"], subprocess.PIPE)
        self.assertIn("pbs: pasteboard unavailable", message)

    def test_every_mapped_tool_is_covered_by_one_side_or_the_other(self):
        # A new tool must land in _FORKS_ON_WRITE or be documented as not
        # forking; this pins that the decision was made rather than defaulted.
        self.assertEqual(_FORKS_ON_WRITE & set(_WRITE), _FORKS_ON_WRITE)
        self.assertIn("pbcopy", set(_WRITE) - _FORKS_ON_WRITE)


if __name__ == "__main__":
    unittest.main()
