#!/usr/bin/env bash
# Run pyguitest's full check suite -- tests, ruff lint, ruff format, mypy --
# and report a pass/fail summary. Intended as the gate to clear before
# committing.
#
# The command list mirrors .github/workflows/ci.yml rather than inventing a
# house style, so a green run here means what CI means. One deliberate
# deviation: CI runs `python -m unittest discover`, because the package
# declares no dependencies and running the suite bare is itself the claim
# under test. Locally pytest is installed and pyproject.toml configures it
# (testpaths, pythonpath), so this uses pytest -- it collects the same
# unittest classes and gives better failure output.
#
# Complements .pre-commit-config.yaml rather than repeating it. That config
# runs ruff and ruff-format on the *staged* files and rewrites them
# (`--fix`); this runs the whole tree read-only and adds the two gates it
# has no hook for: the test suite and mypy. Nothing here is fixed or
# written -- failures are reported, never repaired.
#
# Of CI's seven jobs, the two a bare checkout can run -- `tests` and `lint` --
# are the four checks above. Three more are runnable here and are behind
# --full, because each needs something a laptop may not have. A missing tool
# is reported as SKIP rather than a pass, never a failure, so --full is safe
# to run anywhere:
#
#   imagemagick  tests/test_imagesearch.py against real ImageMagick. The mock
#                used everywhere else checks the command line and the output
#                parsing, never that the numbers mean what locate() claims.
#   portal       test_portal_dbusmock.py against a real session bus -- the one
#                backend that can be exercised for real. dbus-python and
#                dbusmock come from the distro, not from pip.
#   build        `python -m build`, `twine check --strict`, and the suite run
#                again out of the unpacked sdist -- the only check anywhere
#                that catches docs/ falling out of MANIFEST.in.
#
# The rest of CI is not here at all, deliberately:
#
#   compositor   installs GNOME Shell, enables the window-control extension
#                and runs the validation inside a headless Mutter. That is
#                minutes of apt and a shell of its own, not something to do
#                to the machine whose desktop you are sitting at. Run
#                scripts/headless-session.sh over
#                scripts/validate-gnome-extension.sh by hand when the
#                extension or the window-control tier is what you changed.
#   publish      runs on a v* tag and uploads to PyPI. Not a gate.
#
# Note it checks the working tree, not the index. If you have unstaged
# changes, that is not what `git commit` is about to record.
#
# Usage:
#   ./scripts/pre-commit-test.sh             run everything
#   ./scripts/pre-commit-test.sh -k mypy     only checks whose name matches (repeatable)
#   ./scripts/pre-commit-test.sh -v          stream each check's output as it runs
#   ./scripts/pre-commit-test.sh -q          summary only; do not dump failure logs
#   ./scripts/pre-commit-test.sh -x          stop at the first failure
#   ./scripts/pre-commit-test.sh --full      add the ImageMagick, session-bus
#                                            and build checks above
#
# Exit status: 0 all passed (a SKIP is not a pass, but is not a failure
# either), 1 one or more checks failed, 2 setup problem.

set -uo pipefail

# Repo root, not this script's own directory (scripts/): every check
# below runs `cd "$ROOT"`, and pyproject.toml/src/tests live one level up.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
RUFF="${RUFF:-ruff}"

# name|command. Run from the repository root, via eval, so the quoting here
# is ordinary shell quoting.
CHECKS=(
    "tests|$PYTHON -m pytest -q"
    "ruff|$RUFF check src tests examples scripts"
    # `docs` is here because CI checks it: ruff formats the python blocks
    # inside the markdown, and a snippet reformatted there fails the same
    # job as a source file would. It was missing, so this gate could pass
    # on a branch whose only change was to a doc snippet and CI then fail
    # on exactly that -- which is what happened. Lint deliberately does
    # *not* cover docs, for the reason CI's own comment gives: snippets
    # are fragments and name things they never import.
    "format|$RUFF format --check src tests examples scripts docs"
    "mypy|$PYTHON -m mypy"
)

