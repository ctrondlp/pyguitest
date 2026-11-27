"""Accessible role names.

A role is what a widget *is* -- a button, a text box, a dropdown -- as reported
by the accessibility bus. This is what makes element automation possible at all:
X11::GUITest could only walk X windows, and modern GTK and Qt toolkits draw
their widgets client-side, so a button is not a window and has no id to find.
The accessibility tree is the only place those widgets are individually visible.

The values are the strings AT-SPI uses. They are exposed as constants so code
reads as intent rather than as a magic string, and so a typo fails at import
rather than silently matching nothing::

    gui.find_element(role=Role.PUSH_BUTTON, name="OK").click()

Those strings are not as stable as they look, which is what `spellings` is
for -- see its docstring.
"""

from collections.abc import Sequence

__all__ = ["Role", "canonical_role", "spellings"]


class Role:
    """Accessible role names, grouped by what you would look for."""

    # -- things you press --------------------------------------------------
    PUSH_BUTTON = "push button"
    TOGGLE_BUTTON = "toggle button"
    CHECK_BOX = "check box"
    RADIO_BUTTON = "radio button"
    LINK = "link"
    SWITCH = "switch"
    """A GTK4 `GtkSwitch`, measured live (4.24.1). Qt has no equivalent --
    a QCheckBox is `check box` there -- which is why this is new rather
    than an existing role simply being found. Windows has no spelling for
    it: UIA's control-type table, which is total over the documented ids,
    maps nothing onto `switch`. A Mac has one -- an `AXCheckBox` whose
    subrole is `AXSwitch`, measured on macOS 26 with a real checkbox beside
    the switches in the same pane telling the two apart."""

    # -- things you type into ----------------------------------------------
    TEXT = "text"
    """A text body -- a region of text in its own right, as against a
    caption (`LABEL`). Windows has no spelling for it: every control type
    50020 element measured -- Win32 STATIC, WPF TextBlock and Label,
    WinForms Label, and UWP TextBlocks across the Settings app -- is a
    caption, and the Text pattern cannot separate the two because the UWP
    captions all publish it; a body there is an `entry` or a
    `document frame`."""
    ENTRY = "entry"
    PASSWORD_TEXT = "password text"
    SPIN_BUTTON = "spin button"

    # -- things you choose from --------------------------------------------
    COMBO_BOX = "combo box"
    LIST = "list"
    LIST_ITEM = "list item"
    MENU_BAR = "menu bar"
    """The bar the menus hang from. Every backend already produced exactly
    this string -- at-spi natively, macOS's `AXMenuBar`, UIA control type
    50010 -- but only as a raw string a caller had to know; promoted to a
    constant the way `TABLE_COLUMN_HEADER` was once something could ask
    for it by name."""
    MENU = "menu"
    POPUP_MENU = "popup menu"
    """Qt's spelling for an opened menu -- a `QMenu`, submenus included,
    publishes `popup menu` where GTK publishes `menu`. Measured live on the
    torture fixture, whose drivers had to accept both spellings by hand.
    These are two real roles side by side in one at-spi2 (2.62.1 lists
    both), unlike the renamed push button -- so this is a constant and not
    an alias group, and a search that wants every opened menu asks for
    both: `MENU_ROLES`."""
    MENU_ITEM = "menu item"
    CHECK_MENU_ITEM = "check menu item"
    """On Windows, a MenuItem publishing the Toggle pattern: a WPF
    `IsCheckable` item answers Toggle whether currently checked or not --
    the stable signal a role needs, measured on Windows 10 -- while the
    classic Win32 menu proxy exposes nothing even on a checked item, so
    classic items honestly stay `menu item`. On a Mac the role cannot
    exist: every `AXMenuItem` carries the identical attribute set and only
    the mark char -- the checked *state* -- differs, measured across
    Finder's menus, so an unchecked checkable item is indistinguishable
    from a plain one; the mark feeds `Element.checked` instead."""
    RADIO_MENU_ITEM = "radio menu item"
    """A Linux spelling only. Nothing on Windows distinguishes one --
    even an `MFT_RADIOCHECK` item exposes neither Toggle nor SelectionItem
    through the classic menu proxy, measured on Windows 10 -- and a Mac
    has only the mark char; see `CHECK_MENU_ITEM` for both measurements."""
    PAGE_TAB = "page tab"
    PAGE_TAB_LIST = "page tab list"

    # -- things that hold other things -------------------------------------
    FRAME = "frame"
    WINDOW = "window"
    DIALOG = "dialog"
    ALERT = "alert"
    """A message box's toplevel -- `GtkMessageDialog` (GTK3) and
    `QMessageBox` (Qt 6) both publish it as `alert`, not `dialog`,
    measured live on seven desktops (GTK4's `Gtk.AlertDialog` is a plain
    `dialog`). A toplevel in its own right: an application's direct
    child, listed by `windows()` and ascended to as the window by element
    geometry, which is why it is in `WINDOW_ROLES`. A Linux spelling:
    Windows and macOS have no `alert` role, and a message box there
    reports `dialog` -- which is the other reason `WINDOW_ROLES` carries
    both."""
    FILE_CHOOSER = "file chooser"
    """A file dialog's toplevel -- `GtkFileChooserDialog`, and so Qt's
    `QFileDialog` wherever it uses the GTK platform dialog -- publishes
    as `file chooser` rather than `dialog`, measured live on GTK 3.24. In
    `WINDOW_ROLES` for the same reason as `ALERT`: before it was, a Save
    dialog's Cancel button had no window to be placed in and its
    `extents()` were refused on every Wayland desktop. Also a Linux
    spelling -- the native file dialogs on Windows and macOS report
    `dialog`."""
    PANEL = "panel"
    LAYERED_PANE = "layered pane"
    TOOL_BAR = "tool bar"
    STATUS_BAR = "status bar"
    """Windows and Linux only. UIA maps control type 50017 onto it; no AX
    name is mapped, because a Mac's status surfaces are ordinary groups and
    text with no distinguishing subrole to read -- so a `role=STATUS_BAR`
    search matches nothing there rather than guessing at one."""
    SCROLL_PANE = "scroll pane"
    """Linux and macOS only (`AXScrollArea`). UIA has no counterpart: a
    scrollable region there is the control it scrolls, carrying the Scroll
    pattern rather than a container role of its own, so a
    `role=SCROLL_PANE` search matches nothing on Windows."""
    SPLIT_PANE = "split pane"
    """Linux and macOS only (`AXSplitGroup`). UIA has no counterpart -- a
    splitter there is a `pane` with a Transform pattern -- so a
    `role=SPLIT_PANE` search matches nothing on Windows."""
    VIEWPORT = "viewport"
    TABLE = "table"
    TABLE_CELL = "table cell"
    """Linux and macOS only, and the asymmetry is newer than the role:
    0.29.0 mapped `AXCell` onto it, which left Windows the one platform
    where it matches nothing. UIA reports the *row* (control type 50029)
    where at-spi reports cells and rows separately -- see the note beside
    that entry in `uia.py` -- so a portable row walk asks for the group,
    `role=(Role.TABLE_CELL, Role.TREE_ITEM, Role.TABLE_ROW)`, rather than
    this constant alone."""
    TABLE_ROW = "table row"
    TABLE_COLUMN_HEADER = "table column header"
    TREE = "tree"
    TREE_ITEM = "tree item"
    TREE_TABLE = "tree table"
    """A GTK4 `GtkColumnView`, measured live (4.24.1) -- GTK's replacement
    for the deprecated `GtkTreeView`, which reports as plain `tree`. Not an
    alias of either existing role: a `GtkColumnView`'s own cells publish
    `listitem.scroll-to` rather than `click`/`press`, so code written
    against `tree`'s usual action-click pattern would silently find
    nothing to act on if this matched that role instead of its own.
    Another GTK-only spelling: UIA maps nothing onto it (a data grid
    there is `table`), and no AX name is mapped onto it either."""
    DOCUMENT_FRAME = "document frame"
    """Windows and Linux only. UIA maps control type 50030 onto it; no AX
    name is mapped, because a Mac's document body is an `AXTextArea`,
    which is `TEXT`. So the portable spelling for "the editable body of a
    document" is `role=(Role.DOCUMENT_FRAME, Role.TEXT, Role.ENTRY)`, and
    `role=DOCUMENT_FRAME` alone matches nothing on macOS."""
    FILLER = "filler"

    # -- things you only read ----------------------------------------------
    LABEL = "label"
    """Static text -- a caption, not something typed into. On Windows this
    is UIA's one Text control type, flatly: measured across four frameworks
    (Win32 STATIC, WPF TextBlock and Label, WinForms Label, UWP TextBlock),
    every one is a caption, while an editable body is `entry` and a
    document is `document frame`. See `Role.TEXT`."""
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    IMAGE = "image"
    ICON = "icon"
    TOOL_TIP = "tool tip"
    """A hovering hint. at-spi says `tool tip` natively, UIA control type
    50022 says the same, and macOS's `AXHelpTag` is mapped onto it (the
    measurement lives beside that entry in `_AX_ROLES`); promoted to a
    constant alongside `MENU_BAR`."""
    PROGRESS_BAR = "progress bar"
    LEVEL_BAR = "level bar"
    """A GTK4 `GtkLevelBar`, measured live (4.24.1) -- a read-only value
    display like `progress bar`, but a distinct role: GTK has no single
    widget that is both, and nothing currently maps the two together."""
    SLIDER = "slider"
    DIAL = "dial"
    SCROLL_BAR = "scroll bar"
    SEPARATOR = "separator"

    WINDOW_ROLES = (FRAME, WINDOW, DIALOG, ALERT, FILE_CHOOSER)
    """Roles that count as a toplevel window when listing windows.

    In precedence order where one title matches several -- a main window
    outranks a dialog of the same name. `ALERT` and `FILE_CHOOSER` are the
    two toplevel roles GTK and Qt publish besides the classic three; see
    each one's own note for the measurement."""

    TEXT_ROLES = (TEXT, ENTRY, PASSWORD_TEXT, SPIN_BUTTON)
    """Roles that accept typed text."""

    CHOICE_ROLES = (COMBO_BOX, LIST, MENU, POPUP_MENU, PAGE_TAB_LIST)
    """Roles that present a set of choices.

    Carries both menu spellings for the reason `MENU_ROLES` exists: GTK
    says `menu`, Qt says `popup menu`, and a caller walking everything
    choosable wants both in the one search."""

    MENU_ROLES = (MENU, POPUP_MENU)
    """The two spellings an opened menu answers to across toolkits.

    GTK publishes `menu`; Qt publishes `popup menu`, submenus included. A
    walk that wants every opened menu asks for both in one search, the
    same way `WINDOW_ROLES` covers every toplevel spelling."""

    MENU_ITEM_ROLES = (MENU_ITEM, CHECK_MENU_ITEM, RADIO_MENU_ITEM)
    """Every role a menu's entry can carry.

    What `choose()` and `options()` search for: an entry that can be ticked
    is a `check menu item`, not a `menu item`, so a lookup keyed on the
    plain role alone walked straight past half a real menu -- a View menu's
    toggles, a format menu's radio group. One search over the three costs
    the same walk as a search for one; see `spellings` for the measurement
    that makes that worth insisting on."""


