#!/usr/bin/env python3
"""Live re-validation of `eiinput` after its negotiation moved to python-libei.

`eiinput` used to drive `org.freedesktop.portal.RemoteDesktop` itself, in
~190 lines of Gio Request/Response plumbing. That code now lives in
python-libei as `libei.portal`, and `LibeiBackend` calls
`RemoteDesktopSession.negotiate()` instead. Nothing about the conversation
on the wire is meant to have changed -- but the live runs recorded in
`docs/validation.md` (GNOME 2026-08-26, KDE 2026-08-31) exercised the old
copy, and `eiinput.py`'s own docstring is on record that refactoring a
working negotiation is exactly the risk worth naming. This script is what
closes that gap. It needs a human because `Start()` raises a real consent
dialog and blocks until someone clicks Allow.

What it proves, in order:

1. **Preflight** -- that the code under test really is the new path (the
   old `_negotiate_eis_fd` is gone), that `libei.portal` is importable and
   0.3.0+, and that the new "restore_token without persist_mode" guard
   raises. None of this touches D-Bus, opens a window, or prompts.
2. **Negotiate** -- one consent dialog, then a session with real
   capabilities and (having asked for persistence) a `restore_token`.
3. **Inject** -- opens a text editor, types a known string, and *reads it
   back through AT-SPI*. Not "no exception was raised": the characters have
   to arrive in a native Wayland client. Pointer move/click/scroll are
   commanded too, but this session has no `POINTER_QUERY` to read the
   pointer back with, so those are reported as commanded-not-verified.
4. **Restore** -- closes the session (which now calls `Session.Close()`,
   new in this change) and negotiates again presenting the token. The
   headline check: **no second dialog**, and back in well under a second.
5. **Cleanup** -- both sessions closed, the editor terminated, and the
   backend's portal session confirmed released.

Usage:

    python3 examples/_eiinput_portal_validate.py             # the full run
    python3 examples/_eiinput_portal_validate.py --preflight  # checks only
    python3 examples/_eiinput_portal_validate.py --rehearse   # no portal, no dialog
    python3 examples/_eiinput_portal_validate.py --show-token # print it in full
    python3 examples/_eiinput_portal_validate.py --token TOK  # restore only

`--preflight` and `--rehearse` are worth running first, in that order: they
check everything that does not need consent, so the dialog is only spent
once the rest is known to work here.

The last form is the cross-process restore -- the case persistence actually
exists for -- run it in a second terminal with a token from `--show-token`.
Nothing here writes a token to disk: it is a standing grant of input
injection, and where it gets stored is your decision, not a script's. It is
printed truncated unless you ask for it in full.

Only touches the editor window this script opens itself.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shlex
import shutil
import sys
import tempfile
import time
import uuid

import pyguitest
from pyguitest import BackendUnavailable, Capability, PermissionRequired

PERSIST_UNTIL_REVOKED = 2

TYPED = f"pyguitest eiinput portal check {uuid.uuid4().hex[:8]}"
"""What gets typed, and what the readback has to find.

Letters, digits and spaces only -- every layout can produce all of them, so
a readback mismatch means input went wrong, not that the keymap could not
express the text.

Unique per run, which is not cosmetic. GNOME Text Editor keeps drafts of
unsaved documents and restores them on the next launch (`--ignore-session`
governs session restore, not drafts), so a fixed string would already be on
screen before a single key was pressed -- and the readback would pass
without any injection having happened at all. Observed live: the editor
opened showing the previous run's text, with a "Document Restored" banner
and a window title to match."""

EDITOR = "gnome-text-editor --standalone --ignore-session --new-window"
"""A throwaway editor instance, deliberately not a shared one.

`--standalone` is what makes the spawned process *be* the app rather than a
launcher that hands the file to an already-running instance: without it,
typing here would land in whatever document the user already had open, and
terminate() would kill a launcher while their window kept the text.

A freshly created empty file is appended to this at launch (see `Editor`):
opened with no argument, the editor restores its last unsaved draft, which
puts the previous run's text on screen before this one types anything."""

