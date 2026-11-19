"""Window events on KDE, via an ad hoc KWin script.

The gap this fills: `kdotool` (this project's KDE window backend) has no
event-subscription mechanism at all -- no `behave`, no watch/wait
subcommand, nothing (checked against `kdotool --help` directly: purely
query/action, unlike `xdotool behave`). Every other window/input/element
capability this package can offer on KDE is already available; this is
the one real gap `Capability.WINDOW_EVENTS` closes.

The GNOME Shell extension (`gnomeshell.py`) exposes its own D-Bus signal,
because it runs inside the shell process and Gio makes that easy. KWin
scripts have no equivalent: introspecting `org.kde.kwin.Scripting` live
turned up `loadScript(path)`/`Script.run()` (load and run an arbitrary
script file at runtime -- no install-and-enable-in-System-Settings step,
unlike a real installed KWin Script or the GNOME extension) and
`callDBus(service, path, interface, method, ...)` *inside* the script
(call an existing D-Bus service -- well-documented; registering a new one
is not). So the architecture inverts relative to GNOME: this backend
hosts the D-Bus service itself, and `_kwin_window_events.js` is the
client, pushing one `Notify(change, id, title)` call per event.

Confirmed live, KDE Plasma 6 / KWin, 2026-09-03: `workspace.windowAdded`,
`.windowRemoved`, and a per-window `captionChanged` all fire correctly
against a real window open/close, each `callDBus` call reaching a
Python-hosted GDBus service. `window.internalId` matches `kdotool`'s own
window-handle format exactly (`{xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx}`),
cross-checked against a real `kdotool search .` listing -- the detail
that lets a `Window` this yields interoperate with `KdotoolBackend`'s
other operations (geometry, activate, ...) through the composite, since
`CompositeBackend` reads `Window.handle` directly with no ownership
check. See `_kwin_window_events.js`'s own header for the
`workspace.windowList()` vs `workspace.stackingOrder` finding (the former
reliably broke script execution; the latter, a plain property rather
than a method call, did not) and `docs/validation.md` for the full
narrative.

This backend has no window-listing of its own -- `wait_for_window()`
shells out to `kdotool search` for the "does a match already exist"
check, the same tool `KdotoolBackend` wraps, rather than duplicating
window enumeration here.
"""

from __future__ import annotations

import contextlib
import re
import subprocess
import time
from importlib import resources

from ..capabilities import Capability, CapabilitySet
from ..errors import BackendUnavailable
from .base import GUIBackend, Window
from .windows import WindowEvent

__all__ = ["KWinEventsBackend", "available"]

_KWIN_BUS_NAME = "org.kde.KWin"
_SCRIPTING_PATH = "/Scripting"
_SCRIPTING_INTERFACE = "org.kde.kwin.Scripting"
_SCRIPT_INTERFACE = "org.kde.kwin.Script"

# Must match _kwin_window_events.js's own BUS_NAME/OBJECT_PATH/INTERFACE.
_BUS_NAME = "org.pyguitest.KWinEvents"
_OBJECT_PATH = "/WindowEvents"
_INTERFACE = "org.pyguitest.KWinEvents"

_IFACE_XML = f"""
<node>
  <interface name="{_INTERFACE}">
    <method name="Notify">
      <arg type="s" direction="in" name="change"/>
      <arg type="s" direction="in" name="id"/>
      <arg type="s" direction="in" name="title"/>
    </method>
  </interface>
</node>"""

_OWN_NAME_TIMEOUT = 5.0
_SUBPROCESS_TIMEOUT = 5.0
_SWEEP_TIMEOUT = 2.0
"""How long `_sweep` keeps unloading predecessors' copies of the script.

Generous for the case it is there for -- KWin removes one copy per call and
schedules each removal on its own loop, so this is several rounds of a
round-trip and a `deleteLater` -- and short enough that a KWin whose
scripting is wedged refuses the session rather than hanging it."""
_SWEEP_POLL = 0.05
_SWEEP_MIN_CALL_MS = 100
"""The least a sweep call is given, once its budget is spent.

A D-Bus call asked for 0ms is refused before it goes out, which would turn
the last question of a sweep -- the one that decides whether to refuse the
session -- into a guaranteed "cannot say". A tenth of a second is enough for
a KWin that is answering at all."""


