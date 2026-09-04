#!/usr/bin/env bash
# Run a command inside a private, headless GNOME Shell session.
#
# The COMPOSITOR tier is the one thing pyguitest cannot test without a
# desktop: window control, window capture, window events and the GNOME Shell
# extension all need a live compositor, and every claim this repository makes
# about them was hand-validated by a person at a real screen (see
# docs/validation.md). That is not a regression test, and it does not run in
# CI.
#
# `gnome-shell --headless --virtual-monitor WxH` is a full Mutter with no
# output attached. Started on its own session bus, its `org.gnome.Shell` does
# not collide with the one already running, so this works on a developer's
# desktop as well as on a bare CI runner -- nothing appears on screen and the
# real session is untouched.
#
#   ./scripts/headless-session.sh pyguitest doctor
#   ./scripts/headless-session.sh ./scripts/validate-gnome-extension.sh
#   ./scripts/headless-session.sh python3 examples/01_what_can_i_do.py
#
# Options (before the command):
#   --size WxH        virtual monitor size (default 1920x1080)
#   --display NAME    Wayland socket name (default pyguitest-headless-$$)
#   --timeout N       seconds to wait for the shell to come up (default 30)
#   --no-x11          do not start XWayland inside the session
#   --wait-for PATH   also wait for a Shell extension object to answer at
#                     this D-Bus path, or "none" to skip (default: the
#                     pyguitest-window-control extension's own path)
#   --log FILE        keep the shell's own output (default: a temp file,
#                     shown only if startup fails)
#   -v, --verbose     stream the shell's output as it runs
#
# The command runs from the repository root, so relative paths in the examples
# above resolve the way they read. Exit status is the command's own; 1 is a
# setup failure where the command never ran, and 124 means the shell never
# became ready. --timeout bounds each of the two waits below separately, not
# their sum.
#
# TWO TRAPS THIS SCRIPT EXISTS TO CLOSE, both of which make a "headless" run
# quietly address the developer's real desktop instead:
#
#   DISPLAY. Headless mode starts its own XWayland, but nothing exports the
#   new DISPLAY into this shell -- there is no systemd user session here to
#   carry it. Leaving the outer DISPLAY set means xdotool, xclip and every
#   other X11 tool inside the "isolated" session drive the machine's real X
#   server. So DISPLAY is unset for the command, and --no-x11 stops XWayland
#   from starting at all, which is what CI wants.
#
#   The Wayland socket. A shell killed by a signal leaves
#   $XDG_RUNTIME_DIR/<name> and <name>.lock behind -- confirmed here, the
#   first thing a live run showed -- and a later run reusing the name then
#   fails to bind. The trap below removes both, and the default name carries
#   $$ so two runs never collide anyway.
#
# And one delay that reads exactly like a missing extension: the shell takes
# its bus name several seconds before its extensions are loaded -- measured at
# 4.4s here. A command connecting in between gets "Object does not exist at
# path", which is the same error an absent extension gives. Hence the second
# wait below.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ORIGINAL_ARGS=("$@")

SIZE="1920x1080"
DISPLAY_NAME="pyguitest-headless-$$"
READY_TIMEOUT=30
WITH_X11=1
WAIT_FOR="/org/gnome/Shell/Extensions/Pyguitest"
LOG=""
VERBOSE=0

usage() {
    # Everything from line 2 up to the first line that is not a comment.
    awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' \
        "${BASH_SOURCE[0]}"
}

while (($#)); do
    case "$1" in
        --size)     [[ ${2:-} ]] || { echo "--size needs a value" >&2; exit 1; }
                    SIZE="$2"; shift 2 ;;
        --display)  [[ ${2:-} ]] || { echo "--display needs a value" >&2; exit 1; }
                    DISPLAY_NAME="$2"; shift 2 ;;
        --timeout)  [[ ${2:-} ]] || { echo "--timeout needs a value" >&2; exit 1; }
                    READY_TIMEOUT="$2"; shift 2 ;;
        --no-x11)   WITH_X11=0; shift ;;
        --wait-for) [[ ${2:-} ]] || { echo "--wait-for needs a value" >&2; exit 1; }
                    WAIT_FOR="$2"; shift 2 ;;
        --log)      [[ ${2:-} ]] || { echo "--log needs a value" >&2; exit 1; }
                    LOG="$2"; shift 2 ;;
        -v|--verbose) VERBOSE=1; shift ;;
        -h|--help)  usage; exit 0 ;;
        --)         shift; break ;;
        -*)         echo "headless-session.sh: unknown option '$1' (try --help)" >&2
                    exit 1 ;;
        *)          break ;;
    esac
done

