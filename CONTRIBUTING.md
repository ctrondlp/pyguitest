# Contributing to pyguitest

Everything here is about working *on* pyguitest. For using it, see
[README.md](README.md); for how the pieces fit together, see
[docs/structure.md](docs/structure.md) and the two ADRs beside it.

Requires Python 3.10 or newer — 3.9 reached end-of-life in October 2025.

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
