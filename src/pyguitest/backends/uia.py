"""Windows elements: the UIA control types, and UI Automation itself.

Two layers in one module, because they are one subject. The first is a
**table**: every UI Automation control type, mapped to the role name this
package reports for it. Data -- written from the documentation, checkable on
any machine, and the part that says why a Windows role vocabulary is not
at-spi's. The second is `UiaBackend`: element search, element actions, element
geometry and hit-testing, which is the one half of Windows support that needs
COM, and therefore the half behind the `windows` extra.

`atspi.py` is the same backend on Linux, and the thing to read this against:
`Element` here wraps an `IUIAutomationElement` where that one wraps a dogtail
Node, `_build_predicate` answers the same question the same way, `extents` and
`element_at` make the same decisions about screen coordinates, and
`Element.double_click` delegates to the session for the same reason. The
differences are of exactly two kinds -- UIA's vocabulary (control types and
control patterns, where at-spi has roles and actions) and COM's failure mode
(a property read that answers an HRESULT rather than raising) -- and where
either shows through, this module says so.

**This has reached a real UI Automation provider, but not a real desktop.**
On Windows 11 build 26200, `available()`, `_connection()`, `GetRootElement()`,
`CreatePropertyCondition` and `ControlViewCondition` all answer, and
`ElementFromPoint` accepts the point `_point` builds. That run was over SSH,
which is not on an interactive window station, so the desktop element has no
children: every search executes and correctly returns nothing. What no search
has yet done is find a real control, act on one, or read one's text --
docs/validation.md keeps that list.

That run settled one of the two hedges and found one bug:

- Every `UIA_*` id is named as the documentation names it, so a reader
  checking one against the SDK has nothing to translate. Still a hedge, and
  still worth it: a wrong id narrows a search too little rather than failing,
  so the client-side predicate is what keeps the answer right.
- Every `Current*` property is read two ways -- as an attribute and as
  `get_Current*()`. **Measured: comtypes binds the attribute form, and
  `get_CurrentName` does not exist on the generated class.** The method branch
  in `_current` is therefore dead code against this comtypes, and is kept only
  because nothing documents the binding as stable. See `_current`.
- **A NULL COM pointer is not `None`**, which several "or None where there is
  none" contracts here were written as though it were. `_null_to_none` is the
  fix and carries the detail; it is the one bug a fake COM client structurally
  could not have exposed.

`_connection` is the seam the tests replace, and unlike `_winapi`'s DLL
accessors it hands back an *object*: COM's vtables come from the type library
at runtime, so there are no signatures here to declare, and a client module
without an `IUIAutomation` is not a state worth being able to construct.

What this backend deliberately does **not** serve:

- **`windows()` and every `WINDOW_*` capability.** `win32` owns that family.
  The objects UIA could hand back are elements rather than window handles, so
  every placement or state call granted here would have nothing to point at.
- **`WINDOW_EVENTS`.** UIA has a real event model -- handlers on the client,
  fed by a message pump this backend does not own -- and it is a later phase,
  the same answer `win32` gives for its own.
- **`sync()`.** No injection happens here at all, so `INPUT_SYNC` is not
  claimed; `Win32Backend` explains why it withholds it on the half that does.

The table below is a translation, and a translation is where the interesting
part lives -- a role string from a Windows session is *this* package's at-spi
spelling, chosen here, not a name the platform uses:

- **UIA has no push button versus toggle button.** There is one control type
  for both, and the difference is a control *pattern*: an element that also
  answers `TogglePattern` is what at-spi would call `toggle button`. Both
  come back as `push button` here, and a caller that has to tell them apart
  asks the pattern rather than the role.
- **`Text` covers what at-spi splits in two.** `label` (static text) and
  `text` (a document or an editable body) are one control type in UIA, so a
  Windows label is found by `role="text"` and `Role.LABEL` matches nothing
  on that platform.
- **`Pane` is at-spi's `panel` only most of the time.** A UIA pane is also
  what at-spi would call a `scroll pane`, a `viewport` or a `filler`
  depending on what it contains; there is no scroll-pane control type,
  because scrolling is a pattern here rather than a kind of widget.
- **The menu-item family collapses too.** at-spi's `check menu item` and
  `radio menu item` are one UIA control type, for the same reason as the
  button split: the checked state is a pattern, not a type.

Five of the values below are real at-spi role names that `roles.py` has no
constant for -- `calendar`, `menu bar` and `tool tip` among them. They are
spelled here rather than added to `Role`, because `Role` is the vocabulary a
caller types and these five are produced by this one table. Three others are
control types at-spi has no role for at all; the two sets below say which is
which, and a test checks that they are the only values not in `Role`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ..capabilities import Capability, CapabilitySet
from ..errors import (
    BackendUnavailable,
    CapabilityUnsupported,
    ElementNotActionable,
    ElementNotFound,
    PyGUITestError,
)
from ..roles import Role, spellings
from . import _winapi
from .base import GUIBackend

if TYPE_CHECKING:
    # Imported for annotations only: Session.element and the widget finders are
    # annotated with base.Element, and this is what keeps that annotation true.
    from .base import Element as _ElementInterface

__all__ = ["UiaBackend", "Element", "available"]

_UIA_ROLES = {
    50000: Role.PUSH_BUTTON,
    # at-spi has a calendar role; this package has no constant for it.
    50001: "calendar",
    50002: Role.CHECK_BOX,
    50003: Role.COMBO_BOX,
    # A UIA Edit is an editable field, so `entry` rather than `text`: at-spi
    # distinguishes them by whether the text can be typed into.
    50004: Role.ENTRY,
    50005: Role.LINK,
    50006: Role.IMAGE,
    50007: Role.LIST_ITEM,
    50008: Role.LIST,
    50009: Role.MENU,
    50010: "menu bar",
    50011: Role.MENU_ITEM,
    50012: Role.PROGRESS_BAR,
    50013: Role.RADIO_BUTTON,
    50014: Role.SCROLL_BAR,
    50015: Role.SLIDER,
    50016: Role.SPIN_BUTTON,
    50017: Role.STATUS_BAR,
    50018: Role.PAGE_TAB_LIST,
    50019: Role.PAGE_TAB,
    50020: Role.TEXT,
    50021: Role.TOOL_BAR,
    50022: "tool tip",
    50023: Role.TREE,
    50024: Role.TREE_ITEM,
    50025: "custom",
    50026: Role.PANEL,
    # The draggable part of a scroll bar. at-spi reports the scroll bar and
    # its value and never the thumb as an element of its own, so this has no
    # counterpart role and keeps its own UIA name -- mapping it onto
    # Role.SCROLL_BAR would make find_elements(role=SCROLL_BAR) return the bar
    # *and* its thumb here and only the bar on Linux.
    50027: "thumb",
    # A grid that supports the Grid pattern; at-spi's `table` covers both.
    50028: Role.TABLE,
    # One per row in practice, which is at-spi's `table row` -- at-spi
    # reports cells and rows separately, UIA reports the row.
    50029: Role.TABLE_ROW,
    50030: Role.DOCUMENT_FRAME,
    50031: Role.PUSH_BUTTON,
    50032: Role.WINDOW,
    50033: Role.PANEL,
    # A container for column headers, not a text heading: `Role.HEADING` is
    # a different thing and is not what a UIA Header maps to.
    50034: "header",
    50035: "table column header",
    50036: Role.TABLE,
    50037: "title bar",
    50038: Role.SEPARATOR,
    50039: "semantic zoom",
    50040: Role.TOOL_BAR,
}
"""UIA control type id -> the role string this package reports for it.