_ALIASES = (frozenset({"push button", "button"}),)
"""Groups of names that are the same role under different at-spi2 versions.

at-spi2 renamed `ATSPI_ROLE_PUSH_BUTTON` to `ATSPI_ROLE_BUTTON`, keeping the
integer (43) and the old symbol as an alias, and `atspi_role_get_name` follows
the new nick. So the *number* never moved and the *string* did: 2.61.1 reports
"button" and knows no role named "push button" at all, while older versions
report "push button". Measured live on at-spi2-core 2.61.1 (Fedora 45), where
`gui.button()` consequently matched none of gnome-calculator's thirty buttons.

This is not a toolkit or application quirk -- the role is produced by at-spi2
from the enum, so every application on a given version reports the same
spelling whatever it was written in. dogtail met it first and its own
`button()` accepts both, noting they are "represented by the same integer".

Audited against every role name at-spi2 emits -- on 2.61.1 and again on
2.62.1 (132 names) -- this is the only one of `Role`'s constants with no
live counterpart. Add a group here if that ever stops being true. `popup
menu` is deliberately NOT a group with `menu`: both names are live roles in
one at-spi2, reported by different toolkits for the same kind of widget --
see `Role.POPUP_MENU` and `MENU_ROLES`.
"""

_SPELLINGS = {name: group for group in _ALIASES for name in group}

