"""Combining backends.

No single mechanism covers a desktop. A GNOME session might get elements and
window listing from AT-SPI, injection from an input tool, and capture from
gnome-screenshot -- three backends, one session. This merges them and routes
each call to whichever member actually provides the capability.

Precedence is registration order, so a higher-priority backend wins a capability
both provide. That matters for WINDOW_GEOMETRY: AT-SPI's coordinates are
unreliable under Wayland, so a compositor IPC backend that also offers it should
be preferred.

One operation genuinely needs two members at once. `capture(window=...)` is
the join of a window's rectangle and the pixels covering it, and on most
desktops those live in different backends -- the compositor IPC or Shell
extension knows the geometry, the screenshot tool knows the pixels, and
neither can do the other's half. Routing to a single provider the way every
other operation does is what left per-window capture unavailable even on a
session that had both halves installed, so `capture` is written out by hand
below instead of generated.
"""

import warnings

from ..capabilities import Capability, CapabilitySet
from ..errors import CapabilityUnsupported, PyGUITestError
from .base import GUIBackend, check_region

__all__ = ["CompositeBackend", "CaptureFallbackWarning"]


_NO_CAPTURE_MEMBER = (
    "no member backend provides it. This is the normal state of a GNOME "
    "Wayland session: gnome-screenshot cannot reach the Shell's screenshot "
    "interface and is not selected, and XWayland refuses to read the X root, "
    'so nothing is composed automatically. connect(backend="portalcapture") '
    "captures there with no tool installed -- it is opt-in only because its "
    "first use prompts for consent, which the desktop then remembers."
)
"""Why a composite may have no way to capture, and what to do about it.

Shared by both paths that can report it so they cannot drift apart. The
advice is worth carrying in the error and not only in `pyguitest doctor`:
this is reached at the moment someone calls screenshot() and it fails,
which is exactly when they are looking for the next thing to try.
"""


class CaptureFallbackWarning(UserWarning):
    """A capture backend failed and a later one was used instead.

    Warned rather than swallowed because the fallback hides a real,
    fixable problem: a screenshot tool that is installed, was selected,
    and does not work. Nothing else would tell anyone -- `pyguitest
    doctor` reports it as present, and the capture it silently rescued
    succeeded.
    """


# Which capability each operation needs, so dispatch can find its provider.
_DISPATCH = {
    "screens": Capability.SCREEN_INFO,
    "move_mouse": Capability.POINTER_MOVE,
    "press_button": Capability.POINTER_BUTTON,
    "release_button": Capability.POINTER_BUTTON,
    "scroll": Capability.POINTER_SCROLL,
    "press_key": Capability.KEY_EVENT,
    "release_key": Capability.KEY_EVENT,
    "type_text": Capability.TEXT_ENTRY,
    "windows": Capability.WINDOW_LIST,
    "active_window": Capability.WINDOW_STATE,
    "is_window_viewable": Capability.WINDOW_STATE,
    "window_at": Capability.WINDOW_AT_POINT,
    "geometry": Capability.WINDOW_GEOMETRY,
    "move_window": Capability.WINDOW_PLACEMENT,
    "resize_window": Capability.WINDOW_RESIZE,
    "activate_window": Capability.WINDOW_ACTIVATE,
    "minimize_window": Capability.WINDOW_MINIMIZE,
    "window_events": Capability.WINDOW_EVENTS,
    "wait_for_window": Capability.WINDOW_EVENTS,
    "locate": Capability.IMAGE_LOCATE,
    "root_element": Capability.ELEMENT_TREE,
    "find_elements": Capability.ELEMENT_TREE,
    "find_element": Capability.ELEMENT_TREE,
    "get_clipboard": Capability.CLIPBOARD,
    "set_clipboard": Capability.CLIPBOARD,
    # Tier 6. These have no stub on GUIBackend -- they exist only on the
    # backend that can serve them, which today is X11Backend alone -- but
    # they are capability-routed like everything above, so they belong
    # here rather than being left to a caller. Without them a composite
    # advertised POINTER_QUERY (Session.supports said yes, because a
    # member declared it) and then answered `gui.pointer_position()` with
    # AttributeError, since CompositeBackend has no __getattr__ to fall
    # through to. That is the normal X11 session, not a corner case:
    # `connect()` composes, so it hit every caller of the tier-6
    # operations, Session.glide/drag's own origin lookup included.
    "pointer_position": Capability.POINTER_QUERY,
    "is_button_pressed": Capability.INPUT_STATE_QUERY,
    "is_key_pressed": Capability.INPUT_STATE_QUERY,
    "set_window_title": Capability.WINDOW_TITLE_SET,
    "lower_window": Capability.WINDOW_LOWER,
    "is_window_cursor": Capability.WINDOW_CURSOR_QUERY,
}


