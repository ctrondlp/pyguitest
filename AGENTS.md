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

## Testing against a real desktop

If a task involves actually running pyguitest against a live GUI (not just
unit tests with mocks/fakes), don't launch or raise real windows on the
user's own desktop without asking first — this includes recording/replaying
`pyguitest-recorder` scripts, which move the real mouse and click.
