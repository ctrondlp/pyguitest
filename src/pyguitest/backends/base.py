"""The backend interface.

Shaped by capability rather than by the 50 legacy function names -- the audit
found that only 13 of those port unchanged, 6 should not exist, and the rest
change shape, so a faithful 1-to-1 interface would spend most of its surface on
the parts most worth redesigning.

A backend implements what it can and declares it in `capabilities`. Every
unimplemented operation raises CapabilityUnsupported by default, so a partial
backend is a valid backend -- which is the normal case, not the exception.
"""

from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, Protocol, TypeVar

from ..capabilities import Capability, CapabilitySet
from ..errors import CapabilityUnsupported, ElementNotActionable

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    # Imported for annotations only: windows.py imports this module, and
    # WindowEvent is the one name from it the interface has to name.
    from .windows import WindowEvent

__all__ = [
    "GUIBackend",
    "Window",
    "Screen",
    "ImageMatch",
    "Element",
    "check_region",
    "fit_region",
    "identity_scope",
    "click_by_pointer",
    "resolve_click_route",
    "require_click_route",
    "numeric_value",
]

_Backend = TypeVar("_Backend", bound="GUIBackend")


def check_region(
    region: Sequence[float] | None,
    window: Window | None = None,
) -> tuple[int, int, int, int] | None:
    """Validate a capture region and return it as four ints.

    One place rather than four: every capture backend takes the same
    normalized `(x, y, width, height)` in screen coordinates, and every one
    of them would otherwise repeat the same three checks slightly
    differently.

    A zero or negative width or height is rejected rather than passed
    through. The tools disagree on what they do with one -- grim errors,
    ImageMagick's `-crop 0x0` quietly yields the *whole* image -- and a
    caller who asked for an empty rectangle and got back a full-screen
    shot has the worst possible outcome: a plausible-looking image of the
    wrong thing.

    Fractional coordinates are *covered*, not truncated: the origin is
    floored and the far edge ceiled, so every pixel the caller named is in
    the capture. Fractions arrive honestly -- element extents on a 150%
    display, a midpoint of an odd width -- and `int()` here used to drop
    them silently, shaving the right and bottom edges off exactly the
    HiDPI captures that already had the least room for error (and rounding
    *toward zero*, so a region at x=-0.5 on a monitor left of the primary
    moved right while every positive one moved left). Integer regions pass
    through unchanged.
    """
    if region is None:
        return None
    if window is not None:
        raise ValueError(
            "capture() takes window= or region=, not both: region is "
            "screen-absolute, so pairing it with a window would silently "
            "mean one thing or the other"
        )
    try:
        # OverflowError too: float() raises it for a Python int beyond
        # double range (10**400), which int() used to accept silently.
        x, y, width, height = (float(value) for value in region)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            f"region must be four numbers (x, y, width, height), got {region!r}"
        ) from exc
    # NaN slips past the size check below (every comparison with it is
    # False) and infinity passes it outright; both would then blow up in
    # floor/ceil with an error naming neither the argument nor the caller.
    # The far edges are checked too: two finite halves can still sum to
    # infinity (x = width = 1e308), and ceil(inf) is the same bare blow-up.
    if not all(map(math.isfinite, (x, y, width, height, x + width, y + height))):
        raise ValueError(
            f"region must be four finite numbers (x, y, width, height), got {region!r}"
        )
    if width <= 0 or height <= 0:
        raise ValueError(
            f"region must have a positive width and height, got {width}x{height}"
        )
    left = math.floor(x)
    top = math.floor(y)
    covered = (left, top, math.ceil(x + width) - left, math.ceil(y + height) - top)
    if covered[2] <= 0 or covered[3] <= 0:
        # An origin past 2**53 swallows the width in float addition
        # (x + width == x), collapsing the covered size to zero -- which
        # would hand ImageMagick a `-crop 0x...` that quietly yields the
        # *whole* image, the exact outcome the size check above refuses.
        raise ValueError(
            f"region {region!r} is too large to resolve to pixels: its "
            "origin is beyond float precision, so its size collapses to zero"
        )
    return covered


def fit_region(
    region: tuple[int, int, int, int],
    size: tuple[int, int],
    source: str = "screen",
) -> tuple[int, int, int, int]:
    """Fit a covered region into `size`, refusing one genuinely outside.

    `check_region` covers fractional coordinates outward -- floor the
    origin, ceil the far edge -- so a region computed from extents flush
    with a screen edge legitimately lands one pixel past it. One pixel per
    side is exactly as much as covering can add, so up to that much is
    fitted back in; anything further out cannot be rounding, and is
    refused with a ValueError naming both rectangles, because capturing it
    anyway returns a smaller or shifted image under a success exit -- a
    plausible-looking picture of the wrong thing.

    Every capture path that knows its bounds applies this same policy, so
    one region means one outcome across backends. The fit cannot tell a
    covering artifact from an integer caller error exactly one pixel out
    -- the fraction is gone by the time this runs -- and chooses to serve
    the HiDPI edge case; a caller needing the strict answer can compare
    the returned region with the one it passed.
    """
    width_limit, height_limit = size
    x, y, width, height = region
    left, top = max(x, 0), max(y, 0)
    right = min(x + width, width_limit)
    bottom = min(y + height, height_limit)
    past_covering = (
        left - x > 1
        or top - y > 1
        or (x + width) - right > 1
        or (y + height) - bottom > 1
    )
    if past_covering or right <= left or bottom <= top:
        raise ValueError(
            f"region {width}x{height} at ({x}, {y}) is not inside the "
            f"{width_limit}x{height_limit} {source}: capturing it would "
            "return a smaller or empty image instead of the rectangle "
            "asked for"
        )
    return (left, top, right - left, bottom - top)


class Screen:
    """One output."""

    __slots__ = ("index", "width", "height", "scale", "name")

    def __init__(
        self,
        index: int,
        width: int,
        height: int,
        scale: float = 1.0,
        name: str = "",
    ) -> None:
        """Describe one output.

        `scale` is the output's DPI divided by 96 — 1.0 unscaled, 1.5 on a
        144 DPI monitor — and never a pixel count: `width` and `height` are
        physical pixels, the same numbers `geometry()` and `screenshot()`
        work in. Windows reports it as `GetDpiForMonitor` over 96, which is
        the definition the other platforms' answers are normalised to as
        well.
        """
        self.index = index
        self.width = width
        self.height = height
        self.scale = scale
        self.name = name

    @property
    def size(self) -> tuple[int, int]:
        """The output's (width, height) in pixels."""
        return (self.width, self.height)

    def __repr__(self) -> str:
        """Index, size, and the scale or name when they are worth showing."""
        return (
            f"Screen({self.index}, {self.width}x{self.height}"
            f"{f'@{self.scale}x' if self.scale != 1.0 else ''}"
            f"{f' {self.name!r}' if self.name else ''})"
        )


