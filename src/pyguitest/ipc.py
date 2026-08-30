"""Compositor IPC over unix sockets.

Speaking the documented socket protocol directly is better than shelling out to
the compositor's CLI on every axis that matters: no runtime tool requirement, no
process spawn per query, a versioned wire format instead of scraped output, and
a persistent connection that can stream events.

Stdlib only -- `socket`, `struct`, `json`. The CLI adapters remain as fallbacks
for setups where the socket is not reachable but the tool is.
"""

import json
import os
import re
import select
import socket
import struct
import subprocess
import time

__all__ = [
    "SwaySocket",
    "SwayCLI",
    "HyprlandSocket",
    "HyprlandCLI",
    "NiriSocket",
    "NiriCLI",
    "connect_sway",
    "connect_hyprland",
    "connect_niri",
]

DEFAULT_TIMEOUT = 10
"""Seconds before an unbounded socket read or subprocess call gives up.

Applied everywhere a request has no caller-supplied deadline of its own, so
a compositor that stops answering fails loudly instead of hanging the
process -- the "no timeouts anywhere" gap the request/subscribe paths had.
"""

# -- sway / i3 -------------------------------------------------------------

_MAGIC = b"i3-ipc"
_HEADER = struct.Struct("=II")  # payload length, message type
_HEADER_SIZE = len(_MAGIC) + _HEADER.size

RUN_COMMAND = 0
SUBSCRIBE = 2
GET_OUTPUTS = 3
GET_TREE = 4
EVENT_FLAG = 0x80000000


class SwaySocket:
    """The i3/sway IPC protocol.

    Framing is a 6-byte magic string, then payload length and message type as
    native-endian uint32s, then the payload. Replies use the same frame; event
    messages are distinguished by the high bit of the type.
    """

    def __init__(self, path=None, sock=None):
        # `sock` is for tests: an already-connected socket bypasses discovery.
        """Connect to the sway IPC socket, or use an already-connected `sock`."""
        if sock is not None:
            self.path, self._sock = path, sock
            return
        self.path = path or os.environ.get("SWAYSOCK") or os.environ.get("I3SOCK")
        if not self.path:
            raise OSError("no SWAYSOCK or I3SOCK in the environment")
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(DEFAULT_TIMEOUT)
        try:
            self._sock.connect(self.path)
        except OSError:
            self._sock.close()
            raise

    def close(self):
        """Close the socket."""
        self._sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def _recv_exactly(self, count):
        """Read exactly `count` bytes, looping over short reads."""
        chunks = []
        remaining = count
        while remaining:
            chunk = self._sock.recv(remaining)
            if not chunk:
                raise OSError("compositor closed the IPC connection")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _send(self, message_type, payload=b""):
        """Send one framed message."""
        if isinstance(payload, str):
            payload = payload.encode()
        self._sock.sendall(_MAGIC + _HEADER.pack(len(payload), message_type) + payload)

    def _read(self):
        """Read one framed message and return (type, parsed payload)."""
        header = self._recv_exactly(_HEADER_SIZE)
        if not header.startswith(_MAGIC):
            raise OSError("bad IPC magic; not a sway socket")
        length, message_type = _HEADER.unpack(header[len(_MAGIC) :])
        return message_type, json.loads(self._recv_exactly(length) or b"null")

    def request(self, message_type, payload=b""):
        """Send a request and return its reply payload."""
        self._send(message_type, payload)
        _, body = self._read()
        return body

    def get_tree(self):
        """The full window tree."""
        return self.request(GET_TREE)

    def get_outputs(self):
        """Every output sway knows about."""
        return self.request(GET_OUTPUTS)

    def run_command(self, command):
        """Run a sway command string."""
        return self.request(RUN_COMMAND, command)

    def subscribe(self, events=("window",), deadline=None):
        """Yield event payloads until `deadline` (a time.monotonic() value).

        `deadline=None` blocks indefinitely, as before -- explicitly, via
        settimeout(None) before every read: the socket already carries
        DEFAULT_TIMEOUT from __init__, so leaving that ambient timeout in
        place here would silently cap "indefinitely" at DEFAULT_TIMEOUT
        instead of genuinely waiting forever. The socket's timeout is
        recomputed before every read, so a real deadline is a true
        wall-clock bound across the whole subscription -- not a per-event
        one that resets whenever an unrelated event arrives. Either way,
        DEFAULT_TIMEOUT is restored on exit so a request() made on this same
        connection afterwards stays protected too.
        """
        self._send(SUBSCRIBE, json.dumps(list(events)))
        self._read()  # the subscribe acknowledgement
        try:
            while True:
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return
                    self._sock.settimeout(remaining)
                else:
                    self._sock.settimeout(None)
                message_type, body = self._read()
                if message_type & EVENT_FLAG:
                    yield body
        except TimeoutError:
            return
        finally:
            self._sock.settimeout(DEFAULT_TIMEOUT)


