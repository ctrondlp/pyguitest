"""Accessible-tree dump, the data and rendering `pyguitest inspect` prints.

Split the same way `__main__._debug_data`/`_format_debug` are: one function
builds a JSON-safe structure, the other renders it as text, so `--json` and
the default output can never say two different things about the same tree.

Kept out of `__main__.py` and out of `Session` so later work -- extending
`capture_on_failure`'s failure bundle, accessibility-regression assertions --
can call `tree_data` directly without going through the CLI or growing the
core session API.
"""

import re

from .roles import Role

__all__ = ["tree_data", "format_tree"]


def _node_fields(element):
    """One element's own fields -- role, name, state -- no children.

    `checked`/`selected` are included only when `checkable`/`selectable`
    say the state actually means something for this element -- AT-SPI
    reports checked/selected as real booleans everywhere, unset (False) on
    a plain panel exactly like an unchecked check box, so gating on the
    value itself would not filter anything out. See Element.checked's
    docstring in backends/atspi.py.

    Split from _node_data so a single element can be serialized without its
    whole subtree -- capture_on_failure's focused-element artifact wants
    that; the tree dump below wants the recursive form.
    """
    data = {
        "role": element.role,
        "name": element.name,
        "enabled": element.enabled,
        "visible": element.visible,
    }
    if element.checkable:
        data["checked"] = element.checked
    if element.selectable:
        data["selected"] = element.selected
    if element.actions:
        data["actions"] = element.actions
    return data


def _node_data(element):
    """One element and its descendants, as a JSON-safe recursive dict."""
    data = _node_fields(element)
    data["children"] = [_node_data(child) for child in element.children]
    return data


def tree_data(gui, window=None):
    """Every window's accessible tree, grouped by owning application.

    `window`, given, is a regex matched against window titles the same way
    Session.find_window matches (`pattern.search`), so a busy desktop can be
    scoped to one application's tree.

    Reaches every toplevel the way the AT-SPI backend's own windows() does --
    gui.elements(role=r) for each r in Role.WINDOW_ROLES, then each match's
    parent is the owning application -- rather than through Window.handle,
    which is documented backend-private.
    """
    pattern = re.compile(window) if window is not None else None
    windows_by_app: dict[str, list] = {}
    app_order: list[str] = []
    for role in Role.WINDOW_ROLES:
        for element in gui.elements(role=role):
            if pattern is not None and not pattern.search(element.name or ""):
                continue
            app_name = element.parent.name if element.parent is not None else ""
            if app_name not in windows_by_app:
                windows_by_app[app_name] = []
                app_order.append(app_name)
            windows_by_app[app_name].append(_node_data(element))
    return [
        {"application": name, "windows": windows_by_app[name]} for name in app_order
    ]


def _label(node, top_level):
    """One node's text, e.g. 'Window: Preferences [frame]'.

    Top-level nodes (the windows themselves) get the friendly "Window:"
    label with their real role kept alongside in brackets -- frame/window/
    dialog is an implementation detail most readers do not need to parse a
    tree, but should not be hidden either.
    """
    flags = []
    if not node["enabled"]:
        flags.append("disabled")
    if not node["visible"]:
        flags.append("hidden")
    if "checked" in node:
        flags.append("checked" if node["checked"] else "unchecked")
    if "selected" in node:
        flags.append("selected" if node["selected"] else "unselected")

    if top_level:
        text = f"Window: {node['name']} [{node['role']}]"
    else:
        text = f"{node['role']}: {node['name']}"
    if flags:
        text += " [" + ", ".join(flags) + "]"
    return text


def _format_node(node, prefix, is_last, top_level=False):
    connector = "└── " if is_last else "├── "
    lines = [prefix + connector + _label(node, top_level)]
    extension = "    " if is_last else "│   "
    child_prefix = prefix + extension
    children = node["children"]
    for index, child in enumerate(children):
        lines.extend(_format_node(child, child_prefix, index == len(children) - 1))
    return lines


def format_tree(data):
    """Render `tree_data`'s output as an indented tree.

    enabled/visible are called out only when False -- most elements are
    both, and a page of "[enabled, visible]" on every line would bury the
    exceptions that actually matter to a reader.
    """
    lines = []
    for index, app in enumerate(data):
        if index:
            lines.append("")
        lines.append(f"Application: {app['application'] or '(unnamed)'}")
        windows = app["windows"]
        for w_index, window in enumerate(windows):
            lines.extend(
                _format_node(
                    window, "", w_index == len(windows) - 1, top_level=True
                )
            )
    return "\n".join(lines)
