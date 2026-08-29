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

__all__ = ["gio", "available", "BUS_NAME", "OBJECT_PATH", "call", "request"]

BUS_NAME = "org.freedesktop.portal.Desktop"
OBJECT_PATH = "/org/freedesktop/portal/desktop"
REQUEST_INTERFACE = "org.freedesktop.portal.Request"


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


def request(modules, connection, interface, method, signature, args):
    """Call a method that returns a Request handle, and await its reply.

    `args` must end with the options dict; a `handle_token` is added to it
    here, since the token is what makes subscribing before the call
    possible at all.

    The handle the call actually returns is subscribed to as well when it
    differs from the derived path -- the spec says it should not, but a
    portal is free to hand back something else, and the fake in
    tests/test_portal_dbusmock.py does exactly that. Listening on both is
    strictly safer than trusting either alone.

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

    def on_response(_conn, _sender, _path, _iface, _signal, params, *_):
        if result:  # both subscriptions may fire; first reply wins
            return
        result["code"], result["results"] = params.unpack()
        loop.quit()

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
            loop.run()
    finally:
        for subscription in subscriptions:
            connection.signal_unsubscribe(subscription)
    return result["code"], result["results"]