(($#)) || { echo "headless-session.sh: no command given (try --help)" >&2; exit 1; }

# ----------------------------------------------------------------- preflight

missing=()
for binary in gnome-shell dbus-run-session gdbus; do
    command -v "$binary" >/dev/null || missing+=("$binary")
done
if ((${#missing[@]})); then
    echo "headless-session.sh: not installed: ${missing[*]}" >&2
    echo "needs gnome-shell (>=40, for --headless) plus the glib2/dbus tools" >&2
    exit 1
fi

# A CI runner often has no XDG_RUNTIME_DIR at all, and the Wayland socket has
# to live in a directory only this user can read. Made here rather than
# assumed, and torn down with everything else. The env var carries ownership
# across the re-exec below, so the inner run knows to remove what the outer
# one created -- the outer process is gone by then.
if [[ -z ${XDG_RUNTIME_DIR:-} || ! -d ${XDG_RUNTIME_DIR:-} ]]; then
    XDG_RUNTIME_DIR="$(mktemp -d "${TMPDIR:-/tmp}/pyguitest-runtime.XXXXXX")"
    chmod 700 "$XDG_RUNTIME_DIR"
    export XDG_RUNTIME_DIR PYGUITEST_HEADLESS_OWN_RUNTIME=1
fi

# ------------------------------------------------- re-exec on a private bus
#
# dbus-run-session rather than a hand-started dbus-daemon: it owns the
# daemon's whole lifetime, so an interrupted run cannot strand one. The
# re-exec is what puts the command on the *same* bus as the shell -- past this
# point $DBUS_SESSION_BUS_ADDRESS is the private one and everything inherits
# it. The original arguments go back in unparsed, so this file has exactly one
# argument parser rather than two that can drift.

if [[ ${PYGUITEST_HEADLESS_INNER:-} != 1 ]]; then
    export PYGUITEST_HEADLESS_INNER=1
    exec dbus-run-session -- "${BASH_SOURCE[0]}" "${ORIGINAL_ARGS[@]}"
    # Not reached.
fi

# ------------------------------------------------------------------ the shell

SOCKET="$XDG_RUNTIME_DIR/$DISPLAY_NAME"
if [[ -z $LOG ]]; then
    LOG="$(mktemp "${TMPDIR:-/tmp}/pyguitest-headless.XXXXXX.log")"
    LOG_IS_TEMP=1
else
    LOG_IS_TEMP=0
fi

SHELL_PID=""

cleanup() {
    local status=$?
    if [[ -n $SHELL_PID ]] && kill -0 "$SHELL_PID" 2>/dev/null; then
        kill "$SHELL_PID" 2>/dev/null
        # Give Mutter a moment to unwind; SIGKILL only if it will not.
        for _ in $(seq 10); do
            kill -0 "$SHELL_PID" 2>/dev/null || break
            sleep 0.2
        done
        kill -9 "$SHELL_PID" 2>/dev/null
        wait "$SHELL_PID" 2>/dev/null
    fi
    # A signalled shell leaves both of these behind; see the header.
    rm -f "$SOCKET" "$SOCKET.lock"
    ((LOG_IS_TEMP)) && rm -f "$LOG"
    [[ ${PYGUITEST_HEADLESS_OWN_RUNTIME:-0} == 1 ]] && rm -rf "$XDG_RUNTIME_DIR"
    return $status
}
trap cleanup EXIT

shell_args=(--headless --virtual-monitor "$SIZE" --wayland-display "$DISPLAY_NAME")
((WITH_X11)) || shell_args+=(--no-x11)

if ((VERBOSE)); then
    # Process substitution, not a pipe into tee: a pipeline would make $!
    # tee's pid, and the cleanup below would then signal the wrong process
    # and leave a compositor running.
    gnome-shell "${shell_args[@]}" > >(tee "$LOG") 2>&1 &
else
    gnome-shell "${shell_args[@]}" >"$LOG" 2>&1 &
fi
SHELL_PID=$!

# Readiness is a name on the bus, not a socket on disk: the socket appears
# well before the shell is up, and a command that connects to a compositor
# still starting sees an empty desktop rather than an error -- the worst of
# the two outcomes, since it looks like a real answer.
if ! gdbus wait --session --timeout "$READY_TIMEOUT" org.gnome.Shell; then
    echo "headless-session.sh: gnome-shell never reached the bus" \
         "in ${READY_TIMEOUT}s" >&2
    echo "--- its output ---" >&2
    tail -n 30 "$LOG" >&2
    exit 124
fi

# Second wait, and it is a wait rather than a requirement: a session with no
# extension installed still runs the command, one second late. `gdbus
# introspect` alone is not the probe -- GDBus answers for a path that does not
# exist, with the three standard interfaces and nothing else -- so this looks
# for the extension's own interface in the XML.
if [[ $WAIT_FOR != none ]]; then
    deadline=$((SECONDS + READY_TIMEOUT))
    until gdbus introspect --session --dest org.gnome.Shell \
              --object-path "$WAIT_FOR" --xml 2>/dev/null |
          grep -q 'interface name="org.gnome.Shell.Extensions\.'; do
        if ((SECONDS >= deadline)); then
            echo "headless-session.sh: no extension answered at $WAIT_FOR" \
                 "in ${READY_TIMEOUT}s; running anyway" >&2
            break
        fi
        sleep 0.25
    done
fi

# ---------------------------------------------------------------- the command

# DISPLAY is unset rather than pointed at the inner XWayland: see the header.
# Nothing here knows the inner display number -- Mutter publishes it to a
# systemd user session this run does not have -- and leaving the outer value
# in place is exactly the failure this closes.
export WAYLAND_DISPLAY="$DISPLAY_NAME"
export XDG_SESSION_TYPE="wayland"
export XDG_CURRENT_DESKTOP="GNOME"
export PYGUITEST_HEADLESS_SESSION="$DISPLAY_NAME"
unset DISPLAY

cd "$ROOT" || exit 1
"$@"
exit $?