_CANONICAL = {"button": Role.PUSH_BUTTON}
"""The one spelling `canonical_role` normalizes each alias group's other
members to -- `Role`'s own constant, so a script written against
`Role.PUSH_BUTTON` sees exactly that back from `Element.role`, never the
other spelling, whichever this desktop's at-spi2 happens to report.
"""


def canonical_role(raw: str) -> str:
    """`raw`, or the stable spelling it is an alias for.

    What `Element.role` reads a search's own role-spelling matching
    (`spellings`) gives for free but a *read-back* does not: a push button's
    raw `roleName` is "button" on every at-spi2 measured on this project's
    fleet -- 2.56.2, 2.61.1 and 2.62.1 all emit it, so the rename is older
    than 2.56 and the old spelling survives in recordings, in scripts, and
    in dogtail's note that both names are one integer -- so a caller
    comparing `element.role` against `Role.PUSH_BUTTON` -- or
    against a `(name, role)` pair read out of its own test data -- gets a
    version-dependent answer unless something here settles on one spelling.
    Measured live: a driver enumerating every actable control by reading
    `element.role` back (one combined search, not 16 role-by-role ones) saw
    every push button's role come back `"button"` on an at-spi2 that uses the
    new name, which matched none of its own `(name, "push button")` records.
    Roles with no alias group pass through unchanged.
    """
    return _CANONICAL.get(raw, raw)


