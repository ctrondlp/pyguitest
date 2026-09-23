"""Capabilities and the tier scale.

The tiers come from the audit of all 50 X11::GUITest exports in
docs/developers/wayland-audit.md. They are ordered by implementation cost:
each costs strictly more than the one above it, and NO_PATH cannot be bought
at any price short of being the compositor.

A Capability is the unit a backend declares support for. Capabilities are
deliberately coarser than the legacy function list -- the audit's conclusion was
that a faithful 1-to-1 port would spend most of its effort on the parts most
worth redesigning. See compat.py for the mapping from the old names.
"""

from __future__ import annotations

from enum import Enum, IntEnum

__all__ = ["Tier", "Capability", "CapabilitySet", "TIERS"]


class Tier(IntEnum):
    """What it costs to implement a capability on Wayland."""

    PORTABLE = 1
    DIRECT = 2
    COMPOSITOR = 3
    PRIVILEGED = 4
    REWORK = 5
    NO_PATH = 6


TIERS = {
    Tier.PORTABLE: "No display server involved",
    Tier.DIRECT: "Core Wayland protocol",
    Tier.COMPOSITOR: "Per-desktop backend",
    Tier.PRIVILEGED: "Consent or device access",
    Tier.REWORK: "Goal survives, model does not",
    Tier.NO_PATH: "Deliberately prevented",
}
"""What each tier costs, in the words the capability report and the API
reference print beside it.

The member names alone do not carry it: "REWORK" tells a reader nothing it can
act on, where "goal survives, model does not" does. An attribute docstring
rather than a comment because scripts/gen-api-docs.py reads it -- `TIERS` is
public, and a dict has no `__doc__` of its own to hold one.
"""


