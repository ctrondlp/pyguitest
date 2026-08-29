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
"""

__all__ = ["Role"]


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