RESTORE_BUDGET = 3.0
"""Seconds a restore may take before it is treated as suspicious. A restore
that skips the dialog is a couple of D-Bus round trips (python-libei
measured 0.2s on GNOME); anything near this bound suggests a human answered
a prompt that should never have appeared."""

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> bool:
    """Record one pass/fail line, print it, and hand the verdict back."""
    results.append((ok, label))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    return ok


def show(token: str | None, full: bool) -> str:
    """A token, truncated unless asked for in full."""
    if token is None:
        return "(none issued)"
    return token if full else f"{token[:8]}... ({len(token)} chars)"


# -- 1. preflight: no D-Bus, no dialog, no windows ------------------------


def preflight(command: list[str]) -> bool:
    """Everything checkable without prompting anyone."""
    print("\n== preflight ==")
    from pyguitest.backends import eiinput

    check(
        not hasattr(eiinput.LibeiBackend, "_negotiate_eis_fd"),
        "the old in-tree negotiation is gone (this is the new code path)",
    )
    check(
        hasattr(eiinput.LibeiBackend, "_negotiate"),
        "LibeiBackend._negotiate (the translation layer) is present",
    )

    try:
        from libei import portal
    except Exception as exc:
        check(False, f"import libei.portal -- {exc}")
        return False
    check(True, "libei.portal imports")

    try:
        import libei

        version = getattr(libei, "__version__", "unknown")
    except Exception:
        version = "unknown"
    print(f"        python-libei {version}")

    check(portal.is_available(), "libei.portal.is_available() (PyGObject present)")
    check(eiinput.available(), "eiinput.available()")

    # The guard added with the cutover: negotiate() refuses a restore_token
    # with no persist_mode rather than spending it for a token it will not
    # get back. It is the first statement in negotiate(), so this raises
    # before any D-Bus traffic -- no dialog, nothing negotiated.
    try:
        eiinput.LibeiBackend(restore_token="not-a-real-token", persist_mode=0)
    except ValueError:
        check(True, "a restore_token with persist_mode=NONE is refused (no D-Bus)")
    except Exception as exc:  # pragma: no cover - live-only path
        check(False, f"expected ValueError from the persist guard, got {exc!r}")
    else:  # pragma: no cover - live-only path
        check(False, "the persist guard did not fire -- a token would be spent")

    check(shutil.which(command[0]) is not None, f"{command[0]} is installed")

    environment = pyguitest.detect()
    print(
        f"        session {environment.session_type.value}, "
        f"compositor {environment.compositor.value}"
    )
    return all(ok for ok, _ in results)


# -- 2/4. negotiation ------------------------------------------------------


def negotiate(label: str, token: str | None, full: bool):
    """Negotiate one eiinput session, timed. Returns (session, seconds)."""
    options: dict[str, object] = {"persist_mode": PERSIST_UNTIL_REVOKED}
    if token is not None:
        options["restore_token"] = token
    print(f"\n== {label} ==")
    if token is None:
        print("  a consent dialog is about to appear -- click Allow")
    else:
        print(f"  presenting {show(token, full)} -- expect NO dialog")
    started = time.monotonic()
    gui = pyguitest.connect(backend="eiinput", backend_options=options)
    elapsed = time.monotonic() - started
    print(f"  negotiated in {elapsed:.2f}s")
    return gui, elapsed


# -- 3. injection ----------------------------------------------------------