class Capability(Enum):
    """An operation a backend may or may not support.

    Each member carries the tier that operation sits in *for a Wayland
    session*. An X11 backend supports nearly all of these cheaply; the tier
    recorded here is the Wayland ceiling, which is what determines whether a
    portable API can rely on it.
    """

    tier: Tier
    """The Wayland cost tier for this capability."""

    description: str
    """One line describing what the capability does."""

    def __new__(cls, tier: Tier, description: str) -> Capability:
        """Attach the tier and description carried by each member's value."""
        obj = object.__new__(cls)
        obj._value_ = len(cls.__members__) + 1
        obj.tier = tier
        obj.description = description
        return obj

    # -- T1: no display server involved ------------------------------------
    PROCESS_LAUNCH = (Tier.PORTABLE, "Start and run applications")
    TIMING = (Tier.PORTABLE, "Waits, and inter-event and inter-key delays")
    IMAGE_LOCATE = (
        Tier.PORTABLE,
        "Find a template image's position inside an already-captured "
        "screenshot by pixel comparison; a new feature, X11::GUITest never "
        "had it, and needs no live display connection, only files",
    )

    # -- T2: core Wayland protocol -----------------------------------------
    SCREEN_INFO = (Tier.DIRECT, "Output enumeration, resolution, scale (wl_output)")

    # -- T4: input injection -----------------------------------------------
    POINTER_MOVE = (
        Tier.PRIVILEGED,
        "Absolute pointer positioning; relative-only injection cannot do this",
    )
    POINTER_BUTTON = (Tier.PRIVILEGED, "Button press and release")
    POINTER_SCROLL = (
        Tier.PRIVILEGED,
        "Axis events; X11 buttons 4/5 were scroll, Wayland's are not",
    )
    KEY_EVENT = (Tier.PRIVILEGED, "Keycode press and release")
    TEXT_ENTRY = (
        Tier.PRIVILEGED,
        "Typing characters; needs a backend-controlled keymap, which raw uinput lacks",
    )
    INPUT_SYNC = (
        Tier.PRIVILEGED,
        "Confirm the compositor has consumed the events sent so far, instead "
        "of sleeping and hoping; proves delivery to the compositor, never "
        "that the application processed or repainted them",
    )
    INPUT_CAPTURE = (
        Tier.PRIVILEGED,
        "Learn where the pointer is from a real crossing of a screen edge "
        "the caller sets up, exclusively diverting real input for the "
        "length of one activation; not a query -- see Session."
        "wait_for_pointer_activation, which can return None rather than "
        "answer at all if nobody moves the pointer there in time",
    )

    # -- T3: per-desktop window backends -----------------------------------
    WINDOW_LIST = (Tier.COMPOSITOR, "Enumerate toplevels, read titles and app ids")
    WINDOW_EVENTS = (
        Tier.COMPOSITOR,
        "Subscribe to open/close/title-change instead of polling",
    )
    WINDOW_STATE = (Tier.COMPOSITOR, "Read minimized and activated state")
    WINDOW_ACTIVATE = (
        Tier.COMPOSITOR,
        "Raise and focus; there is no raise-without-focus operation",
    )
    WINDOW_MINIMIZE = (Tier.COMPOSITOR, "Minimize and restore")
    WINDOW_GEOMETRY = (
        Tier.COMPOSITOR,
        "Read position and size; no foreign-toplevel protocol carries this",
    )
    WINDOW_PLACEMENT = (
        Tier.COMPOSITOR,
        "Move to a position; placement is the compositor's prerogative",
    )
    WINDOW_RESIZE = (
        Tier.COMPOSITOR,
        "Change width and height; separate from placement because a tiling "
        "compositor can size a window without letting anyone position it",
    )
    WINDOW_PID = (Tier.COMPOSITOR, "Map a window to a process id; prefer app_id")
    WINDOW_AT_POINT = (
        Tier.COMPOSITOR,
        "Hit-test a coordinate; needs geometry and stacking order",
    )
    SCREEN_CAPTURE = (
        Tier.COMPOSITOR,
        "Pixels; a new feature, X11::GUITest never had it",
    )
    WINDOW_CAPTURE = (
        Tier.COMPOSITOR,
        "Pixels of one window, captured natively rather than cropped out of "
        "a screen shot -- so an occluded or offscreen window still comes "
        "back whole",
    )
    CLIPBOARD = (
        Tier.COMPOSITOR,
        "Read and write the clipboard's text content; a new feature, "
        "X11::GUITest never had it",
    )

    # -- T5: served by AT-SPI instead --------------------------------------
    ELEMENT_TREE = (
        Tier.REWORK,
        "Walk the accessible tree; replaces the X11 window-tree walk",
    )
    ELEMENT_ACTION = (
        Tier.REWORK,
        "Act on an element without coordinates or injection permission",
    )
    ELEMENT_GEOMETRY = (
        Tier.REWORK,
        "Read an element's screen rectangle, and hit-test a coordinate "
        "against the accessible tree; one capability rather than the two "
        "windows get, because on AT-SPI both are the same Component call "
        "and no toolkit answers one without the other. Where it is withheld "
        "is a per-backend answer, not a property of the capability: AT-SPI "
        "offers it on X11 and XWayland only, since a pure Wayland client is "
        "never told where it sits on screen, while UI Automation answers it "
        "on any Windows session",
    )

    # -- T6: no path -------------------------------------------------------
    POINTER_QUERY = (
        Tier.NO_PATH,
        "Read the global pointer position; injection works, readback does not",
    )
    INPUT_STATE_QUERY = (
        Tier.NO_PATH,
        "Read global keyboard or button state; this is what a keylogger reads",
    )
    WINDOW_TITLE_SET = (
        Tier.NO_PATH,
        "Rewrite another application's title; that is impersonation",
    )
    WINDOW_LOWER = (Tier.NO_PATH, "Lower or restack; only activate exists, upward only")
    WINDOW_CURSOR_QUERY = (
        Tier.NO_PATH,
        "Read the cursor shape over a window; no workaround anywhere on "
        "Wayland, and a scoped one on Windows, where the cursor being shown "
        "is readable but compared against the system's own handles rather "
        "than asked of a window",
    )

    def __str__(self) -> str:
        """The member's name, which is how a capability is written elsewhere."""
        return self.name