# name|command|guard|needs -- only with --full. The command is the CI job of
# the same name, with one deviation: this writes nothing into the tree, where
# the build job's `python -m build` fills dist/.
#
# The guard answers "are the tools here?" and is what turns a missing
# ImageMagick or an interpreter without dbusmock into a SKIP instead of a
# failure -- a laptop is allowed to be missing what a runner installs. `needs`
# is what to name in the SKIP line.
FULL_CHECKS=(
    "imagemagick|check_imagemagick|needs_imagemagick|ImageMagick"
    "portal|check_portal|needs_portal|dbus-python, dbusmock and PyGObject on a session bus"
    "build|check_build|needs_build|the build and twine packages"
)

# ---------------------------------------------------------------- arguments

verbose=0
quiet=0
fail_fast=0
full=0
want_checks=()

# The usage text is this script's own header: every comment line from line 2
# down to the first line that is not one. Extracted by pattern rather than by
# line number, so adding a paragraph up there cannot silently truncate --help
# -- or leak the `set` line below into it.
usage() {
    awk 'NR > 1 && !/^#/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
}

while (($#)); do
    case "$1" in
        -k|--check)   [[ ${2:-} ]] || { echo "-k needs a value" >&2; exit 2; }
                      want_checks+=("$2"); shift 2 ;;
        -v|--verbose) verbose=1; shift ;;
        -q|--quiet)   quiet=1; shift ;;
        -x|--fail-fast) fail_fast=1; shift ;;
        --full)       full=1; shift ;;
        -h|--help)    usage; exit 0 ;;
        *) echo "pre-commit-test.sh: unknown option '$1' (try --help)" >&2; exit 2 ;;
    esac
done

# ------------------------------------------------------------------ display

if [[ -t 1 ]]; then
    TTY=1
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
    YELLOW=$'\033[33m'; RESET=$'\033[0m'
else
    TTY=0
    BOLD=; DIM=; RED=; GREEN=; YELLOW=; RESET=
fi

now() { date +%s.%N; }

# ----------------------------------------------------------------- preflight