class ImageMatch(NamedTuple):
    """Where a template image was found.

    In whichever coordinate space it was searched in -- screen-absolute or
    otherwise -- the same way geometry()'s (x, y, width, height) leaves that
    to the caller.
    """

    x: int
    y: int
    width: int
    height: int
    score: float


def identity_scope(backend: GUIBackend | None) -> object:
    """What a Window's identity is compared within.

    The backend itself, unless it is a member of a composite -- in which
    case every member of that composite shares one scope, so a Window
    issued by any of them is comparable with a Window issued by any other.

    That grouping is what makes a desktop whose window capabilities are
    split across members work at all: KDE serves WINDOW_LIST from
    `kdotool` and WINDOW_EVENTS from `KWinEventsBackend`, and the two
    speak the same KWin UUIDs. `CompositeBackend.__init__` is what sets
    it; nothing else assigns to `_identity_scope`, and a backend used on
    its own keeps the strict per-backend rule it always had.
    """
    return getattr(backend, "_identity_scope", None) or backend


def numeric_value(raw: Any) -> float | None:
    """What `Element.value` reports for whatever the toolkit published.

    One rule in one place, because all three toolkits answer this question
    with a different shape and each backend used to decide for itself what
    counted as a number -- which is how they came to disagree. AT-SPI's
    `value` is `False` on anything with no Value interface (dogtail's own
    answer, measured on live GNOME and KDE trees: every role but `slider`,
    `level bar` and `scroll bar`), UIA answers only where the RangeValue
    pattern exists, and AX puts a slider's position and a check box's tick
    in the same attribute. The property is annotated `float | None`, so this
    is where everything that is not a float becomes None.

    A bool is excluded even though Python counts it as an int: `True` would
    otherwise widen to 1.0 and a ticked check box would read as a position
    of 1.0, which is exactly how a tick gets compared against a slider's
    range by accident. A string is excluded because a toolkit putting text
    there is answering `text`'s question, not this one.

    `raw` is `Any` rather than `object` deliberately: it is whatever a
    D-Bus, COM or Objective-C bridge handed back, and the point of this
    function is to duck-type it with `float()` and catch the failure. Typing
    it `object` only moves that cast to a `type: ignore` at the one line that
    does the work.

    Not given the bounded-call treatment the backends wrap their own reads
    in: this is pure arithmetic on a value already in hand, so the caller
    does the `_run_bounded`/`_current` read and hands the result here --
    which is also what keeps a `BackendCallTimeout` propagating rather than
    being flattened into "no value".
    """
    if isinstance(raw, (bool, str)) or raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def click_by_pointer(element: Element, reason: str) -> None:
    """Click `element` through the pointer, or refuse with the typed error.

    The last route of `Element.click` on the two backends whose toolkits
    publish an action per widget and nothing at all for the widgets that
    publish none: UI Automation and AX. Both ended their ladder with a
    refusal and a hint to click by coordinate; both now take that step
    themselves. The element stays the locator and only the gesture falls
    back to the pointer, which is the shape `Element.double_click` already
    had, and which needs `Capability.ELEMENT_GEOMETRY` plus the pointer
    capabilities -- those belong to whichever input backend the session was
    built with, not to the accessibility one.

    `AtspiBackend` reaches here too, as a second rung rather than a second
    attempt at the same wall: it tries dogtail's own `Node.click()` first,
    but modern dogtail injects through GNOME's gnome-ponytail-daemon, so on
    a desktop without that daemon -- GhostBSD/X11, KWin, sway -- dogtail
    refuses while this session's `x11`/`uinput`/compositor pointer clicks
    perfectly well. The two rungs have different dependencies, which is why
    trying the second one is worth doing.

    `reason` is the backend's own account of what it tried, in this
    platform's words; this appends the one thing the pointer route needs and
    may not have. Raises `ElementNotActionable` rather than doing nothing --
    where there is no session to click through, which is an element taken
    straight from a backend, and where the element publishes no rectangle to
    aim at. `Session.click_element` is this call spelled for an element with
    no session of its own.
    """
    session = getattr(element, "_session", None)
    if session is None:
        raise ElementNotActionable(
            element.role,
            element.name,
            f"{reason}. It carries no session to click through either, so "
            "there is nothing to aim the pointer with: it came from a backend "
            "directly rather than from a Session. Take the element from the "
            "session instead -- gui.button(...), gui.element(...), "
            "gui.root_element() -- or call gui.click_element(element)",
        )
    if session.extents(element) is None:
        raise ElementNotActionable(
            element.role,
            element.name,
            f"{reason}, and it publishes no rectangle for that to aim at, so "
            "the coordinate route is closed too: gui.extents(element) has "
            "nothing to hand back",
        )
    session.click_element(element)


CLICK_ROUTES = ("action", "pointer", "auto")
"""The routes `Element.click(via=...)` accepts, and what each means.

`"action"` is the accessible action only; `"pointer"` is the user pathway
only (extents, move, click); `"auto"` is today's ladder -- action first, with
the pointer as the fallback -- and is the default so no existing script
changes behaviour.
"""


def resolve_click_route(element: Element, via: str | None) -> str:
    """The concrete click route: `via` resolved against the session default.

    `via=None` means "use the session default", `Session.click_via`, which is
    `"auto"` unless set otherwise. An unknown value is refused here rather
    than silently acting like `"auto"`, because a caller who spelled a route
    wrongly should hear about it.
    """
    route = via
    if route is None:
        session = getattr(element, "_session", None)
        route = getattr(session, "click_via", "auto") if session is not None else "auto"
    if route not in CLICK_ROUTES:
        raise ValueError(
            f"unknown click route {route!r}; expected one of {', '.join(CLICK_ROUTES)}"
        )
    return route


def require_click_route(element: Element, route: str) -> None:
    """Fail loudly now rather than no-op later, for the route `via` picked.

    `"pointer"` needs the pointer half -- geometry to aim at, and move/button
    to inject -- which a headless or consent-less session may not have;
    `"action"` needs the accessible action. `"auto"` declares nothing, since
    it degrades the way it always has. An element with no session behind it
    has nothing to require, and the route itself reports the real problem.
    """
    session = getattr(element, "_session", None)
    if session is None:
        return
    if route == "pointer":
        session.require(
            Capability.ELEMENT_GEOMETRY,
            Capability.POINTER_MOVE,
            Capability.POINTER_BUTTON,
        )
    elif route == "action":
        session.require(Capability.ELEMENT_ACTION)


