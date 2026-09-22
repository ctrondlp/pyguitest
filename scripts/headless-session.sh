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
#   --x11-display     find the session's own XWayland and export DISPLAY and
#                     XAUTHORITY into the command, so the XWayland half of a
#                     Wayland desktop can be exercised (see below)
#   --a11y            start a private accessibility bus inside the session, so
#                     applications publish elements (see below)
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
# A THIRD TRAP, WHICH THIS SCRIPT CANNOT CLOSE, only warn about:
#
#   INPUT. `pyguitest doctor` run inside this session reports the transport it
#   would use on *this machine*, and on an ordinary Linux desktop that is
#   in-process uinput. uinput is a kernel device: the events it injects go to
#   the seat, which is the developer's real session, not the headless Mutter
#   started here -- which reads libinput not at all, having no seat of its own.
#   So input injected from inside this session lands outside it, silently and
#   on whatever has focus. Confirmed here: XTest aimed at the inner XWayland
#   reached no client at all (the compositor owns the pointer), while the
#   doctor in the same session went on naming uinput.
#
#   There is no injection path into a headless GNOME session short of Mutter's
#   own org.gnome.Mutter.RemoteDesktop, which nothing in this package speaks
#   yet. Until then this harness is for the halves that only *read* -- window
#   listing, geometry, elements, capture, events -- and anything that types or
#   clicks has to be run somewhere else. See docs/validation.md.
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
#   --x11-display is the other answer to the same trap: rather than unsetting
#   DISPLAY it works out what the *inner* one is and points the command at
#   that. Without it the XWayland half of a GNOME Wayland session -- which is
#   where every X11 client on such a desktop actually lives -- cannot be
#   reached from this harness at all, so nothing about it could be tested
#   here. It stays opt-in because exporting DISPLAY changes what
#   `pyguitest.detect()` says: a session that classifies as `wayland` with no
#   DISPLAY classifies as `xwayland` with one, which selects different
#   backends. Callers that want the pure-Wayland answer must keep getting it.
#
#   Finding it is not a lookup. Mutter publishes the display to a systemd user
#   session this run does not have, and it starts Xwayland *lazily* -- the
#   socket exists from the start but no process does until something connects,
#   so there is nothing to read the number off yet. Mutter also binds two
#   socket files per display (X<n> and X<n+1>, both reaching the one server),
#   so counting new sockets does not give the answer either: it gives two
#   answers, one of them wrong. So this connects to the lowest new socket,
#   which forces the spawn, and then reads the display number out of the
#   Xwayland process's own argv, which is authoritative. The cookie comes from
#   the .mutter-Xwaylandauth file Mutter writes into XDG_RUNTIME_DIR at
#   startup; its entries are host-wide (`fedora/unix:` with no display
#   number), so the same file authenticates whichever display it turns out to
#   be.
#
#   The accessibility bus. A private session bus has no systemd to activate
#   org.a11y.Bus, so `at-spi-bus-launcher` never starts and nothing on the
#   desktop publishes an element -- `Atspi.get_desktop(0)` still succeeds
#   against the empty result, which is why this fails as "no elements" rather
#   than as an error. --a11y starts the launcher and the registry by hand, the
#   same way pyguitest-recorder's own live check does.
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
EXPORT_X11=0
WITH_A11Y=0
A11Y_PIDS=()
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
        --x11-display) EXPORT_X11=1; shift ;;
        --a11y)     WITH_A11Y=1; shift ;;
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

if ((EXPORT_X11)) && ! ((WITH_X11)); then
    echo "headless-session.sh: --x11-display and --no-x11 contradict each other" >&2
    exit 1
fi

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
    # Before the compositor, and inside the trap rather than after the command:
    # a run ended by a signal would otherwise leave one `at-spi2-registryd` per
    # run behind on the machine, which is the exact leak pyguitest-recorder's
    # own live check documents having had. The ${x+"${x[@]}"} spelling is for
    # `set -u` with an array that may not exist yet.
    for a11y_pid in ${A11Y_PIDS+"${A11Y_PIDS[@]}"}; do
        kill "$a11y_pid" 2>/dev/null
    done
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

# Snapshotted before the shell exists, so "new" below means "this run's".
# Two sockets appear per display and the auth file appears at startup; see the
# header for why neither is read as the answer on its own.
X_SOCKETS_BEFORE=""
X_AUTH_BEFORE=""
if ((EXPORT_X11)); then
    X_SOCKETS_BEFORE="$(ls /tmp/.X11-unix 2>/dev/null | tr '\n' ' ')"
    X_AUTH_BEFORE="$(ls -d "$XDG_RUNTIME_DIR"/.mutter-Xwaylandauth* 2>/dev/null |
                     tr '\n' ' ')"