matches() {  # matches <needle-array-name> <value>; empty array matches all
    local -n _pats="$1"; local val="$2" pat
    ((${#_pats[@]} == 0)) && return 0
    for pat in "${_pats[@]}"; do [[ $val == *"$pat"* ]] && return 0; done
    return 1
}

[[ -f "$ROOT/pyproject.toml" ]] || {
    echo "pre-commit-test.sh: $ROOT does not look like the pyguitest repo" >&2
    exit 2
}

missing=()
command -v "$PYTHON" >/dev/null || missing+=("$PYTHON")
command -v "$RUFF" >/dev/null || missing+=("$RUFF")
"$PYTHON" -c 'import pytest' 2>/dev/null || missing+=("pytest (for $PYTHON)")
"$PYTHON" -c 'import mypy' 2>/dev/null || missing+=("mypy (for $PYTHON)")
if ((${#missing[@]})); then
    printf '%spre-commit-test.sh: not installed:%s %s\n' "$RED" "$RESET" "${missing[*]}" >&2
    echo "install with: pip install --user pytest ruff mypy" >&2
    exit 2
fi

LOGDIR="$(mktemp -d "${TMPDIR:-/tmp}/pyguitest-checks.XXXXXX")"

# --------------------------------------------------------------- full checks
#
# Each is a CI job that is a job of its own there, because the thing it checks
# cannot be faked: a mocked test of real ImageMagick, or of a real D-Bus
# service, proves nothing about either.

# ImageMagick 7 renamed the binary; 6's `convert` is still what ships on many
# distributions, and the tests accept either.
needs_imagemagick() { command -v magick >/dev/null || command -v convert >/dev/null; }

needs_portal() {
    "$PYTHON" -c 'import dbus, dbusmock, gi' 2>/dev/null || return 1
    # The tests put a mock service on a session bus. CI's runner has one; a
    # shell on a developer's machine may not, and dbus-run-session is how the
    # sibling repo's live check gets its own. With neither, this is a missing
    # tool rather than a failing check.
    [[ ${DBUS_SESSION_BUS_ADDRESS:-} ]] || command -v dbus-run-session >/dev/null
}

needs_build() { "$PYTHON" -c 'import build, twine' 2>/dev/null; }

# Run a suite that skips *itself* when its backend is missing, and fail if it
# did. Skipping is right on a laptop and wrong here: CI's imagemagick and
# portal jobs exist for the case where nothing was installed for them, and a
# green run that silently proved nothing is worse than a red one. This is
# CI's `grep -q skipped`, adapted to a script that cannot use `::error::`.
must_not_skip() {
    local out status=0
    out="$("$@" 2>&1)" || status=$?
    printf '%s\n' "$out"
    ((status)) && return "$status"
    if grep -q 'skipped' <<<"$out"; then
        echo "the tests skipped themselves, so this check proved nothing"
        return 1
    fi
    return 0
}

# `unittest discover -p`, never the dotted module name: there is no
# tests/__init__.py, and 3.14 refuses to resolve the name -- a
# ModuleNotFoundError that reads like a missing file rather than a bad
# invocation. Both jobs in CI use -p for the same reason.
check_imagemagick() {
    must_not_skip env PYTHONPATH=src "$PYTHON" -m unittest discover \
        -s tests -p test_imagesearch.py -v
}

check_portal() {
    local -a runner=("$PYTHON")
    [[ ${DBUS_SESSION_BUS_ADDRESS:-} ]] || runner=(dbus-run-session -- "$PYTHON")
    must_not_skip env PYTHONPATH=src "${runner[@]}" -m unittest discover \
        -s tests -p test_portal_dbusmock.py -v
}

# CI's build job, minus the artifact upload. Everything happens in temporary
# directories rather than in dist/, so a release artifact already sitting in
# the tree is neither clobbered nor mistaken for this run's output.
check_build() {
    local dist outer status=0
    dist="$(mktemp -d "${TMPDIR:-/tmp}/pyguitest-build.XXXXXX")" || return 1
    outer="$(mktemp -d "${TMPDIR:-/tmp}/pyguitest-sdist.XXXXXX")" || {
        rm -rf "$dist"
        return 1
    }

    # A subshell under `set -e`, so the first failing step is where it stops --
    # the shape CI has, where each step is its own `run:`. Both temporary
    # directories are removed on the way out either way.
    (
        set -e
        "$PYTHON" -m build --outdir "$dist"
        "$PYTHON" -m twine check --strict "$dist"/*

        # CI's tag check, which only means anything when HEAD *is* a tag. A
        # PyPI version number can never be reused, so a tag disagreeing with
        # the packaged version has to fail rather than ship -- read back out of
        # the built artifact, not out of the source.
        tagged=""
        if command -v git >/dev/null &&
            tag="$(git -C "$ROOT" describe --exact-match --tags HEAD 2>/dev/null)"; then
            [[ $tag == v* ]] && tagged="$tag"
        fi
        if [[ $tagged ]]; then
            built="$(cd "$dist" && printf '%s' pyguitest-*.tar.gz)"
            built="${built#pyguitest-}"
            built="${built%.tar.gz}"
            printf 'tag=%s packaged=%s\n' "$tagged" "$built"
            if [[ ${tagged#v} != "$built" ]]; then
                echo "tag $tagged does not match the packaged version $built" \
                    "-- fix __version__ or retag."
                exit 1
            fi
        fi

        # The sdist has to be self-testing. tests/test_docs.py reads docs/, and
        # building from the sdist and running the suite is what Fedora and
        # Debian do -- which is what MANIFEST.in is for. A failure here names
        # the file that was missing; the sdist itself is not kept.
        tar xzf "$dist"/pyguitest-*.tar.gz -C "$outer"
        ( cd "$outer"/pyguitest-* && PYTHONPATH=src "$PYTHON" -m unittest discover -s tests -v )
    )
    status=$?

    rm -rf "$dist" "$outer"
    return "$status"
}

# --------------------------------------------------------------------- run

results=()   # "check|status|duration|logfile"
failed=0
ran=0
skipped=0
run_start="$(now)"

printf '%s%s%s  %s  %s\n' "$BOLD" "pre-commit-test" "$RESET" \
       "$("$PYTHON" -V 2>&1)" "${DIM}$(date '+%F %T')${RESET}"
printf '\n%s== pyguitest ==%s\n' "$BOLD" "$RESET"

active_checks=("${CHECKS[@]}")
((full)) && active_checks+=("${FULL_CHECKS[@]}")

for entry in "${active_checks[@]}"; do
    IFS='|' read -r name cmd guard needs <<<"$entry"
    matches want_checks "$name" || continue

    # A guard that says no is a SKIP: printed, counted, and not a failure.
    # --full on a machine missing ImageMagick or dbusmock is a legitimate run,
    # and the summary says which claims it did not check.
    if [[ $guard ]] && ! "$guard"; then
        printf '  %-8s %sSKIP%s  %s(%s not available)%s\n' \
               "$name" "$YELLOW" "$RESET" "$DIM" "$needs" "$RESET"
        results+=("$name|SKIP|-|")
        ((skipped++))
        continue
    fi

    log="$LOGDIR/$name.log"
    if ((verbose)); then
        printf '  %-8s %s$ %s%s\n' "$name" "$DIM" "$cmd" "$RESET"
    elif ((TTY)); then
        printf '  %-8s %s...%s' "$name" "$DIM" "$RESET"
    fi

    start="$(now)"
    if ((verbose)); then
        ( cd "$ROOT" && eval "$cmd" ) 2>&1 | tee "$log"
        status=${PIPESTATUS[0]}
    else
        ( cd "$ROOT" && eval "$cmd" ) >"$log" 2>&1
        status=$?
    fi
    dur="$(awk -v a="$start" -v b="$(now)" 'BEGIN { printf "%.1fs", b - a }')"
    ((ran++))

    cr=$'\r'; { ((verbose)) || ((!TTY)); } && cr=''
    if ((status == 0)); then
        printf '%s  %-8s %sPASS%s  %6s\n' "$cr" "$name" "$GREEN" "$RESET" "$dur"
        results+=("$name|PASS|$dur|$log")
    else
        printf '%s  %-8s %sFAIL%s  %6s  %s(exit %d)%s\n' "$cr" \
               "$name" "$RED" "$RESET" "$dur" "$DIM" "$status" "$RESET"
        results+=("$name|FAIL|$dur|$log")
        ((failed++))
        ((fail_fast)) && break
    fi
done

((ran + skipped)) || { echo "pre-commit-test.sh: no checks matched" >&2; rm -rf "$LOGDIR"; exit 2; }

# ----------------------------------------------------------------- summary

total_dur="$(awk -v a="$run_start" -v b="$(now)" 'BEGIN { printf "%.1fs", b - a }')"

if ((failed && !quiet && !verbose)); then
    for entry in "${results[@]}"; do
        IFS='|' read -r name status dur log <<<"$entry"
        [[ $status == FAIL ]] || continue
        printf '\n%s---- %s ----%s\n' "$YELLOW" "$name" "$RESET"
        # Long test failures are the norm; the tail is where the summary is.
        tail -n 40 "$log"
        printf '%sfull log: %s%s\n' "$DIM" "$log" "$RESET"
    done
fi

# Named rather than counted alone: a skip is --full telling you which claims
# this machine could not check, and a run without them is still a pass.
list_skipped() {
    local entry name status
    for entry in "${results[@]}"; do
        IFS='|' read -r name status _ _ <<<"$entry"
        [[ $status == SKIP ]] && printf '  %sSKIP%s %s\n' "$YELLOW" "$RESET" "$name"
    done
}

printf '\n%s%s%s  ' "$BOLD" "summary" "$RESET"
if ((failed == 0)); then
    printf '%s%d/%d passed%s' "$GREEN" "$ran" "$ran" "$RESET"
    ((skipped)) && printf ', %s%d skipped%s' "$YELLOW" "$skipped" "$RESET"
    printf ' in %s\n' "$total_dur"
    list_skipped
    rm -rf "$LOGDIR"
    exit 0
fi

printf '%s%d of %d failed%s' "$RED" "$failed" "$ran" "$RESET"
((skipped)) && printf ', %s%d skipped%s' "$YELLOW" "$skipped" "$RESET"
printf ' in %s\n' "$total_dur"
list_skipped
for entry in "${results[@]}"; do
    IFS='|' read -r name status dur log <<<"$entry"
    [[ $status == FAIL ]] && printf '  %sFAIL%s %s\n' "$RED" "$RESET" "$name"
done
printf '%slogs kept in %s%s\n' "$DIM" "$LOGDIR" "$RESET"
exit 1
