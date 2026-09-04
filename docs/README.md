# pyguitest documentation

pyguitest is cross-platform GUI automation for Python, the successor to
X11::GUITest. For what it is
and how to install it, start at the
[project README](../README.md) — this folder is the detail that would
otherwise drown it.

| File | What's in it |
|------|---------------|
| [api.md](api.md) | Every public class, method and enum, with the capability each one requires |
| [design.md](design.md) | Why the API is not a one-to-one port of X11::GUITest, and the decisions that follow |
| [install.md](install.md) | What each backend needs, per distribution — the companion to `pyguitest doctor` |
| [input.md](input.md) | Injecting pointer and keyboard input: permissions, keymap safety, libei, the portal |
| [structure.md](structure.md) | The repository layout, file by file |
| [validation.md](validation.md) | What has actually been run against a real desktop, and what has not |
| [wayland-audit.md](wayland-audit.md) | The audit of all 50 X11::GUITest exports that this project's API design derives from |
| [upstream.md](upstream.md) | The two protocol gaps worth filing upstream, written as issue text |
| [adr-001-dependencies.md](adr-001-dependencies.md) | Why libraries were chosen as they were |
| [adr-002-transports.md](adr-002-transports.md) | Why sockets replaced CLI tools for the compositor IPC backends |

Reach for the ADRs when a design choice looks arbitrary and you want the
reasoning; reach for `validation.md` before trusting a claim about what
works on your desktop specifically; reach for everything else when you
already know which piece you need more detail on.

The GNOME Shell extension has its own docs at
[gnome-shell-extension/README.md](../gnome-shell-extension/README.md).
