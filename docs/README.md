# pyguitest documentation

pyguitest is cross-platform GUI automation for Python, the successor to
X11::GUITest. For what it is and how to install it, start at the
[project README](../README.md).

## Start here

| File | What's in it |
|------|---------------|
| [getting-started.md](getting-started.md) | Five minutes from nothing to a script that drives a real application — plus which API to reach for, and what X11 / Wayland / XWayland each change |
| [recipes.md](recipes.md) | Task-shaped answers: waiting properly, forms, windows, screenshots, clipboard, CI, and an X11::GUITest cheat sheet |
| [troubleshooting.md](troubleshooting.md) | Symptom first: nothing is found, nothing is typed, nothing is captured |

## Reference

| File | What's in it |
|------|---------------|
| [api.md](api.md) | Every public class, method and enum, with the capability each one requires. Generated from the source |
| [install.md](install.md) | What each backend needs, per distribution — the companion to `pyguitest doctor` |
| [input.md](input.md) | Injecting pointer and keyboard input: permissions, keymap safety, libei, the portal |
| [validation.md](validation.md) | What has actually been run against a real desktop, and what has not |
| [ai-assistants.md](ai-assistants.md) | Rules for a coding assistant generating pyguitest code |

Reach for `validation.md` before trusting a claim about what works on your
desktop specifically — it is the record of what was run, where, and what
broke.

## Design and internals

[developers/](developers/) holds the rationale and the internals: why the API
is not a port, the audit it derives from, the two ADRs, the repository
structure, and the protocol gaps worth taking upstream. None of it is needed
to use the library.

Working *on* pyguitest rather than with it: [CONTRIBUTING.md](../CONTRIBUTING.md).
The GNOME Shell extension has its own docs at
[gnome-shell-extension/README.md](../gnome-shell-extension/README.md).