class SwayCLI:
    """Fallback transport using swaymsg, for when the socket is unreachable."""

    def __init__(self, runner=None, streamer=None):
        """Wrap swaymsg, optionally with injected runner and streamer."""
        self._runner = runner or self._run
        self._streamer = streamer or self._stream

    @staticmethod
    def _run(argv):
        """Run `argv` and return stdout, raising if it fails or hangs."""
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=DEFAULT_TIMEOUT
            )
        except subprocess.TimeoutExpired as exc:
            raise OSError(f"{' '.join(argv)} timed out") from exc
        if result.returncode != 0:
            raise OSError(f"{' '.join(argv)} failed: {result.stderr.strip()}")
        return result.stdout

    @staticmethod
    def _stream(argv, deadline=None):
        """Yield stdout lines from a long-running command, until `deadline`.

        `deadline` is a time.monotonic() value; None blocks indefinitely.
        Uses select() rather than iterating the pipe directly, since a plain
        readline() blocks with no way to bound how long it waits.
        """
        process = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
        )
        # stdout=PIPE guarantees a pipe, but Popen's own type only promises
        # one when text/bytes mode is known statically, which it is not
        # here -- assert once rather than re-deriving "not None" every use.
        assert process.stdout is not None
        stdout = process.stdout
        try:
            while True:
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return
                    ready, _, _ = select.select([stdout], [], [], remaining)
                    if not ready:
                        return
                line = stdout.readline()
                if not line:
                    return
                yield line
        finally:
            process.terminate()
            process.wait()
            stdout.close()

    def close(self):
        """Nothing to release; each call is its own process."""
        pass

    def get_tree(self):
        """The full window tree."""
        return json.loads(self._runner(["swaymsg", "-t", "get_tree", "-r"]))

    def get_outputs(self):
        """Every output sway knows about."""
        return json.loads(self._runner(["swaymsg", "-t", "get_outputs", "-r"]))

    def run_command(self, command):
        """Run a sway command string."""
        return self._runner(["swaymsg", *command.split()])

    def subscribe(self, events=("window",), deadline=None):
        """Yield event payloads, skipping blank and malformed lines."""
        argv = ["swaymsg", "-t", "subscribe", "-m", json.dumps(list(events))]
        for line in self._streamer(argv, deadline):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


# -- Hyprland --------------------------------------------------------------


def _hypr_dir():
    """Locate the running Hyprland instance's socket directory."""
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not signature:
        raise OSError("HYPRLAND_INSTANCE_SIGNATURE is not set")
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    for base in ([f"{runtime}/hypr"] if runtime else []) + ["/tmp/hypr"]:
        path = f"{base}/{signature}"
        if os.path.isdir(path):
            return path
    raise OSError("no Hyprland instance directory found")