class Window:
    """A toplevel window.

    Deliberately not an integer. X11 window ids are reusable, so a stale id can
    silently address a different window; Wayland toplevel handles are objects
    with a lifetime, and this mirrors that. `handle` is backend-private.
    """

    __slots__ = ("handle", "backend", "title", "app_id", "pid")

    def __init__(
        self,
        handle: object,
        backend: GUIBackend,
        title: str = "",
        app_id: str = "",
        pid: int | None = None,
    ) -> None:
        """Describe one toplevel window."""
        self.handle = handle
        self.backend = backend
        self.title = title
        self.app_id = app_id
        self.pid = pid

    def __eq__(self, other: object) -> bool:
        """Whether both describe the same window, within one identity scope.

        By handle, never by title: two windows can share a title, and a
        title can change while the window stays put -- GNOME Text Editor
        renames itself the moment it has content, which is enough to make
        a title comparison answer "different window" about the window you
        are looking at.

        The *scope* is the backend, or the composite it belongs to where
        there is one -- see `identity_scope`. Scoping to the bare backend
        was the original rule, and it broke the moment one desktop served
        two window capabilities from two different members: on KDE
        `kdotool` lists windows and `KWinEventsBackend` watches them, both
        speaking the same KWin UUIDs, and a Window from `wait_for_window()`
        compared unequal to the identical window from `windows()`. That
        made `Session.is_window_open` answer False about an open window,
        `refresh_window` answer None, and `wait_window_close` return True
        immediately -- a test waiting on a dialog sailing straight past it.

        The cost is the case the old rule was aimed at, and it is worth
        stating plainly: two members of one composite that hand out
        unrelated handles from overlapping value spaces (small integers,
        say) can now false-match on `106 == 106`. That risk is confined to
        members of the same composite, which is a set the caller composed
        deliberately; between unrelated backends the strict rule still
        holds.

        What this cannot tell you: whether a handle has gone stale. An X11
        id is reusable (see the class docstring), so a window equal to one
        from five minutes ago may be a different window wearing a recycled
        id. `Session.is_window_open` and `Session.refresh_window` are the
        way to ask about *now*.
        """
        if not isinstance(other, Window):
            return NotImplemented
        return (
            identity_scope(self.backend) is identity_scope(other.backend)
            and self.handle == other.handle
        )

    def __hash__(self) -> int:
        """Hash on the same pair `__eq__` compares, so sets and dicts work.

        A member's scope is set once, when its composite is built, and
        never changes afterwards -- so a Window's hash is stable for its
        life. Windows only exist after composition, since composing is
        what `connect()` does before handing anything back.
        """
        return hash((id(identity_scope(self.backend)), self.handle))

    def __repr__(self) -> str:
        """The title, plus app_id and pid when they are known."""
        bits = [repr(self.title)]
        if self.app_id:
            bits.append(f"app_id={self.app_id!r}")
        if self.pid:
            bits.append(f"pid={self.pid}")
        return f"Window({', '.join(bits)})"


