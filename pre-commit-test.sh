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
# Note it checks the working tree, not the index. If you have unstaged
# changes, that is not what `git commit` is about to record.
#
# Usage:
#   ./pre-commit-test.sh             run everything
#   ./pre-commit-test.sh -k mypy     only checks whose name matches (repeatable)
#   ./pre-commit-test.sh -v          stream each check's output as it runs
#   ./pre-commit-test.sh -q          summary only; do not dump failure logs
#   ./pre-commit-test.sh -x          stop at the first failure
#
# Exit status: 0 all passed, 1 one or more checks failed, 2 setup problem.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"
RUFF="${RUFF:-ruff}"

# name|command. Run from the repository root, via eval, so the quoting here
# is ordinary shell quoting.
CHECKS=(
    "tests|$PYTHON -m pytest -q"
    "ruff|$RUFF check src tests examples scripts"
    "format|$RUFF format --check src tests examples scripts"
    "mypy|$PYTHON -m mypy"
)

# ---------------------------------------------------------------- arguments

verbose=0
quiet=0
fail_fast=0
want_checks=()

usage() { sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while (($#)); do
    case "$1" in
        -k|--check)   [[ ${2:-} ]] || { echo "-k needs a value" >&2; exit 2; }
                      want_checks+=("$2"); shift 2 ;;
        -v|--verbose) verbose=1; shift ;;
        -q|--quiet)   quiet=1; shift ;;
        -x|--fail-fast) fail_fast=1; shift ;;
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

# --------------------------------------------------------------------- run

results=()   # "check|status|duration|logfile"
failed=0
ran=0
run_start="$(now)"

printf '%s%s%s  %s  %s\n' "$BOLD" "pre-commit-test" "$RESET" \
       "$("$PYTHON" -V 2>&1)" "${DIM}$(date '+%F %T')${RESET}"
printf '\n%s== pyguitest ==%s\n' "$BOLD" "$RESET"

for entry in "${CHECKS[@]}"; do
    IFS='|' read -r name cmd <<<"$entry"
    matches want_checks "$name" || continue

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

((ran)) || { echo "pre-commit-test.sh: no checks matched" >&2; rm -rf "$LOGDIR"; exit 2; }

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

printf '\n%s%s%s  ' "$BOLD" "summary" "$RESET"
if ((failed == 0)); then
    printf '%s%d/%d passed%s in %s\n' "$GREEN" "$ran" "$ran" "$RESET" "$total_dur"
    rm -rf "$LOGDIR"
    exit 0
fi

printf '%s%d of %d failed%s in %s\n' "$RED" "$failed" "$ran" "$RESET" "$total_dur"
for entry in "${results[@]}"; do
    IFS='|' read -r name status dur log <<<"$entry"
    [[ $status == FAIL ]] && printf '  %sFAIL%s %s\n' "$RED" "$RESET" "$name"
done
printf '%slogs kept in %s%s\n' "$DIM" "$LOGDIR" "$RESET"
exit 1
