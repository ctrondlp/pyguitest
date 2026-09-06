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

__all__ = ["Role", "spellings"]


class Role:
    """Accessible role names, grouped by what you would look for."""

    # -- things you press --------------------------------------------------
    PUSH_BUTTON = "push button"
    TOGGLE_BUTTON = "toggle button"
    CHECK_BOX = "check box"
    RADIO_BUTTON = "radio button"
    LINK = "link"

    # -- things you type into ----------------------------------------------
    TEXT = "text"
    ENTRY = "entry"
    PASSWORD_TEXT = "password text"
    SPIN_BUTTON = "spin button"

    # -- things you choose from --------------------------------------------
    COMBO_BOX = "combo box"
    LIST = "list"
    LIST_ITEM = "list item"
    MENU = "menu"
    MENU_ITEM = "menu item"
    CHECK_MENU_ITEM = "check menu item"
    RADIO_MENU_ITEM = "radio menu item"
    PAGE_TAB = "page tab"
    PAGE_TAB_LIST = "page tab list"

    # -- things that hold other things -------------------------------------
    FRAME = "frame"
    WINDOW = "window"
    DIALOG = "dialog"
    PANEL = "panel"
    TOOL_BAR = "tool bar"
    STATUS_BAR = "status bar"
    SCROLL_PANE = "scroll pane"
    VIEWPORT = "viewport"
    TABLE = "table"
    TABLE_CELL = "table cell"
    TABLE_ROW = "table row"
    TREE = "tree"
    TREE_ITEM = "tree item"
    DOCUMENT_FRAME = "document frame"

    # -- things you only read ----------------------------------------------
    LABEL = "label"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    IMAGE = "image"
    ICON = "icon"
    PROGRESS_BAR = "progress bar"
    SLIDER = "slider"
    SCROLL_BAR = "scroll bar"
    SEPARATOR = "separator"

    WINDOW_ROLES = (FRAME, WINDOW, DIALOG)
    """Roles that count as a toplevel window when listing windows."""

    TEXT_ROLES = (TEXT, ENTRY, PASSWORD_TEXT, SPIN_BUTTON)
    """Roles that accept typed text."""

    CHOICE_ROLES = (COMBO_BOX, LIST, MENU, PAGE_TAB_LIST)
    """Roles that present a set of choices."""


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

Audited against every role name at-spi2 2.61.1 emits: this is the only one of
`Role`'s constants with no live counterpart. Add a group here if that ever
stops being true.
"""

_SPELLINGS = {name: group for group in _ALIASES for name in group}


def spellings(role: str) -> frozenset[str]:
    """Every name the accessibility bus might report for `role`.

    Deliberately symmetric: a script that asks for "button" has to match a
    desktop that says "push button" just as much as the other way round, since
    a test written on one machine is run on another, and a recording outlives
    the at-spi2 it was made against.
    """
    return _SPELLINGS.get(role, frozenset({role}))
