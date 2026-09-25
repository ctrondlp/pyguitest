# Agent instructions

*Regarding AI, adopt or get left behind...*

Working conventions for this repo, learned from the maintainer (Dennis K.
Paulsden) over prior sessions. This is about *how* to work here, not *what
the code does* — read the source and `docs/` for that.

## Git workflow

- **AI will never commit.** Never run `git commit` (or `git add` in
  service of one), no exceptions. The user commits their own work, always.
  Make changes, verify them, and leave them in the working tree.
- **Never work on `main`.** Every change goes on a feature branch named
  `P<priority>-<short-kebab-description>` (e.g. `P2-window-title-collision-
  topmost-match`). If no priority number was given, ask for one before
  starting.
- **Any mention of "commit" from the user — "commit this," "one line
  commit for each," anything — means produce the message text for them to
  use, never run the command.** One line, Conventional Commits style
  (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, …), no body.
- **A multi-repo or multi-file plan approved once is not standing
  approval for the rest of it.** Do one repo (or one file, for something
  like a README pass), stop, report what changed, and ask before doing the
  next. This applies doubly to editing `README.md` specifically — ask
  first even within one approved plan.

## Verifying a change

`./scripts/pre-commit-test.sh` is the authoritative gate — tests (pytest),
ruff lint, ruff format check, and mypy, over `src tests examples scripts`.
It mirrors `.github/workflows/ci.yml`, so a green run here is what CI means.
A bare `ruff format --check .` also flags pre-existing formatting in
`docs/*.md` that the real gate does not check and does not care about —
don't chase that.

Fix lint/format/type issues right the first time rather than looping
edit-check-edit-check; run the full gate once near the end of a change, not
after every edit.

## Docs and changelog

`CHANGELOG.md`'s `[Unreleased]` section gets an entry for any user-facing
fix or feature, in the same voice as the existing entries (a bold one-line
summary, then the story: what broke, how it was found, what changed).
`docs/api.md` is generated — run `python3 scripts/gen-api-docs.py` after
touching a public docstring; `test_api_docs.py` enforces it stays in sync.

**Cutting a version.** Bumping `__version__` in `src/pyguitest/__init__.py`
and retitling `## [Unreleased]` to `## [x.y.z] — <date>` is itself ordinary
fix/feature work here, not a separate release ceremony gated behind
something more formal — match the semver precedent in `CHANGELOG.md`'s
history (new public API is a minor bump). Do this whenever a change adds
enough shape to `[Unreleased]` to warrant it, particularly when
`pyguitest-recorder`'s version floor needs a real released version to point
at (see "This repo's place in the family").

## Testing against a real desktop

If a task involves actually running pyguitest against a live GUI (not just
unit tests with mocks/fakes), don't launch or raise real windows on the
user's own desktop without asking first — this includes recording/replaying
`pyguitest-recorder` scripts, which move the real mouse and click.

**Windows.** If testing against a live Windows box, note that a plain SSH
shell typically lands in Session 0 (services), which cannot see, let alone
drive, the interactive desktop — see `docs/validation.md`'s own account of
this. Reaching the real console session generally requires a one-shot
scheduled task run with the `/it` flag (`schtasks /create ... /it /tr "..."`,
then `schtasks /run`), polling the script's own log file rather than waiting
on the SSH command itself, which returns long before the task does.

**`SetWinEventHook`'s `WINEVENT_SKIPOWNPROCESS` flag matters for live
checks.** A window created in the same Python process that installs the hook
(e.g. via `pyguitest.connect()` in a probe script) never fires hook events
for itself — that flag in `win32.py` is deliberate. A live check of
`window_events()`/`wait_for_window()` needs the windows under test owned by a
*separate* process (spawn one with `subprocess.Popen`, coordinate with a file
or a small IPC mechanism); testing hook-driven behavior same-process silently
tests nothing, and looks like a hang or a false failure rather than a clear
error about why.

## This repo's place in the family

`pyguitest-recorder` (a sibling checkout) generates scripts against this
package's public API and pins a version floor to it in its own
`pyproject.toml`. Adding or changing public API here — a new `Element`
property or method, a new `Session` call, anything `docs/api.md` lists —
means checking whether the recorder's generator, resolver, or model reads it,
and if so bumping its floor and adding its own changelog entry once this
package's version reflects the change. Don't consider that side's work done
without checking; see this file's own git-workflow note on scope.