class CompositeBackend(GUIBackend):
    """Several backends presented as one."""

    def __init__(self, members):
        """Combine `members`, earlier ones taking precedence."""
        self.members = list(members)
        if not self.members:
            raise ValueError("a composite needs at least one member")
        self._capture_failed: dict = {}
        """Members whose capture() has already failed, and why.

        A broken screenshot tool is not a transient condition -- the one
        that prompted this hung for the full 15s timeout on every call --
        so a member that fails once is skipped for the rest of the
        session rather than re-tried. Keyed by id() because backends are
        not required to be hashable."""

    # A read-only override of GUIBackend's plain, writable `name` attribute
    # -- see the same note in input.py. Nothing assigns to it externally.
    @property
    def name(self) -> str:  # type: ignore[override]
        """The member names joined, e.g. 'sway+atspi'."""
        return "+".join(m.name for m in self.members)

    @property
    def capabilities(self):
        """The union of every member's capabilities."""
        merged = set()
        for member in self.members:
            merged |= set(member.capabilities)
        return CapabilitySet(merged)

    # -- send_keys() key-name tables ---------------------------------------
    #
    # Session.send_keys() reads these three straight off self.backend, then
    # hands the names it builds to press_key/release_key -- which dispatch
    # (below) to whichever member provides KEY_EVENT. Left as GUIBackend's
    # plain class attributes, a composite would build names in its own
    # inherited X11-keysym vocabulary while routing the actual key press to
    # a member that speaks a different one (uinput's evdev names), e.g.
    # send_keys("^(a)") pressing "Control_L" against UinputBackend, which
    # only knows "LEFTCTRL". Confirmed live on KDE/KWin, where uinput is the
    # composite's only KEY_EVENT provider.

    @property
    def MODIFIER_KEYS(self):  # type: ignore[override]
        """send_keys()'s modifiers, in the KEY_EVENT provider's vocabulary."""
        provider = self.provider(Capability.KEY_EVENT)
        return GUIBackend.MODIFIER_KEYS if provider is None else provider.MODIFIER_KEYS

    @property
    def KEY_ALIASES(self):  # type: ignore[override]
        """send_keys()'s `{BAC}`-style abbreviations, ditto."""
        provider = self.provider(Capability.KEY_EVENT)
        return GUIBackend.KEY_ALIASES if provider is None else provider.KEY_ALIASES

    def resolve_char_key(self, char):
        """The (key name, needs_shift) send_keys() uses to press one char."""
        provider = self.provider(Capability.KEY_EVENT)
        if provider is None:
            return super().resolve_char_key(char)
        return provider.resolve_char_key(char)

    def provider(self, capability):
        """The member that serves `capability`, or None."""
        for member in self.members:
            if capability in member.capabilities:
                return member
        return None

    def member(self, name):
        """The member backend called `name`.

        How a caller reaches one backend's own extras through a composite --
        `session.backend.member("eiinput").restore_token`, the composed
        spelling of the `session.backend.restore_token` that `connect()`
        documents for a single named backend.

        Deliberately explicit rather than a `__getattr__` that forwards
        unknown attributes to whichever member happens to have one. Every
        other route through this class is capability-routed and written out
        (see this module's docstring on `capture`); a catch-all would answer
        `restore_token` from whichever backend was first in the list, which
        is exactly the kind of quiet wrong answer the rest of the class is
        built to avoid.

        The registry name is enough: a tool-backed backend reports which
        tool it found in its own name (`imagesearch:compare`,
        `input:wtype`, `capture:grim`, `clipboard:wl-paste`), and asking for
        `member("imagesearch")` should not require knowing which one was
        installed. The full name matches too, for a caller that has one.
        """
        for member in self.members:
            if member.name == name or member.name.split(":", 1)[0] == name:
                return member
        raise PyGUITestError(
            f"no member backend named {name!r}; this composite has "
            f"{', '.join(m.name for m in self.members)}"
        )

    def providers(self):
        """A capability -> backend name map, for diagnostics."""
        return {
            cap.name: provider.name
            for cap in Capability
            if (provider := self.provider(cap)) is not None
        }

    @staticmethod
    def _implements(provider, attr):
        """Whether `provider` actually overrides `attr`.

        Declaring a capability is not the same as implementing it: every
        dispatched method also exists on GUIBackend as a stub that raises
        NotImplementedError, so hasattr() would say yes for a backend that
        never overrode it.
        """
        overridden = getattr(type(provider), attr, None)
        return overridden is not None and overridden is not getattr(
            GUIBackend, attr, None
        )

    def _check_implemented(self, provider, attr, capability):
        """Raise unless `provider` actually overrides `attr`.

        A plain hasattr() check would miss the common case: every dispatched
        method also exists on GUIBackend itself, as a stub that raises
        NotImplementedError -- so a member that declares the capability
        without actually overriding the method would satisfy hasattr() and
        then raise that bare, untyped error instead of the typed
        CapabilityUnsupported every other unsupported operation raises.
        Comparing the unbound function catches both that case and a member
        missing the method outright (GUIBackend.<attr> is then None too, so
        the comparison still holds).
        """
        if not self._implements(provider, attr):
            raise CapabilityUnsupported(
                capability,
                self.name,
                f"{provider.name} declares {capability.name} but has no {attr}()",
            )

    def _issuer(self, window):
        """The backend a Window names as its own, or None for a raw handle.

        Window.handle is documented backend-private: an X11 handle is an
        Xlib drawable, the GNOME Shell extension's is a stable_sequence
        integer, sway's is a node id. They are not interchangeable, so a
        Window issued by one backend cannot be passed to another --
        doing it turned a working per-window capture into
        "'int' object has no attribute 'get_geometry'" the first time a
        composite paired a Shell-extension window list with X11 capture.

        None means the caller passed a raw handle rather than a Window.
        Only the caller knows whose namespace that is from, so the
        historic behaviour -- hand it to the capability's provider -- is
        kept for it.
        """
        return getattr(window, "backend", None)

    def _owner(self, window):
        """The *member* that issued `window`, or None.

        Distinct from _issuer on purpose. A Window naming a backend that
        is not a member of this composite is not a raw handle -- it is
        positively foreign, from a namespace nothing here can read -- so
        it must not be treated as "unknown, therefore fine to pass along".
        """
        issuer = self._issuer(window)
        return issuer if issuer in self.members else None

    def _geometry_of(self, window):
        """`window`'s rectangle, asked of the member that owns it.

        Prefers the owning member over the WINDOW_GEOMETRY provider for
        the same reason as above: the provider might be a different
        backend that cannot read this handle at all. Falls back to the
        provider when the owner does not serve geometry, or when the owner
        is unknown.
        """
        owner = self._owner(window)
        if owner is not None and Capability.WINDOW_GEOMETRY in owner.capabilities:
            source = owner
        else:
            source = self.provider(Capability.WINDOW_GEOMETRY)
        if source is None:
            return None
        self._check_implemented(source, "geometry", Capability.WINDOW_GEOMETRY)
        return source.geometry(window)

    def _capture_members(self):
        """Every member that can actually grab pixels, in precedence order."""
        return [
            member
            for member in self.members
            if Capability.SCREEN_CAPTURE in member.capabilities
            and self._implements(member, "capture")
        ]

    def _grab(self, path, region):
        """Capture through the first member that succeeds.

        One broken tool must not take the session's capture down with it.
        That is not a hypothetical: `gnome-screenshot -f` on GNOME Shell
        50.4 cannot reach the Shell's screenshot interface (post-42 it is
        restricted to an allowlist of senders), falls back to X11, which
        on a Wayland session grabs nothing, and then hangs until the
        timeout -- while python-xlib sat in the same composite, able to
        capture natively. The README already documents the identical
        shape for ImageMagick's `import` on Fedora 43. "Installed but
        non-functional" is a recurring condition here, not an accident.

        A member that fails is remembered and skipped for the rest of the
        session: the failure is a property of the installation, not of
        the moment, and re-trying it would pay the same 15s timeout on
        every single capture.

        Failing over is deliberately noisy. Silently rescuing the call
        would hide a real and fixable problem -- nothing else reports it,
        since `pyguitest doctor` sees the tool as present and the capture
        the fallback rescued did succeed.
        """
        candidates = self._capture_members()
        if not candidates:
            raise CapabilityUnsupported(
                Capability.SCREEN_CAPTURE,
                self.name,
                _NO_CAPTURE_MEMBER,
            )

        errors = []
        for member in candidates:
            previous = self._capture_failed.get(id(member))
            if previous is not None:
                errors.append((member, previous))
                continue
            try:
                return member.capture(path=path, region=region)
            except PyGUITestError as exc:
                # Only a failure of the *mechanism* falls through. A
                # ValueError from a malformed region is the caller's
                # mistake and would fail identically on every member, so
                # it is left to propagate rather than retried four times.
                self._capture_failed[id(member)] = exc
                errors.append((member, exc))
                if member is not candidates[-1]:
                    warnings.warn(
                        f"{member.name} failed and will be skipped for the "
                        f"rest of this session: {exc}",
                        CaptureFallbackWarning,
                        stacklevel=3,
                    )

        if len(errors) == 1:
            raise errors[0][1]
        first = errors[0][1]
        summary = "; ".join(f"{member.name}: {exc}" for member, exc in errors)
        # The one path worth naming here is the one that was never tried:
        # portalcapture is registered opt_in, so automatic composition
        # never includes it. Naming the members that just failed would be
        # advice to retry what is listed as broken in the same sentence.
        raise PyGUITestError(
            f"every capture backend failed -- {summary}. One capture path is "
            "never composed automatically, because its first use prompts for "
            'consent: connect(backend="portalcapture") goes through the '
            "Screenshot portal and needs no tool installed."
        ) from first

    def capture(self, window=None, path=None, region=None):
        """Screenshot the desktop, one window, or one rectangle.

        The only operation here that can need two members. Three routes, in
        order:

        1. a member declaring WINDOW_CAPTURE gets a `window` straight
           through -- a native per-window grab, so an occluded or partly
           offscreen window still comes back whole and undamaged. Only
           when that member is the one that *issued* the window, though:
           handles are backend-private, so a foreign one is meaningless to
           it. See _issuer.
        2. otherwise a `window` is resolved to its rectangle -- through the
           member that owns it where possible -- and handed to a
           SCREEN_CAPTURE member as a region. Cropped out of a screen shot,
           so whatever is stacked on top of the window is in the image too
           -- the honest limit of this route, and why it is second;
        3. no window: SCREEN_CAPTURE alone, with any region passed on.

        The pixel grab itself is tried against each capable member in turn
        rather than only the first -- see _grab.

        Raises CapabilityUnsupported naming the missing half, rather than
        the generic "no member provides it" -- on a session with a
        screenshot tool but no geometry source, knowing *which* piece is
        absent is the difference between a fixable message and a dead end.
        """
        # Validated here, once, rather than left to whichever member the
        # grab happens to reach. Every backend checks it too, but relying
        # on that would make a malformed region's error depend on which
        # member won -- and would let a caller's mistake be mistaken for a
        # broken backend and fail the call over to the next one.
        region = check_region(region, window)

        # The native per-window grab is tried before anything is required
        # of SCREEN_CAPTURE, because it needs nothing from it. Checking
        # for a pixel-grabbing member first refused a capture that would
        # have worked: on GNOME Wayland, X11Backend provides
        # WINDOW_CAPTURE (GetImage on an X11 client's own drawable
        # succeeds there -- measured) while SCREEN_CAPTURE has no provider
        # at all, since the root is unreadable and no screenshot tool can
        # run. That is a real session, not a contrived one.
        if window is not None:
            native = self.provider(Capability.WINDOW_CAPTURE)
            issuer = self._issuer(window)
            if native is not None and issuer in (None, native):
                self._check_implemented(native, "capture", Capability.WINDOW_CAPTURE)
                return native.capture(window=window, path=path)

        if not self._capture_members():
            raise CapabilityUnsupported(
                Capability.SCREEN_CAPTURE, self.name, _NO_CAPTURE_MEMBER
            )
        if window is None:
            return self._grab(path, region)

        rectangle = self._geometry_of(window)
        if rectangle is None:
            raise CapabilityUnsupported(
                Capability.WINDOW_CAPTURE,
                self.name,
                f"{self._capture_members()[0].name} can capture pixels but no "
                "member provides WINDOW_GEOMETRY or WINDOW_CAPTURE, so a "
                "window cannot be resolved to a rectangle",
            )
        return self._grab(path, rectangle)

    def close(self):
        """Close every member."""
        for member in self.members:
            member.close()

    def report(self):
        """Show which member serves which capability."""
        lines = [f"composite of {len(self.members)} backend(s)"]
        for member in self.members:
            served = sorted(
                cap.name for cap in member.capabilities if self.provider(cap) is member
            )
            summary = ", ".join(served) or "nothing (shadowed)"
            lines.append(f"  {member.name}: {summary}")
        return "\n".join(lines)


def _delegate(attr, capability):
    """Build a method that routes `attr` to whichever member provides it."""

    def method(self, *args, **kwargs):
        """Call the member that provides this capability."""
        provider = self.provider(capability)
        if provider is None:
            raise CapabilityUnsupported(
                capability, self.name, "no member backend provides it"
            )
        self._check_implemented(provider, attr, capability)
        return getattr(provider, attr)(*args, **kwargs)

    method.__name__ = attr
    method.__qualname__ = f"CompositeBackend.{attr}"
    method.__doc__ = f"Delegated to the member providing {capability.name}."
    return method


# Bound explicitly rather than through __getattr__. That hook only fires when
# normal attribute lookup *fails*, and every one of these names is defined on
# GUIBackend -- so __getattr__ would never run and each call would hit the
# base class's raising stub instead of the member that can serve it.
for _attr, _capability in _DISPATCH.items():
    setattr(CompositeBackend, _attr, _delegate(_attr, _capability))