Every id from 50000 to 50040, with no gaps: the range is contiguous in the
UIA documentation, and a test asserts that rather than trusting it.
"""

_UNKNOWN_ROLE = "unknown"
"""The role for a control type this table does not know.

`ATSPI_ROLE_UNKNOWN` is a real at-spi role and this is its name, so unlike the
five above it is neither a name without a constant nor a name at-spi lacks --
it is what at-spi itself says about a widget whose role it cannot tell. The
range above is total over the documented ids, so this can only be reached by a
provider reporting something outside it, which is a genuine "the provider did
not answer" rather than a mismatch to be papered over.
"""

_ATSPI_NAMES_WITHOUT_CONSTANTS = frozenset(
    {"calendar", "header", "menu bar", "table column header", "tool tip"}
)
"""Values that are real at-spi role names with no `Role` constant beside them.

Five of the fifty-odd names at-spi can report have no constant in `roles.py`,
because until now nothing in this package could produce them.
"""

_NO_ATSPI_COUNTERPART = frozenset({"custom", "semantic zoom", "thumb", "title bar"})
"""Values at-spi has no role for at all.

Four control types describe something the at-spi vocabulary does not name: a
provider that implements no standard control type, the Windows 8 start
screen's zoom-out control, a scroll bar's draggable thumb, and a window's
title bar. The UIA name, lower-cased into the style of the others, is the
honest answer; mapping them onto a real role name would claim a match that
will not happen, and `unknown` would claim the provider never answered -- see
`_UNKNOWN_ROLE`, which is what that name is kept for.

The thumb is the one where the wrong answer is not merely unhelpful but
divergent: it is a *child* of the scroll bar it belongs to, so calling it a
scroll bar would make `find_elements(role=SCROLL_BAR)` report two elements
per scroll bar on Windows and one on Linux.
"""

# -- the UIA vocabulary ----------------------------------------------------
#
# Named as Microsoft's documentation names it, for the same reason `_winapi`'s
# constants are: a reader checking one against uiautomationclient.h should have
# nothing to translate first. Only the ids this backend uses are here -- the
# module has no opinion about the eighty it does not.

UIA_BoundingRectanglePropertyId = 30001
"""The element's screen rectangle, as left/top/right/bottom."""

UIA_ProcessIdPropertyId = 30002
"""The process hosting the element."""

UIA_ControlTypePropertyId = 30003
"""The element's control type: one of the ids in the table above."""

UIA_NamePropertyId = 30005
"""The element's accessible name -- a button's label, a field's caption."""

UIA_IsEnabledPropertyId = 30010
"""Whether the element accepts input, rather than being greyed out."""

UIA_HelpTextPropertyId = 30013
"""The element's help text: at-spi's description, and often a tooltip."""

UIA_IsOffscreenPropertyId = 30022
"""Whether the element is scrolled out of view. The inverse of `visible`."""

UIA_InvokePatternId = 10000
"""The pattern a button publishes: `Invoke` is its "I have been pressed"."""

UIA_ValuePatternId = 10002
"""The pattern an editable field publishes: `SetValue` and `CurrentValue`."""

UIA_RangeValuePatternId = 10003
"""The pattern a slider, spinner or progress bar publishes: a number."""

UIA_ExpandCollapsePatternId = 10005
"""The pattern a dropdown or a tree item publishes."""

UIA_SelectionItemPatternId = 10010
"""The pattern a list item, tab or menu entry publishes: `Select`."""

UIA_TextPatternId = 10014
"""The pattern a document or a read-only text body publishes."""

UIA_TogglePatternId = 10015
"""The pattern a check box or a switch publishes: `Toggle`, plus its state."""

UIA_ScrollItemPatternId = 10017
"""The pattern that scrolls an element into view."""

UIA_LegacyIAccessiblePatternId = 10018
"""The MSAA compatibility pattern, whose default action is often a click."""

TreeScope_Children = 2
"""`FindAll`: the element's immediate children."""

TreeScope_Descendants = 4
"""`FindAll`: everything below the element, the element itself excluded."""

ToggleState_Off = 0
ToggleState_On = 1
ToggleState_Indeterminate = 2
"""`TogglePattern`'s three answers, in the order the documentation lists them."""

_PATTERN_INTERFACES = {
    "invoke": (UIA_InvokePatternId, "IUIAutomationInvokePattern"),
    "value": (UIA_ValuePatternId, "IUIAutomationValuePattern"),
    "range value": (UIA_RangeValuePatternId, "IUIAutomationRangeValuePattern"),
    "expand collapse": (
        UIA_ExpandCollapsePatternId,
        "IUIAutomationExpandCollapsePattern",
    ),
    "selection item": (
        UIA_SelectionItemPatternId,
        "IUIAutomationSelectionItemPattern",
    ),
    "text": (UIA_TextPatternId, "IUIAutomationTextPattern"),
    "toggle": (UIA_TogglePatternId, "IUIAutomationTogglePattern"),
    "scroll item": (UIA_ScrollItemPatternId, "IUIAutomationScrollItemPattern"),
    "legacy": (UIA_LegacyIAccessiblePatternId, "IUIAutomationLegacyIAccessiblePattern"),
}
"""Pattern key -> its id, and the interface name its methods live on.

The pattern is fetched by id and then asked for that interface, which is the
documented two-step. `GetCurrentPatternAs(riid)` would do both in one call and
is not usable from here: its `riid` is a pointer to a GUID that comtypes binds
as an out-parameter a Python caller has no natural way to pass.
"""

_ACTIONS = {
    "invoke": ("invoke", "Invoke"),
    "toggle": ("toggle", "Toggle"),
    "select": ("selection item", "Select"),
    "expand": ("expand collapse", "Expand"),
    "collapse": ("expand collapse", "Collapse"),
    "scroll into view": ("scroll item", "ScrollIntoView"),
    "do default action": ("legacy", "DoDefaultAction"),
}
"""Action name -> (pattern key, the method on that pattern that performs it).

These are the actions `Element.actions` reports and `Element.do_action`
performs, and they are UIA's names for UIA's mechanisms rather than at-spi's
for at-spi's: a Windows session has no Action interface to be asked what a
widget offers, so a pattern either exists or it does not. `Element.do_action`
also accepts at-spi's spelling of the first one -- a script written against
the Linux backend says `click`, and UIA's Invoke pattern is what that means.
"""

_ACTION_ALIASES = {
    "click": "invoke",
    "press": "invoke",
    "activate": "invoke",
}
"""at-spi action spellings -> the UIA action that means the same thing."""

_MAX_DEPTH = 24
"""Descent limit for `element_at`, so a cyclic tree cannot spin forever.

The same figure and the same reason as `atspi._MAX_DEPTH`: deep enough for any
real widget hierarchy, and a tree that has not bottomed out by here is
malformed -- the element reached is still a truthful answer, just not the
deepest one.
"""

_ABSENT = object()
"""A default no COM property can return. See `Element.alive`."""


def _null_to_none(pointer):
    """A COM interface pointer, with a NULL one turned into `None`.

    **A NULL COM pointer is not `None`.** It is an ordinary Python object
    wrapping address zero, so `if result is None` never fires for it, and
    every "or None where there is none" contract written that way was false.
    ctypes does define truthiness on a pointer as "not NULL", which is what
    this leans on, and the falsy case covers a real `None` as well -- so one
    guard answers both.

    Found live rather than by reading: `_parent` at the tree root came back as
    a null `IUIAutomationElement`, and `Element.parent` duly wrapped it, so a
    caller walking upwards got `Element('unknown', '')` where they were
    promised None. A fake COM client cannot reproduce that -- a Python
    stand-in returns None because that is what Python returns -- which is why
    this is the kind of bug the first real run exists to find.
    """
    return pointer if pointer else None