class Element(Protocol):
    """One node of the accessible tree, as every backend agrees to expose it.

    A Protocol rather than a base class: the concrete element belongs to
    whichever backend produced it -- AtspiBackend's is a thin wrapper over a
    dogtail Node and cannot usefully inherit from here -- so this states the
    shape they agree on instead of demanding they share an ancestor.

    It exists to be *named*: Session.element, Session.button and the rest are
    annotated with it, which is what makes `gui.button("Save").click()`
    complete and type-check in an editor. Backends stay free to offer more,
    and AtspiBackend's Element keeps `node` for the parts this does not cover.
    """

    @property
    def name(self) -> str:
        """The element's accessible name, such as a button's label.

        Always a string on every backend, never None. On Windows and macOS
        that is enforced on the way out: an application is free to put a
        non-string there, and coercing it is what stops a `TypeError`
        surfacing from inside somebody's search instead. On macOS what comes
        back is PyObjC's own `str` subclass, so `isinstance(name, str)` holds
        but `type(name) is str` does not -- compare and concatenate freely,
        and do not test the concrete type. An empty name is the
        commonest reason a locator finds nothing -- an icon-only button whose
        developer never set one -- and `description` is where the label went
        in that case, which makes it the second thing a locator should try.
        """
        ...

    @property
    def role(self) -> str:
        """The element's accessible role, in at-spi's spelling everywhere.

        The spelling is at-spi's on every platform, not the native layer's:
        UIA control types and macOS AX roles are both translated on the way
        out, which is what makes one `Role.PUSH_BUTTON` locator match on all
        three. at-spi2's own disagreements are folded in too -- a push button
        reads back the same whether the bridge calls it `"button"` or `"push
        button"`.

        What an *untranslated* role reads as differs, and only one of the two
        is `unknown`: Windows reports `unknown` for a control type outside
        the documented range, while macOS spells an unmapped AX role out into
        words instead, keeping `unknown` for the case where no role could be
        read at all. Either way it is a prompt to run `pyguitest inspect`
        rather than to guess.
        """
        ...

    @property
    def parent(self) -> Element | None:
        """The containing element, or None at the root.

        The direct parent only, and on Windows the parent in UIA's control
        view rather than its raw tree. The element handed back carries the
        same session this one did, so a walk upwards can still click.
        """
        ...

    @property
    def children(self) -> list[Element]:
        """The elements directly inside this one, in the toolkit's order.

        One level only: `find` and `child` are the recursive pair. The order
        is whatever the toolkit publishes rather than anything sorted here,
        so position within this list is a locator of last resort -- it breaks
        the first time someone inserts a widget.
        """
        ...

    @property
    def visible(self) -> bool:
        """Whether the element is currently showing, as far as it can be known.

        The one property whose meaning genuinely differs by platform, because
        no two of these layers answer the same question. at-spi has a
        `showing` state. UIA answers `IsOffscreen`, which counts a row
        scrolled out of a long list as hidden. macOS publishes no such state
        at all, so it is inferred from three things -- the element reports a
        rectangle, it is not marked hidden, and its window is in the window
        server's on-screen list -- and cannot see a scrolling ancestor having
        moved it out of view.

        Lenient on Windows and macOS, where a read the toolkit refuses and a
        join that cannot decide both answer True: a caller about to act on
        the element is better served by the optimistic answer than by a
        confident "it is hidden" that is wrong. Linux has no such default and
        lets the failed read surface instead. Either way this is a hint, and
        the action itself is the better proof.
        """
        ...

    @property
    def enabled(self) -> bool:
        """Whether the element accepts input, rather than being greyed out.

        True where the toolkit will not say, on all three backends -- the same
        leniency `visible` documents. Worth waiting on rather than asserting
        once: a Save button is disabled until the document is modified, which
        is `Session.wait_until(lambda: save.enabled)`, not a single read.
        """
        ...

    @property
    def description(self) -> str:
        """The element's longer accessible description, often a tooltip.

        Always a string, empty where there is none. Where a control publishes
        no `name`, this is usually where its label went, which makes it the
        second thing to try in a locator. On macOS it falls back to the same
        attribute `name` does, so the two can come back identical there.
        """
        ...

    @property
    def text(self) -> str | None:
        """The element's text content, for text boxes and labels, or None.

        None where the element holds no text at all -- and which elements
        those are is a platform answer, not a universal one: an at-spi label
        reports None while a macOS static text answers with its visible
        string, because on a Mac the label's string lives in the one
        attribute that holds text. On Windows the read tries UIA's Text
        pattern first (a document or a read-only body) and then its Value
        pattern (an editable field), which is one property at-spi splits
        differently. `value` is the numeric sibling, not this.
        """
        ...

    @property
    def value(self) -> float | None:
        """The numeric value of a slider, spinner, or progress bar, or None.

        A float where the control publishes a value, None where it does not,
        on every backend. Linux used to answer dogtail's raw `False` for
        anything with no Value interface, which made `value is not None`
        true for a push button there and false for one everywhere else; it
        is coerced now, so the annotation and the behaviour agree.

        One platform difference survives, because it is the toolkit's data
        rather than this package's shape: **macOS reports a number for a
        tick as well as for a position**, since AX keeps both in the same
        attribute. A check box there reads `value == 0.0` alongside
        `checked is False`, where Linux and Windows both answer None.
        Measured on live trees -- `check box`, `radio button`, `separator`,
        `ruler marker` and `dock item` all answered a float on macOS.

        So `isinstance(element.value, float)` still does not mean "this is a
        slider". Branch on the role: `checked` is the read for a tick and
        `text` for an editable field's contents, and `value` is meaningful
        only once the role says the control has a position.
        """
        ...

    @property
    def checked(self) -> bool | None:
        """Whether a check box, radio button, or toggle is set.

        A real boolean everywhere, not only where `checkable` is true --
        read that first to know whether this value means anything.
        """
        ...

    @property
    def checkable(self) -> bool:
        """Whether the element has a check box, radio button, or toggle.

        Read this before trusting `checked`, which reports a plain bool on
        elements that have no such state to report. How it is decided is the
        platform's business -- a Toggle pattern on Windows, an at-spi state,
        the role plus a menu item's mark character on macOS -- but the
        question it answers is the same one everywhere.
        """
        ...

    @property
    def selected(self) -> bool | None:
        """Whether a list item, tab, or menu item is currently selected.

        Same caveat as `checked`: read `selectable` first.
        """
        ...

    @property
    def selectable(self) -> bool:
        """Whether the element can be a list item, tab, or menu selection.

        Read this before trusting `selected`, for `checkable`'s reason. On
        Windows and macOS it is the same signal `select()` acts on, so a
        False here is also the reason that call would refuse.
        """
        ...

    @property
    def expanded(self) -> bool | None:
        """Whether a tree item or similar disclosure control is open.

        Same caveat as `checked`: read `expandable` first.
        """
        ...

    @property
    def expandable(self) -> bool:
        """Whether the element can be expanded or collapsed, like a tree item.

        Read this before trusting `expanded`. On Windows and macOS it is the
        same signal `expand()` and `collapse()` act on. A collapsed branch's
        rows are not in the tree at all until something opens it, which is
        what `Session.tree_items` walks the tree expanding as it goes.
        """
        ...

    @property
    def focused(self) -> bool:
        """Whether the element currently has keyboard focus.

        Not every desktop publishes per-widget focus, and one that does not
        reports False for everything rather than saying so -- which reads as
        a test failure when it is an unsupported desktop. Ask
        `Session.focus_tracking_works()` first on a machine you do not know.
        """
        ...

    @property
    def actions(self) -> list[str]:
        """The names of the actions this element offers, e.g. 'click'.

        The names `do_action` takes. Treat it as a set, not a sequence: it
        comes back sorted on Linux and Windows but in AX's own preference
        order on macOS, so indexing into it means something different per
        platform. Print it before reaching for a toolkit-specific action
        name -- what a widget publishes is the toolkit's choice, and an
        element that publishes none at all is why `click` has a pointer rung.
        """
        ...

    @property
    def pid(self) -> int | None:
        """The process this element belongs to, where the bridge says.

        None where the accessibility bridge publishes none, which is common
        enough that a caller must handle it: it is the one field here that
        the toolkit is not obliged to answer. Its use is telling elements
        of one application apart from another's -- the accessibility bus is
        scoped to the login session, not to a display, so on a machine
        running two of them the tree carries both.
        """
        ...

    @property
    def alive(self) -> bool:
        """Whether the underlying widget still exists.

        Every property above assumes this and raises from wherever it is
        touched otherwise -- an application redraw, a closed dialog, a row
        destroyed by a list that has scrolled past it. Checking first turns
        that crash into a clean answer, the same shape `Session.
        is_window_open` already gives for a stale `Window` handle: an
        `Element` held across time should be asked this before it is used,
        rather than assumed good.

        What this cannot tell you: whether the reference has gone stale in
        the other direction, addressing a *different* live widget that
        happened to reuse the same identity. Nothing in this interface
        promises identity survives a redraw at all -- there is no
        `refresh_element`, unlike `Window`, because nothing here remembers
        the locator that found it in the first place to search again with.
        """
        ...

    def click(self, via: str | None = None) -> None:
        """Act on the element: the accessible action where there is one.

        The toolkit's own action first, named -- Invoke, Toggle, an MSAA
        default action, AT-SPI's `click`. Where the element publishes none at
        all, the click falls back to the pointer at the element's own
        rectangle, which is what a person does with a widget that offers
        nothing; `click_by_pointer` is that step on Windows and macOS, and on
        Linux it is dogtail's own coordinate click, already the last item in
        the ladder there.

        An element with no session behind it -- one taken straight from a
        backend -- has no pointer to reach, and raises `ElementNotActionable`
        rather than doing nothing; `Session.click_element` is the same call
        spelled for that case.

        `via` chooses the route. `"action"` is the accessible action only,
        raising `ElementNotActionable` where there is none; `"pointer"` is
        the user pathway only (extents, move, click), raising rather than
        silently substituting the action; `"auto"` (the default) is today's
        ladder. `None` means "use the session's `click_via`".
        """
        ...

    def double_click(self, via: str | None = None) -> None:
        """Double-click the element: locate it, then inject the gesture.

        Not an accessible action, the way click is. `click` names an action
        the toolkit publishes and lets the toolkit perform it; a
        double-click is a gesture on the pointer, and no toolkit publishes
        one on the bus -- two `click()` calls are two round trips, slower
        than any toolkit's double-click interval, so the pair arrives as two
        single clicks. So the element stays the locator and the gesture
        falls back to the pointer, which needs the pointer capabilities on
        top of Capability.ELEMENT_GEOMETRY.

        An element a Session handed out answers this. One taken straight
        from a backend has no session to reach the pointer through, and says
        so rather than doing nothing; Session.double_click_element is the
        same call spelled for that case.

        `via` may only be `"pointer"` (or `None`/`"auto"`, both of which mean
        pointer here): no toolkit publishes a double-click action, so
        `"action"` is an honest refusal instead of the silent pointer click
        that happens today.
        """
        ...

    def focus(self) -> None:
        """Give the element keyboard focus -- a request, not a guarantee.

        On Windows and macOS a toolkit that will not focus the element says
        so with a typed error naming what was attempted, rather than
        returning quietly. Either way the grant is the toolkit's to make, so
        read `focused` or call `Session.assert_focused` afterwards wherever
        the next step depends on it.
        """
        ...

    def set_text(self, text: str) -> None:
        """Replace the element's text content through the accessible interface.

        Not typing: no key events are sent, so autocomplete, input masks and
        per-keystroke validation are never exercised. That is the right call
        when the value is the point, and the wrong one when the application's
        key handling is what is under test -- `Session.type_text` is that.

        On Windows and macOS a control with no way in -- a read-only label, a
        document, something that merely looks like a field -- raises
        `CapabilityUnsupported`, because "there is no route" is the useful
        half of the answer.
        """
        ...

    def do_action(self, name: str) -> None:
        """Perform a named accessible action, such as "click" or "activate".

        The names are the ones `actions` publishes. Windows and macOS also
        accept at-spi's spellings, so `click`, `press` and `activate` all
        mean the native invoke there and a script written against Linux keeps
        working. Both raise `ValueError` listing what the element does offer
        when the name is unknown, and `ElementNotActionable` when it is a
        real action this element does not publish -- which is what tells a
        typo apart from a widget that cannot. On Linux the name is passed to
        the toolkit as given, so the refusal is the toolkit's own.
        """
        ...

    def select(self) -> None:
        """Select this element, for a list item, tab, or menu entry.

        On Windows and macOS an element that publishes no selection raises
        `CapabilityUnsupported` rather than returning quietly: a caller who
        asked for a selection and got silence cannot tell that apart from a
        selection that happened. `selectable` is the question asked first.
        """
        ...

    def expand(self) -> None:
        """Open a tree item or similar disclosure control.

        A no-op where `expanded` already reads True, since the underlying
        action toggles on at least one platform -- calling this twice must
        not collapse what the first call opened.
        """
        ...

    def collapse(self) -> None:
        """Close a tree item or similar disclosure control. See `expand`.

        A no-op where `expanded` already reads False, for the reason `expand`
        gives. On Windows and macOS an element with nothing to collapse
        raises `CapabilityUnsupported` rather than doing nothing silently.
        """
        ...

    def choose(self, option: str) -> None:
        """Pick `option` from this dropdown by its visible text.

        Through the control's own accessibility interface, which is why it
        keeps working when the list is long enough to scroll. The mechanics
        differ underneath: Windows and macOS open the popup first, because a
        combo box's items often do not exist in the tree until it is open,
        and then look for the item inside the control and beside the whole
        window in turn -- the two kinds of combo box put it in different
        places. When the text does not match, `options()` is what to print.
        """
        ...

    def options(self) -> list[Element]:
        """The choices this dropdown or list offers, as Elements.

        Menu items first, then list items, on every backend. An empty list is
        a truthful answer about a popup nobody has opened yet rather than a
        failure, so this is the diagnostic to reach for when `choose` cannot
        find an option -- it prints what the control actually offers.
        """
        ...

    def find(self, role: str | None = None, name: str | None = None) -> list[Element]:
        """Search this element's whole subtree, not just its children.

        The recursive search, where `children` is one level. An empty list is
        how it reports no match, rather than raising. The elements it hands
        back carry this one's session, so a result can be clicked and
        double-clicked like one the session found itself. For anything beyond
        role and name -- a state, a description, a predicate --
        `Session.elements(within=...)` is the fuller filter.

        Unbounded on Linux and Windows, where the toolkit does the searching.
        macOS has no tree search to ask, so the walk is this package's own and
        carries a budget: 24 levels deep and 20,000 nodes, past which the
        answer is what was found so far rather than an error. Deep enough for
        any ordinary interface, and reachable by a large Electron document.
        """
        ...

    def child(self, role: str | None = None, name: str | None = None) -> Element | None:
        """Return the first descendant matching role and/or name, or None.

        `find`'s first match, with the same recursive reach, and None rather
        than an exception where nothing matches -- the opposite convention
        from `Session.element`, which raises. Useful for a local lookup
        inside an element already in hand.
        """
        ...

    def is_ancestor_of(self, other: Element) -> bool:
        """Whether `other` sits somewhere inside this element.

        Strictly inside: an element is not its own ancestor. Answered by
        walking upwards from `other` rather than descending, since the
        question is about one path and descending would visit the whole tree.

        How the comparison at each step is made is the platform's: Windows
        asks `CompareElements` and macOS asks CoreFoundation, because two
        references to one element are distinct objects there as often as not,
        while on Linux PyGObject hands back the same wrapper for the same
        accessible and `==` is already the right question. The walk is
        depth-bounded on Windows and macOS, so a toolkit reporting a cycle
        cannot make it spin; on Linux it is not, so a cyclic tree would.

        Its use is confirming that a control really belongs to the dialog a
        test thinks it scoped the search to.
        """
        ...


