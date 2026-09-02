"""The XDG portal request/response dance, shared by every portal backend.

Every portal method that can show UI works the same way: the immediate D-Bus
reply is only a handle, and the real result -- including whether the user
clicked Allow -- arrives later as a Response signal on that handle. Two
backends here need it against two different interfaces (RemoteDesktop for
injection, Screenshot for pixels), and the interesting part is a race that
is easy to reintroduce, so it lives in one place rather than being copied.

The race: subscribing only *after* the call returns loses a fast,
non-interactive response, which can be emitted before the subscription
exists -- the wait then blocks forever on a signal that already came and
went. It was reproduced repeatedly in eiinput.py, which performs the same
negotiation. The fix is the one xdg-desktop-portal documents: pick the
`handle_token` yourself, derive the request object path from it, and
subscribe *before* issuing the call.
"""

from __future__ import annotations

import uuid

from ..errors import PortalTimeout

__all__ = [
    "gio",
    "available",
    "BUS_NAME",
    "OBJECT_PATH",
    "DEFAULT_TIMEOUT",
    "call",
    "request",
]

BUS_NAME = "org.freedesktop.portal.Desktop"
OBJECT_PATH = "/org/freedesktop/portal/desktop"
REQUEST_INTERFACE = "org.freedesktop.portal.Request"

DEFAULT_TIMEOUT = 60
"""Seconds to wait for a Response before giving up, for requests that can
show UI -- generous, since a human has to see and answer the dialog.

Bounded at all because the alternative is worse than a late answer: a portal
that accepts the call and then dies (or a Response delivered to a path
nobody is listening on) sends nothing, and an unbounded `loop.run()` then
blocks the calling thread forever, with no fd to poll and no way to
interrupt it. A caller who genuinely wants to wait indefinitely can pass
`timeout=None`.
"""


def gio():
    """Import Gio and GLib, or return None.

    Same PyGObject dependency the atspi extra already needs -- see
    docs/adr-001-dependencies.md.
    """
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except Exception:
        return None
    return Gio, GLib


def available():
    """Whether the library the portal backends need is importable."""
    return gio() is not None


def call(modules, connection, interface, method, signature, args):
    """Call one portal method, returning its raw GVariant reply."""
    Gio, GLib = modules
    parameters = GLib.Variant(signature, args)
    return connection.call_sync(
        BUS_NAME,
        OBJECT_PATH,
        interface,
        method,
        parameters,
        None,
        Gio.DBusCallFlags.NONE,
        -1,
        None,
    )


def request(
    modules, connection, interface, method, signature, args, timeout=DEFAULT_TIMEOUT
):
    """Call a method that returns a Request handle, and await its reply.

    `args` must end with the options dict; a `handle_token` is added to it
    here, since the token is what makes subscribing before the call
    possible at all.

    The handle the call actually returns is subscribed to as well when it
    differs from the derived path -- the spec says it should not, but a
    portal is free to hand back something else, and the fake in
    tests/test_portal_dbusmock.py does exactly that. Listening on both is
    strictly safer than trusting either alone.

    Waits at most `timeout` seconds for the Response, raising
    `PortalTimeout` if none arrives; `timeout=None` waits indefinitely,
    which is what this did unconditionally before -- see DEFAULT_TIMEOUT for
    why that is no longer the default.

    Returns `(code, results)`: the portal's own response code (0 is
    success) and its results dictionary.
    """
    Gio, GLib = modules
    *leading, options = args
    token = uuid.uuid4().hex
    options = dict(options)
    options["handle_token"] = GLib.Variant("s", token)
    sender = connection.get_unique_name()[1:].replace(".", "_")
    expected = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"

    loop = GLib.MainLoop()
    result: dict = {}
    subscriptions: list = []
    timed_out = False

    def on_response(_conn, _sender, _path, _iface, _signal, params, *_):
        if result:  # both subscriptions may fire; first reply wins
            return
        result["code"], result["results"] = params.unpack()
        loop.quit()

    def on_timeout():
        # Recorded rather than inferred from an empty `result` afterwards: a
        # Response carrying no results is legitimate (SelectDevices answers
        # with an empty dict), so "did this end because it timed out" has to
        # be a fact, not a deduction.
        nonlocal timed_out
        timed_out = True
        loop.quit()
        return False  # one-shot; GLib drops the source when this is False

    def subscribe(path):
        subscriptions.append(
            connection.signal_subscribe(
                BUS_NAME,
                REQUEST_INTERFACE,
                "Response",
                path,
                None,
                Gio.DBusSignalFlags.NONE,
                on_response,
                None,
            )
        )

    subscribe(expected)
    try:
        (handle,) = call(
            modules, connection, interface, method, signature, (*leading, options)
        ).unpack()
        if handle != expected:
            subscribe(handle)
        # A fake connection may answer synchronously, before run() is ever
        # reached; running the loop then would block with nothing left to
        # deliver it.
        if not result:
            source = None
            if timeout is not None:
                source = GLib.timeout_add_seconds(timeout, on_timeout)
            try:
                loop.run()
            finally:
                # Leaving a live source holds a reference to this closure
                # and fires it into a dead loop on some later request, so
                # it has to go -- but only if it is still there. on_timeout
                # returns False, which makes GLib drop the source itself,
                # and removing it again is not merely redundant: GLib logs
                # "Source ID N was not found when attempting to remove it"
                # every time, which is noise on the one path where the
                # output is being read for a reason.
                if source is not None and not timed_out:
                    GLib.source_remove(source)
    finally:
        for subscription in subscriptions:
            connection.signal_unsubscribe(subscription)
    if timed_out:
        raise PortalTimeout(method, timeout)
    return result["code"], result["results"]
