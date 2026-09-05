# Contributing to pyguitest

Everything here is about working *on* pyguitest. For using it, see
[README.md](README.md); for how the pieces fit together, see
[docs/structure.md](docs/structure.md) and the two ADRs beside it.

Requires Python 3.10 or newer — 3.9 reached end-of-life in October 2025.

## Filing a bug

Run `pyguitest debug` (or `python3 -m pyguitest debug` from a checkout) and
paste its output into the issue. It reports the package and Python versions,
every environment probe, each detected tool's own `--version`, and whether
the process is running inside a Flatpak, toolbox, or other container --
which changes what every other line in that output actually describes.
Add `--json` if you'd rather attach a file than paste text.

## Setting up

```sh
pip install -e '.[dev]'      # editable, plus pytest, ruff and mypy
pre-commit install
```

`-e` links to the source tree instead of installing a copy, which is what you
want here and *not* what a user of the package wants.

## Running the tests

```sh
PYTHONPATH=src python3 -m unittest discover -s tests   # unit tests, no deps
```

Really no deps: the suite passes on a machine with nothing installed and no
capture or input tool on `PATH`. That is not a convenience, it is the claim
under test — every display-server mechanism is probed at runtime and stands
in as a fake here, so a stray unconditional `import` in a module that must
stay importable without the optional extras shows up as a failure.

## The D-Bus tests

