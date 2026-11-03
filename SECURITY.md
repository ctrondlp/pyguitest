# Security

## Reporting a vulnerability

Report privately through GitHub's private vulnerability reporting:
**[open a draft advisory](https://github.com/ctrondlp/pyguitest/security/advisories/new)**.
That keeps the report between you and the maintainer until there is a fix to
publish. Please do not open a public issue for something you believe is
exploitable.

A useful report says what an attacker starts with (an unprivileged local
process? a sandboxed application? a file the user was persuaded to open?),
what they end up able to do, and which of the two is more than they had
before. Output from `pyguitest debug` identifies the desktop, the session
type and the selected backends, which is usually the context that matters.

Expect an acknowledgement within a week. This is a single-maintainer project,
so a fix for anything involving a live compositor may wait on a machine that
can reproduce it; the advisory will say so rather than going quiet.

## What this project is

`pyguitest` drives a desktop: it injects keystrokes and pointer events, reads
accessibility trees, enumerates and moves windows, and captures screenshots.
Those capabilities are the product. A report that amounts to "this library can
observe or control other applications" describes the intended function, not a
vulnerability.

The security question is narrower, and it is the one worth your time and
mine: **can a capability be obtained, kept, or widened beyond the
authorization it was granted under?** Concretely, these are in scope:

- Acquiring input injection, capture, or accessibility access without the
  consent step the platform requires for it — a portal prompt that is
  bypassed, a grant that is reused after it should have lapsed, a backend
  selected automatically that is documented as opt-in.
- Crossing a sandbox boundary: a confined application (Flatpak, Snap) reaching
  a privileged operation through this package or its GNOME Shell extension
  that it could not perform itself.
- Writing, reading, or deleting a file outside what the caller could already
  reach, including through a path supplied to a more privileged component.
- Leaking a credential this project holds: a portal restore token, clipboard
  contents, or captured pixels reaching a log, a temporary file with
  permissive modes, or a process that should not see them.
- Privilege escalation of any kind, or remote reachability of an interface
  intended to be local.
- Code execution from data that is merely *read* — a recording, an image, a
  configuration file — rather than from a script the user chose to run.

## Known exposures, documented on purpose

These are deliberate design decisions with their reasoning written down. They
are not secrets and they are not open reports; please read the linked note
before filing one.

- **The GNOME Shell extension removes the screenshot consent prompt.** While
  it is enabled, anything that can reach your session bus can capture any
  window with no prompt and no record, and can have the resulting PNG written
  to any path you can write. Both are stated plainly in
  [the extension's README](gnome-shell-extension/README.md#what-installing-this-means),
  along with the advice to enable it on a test machine and disable it
  otherwise. The extension is opt-in and installed by hand.

  Sandbox reachability was measured rather than assumed (2026-11-02, GNOME
  Shell 51.1): a Flatpak app is **blocked** by default and with an ordinary
  portal-only `--talk-name`, and **reachable** only with
  `--talk-name=org.gnome.Shell` or `--socket=session-bus` — permissions
  Flatpak itself treats as sandbox-weakening. In those two cases a confined
  app did escape its filesystem view. A report that a *default* or
  portal-only sandbox can reach this interface would be a new finding and is
  in scope.
- **Consent-based backends are opt-in and stay that way.** `connect()` never
  selects a backend that would raise a consent dialog; naming one is how you
  ask for it. A report that automatic selection *does* reach one is in scope.
- **Restore tokens are never persisted for you.** Keeping one across runs is
  the caller's decision, and the token is a credential — see
  [docs/input.md](docs/input.md).
- **`run_command()` on the sway IPC transports passes a command straight to
  the compositor by design** — `SwaySocket` and `SwayCLI` in
  `src/pyguitest/ipc.py`. It is not in `__all__`, but it is reachable;
  handing it an untrusted string is the caller's bug, the same as with
  `subprocess`.

The companion [`pyguitest-recorder`](https://github.com/ctrondlp/pyguitest-recorder)
observes input desktop-wide — that is what recording is — and its saved
sessions are credential-bearing artifacts. Report recorder issues on that
repository.

## Supported versions

Fixes land on the latest release line. There are no long-term support
branches: if you are more than a minor behind, the first question will be
whether the current release still has the problem.
