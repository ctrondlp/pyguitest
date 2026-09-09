#!/usr/bin/env bash
# Run a command inside a private, headless sway session.
#
# SwayBackend (backends/windows.py) is the only wlroots-family window
# backend with a live compositor to test against so far -- Hyprland and
# niri speak variants of the same IPC shape but neither has run live
# either, and docs/validation.md's "Not run live" section has carried all
# three since they were written. sway is the one to close first: its IPC
# is the most stable of the three and `WLR_BACKENDS=headless` needs no
# real GPU or seat, so this works unattended on a bare CI runner as well
# as a developer desktop -- nothing appears on screen and the real
# session (Wayland, X11, or another compositor entirely) is untouched.
#
#   ./scripts/headless-sway-session.sh pyguitest doctor
#   ./scripts/headless-sway-session.sh python3 examples/_sway_validate.py
#
# Options (before the command):
#   --size WxH       headless output resolution (default 1920x1080)
#   --renderer NAME  WLR_RENDERER value (default pixman -- software, so no
#                    GPU/EGL is required; passing "" leaves wlroots to pick)
#   --timeout N      seconds to wait for the socket and first output (default 30)
#   --log FILE       keep sway's own output (default: a temp file, shown
#                     only if startup fails)
#   -v, --verbose    stream sway's output as it runs
#
# The command runs from the repository root, so relative paths resolve the
# way they read. Exit status is the command's own; 1 is a setup failure
# where the command never ran, and 124 means sway never became ready.
#
# THE TRAP THIS SCRIPT EXISTS TO CLOSE: sway never hands its own IPC socket
# path back to the shell that launched it -- it sets SWAYSOCK via setenv()
# on its *own* running process, and /proc/<pid>/environ is a kernel
# snapshot taken at execve() that never reflects that (confirmed live on
# this box: sway 1.12, wlroots 0.20.2 -- SWAYSOCK never appears there,
# though $XDG_RUNTIME_DIR does get a real `sway-ipc.<uid>.<pid>.sock`
# socket file the moment it is ready). So this computes that path directly
# from the pid this script itself started -- confirmed live to be sway's
# actual naming -- and finds the new Wayland socket the same way, by
# diffing $XDG_RUNTIME_DIR's `wayland-*` entries from before sway started.
# Both are exported for the command -- never the outer session's own
# SWAYSOCK/WAYLAND_DISPLAY/DISPLAY, which are unset for the command so
# nothing "headless" quietly drives the real desktop instead.
#
# The headless backend auto-creates one output (1280x720) at startup, so
# this just resizes it to --size rather than adding a second one -- see
# the "size the output" section below for why that distinction matters.
#
# A SECOND TRAP, found live running this against the real desktop: sway
# itself needs no D-Bus, but pyguitest's *automatic* backend composition
# (plain connect(), and so `pyguitest doctor`) consults the session bus for
# other things -- GNOME/KDE reachability among them -- and desktop-name
# variables like XDG_SESSION_DESKTOP/DESKTOP_SESSION leak in from the
# outer shell too. Confirmed live here: without the fix below, `pyguitest
# doctor` run through an earlier version of this script reported
# `compositor mutter`, having read the *real* desktop's leftover
# XDG_SESSION_DESKTOP=gnome/DESKTOP_SESSION=gnome rather than this
# session's sway. So, same as headless-session.sh, the command runs on a
# private `dbus-run-session` bus, and every desktop-name variable is
# overridden or unset for it, not merely XDG_CURRENT_DESKTOP.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ORIGINAL_ARGS=("$@")

SIZE="1920x1080"
RENDERER="pixman"
READY_TIMEOUT=30
LOG=""
VERBOSE=0

usage() {
    awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' \
        "${BASH_SOURCE[0]}"
}

while (($#)); do
    case "$1" in
        --size)     [[ ${2:-} ]] || { echo "--size needs a value" >&2; exit 1; }
                    SIZE="$2"; shift 2 ;;
        --renderer) [[ ${2+x} ]] || { echo "--renderer needs a value" >&2; exit 1; }
                    RENDERER="$2"; shift 2 ;;
        --timeout)  [[ ${2:-} ]] || { echo "--timeout needs a value" >&2; exit 1; }
                    READY_TIMEOUT="$2"; shift 2 ;;
        --log)      [[ ${2:-} ]] || { echo "--log needs a value" >&2; exit 1; }
                    LOG="$2"; shift 2 ;;
        -v|--verbose) VERBOSE=1; shift ;;
        -h|--help)  usage; exit 0 ;;
        --)         shift; break ;;
        -*)         echo "headless-sway-session.sh: unknown option '$1' (try --help)" >&2
                    exit 1 ;;
        *)          break ;;
    esac