# -- the COM seam ----------------------------------------------------------

_UIAUTOMATION_CORE = "UIAutomationCore.dll"
"""The type library every interface and structure below comes from."""

_CLSID_CUIAUTOMATION = "{ff48dba4-60ef-4201-aa87-54103eef594e}"
"""`CUIAutomation`'s documented CLSID: the class that serves `IUIAutomation`."""

_CLIENT_MODULE: Any = None
"""The generated `UIAutomationClient` module, once loaded -- never at import.

A module-level cache rather than one per backend, because the type library is
process-wide and a second backend should not generate it twice. Only a
*success* is cached: a machine where the load failed has to be able to try
again once whatever was missing is installed, which is the rule
`atspi.a11y_bus_probe` follows for a bus that had not started yet.

Tests replace this attribute -- `mock.patch.object(uia, "_CLIENT_MODULE",
fake)` -- which is why the seam is a value here rather than an import inside
the call.
"""


def comtypes_client():
    """`comtypes.client`, or None where comtypes is not installed.

    Imported inside the function and never at module scope: comtypes is a
    Windows library in practice, `import pyguitest` has to work everywhere, and
    a missing optional dependency is a condition to report rather than an
    ImportError to raise on the way in. Anything failing counts as the same
    answer -- comtypes refuses to import on some non-Windows builds with an
    OSError rather than an ImportError.
    """
    try:
        import comtypes.client as client
    except Exception:  # noqa: BLE001 - an unimportable comtypes is "no UIA"
        return None
    return client


def _initialize_com():
    """Put the calling thread in an apartment, so COM objects can be made in it.

    COM is initialized per *thread*, and comtypes can only ever do that for the
    thread that imported it. A `Session` built anywhere else -- a worker driving
    a second window in parallel, a test runner that dispatches onto a pool --
    would otherwise reach `CreateObject` with no apartment at all and be told
    "CoInitialize has not been called", which `_connection` can only report as
    this machine having no UI Automation: a per-thread state misread as a
    missing provider.

    Every failure is swallowed, and none of them is this function's to report.
    `RPC_E_CHANGED_MODE` -- the thread already being in the other apartment
    model, because the host application chose MTA -- is not an error at all
    here, since UIA is callable from either. Anything else that is genuinely
    wrong is wrong one line later too, where `CreateObject` runs into it and
    says so with the reason attached.
    """
    try:
        import comtypes
    except Exception:  # noqa: BLE001 - the same "no comtypes" comtypes_client sees
        return
    initialize = getattr(comtypes, "CoInitialize", None)
    if initialize is None:
        return
    try:
        initialize()
    except OSError:
        return


def client_module():
    """The generated `UIAutomationClient` bindings, or None.

    `comtypes.client.GetModule` reads `UIAutomationCore.dll`'s type library and
    generates the Python classes for every interface, structure and constant in
    it, then caches them in `comtypes.gen` -- a once-per-installation
    operation that later calls get for free. That is what makes `IUIAutomation`,
    the pattern interfaces and the `POINT` structure available by name: there
    is no comtypes equivalent of `_winapi`'s hand-declared prototypes, which is
    why this module has no prototypes in it.
    """
    global _CLIENT_MODULE
    if _CLIENT_MODULE is not None:
        return _CLIENT_MODULE
    client = comtypes_client()
    if client is None:
        return None
    try:
        _CLIENT_MODULE = client.GetModule(_UIAUTOMATION_CORE)
    except Exception:  # noqa: BLE001 - any failure at all means "no UIA here"
        return None
    return _CLIENT_MODULE


def available():
    """Whether UI Automation can be reached from this process.

    A type-library load rather than an import check, and the factory asks it
    before building anything: comtypes can be installed on a machine whose
    `UIAutomationCore.dll` will not register its typelib, and that condition is
    worth reporting (`_connection` spells it out) rather than discovering on
    the first element query. False on every host that is not Windows, since
    that is where the DLL lives.
    """
    return client_module() is not None


def _connection():
    """The (client module, `IUIAutomation`) pair this backend calls through.

    One function rather than two, because the pair is only ever useful
    together: a backend holding an interface module and no automation object
    would be half-constructed, and every method would have to ask which half
    was missing. This is the seam the tests replace.

    `CreateObject` with the documented CLSID and an explicit `interface=` is
    the shape the reference implementations use -- comtypes' `CreateObject`
    takes a CLSID string and asks the result for the interface -- and it means
    the COM object is created on the first query rather than when this module
    is imported.

    `_initialize_com` runs first because this may be any thread, and a thread
    with no apartment cannot create a COM object at all; its docstring has why
    that is not something comtypes has already done.
    """
    client = client_module()
    comtypes = comtypes_client()
    if client is None or comtypes is None:
        raise BackendUnavailable(
            "UI Automation is not reachable: comtypes is not installed, or "
            "UIAutomationCore.dll's type library could not be loaded. Install "
            "it with pip install 'pyguitest[windows]' -- the Windows element "
            "tree needs COM, and there is no other route to it"
        )
    _initialize_com()
    try:
        automation = comtypes.CreateObject(
            _CLSID_CUIAUTOMATION, interface=client.IUIAutomation
        )
    except Exception as exc:  # noqa: BLE001 - every failure here is the same
        raise BackendUnavailable(
            f"COM refused to create CUIAutomation ({exc}); this session has no "
            "UI Automation provider, or COM is not usable in this process"
        ) from exc
    return client, automation


# -- reading, matching and pointing ----------------------------------------


def _current(obj, name, default=None):
    """One of UIA's `Current*` properties, or `default` where it is not there.

    Every one of them is a COM `[propget]`, and comtypes' generated class may
    expose that as an attribute (`element.CurrentName`) or as the raw method
    (`element.get_CurrentName()`). Which of the two it does is not something a
    document settles, and their spellings are the only difference -- both are
    one call -- so this tries the attribute and then the method, and answers
    `default` for everything else. A provider that refuses (a stale element, a
    property it does not implement) is a state every caller here has a sensible
    answer for.

    That is exactly why `default` matters where "no answer" and "a falsy answer"
    have to be told apart: `Element.alive` passes `_ABSENT`, which no property
    can return.
    """
    try:
        value = getattr(obj, name)
    except AttributeError:
        method = getattr(obj, "get_" + name, None)
        if method is None:
            return default
        try:
            return method()
        except Exception:  # noqa: BLE001 - a refused property is not an error
            return default
    except Exception:  # noqa: BLE001 - the same, from the property itself
        return default
    return value() if callable(value) else value


def _matches_text(value, wanted):
    """Whether `value` satisfies `wanted`.

    Exact match for a plain string, `.search()` for a compiled pattern --
    mirrors `atspi._matches_text`, and through it the regex convention
    `Session.find_window` already uses for window titles. A caller who passes
    `re.compile("^Save")` gets a search on either platform.
    """
    if isinstance(wanted, re.Pattern):
        return wanted.search(value or "") is not None
    return value == wanted


