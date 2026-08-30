"""pyguitest -- cross-platform GUI automation.

Successor to X11::GUITest. The shape of this API follows an audit of all 50 of
that module's exports against Wayland (docs/wayland-audit.html), whose finding
was that a faithful port is the wrong target: 13 functions carry over unchanged,
6 have no path on any compositor, and the rest change shape.

Two things follow, and both are visible in the API.

Capability negotiation is public. Nineteen of the legacy functions vary by
desktop, so a call cannot just return zero on failure -- callers ask first:

    import pyguitest
    from pyguitest import Capability

    gui = pyguitest.connect()
    if gui.supports(Capability.WINDOW_GEOMETRY):
        ...

Elements lead, coordinates follow. AT-SPI answers what the window-tree walk was
really used for, needs neither geometry nor injection permission, and behaves
identically under X11 and Wayland -- the one layer with no backend matrix.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Literal

from . import backends, compat
from .backends import Element, GUIBackend, ImageMatch, NullBackend, Screen, Window
from .capabilities import TIERS, Capability, CapabilitySet, Tier
from .errors import (
    BackendUnavailable,
    CapabilityUnsupported,
    ElementNotFound,
    ImageNotFound,
    PermissionRequired,
    PyGUITestError,
    WindowNotFound,
)
from .roles import Role
from .session import Compositor, Environment, SessionType, detect

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import TracebackType

    # Annotation-only: backends.windows pulls in the compositor IPC clients,
    # and importing it here would make that eager for every user of the
    # package, including the ones on a backend that never touches it.
    from .backends.windows import WindowEvent

__version__ = "0.1.1"

__all__ = [
    "connect",
    "Session",
    "detect",
    "quote_for_type",
    "Role",
    "Capability",
    "CapabilitySet",
    "Tier",
    "TIERS",
    "Environment",
    "SessionType",
    "Compositor",
    "GUIBackend",
    "Element",
    "Window",
    "Screen",
    "ImageMatch",
    "NullBackend",
    "PyGUITestError",
    "BackendUnavailable",
    "CapabilityUnsupported",
    "ElementNotFound",
    "ImageNotFound",
    "PermissionRequired",
    "WindowNotFound",
    "backends",
    "compat",
]

# The characters send_keys treats as syntax. Preserved from X11::GUITest so
# existing scripts' quoted strings keep working -- with one fix: the original
# QuoteStringForSendKeys omitted # (Meta) despite it being a modifier there
# too, so a literal # slipped through unescaped. & (AltGr) is excluded on
# purpose, matching upstream: escape it manually with "{&}" if you need one.
_SENDKEYS_SPECIAL = re.compile(r"([\^%+~#(){}])")


def quote_for_type(text: str | None) -> str | None:
    """Escape the characters `send_keys` reads as syntax.

    Replaces X11::GUITest's QuoteStringForSendKeys, with the same behaviour
    plus the # fix noted above: each of ^ % + ~ # ( ) { } is wrapped in
    braces, so send_keys(quote_for_type(text)) sends `text` literally.
    """
    if text is None:
        return None
    return _SENDKEYS_SPECIAL.sub(r"{\1}", text)


SESSION_CAPABILITIES = frozenset({Capability.PROCESS_LAUNCH, Capability.TIMING})
"""Capabilities Session implements itself, on any backend.