_UNSUPPORTED = object()
"""The answer "there is no such method to call", as distinct from no answer.

D-Bus reports a method a service does not have as `UnknownMethod` (or
`UnknownInterface`), which is a fact about the service: no retry invents it,
so a sweep that cannot ask whether a script is loaded falls back to the
single best-effort unload. A call that *timed out* or was refused is the
other thing entirely -- the question is still open -- and the two were one
answer until a timeout became likely enough to matter (see
`_sweep`)."""

_MISSING_METHOD = ("unknownmethod", "unknowninterface", "unknownobject", "notsupported")
"""The D-Bus error names that mean "no such method here".

Matched on the text because Gio raises these as a `GLib.Error` whose domain
and code depend on how the error came back (an error *name* on the wire, a
local `G_DBUS_ERROR` otherwise), while the name itself is in the message
either way -- and this file cannot import GLib at module scope, since
PyGObject is exactly what it may not have."""


def _is_missing_method(refusal):
    """Whether a failed call means the method is not there to call."""
    text = f"{type(refusal).__name__}: {refusal}".lower()
    return any(name in text for name in _MISSING_METHOD)


def _gio():
    """Import Gio (and GLib, for Variant construction), or return None.

    Same PyGObject dependency the atspi/gnomeshell backends already need
    -- see docs/developers/adr-001-dependencies.md -- so this adds nothing
    new to install, just a new use of it.
    """
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except Exception:
        return None
    return Gio, GLib


def available():
    """Whether the library this backend needs is importable."""
    return _gio() is not None