class HyprlandSocket:
    """Hyprland's request socket: write a command, read until EOF."""

    def __init__(self, path=None):
        """Locate the Hyprland request socket."""
        self.path = path or f"{_hypr_dir()}/.socket.sock"

    def close(self):
        """Nothing to release; each request opens its own socket."""
        pass

    def _request(self, command):
        """Send a command and read the reply until end of stream."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(DEFAULT_TIMEOUT)
            sock.connect(self.path)
            sock.sendall(command.encode())
            chunks = []
            while True:
                chunk = sock.recv(8192)
                if not chunk:
                    break
                chunks.append(chunk)
        return b"".join(chunks).decode()

    def json(self, command):
        """Run a command in JSON mode and parse the reply."""
        return json.loads(self._request(f"j/{command}") or "null")

    def dispatch(self, command):
        """Run a dispatch command."""
        return self._request(f"dispatch {command}")

    def clients(self):
        """Every open window."""
        return self.json("clients")

    def active_window(self):
        """The focused window, as raw JSON."""
        return self.json("activewindow")

    def monitors(self):
        """Every monitor."""
        return self.json("monitors")

    def active_workspace(self):
        """The currently focused workspace, as raw JSON."""
        return self.json("activeworkspace")


class HyprlandCLI:
    """Fallback transport using hyprctl."""

    def __init__(self, runner=None):
        """Wrap hyprctl, optionally with an injected runner."""
        self._runner = runner or SwayCLI._run

    def close(self):
        """Nothing to release; each call is its own process."""
        pass

    def json(self, command):
        """Run a command in JSON mode and parse the reply."""
        return json.loads(self._runner(["hyprctl", "-j", command]) or "null")

    def dispatch(self, command):
        """Run a dispatch command."""
        return self._runner(["hyprctl", "dispatch", *command.split()])

    def clients(self):
        """Every open window."""
        return self.json("clients")

    def active_window(self):
        """The focused window, as raw JSON."""
        return self.json("activewindow")

    def monitors(self):
        """Every monitor."""
        return self.json("monitors")

    def active_workspace(self):
        """The currently focused workspace, as raw JSON."""
        return self.json("activeworkspace")


# -- selection -------------------------------------------------------------


def connect_sway():
    """Prefer the socket; fall back to swaymsg; None if neither works.

    The CLI fallback still requires SWAYSOCK or I3SOCK to be set: swaymsg
    being on PATH proves nothing by itself -- it could be a leftover
    install, or present alongside a different running compositor -- so it
    is only trusted once sway/i3 is the one actually advertising a socket.
    """
    try:
        return SwaySocket()
    except OSError:
        pass
    if not (os.environ.get("SWAYSOCK") or os.environ.get("I3SOCK")):
        return None
    import shutil

    return SwayCLI() if shutil.which("swaymsg") else None


def connect_hyprland():
    """Open a Hyprland transport: socket first, then hyprctl, else None."""
    try:
        socket_path = f"{_hypr_dir()}/.socket.sock"
        if os.path.exists(socket_path):
            return HyprlandSocket(socket_path)
    except OSError:
        pass
    import shutil

    return HyprlandCLI() if shutil.which("hyprctl") else None


# -- niri ------------------------------------------------------------------


def _niri_reply(reply):
    """Unwrap one niri Reply, raising on the error arm.

    A Reply is `{"Ok": <response>}` or `{"Err": "message"}`, and a Response
    is an externally tagged enum -- `{"Windows": [...]}` -- or a bare string
    for the payload-free `"Handled"`. Unwrapping the single key here keeps
    every caller working in plain payloads rather than envelopes, and makes
    the two transports agree: `niri msg --json` prints the payload already
    unwrapped, so only the socket sees the envelope at all.
    """
    if not isinstance(reply, dict) or ("Ok" not in reply and "Err" not in reply):
        raise OSError(f"unexpected niri reply: {reply!r}")
    if "Err" in reply:
        raise OSError(f"niri refused the request: {reply['Err']}")
    body = reply["Ok"]
    if isinstance(body, dict) and len(body) == 1:
        return next(iter(body.values()))
    return body


class NiriSocket:
    """niri's IPC socket: one JSON line out, one JSON line back.

    A connection per request, like HyprlandSocket and unlike SwaySocket.
    niri documents that requests are processed separately with time passing
    between them, so holding one connection open buys no atomicity -- and a
    held connection cannot serve requests once an event stream has started,
    which is the trap the sway backend needs a second socket to avoid.
    """

    def __init__(self, path=None):
        """Locate the niri socket, from `path` or $NIRI_SOCKET."""
        resolved = path or os.environ.get("NIRI_SOCKET")
        if not resolved:
            raise OSError("NIRI_SOCKET is not set")
        self.path: str = resolved

    def close(self):
        """Nothing to release; each request opens its own socket."""
        pass

    def _connect(self):
        """Open a connection to the niri socket."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(DEFAULT_TIMEOUT)
        try:
            sock.connect(self.path)
        except OSError:
            sock.close()
            raise
        return sock

    @staticmethod
    def _send(sock, request):
        """Write one request as a single JSON line."""
        sock.sendall(json.dumps(request).encode() + b"\n")

    @staticmethod
    def _lines(sock, pending=b""):
        """Yield complete lines from `sock`, buffering partial reads.

        Every reply and every event is one line, but nothing guarantees a
        recv() lands on that boundary -- a long window list arrives in
        several chunks, and two events can arrive in one.
        """
        while True:
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                if line.strip():
                    yield line
            chunk = sock.recv(8192)
            if not chunk:
                if pending.strip():
                    yield pending
                return
            pending += chunk

    def request(self, request):
        """Send one request and return its unwrapped payload."""
        with self._connect() as sock:
            self._send(sock, request)
            for line in self._lines(sock):
                return _niri_reply(json.loads(line))
        raise OSError("niri closed the connection without replying")

    def windows(self):
        """Every open window."""
        return self.request("Windows")

    def outputs(self):
        """Every output, as a name -> output mapping."""
        return self.request("Outputs")

    def workspaces(self):
        """Every workspace."""
        return self.request("Workspaces")

    def action(self, name, **arguments):
        """Perform one niri action, named as in the IPC enum."""
        return self.request({"Action": {name: arguments}})

    def event_stream(self, deadline=None):
        """Yield events until `deadline`, a time.monotonic() value.

        Requesting the stream ends this connection's request/reply life --
        niri stops reading requests and writes events forever -- so this
        opens its own socket and never shares one with request().
        """
        sock = self._connect()
        try:
            self._send(sock, "EventStream")
            lines = self._lines(sock)
            for line in lines:
                _niri_reply(json.loads(line))  # the "Handled" acknowledgement
                break
            while True:
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return
                    sock.settimeout(remaining)
                else:
                    sock.settimeout(None)
                try:
                    line = next(lines)
                except (StopIteration, TimeoutError):
                    return
                yield json.loads(line)
        finally:
            sock.close()