class CapabilitySet(frozenset):
    """The capabilities a backend actually provides.

    This is the negotiation surface the audit called for: with 19 legacy
    functions varying by compositor and 6 unavailable everywhere, callers need
    to ask before they depend.

        if gui.supports(Capability.WINDOW_GEOMETRY):
            ...
    """

    def by_tier(self, tier: Tier) -> CapabilitySet:
        """The supported capabilities in one tier."""
        return CapabilitySet(c for c in self if c.tier == tier)

    @property
    def missing(self) -> CapabilitySet:
        """Every capability this backend does not provide."""
        return CapabilitySet(c for c in Capability if c not in self)

    def report(self) -> str:
        """A user-readable support table, grouped by tier."""
        lines = []
        for tier in Tier:
            caps = sorted(
                (c for c in Capability if c.tier == tier), key=lambda c: c.name
            )
            if not caps:
                continue
            lines.append(f"T{int(tier)} {tier.name} -- {TIERS[tier]}")
            for cap in caps:
                lines.append(f"  [{'yes' if cap in self else ' no'}] {cap.name}")
        return "\n".join(lines)

    def __repr__(self) -> str:
        """The member names, in a form that can be pasted back into code."""
        return f"CapabilitySet({{{', '.join(sorted(c.name for c in self))}}})"

    # frozenset's set operators return a plain frozenset even on a
    # subclass instance, which drops .report() and .missing -- the two
    # methods this class exists for. Re-wrapped here so
    # `gui.capabilities | {...}` stays a CapabilitySet.
    def __or__(self, other) -> CapabilitySet:
        """Union, re-wrapped so the result keeps .report() and .missing."""
        return CapabilitySet(super().__or__(other))

    def __and__(self, other) -> CapabilitySet:
        """Intersection, re-wrapped for the same reason as __or__."""
        return CapabilitySet(super().__and__(other))

    def __sub__(self, other) -> CapabilitySet:
        """Difference, re-wrapped for the same reason as __or__."""
        return CapabilitySet(super().__sub__(other))

    def __xor__(self, other) -> CapabilitySet:
        """Symmetric difference, re-wrapped for the same reason as __or__."""
        return CapabilitySet(super().__xor__(other))

    # The named methods are the same defect one call along, and the four
    # operators above did not cover them: `union()` and its three siblings
    # are ordinary methods on frozenset, so nothing routes a call through
    # __or__ -- `caps.union({...})` came back a frozenset, and so did
    # `caps.copy()`, which is the one a caller reaches for before handing a
    # merge on. Re-wrapped on the same grounds.
    def union(self, *others) -> CapabilitySet:
        """Union with each of `others`, re-wrapped as __or__ is."""
        return CapabilitySet(super().union(*others))

    def intersection(self, *others) -> CapabilitySet:
        """Intersection with each of `others`, re-wrapped as __or__ is."""
        return CapabilitySet(super().intersection(*others))

    def difference(self, *others) -> CapabilitySet:
        """This set minus each of `others`, re-wrapped as __or__ is."""
        return CapabilitySet(super().difference(*others))

    def symmetric_difference(self, other) -> CapabilitySet:
        """The members in exactly one of this set and `other`, re-wrapped."""
        return CapabilitySet(super().symmetric_difference(other))

    def copy(self) -> CapabilitySet:
        """A shallow copy that is still a `CapabilitySet`."""
        return CapabilitySet(self)

    # And the reflected operators, which bite only with a frozenset on the
    # left: `frozenset({...}) | caps` asked frozenset's own __or__ and came
    # back a frozenset. Python consults a reflected method only when the
    # right operand's type is a subclass of the left's, which CapabilitySet
    # is of frozenset -- so these four are reached, and defining them is
    # what keeps the merge a CapabilitySet. A plain `set` on the left is not
    # reachable this way at all: `{...} | caps` is answered by set.__or__
    # (set and frozenset are siblings, neither a subclass of the other) and
    # comes back a mutable set, whose later mutation cannot affect
    # `gui.capabilities` and is silently discarded. Documented rather than
    # worked around: a wrapper type would have to lie about being a set.
    #
    # Written as `frozenset(other).union(self)` rather than the obvious
    # `super().__ror__(other)`, which mypy rejects: typeshed's frozenset stub
    # leaves the four reflected methods out even though the type has them at
    # runtime. Going through a plain frozenset computes the same set by the
    # same set algebra, and needs no `# type: ignore` to say so.
    def __ror__(self, other) -> CapabilitySet:
        """Union with `other` on the left, re-wrapped as __or__ is."""
        return CapabilitySet(frozenset(other).union(self))

    def __rand__(self, other) -> CapabilitySet:
        """Intersection with `other` on the left, re-wrapped as __or__ is."""
        return CapabilitySet(frozenset(other).intersection(self))

    def __rxor__(self, other) -> CapabilitySet:
        """Symmetric difference with `other` on the left, re-wrapped."""
        return CapabilitySet(frozenset(other).symmetric_difference(self))

    def __rsub__(self, other) -> CapabilitySet:
        """`other` minus this set, re-wrapped as __or__ is."""
        return CapabilitySet(frozenset(other).difference(self))