def wait_for_new_window(gui, before: set[str], timeout: float = 15.0):
    """The first window whose title was not there before, or None.

    Deliberately not `wait_for_window(title_regex)`: the editor's title
    depends on which editor it is and on the GNOME version that named the
    app, and a regex loose enough to survive that ("." matches any non-empty
    title) would happily return the terminal this is running in. What is
    actually known here is that a window appeared that was not there a
    moment ago.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for window in gui.windows():
            if window.title and window.title not in before:
                return window
        time.sleep(0.5)
    return None


class Editor:
    """The editor process and its window, always cleaned up.

    A context manager rather than a return value on purpose: an earlier
    version started the editor inside the injection step and handed the
    handle back when that step *finished*, so a Ctrl-C part way through
    left the caller holding None and the window open on the desktop with
    nothing to close it. Ownership has to begin the instant the process
    exists, not when the work around it succeeds.
    """

    def __init__(self, gui, command: list[str]) -> None:
        self.gui = gui
        self.command = command
        self.app = None
        self.before: set[str] = set()
        self.before_frames: set[str] = set()
        self.window = None
        self.document: pathlib.Path | None = None

    def __enter__(self) -> Editor:
        # An empty file of this run's own, so the editor has a document to
        # open and therefore nothing to restore. See EDITOR.
        handle, path = tempfile.mkstemp(prefix="pyguitest-eiinput-", suffix=".txt")
        os.close(handle)
        self.document = pathlib.Path(path)
        self.before = {w.title for w in self.gui.windows()}
        self.before_frames = frame_names(self.gui)
        self.app = self.gui.start_app([*self.command, str(self.document)])
        try:
            self.window = wait_for_new_window(self.gui, self.before)
        except BaseException:
            # __exit__ does not run for an exception raised *inside*
            # __enter__ -- the `with` body was never entered -- so the same
            # orphaned-window bug this class exists to fix would come back
            # through the 15s window wait. Ctrl-C there is not hypothetical.
            self.__exit__()
            raise
        return self

    def __exit__(self, *_exc: object) -> bool:
        # The process half is `Application.stop()` -- terminate, bounded
        # wait, kill past it. This class only still exists for what that
        # does not cover: the throwaway document, and the window and frame
        # snapshots the injection step needs.
        if self.app is not None:
            self.app.stop()
        if self.document is not None:
            self.document.unlink(missing_ok=True)
            self.document = None
        return False


HOT_CORNER = 80
"""Pixels of clearance to keep from the top-left of the screen.

Not arbitrary: GNOME's Activities hot corner lives at (0, 0), and a click
there opens the overview, which takes keyboard focus away from whatever was
being typed into. An earlier version of this script computed a window centre
of (0, 0) from degenerate AT-SPI extents and clicked exactly there -- the
overview opened, and the text meant for the editor went to the shell's
search box and then to the terminal behind it."""


def frame_names(gui) -> set[str]:
    """Every top-level frame the accessibility tree currently reports."""
    try:
        return {e.name for e in gui.elements(role="frame") if e.name}
    except Exception:
        return set()


def new_frame(gui, before: set[str], timeout: float = 5.0):
    """The accessibility frame that appeared since `before`, or None.

    Deliberately *not* `window_element(title)`. `windows()` and the
    accessibility tree are two different sources of truth about what is on
    screen, and on this desktop they disagree in both directions: the window
    list (served by `gnomeshell`, which outranks `atspi`) shows an Electron
    window AT-SPI has no frame for, and it went on reporting
    "New Document (Draft) - Text Editor" after the editor had already
    retitled itself to the text that had just been typed into it. Looking a
    window-list title up in the element tree therefore searches for a name
    that no longer exists -- and the miss is expensive, because the lookup
    walks every window role on the desktop before giving up.

    Staying inside one source avoids the whole question: the frame that was
    not there before the launch is the editor, whatever it is calling itself
    by now.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for frame in gui.elements(role="frame"):
            if frame.name and frame.name not in before:
                return frame
        time.sleep(0.5)
    return None


def editor_text(gui, before_frames: set[str]) -> str:
    """Whatever the editor currently shows, or "" if it cannot be read.

    Resolved from scratch on each call rather than through a frame captured
    earlier: the editor renames itself as soon as it has content, and the
    accessibility tree is re-walked anyway. Returning "" rather than raising
    keeps the before/after comparison in `inject` readable -- an unreadable
    editor fails the "after" check, which is the honest outcome.
    """
    frame = new_frame(gui, before_frames)
    if frame is None:
        print("        (no accessibility frame for the editor)")
        return ""
    element = find_text_element(gui, frame)
    if element is None:
        return ""
    return element.text or ""


