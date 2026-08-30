# pyguitest documentation

pyguitest is cross-platform GUI automation for Python, the successor to
X11::GUITest. For what it is,
how to install it, and the API itself, start at the
[project README](../README.md) — this folder is the detail that would
otherwise drown it.

| File | What's in it |
|------|---------------|
| [install.md](install.md) | What each backend needs, per distribution — the companion to `pyguitest doctor` |
| [input.md](input.md) | Injecting pointer and keyboard input: permissions, keymap safety, libei, the portal |
| [structure.md](structure.md) | The repository layout, file by file |
| [validation.md](validation.md) | What has actually been run against a real desktop, and what has not |
| [wayland-audit.html](wayland-audit.html) | The audit of all 50 X11::GUITest exports that this project's API design derives from |
| [adr-001-dependencies.md](adr-001-dependencies.md) | Why libraries were chosen as they were |
| [adr-002-transports.md](adr-002-transports.md) | Why sockets replaced CLI tools for the compositor IPC backends |

Reach for the ADRs when a design choice looks arbitrary and you want the
reasoning; reach for `validation.md` before trusting a claim about what
works on your desktop specifically; reach for everything else when you
already know which piece you need more detail on.

The GNOME Shell extension has its own docs at
[gnome-shell-extension/README.md](../gnome-shell-extension/README.md).