def _control_types(role):
    """The UIA control type ids this package reports as `role`.

    Usually one, and five times in the table above two -- `push button` covers
    both a Button and a SplitButton, `panel` covers Pane and Group -- which is
    why a role filter becomes an *or* of two property conditions rather than
    one.

    Empty for a role UIA cannot produce at all. `Role.LABEL` is the one a
    caller is likeliest to ask for, since UIA's single Text control type comes
    back as `text`; an empty set means the search narrows to nothing and then
    rejects every candidate, which is the honest answer -- there is no label to
    find on Windows -- rather than an error about the question.

    Compared against every spelling of the role, for the reason
    `atspi._build_predicate` does the same: "button" and "push button" are one
    role to a caller, whichever at-spi2 version they learned it on, so a script
    written on Linux keeps working here.
    """
    wanted = spellings(role)
    return frozenset(
        control_type for control_type, name in _UIA_ROLES.items() if name in wanted
    )


def _point(client, x, y):
    """`(x, y)` as the POINT structure this typelib's `ElementFromPoint` wants.

    UIA's typelib carries the `wtypes` POINT, which comtypes turns into a
    `ctypes.Structure` class of its own. Passing `ctypes.wintypes.POINT` where
    that generated class is expected is a `TypeError` rather than a conversion,
    because ctypes compares structure classes by identity -- so the generated
    class is preferred where the module has one, under either of the two names
    MIDL emits for a typedef'd struct, and `_winapi`'s declaration (the same two
    LONGs, end to end) is the fallback for a module that has neither.
    """
    for name in ("POINT", "tagPOINT"):
        factory = getattr(client, name, None)
        if factory is not None:
            return factory(x, y)
    return _winapi.POINT(x, y)


