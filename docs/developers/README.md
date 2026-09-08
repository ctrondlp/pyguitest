# Design and internals

Why pyguitest is shaped the way it is, and how it works inside. Nothing here
is needed to *use* the library — start at [getting-started.md](../getting-started.md)
for that, or [CONTRIBUTING.md](../../CONTRIBUTING.md) to work on the package
itself.

| File | What's in it |
|------|---------------|
| [design.md](design.md) | Why the API is not a one-to-one port of X11::GUITest, and the decisions that follow |
| [structure.md](structure.md) | The repository layout, how a call flows through the layers, the backend registry, and what adding a backend involves |
| [wayland-audit.md](wayland-audit.md) | The audit of all 50 X11::GUITest exports that the API design derives from |
| [adr-001-dependencies.md](adr-001-dependencies.md) | Why these libraries, and why so few |
| [adr-002-transports.md](adr-002-transports.md) | Why sockets replaced CLI tools for the compositor IPC backends |
| [upstream.md](upstream.md) | The two Wayland protocol gaps worth filing upstream, written as issue text |

Reach for the ADRs when a design choice looks arbitrary and you want the
reasoning behind it. Reach for `wayland-audit.md` when you want to know why a
capability sits in the tier it does — that classification is the origin of
the whole capability model.

The GNOME Shell extension has its own docs at
[gnome-shell-extension/README.md](../../gnome-shell-extension/README.md).