fi

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

# --------------------------------------------------- the inner X11 display

# Returns 0 and sets INNER_DISPLAY/INNER_XAUTHORITY, or returns 1 having said
# why. Kept as a function so the failure path is one place: a session whose
# XWayland cannot be found still runs the command, without a display, rather
# than dying -- the same choice --wait-for makes about a missing extension.
INNER_DISPLAY=""
INNER_XAUTHORITY=""

find_inner_x11() {
    local now new_socket first_socket auth probe number

    auth=""
    for candidate in "$XDG_RUNTIME_DIR"/.mutter-Xwaylandauth*; do
        [[ -e $candidate ]] || continue
        case " $X_AUTH_BEFORE " in *" $candidate "*) continue ;; esac
        auth="$candidate"
    done
    if [[ -z $auth ]]; then
        echo "headless-session.sh: no new Xwayland auth file appeared;" \
             "running without DISPLAY" >&2
        return 1
    fi

    now="$(ls /tmp/.X11-unix 2>/dev/null | tr '\n' ' ')"
    first_socket=""
    for new_socket in $now; do
        case " $X_SOCKETS_BEFORE " in *" $new_socket "*) continue ;; esac
        # The lowest new one. Mutter binds X<n> and X<n+1> for a single
        # display, so the higher is not a second server to choose between.
        if [[ -z $first_socket || ${new_socket#X} -lt ${first_socket#X} ]]; then
            first_socket="$new_socket"
        fi
    done
    if [[ -z $first_socket ]]; then
        echo "headless-session.sh: no new X socket appeared;" \
             "running without DISPLAY" >&2
        return 1
    fi

    # Connecting is what starts Xwayland: Mutter binds the socket at startup
    # and spawns the server on the first client. Until that happens there is
    # no process to read the real display number off, so this poke is load
    # bearing rather than a check.
    probe=":${first_socket#X}"
    XAUTHORITY="$auth" DISPLAY="$probe" python3 - <<'PROBE' >/dev/null 2>&1
import sys
try:
    from Xlib import display
except ImportError:
    sys.exit(0)
try:
    display.Display().close()
except Exception:
    pass
PROBE

    # Authoritative: the number Mutter actually started it on. The poke may
    # have reached it through either socket, so argv is read rather than
    # trusted from the socket name.
    number=""
    for _ in $(seq 20); do
        number="$(ps -eo ppid,args 2>/dev/null |
                  awk -v parent="$SHELL_PID" \
                      '$1 == parent && $2 ~ /Xwayland$/ { print $3; exit }')"
        [[ -n $number ]] && break
        sleep 0.25
    done
    if [[ -z $number ]]; then
        # No process, but the socket answered -- fall back to the socket name,
        # which is right whenever Mutter took the first display it bound.
        number="$probe"
    fi

    INNER_DISPLAY="$number"
    INNER_XAUTHORITY="$auth"
    return 0
}

# ------------------------------------------------- a private accessibility bus

start_a11y() {
    local launcher registry directory
    launcher=""
    registry=""
    for directory in /usr/libexec /usr/lib/at-spi2-core /usr/lib /usr/local/libexec; do
        [[ -x $directory/at-spi-bus-launcher && -z $launcher ]] &&
            launcher="$directory/at-spi-bus-launcher"
        [[ -x $directory/at-spi2-registryd && -z $registry ]] &&
            registry="$directory/at-spi2-registryd"
    done
    if [[ -z $launcher ]]; then
        echo "headless-session.sh: no at-spi-bus-launcher found;" \
             "running without an accessibility bus" >&2
        return 1
    fi
    # --launch-immediately, because nothing here will activate it on demand:
    # a private session bus has no systemd behind it.
    "$launcher" --launch-immediately >/dev/null 2>&1 &
    A11Y_PIDS+=($!)
    sleep 2
    # Started by hand for the same reason. Without the registry the bus exists
    # and answers nothing, which reads as "no elements" rather than as a
    # failure -- the shape this whole option exists to stop.
    if [[ -n $registry ]]; then
        "$registry" >/dev/null 2>&1 &
        A11Y_PIDS+=($!)
        sleep 2
    fi
    return 0
}

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

if ((EXPORT_X11)) && find_inner_x11; then
    export DISPLAY="$INNER_DISPLAY"
    export XAUTHORITY="$INNER_XAUTHORITY"
fi

((WITH_A11Y)) && start_a11y

cd "$ROOT" || exit 1
"$@"
exit $?