They never touched a display server, so they are available even when no backend
could be found at all. Capability.IMAGE_LOCATE is tier-PORTABLE for the same
"no display server" reason but is deliberately not listed here: it still
needs `compare` on PATH, so unlike these two it is not always available and
stays backend-provided.
"""


class Session:
    """A connected automation session.

    Wraps a backend and the environment it was chosen for. The tier-1
    operations -- process launch and timing -- are implemented here, because
    they never involved a display server and so never vary by backend.
    """

    def __init__(
        self,
        backend: GUIBackend,
        environment: Environment,
        event_delay: float = 0.0,
        key_delay: float = 0.0,
    ) -> None:
        """Wrap `backend` with the `environment` it was selected for.

        `event_delay` is the pause in seconds after each discrete input event,
        and `key_delay` the pause between characters when typing. Both replace
        X11::GUITest's global delay settings, which were module state there.
        """
        self.backend = backend
        self.environment = environment
        self.event_delay = event_delay
        self.key_delay = key_delay

    # -- negotiation -------------------------------------------------------

    @property
    def capabilities(self) -> CapabilitySet:
        """Everything this session can do.

        The backend's capabilities plus the tier-1 ones Session implements
        directly -- process launch and timing work regardless of which backend
        was selected, or whether one was found at all.
        """
        return CapabilitySet(set(self.backend.capabilities) | SESSION_CAPABILITIES)

    def supports(self, capability: Capability) -> bool:
        """Whether this session provides `capability`."""
        return capability in self.capabilities

    def require(self, *capabilities: Capability) -> None:
        """Raise unless every capability is available.

        For declaring a test suite's needs up front rather than failing part
        way through a run.
        """
        for capability in capabilities:
            if capability not in self.capabilities:
                self.backend.require(capability)

    def report(self) -> str:
        """A support table for this session, for logs and bug reports.

        Appends the backend's own provider breakdown when it offers one --
        CompositeBackend.report() shows which member serves which
        capability, which was otherwise unreachable from here even though
        Session.report() is the CLI's whole `pyguitest` output.
        """
        text = (
            f"{self.environment.summary()}\n"
            f"backend      {self.backend.name}\n\n"
            f"{self.capabilities.report()}"
        )
        backend_report = getattr(self.backend, "report", None)
        if callable(backend_report):
            text += f"\n\n{backend_report()}"
        return text

    # -- tier 1: no display server involved --------------------------------

    def start_app(
        self, command: str | Sequence[str], **kwargs: Any
    ) -> subprocess.Popen[Any]:
        """Launch a program without waiting. Replaces StartApp."""
        kwargs.setdefault("shell", isinstance(command, str))
        return subprocess.Popen(command, **kwargs)

    def run_app(
        self, command: str | Sequence[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[Any]:
        """Run a program to completion. Replaces RunApp."""
        kwargs.setdefault("shell", isinstance(command, str))
        return subprocess.run(command, **kwargs)

    def wait(self, seconds: float) -> None:
        """Sleep for `seconds`. Replaces WaitSeconds."""
        time.sleep(seconds)

    # -- input, with the session's delays applied --------------------------

    def _after_event(self) -> None:
        """Pause for `event_delay` after an input event."""
        if self.event_delay:
            time.sleep(self.event_delay)

    def move_mouse(self, x: int, y: int, screen: int = 0) -> None:
        """Move the pointer to an absolute position."""
        self.backend.move_mouse(x, y, screen)
        self._after_event()

    def press_button(self, button: int) -> None:
        """Press a mouse button. 1 is left, 2 middle, 3 right."""
        self.backend.press_button(button)
        self._after_event()

    def release_button(self, button: int) -> None:
        """Release a mouse button."""
        self.backend.release_button(button)
        self._after_event()

    def click(self, button: int = 1) -> None:
        """Press and release a mouse button. Replaces ClickMouseButton."""
        self.press_button(button)
        self.release_button(button)

    def scroll(self, dx: int = 0, dy: int = 0) -> None:
        """Scroll by axis steps."""
        self.backend.scroll(dx, dy)
        self._after_event()

    def press_key(self, key: str) -> None:
        """Press a key by name, without releasing it."""
        self.backend.press_key(key)
        self._after_event()

    def release_key(self, key: str) -> None:
        """Release a key by name."""
        self.backend.release_key(key)
        self._after_event()

    def tap_key(self, key: str) -> None:
        """Press and release a key. Replaces PressReleaseKey."""
        self.press_key(key)
        self.release_key(key)

    def type_text(self, text: str, delay: float | None = None, **kwargs: Any) -> None:
        """Type `text`, using the session's `key_delay` unless overridden."""
        delay = self.key_delay if delay is None else delay
        self.backend.type_text(text, delay=delay, **kwargs)

    def send_keys(self, keys: str) -> None:
        r"""Send literal text and key combinations in one string.

        Replaces X11::GUITest's SendKeys and its `{}` grammar -- see
        quote_for_type for the escaping half of this contract. Special
        characters:

            ^ % + # &   modifiers: Ctrl, Alt, Shift, Meta, AltGr
            ~ or \n     Enter
            ( )         group the following keys under a modifier
            { }         escape a special character, name a key by
                        abbreviation, repeat one, or pause

        Everything else is sent as literal text. A modifier not immediately
        followed by `(` is pressed and released on its own, combined with
        nothing:

            gui.send_keys("Hello, how are you?\n")
            gui.send_keys("%(f)q")        # Alt-f, then plain q
            gui.send_keys("^(+(l))")      # Ctrl-Shift-l
            gui.send_keys("+(abc)")       # Shift held: ABC
            gui.send_keys("{BAC}")        # Backspace
            gui.send_keys("{F1 F2 F3}")   # F1, F2, F3
            gui.send_keys("{TAB 3}")      # Tab, three times
            gui.send_keys("{PAUSE 500}")  # sleep 500ms

        Key names inside `{}` are looked up case-insensitively in the active
        backend's KEY_ALIASES, or else passed to press_key as written, so an
        unabbreviated name works too: `{BackSpace}` in place of `{bac}`.

        This is the classic ASCII grammar, faithfully ported -- not a
        keymap-aware text-entry path. Use type_text for arbitrary or
        non-ASCII text; this raises ValueError for a character with no
        static key mapping (see GUIBackend.resolve_char_key), for malformed
        `{}` syntax, and for a `{}` key name this backend does not resolve.
        """
        modifiers = self.backend.MODIFIER_KEYS
        aliases = self.backend.KEY_ALIASES
        # Not .get(): every backend's MODIFIER_KEYS names a Shift key, and
        # press_literal cannot type a shifted character without one. Missing
        # it is a broken backend, and better reported here than as a
        # TypeError inside its key lookup halfway through the string.
        shift_name = modifiers["+"]
        held: list[str] = []
        grouped = False

        def release_held() -> None:
            nonlocal grouped
            for name in reversed(held):
                self.release_key(name)
            held.clear()
            grouped = False

        def press_literal(char: str) -> None:
            name, needs_shift = self.backend.resolve_char_key(char)
            auto_shift = needs_shift and shift_name not in held
            if auto_shift:
                self.press_key(shift_name)
            self.tap_key(name)
            if auto_shift:
                self.release_key(shift_name)

        def process_brace(content: str) -> None:
            tokens = [t for t in content.split(" ") if t]
            if not tokens:
                raise ValueError("empty {} in send_keys")
            pending_pause = False
            # (callable, key argument) for a repeat count. Annotated because
            # the two callables assigned below name their parameter
            # differently, which is enough to stop the type being inferred.
            last_action: tuple[Callable[[str], None], str] | None = None
            for token in tokens:
                if token.isdigit():
                    count = int(token)
                    if count <= 0:
                        raise ValueError(
                            f"non-positive repeat count {token!r} in send_keys"
                        )
                    if pending_pause:
                        self.wait(count / 1000)
                        pending_pause = False
                    elif last_action is not None:
                        action, arg = last_action
                        for _ in range(count - 1):
                            action(arg)
                    else:
                        raise ValueError(
                            f"repeat count {token!r} with no preceding key"
                        )
                    continue
                if token.upper() == "PAUSE":
                    pending_pause = True
                    continue
                pending_pause = False
                if len(token) == 1:
                    last_action = (press_literal, token)
                else:
                    last_action = (self.tap_key, aliases.get(token.upper(), token))
                last_action[0](last_action[1])

        i, n = 0, len(keys)
        while i < n:
            ch = keys[i]
            if ch == "{":
                end = keys.find("}", i + 1)
                if end == -1:
                    raise ValueError(f"unterminated '{{' in send_keys at position {i}")
                if keys[end + 1 : end + 2] == "}":
                    end += 1
                process_brace(keys[i + 1 : end])
                i = end + 1
                continue
            if ch == "~":
                self.tap_key(aliases["ENT"])
            elif ch in modifiers:
                name = modifiers[ch]
                if i + 1 < n and keys[i + 1] == "(":
                    self.press_key(name)
                    held.append(name)
                    grouped = True
                    i += 2
                    continue
                self.tap_key(name)
            elif ch == "(":
                grouped = True
            elif ch == ")":
                release_held()
                i += 1
                continue
            else:
                press_literal(ch)
            i += 1
            if not grouped:
                release_held()

    # -- finding things ----------------------------------------------------

    def windows(self) -> list[Window]:
        """Return every open window."""
        return self.backend.windows()

    def find_windows(self, title: str) -> list[Window]:
        """Return every window whose title matches the `title` regex."""
        pattern = re.compile(title)
        return [w for w in self.backend.windows() if pattern.search(w.title)]

    def find_window(self, title: str) -> Window:
        """Return the first window matching the `title` regex.

        Raises WindowNotFound if nothing matches, so a script stops where the
        mistake is.
        """
        found = self.find_windows(title)
        if not found:
            raise WindowNotFound(f"no window with a title matching {title!r}")
        return found[0]

    def wait_for_window(
        self, title: str, timeout: float | None = None, interval: float = 0.5
    ) -> Window | None:
        """Block until a window matching the `title` regex appears.

        Delegates to the backend's own event-driven implementation where
        Capability.WINDOW_EVENTS is available (sway today) -- real
        notification, not polling. Everywhere else, this polls find_windows
        every `interval` seconds instead, so a script does not need to know
        which case it is in, or hand-roll the poll loop itself: checking for
        WINDOW_EVENTS and falling back to a fixed sleep was exactly the
        mistake examples/04_drive_an_editor.py made before this existed.

        `timeout` bounds the wait in seconds; None waits indefinitely.
        Returns the matched Window, or None if `timeout` elapses first --
        not WindowNotFound, since polling for something that may simply not
        exist *yet* is the expected outcome here, unlike find_window.
        """
        if self.supports(Capability.WINDOW_EVENTS):
            return self.backend.wait_for_window(title, timeout)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            found = self.find_windows(title)
            if found:
                return found[0]
            if deadline is not None and time.monotonic() >= deadline:
                return None
            time.sleep(interval)

    def wait_window_close(
        self, window: Window, timeout: float | None = None, interval: float = 0.5
    ) -> bool:
        """Block until `window` (from find_window or wait_for_window) closes.

        Replaces X11::GUITest's WaitWindowClose. Compares by `window.handle`,
        which is stable for the backend that produced it (see the Window
        docstring), rather than by title -- two windows can share a title,
        and a title can change before the window actually closes.

        Uses the same event-driven-where-possible, poll-otherwise split as
        wait_for_window: with Capability.WINDOW_EVENTS, watches for a "close"
        event naming this window; otherwise polls windows() every `interval`
        seconds for its disappearance. `timeout` bounds the wait in seconds;
        None waits indefinitely. Returns True once closed, False if `timeout`
        elapses first while it is still open.
        """

        def still_open() -> bool:
            return any(w.handle == window.handle for w in self.windows())

        if not still_open():
            return True
        if self.supports(Capability.WINDOW_EVENTS):
            for event in self.backend.window_events(timeout=timeout):
                if event.change == "close" and event.window.handle == window.handle:
                    return True
            return not still_open()
        deadline = None if timeout is None else time.monotonic() + timeout
        while still_open():
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(interval)
        return True

    def wait_until(
        self,
        predicate: Callable[[], bool],
        timeout: float | None = None,
        interval: float = 0.5,
    ) -> bool:
        """Block until predicate() is truthy, or timeout elapses.

        The general-purpose polling primitive behind element state waits --
        "wait until this button is enabled" is
        `gui.wait_until(lambda: button.enabled)` rather than a hand-rolled
        sleep loop. Returns whether predicate() became truthy before
        timeout; never raises on timeout, matching
        wait_for_window/wait_window_close.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if predicate():
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(interval)

    def wait_for_element(
        self,
        role: str | None = None,
        name: str | None = None,
        within: Element | None = None,
        timeout: float | None = None,
        interval: float = 0.5,
    ) -> Element | None:
        """Block until an element matching role/name appears.

        Poll-based only -- unlike wait_for_window, no backend currently
        offers element-level change events, so there is no event-driven
        branch here. Returns the first matching Element, or None on timeout
        -- not ElementNotFound, since polling for something that may simply
        not exist yet is the expected outcome, the same reasoning
        wait_for_window uses.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            found = self.elements(role=role, name=name, within=within)
            if found:
                return found[0]
            if deadline is not None and time.monotonic() >= deadline:
                return None
            time.sleep(interval)

    def wait_until_gone(
        self,
        role: str | None = None,
        name: str | None = None,
        within: Element | None = None,
        timeout: float | None = None,
        interval: float = 0.5,
    ) -> bool:
        """Block until no element matches role/name/within, or timeout.

        Mirrors wait_window_close for elements -- waiting for a "Saving..."
        toast or a modal's spinner to disappear. Returns True once the
        element is gone, False if timeout elapses first while it is still
        present.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if not self.elements(role=role, name=name, within=within):
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(interval)

    # -- capture -----------------------------------------------------------

    def screenshot(
        self,
        path: str | None = None,
        window: Window | None = None,
        region: tuple[int, int, int, int] | None = None,
    ) -> str:
        """Write a screenshot and return the path it was written to.

        With no arguments, captures the whole desktop to a temporary file.
        `window` captures one window; `region` captures an `(x, y, width,
        height)` rectangle in screen coordinates. The two are mutually
        exclusive -- a region is already screen-absolute, so pairing it with
        a window could only mean one of two different things.

        How a window is captured depends on what the session offers, and
        the difference is visible in the image. A backend declaring
        Capability.WINDOW_CAPTURE (X11 today) reads the window's own
        pixels, so anything stacked on top of it is absent. Everywhere else
        the window's rectangle is looked up through WINDOW_GEOMETRY and cut
        out of a full-screen shot, which does include whatever is covering
        it. Both are honest screenshots of a window; only one is a
        screenshot of *just* that window.
        """
        return self.backend.capture(window=window, path=path, region=region)

    def capture_on_failure(
        self,
        directory: str | None = None,
        name: str | None = None,
        window: Window | None = None,
    ) -> _CaptureOnFailure:
        """Screenshot the screen if the wrapped block raises.

        A GUI test that fails tells you an assertion did not hold; a
        screenshot taken at that moment tells you what was actually on
        screen, which is usually the whole diagnosis. Capturing it after
        the fact is too late -- by then the app has been torn down -- so it
        has to happen while the exception is still propagating:

            with gui.capture_on_failure("artifacts"):
                gui.click_button("Save")
                assert gui.element(name="Saved")

        The path is attached to the exception as a `screenshot` attribute
        and the exception is re-raised untouched, so a test runner reports
        the original failure and the file is there to look at. Nothing is
        captured on success.

        A failing capture never replaces the failure it was trying to
        document: if the screenshot itself raises -- no capture backend, a
        session already gone -- that error is recorded on the exception as
        `screenshot_error` and swallowed. The original exception is what
        the caller needs to see.

        `directory` defaults to $PYGUITEST_SCREENSHOT_DIR, then the
        system temporary directory. `window` narrows the capture the same
        way it does for :meth:`screenshot`.
        """
        return _CaptureOnFailure(self, directory, name, window)

    def locate_image(
        self,
        template_path: str,
        within: Window | None = None,
        threshold: float | None = None,
        metric: str = "RMSE",
    ) -> ImageMatch:
        """Find `template_path` on screen, restricted to `within` if given.

        Captures the whole desktop -- there is no per-window capture path
        this can lean on, since ToolCaptureBackend itself refuses a window
        argument -- looks up `within`'s rectangle via WINDOW_GEOMETRY when a
        Window is given, and hands both to the backend's own template
        matcher, which restricts its search to that rectangle. Because the
        search always runs against the same full-screen image whether or not
        `within` is given, the match's (x, y) comes back screen-absolute
        either way -- callers never need to add the window's own offset back
        on.

        Raises ImageNotFound if no match clears `threshold`. With no
        `threshold`, the single best match is always returned, however poor.
        """
        region = None
        if within is not None:
            region = self.backend.geometry(within)
        haystack = self.backend.capture()
        try:
            match = self.backend.locate(
                haystack,
                template_path,
                region=region,
                metric=metric,
                threshold=threshold,
            )
        finally:
            if os.path.exists(haystack):
                os.unlink(haystack)
        if match is None:
            raise ImageNotFound(
                f"{template_path!r} not found on screen"
                + (f" within {within!r}" if within is not None else "")
            )
        return match

    def elements(
        self,
        role: str | None = None,
        name: str | None = None,
        within: Element | None = None,
    ) -> list[Element]:
        """Return every accessible element matching a role and/or name."""
        return self.backend.find_elements(role=role, name=name, within=within)

    def element(
        self,
        role: str | None = None,
        name: str | None = None,
        within: Element | None = None,
    ) -> Element:
        """Return the first element matching a role and/or name.

        Raises ElementNotFound if nothing matches.
        """
        found = self.elements(role=role, name=name, within=within)
        if not found:
            wanted = ", ".join(
                part
                for part in (
                    f"role={role!r}" if role else "",
                    f"name={name!r}" if name else "",
                )
                if part
            )
            raise ElementNotFound(f"no element with {wanted}")
        return found[0]

    # -- finding things by what they are -----------------------------------
    #
    # Sugar over element(): the common widget kinds, named as a user would
    # describe them rather than by their AT-SPI role string.

    def button(self, name: str, within: Element | None = None) -> Element:
        """Return the push button labelled `name`."""
        return self.element(role=Role.PUSH_BUTTON, name=name, within=within)

    def text_field(self, name: str, within: Element | None = None) -> Element:
        """Return the text box named `name`."""
        for role in (Role.ENTRY, Role.TEXT, Role.PASSWORD_TEXT):
            found = self.elements(role=role, name=name, within=within)
            if found:
                return found[0]
        raise ElementNotFound(f"no text field named {name!r}")

    def dropdown(self, name: str, within: Element | None = None) -> Element:
        """Return the combo box named `name`."""
        return self.element(role=Role.COMBO_BOX, name=name, within=within)

    def checkbox(self, name: str, within: Element | None = None) -> Element:
        """Return the check box named `name`."""
        return self.element(role=Role.CHECK_BOX, name=name, within=within)

    def menu_item(self, name: str, within: Element | None = None) -> Element:
        """Return the menu item named `name`."""
        return self.element(role=Role.MENU_ITEM, name=name, within=within)

    def link(self, name: str, within: Element | None = None) -> Element:
        """Return the link named `name`."""
        return self.element(role=Role.LINK, name=name, within=within)

    # -- delegated to the backend ------------------------------------------
    #
    # Operations the backend implements and Session has nothing to add to.
    # Written out rather than left to __getattr__ below: a dynamic forward
    # is invisible to an editor and to a type checker, so `gui.geometry(w)`
    # offered no completion, no signature, and no warning for a typo. Each
    # one raises CapabilityUnsupported from the backend on a desktop that
    # cannot do it, exactly as it did when it arrived here dynamically.
    #
    # Only operations with no Session-level equivalent are listed. capture,
    # find_element and find_elements are deliberately absent: screenshot,
    # element and elements above are those, with the same arguments.

    def screens(self) -> list[Screen]:
        """Every output, in advertised order."""
        return self.backend.screens()

    def active_window(self) -> Window | None:
        """The currently focused window, or None."""
        return self.backend.active_window()

    def is_window_viewable(self, window: Window) -> bool:
        """Whether `window` is currently mapped and showing."""
        return self.backend.is_window_viewable(window)

    def window_at(self, x: int, y: int, screen: int = 0) -> Window | None:
        """The topmost window covering a screen coordinate, or None."""
        return self.backend.window_at(x, y, screen)

    def geometry(self, window: Window) -> tuple[int, int, int, int]:
        """`window`'s (x, y, width, height) in screen coordinates."""
        return self.backend.geometry(window)

    def move_window(self, window: Window, x: int, y: int) -> None:
        """Move a window's top-left corner to (x, y)."""
        self.backend.move_window(window, x, y)

    def resize_window(self, window: Window, width: int, height: int) -> None:
        """Resize a window to `width` by `height`."""
        self.backend.resize_window(window, width, height)

    def activate_window(self, window: Window) -> None:
        """Raise and focus. There is no raise-without-focus operation."""
        self.backend.activate_window(window)

    def minimize_window(self, window: Window, minimized: bool = True) -> None:
        """Minimize a window, or restore it when `minimized` is False."""
        self.backend.minimize_window(window, minimized)

    def window_events(self, timeout: float | None = None) -> Iterator[WindowEvent]:
        """Yield WindowEvents as the compositor reports them.

        Needs Capability.WINDOW_EVENTS. wait_for_window and
        wait_window_close are what most callers want instead: they consume
        this where it exists and poll where it does not.
        """
        return self.backend.window_events(timeout=timeout)

    def root_element(self) -> Element:
        """The accessible-tree root. The replacement for the X11 window tree."""
        return self.backend.root_element()

    def locate(
        self,
        haystack: str,
        template: str,
        region: Sequence[float] | None = None,
        metric: str = "RMSE",
        threshold: float | None = None,
    ) -> ImageMatch | None:
        """Find `template` within the already-captured image `haystack`.

        Returns None if no match clears `threshold`. Unlike locate_image,
        this searches a file rather than the live screen, and answers with
        None rather than raising -- no display connection is involved.
        """
        return self.backend.locate(
            haystack, template, region=region, metric=metric, threshold=threshold
        )

    def get_clipboard(self) -> str:
        """The clipboard's current text content."""
        return self.backend.get_clipboard()

    def set_clipboard(self, text: str) -> None:
        """Replace the clipboard's text content."""
        self.backend.set_clipboard(text)

    # -- dynamic delegation ------------------------------------------------

    def __getattr__(self, attr: str) -> Any:
        # Whatever the section above does not name: a backend's own extras
        # (X11Backend.pointer_position, PortalBackend.restore_token) and the
        # standard operations that already have a Session spelling. Neither
        # is visible to an editor -- that is the cost of a dynamic forward,
        # and why the interface itself is written out rather than left here.
        #
        # Unsupported operations raise CapabilityUnsupported from the
        # backend, not AttributeError.
        #
        # Reads `backend` through object.__getattribute__ rather than
        # `self.backend`: this hook only runs when normal lookup has already
        # failed, so if `backend` itself is ever missing -- unpickling,
        # copy.copy, a subclass that skips super().__init__(), an exception
        # partway through __init__ -- `self.backend` would re-enter this same
        # method and recurse until RecursionError, burying whatever the real
        # problem was.
        if attr.startswith("_"):
            raise AttributeError(attr)
        backend = object.__getattribute__(self, "backend")
        return getattr(backend, attr)

    def close(self) -> None:
        """Release the backend's resources."""
        self.backend.close()

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        self.close()
        return False

    def __repr__(self) -> str:
        return (
            f"<Session backend={self.backend.name!r} "
            f"session={self.environment.session_type.value} "
            f"caps={len(self.capabilities)}/{len(list(Capability))}>"
        )