class Element:
    """One node of the UI Automation tree.

    Thin wrapper over an `IUIAutomationElement`, exposing the subset the
    Capability interface promises so callers are not coupled to COM; `node`
    stays reachable for anything this does not cover, exactly as
    `atspi.Element` keeps its dogtail Node reachable.

    Unlike that one, the backend is not optional. A UIA element is a bare COM
    pointer with no methods of its own beyond its properties, so the tree walk,
    the pattern calls and the geometry all have to go back through the object
    that knows how to make them; `atspi.Element` can stand alone because
    dogtail's Node carries its own API.
    """

    __slots__ = ("node", "_backend", "_session")

    def __init__(self, node, backend, session=None):
        """Wrap one element, the backend that found it, and the session above it.

        `session` is set by Session as it hands an element out, never by a
        backend: a COM pointer knows nothing of the session above it, and
        double_click needs one, since the pointer is the session's. Elements
        made while walking the tree inherit it from the one they came from; see
        parent, children and find.
        """
        self.node = node
        self._backend = backend
        self._session = session

    @property
    def name(self):
        """The element's accessible name, such as a button's label."""
        return _current(self.node, "CurrentName", "") or ""

    @property
    def role(self):
        """The element's accessible role, such as 'push button'.

        Translated from UIA's control type through the table at the top of this
        module, which is why the answer is an at-spi role name rather than
        UIA's, and `unknown` for a control type outside the documented range.
        """
        control_type = _current(self.node, "CurrentControlType", None)
        return _UIA_ROLES.get(control_type, _UNKNOWN_ROLE)

    @property
    def parent(self):
        """The containing element in the control view, or None at the root."""
        parent = self._backend._parent(self.node)
        if parent is None:
            return None
        return Element(parent, self._backend, self._session)

    @property
    def children(self):
        """The elements directly inside this one, in the provider's order."""
        return [
            Element(child, self._backend, self._session)
            for child in self._backend._children(self.node)
        ]

    @property
    def visible(self):
        """Whether the element is currently showing.

        UIA answers the opposite question, `IsOffscreen`, and answers it about
        scrolling as well as hiding: an element scrolled out of view in a long
        list is offscreen, which is what at-spi's `showing` state means there
        too. A provider that refuses the read comes back visible, the same
        leniency `enabled` shows below and for the same reason -- a caller
        about to act on the element is better served by the optimistic answer
        than by a false "it is hidden".
        """
        return not bool(_current(self.node, "CurrentIsOffscreen", False))

    @property
    def enabled(self):
        """Whether the element accepts input, rather than being greyed out."""
        return bool(_current(self.node, "CurrentIsEnabled", True))

    @property
    def description(self):
        """The element's longer accessible description, often a tooltip."""
        return _current(self.node, "CurrentHelpText", "") or ""

    @property
    def text(self):
        """The element's text content, for text boxes and labels.

        Two patterns, in this order, because UIA splits what at-spi calls one
        property: a Text pattern (a document, a label, a read-only body) hands
        back its document range's text, and a Value pattern (an editable field)
        hands back its value as a string. `value` is the third thing and is a
        number -- a slider's position, not its label.
        """
        pattern = self._backend._pattern(self.node, "text")
        if pattern is not None:
            document = self._backend._document_text(pattern)
            if document is not None:
                return document
        pattern = self._backend._pattern(self.node, "value")
        if pattern is None:
            return None
        return _current(pattern, "CurrentValue", None)

    @property
    def value(self):
        """The numeric value of a slider, spinner, or progress bar, or None.

        Only the RangeValue pattern has one: an editable field's Value pattern
        holds text, and reading a float out of that would be inventing a
        number.
        """
        pattern = self._backend._pattern(self.node, "range value")
        if pattern is None:
            return None
        raw = _current(pattern, "CurrentValue", None)
        try:
            return None if raw is None else float(raw)
        except (TypeError, ValueError):
            return None

    @property
    def checked(self):
        """Whether a check box, radio button, or toggle is set.

        None in two cases, and both are an answer rather than a failure: an
        element with no Toggle pattern has nothing to be checked, and an
        *indeterminate* one has not answered "set" or "not set". A mixed-state
        check box is the second case, where True would claim something about
        the widget rather than report what it said. Read `checkable` first, as
        the interface asks.
        """
        pattern = self._backend._pattern(self.node, "toggle")
        if pattern is None:
            return None
        state = _current(pattern, "CurrentToggleState", ToggleState_Indeterminate)
        if state == ToggleState_Indeterminate:
            return None
        return state == ToggleState_On

    @property
    def checkable(self):
        """Whether the element has a check box, radio button, or toggle."""
        return self._backend._pattern(self.node, "toggle") is not None

    @property
    def selected(self):
        """Whether a list item, tab, or menu item is currently selected.

        None where the element publishes no SelectionItem pattern -- the same
        caveat as `checked`, and `selectable` is the same kind of first
        question.
        """
        pattern = self._backend._pattern(self.node, "selection item")
        if pattern is None:
            return None
        return bool(_current(pattern, "CurrentIsSelected", False))

    @property
    def selectable(self):
        """Whether the element can be a list item, tab, or menu selection."""
        return self._backend._pattern(self.node, "selection item") is not None

    @property
    def focused(self):
        """Whether the element currently has keyboard focus."""
        return bool(_current(self.node, "CurrentHasKeyboardFocus", False))

    @property
    def actions(self):
        """The names of the actions this element offers, sorted.

        One pattern probe per action, because that is the only way to ask: UIA
        publishes no list of what a widget can do, so an action is offered
        exactly when its pattern is there. The names are the ones `do_action`
        takes -- UIA's own, except that at-spi's `click` is accepted as a
        second name for `invoke`.
        """
        return sorted(
            action
            for action, (pattern, _method) in _ACTIONS.items()
            if self._backend._pattern(self.node, pattern) is not None
        )

    @property
    def pid(self):
        """The process this element belongs to, or None.

        A pid of zero is a provider saying it does not know, which is the same
        answer as not being asked -- the reading `atspi.Element.pid` gives it
        too.
        """
        return _current(self.node, "CurrentProcessId", None) or None

    @property
    def alive(self):
        """Whether the underlying element still exists.

        UIA has no "dead" flag: an element whose provider has gone answers a
        COM failure to every property, so this reads one and tells the
        difference between answering and not. `_ABSENT` is a default no
        property can return, which is what makes that possible.
        (`atspi.Element.alive` delegates the same question to dogtail's own
        `Node.dead`.)
        """
        return _current(self.node, "CurrentControlType", _ABSENT) is not _ABSENT

    def click(self):
        """Act on the element directly -- no coordinates, no injection.

        Three routes, in the order providers implement them: the Invoke
        pattern, which is a button's own "I have been pressed"; the Toggle
        pattern, which is what a check box or a switch publishes instead --
        `atspi.Element.click` reaches both on Linux, so a role of `check box`
        has to keep working here; and `LegacyIAccessiblePattern`'s default
        action, which is how a provider written for MSAA exposes its click.

        A pattern that is offered but refuses -- a disabled button, a provider
        that answers an error -- raises through `_call_pattern` with the
        HRESULT in the message, and the later routes are *not* tried. That is
        deliberate, and it is the one place this method chooses a loud failure
        over a likely success: an element can publish both Invoke and
        `LegacyIAccessible`, and an MSAA shim is under no obligation to check
        the enabled state that Invoke just refused on, so falling through could
        turn "this button is disabled" into a click reported as successful.
        A test that should have failed passing is the one failure this package
        will not trade for a convenience. Where a refusal really is transient,
        the remaining routes are reachable by name --
        `element.do_action("do default action")` -- which is a caller saying
        they know what they are skipping.

        An element that offers none of the three raises `ElementNotActionable`,
        the same typed answer the AT-SPI backend gives when neither of its own
        routes is open, with this platform's reason in it rather than that
        one's.
        """
        for action in ("invoke", "toggle", "do default action"):
            if self._backend._call_pattern(self.node, action):
                return
        raise ElementNotActionable(
            self.role,
            self.name,
            "UI Automation offers it no Invoke, Toggle or LegacyIAccessible "
            "pattern, so there is no accessible action to perform; click it by "
            "coordinate instead, e.g. gui.extents(element) then gui.click()",
        )

    def double_click(self):
        """Double-click the element: locate it, then inject the gesture.

        There is no accessible action to name here, the way `click` names one: a
        double-click is a gesture on the pointer, and UIA publishes patterns for
        widgets rather than gestures. So the element stays the locator and the
        gesture falls back to the pointer, where this package's real
        double_click lives -- on the Session, which is the only thing holding a
        backend able to move one. Hence the delegation below, and hence
        `_session`.

        Needs `Capability.ELEMENT_GEOMETRY` on top of what
        `Session.double_click` needs, since the rectangle has to be read to find
        the point, and the pointer capabilities on top of that -- which come
        from `win32` on this platform, not from this backend. Raises
        PyGUITestError where `extents()` has no rectangle for this element, which
        is what double_click_element raises, and where there is no session to
        delegate to at all.
        """
        if self._session is None:
            raise PyGUITestError(
                f"{self.name!r} carries no session to double-click through -- "
                "it came from a backend directly rather than from a Session. "
                "Use gui.double_click_element(element), or take the element "
                "from the session instead: gui.button(...), gui.element(...), "
                "gui.root_element()"
            )
        self._session.double_click_element(self)

    def _bind_session(self, session):
        """Record which Session handed this element out.

        Named rather than left as a plain attribute, so that Session can offer
        it to any element type without knowing which backend built it: an
        element with no such method simply never gets a session, and double_click
        is the only thing that notices. Elements made while walking the tree pass
        on whatever their source carried, which is what keeps
        `gui.root_element().child(...)` able to double_click.
        """
        self._session = session

    def focus(self):
        """Give the element keyboard focus.

        `SetFocus` is a method on the element rather than a pattern, and
        providers are allowed to refuse it -- an element that cannot take focus,
        or one that has since gone. Either refusal is a typed error naming what
        was attempted.
        """
        try:
            self.node.SetFocus()
        except Exception as exc:  # noqa: BLE001 - a provider that will not focus
            raise self._backend._refuse(f"could not focus {self!r}", exc) from exc

    def set_text(self, text):
        """Replace the element's text content, through UIA's Value pattern.

        Raises `CapabilityUnsupported` where the element publishes no Value
        pattern at all -- a read-only label, a document, a control that merely
        looks like a field -- because "there is no way in" is the useful part of
        that answer, where a raw COM failure would report an HRESULT and leave
        the reader to work out which of the two it meant.
        """
        pattern = self._backend._pattern(self.node, "value")
        if pattern is None:
            raise CapabilityUnsupported(
                Capability.ELEMENT_ACTION,
                self._backend.name,
                f"{self!r} publishes no Value pattern, so its text is read-only",
            )
        try:
            pattern.SetValue(text)
        except Exception as exc:  # noqa: BLE001 - a field that refuses the write
            raise self._backend._refuse(
                f"could not set the text of {self!r}", exc
            ) from exc

    def do_action(self, name):
        """Perform one of `actions`, named as UIA spells it.

        at-spi's most common spelling is accepted too: `click` (and `press`,
        `activate`) mean `invoke` here, because that is what a script written
        against the Linux backend says and UIA's Invoke pattern is exactly what
        it means. An unknown name raises ValueError listing what this element
        does offer, which is what `press_key` does with an unknown key and is
        far more useful than doing nothing.

        Raises `ElementNotActionable` where the name is a real action that this
        element does not publish -- a `select` on something with no
        SelectionItem pattern -- so a caller can tell "typo" from "this widget
        cannot".
        """
        action = _ACTION_ALIASES.get(name, name)
        if action not in _ACTIONS:
            raise ValueError(
                f"{name!r} is not an action this backend performs; {self.role} "
                f"{self.name!r} offers {', '.join(self.actions) or 'none'}"
            )
        if not self._backend._call_pattern(self.node, action):
            raise ElementNotActionable(
                self.role,
                self.name,
                f"it publishes no pattern that performs {name!r}; it offers "
                f"{', '.join(self.actions) or 'no actions at all'}",
            )

    def select(self):
        """Select this element, for a list item, tab, or menu entry.

        A typed refusal rather than a silent no-op where the element publishes
        no SelectionItem pattern, for the same reason `set_text` refuses: a
        caller who asked for a selection and got nothing has no way to tell
        that apart from a selection that happened.
        """
        if self._backend._call_pattern(self.node, "select"):
            return
        raise CapabilityUnsupported(
            Capability.ELEMENT_ACTION,
            self._backend.name,
            f"{self!r} publishes no SelectionItem pattern, so it cannot be selected",
        )

    def choose(self, option):
        """Pick `option` from this dropdown by its visible text.

        UIA has no combo-box value setter the way dogtail's `combovalue` is, so
        this is the two real steps: expand the control, then find the item and
        select it. Expanded first because a combo box's list usually does not
        exist in the tree until the popup opens.

        The item is looked for among this element's own descendants and then
        under the tree root, in that order, because the two kinds of combo box
        put it in different places: a Win32 one keeps its list inside the
        control, and a UWP or WPF one puts the popup beside the whole window
        rather than inside the box. Whatever is found is selected through its
        SelectionItem pattern, or clicked where it has none -- a menu entry in a
        popup is often Invoke-only. Raises ElementNotFound naming the option
        when neither place has it.
        """
        self._backend._call_pattern(self.node, "expand")
        for container in (self, self._backend.root_element()):
            for role in (Role.MENU_ITEM, Role.LIST_ITEM):
                candidate = container.child(role=role, name=option)
                if candidate is None:
                    continue
                if candidate.selectable:
                    candidate.select()
                else:
                    candidate.click()
                return
        raise ElementNotFound(
            f"no list item or menu item named {option!r} is in {self!r} or "
            "anywhere on the desktop"
        )

    def options(self):
        """The choices this dropdown or list offers, as Elements.

        Menu items before list items, matching `atspi.Element.options` -- and
        an empty list is a truthful answer about a combo box whose popup has
        not been opened, not a failure.
        """
        found = self.find(role=Role.MENU_ITEM)
        return found or self.find(role=Role.LIST_ITEM)

    def find(self, role=None, name=None):
        """Search this element's descendants by role and/or name.

        Wrapped again here rather than handed back as the backend made them,
        because a backend element carries no session: this passes its own on, so
        `gui.root_element().child(...).double_click()` reaches the pointer the
        way an element the session found does. `atspi.Element.find` does the same
        thing with the same reason.
        """
        found = self._backend.find_elements(role=role, name=name, within=self)
        return [Element(node.node, self._backend, self._session) for node in found]

    def child(self, role=None, name=None):
        """Return the first descendant matching role and/or name, or None."""
        matches = self.find(role=role, name=name)
        return matches[0] if matches else None

    def is_ancestor_of(self, other):
        """Whether `other` sits somewhere inside this element.

        Walked upwards asking `CompareElements` at each step rather than
        comparing pointers: two pointers to one element are different addresses
        as often as not. Bounded by `_MAX_DEPTH`, so a provider reporting a
        cycle cannot make this spin.
        """
        node = other.node
        for _ in range(_MAX_DEPTH):
            node = self._backend._parent(node)
            if node is None:
                return False
            if self._backend._same(node, self.node):
                return True
        return False

    def __repr__(self):
        """The role and name, which is how an element is written in a script."""
        return f"Element({self.role!r}, {self.name!r})"