def find_text_element(gui, frame):
    """The editor's text area within `frame`, or None -- saying what was there."""
    for role in ("text", "document text", "document frame", "entry"):
        try:
            return gui.element(role=role, within=frame)
        except Exception:
            continue
    roles = sorted({e.role for e in gui.elements(within=frame)})
    print(f"        no text-like element; roles present: {', '.join(roles) or 'none'}")
    return None


BLIND_POINT = (400, 400)
"""Somewhere to send the pointer when nothing can say where the editor is.

Far enough from (0, 0) to miss the hot corner, and that is all that can be
claimed for it -- which is why a click is never sent to it. See
`pointer_target`."""


def pointer_target(gui, before: set[str]) -> tuple[tuple[int, int], bool]:
    """Where to point, and whether that coordinate is over the editor.

    Three separate things go wrong here on a GNOME session, all of them
    handled rather than assumed away:

    * The `Window` captured before typing can go stale -- a real run failed
      with "no window with id 106" -- so the handle is resolved again here
      rather than reused.
    * AT-SPI extents for a Wayland-native client are not screen coordinates
      and do not fail loudly: they come back as zeros. A (0, 0) rectangle is
      "no answer", not "the window is at the origin".
    * `screens()` needs `SCREEN_INFO`, which this composed session does not
      have, so even the screen centre may be unavailable.

    The second element of the return says whether the point came from the
    editor's own rectangle. Only then is it safe to *click*: a blind click
    lands on whatever happens to be under it.
    """
    window = wait_for_new_window(gui, before, timeout=2.0)
    if window is not None:
        try:
            x, y, width, height = gui.geometry(window)
            if width > 2 * HOT_CORNER and height > 2 * HOT_CORNER:
                return (x + width // 2, y + height // 2), True
            print(f"        window extents ({x}, {y}, {width}, {height}) are unusable")
        except Exception as exc:
            print(f"        geometry unavailable: {exc}")
    try:
        screen = gui.screens()[0]
        candidate = (screen.width // 2, screen.height // 2)
        print(f"        no window rectangle; using the screen centre {candidate}")
        return candidate, False
    except Exception as exc:
        print(f"        no screen size either ({exc}); using {BLIND_POINT}")
        return BLIND_POINT, False


def is_active(gui, window, before: set[str]) -> bool:
    """Whether the editor is the active window -- by identity, not by title.

    A title is not identity here, and comparing them cost a whole live run:
    `windows()` reported "New Document (Draft) - Text Editor" while
    `active_window()` reported "pyguitest eiinput portal (Draft) - Text
    Editor" for the same window at the same moment, so an equality check
    could never succeed and the run stopped to ask a human to focus a window
    that was already focused. Both reads come from a desktop where the
    editor renames itself the instant it has content, and the two sources
    notice at different times (see docs/validation.md).

    So: prefer the backend's own handle, fall back to the title matching,
    and finally accept any active window that was not on screen before the
    launch -- which is the same identity `new_frame` and
    `wait_for_new_window` use, and the one that survives a rename.
    """
    active = gui.active_window()
    if active is None or not active.title:
        return False
    handle = getattr(active, "handle", None)
    if handle is not None and handle == getattr(window, "handle", None):
        return True
    return active.title == window.title or active.title not in before


def focus_editor(gui, window, before: set[str], attempts: int = 2) -> bool:
    """Get the editor focused, asking for help rather than giving up.

    `activate_window()` is not reliably enough on its own here, and the run
    that prompted this shows why: with `WINDOW_ACTIVATE` served by AT-SPI it
    is `grabFocus()`, and a client cannot raise itself under Mutter -- the
    GNOME Shell window-control extension can, but it is not always
    installed. Focus-stealing prevention also leaves a freshly launched
    window unfocused and merely "demanding attention".

    So: poll rather than sleep a fixed time (activation is asynchronous),
    retry, and if the desktop still will not do it, ask the human who is
    already here for the consent dialog. Refusing to type is the one thing
    this must not do quietly -- but so is typing into the wrong window.
    """
    for attempt in range(attempts):
        try:
            gui.activate_window(window)
        except Exception as exc:
            print(f"        activate_window failed: {exc}")
        if gui.wait_until(
            lambda: is_active(gui, window, before), timeout=3.0, interval=0.25
        ):
            return True
        print(f"        not focused after activate_window (attempt {attempt + 1})")

    active = gui.active_window()
    print(
        f"        active window is {active.title!r}"
        if active
        else "        no active window"
    )
    print(f"  click the {window.title!r} window to focus it, then press Enter here")
    try:
        input("  (or press Enter with nothing focused to skip typing) ")
    except EOFError:
        return False
    return gui.wait_until(
        lambda: is_active(gui, window, before), timeout=3.0, interval=0.25
    )


def inject(input_gui, elements_gui, editor: Editor) -> None:
    """Type into a real editor window and read the text back.

    Order matters here, and it is not the obvious one. Typing goes wherever
    the compositor says focus is, so anything that can move focus has to
    happen *after* the assertion that depends on it -- which is why the
    pointer commands come last, and why this refuses to type at all unless
    the editor is genuinely the active window.
    """
    print("\n== injection ==")
    window, before = editor.window, editor.before
    if window is None:
        check(False, f"{editor.command[0]} never opened a window of its own")
        return
    print(f"        window: {window.title!r}")

    if not focus_editor(elements_gui, window, before):
        # Never type into a window that just happens to have focus: the
        # keystrokes are real, and whatever is in front of them gets them.
        check(False, "the editor never became the active window -- not typing")
        return
    check(True, "the editor is the active window")

    if input_gui.supports(Capability.TEXT_ENTRY):
        # Read first. What is on screen before a key is pressed is the
        # baseline the readback has to beat -- an editor that restored a
        # draft would otherwise let this pass without any injection at all.
        was = editor_text(elements_gui, editor.before_frames)
        print(f"        before typing: {was.strip()[:50]!r}")
        check(TYPED not in was, "the marker for this run is not already on screen")

        input_gui.type_text(TYPED)
        time.sleep(0.8)
        got = editor_text(elements_gui, editor.before_frames)
        print(f"        read back:     {got.strip()[:70]!r}")
        check(TYPED in got, "the typed text arrived in a native Wayland client")
        if TYPED not in got:
            print("        (the text may still have arrived -- look at the window)")
    else:
        check(False, "TEXT_ENTRY not offered -- no keymap was readable")

    # Pointer last: a click can open the overview, dismiss a window, or move
    # focus, and none of that can spoil an assertion that already happened.
    if input_gui.supports(Capability.POINTER_MOVE):
        target, over_editor = pointer_target(elements_gui, before)
        input_gui.move_mouse(*target)
        time.sleep(0.3)
        check(True, f"move_mouse{target} (commanded; no POINTER_QUERY to verify)")
        if input_gui.supports(Capability.POINTER_BUTTON):
            if over_editor:
                input_gui.click()
                time.sleep(0.3)
                check(True, "click (commanded)")
            else:
                # Moving the pointer somewhere unknown is harmless; pressing
                # a button there is not -- it would land on whatever happens
                # to be under it, which on this desktop is the terminal
                # running the validation.
                print("        click skipped: that point is not known to be the editor")
        if input_gui.supports(Capability.POINTER_SCROLL):
            input_gui.scroll(dy=1)
            time.sleep(0.3)
            check(True, "scroll (commanded)")


# -- the run --------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--editor", default=EDITOR, help=f"editor command (default: {EDITOR!r})"
    )
    parser.add_argument("--preflight", action="store_true", help="checks only")
    parser.add_argument(
        "--rehearse",
        action="store_true",
        help="the window/typing half only, through the default session -- "
        "no portal, no dialog. Proves the AT-SPI readback works here "
        "before a consent dialog is spent on it.",
    )
    parser.add_argument("--show-token", action="store_true", help="print it in full")
    parser.add_argument("--token", help="restore-only run, in a second process")
    args = parser.parse_args()
    command = shlex.split(args.editor)

    if not preflight(command) and not args.token:
        print("\npreflight failed -- not negotiating anything")
        return 1
    if args.preflight:
        return summary()

    if args.rehearse:
        # No eiinput, so no dialog: the default composed session injects
        # through uinput here. That is not keymap-safe -- it types through a
        # hardcoded US table -- so a readback mismatch on a non-US layout is
        # the rehearsal's own limitation, not a fault in what is being
        # validated. Everything else (window discovery, the element lookup,
        # the readback) is the same code the real run uses.
        print("\n== rehearsal: no portal, no dialog ==")
        gui = pyguitest.connect()
        print(f"  backend: {gui.backend.name}")
        try:
            with Editor(gui, command) as editor:
                inject(gui, gui, editor)
        finally:
            gui.close()
        return summary()

    if args.token:
        gui, elapsed = negotiate("restore (this process's first)", args.token, True)
        try:
            check(elapsed < RESTORE_BUDGET, f"restored in {elapsed:.2f}s, no dialog")
            check(
                bool(gui.capabilities),
                f"capabilities: {sorted(c.name for c in gui.capabilities)}",
            )
            print(f"  new token: {show(gui.backend.restore_token, args.show_token)}")
        finally:
            gui.close()
        return summary()

    # The ordinary composed session, deliberately, and not a forced "atspi":
    # `--rehearse` runs against exactly this, and a rehearsal is only worth
    # having if it exercises the same window and element path the real run
    # will. Forcing atspi drops `gnomeshell`, which is what served
    # WINDOW_ACTIVATE and a usable window rectangle in the rehearsal -- so
    # the real run would have been the first time that path was tried, which
    # is the situation the rehearsal exists to avoid. Being the composed
    # session it also has uinput, which is never used here: injection is
    # `input_gui`'s job, and that is the eiinput session under test.
    #
    # Deliberately two sessions rather than one `connect(backend=[...])`,
    # which is what `_eiinput_validate.py` uses. Naming a list means naming
    # every member, and what this script actually wants is "whatever this
    # desktop composes automatically, plus eiinput" -- a fixed list would
    # pin it to one desktop (`gnomeshell` declines off Mutter, and a named
    # backend that cannot build raises rather than being skipped), and this
    # script is meant to run on KDE too.
    elements_gui = pyguitest.connect()
    try:
        gui, _elapsed = negotiate("fresh negotiation", None, args.show_token)
        try:
            caps = sorted(c.name for c in gui.capabilities)
            check(bool(caps), f"capabilities: {caps}")
            token = gui.backend.restore_token
            check(token is not None, f"restore_token issued: {show(token, False)}")
            with Editor(elements_gui, command) as editor:
                inject(gui, elements_gui, editor)
        finally:
            gui.close()
            check(
                getattr(gui.backend, "_session", None) is None,
                "close() released the portal session (Session.Close)",
            )

        if token is None:
            print("\nno token to restore with -- skipping the restore phase")
            return summary()

        second, elapsed = negotiate("restore", token, args.show_token)
        try:
            check(elapsed < RESTORE_BUDGET, f"restored in {elapsed:.2f}s")
            answer = input("  did a second consent dialog appear? [y/N] ").strip()
            check(
                answer.lower() not in ("y", "yes"),
                "no second dialog -- persistence survived the cutover",
            )
            print(f"  new token: {show(second.backend.restore_token, args.show_token)}")
            if not args.show_token:
                print("        (--show-token prints it in full, for --token runs)")
        finally:
            second.close()
    except (BackendUnavailable, PermissionRequired) as exc:
        # connect() flattens a factory's BackendUnavailable into a generic
        # "cannot drive this session"; `pyguitest debug` reports the real
        # reason, and constructing LibeiBackend() directly shows it verbatim.
        check(False, f"{type(exc).__name__}: {exc}")
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        elements_gui.close()
    return summary()


def summary() -> int:
    failed = [label for ok, label in results if not ok]
    print(f"\n== summary ==\n  {len(results) - len(failed)}/{len(results)} passed")
    for label in failed:
        print(f"  FAIL  {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # Ctrl-C is an ordinary way to end a run that is waiting on a human,
        # so it gets a summary rather than a stack trace. The editor is
        # already gone by here either way -- Editor.__exit__ runs on the way
        # out, which is the whole reason it is a context manager.
        print("\ninterrupted")
        sys.exit(summary())