class _CaptureOnFailure:
    """The context manager Session.capture_on_failure returns.

    A class rather than @contextmanager because the failure path has to
    inspect the exception, attach to it, and then decline to suppress it --
    which a generator-based manager expresses awkwardly, and which is the
    entire behaviour here.
    """

    def __init__(
        self,
        session: Session,
        directory: str | None,
        name: str | None,
        window: Window | None,
    ) -> None:
        """Capture from `session` into `directory` when the block raises."""
        self.session = session
        self.directory = directory
        self.name = name
        self.window = window
        self.path: str | None = None

    def _destination(self, exception: BaseException) -> str:
        """Where to write, named after the failure and the moment it hit."""
        directory = (
            self.directory
            or os.environ.get("PYGUITEST_SCREENSHOT_DIR")
            or tempfile.gettempdir()
        )
        os.makedirs(directory, exist_ok=True)
        stem = self.name or type(exception).__name__
        # A monotonic-ish suffix rather than a bare name: a test run that
        # fails the same assertion in several cases would otherwise have
        # each failure overwrite the last, leaving only the final one.
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return os.path.join(directory, f"{stem}-{stamp}-{os.getpid()}.png")

    def __enter__(self) -> _CaptureOnFailure:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        if exception is None:
            return False
        try:
            self.path = self.session.screenshot(
                path=self._destination(exception), window=self.window
            )
            # Attached to the exception object, which of course declares no
            # such attribute -- that is the whole mechanism: the runner
            # reports the original failure and the path travels on it.
            failure_object: Any = exception
            failure_object.screenshot = self.path
        except Exception as failure:  # noqa: BLE001 -- see the docstring
            # Deliberately broad, and deliberately swallowed. Anything at
            # all can go wrong here -- no capture backend, an unwritable
            # directory, a display that has already gone away -- and none
            # of it is more important than the exception being reported.
            failure_object = exception
            failure_object.screenshot = None
            failure_object.screenshot_error = failure
        return False


def connect(
    backend: str | None = None,
    environment: Environment | None = None,
    backend_options: dict | None = None,
    **kwargs: float,
) -> Session:
    """Open an automation session.

    Detects the environment, selects a backend, and returns a Session. Never
    raises for a limited desktop -- a session with few capabilities is the
    normal case, and `supports()` is how you find out. Pass `backend` by name
    to override selection.

    `backend_options` goes to that named backend's own constructor, for
    settings only it understands -- `persist_mode`/`restore_token` on
    `portal` and `eiinput`, say. `**kwargs` is unrelated and configures the
    Session's own delays. The returned backend is reachable as
    `session.backend`, which is how a caller reads a value back out:

        gui = connect(backend="portal", backend_options={
            "persist_mode": 2,        # until explicitly revoked
            "restore_token": saved,   # None on the first run
        })
        save_somewhere(gui.backend.restore_token)
    """
    environment = detect() if environment is None else environment
    chosen = backends.select(environment, backend, backend_options)
    return Session(chosen, environment, **kwargs)