def spellings(role: str | Sequence[str]) -> frozenset[str]:
    """Every name the accessibility bus might report for `role`.

    Deliberately symmetric: a script that asks for "button" has to match a
    desktop that says "push button" just as much as the other way round, since
    a test written on one machine is run on another, and a recording outlives
    the at-spi2 it was made against.

    `role` may name several roles at once -- `role=("push button", "check
    box")` -- answering the union of each one's own spellings. This is what
    lets `find_elements(role=ACTABLE_ROLES)` ask for every interactive role in
    one search, rather than one search per role: measured live (GhostBSD's
    at-spi2, a ~130-element window), one `find_all_descendants` call over a
    window's subtree costs the same handful of seconds regardless of how many
    roles the predicate accepts or how many elements match it, because the
    cost is the walk itself, not the filter -- so 16 separate role-by-role
    searches pay for 16 full walks of the same subtree where one search
    scoped to all 16 roles pays for one. A caller driving every actable
    control on a page, one role at a time, turned a few-second pass into
    over a minute; see CHANGELOG.
    """
    if isinstance(role, str):
        return _SPELLINGS.get(role, frozenset({role}))
    result: set[str] = set()
    for one in role:
        result |= _SPELLINGS.get(one, frozenset({one}))
    return frozenset(result)