done

(($#)) || { echo "headless-sway-session.sh: no command given (try --help)" >&2; exit 1; }

# ----------------------------------------------------------------- preflight

missing=()
for binary in sway swaymsg python3 dbus-run-session; do
    command -v "$binary" >/dev/null || missing+=("$binary")
done
if ((${#missing[@]})); then
    echo "headless-sway-session.sh: not installed: ${missing[*]}" >&2
    echo "needs sway (built with the headless backend), swaymsg, and dbus" >&2
    exit 1
fi

# Same trap as headless-session.sh: a CI runner often has no
# XDG_RUNTIME_DIR, and the IPC socket has to live in a directory only this
# user can read. The env var carries ownership across the re-exec below,
# the same way headless-session.sh's PYGUITEST_HEADLESS_OWN_RUNTIME does.
if [[ -z ${XDG_RUNTIME_DIR:-} || ! -d ${XDG_RUNTIME_DIR:-} ]]; then
    XDG_RUNTIME_DIR="$(mktemp -d "${TMPDIR:-/tmp}/pyguitest-sway-runtime.XXXXXX")"
    chmod 700 "$XDG_RUNTIME_DIR"
    export XDG_RUNTIME_DIR PYGUITEST_SWAY_OWN_RUNTIME=1
fi

# ------------------------------------------------- re-exec on a private bus
#
# See the second header trap above: this puts the command on a session bus
# of its own, so automatic backend composition cannot reach the real
# desktop's D-Bus by accident. The original arguments go back in unparsed,
# so this file has exactly one argument parser rather than two that can
# drift -- same technique as headless-session.sh.

if [[ ${PYGUITEST_SWAY_INNER:-} != 1 ]]; then
    export PYGUITEST_SWAY_INNER=1
    exec dbus-run-session -- "${BASH_SOURCE[0]}" "${ORIGINAL_ARGS[@]}"
    # Not reached.
fi

OWN_RUNTIME="${PYGUITEST_SWAY_OWN_RUNTIME:-0}"

if [[ -z $LOG ]]; then
    LOG="$(mktemp "${TMPDIR:-/tmp}/pyguitest-sway-headless.XXXXXX.log")"
    LOG_IS_TEMP=1
else
    LOG_IS_TEMP=0
fi

SWAY_PID=""

cleanup() {
    local status=$?
    if [[ -n $SWAY_PID ]] && kill -0 "$SWAY_PID" 2>/dev/null; then
        kill "$SWAY_PID" 2>/dev/null
        for _ in $(seq 10); do
            kill -0 "$SWAY_PID" 2>/dev/null || break
            sleep 0.2
        done
        kill -9 "$SWAY_PID" 2>/dev/null
        wait "$SWAY_PID" 2>/dev/null
    fi
    [[ -n ${SWAYSOCK:-} ]] && rm -f "$SWAYSOCK"
    ((LOG_IS_TEMP)) && rm -f "$LOG"
    ((OWN_RUNTIME)) && rm -rf "$XDG_RUNTIME_DIR"
    return $status
}
trap cleanup EXIT

# ------------------------------------------------------------------- sway up
#
# `-c /dev/null`: an empty config, deliberately. No keybindings are needed
# -- everything here drives sway over IPC -- and no `xwayland enable` line
# means no XWayland starts, which is the point: this validates the native
# wlroots IPC path, not windows reached through an X server underneath it.
#
# WLR_BACKENDS=headless: no real output or input device is touched.
# WLR_LIBINPUT_NO_DEVICES=1: skips libinput device enumeration entirely, so
# this needs no seatd/logind session and no elevated permissions, the same
# reason wlroots' and sway's own CI can run it in a bare container.
# WLR_RENDERER defaults to pixman (software) so no GPU/EGL/DRM node is
# required either; pass --renderer "" to let wlroots choose instead.

# Snapshot before starting, so the Wayland socket sway creates can be told
# apart from any that were already there -- $XDG_RUNTIME_DIR is the user's
# real one whenever one already existed (see above), and a live desktop's
# own `wayland-0` is exactly the kind of thing this must not pick up.
pre_wayland_sockets=()
for f in "$XDG_RUNTIME_DIR"/wayland-*; do
    [[ -S $f ]] && pre_wayland_sockets+=("$(basename "$f")")
done

env=(env WLR_BACKENDS=headless WLR_LIBINPUT_NO_DEVICES=1)
[[ -n $RENDERER ]] && env+=(WLR_RENDERER="$RENDERER")

if ((VERBOSE)); then
    "${env[@]}" sway -c /dev/null > >(tee "$LOG") 2>&1 &
else
    "${env[@]}" sway -c /dev/null >"$LOG" 2>&1 &
fi
SWAY_PID=$!

# sway's own naming, confirmed live (1.12 / wlroots 0.20.2): the socket
# this process creates is deterministic from its own pid, so this is
# computed rather than discovered -- see the header on why /proc/environ
# cannot be used instead.
SWAYSOCK="$XDG_RUNTIME_DIR/sway-ipc.$(id -u).$SWAY_PID.sock"
INNER_WAYLAND_DISPLAY=""

deadline=$((SECONDS + READY_TIMEOUT))
while [[ ! -S $SWAYSOCK || -z $INNER_WAYLAND_DISPLAY ]]; do
    if ! kill -0 "$SWAY_PID" 2>/dev/null; then
        echo "headless-sway-session.sh: sway exited before it was ready" >&2
        echo "--- its output ---" >&2
        tail -n 30 "$LOG" >&2
        exit 1
    fi
    if ((SECONDS >= deadline)); then
        echo "headless-sway-session.sh: sway never became ready" \
             "in ${READY_TIMEOUT}s (socket=$([[ -S $SWAYSOCK ]] && echo yes || echo no)," \
             "wayland=${INNER_WAYLAND_DISPLAY:-none})" >&2
        echo "--- its output ---" >&2
        tail -n 30 "$LOG" >&2
        exit 124
    fi
    if [[ -z $INNER_WAYLAND_DISPLAY ]]; then
        for f in "$XDG_RUNTIME_DIR"/wayland-*; do
            [[ -S $f ]] || continue
            name="$(basename "$f")"
            printf '%s\n' "${pre_wayland_sockets[@]:-}" | grep -qxF "$name" && continue
            INNER_WAYLAND_DISPLAY="$name"
            break
        done
    fi
    [[ -S $SWAYSOCK && -n $INNER_WAYLAND_DISPLAY ]] || sleep 0.1
done

# ------------------------------------------------------------- size the output
#
# The headless backend auto-creates exactly one output at startup,
# HEADLESS-1 @ 1280x720 -- confirmed live (sway 1.12 / wlroots 0.20.2), and
# NOT zero outputs as an earlier version of this script assumed. That
# assumption cost a real debugging session: it called `swaymsg
# create_output` unconditionally, which created a *second*, unused output
# (HEADLESS-2) and resized that one, leaving every window mapped onto the
# untouched 1280x720 HEADLESS-1 -- geometry()/move_window()/resize_window()
# all looked broken as a result, and were not. So this reads back
# get_outputs and resizes whichever output(s) already exist instead of
# creating one.

output_names=()
while IFS= read -r name; do
    output_names+=("$name")
done < <(swaymsg -s "$SWAYSOCK" -t get_outputs -r | python3 -c '
import json, sys
for o in json.load(sys.stdin):
    print(o["name"])
')
if ((${#output_names[@]} == 0)); then
    echo "headless-sway-session.sh: no output exists to size" >&2
    exit 1
fi
for name in "${output_names[@]}"; do
    if ! swaymsg -s "$SWAYSOCK" "output $name resolution $SIZE" >/dev/null; then
        echo "headless-sway-session.sh: failed to size $name to $SIZE" >&2
        exit 1
    fi
done

# ---------------------------------------------------------------- the command
#
# The outer session's own SWAYSOCK/WAYLAND_DISPLAY/DISPLAY are never
# consulted above and are overridden here, so a real desktop underneath
# this shell (Wayland, X11, or another compositor) cannot be reached by
# accident -- see the header.

# All three desktop-name variables session.py's _compositor() reads, not
# just XDG_CURRENT_DESKTOP -- see the second header trap. Left alone,
# XDG_SESSION_DESKTOP/DESKTOP_SESSION carry the outer desktop's name
# (e.g. "gnome") straight through into the haystack it matches against,
# and "gnome" is checked before the wlroots hints are.
export SWAYSOCK
export WAYLAND_DISPLAY="$INNER_WAYLAND_DISPLAY"
export XDG_SESSION_TYPE="wayland"
export XDG_CURRENT_DESKTOP="sway"
export XDG_SESSION_DESKTOP="sway"
export PYGUITEST_HEADLESS_SWAY_SESSION="$SWAYSOCK"
unset DISPLAY DESKTOP_SESSION

cd "$ROOT" || exit 1
"$@"
exit $?