class KWinEventsBackend(GUIBackend):
    """Window create/close/title-change events, via an ad hoc KWin script."""

    name = "kwinevents"

    def __init__(self, runner=None, connect=True):
        """Host the events service and load the script.

        `connect=False` skips both -- no D-Bus service is hosted, no KWin
        script is loaded, no session bus is touched at all -- leaving
        `self._events` as a plain empty list a test can append to
        directly, and `self._scripting` as a no-op stand-in `close()`
        does nothing to. That is the whole surface `window_events()` and
        `wait_for_window()`'s own logic depends on; nothing about *how*
        `self._events` gets populated is inside what those two methods
        do. Mirrors GnomeShellBackend's `proxy=None` injection in spirit
        -- a real KWin/D-Bus connection is compound state here rather
        than one proxy object, so a boolean gate stands in for it.
        """
        modules = _gio()
        if modules is None:
            raise BackendUnavailable(
                "PyGObject is not installed; pip install 'pyguitest[atspi]' "
                "pulls in the same dependency this needs"
            )
        self._Gio, self._GLib = modules
        self._runner = runner or self._run_kdotool
        self._events: list[tuple[str, str, str]] = []
        self._connection = None
        self._registration_id = None
        self._own_name_id = None
        self._scripting = None
        self._context = None
        """The main context Gio delivers this service's calls on, set by
        `_host_service`; None until then, and for a `connect=False` backend
        that hosts nothing, where the global default context is used."""
        self._wake = None
        """The active window_events() call's loop.quit, while it is waiting
        on an empty queue -- None otherwise. Sole reason _handle_method_call
        needs no lock: both it and window_events() only ever touch this
        from the same GLib main context's callbacks, never concurrently."""

        if connect:
            try:
                self._host_service()
                self._scripting = self._load_and_run_script()
            except BaseException:
                # `_host_service()` has already claimed the bus name and
                # exported the object by the time the script load can fail,
                # and that failure is a documented, reachable one: the
                # factory's own docstring names "the script failing to load"
                # among the reasons construction raises where `available()`
                # saw nothing wrong. Nothing else can release the name --
                # `close()` is never reached on an object no caller was
                # handed, and there is no `__del__` anywhere in these
                # backends -- so it stayed owned for the life of the
                # process. Worse, it was claimed without DO_NOT_QUEUE, so a
                # later connect() in the same process queued behind this
                # abandoned ownership and timed out blaming "another
                # instance", which was this process all along. close() is
                # idempotent and suppresses its own errors, so it is safe
                # here however far construction got.
                self.close()
                raise
        else:
            self._scripting = _Closer(lambda: None)

    def _host_service(self):
        """Own `_BUS_NAME` and export the Notify method, waiting for both.

        Waits for the *name*, not the bus. Gio fires `bus_acquired` as soon
        as it is connected to the session bus -- before `RequestName` has
        even been sent -- and only then reports the outcome, as
        `name_acquired` or, for a name another connection already owns
        (queued behind it, since nothing here asks for `DO_NOT_QUEUE`), as
        `name_lost`. Returning on `bus_acquired` returned before the answer,
        so a second session in another process came up believing it owned
        the name -- and `_load_and_run_script`'s sweep, which is safe only
        because the name is owned first, then unloaded the live session's
        script. The object is still exported on `bus_acquired`, where the
        connection is first in hand; it is only the verdict that waits.

        Waited on the context the callbacks will arrive on, with the
        deadline *attached to that context* rather than read between
        iterations. Two reasons, both about `iteration(True)`, which blocks
        until some source is ready:

        - A clock tested between iterations cannot interrupt one. A
          `RequestName` whose answer never came -- a wedged bus daemon --
          left `_OWN_NAME_TIMEOUT` describing nothing, and construction
          blocked for as long as the bus stayed quiet. A timeout source on
          the same context is what wakes the wait, so the refusal below is
          reachable rather than aspirational.
        - Gio delivers these callbacks on whatever context was
          thread-default when `bus_own_name` was called, which is not the
          global default one where a caller has pushed its own (this
          package pushes one around every asynchronous AT-SPI action).
          Iterating the default context there dispatches nothing and times
          out owning a name the bus had already granted.
        """
        node_info = self._Gio.DBusNodeInfo.new_for_xml(_IFACE_XML)
        interface_info = node_info.interfaces[0]
        acquired = []

        def on_bus_acquired(connection, _name):
            """Export the Notify method on the connection just made."""
            self._connection = connection
            self._registration_id = connection.register_object(
                _OBJECT_PATH, interface_info, self._handle_method_call, None, None
            )

        def on_name_acquired(_connection, _name):
            """Record that the bus name is ours."""
            acquired.append(True)

        def on_name_lost(_connection, _name):
            """Record that the bus name could not be taken."""
            acquired.append(False)

        self._own_name_id = self._Gio.bus_own_name(
            self._Gio.BusType.SESSION,
            _BUS_NAME,
            self._Gio.BusNameOwnerFlags.NONE,
            on_bus_acquired,
            on_name_acquired,
            on_name_lost,
        )
        context = (
            self._GLib.MainContext.get_thread_default()
            or self._GLib.MainContext.default()
        )
        # Kept, not just used: this is the context the exported object's
        # `Notify` calls will be dispatched on, so `window_events` has to
        # run its loop on the same one. Running it on the global default
        # while a caller had pushed its own meant nothing dispatched the
        # queued calls at all -- a bounded wait returned no events, an
        # unbounded one never returned.
        self._context = context
        expired = []

        def expire(*_args):
            """Record the deadline, and say so once: GLib drops a False source."""
            expired.append(True)
            return False

        deadline = self._GLib.timeout_source_new(int(_OWN_NAME_TIMEOUT * 1000))
        deadline.set_callback(expire)
        deadline.attach(context)
        try:
            while not acquired and not expired:
                context.iteration(True)
        finally:
            deadline.destroy()
        if not acquired or acquired[-1] is False:
            # The two ways this fails are different news, and the second is
            # the one a caller can act on. Measured: a name another session
            # holds is answered `name_lost` in about 70ms, so a refusal that
            # took the whole deadline is a bus that said nothing at all.
            why = (
                f"another instance may already be running (it answered that "
                f"{_BUS_NAME} is taken)"
                if acquired
                else f"the session bus did not answer the request within "
                f"{_OWN_NAME_TIMEOUT}s"
            )
            raise BackendUnavailable(
                f"could not own {_BUS_NAME} on the session bus -- {why}"
            )

    def _handle_method_call(
        self, connection, sender, path, interface, method, params, invocation
    ):
        """The Notify method the KWin script calls, once per event."""
        if method == "Notify":
            change, window_id, title = params.unpack()
            self._events.append((change, window_id, title))
            invocation.return_value(None)
            if self._wake is not None:
                self._wake()

    @staticmethod
    def _sweep(unload, still_loaded):
        """Unload every resident copy of the script, or refuse.

        One `unload` per round, because that is what KWin's own
        `unloadScript` does -- the first script matching the name -- and
        then `still_loaded` again, because the removal it schedules is a
        `deleteLater` that only takes effect once KWin's loop runs. Rounds
        until nothing is left, bounded by `_SWEEP_TIMEOUT`, and
        `BackendUnavailable` if a copy survives it: adding another producer
        to one that would not go is how every window event came to arrive
        twice, and a refusal naming the leftover is something a caller can
        act on (restart KWin's scripting, or the session).

        Three answers, not two, and the third is split. `_UNSUPPORTED` is
        a KWin that does not have `isScriptLoaded` at all: nothing here can
        be checked on such a desktop, so it gets the single best-effort
        unload this did before the loop existed, and the load proceeds.
        None is a question that was *asked* and not answered -- a call that
        timed out, a KWin that refused it -- which says nothing about
        whether a copy is resident; it is retried for as long as the budget
        lasts and refuses if it never clears. The two used to be one
        answer, and since the calls are now bounded by the sweep's own
        budget, a timeout is the more likely of them: read as "no such
        method" it skipped straight to loading another producer.

        A None *after* a copy has been seen refuses for the same reason:
        the question that mattered went unanswered, so clearance was never
        established, and loading on top of what may still be there is the
        duplication this exists to prevent.

        Both calls are bounded by the sweep's own budget rather than by the
        proxy's default (25s): the deadline is taken before the first
        question, and each call gets what is left of it, so a KWin whose
        scripting has stopped answering costs the construction two seconds
        rather than a minute.
        """
        deadline = time.monotonic() + _SWEEP_TIMEOUT

        def budget():
            """Milliseconds left of the sweep, for the next call."""
            return max(_SWEEP_MIN_CALL_MS, int((deadline - time.monotonic()) * 1000))

        loaded = still_loaded(budget())
        if loaded is _UNSUPPORTED:
            unload(budget())
            return
        while loaded is not False and time.monotonic() < deadline:
            unload(budget())
            # Asked after a pause, not straight away: the removal KWin
            # schedules only takes effect once its own loop runs, so an
            # immediate re-read answers True about a script already going.
            time.sleep(_SWEEP_POLL)
            loaded = still_loaded(budget())
        if loaded is not False:
            why = (
                "is still loaded"
                if loaded
                else "may still be loaded -- KWin did not answer "
                "isScriptLoaded within the sweep's budget"
            )
            raise BackendUnavailable(
                f"a copy of the window-events KWin script {why} "
                f"after {_SWEEP_TIMEOUT}s of unloading it -- a previous "
                "session's, left behind when it was killed. Loading another "
                "would duplicate every window event, so this is refused; "
                "restarting the Plasma session clears it"
            )

    def _load_and_run_script(self):
        """Load `_kwin_window_events.js` into KWin and start it running.

        Returns a small object exposing `.close()`, which unloads it --
        matching the shape an injected fake needs for tests.

        The script is addressed by its *plugin name* throughout, and the
        one-argument `loadScript(path)` gives it one: KWin's
        `AbstractScript` constructor fills a null plugin name in with the
        file path (`scripting.cpp`, `if (m_pluginName.isNull())
        m_pluginName = scriptName;`), so `unloadScript(path)` names exactly
        the script `loadScript(path)` registered, and names nothing -- it
        returns False -- when none is loaded under it. That makes the unload
        safe to issue without knowing whether a load took, which both uses
        below depend on.

        **What that name does not cover**, stated rather than left to be
        discovered: it is the *deployment path*, so two installs of this
        package -- or one test harness that deploys it to two directories --
        register two different plugin names, and a sweep of either cannot
        see a copy loaded under the other. Measured by accident on a KDE
        box: three probe trees of one afternoon each left a copy of their
        own, two of them outlived their files (KWin keeps the compiled
        script), and every window event arrived twice until each name was
        unloaded by hand. One install on a desktop has one path and is not
        affected. The fix is KWin's **two**-argument
        `loadScript(path, pluginName)`, which would let every session
        register the same constant name and make the sweep reach any copy
        whatever directory it came from; it is not done here because it
        changes how every KDE session registers its script -- including
        what KWin's own duplicate check then compares -- and that is not
        something to land on unit tests alone. See `docs/validation.md`.

        KWin registers the script *before* replying with its id
        (`scripts.append(script); return id;`), so a `loadScript` that
        fails on this side -- the reply timed out, the connection dropped
        mid-call -- can still have loaded it. An unload gated on having
        *seen* the id would skip exactly that case and leave the script
        resident, calling `Notify` on this bus name for the life of the
        compositor. So every failure unloads, not only the ones after a
        reply. The same leak has a second route: a session killed outright
        (`SIGKILL`, a crashed harness) never runs the closer, and the next
        session's `loadScript` does not notice -- KWin's duplicate check
        compares the plugin name *as passed*, null here, which no
        registered script carries -- so two copies of the script run and
        every window event arrives twice. A sweep of the plugin name before
        loading clears a predecessor's copy; it is ours to clear, since the
        bus name this backend owns first keeps two live sessions from
        coexisting.

        *Copies*, plural, and that is why the sweep is a loop: two sessions
        killed outright leave two, and one `unloadScript` removes one of
        them -- KWin's own takes the first script matching the name
        (`scripting.cpp`, `unloadScript` -> `findScript` -> a single
        `deleteLater`) and answers True. Swept once, a third copy was then
        added on top of a survivor and every event still arrived twice. So
        the sweep asks `isScriptLoaded` between unloads and stops when KWin
        says none is left, bounded by `_SWEEP_TIMEOUT` -- the removal is a
        `deleteLater`, so the answer only turns False once KWin's own loop
        runs -- and refuses rather than adding to a pile it could not
        clear. A KWin that will not answer `isScriptLoaded` at all (an
        older one, a refused call) is left the single best-effort unload
        this always did: no worse than before, and still not a reason to
        refuse the session.
        """
        script_path = str(
            resources.files("pyguitest.backends") / "_kwin_window_events.js"
        )
        kwin = self._Gio.DBusProxy.new_for_bus_sync(
            self._Gio.BusType.SESSION,
            self._Gio.DBusProxyFlags.NONE,
            None,
            _KWIN_BUS_NAME,
            _SCRIPTING_PATH,
            _SCRIPTING_INTERFACE,
            None,
        )

        def unload(timeout_ms=-1):
            """Unload the script by plugin name, best-effort.

            Best-effort in every use: before a load it clears a predecessor
            that may not exist, after a failure it clears a load that may
            not have taken, and on close the session is going away anyway.
            `timeout_ms` is the sweep's remaining budget where the sweep is
            the caller; -1, the proxy's own default, everywhere else, since
            a rollback or a close has no budget to keep to.
            """
            with contextlib.suppress(Exception):
                kwin.call_sync(
                    "unloadScript",
                    self._GLib.Variant("(s)", (script_path,)),
                    self._Gio.DBusCallFlags.NONE,
                    timeout_ms,
                    None,
                )

        def still_loaded(timeout_ms=-1):
            """Whether KWin still has a script under this plugin name.

            `_UNSUPPORTED` where the method is not there to call -- D-Bus
            answers that as `UnknownMethod`/`UnknownInterface`, and no
            retry invents it. None where the call was made and produced no
            usable answer: it timed out, KWin refused it, the reply came
            back in another shape. Neither is False, and the sweep treats
            the two differently (see `_sweep`): the first is a desktop that
            cannot be checked, the second a question still open.
            """
            try:
                answer = kwin.call_sync(
                    "isScriptLoaded",
                    self._GLib.Variant("(s)", (script_path,)),
                    self._Gio.DBusCallFlags.NONE,
                    timeout_ms,
                    None,
                )
            except Exception as refusal:  # noqa: BLE001 - a KWin that will not say
                return _UNSUPPORTED if _is_missing_method(refusal) else None
            try:
                return bool(answer.unpack()[0])
            except Exception:  # noqa: BLE001 - an answer in another shape
                return None

        self._sweep(unload, still_loaded)
        try:
            script_id = kwin.call_sync(
                "loadScript",
                self._GLib.Variant("(s)", (script_path,)),
                self._Gio.DBusCallFlags.NONE,
                -1,
                None,
            ).unpack()[0]
            script = self._Gio.DBusProxy.new_for_bus_sync(
                self._Gio.BusType.SESSION,
                self._Gio.DBusProxyFlags.NONE,
                None,
                _KWIN_BUS_NAME,
                f"{_SCRIPTING_PATH}/Script{script_id}",
                _SCRIPT_INTERFACE,
                None,
            )
            script.call_sync("run", None, self._Gio.DBusCallFlags.NONE, -1, None)
        except Exception as exc:
            # KWin keeps a loaded script whether or not this object survives
            # to unload it, and only the closer returned below unloads one --
            # never created when this raises. So any failure past the point
            # KWin may have registered the script (which is *every* failure
            # here, the docstring says why a load that raised counts) left
            # it resident with nothing holding a handle. Unloaded instead,
            # and the original failure is still what surfaces.
            unload()
            raise BackendUnavailable(
                f"could not load/run the window-events KWin script: {exc}"
            ) from exc

        return _Closer(unload)

    def _run_kdotool(self, argv):
        """Run a kdotool command, returning its stripped stdout, or ''."""
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=_SUBPROCESS_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return (result.stdout or "").strip()

    @property
    def capabilities(self):
        """Window events only -- everything else on KDE comes from kdotool."""
        return CapabilitySet({Capability.WINDOW_EVENTS})

    def window_events(self, timeout=None):
        """Yield WindowEvents as the KWin script reports them.

        Mirrors GnomeShellBackend's own version of this method closely:
        one GLib.MainLoop for the whole call, woken either by a timeout
        source or by `_handle_method_call` calling `self._wake()` (its
        `on_signal`'s job there) the moment a Notify arrives -- rather
        than a bare `context.iteration(True)` poll, which an earlier
        draft used and had a real bug, caught before it ever ran against
        a live KWin: with nothing else scheduled on the context (exactly
        the state a `connect=False` test backend is in, deliberately, so
        its own event queue can be exercised without a real KWin),
        `iteration(True)` blocks waiting for a source that will never
        fire, silently ignoring `timeout` altogether.

        Already-queued events (typically true in exactly that same test
        case, and also whenever this generator is resumed with events
        that arrived since the last yield) are drained before touching
        the loop at all, so appending straight to `self._events` and
        calling this is enough to exercise the yield/ordering logic with
        no GLib mainloop iteration involved.

        The loop and its timeout both run on the context `_host_service`
        hosted the service on (`self._context`), not on the global default
        one. Gio dispatches an exported object's method calls on the
        context that was thread-default when the name was owned, so where a
        caller had pushed its own -- this package pushes one around every
        asynchronous AT-SPI action -- a loop on the default context
        dispatched none of them: a bounded wait returned no events and an
        unbounded one blocked for good. A backend hosting nothing
        (`connect=False`) has no context of its own and uses the default,
        which is where its test-injected events are waited on.
        """
        self.require(Capability.WINDOW_EVENTS)
        deadline = None if timeout is None else time.monotonic() + timeout
        context = self._context or self._GLib.MainContext.default()
        loop = self._GLib.MainLoop.new(context, False)
        try:
            while True:
                if not self._events:
                    waker = None

                    def on_timeout(*_args):
                        """Stop the loop; one shot, so GLib drops the source."""
                        loop.quit()
                        return False

                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return
                        # Attached to the service's own context rather than
                        # added to the default one, for the reason the
                        # docstring gives: a timeout on a context nothing
                        # iterates never fires.
                        waker = self._GLib.timeout_source_new(
                            max(0, int(remaining * 1000))
                        )
                        waker.set_callback(on_timeout)
                        waker.attach(context)
                    self._wake = loop.quit
                    loop.run()
                    self._wake = None
                    if waker is not None:
                        # Safe whether or not it fired: destroying an
                        # already-dispatched one-shot source is a no-op.
                        waker.destroy()
                    if not self._events:
                        return
                change, window_id, title = self._events.pop(0)
                window = Window(handle=window_id, backend=self, title=title)
                yield WindowEvent(change=change, window=window)
        finally:
            self._wake = None

    def wait_for_window(self, title, timeout=None):
        """Block until a window whose title matches `title` (regex) appears.

        Checks existing windows first via a plain `kdotool search`, so
        one already open is not missed -- the same race a bare
        `window_events()` subscription would have. This backend has no
        `windows()` of its own to check instead, unlike
        GnomeShellBackend/SwayBackend/NiriBackend's own overrides of this
        method, which is why it shells out here rather than calling
        `self.windows()`.

        The *last* match among those, not the first, because
        `Session.find_window` is `find_windows(...)[-1]` over the window
        list, and on a KDE session that list comes from `KdotoolBackend`,
        which reads it with the same `kdotool search .` in the same order.
        Taking the last is therefore what makes a title several windows
        share mean the same window through both calls. See
        `win32.Win32Backend.wait_for_window`, where the convention is
        written out in full.
        """
        self.require(Capability.WINDOW_EVENTS)
        pattern = re.compile(title)
        existing = [
            (handle, found)
            for handle, found in self._existing_windows()
            if pattern.search(found)
        ]
        if existing:
            handle, found = existing[-1]
            return Window(handle=handle, backend=self, title=found)
        for event in self.window_events(timeout=timeout):
            if event.change in ("new", "title") and pattern.search(event.window.title):
                return event.window
        return None

    def _existing_windows(self):
        """(handle, title) for every window kdotool currently sees."""
        handles = self._runner(["kdotool", "search", "."]).splitlines()
        result = []
        for handle in handles:
            handle = handle.strip()
            if not handle:
                continue
            title = self._runner(["kdotool", "getwindowname", handle])
            result.append((handle, title))
        return result

    def close(self):
        """Unload the script and release the D-Bus service."""
        if self._scripting is not None:
            self._scripting.close()
            self._scripting = None
        if self._connection is not None and self._registration_id is not None:
            with contextlib.suppress(Exception):
                self._connection.unregister_object(self._registration_id)
            self._registration_id = None
        if self._own_name_id is not None:
            with contextlib.suppress(Exception):
                self._Gio.bus_unown_name(self._own_name_id)
            self._own_name_id = None


class _Closer:
    """Wraps a zero-argument cleanup callable as a `.close()`-able object."""

    def __init__(self, on_close):
        """Remember the callable to run on close."""
        self._on_close = on_close

    def close(self):
        """Run the wrapped cleanup callable."""
        self._on_close()