class NiriCLI:
    """Fallback transport using `niri msg`.

    `niri msg --json` prints the response payload already unwrapped, so
    these return exactly what NiriSocket does after _niri_reply.
    """

    def __init__(self, runner=None, streamer=None):
        """Wrap `niri msg`, optionally with injected runner and streamer."""
        self._runner = runner or SwayCLI._run
        self._streamer = streamer or SwayCLI._stream

    def close(self):
        """Nothing to release; each call is its own process."""
        pass

    def _json(self, *arguments):
        """Run one `niri msg --json` subcommand and parse its output."""
        return json.loads(self._runner(["niri", "msg", "--json", *arguments]) or "null")

    def windows(self):
        """Every open window."""
        return self._json("windows")

    def outputs(self):
        """Every output, as a name -> output mapping."""
        return self._json("outputs")

    def workspaces(self):
        """Every workspace."""
        return self._json("workspaces")

    def action(self, name, **arguments):
        """Perform one niri action.

        The CLI spells actions in kebab case and takes their fields as long
        options, so FocusWindow{id: 7} becomes `action focus-window --id 7`.
        """
        argv = ["niri", "msg", "action", _kebab(name)]
        for key, value in arguments.items():
            argv += [f"--{_kebab(key)}", str(value)]
        return self._runner(argv)

    def event_stream(self, deadline=None):
        """Yield events, skipping blank and malformed lines."""
        argv = ["niri", "msg", "--json", "event-stream"]
        for line in self._streamer(argv, deadline):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def _kebab(name):
    """CamelCase or snake_case to the kebab case the niri CLI expects."""
    if "_" in name:
        return name.replace("_", "-")
    return re.sub(r"(?<!^)(?=[A-Z])", "-", name).lower()


def connect_niri():
    """Prefer the socket; fall back to `niri msg`; None if neither works.

    Same rule as connect_sway: `niri` being on PATH proves nothing on its
    own, so the CLI is only trusted once NIRI_SOCKET says niri is the
    compositor actually running.
    """
    try:
        transport = NiriSocket()
        if os.path.exists(transport.path):
            return transport
    except OSError:
        pass
    if not os.environ.get("NIRI_SOCKET"):
        return None
    import shutil

    return NiriCLI() if shutil.which("niri") else None