if TYPE_CHECKING:

    def _conforms(element: Element) -> _ElementInterface:
        """Check, statically only, that this Element satisfies the protocol.

        Session.element and the widget finders are annotated with base.Element,
        so this class is what makes those annotations true. Renaming or dropping
        a member here would otherwise surface as a type error in whoever called
        it, a module away from the cause.
        """
        return element


class UiaBackend(GUIBackend):
    """Element search, element actions and element geometry, over UI Automation.

    Registered at 90, in the read-only band beside `atspi`, because that is what
    it is: the elements it hands out are acted on through their own control
    patterns, with no coordinates and no injected input, so it needs neither the
    geometry nor the input permission the other backends divide up.
    """

    name = "uia"

    def __init__(self, environment=None):
        """Reach UI Automation, and make this thread's coordinates physical.

        `_connection` is what can raise: no comtypes, a type library that will
        not load, or a COM object the machine refuses to create. Each arrives as
        `BackendUnavailable` with its own reason, which is what a caller who
        named `uia` directly asked for -- and what automatic composition folds
        into "not this session" without comment.

        The DPI call is neither optional nor this backend's own: UIA reports
        bounding rectangles and takes `ElementFromPoint` coordinates in physical
        screen pixels, `win32` injects and reads in the same space, and an
        unaware thread is shown a virtualised desktop instead. See
        `_winapi.set_thread_dpi_awareness` for the whole argument, including why
        it is per thread and why a failure there is swallowed.
        """
        self._client, self._automation = _connection()
        self.environment = environment
        _winapi.set_thread_dpi_awareness()

    def close(self):
        """Stop holding the automation object.

        There is no session to close: COM is per thread and its objects belong
        to the process, so this drops the one reference this backend owns and
        leaves the thread's apartment alone -- releasing that is comtypes' own
        business, and a caller who wants `CoUninitialize` can call it once
        nothing they hold still needs COM.

        Elements the caller already holds keep answering, since they are the
        caller's own COM pointers; every *search* and hit test goes back through
        the automation object, and those refuse through `_uia`, naming what
        happened rather than failing on a None.
        """
        self._automation = None

    def _uia(self):
        """The `IUIAutomation` object, or raise naming that this one is closed."""
        if self._automation is None:
            raise CapabilityUnsupported(
                Capability.ELEMENT_TREE,
                self.name,
                "this backend has been closed; connect() again to reach UI Automation",
            )
        return self._automation

    @property
    def capabilities(self):
        """Elements, their actions and their geometry -- and nothing else.

        The absence is the interesting part, and it is the split
        docs/developers/adr-003-windows.md draws: `win32` serves `windows()`, so
        no `WINDOW_*` capability is claimed here even though UIA could answer
        some of them -- the objects this backend hands back are elements rather
        than window handles, so every placement or state call granted here would
        have nothing to point at. `PROCESS_LAUNCH`, `TIMING`, the input
        capabilities and the tier-6 reads come from that backend too.
        """
        return CapabilitySet(
            {
                Capability.ELEMENT_TREE,
                Capability.ELEMENT_ACTION,
                Capability.ELEMENT_GEOMETRY,
            }
        )

    def _refuse(self, what, error):
        """A typed refusal naming what was attempted and what answered.

        One place, because several call sites would otherwise each invent half
        the sentence -- and the half that matters is the one
        `CapabilityUnsupported` cannot know: the HRESULT a provider returned.
        """
        return CapabilityUnsupported(
            Capability.ELEMENT_ACTION, self.name, f"{what}: {error}"
        )

    def _pattern(self, node, key):
        """The control pattern `key` names on `node`, or None.

        `GetCurrentPattern` returns the pattern as an `IUnknown`, which is then
        asked for the interface its methods live on -- the documented two-step.
        An element that does not implement the pattern answers a COM failure,
        and that is the ordinary answer here rather than an error: it is how
        "this widget cannot be toggled" is spelled in UIA.

        Every refusal folds into None, including a provider raising something
        other than a COM error, because every caller has a sensible answer for
        "not offered": `actions` leaves it out, `click` tries its next route,
        `checkable` is False.
        """
        pattern_id, interface_name = _PATTERN_INTERFACES[key]
        interface = getattr(self._client, interface_name, None)
        if interface is None:
            return None
        try:
            unknown = _null_to_none(node.GetCurrentPattern(pattern_id))
        except Exception:  # noqa: BLE001 - not implemented is the usual answer
            return None
        if unknown is None:
            # The ordinary answer for a pattern this element does not publish,
            # and it arrives as a NULL pointer rather than as None -- see
            # `_null_to_none`. Without that conversion this fell through to
            # QueryInterface on a null pointer and relied on the raise, which
            # is an exception raised and caught for every pattern an element
            # lacks: seven per element on `actions` alone.
            return None
        try:
            return unknown.QueryInterface(interface)
        except Exception:  # noqa: BLE001 - the same question, one call later
            return None

    def _call_pattern(self, node, action):
        """Perform `action` through its pattern; False where it is not offered.

        The split between False and an exception is deliberate. False means
        "this element publishes no such pattern", which `click` answers by
        trying its next route; `CapabilityUnsupported` means the pattern was
        there and refused the call, which a caller needs to see -- a disabled
        button's Invoke really did fail, and quietly trying something else would
        hide that.
        """
        pattern_key, method = _ACTIONS[action]
        pattern = self._pattern(node, pattern_key)
        if pattern is None:
            return False
        try:
            getattr(pattern, method)()
        except Exception as exc:  # noqa: BLE001 - the pattern refused the call
            raise self._refuse(
                f"the {action} pattern failed on this element", exc
            ) from exc
        return True

    def _document_text(self, pattern):
        """A Text pattern's whole text, or None where it will not give it.

        `GetText(-1)` is the documented "all of it": a negative length means the
        range is not truncated, where a positive one is a character count.
        """
        document = _null_to_none(_current(pattern, "DocumentRange"))
        if document is None:
            return None
        try:
            return document.GetText(-1)
        except Exception:  # noqa: BLE001 - a range that will not answer
            return None

    # -- elements ----------------------------------------------------------

    def root_element(self):
        """The desktop element, which is UI Automation's tree root.

        Not an application the way it is on Linux: UIA has no per-application
        node, so this element's children are the toplevel windows themselves and
        a window's parent is the desktop. Anything grouping windows by their
        parent -- `pyguitest inspect` does -- therefore shows the desktop
        element's own name as the group heading, which is a difference from the
        Linux output rather than a bug; docs/validation.md carries it among the
        things the first real run confirms.
        """
        self.require(Capability.ELEMENT_TREE)
        return Element(self._uia().GetRootElement(), self)

    def find_elements(
        self,
        role=None,
        name=None,
        within=None,
        enabled=None,
        visible=None,
        description=None,
        predicate=None,
    ):
        """Search the element tree, answering exactly what was asked.

        Two layers, and the second is what makes the answer right. UIA is asked
        to narrow the tree itself -- one property condition per filter it can
        express, folded into a single condition -- because walking a browser's
        several thousand elements one COM call at a time is the difference
        between a search and a hang. Everything that comes back is then checked
        against the same predicate `atspi.find_elements` applies, which is what
        makes the two backends agree about what a role, a name and a state mean
        -- and what keeps a condition this module got subtly wrong from becoming
        a silently missing match: the narrowing can only come back coarser than
        asked, and the predicate is the answer.

        `name`/`description` take a plain string (exact match) or a compiled
        regex (`.search()`); `enabled`/`visible` filter on element state;
        `predicate` is an arbitrary `Element -> bool` for anything else, which
        together with `Element.parent`/`.children`/`.is_ancestor_of` covers
        ancestor and descendant queries without a dedicated relation API.
        """
        self.require(Capability.ELEMENT_TREE)
        root = within.node if within is not None else self._uia().GetRootElement()
        conditions = self._conditions(role, name, enabled, visible, description)
        matches = self._build_predicate(
            role, name, enabled, visible, description, predicate
        )
        return [
            Element(node, self)
            for node in self._find(root, TreeScope_Descendants, conditions)
            if matches(node)
        ]

    def find_element(
        self,
        role=None,
        name=None,
        within=None,
        enabled=None,
        visible=None,
        description=None,
        predicate=None,
    ):
        """The first match, or None."""
        matches = self.find_elements(
            role=role,
            name=name,
            within=within,
            enabled=enabled,
            visible=visible,
            description=description,
            predicate=predicate,
        )
        return matches[0] if matches else None

    def _build_predicate(self, role, name, enabled, visible, description, predicate):
        """A `node -> bool` function applying every filter a search was given.

        Mirrors `atspi._build_predicate` deliberately, defaults included: a
        provider that refuses `IsEnabled` counts as enabled, and one that refuses
        `IsOffscreen` counts as showing, because a caller about to try acting on
        the element is better served by trying than by a silent "it is not
        there". The role is compared against every control type this package
        reports for it *and* every spelling of the role that was asked for.
        """
        control_types = None if role is None else _control_types(role)

        def matches(node):
            """Whether this node has the wanted role and matches the filters."""
            if control_types is not None and (
                _current(node, "CurrentControlType", None) not in control_types
            ):
                return False
            if name is not None and not _matches_text(
                _current(node, "CurrentName", "") or "", name
            ):
                return False
            if (
                enabled is not None
                and bool(_current(node, "CurrentIsEnabled", True)) != enabled
            ):
                return False
            if (
                visible is not None
                and bool(_current(node, "CurrentIsOffscreen", False)) == visible
            ):
                return False
            if description is not None and not _matches_text(
                _current(node, "CurrentHelpText", "") or "", description
            ):
                return False
            if predicate is not None:
                return bool(predicate(Element(node, self)))
            return True

        return matches

    def _conditions(self, role, name, enabled, visible, description):
        """The filters UIA can apply itself, folded into one condition.

        UIA searches by property, and these are the same properties the filters
        read back, so every *exact* filter is pushed down to the providers --
        where a search over a browser's tree costs one call instead of one per
        element. Three things stay client-side because no property expresses
        them: a regex, a `predicate`, and a role, which is a translation of a
        control type rather than a property value and so becomes one condition
        per control type, or-ed where the role spans two.

        `None` for a filter that cannot be pushed down, and `None` for the whole
        answer where nothing could be. `_find` reads that as "no narrowing
        possible" and asks for everything in scope, which is the same tree the
        predicate is about to walk anyway.
        """
        parts = []
        if role is not None:
            parts.append(self._control_type_condition(role))
        if isinstance(name, str):
            parts.append(self._property_condition(UIA_NamePropertyId, name))
        if enabled is not None:
            parts.append(self._property_condition(UIA_IsEnabledPropertyId, enabled))
        if visible is not None:
            # The property is the inverse of the filter: asking for visible
            # elements is asking for the ones that are not offscreen.
            parts.append(
                self._property_condition(UIA_IsOffscreenPropertyId, not visible)
            )
        if isinstance(description, str):
            parts.append(self._property_condition(UIA_HelpTextPropertyId, description))
        return self._and(parts)

    def _control_type_condition(self, role):
        """A condition matching any control type this package reports as `role`.

        One condition for the five roles that map to two control types, and one
        for everything else; `None` for a role UIA cannot report at all, which
        leaves the search unnarrowed and lets the predicate reject every
        candidate -- the honest answer for `role=Role.LABEL` on this platform.
        """
        parts = [
            self._property_condition(UIA_ControlTypePropertyId, control_type)
            for control_type in sorted(_control_types(role))
        ]
        return self._or(parts)

    def _property_condition(self, property_id, value):
        """A condition matching one property, or None where UIA refused it.

        A refusal is swallowed rather than raised, for a reason worth knowing: a
        property id this module transcribed wrongly would come back as an
        HRESULT error from every provider, and a search that narrows too little
        still answers correctly, because the predicate is what decides. Raising
        here would turn an unverifiable constant into a hard failure instead of
        a slow query.
        """
        try:
            return self._uia().CreatePropertyCondition(property_id, value)
        except Exception:  # noqa: BLE001 - coarse narrowing, never a wrong answer
            return None

    def _view_condition(self):
        """UIA's own control-view condition, or None where it cannot be had.

        The control view is the tree a user can interact with: providers filter
        out layout panes and decoration of their own accord, which makes it the
        closest thing UIA has to the accessible tree AT-SPI hands over. `FindAll`
        walks whichever view the condition selects, so every search here is
        combined with this one -- and a None falls back to the raw view, which is
        coarser but never wrong, since the predicate filters the result.
        """
        return _current(self._uia(), "ControlViewCondition")

    def _true_condition(self):
        """UIA's match-everything condition, or None where it cannot be built."""
        try:
            return self._uia().CreateTrueCondition()
        except Exception:  # noqa: BLE001 - a condition this UIA will not make
            return None

    def _or(self, conditions):
        """One condition matching any of `conditions`, folded two at a time."""
        return self._fold(conditions, "CreateOrCondition")

    def _and(self, conditions):
        """One condition matching all of `conditions`, plus the control view."""
        parts = [condition for condition in conditions if condition is not None]
        view = self._view_condition()
        if view is not None:
            parts.insert(0, view)
        return self._fold(parts, "CreateAndCondition")

    def _fold(self, conditions, method):
        """`conditions` combined by `method`, or None where there are none.

        `CreateAndCondition` and `CreateOrCondition` take exactly two
        conditions; the three-or-more forms want an array of interface pointers,
        which a Python caller has no good way to build through comtypes. Folding
        pairwise needs no array and asks the identical question -- and the
        nesting stays shallow, since the widest fold here is the five filters
        plus the control view.
        """
        parts = [condition for condition in conditions if condition is not None]
        if not parts:
            return None
        combined = parts[0]
        for part in parts[1:]:
            try:
                combined = getattr(self._uia(), method)(combined, part)
            except Exception as exc:  # noqa: BLE001 - a condition it will not make
                raise CapabilityUnsupported(
                    Capability.ELEMENT_TREE,
                    self.name,
                    f"UI Automation could not build a search condition: {exc}",
                ) from exc
        return combined

    def _find(self, node, scope, condition):
        """`FindAll`'s answer as a list of element pointers.

        A search that fails at the top is a typed refusal rather than an empty
        list: an empty list is a real answer -- "nothing matched" -- and a caller
        has to be able to tell the two apart, which is the whole reason this
        package raises typed errors rather than returning zero.
        """
        if condition is None:
            condition = self._true_condition()
        if condition is None:
            raise CapabilityUnsupported(
                Capability.ELEMENT_TREE,
                self.name,
                "UI Automation would not build any search condition at all",
            )
        try:
            found = node.FindAll(scope, condition)
        except Exception as exc:  # noqa: BLE001 - a provider that will not search
            raise CapabilityUnsupported(
                Capability.ELEMENT_TREE,
                self.name,
                f"UI Automation could not search this tree: {exc}",
            ) from exc
        return self._elements_in(found)

    def _elements_in(self, array):
        """Every element in an `IUIAutomationElementArray`.

        An element that vanishes between the search and this read is skipped
        rather than fatal: a desktop where windows open and close under a
        running test does that routinely, and one element closing is no reason
        to throw away the rest of the answer.
        """
        if array is None:
            return []
        count = _current(array, "Length", 0) or 0
        found = []
        for index in range(count):
            try:
                found.append(array.GetElement(index))
            except Exception:  # noqa: BLE001 - an element gone mid-walk
                continue
        return found

    # -- geometry ----------------------------------------------------------

    def extents(self, element):
        """An element's (x, y, width, height) in screen coordinates, or None.

        No parameter about which coordinate space these are in, because there is
        no ambiguity to resolve: UIA answers in physical screen pixels, which is
        what `win32` reads and injects in, and what a caller sees in a
        screenshot.
        """
        self.require(Capability.ELEMENT_GEOMETRY)
        return self._rect(element.node)

    def _rect(self, node):
        """One element's screen rectangle, or None where it has no useful one.

        UIA's rectangle is left/top/right/bottom, converted here to the
        (x, y, width, height) every backend in this package answers with. An
        element occupying no screen space reports an empty rectangle -- all
        four fields zero -- which is the ordinary answer for something not
        currently laid out, so it comes back as None rather than as a
        zero-sized box at the origin, exactly as `atspi` treats the same case.
        """
        rect = _current(node, "CurrentBoundingRectangle")
        if rect is None:
            return None
        left, top = int(rect.left), int(rect.top)
        width, height = int(rect.right) - left, int(rect.bottom) - top
        if width <= 0 or height <= 0:
            return None
        return (left, top, width, height)

    def element_at(self, x, y):
        """The deepest element at a screen point, or None.

        `ElementFromPoint` answers with the element at the point -- the topmost
        one, as the provider sees it -- and the point is then followed down
        through the children that really contain it: the same descent the AT-SPI
        backend does, for the same reason. A provider may answer hit tests with
        a container, and a caller asking what is at a point wants the widget
        under the pointer rather than the window it sits in.

        An element whose rectangle does not contain the point is rejected rather
        than returned, which is the rule `atspi.element_at` applies too: a
        provider reporting rectangles it does not cover cannot be checked any
        other way, and answering with an element that is demonstrably somewhere
        else would be worse than answering None.

        None as well where the element under the point was removed before it
        could be read -- UIA documents `UIA_E_ELEMENTNOTAVAILABLE` for exactly
        that race, and on a desktop where windows open and close under a running
        test it is ordinary rather than exceptional.
        """
        self.require(Capability.ELEMENT_GEOMETRY)
        try:
            node = _null_to_none(
                self._uia().ElementFromPoint(_point(self._client, x, y))
            )
        except Exception:  # noqa: BLE001 - that race, or a provider that refuses
            return None
        if node is None or not self._covers(node, x, y):
            return None
        return Element(self._descend(node, x, y), self)

    def _descend(self, node, x, y):
        """Follow the point down to the deepest child that really covers it.

        One level at a time rather than by asking `ElementFromPoint` again: that
        call is desktop-wide, so asking it about a point inside the element it
        just returned would answer with the same element again. Bounded by
        `_MAX_DEPTH`, so a provider reporting a cycle cannot make this spin.
        """
        for _ in range(_MAX_DEPTH):
            child = self._child_at(node, x, y)
            if child is None:
                return node
            node = child
        return node

    def _child_at(self, node, x, y):
        """The child of `node` whose own rectangle contains the point, or None."""
        for child in self._children(node):
            if self._covers(child, x, y):
                return child
        return None

    def _covers(self, node, x, y):
        """Whether the element's own rectangle contains the point."""
        rect = self._rect(node)
        if rect is None:
            return False
        left, top, width, height = rect
        return left <= x < left + width and top <= y < top + height

    def _children(self, node):
        """`node`'s children in the control view, in the provider's order.

        One `FindAll` rather than a walker loop, for two reasons: it is one COM
        call instead of one per child, and it cannot spin on a provider that
        reports a cycle, where stepping through siblings until the walker
        answers None could.
        """
        return self._find(node, TreeScope_Children, self._view_condition())

    def _parent(self, node):
        """The containing element in the control view, or None at the root.

        The walker is asked here rather than `FindAll`, because there is no
        parent-scoped search: `TreeScope_Parent` selects the parent *of each
        element in a set that satisfied a condition*, which is a different
        question. The walker also answers in the control view, matching the
        condition `_children` searches with.

        `_null_to_none` is what makes "or None at the root" true, and it was
        not: COM answers the root's parent with a **NULL pointer**, which is a
        perfectly ordinary Python object and emphatically not `None`, so the
        `is None` this used to rely on never fired. Measured live against real
        UI Automation, where `root_element().parent` came back as
        `Element('unknown', '')` -- an element wrapping a null pointer, whose
        `alive` is False and whose every property answers the empty default --
        instead of the None the caller is told to expect. No fake could have
        shown it: a fake written in Python returns None because that is what
        Python does.
        """
        walker = _current(self._uia(), "ControlViewWalker")
        if walker is None:
            return None
        try:
            return _null_to_none(walker.GetParentElement(node))
        except Exception:  # noqa: BLE001 - a parent that will not answer
            return None

    def _same(self, first, second):
        """Whether two element pointers are the same element.

        `CompareElements`, not `==`: two COM pointers to one element are
        different addresses as often as not, so comparing them directly would
        answer "no" about an element and itself.
        """
        try:
            return bool(self._uia().CompareElements(first, second))
        except Exception:  # noqa: BLE001 - one of them is gone, so: not the same
            return False