_SENDKEYS_PLAIN = {
    "-": "minus",
    "=": "equal",
    "[": "bracketleft",
    "]": "bracketright",
    "\\": "backslash",
    ";": "semicolon",
    "'": "apostrophe",
    ",": "comma",
    ".": "period",
    "/": "slash",
    "`": "grave",
    " ": "space",
    "\n": "Return",
    "\t": "Tab",
}
"""char -> X11 keysym name, unshifted. Values for GUIBackend.resolve_char_key."""

_SENDKEYS_SHIFTED = {
    "!": "1",
    "@": "2",
    "#": "3",
    "$": "4",
    "%": "5",
    "^": "6",
    "&": "7",
    "*": "8",
    "(": "9",
    ")": "0",
    "_": "minus",
    "+": "equal",
    "{": "bracketleft",
    "}": "bracketright",
    "|": "backslash",
    ":": "semicolon",
    '"': "apostrophe",
    "<": "comma",
    ">": "period",
    "?": "slash",
    "~": "grave",
}
"""char -> X11 keysym name of its unshifted base key. The other half of
resolve_char_key's default table."""


class GUIBackend(ABC):
    """One way of driving a desktop.

    Subclasses override `capabilities` and the methods they can honour. The
    base implementations raise, so nothing silently no-ops -- the failure mode
    the audit singled out in the Perl module, where every error was a zero.
    """

    name = "abstract"

    MODIFIER_KEYS = {
        "^": "Control_L",
        "%": "Alt_L",
        "+": "Shift_L",
        "#": "Meta_L",
        "&": "ISO_Level3_Shift",
    }
    """send_keys()'s modifier characters, mapped to the name press_key/
    release_key expect for that key on this backend. X11 keysym names by
    default -- correct for X11Backend as-is, and for the xdotool/wdotool/wtype
    tool adapters, which speak the same vocabulary. A backend with a different
    key-naming convention (uinput's evdev names) overrides this."""

    KEY_ALIASES = {
        "BAC": "BackSpace",
        "BS": "BackSpace",
        "BKS": "BackSpace",
        "BRE": "Break",
        "CAN": "Cancel",
        "CAP": "Caps_Lock",
        "DEL": "Delete",
        "DOWN": "Down",
        "END": "End",
        "ENT": "Return",
        "ESC": "Escape",
        "HEL": "Help",
        "HOM": "Home",
        "INS": "Insert",
        "LEF": "Left",
        "NUM": "Num_Lock",
        "PGD": "Next",
        "PGU": "Prior",
        "PRT": "Print",
        "RIG": "Right",
        "SCR": "Scroll_Lock",
        "TAB": "Tab",
        "UP": "Up",
        "F1": "F1",
        "F2": "F2",
        "F3": "F3",
        "F4": "F4",
        "F5": "F5",
        "F6": "F6",
        "F7": "F7",
        "F8": "F8",
        "F9": "F9",
        "F10": "F10",
        "F11": "F11",
        "F12": "F12",
        "SPC": "space",
        "SPA": "space",
        "LSK": "Super_L",
        "RSK": "Super_R",
        "MNU": "Menu",
        "LSH": "Shift_L",
        "RSH": "Shift_R",
        "LCT": "Control_L",
        "RCT": "Control_R",
        "LAL": "Alt_L",
        "RAL": "Alt_R",
        "LMA": "Meta_L",
        "RMA": "Meta_R",
    }
    """send_keys()'s `{BAC}`-style abbreviations, looked up case-insensitively
    and mapped the same way as MODIFIER_KEYS. Ported from X11::GUITest's own
    table. A name absent here is passed to press_key unabbreviated, e.g.
    `{BackSpace}` in place of `{bac}`."""

    def resolve_char_key(self, char: str) -> tuple[str, bool]:
        """The (key name, needs_shift) that presses one character's own key.

        The name goes to press_key/release_key as-is. Used by send_keys() for
        the characters in a modifier group or a `{}`
        brace set, where the *physical* key matters and an already-held
        modifier must combine with it -- unlike type_text, which decides its
        own shift state per character and would fight a modifier the caller
        is already holding.

        A static US-layout ASCII table, matching X11::GUITest's own SendKeys,
        which was ASCII-only. Raises ValueError outside that range; use
        type_text for arbitrary or non-ASCII text instead.
        """
        if len(char) == 1 and char.isascii() and char.isalpha():
            return char.lower(), char.isupper()
        if len(char) == 1 and char.isascii() and char.isdigit():
            return char, False
        if char in _SENDKEYS_PLAIN:
            return _SENDKEYS_PLAIN[char], False
        if char in _SENDKEYS_SHIFTED:
            return _SENDKEYS_SHIFTED[char], True
        raise ValueError(
            f"{char!r} has no static key mapping on {self.name}; "
            "use type_text for arbitrary text"
        )

    @property
    @abstractmethod
    def capabilities(self) -> CapabilitySet:
        """A CapabilitySet describing what this backend can actually do."""

    def supports(self, capability: Capability) -> bool:
        """Whether this backend provides `capability`."""
        return capability in self.capabilities

    def require(self, capability: Capability, reason: str | None = None) -> None:
        """Raise unless `capability` is supported. Call at the top of methods."""
        if capability not in self.capabilities:
            raise CapabilityUnsupported(capability, self.name, reason)

    def close(self) -> None:
        """Release compositor connections, portal sessions, virtual devices."""

    def __enter__(self: _Backend) -> _Backend:
        """Return self; there is nothing to open."""
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        """Close on the way out, and never swallow the exception."""
        self.close()
        return False

    def __repr__(self) -> str:
        """The backend's name and how many capabilities it provides."""
        return f"<{type(self).__name__} {self.name!r} caps={len(self.capabilities)}>"

    # -- screens (T2) ------------------------------------------------------

    def screens(self) -> list[Screen]:
        """Every output, in advertised order."""
        self.require(Capability.SCREEN_INFO)
        raise NotImplementedError

    # -- input (T4) --------------------------------------------------------

    def move_mouse(self, x: int, y: int, screen: int = 0) -> None:
        """Move the pointer to an absolute position."""
        self.require(Capability.POINTER_MOVE)
        raise NotImplementedError

    def press_button(self, button: int) -> None:
        """Press a mouse button. 1 is left, 2 middle, 3 right."""
        self.require(Capability.POINTER_BUTTON)
        raise NotImplementedError

    def release_button(self, button: int) -> None:
        """Release a mouse button."""
        self.require(Capability.POINTER_BUTTON)
        raise NotImplementedError

    def scroll(self, dx: int = 0, dy: int = 0) -> None:
        """Scroll by whole wheel detents: `dy` positive is up, `dx` right.

        Both halves of that are a decision, because the mechanisms
        underneath disagree with each other:

        **Units are detents**, one notch of a physical wheel, never
        pixels. X11 and xdotool click buttons 4-7 `abs(steps)` times,
        uinput emits `REL_WHEEL`, the portal sends
        `NotifyPointerAxisDiscrete` -- all detents already. libei is the
        odd one out: it offers `scroll_delta` in logical pixels as well,
        and `LibeiBackend` deliberately uses `scroll_discrete` (120 units
        to the detent) so that `scroll(0, 3)` means the same thing on
        every backend rather than three notches on five of them and three
        pixels on the sixth.

        **`dy > 0` scrolls up**, following X11's buttons 4/5 rather than
        Wayland's axis events, which take positive as *down*. The X11
        reading won on numbers -- four of six backends already did it, and
        this package is the successor to an X11 module -- so `portal` and
        `eiinput` negate `dy` on the way out. Horizontal needs no such
        fix: positive is right in both conventions.
        """
        self.require(Capability.POINTER_SCROLL)
        raise NotImplementedError

    def press_key(self, key: str) -> None:
        """Press a key by name, without releasing it."""
        self.require(Capability.KEY_EVENT)
        raise NotImplementedError

    def release_key(self, key: str) -> None:
        """Release a key by name."""
        self.require(Capability.KEY_EVENT)
        raise NotImplementedError

    def type_text(
        self, text: str, delay: float = 0.0, allow_keymap_unsafe: bool = True
    ) -> None:
        """Type `text`, pausing `delay` seconds between characters.

        Separate from press_key because correctness depends on the keymap: a
        backend injecting raw scancodes types the wrong characters on a
        non-US layout, and no protocol reports which layout is active.

        `allow_keymap_unsafe` is part of the common signature even though
        only the scancode-injecting backends (uinput, ydotool) act on it --
        a keymap-safe backend accepts and ignores it rather than raising
        TypeError, so a caller does not need to know which backend is active
        to pass the setting a keymap-unsafe suite needs.
        """
        self.require(Capability.TEXT_ENTRY)
        raise NotImplementedError

    def sync(self, timeout: float = 1.0) -> bool:
        """Block until the compositor has consumed the events sent so far.

        The honest alternative to sleeping after injection. A backend that
        can offer this has a round trip to the compositor -- libei's
        ping/pong -- so "has my input arrived" stops being a guess.

        What it proves is bounded, and the bound matters: the *compositor*
        has taken the events. It says nothing about whether the
        application under the pointer processed them, laid out, or
        repainted. `Session.wait_until` on an element's own state is what
        answers that; this removes one source of flakiness underneath it,
        not all of them.

        Returns True once confirmed, False if `timeout` elapses first --
        never raising on timeout, matching wait_for_window and the rest of
        the wait family.
        """
        self.require(Capability.INPUT_SYNC)
        raise NotImplementedError

    def wait_for_pointer_activation(
        self, timeout: float | None
    ) -> tuple[float, float] | None:
        """Block for one real crossing of a screen edge.

        Not a query -- see `Session.wait_for_pointer_activation` for what
        this actually promises, and why a successful answer means the
        user's real input was exclusively diverted for as long as the
        compositor held it.
        """
        self.require(Capability.INPUT_CAPTURE)
        raise NotImplementedError

    # -- windows (T3) ------------------------------------------------------

    def windows(self) -> list[Window]:
        """Every toplevel currently known."""
        self.require(Capability.WINDOW_LIST)
        raise NotImplementedError

    def active_window(self) -> Window | None:
        """The currently focused window, or None."""
        self.require(Capability.WINDOW_STATE)
        raise NotImplementedError

    def window_events(self, timeout: float | None = None) -> Iterator[WindowEvent]:
        """Yield WindowEvent objects as the compositor reports them.

        `timeout` bounds the total wait in seconds; None yields indefinitely.
        The one real event feed backends dispatch through -- see
        Session.wait_for_window and Session.wait_window_close, which are
        what most callers want instead of consuming this directly.
        """
        self.require(Capability.WINDOW_EVENTS)
        raise NotImplementedError

    def wait_for_window(
        self, title: str | re.Pattern[str], timeout: float | None = None
    ) -> Window | None:
        """Block until a window matching `title` (regex) appears, or None.

        `title` is regex source or an already-compiled pattern; an
        implementation passes it through `re.compile`, which returns a
        compiled one unchanged, flags and all.

        Session.wait_for_window is the capability-agnostic entry point --
        it calls this only where Capability.WINDOW_EVENTS is available
        (real notification), and polls windows() itself everywhere else.
        """
        self.require(Capability.WINDOW_EVENTS)
        raise NotImplementedError

    def _normalize_window(self, window: Window | None) -> Window | None:
        """Adjust `window` for cross-backend handle consistency, or pass it through.

        Identity here: a single backend's own windows are always safe to act
        on through itself. `CompositeBackend` overrides this -- see there for
        why a window an event feed just issued can need re-homing before a
        caller's next call (geometry, move_window, ...) touches it.
        """
        return window

    def is_window_viewable(self, window: Window) -> bool:
        """Whether `window` is currently mapped and showing.

        Replaces X11::GUITest's WaitWindowViewable -- a one-shot state read
        here rather than a wait, since it shares WINDOW_STATE's tier and
        polling it in a loop is exactly what a caller already gets for free
        from wait_for_window/wait_window_close.
        """
        self.require(Capability.WINDOW_STATE)
        raise NotImplementedError

    def window_at(self, x: int, y: int, screen: int = 0) -> Window | None:
        """The topmost window covering a screen coordinate, or None."""
        self.require(Capability.WINDOW_AT_POINT)
        raise NotImplementedError

    def geometry(self, window: Window) -> tuple[int, int, int, int]:
        """(x, y, width, height). The most commonly missing capability."""
        self.require(Capability.WINDOW_GEOMETRY)
        raise NotImplementedError

    def move_window(self, window: Window, x: int, y: int) -> None:
        """Move a window's top-left corner to (x, y)."""
        self.require(Capability.WINDOW_PLACEMENT)
        raise NotImplementedError

    def resize_window(self, window: Window, width: int, height: int) -> None:
        """Resize a window to `width` by `height`."""
        self.require(Capability.WINDOW_RESIZE)
        raise NotImplementedError

    def activate_window(self, window: Window) -> None:
        """Raise and focus. There is no raise-without-focus operation."""
        self.require(Capability.WINDOW_ACTIVATE)
        raise NotImplementedError

    def minimize_window(self, window: Window, minimized: bool = True) -> None:
        """Minimize a window, or restore it when `minimized` is False."""
        self.require(Capability.WINDOW_MINIMIZE)
        raise NotImplementedError

    def capture(
        self,
        window: Window | None = None,
        path: str | None = None,
        region: Sequence[float] | None = None,
    ) -> str:
        """Screenshot the whole desktop, one window, or one rectangle.

        Writes a PNG and returns its path; `path` names the file, and one
        is allocated in the temporary directory when it is omitted. No
        image library is involved -- callers wanting pixels hand the path
        to Pillow.

        `region` is a normalized `(x, y, width, height)` in *screen*
        coordinates, the same tuple `geometry()` returns, so a rectangle
        read from one backend can be handed straight to another. Backends
        translate it into whatever their own mechanism wants; no caller
        ever writes grim's "x,y WxH" or ImageMagick's "WxH+X+Y" by hand.

        `window` and `region` are mutually exclusive: `region` is already
        screen-absolute, so pairing the two would mean either interpreting
        it relative to the window (a second, silently different coordinate
        space) or ignoring one of the two arguments. Both are worse than
        refusing.

        The two arguments need different capabilities, and only the one
        actually being used is required. A backend can serve
        WINDOW_CAPTURE without SCREEN_CAPTURE -- X11 under XWayland is
        exactly that, since an X11 client's own drawable is readable there
        while the root is not -- and demanding both would refuse a
        per-window capture for lacking a capability it never needed.
        """
        self.require(
            Capability.WINDOW_CAPTURE
            if window is not None
            else Capability.SCREEN_CAPTURE
        )
        raise NotImplementedError

    def desktop_region(self) -> tuple[int, int, int, int] | None:
        """The screen rectangle a whole-desktop `capture()` covers, or None.

        `(x, y, width, height)` in the coordinates `geometry()` uses. What
        `Session.locate_image` maps a match back through: image pixel (0, 0)
        is this rectangle's corner, and the image's own size against its
        width and height is the capture's scale. None -- the default, and the
        answer wherever a capture starts at screen (0, 0) at one pixel per
        unit -- keeps that identity reading. Windows answers its virtual
        desktop, whose origin is negative with a monitor left of or above the
        primary, and macOS the main display in points, which a Retina capture
        covers at two pixels each.
        """
        return None

    # -- tier 6: no path anywhere but X11 ----------------------------------
    #
    # Not a degraded path on Wayland, an absent one -- see Tier.NO_PATH.
    # X11Backend is the only override any of these have ever had.

    def pointer_position(self) -> tuple[int, int]:
        """The global pointer position. Replaces GetMousePos."""
        self.require(Capability.POINTER_QUERY)
        raise NotImplementedError

    def is_button_pressed(self, button: int) -> bool:
        """Whether a mouse button is currently held down."""
        self.require(Capability.INPUT_STATE_QUERY)
        raise NotImplementedError

    def is_key_pressed(self, key: str) -> bool:
        """Whether a key is currently held down. Replaces IsKeyPressed."""
        self.require(Capability.INPUT_STATE_QUERY)
        raise NotImplementedError

    def set_window_title(self, window: Window, title: str) -> None:
        """Replaces SetWindowName."""
        self.require(Capability.WINDOW_TITLE_SET)
        raise NotImplementedError

    def lower_window(self, window: Window) -> None:
        """Replaces LowerWindow."""
        self.require(Capability.WINDOW_LOWER)
        raise NotImplementedError

    def is_window_cursor(self, window: Window, shape: int) -> bool:
        """Whether `window` is currently showing cursor `shape`.

        Under X11 this is a per-window question with a per-window answer. On
        Windows it is scoped to the *pointer* instead: what is readable is the
        cursor the system is showing, compared against the system's own
        standard handles, so the answer concerns `window` only while the
        pointer is inside it — and a themed desktop's cursor may match no
        standard handle at all, which reads as False. WINDOW_CURSOR_QUERY's
        description carries the same caveat.
        """
        self.require(Capability.WINDOW_CURSOR_QUERY)
        raise NotImplementedError

    # -- clipboard (T3) ----------------------------------------------------

    def get_clipboard(self, primary: bool = False) -> str:
        """The current text content of the clipboard, or of PRIMARY."""
        self.require(Capability.CLIPBOARD)
        raise NotImplementedError

    def set_clipboard(self, text: str, primary: bool = False) -> None:
        """Replace the text content of the clipboard, or of PRIMARY."""
        self.require(Capability.CLIPBOARD)
        raise NotImplementedError

    # -- images (T1) -------------------------------------------------------

    def locate(
        self,
        haystack: str,
        template: str,
        region: Sequence[float] | None = None,
        metric: str = "RMSE",
        threshold: float | None = None,
    ) -> ImageMatch | None:
        """Find `template`'s position within `haystack`.

        Returns None if no match clears `threshold`. Both are paths to
        already-captured image files; this needs no live display connection,
        only pixels already on disk. `region` restricts the search to
        (x, y, width, height) within `haystack`, in that image's own pixel
        coordinates. `metric` and `threshold` are forwarded to the concrete
        backend's own similarity measure.
        """
        self.require(Capability.IMAGE_LOCATE)
        raise NotImplementedError

    # -- elements (T5) -----------------------------------------------------

    def root_element(self) -> Element:
        """The accessible-tree root. The replacement for the X11 window tree."""
        self.require(Capability.ELEMENT_TREE)
        raise NotImplementedError

    def find_elements(
        self,
        role: str | Sequence[str] | None = None,
        name: str | re.Pattern | None = None,
        within: Element | None = None,
        enabled: bool | None = None,
        visible: bool | None = None,
        description: str | re.Pattern | None = None,
        predicate: Callable[[Element], bool] | None = None,
    ) -> list[Element]:
        """Search the accessible tree.

        The preferred automation entry point: unaffected by the missing
        geometry and input capabilities, and identical under X11 and Wayland.
        `name`/`description` take a plain string (exact match) or a compiled
        regex (`.search()`); `predicate` is an escape hatch for anything else.

        `role` may name several roles at once -- `role=("push button",
        "check box")` -- matching any of them in one search. This is not
        just convenience: every backend's role filter funnels through
        `roles.spellings()`, and a search is one walk of the subtree in
        scope regardless of how many roles the predicate built from `role`
        accepts, so asking for N roles in one call costs one walk, where N
        separate calls cost N walks of the same subtree. Confirmed live on
        AT-SPI (GhostBSD): one combined search over a ~130-element window
        took the same few seconds as a single-role one; doing it role by
        role for 16 roles turned that into over a minute.
        """
        self.require(Capability.ELEMENT_TREE)
        raise NotImplementedError

    def find_element(
        self,
        role: str | Sequence[str] | None = None,
        name: str | re.Pattern | None = None,
        within: Element | None = None,
        enabled: bool | None = None,
        visible: bool | None = None,
        description: str | re.Pattern | None = None,
        predicate: Callable[[Element], bool] | None = None,
    ) -> Element | None:
        """The first accessible element matching, or None.

        See `find_elements` for what `role` and the other filters accept.
        """
        self.require(Capability.ELEMENT_TREE)
        raise NotImplementedError

    def extents(self, element: Element) -> tuple[int, int, int, int] | None:
        """`element`'s (x, y, width, height) in screen coordinates.

        None where the element occupies no screen space at all -- it
        publishes no Component interface, or reports an empty rectangle
        because it is not currently showing. That is an ordinary answer
        about an ordinary element, not a missing capability, which is why
        it is None rather than a raise.
        """
        self.require(Capability.ELEMENT_GEOMETRY)
        raise NotImplementedError

    def element_at(self, x: int, y: int) -> Element | None:
        """The deepest accessible element at a screen coordinate, or None.

        The element counterpart of window_at, and takes no `screen`
        argument on purpose: the accessible tree is addressed in one
        desktop-wide coordinate space, with no per-output index to pass.
        """
        self.require(Capability.ELEMENT_GEOMETRY)
        raise NotImplementedError