More live in `tests/test_portal_dbusmock.py` and count separately -- see
[Avoiding repeat consent dialogs](docs/input.md#avoiding-repeat-consent-dialogs)
for what they verify. `pip install '.[dev]'` alone is *not* enough for them:
`dbus-python` needs compiling and `dbus-daemon` is a binary, so both come
from the distribution. On Fedora:

```sh
sudo dnf install python3-dbusmock python3-dbus dbus-daemon
PYTHONPATH=src python3 -m unittest discover -s tests \
    -p test_portal_dbusmock.py -v
```

Use `discover -p`, not `unittest tests.test_portal_dbusmock`: there is no
`tests/__init__.py`, and while Python 3.13 resolves that dotted name as a
namespace package, 3.14 does not -- it fails with `ModuleNotFoundError`,
which reads like a missing file rather than a bad invocation.

**Check they actually ran.** These skip themselves rather than failing when
a prerequisite is missing, and a skipped run still prints `OK` -- so an
incomplete install looks exactly like a passing one. `-v` shows `... ok`
per test, and the skip reason names whichever piece is missing. To assert
it in a script:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests \
    -p test_portal_dbusmock.py -v 2>&1 | grep -cE '\.\.\. ok$'
# 0 means it skipped (or failed to load), not passed
```

That distinction is not pedantry: a permanently-skipped test in this repo
went on hiding a constructor signature that no longer existed.

## The headless GNOME session

Window control, window capture and window events need a live compositor, so
none of them can be covered by the unit tests above. `headless-session.sh`
runs a command inside a private `gnome-shell --headless` on its own session
bus — nothing appears on screen, and a shell already running is untouched:

```sh
./scripts/headless-session.sh ./scripts/validate-gnome-extension.sh
```

That is green end to end with nobody watching, and it is the closest thing
this project has to a compositor-tier regression test. Anything else works
too:

```sh
./scripts/headless-session.sh pyguitest doctor
./scripts/headless-session.sh python3 examples/01_what_can_i_do.py
```

Needs `gnome-shell` (40 or newer, for `--headless`), `dbus-run-session` and
`gdbus`. The window-control checks also want the
`pyguitest-window-control` extension installed and enabled for your user —
the same install as on a real session, since the headless shell reads the
same `~/.local/share/gnome-shell/extensions`.

What it does not cover: input actually reaching a client, and anything
needing the portal's consent dialog, which has nobody to click it.

## Lint, format, types

All configured in `pyproject.toml`:

```sh
ruff check src tests      # lint  (pycodestyle, pyflakes, pydocstyle, bugbear…)
ruff format src tests     # format
mypy                      # type check
```

`pre-commit install` runs lint and format on every commit. The tree is
currently clean under all three. The package ships `py.typed` (PEP 561), so
annotations are visible to your editor and type checker.

## Continuous integration

[CI](.github/workflows/ci.yml) runs the suite on Python 3.10 through 3.14,
plus `ruff check`, `ruff format --check` and `mypy` on the 3.10 floor.
Almost nothing in this package can be exercised against the real thing
automatically — there is no compositor, no session bus, no X server, no
consent dialog anyone can click — so the tests drive stand-ins for
python-xlib, Gio and the portal, and running them everywhere is the cheapest
guard against those stand-ins drifting from what they imitate. The one
exception is the portal job, which installs `python3-dbusmock` and
`dbus-daemon` and negotiates against a real private session bus; it fails if
those tests *skip*, since a green job that proved nothing is worse than a
red one.

"No compositor" is no longer true. A `compositor` job runs the GNOME Shell
extension validation inside `headless-session.sh` — the whole COMPOSITOR
tier, window control, capture and events, with nobody watching. It runs on
pushes to main, on a nightly schedule and on demand, but not on pull
requests, where five minutes of `apt` would not earn its keep.

Worth knowing when you read a result: **the runner is GNOME Shell 46 and a
Fedora desktop is 51**, and the extension's two capture paths are split by
exactly that version — `_captureLegacy` uses an API Mutter 51 removed. So
CI and your desktop execute *different code*, and neither can run the
other's. That is how the legacy path's uncropped capture was found after
months as an unconfirmed comment.

## Documentation is tested too

`tests/test_docs.py` checks the prose against the code: every extra declared
in `pyproject.toml` must be mentioned in the README or a page it links,
every externally discovered tool must be listed in `docs/install.md` with its
constraints (*X11 only*, *wlroots only*), the no-dependencies claim must match
the metadata, and every
script in `examples/` must appear in `examples/README.md`. Adding a tool or an
extra without documenting it fails the suite.

## Where things live

The annotated file tree is in
[docs/structure.md](docs/structure.md#layout), along with how a call flows
through the layers, the backend registry and its opt-in gating, and
[what adding a backend involves](docs/structure.md#adding-a-backend).
Why the dependencies and transports are what they are:
[ADR 001](docs/adr-001-dependencies.md), [ADR 002](docs/adr-002-transports.md).

## Releasing

Publishing to PyPI is automated and tag-driven. There are no GitHub Releases;
a plain annotated tag is the record, and [CHANGELOG.md](CHANGELOG.md) is the
release notes.

The version lives in exactly one place, `src/pyguitest/__init__.py`.
`pyproject.toml` reads it from there via `[tool.setuptools.dynamic]`, so the
wheel, `pyguitest --version` and `pyguitest.__version__` cannot drift apart.

pyguitest is on PyPI: https://pypi.org/project/pyguitest/

### One-time setup

This is already done for this repository — recorded here for reference, or
in case Trusted Publishing ever needs to be re-registered (e.g. after a repo
rename or transfer).

**On PyPI** — the project registered a Trusted Publisher rather than
uploading via API token:

1. pypi.org → *Your account* → *Publishing* → *Add a new pending publisher*
   (or, once the project exists, *Manage* → *Publishing* on the project
   itself)
2. PyPI Project Name `pyguitest`, Owner `ctrondlp`, Repository `pyguitest`,
   Workflow name `ci.yml`, Environment name `pypi`.

That is the whole credential story. Trusted Publishing (OIDC) mints a
short-lived token for exactly this repository, workflow file and environment,
so there is no API token in repository secrets to leak or rotate. The four
fields above must match the workflow exactly or the publish is rejected.

**On GitHub** — *Settings* → *Environments* → *New environment* → `pypi`.
Adding yourself as a required reviewer on it is worth doing: it makes each
publish a deliberate approval rather than a side effect of pushing a tag,
and a PyPI version number can never be reused or reverted.

### Cutting a release

```sh
# 1. Bump the single source of truth and record the change.
$EDITOR src/pyguitest/__init__.py   # __version__ = "0.2.0"
$EDITOR CHANGELOG.md                # move Unreleased -> 0.2.0, add the links
git commit -am "Release 0.2.0"
git push

# 2. Tag. The tag must match __version__ or the build job fails on purpose.
git tag -a v0.2.0 -m "pyguitest 0.2.0"
git push origin v0.2.0
```

The tag push runs the full suite, then `build`, then `publish`. `build` is
where the release-specific guards live: the tag is checked against the version
in the built artifact, `twine check --strict` catches a README that renders on
GitHub but not on PyPI, and the sdist is unpacked and its test suite run — the
sdist ships `tests/`, and `tests/test_docs.py` reads `docs/`, so `MANIFEST.in`
has to keep including them for a distro packager to be able to build and test
from it. `publish` uploads the exact artifact `build` checked, never a rebuild.

Nothing publishes on a branch push: `publish` is gated on `refs/tags/v*`.

## Generated documentation

`docs/api.md` is generated, not written. Its capability column comes from
`CompositeBackend._DISPATCH` and its provider column from each backend's
`capabilities` property, so it changes whenever a backend or a Session
method does:

```sh
python3 scripts/gen-api-docs.py
```

Edit the docstrings and regenerate; edits to `docs/api.md` itself are
overwritten. `tests/test_api_docs.py` fails when the committed file no
longer matches the source, so a stale reference cannot reach main.
