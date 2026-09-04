# Changelog

All notable changes to pyguitest are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[semantic versioning](https://semver.org/spec/v2.0.0.html) — with the usual
0.x caveat that the API may still change between minor versions.

## [Unreleased]

### Changed

- **`scroll()` now means the same thing on every backend**, which took a
  change to two of them. It counts whole wheel detents and `dy > 0`
  scrolls up.

  Neither half was true before. `LibeiBackend` called `scroll_delta`,
  which takes logical *pixels*, so `gui.scroll(0, 3)` was three wheel
  notches on X11, xdotool, uinput and the portal, and three pixels on
  `eiinput`. And the direction split four/two: X11 clicks buttons 4/5 and
  uinput emits `REL_WHEEL`, both taking positive as up, while the portal
  and libei follow `wl_pointer`, where positive is down. A caller could
  not write one scroll that behaved the same way twice.

  The X11 reading won -- on numbers, and because this package is the
  successor to an X11 module. `eiinput` sends `scroll_discrete` (120
  units to the detent) and both it and `portal` negate `dy`. Horizontal
  is untouched: positive is right in both conventions, and flipping both
  axes would have introduced a second bug while fixing the first. The
  convention is now stated in `GUIBackend.scroll`, `Session.scroll` and
  [docs/input.md](docs/input.md).

  **This changes behaviour for existing callers of those two backends**:
  a `portal` or `eiinput` scroll now goes the other way, and an `eiinput`
  scroll moves 120x further per unit. Both were previously inconsistent
  with the majority rather than documented, so there is no correct code
  to migrate -- but code written against the old `eiinput` behaviour will
  need its magnitudes rescaled.

- The `eiinput` extra now requires `python-libei>=0.4.0`, up from `0.3.0`.
  Two things in that release matter here and neither can be worked around
  from this side: a negotiation that failed part-way used to leave the
  portal session `CreateSession` had made open — `negotiate()` raises
  rather than returning the object whose `close()` would end it, so a
  declined consent dialog stranded one session per attempt — and the
  `timeout` this backend passes now bounds every leg of a round trip
  rather than only the wait for the portal's reply, which is what
  `_PORTAL_TIMEOUT`'s docstring has claimed all along.

- A backend factory can now raise `BackendUnavailable` from construction,
  with the real reason, instead of swallowing it into a bare `None`. Six
  factories (atspi, gnomeshell, x11, portal, eiinput, portalcapture) used to
  catch their own construction failure and return `None`, which is right
  for automatic composition -- one member declining is not an error -- but
  wrong for `connect(backend=name)`: naming a backend is a request, and the
  caller got `select()`'s own generic "backend 'x' cannot drive this
  session" instead of whatever the backend actually said, e.g. why
  `eiinput` couldn't negotiate. The catch is now centralized in one place
  (`_auto_build`) that only automatic composition goes through; naming a
  backend lets its own exception propagate verbatim. A factory that simply
  cannot apply here at all -- wrong compositor, a library not importable --
  still returns `None` either way.
- `send_keys`'s grammar moved to a new `sendkeys.py` as `KeySender`, with
  no change to what the grammar does. It was one method holding three
  closures over four pieces of shared mutable state, scoring 26 on
  cyclomatic complexity -- the worst in the package, and the reason the
  new `C901` ceiling exists. `Session.send_keys` keeps the documentation,
  since that is where a caller looks for it; the 31 grammar tests pass
  unchanged, which is what makes the move safe to claim as behaviour-
  preserving. `examples/09_gui_spy.py` and
  `examples/_xtest_input_validate.py` were split the same way, each mode
  and each validated capability becoming its own function.

### Added

- `scripts/headless-session.sh` runs a command inside a private
  `gnome-shell --headless --virtual-monitor WxH` on its own session bus.
  Nothing appears on screen and a shell already running is untouched, so
  it works on a developer's desktop as well as on a bare CI runner -- and
  it makes the COMPOSITOR tier testable without a human for the first
  time. `./scripts/headless-session.sh ./scripts/validate-gnome-extension.sh`
  is green end to end: window move/resize composing over six rounds, a
  capture matching the window exactly, the `new`/`title`/`close` window
  events, and `window_at()` hit-testing, all unattended.

  Three things had to be closed to get there, each of which made a
  "headless" run quietly wrong rather than obviously broken. `DISPLAY` is
  unset for the command, because headless mode starts its own XWayland but
  exports its display to nothing -- leaving the outer value in place points
  every X11 tool at the developer's real X server. The Wayland socket and
  its `.lock` are removed on the way out, since a signalled shell leaves
  both behind and the next run cannot bind. And the script waits for the
  shell's *extensions* to load, not just for its bus name: those are about
  4.4 seconds apart here, and a connect in between fails with the same
  error an uninstalled extension gives.

- A `compositor` job in CI, running that validation inside the headless
  session so the COMPOSITOR tier stays working rather than having worked
  once. On `workflow_dispatch` and a nightly schedule, not on pushes:
  whether a GitHub runner can host `gnome-shell --headless` is untested,
  and an unproven job has no business failing someone's pull request. It
  fails loudly if the backend battery never ran, the same way the portal
  and ImageMagick jobs fail on a silent skip.

  Its first run settled the question it was built to answer: **a GitHub
  runner can host `gnome-shell --headless`**, with no GPU, software
  rendering and no logind session. The compositor came up, the extension
  loaded and exported its D-Bus object, `GnomeShellBackend` constructed
  with every window capability, and move, resize, restore and hit-testing
  all passed there.

- The extension now logs the four rectangles behind a window capture --
  frame, buffer, texture and the crop it computed. The crop was derived on
  one machine and disagreed on the second: a CI runner with software
  rendering produced a 628x429 image for a 600x400 window, an x scale of
  1.047 against a y scale of 1.073, which is not a scale factor at all.
  The arithmetic assumes texture-size/buffer-size *is* the device-pixel
  ratio, and that holds only while `buffer_rect` genuinely includes the
  shadow margin; where it equals `frame_rect`, the ratio becomes
  meaningless and the "crop" expands the image instead of trimming it.
  Which of those is happening is exactly what nothing reported, so this
  logs it rather than guessing at a fix. The CI job prints the shell log
  when the run fails.

- `scripts/probe-window.py`: one plain GTK4 window, held open until killed,
  for the window-control checks to point at.
  `validate-gnome-extension.sh` spawns it when nothing is open -- which is
  always the case in a headless session -- instead of exiting early with
  "open a window and re-run". It waits for a window with a real size, not
  merely a listed one: Mutter publishes a toplevel to `ListWindows` before
  the client's first configure round trip, so an unlucky first look gets a
  real window whose `frame_rect` is still 0x0, and the geometry battery
  then measures move and resize against a size that never existed.

- `Environment.input_transport`: what will actually inject input here,
  which is a different question from `preferred_input` and now the one
  `summary()` answers. See Fixed below.

- `examples/_portal_clipboard_validate.py`, the live check the portal
  clipboard shipped without. Its write half hands the portal no bytes --
  `SetSelection` declares ownership and a GLib loop on a daemon thread
  answers each `SelectionTransfer` -- so a fake connection cannot tell
  whether it works, and a failure reads as an *empty* clipboard to the
  whole desktop rather than as an error. The script settles it the only way
  it can be settled: a separate process pastes what was written. It also
  counts the transfers the portal actually asked for, runs the read
  direction first as a control so a failure can be attributed, and checks
  that the selection dies with the session as documented.

- `docs/upstream.md`: the two Wayland protocol gaps worth filing upstream,
  written as issue text rather than as notes --
  `ext-image-copy-capture-v1` in Mutter, and a foreign-toplevel geometry
  protocol that nobody implements. The second is the one number standing
  between an accessible tree and clicking what it finds.

- Clipboard on GNOME, at last: `PortalBackend` takes `clipboard=True` and
  then serves `Capability.CLIPBOARD` through
  `org.freedesktop.portal.Clipboard`. Mutter implements no
  wlr-data-control, so wl-clipboard cannot work there and GNOME has had no
  clipboard path at all -- `connect(backend="portal", backend_options={
  "clipboard": True})` is now one.

  `RequestClipboard` is issued between `SelectDevices` and `Start`, and
  that ordering is the whole constraint: the portal binds clipboard access
  to the session at `Start`, so requesting it afterwards is ignored and the
  session comes up without access, silently. It rides the RemoteDesktop
  session this backend already negotiates, so it costs no second consent
  dialog and no new dependency -- `portal.py` owns its own D-Bus
  negotiation and needed nothing from python-libei.

  Reading is a `SelectionRead` and a pipe. Writing has a *lifetime*, which
  is the part worth knowing before relying on it: `SetSelection` declares
  ownership and carries no content, and the portal then asks for the bytes
  once per paste via a `SelectionTransfer` signal. Something must be
  listening for as long as the selection is owned -- the same problem
  `wl-copy` and `xclip` solve by forking a daemon. This cannot fork, since
  the session belongs to this process, so it runs a GLib loop on a daemon
  thread with its own `MainContext` (not the default one, which a caller's
  own loop may own). The consequence: **the clipboard holds only while the
  Session does**, where the CLI backends leave a daemon behind that
  outlives them.

  `primary=True` raises rather than being served from the clipboard: the
  portal interface has no PRIMARY selection at all, and the two are
  independent by design, so answering one with the other would answer a
  different question than was asked.
- `Capability.INPUT_SYNC` and `Session.sync()`, the honest replacement for
  the `time.sleep()` every GUI script grows after injection. `eiinput` is
  the one backend that can offer it: libei has a real round trip
  (`ei_new_ping()`, answered by a `PONG`), and the reply cannot be sent
  before EIS has read past everything queued ahead of the ping -- so
  receiving it proves the compositor *consumed* the events rather than
  merely that the socket accepted them.

  Scoped deliberately. It proves delivery to the compositor and says
  nothing about whether the application processed or repainted them;
  `wait_until` on the state you care about is what answers that, and
  nothing in this stack reports a repaint at all. Returns True on
  confirmation and False on timeout like the rest of the wait family, and
  raises where no backend can round trip rather than returning a
  meaningless False. Needs libei 1.4+, probed by building a Ping rather
  than by reading a version, since the binding resolves the symbol lazily
  and an older library raises rather than reporting anything -- the
  capability is withheld there instead of faked.
- `Capability.SCREEN_INFO` on GNOME, via `GnomeShellBackend.screens()` --
  the last failing tier-2 line, and the only capability gap that was ever
  just a matter of not asking. Mutter's own
  `org.gnome.Mutter.DisplayConfig.GetCurrentState` answers any session-bus
  client with the full monitor, mode and scale list and raises no prompt;
  nothing needed to be installed or negotiated. It is also the only source
  in this package that reports a real *scale* -- X11 has no notion of one,
  so an XWayland session's outputs come back at 1.0 however the desktop is
  actually configured.

  Logical monitors, not physical ones, and the size is derived rather than
  read: `GetCurrentState` reports the panel's pixel mode and the logical
  monitor's scale and transform separately, so the logical size is the mode
  divided by the scale (under logical layout mode) with the axes swapped on
  a 90/270-degree rotation. That is what keeps `screens()` in the same
  coordinate space as `geometry()` and `window_at()` -- verified live, where
  a maximized window sat at `(0, 32, 1920, 1048)` against a 1920x1080
  screen. Reporting the raw mode instead would have put the two in
  different coordinate systems on exactly the fractional-scale desktops
  where nobody would notice by eye. A mirrored pair is one `Screen`; a
  disabled output is none, matching `NiriBackend.screens()`.

  One limitation worth knowing: this rides on `GnomeShellBackend`, which
  will not construct without the `pyguitest-window-control` extension, so a
  GNOME session without it gets no `screens()` either -- an artefact of
  where the method lives rather than of what Mutter allows.
- `primary=True` on `get_clipboard()`/`set_clipboard()`, reaching the
  X11/Wayland PRIMARY selection -- what a middle-click paste reads --
  independently of the clipboard proper. `Session.assert_clipboard(expected,
  primary=False)` checks either one for an exact match, raising the new
  `ClipboardMismatch`. Every existing `CLIPBOARD_TOOLS` member (wl-copy,
  xclip, xsel) already speaks PRIMARY as a second named selection rather
  than a separate command, so this is one argument on the existing backend
  rather than a new one.
- `Session.assert_accessible()`, `assert_no_missing_accessible_names()` and
  `assert_no_duplicate_accessible_names()`, raising the new
  `AccessibilityViolation`. The same defect shows up twice over: a control
  with no accessible name is read out by a screen reader as just "button",
  and cannot be found by `gui.button("Save")` either -- so it is worth
  asserting in an ordinary GUI test rather than only in an audit. Duplicates
  matter for the same reason: two buttons both called "Delete" mean whichever
  the locator returns first is a coin toss, and a test written against it
  fails the day the order changes.

  Which roles are *required* to carry a name is the whole design, and it is
  one visible constant, `NAMED_ROLES`, overridable per call with `roles=`.
  The line is drawn at "act on it by name": pressable controls, text entries,
  choices, tabs and sliders. Content roles (label, heading, paragraph, table
  cell, list item) carry their text as content rather than as a name;
  structural ones (panel, viewport, separator, toolbar) are furniture; images
  and icons are frequently decorative. Only visible elements are considered.
  All of that errs toward quiet on purpose -- an assertion nobody can switch
  on catches nothing, and a hidden dialog's worth of unnamed widgets would
  bury the findings that matter.
- `Session.refresh_window(window)` and `Session.is_window_open(window)`, plus
  equality and hashing on `Window`. A `Window` is a snapshot: its title was
  true when it was taken and the handle beneath it may since have gone, and
  both bite in practice — a live run had `geometry()` fail with "no window
  with id 106" for a window the list had just returned, and an editor that
  renamed itself the moment it had content left a captured `Window`
  describing nothing. `refresh_window` exchanges a stale one for the current
  one, or `None` if it has closed. Equality is by handle *and* backend
  identity, never by title (two windows can share a title, and a title can
  change while the window stays put) and never across backends (two members
  of one composite can both hand out small integer handles, and `106 == 106`
  across them would be a confident false match). `wait_window_close` now
  uses `is_window_open` rather than its own private copy of the same idiom.
  What none of this addresses is `windows()` and the accessibility tree
  disagreeing about *membership* — that needs a way to relate the two
  sources, not a way to compare windows within one, and is left alone.
- `doctor` warns when KDE's GTK applications cannot publish elements, and
  `debug` reports the setting behind it on every desktop
  (`toolkit_accessibility()` in `session.py`). GTK loads its AT-SPI bridge
  only when `org.gnome.desktop.interface toolkit-accessibility` is true;
  with it off on KDE, element queries come back empty and *nothing* else
  reports a problem — packages present, `can_use_atspi` true, dogtail
  connecting happily. Scoped to KWin on purpose: measured the same day, a
  GNOME session had the setting off with AT-SPI working perfectly, so "off"
  alone is not evidence of a fault and a hint that fired on it would be
  wrong on GNOME. The probe is a function rather than an `Environment`
  field because reading the value means importing PyGObject, and `detect()`
  deliberately imports nothing — it uses `find_spec` throughout, so a plain
  `connect()` still pulls in no GNOME stack it was not already using.
- `Capability.WINDOW_EVENTS` on KDE, via a new `KWinEventsBackend`. Closes
  the one real capability gap KDE had left: `kdotool` has no
  event-subscription mechanism of its own (checked against `kdotool
  --help` directly — purely query/action, unlike `xdotool behave`), so
  `wait_for_window`/`window_events` had no path there at all. An ad hoc
  KWin script (`_kwin_window_events.js`, shipped as package data, loaded
  via `org.kde.kwin.Scripting.loadScript()`/`Script.run()` at
  construction — no install-and-enable step, unlike the GNOME Shell
  extension this complements) calls back into a small D-Bus service this
  backend hosts itself, inverting the GNOME extension's architecture: KWin
  scripting has no documented way to register a new D-Bus interface or
  emit its own signal, only to call an existing one (`callDBus`), so the
  script is the client here rather than the server. Composed alongside
  `KdotoolBackend` rather than merged into it, the same way
  capture/clipboard/imagesearch already sit next to it. Live-validated end
  to end on KDE Plasma 6/KWin — mechanism-level
  (`scripts/validate-kwin-events.sh`) and through the real backend
  (`examples/_kwin_events_validate.py`) — see `docs/validation.md`.

### Fixed

- **A clipboard read could fail with an `AttributeError`** from inside a
  D-Bus helper. Found the first time the new validation script ran
  against a real desktop (2026-09-04): `SelectionRead` answered with an
  ordinary `(h)` reply and **no file descriptor list**, so the code read
  `.get` off `None`. It now raises a typed error saying what happened,
  and saying that the condition is usually transient -- a selection
  changing hands answers this way while the hand-over is in flight, so
  the right response is to retry, not to conclude the clipboard is empty.

  Reading also tries several MIME spellings in turn now
  (`text/plain;charset=utf-8`, `text/plain`, `UTF8_STRING`, `STRING`).
  That went in on the theory that the failure was a type mismatch, since
  `xclip` advertises only `TARGETS` and `UTF8_STRING` -- and a later run
  disproved it by reporting that `text/plain;charset=utf-8` answers on
  the first try. Mutter maps X11 atoms onto MIME types, as it should. The
  list is kept as a defensive measure for other portal implementations,
  costing nothing when the first type answers, and its docstring says
  plainly that no spelling below the first is known to be needed.

  The same run proved the half that was actually in doubt: a separate
  process pasted back exactly what `set_clipboard()` wrote, with
  `SelectionTransfer` firing five times to produce it.

- **The same read could also return `None` instead of text**, which
  reached the caller as `'NoneType' object has no attribute 'decode'`.
  Found on the next run, once the fix above stopped hiding it: the
  descriptor the portal hands back can be **non-blocking**, and
  `BufferedReader.read()` answers None rather than bytes when a
  non-blocking read has nothing ready yet -- so `os.fdopen(fd).read()`
  only ever worked because the owner had usually already written. Reading
  now waits on the descriptor and drains it to EOF, bounded at five
  seconds: the writer is another application reached through the portal,
  and a clipboard read that blocks forever inside a test run is the worst
  of the available failures.

- **`PortalBackend.set_clipboard`'s documented lifetime was wrong.** It
  said a script that sets the clipboard and exits "leaves nothing
  behind". Measured: after `close()` the content was still pasteable, and
  no transfer fired to produce it -- something downstream had cached the
  bytes, which on GNOME is Mutter's XWayland selection bridge. What the
  session can honestly promise is that it stops *serving*; what a given
  application sees afterwards is not ours to claim. Corrected there, in
  `backends/clipboard.py`, and in the `doctor` hint.

- **`connect()` could abort the process** -- SIGABRT, core dumped, no
  exception -- on any session with no reachable accessibility bus.
  libatspi answers an unreachable bus with `g_error()`, which is
  `abort()`, and `import dogtail.tree` reaches that path on the way in:
  the module builds its `root` from `pyatspi.Registry.getDesktop(0)` at
  import time. No `try`/`except` around the import can help. `atspi.py`
  now asks `gdbus` whether `org.a11y.Bus` answers *before* importing
  anything, and declines the backend with a typed `BackendUnavailable`
  naming the bus if it does not. A subprocess rather than Gio in-process
  for the same reason `session.toolkit_accessibility` shells out:
  importing Gio caches the session bus for the life of the process. Found
  in a headless GNOME session, but reachable in any container or CI
  runner.

  `pyguitest debug` now reports the same answer on an `a11y bus` line --
  the one AT-SPI fact that could not previously appear in a bug report,
  because asking for it ended the process. `has_atspi` beside it says the
  *library* is installed, which is a different claim.

- `ExternalTool.version()` printed a failed run's stderr as the tool's
  version. Seen in a real `pyguitest debug` on a Wayland session, where
  xclip's version column read `xclip: Error: Can't open display: (null)`.
  stderr is still read when stdout is empty -- several of these tools
  report their version there -- but only when the command actually
  succeeded.

- `Environment.summary()` reported `input none available` on a machine
  injecting input perfectly well, while the `mechanisms` line two rows
  below it said `uinput`. `preferred_input` ranks *tools*, and answers
  None on a session served by in-process uinput; the summary line was
  asking it the wrong question. The new `input_transport` answers the
  right one, mirroring `backends._input_factory`'s own order -- keymap-safe
  tools, then in-process uinput, then keymap-unsafe tools, with libei last
  and marked opt-in, since automatic composition never selects it.

- `pyguitest debug` never reported the clipboard tools. `_debug_data`
  asked about four of the five groups in `tools.py`, so a pasted bug
  report about the clipboard said nothing about wl-copy, xclip or xsel --
  while `environment.clipboard_tools`, a few lines above in the same
  output, listed them.

- `pyguitest doctor` was silent on a session with no clipboard path at
  all. `Environment.can_use_clipboard` existed and nothing consulted it.
  It now yields a hint, and the advice differs by desktop, because on
  GNOME there is nothing to install: Mutter implements no
  wlr-data-control, so the hint points at
  `connect(backend="portal", backend_options={"clipboard": True})`
  instead of naming a package that would run, exit 0, and copy nothing.

- Session classification now trusts `WAYLAND_DISPLAY`/`DISPLAY` over a
  possibly-stale `XDG_SESSION_TYPE`, not the other way around. Found live
  on a machine offering several session types at login (Plasma X11,
  Plasma Wayland, GNOME): logind reported `XDG_SESSION_TYPE=wayland` for
  a session the user had explicitly chosen as `plasmax11`
  (`DESKTOP_SESSION=plasmax11`), with `DISPLAY=:0` set and
  `WAYLAND_DISPLAY` entirely absent -- no Wayland socket to connect to at
  all. `_classify()` checked the declared type before ever looking at
  `DISPLAY`, so this reported `SessionType.WAYLAND` on a real X11
  session, `X11Backend` never joined the composite, and the eight
  tier-3/tier-6 capabilities only it serves
  (`SCREEN_INFO`, `WINDOW_CAPTURE`, `WINDOW_PID`, `INPUT_STATE_QUERY`,
  `POINTER_QUERY`, `WINDOW_CURSOR_QUERY`, `WINDOW_LOWER`,
  `WINDOW_TITLE_SET`) all read `[ no]` on `01_what_can_i_do.py`, not
  because anything was missing but because a backend that would have
  connected fine was never tried. The declared type is now only a
  fallback for when neither environment variable is set at all.
- `Window` identity is scoped to the composite rather than to one member,
  so a window watched through `WINDOW_EVENTS` compares equal to the same
  window listed through `WINDOW_LIST` when the two come from different
  members. KDE is the first desktop where they do -- `kdotool` lists and
  the new `KWinEventsBackend` watches, both speaking the same KWin UUIDs
  -- and `Window.__eq__` compared backend *identity*, so the two were
  unequal. `Session.is_window_open()` answered False about an open window,
  `refresh_window()` answered None, and `wait_window_close()` returned
  True in 0.00s having consumed no events at all, because it
  short-circuits on `is_window_open` before ever reaching the event loop:
  a test waiting for a dialog to close sailed straight past it. In the
  other direction -- a window from `windows()` -- the close event never
  matched, so the call waited out its whole timeout and got the right
  answer only from the fallback, or hung indefinitely with `timeout=None`.

  `CompositeBackend.__init__` now gives every member one shared identity
  scope, and `Window.__eq__`/`__hash__` compare within it. The tradeoff is
  the case the strict rule was aimed at, and it is worth stating: two
  members of one composite handing out unrelated handles from overlapping
  value spaces can now false-match on `106 == 106`. That is confined to
  members the caller composed deliberately; between unrelated backends,
  and for a backend used on its own, the strict rule is unchanged.

- `PortalBackend` never ended its portal session. There was no `close()` at
  all, so the base class's no-op hook ran and the session stayed live in
  xdg-desktop-portal as a standing input grant -- a session outlives the
  object that negotiated it, living on until `Session.Close()` or until the
  D-Bus connection drops, and that connection is GLib's *shared*
  session-bus singleton, which does not drop when a backend is discarded.
  A process that builds backends repeatedly accumulated one live grant per
  backend. `close()` now ends it (and so does `Session.close()` and the
  context-manager form, which already called through to the backend), a
  session that was *injected* rather than negotiated is left alone since it
  belongs to whoever passed it in, and injecting through a closed session
  raises instead of being sent at a session the portal has dropped.
  Verified over a real bus in `tests/test_portal_dbusmock.py`, not only
  against the stand-in connection: a cleanup path that swallows everything
  is exactly the kind that can look right in Python and put nothing on the
  wire.

- A `PortalBackend` whose negotiation failed after `CreateSession` -- a
  declined `SelectDevices` or `Start`, a request the portal never answered,
  or a Ctrl-C while the consent dialog was up -- left that session behind
  too, and this time nothing could ever close it: `__init__` raises rather
  than returning the object whose `close()` would. `_negotiate_session` now
  closes it on the way out, `KeyboardInterrupt` included, since `Start`
  blocks on a human and Ctrl-C during that wait strands an approved session
  exactly as a decline does. `libei.portal` (used by the `eiinput` backend)
  had the same gap, fixed upstream in python-libei.

- Tier-6 operations are reachable through a composite again -- which is to
  say, reachable at all on a real desktop. `CompositeBackend` routes each
  call to the member that declares the capability, and the five capabilities
  only `X11Backend` serves (`POINTER_QUERY`, `INPUT_STATE_QUERY`,
  `WINDOW_TITLE_SET`, `WINDOW_LOWER`, `WINDOW_CURSOR_QUERY`) were missing
  from that table, with no `__getattr__` to fall through to. So
  `gui.supports(Capability.POINTER_QUERY)` answered `True` -- a member
  really does provide it -- and `gui.pointer_position()` then raised
  `AttributeError`. Since `connect()` composes on every real session, a
  bare `X11Backend` was the only place these ever worked; `examples/
  07_keys_and_pointer.py` and `examples/09_gui_spy.py` both check
  `supports()` and then call straight into the gap. `Session.glide()` and
  `drag()` hit it too, by way of `_origin`, and reported "POINTER_QUERY is
  unavailable" on a session whose own `supports()` had just said otherwise.
- `type_text()` on the `portal` and `eiinput` backends sends Enter for a
  newline, as X11 and uinput already did. Both resolved `"\n"` as an
  ordinary character and both got a real, wrong answer rather than an
  error: `portal.py` produced `0x01000000 | 10`, the Unicode keysym form
  of U+000A, which names no key on any keymap; and `xkb.py`'s
  `keysym_for_char` inherited `xkb_utf32_to_keysym`'s answer of
  `XKB_KEY_Linefeed`, which *is* on the US keymap (evdev 101,
  `KEY_LINEFEED`) and so resolved happily to a key no physical keyboard
  has. `type_text("name\n")` -- filling a field and submitting it, about
  as ordinary as this API gets -- therefore typed the name and then did
  nothing. The other control characters were already right in both places
  and are now asserted so they stay that way.
- `resize_window()` works on niri's `niri msg` fallback transport. niri's
  `SizeChange` is an externally tagged enum over the socket
  (`{"SetFixed": 800}`) and `NiriCLI.action` stringified every argument
  into a long option, emitting `--change "{'SetFixed': 800}"` -- an
  argument no CLI parses. The CLI takes the change positionally in its own
  spelling (`set-window-width --id 7 800`), which is what is sent now; an
  enum variant this does not know is refused rather than stringified. Like
  the rest of that transport, still unexercised against a live niri.
- A wlroots-only input tool is no longer offered to a session that merely
  has an X display. `allow_wlroots_only` was `compositor is WLROOTS or
  x11`, so `wtype` survived discovery on a plain X11 session with no
  Wayland compositor at all, and on GNOME/KDE XWayland -- and being
  keymap-safe it outranked `xdotool`, so a session `xdotool` would have
  driven correctly got a backend that fails on every call. wtype needs
  `zwp_virtual_keyboard_manager_v1`; an X server is not a substitute. A
  wlroots session with XWayland is unaffected, its compositor being
  `WLROOTS` either way. Fixed in both places that make the call:
  `_input_factory`, which picks the backend, and `detect()`, whose
  `input_tools` feeds `preferred_input` and `doctor`.
- `KWinEventsBackend.window_events()` would have hung on every call that
  found nothing queued, ignoring `timeout` entirely -- caught before it
  ever ran against a live KWin, by the unit tests written alongside it. An
  early draft pumped `GLib.MainContext.default().iteration(True)` in a
  plain loop; with nothing else scheduled on the context (which is exactly
  the state a `connect=False` test backend is in, deliberately, so those
  tests could exercise the event queue with no real KWin at all),
  `iteration(True)` blocks for a source that will never fire. Fixed by
  mirroring `GnomeShellBackend.window_events()`'s proven `GLib.MainLoop` +
  `timeout_add` + wake-on-event shape instead.
- The GNOME Shell extension's window capture no longer breaks on Mutter 51,
  which removed `Meta.WindowActor.get_image` with no drop-in replacement --
  the id-0 capability probe correctly reported `WINDOW_CAPTURE` unsupported
  rather than failing silently, but that meant no capture at all on Shell
  >=51. `pyguitest-window-control` now uses `paint_to_content()` +
  `Shell.Screenshot.composite_to_stream()` on those shells, a version fork
  alongside the unchanged `get_image` path for Shell <=50. The first live
  run against the new path found a second bug: the composited image was
  the actor's whole allocation, shadow margin included (+50px on both
  axes versus the window's real size), not just the visible frame -- fixed
  by cropping against `get_frame_rect()`. Both the API pair and the crop
  are now live-validated on a GNOME Shell 51.beta session with
  `scripts/validate-gnome-extension.sh`'s new capture check (a captured
  921x1035 window now comes back as an exact 921x1035 PNG).

## [0.2.0] — 2026-09-01

### Added

- `Session.glide()` and `Session.drag()`: pointer motion emitted as a
  stream of events on a wall-clock schedule, rather than the single-event
  teleport `move_mouse()` has always been. A teleport is enough to click
  with and wrong for everything that watches the pointer on its way —
  drag-and-drop arms on the press in both GTK and Qt and only begins once
  later motion crosses a threshold, so press-teleport-release is a click at
  the destination and not a drag; hover reveals and tooltips need
  enter/leave crossings, which a teleport *through* a widget never
  generates; kinetic scrolling and gesture recognisers derive velocity from
  event timestamps; hot corners fire on approach. `duration` and `rate`
  (default 120 Hz) give the event count between them, `via` routes the path
  through waypoints, and `ease` reshapes progress but is off by default
  because constant velocity is what a flick test wants. Randomised
  human-shaped jitter is deliberately not offered: it is a bot-detection
  evasion technique, nothing this side of the compositor looks for it, and
  a path that varies run to run buys a test suite only flakiness. Since
  `POINTER_QUERY` is tier NO_PATH everywhere but X11, `Session` now
  remembers where it last sent the pointer to interpolate from, falling
  back to a live read where one exists and raising — rather than assuming
  `(0, 0)` — where neither is available. See `docs/input.md`.
- `examples/09_gui_spy.py` gained three ways to inspect a point beyond a
  bare `X Y`: `--find IMG.png` locates a control by picture (reusing
  example 08's `IMAGE_LOCATE`) and inspects its centre, so a script can be
  written against `role=`/`name=` even where AT-SPI cannot see a control's
  location on its own; `--tree` lists every accessible element containing
  the point instead of collapsing to the single smallest-area match,
  for when two controls overlap and that heuristic picks the wrong one;
  and `--json` gives one line of machine-readable output instead of the
  formatted report, with `--watch --json` streaming one JSON object per
  click as valid JSONL. The formatted report also gained an `ancestors`
  breadcrumb — the element's containing chain up to the window — for
  when the matched element itself has no name to match on but a parent
  does.
- `examples/_eiinput_validate.py`: a live-validation script for
  `LibeiBackend` (`eiinput`), forced rather than composited, pairing it
  with a separately forced `windows` session for window discovery since
  `eiinput` is input-only. Live-validated on KDE/KWin — see
  `docs/validation.md`. `eiinput` had previously only been run against
  GNOME Shell; this confirms KDE's `xdg-desktop-portal-kde` negotiates the
  same `RemoteDesktop`+libei path.
- `Capability.CLIPBOARD`: read and write the clipboard's text content,
  via `Session.get_clipboard()`/`set_clipboard()`. New `ToolClipboardBackend`
  adapts whichever clipboard tool is installed — `wl-copy`/`wl-paste` on
  wlroots compositors and, confirmed live, KWin too (not a wlroots
  compositor, but implements the same `wlr-data-control-unstable-v1`
  protocol); `xclip`/`xsel` on X11. No member yet on GNOME/Mutter, which
  implements neither that protocol nor a portal path this package can
  reach without a `RemoteDesktop` session — closing that gap needs the
  Shell extension this package already carries for other Mutter-shaped
  gaps, not a new CLI adapter, and is left for later rather than shipped
  half-working. `tools.ExternalTool` gained two fields for this:
  `also_needs` (wl-clipboard is two binaries, not one) and
  `mutter_incompatible` (distinct from `wlroots_only` — KWin needs the
  former, not the latter). `examples/_clipboard_validate.py`: a
  live-validation script, live-validated on KDE/KWin — see
  `docs/validation.md`.
- `examples/_cursor_validate.py`: a live-validation script for
  `X11Backend.is_window_cursor()` (`WINDOW_CURSOR_QUERY`, the fifth
  tier-6 capability, previously only exercised against a fake Xlib).
  Live-validated on KDE/KWin (forced under XWayland) — see
  `docs/validation.md`. The protocol call itself works cleanly, but the
  run surfaced a real practical limitation rather than a bug: on a themed
  (Xcursor) desktop — the default on essentially every modern one —
  `is_window_cursor()` reads `False` regardless of shape queried or what
  is actually on screen, because it compares against the classic X core
  cursor font rather than the theme in use. Documented rather than
  "fixed": matching X11::GUITest's original `IsWindowCursor` contract is
  the point of this backend, so this is an inherent limitation of that
  contract on a modern desktop, not a regression to correct.
- `examples/01_what_can_i_do.py` now follows its capability table with
  install advice for whatever is missing, via `hints.advice()` — the same
  call `pyguitest doctor` already made, now demonstrated in the example
  that says "start here" rather than left for the reader to discover on
  their own. `hints.hints_for()`/`hints.advice()` gained an optional
  `capabilities=` parameter, which enables one new hint: on GNOME/Mutter, a
  missing `WINDOW_PLACEMENT` — never provided by AT-SPI there, since Mutter
  implements no foreign-toplevel protocol for it to read placement from —
  means the `pyguitest-window-control` Shell extension isn't installed or
  enabled, and `doctor`/the example now say so, pointing at
  `gnome-shell-extension/README.md` and `gnome-extensions enable`.
  Detecting this needs a real D-Bus call, which `session.py`'s `detect()`
  deliberately never makes (see its module docstring), so the signal
  instead comes from the capability set a live `connect()` already
  assembled — passed through by `pyguitest.__main__` and the example,
  rather than adding a second, redundant probe.
- A `doctor` hint for the `x11` extra: on an X11 or XWayland session with
  no python-xlib installed, `X11Backend` — the only backend serving the
  tier-6 query capabilities at all — never joins the composite, and
  nothing said so. XWayland counts here, so a mostly-native Wayland
  session still gets the advice; a pure Wayland session with no X11
  connection does not, since those capabilities stay unreachable there no
  matter what is installed.
- `docs/validation.md` records a live run of the tier-6 query capabilities
  under XWayland on GNOME Shell 51.beta. `POINTER_QUERY` and
  `INPUT_STATE_QUERY` both work, but only for X's world — accurate over an
  X surface or while an X client holds focus, and silently stale (the last
  position, an idle modifier) elsewhere, with no error either way. The same
  run drove `UinputBackend` live on GNOME and read the commanded move back
  off a real X client 1px out from rounding, so it is no longer listed as
  never run live. The two places that consume a pointer readback now say so:
  `glide()`'s origin falls back to one, and starts from the wrong place
  rather than raising if it is stale, so prefer an explicit `start` under
  XWayland; and `examples/09_gui_spy.py`'s `--here` is exact over an X11
  client and quietly wrong elsewhere, which had been documented there as an
  unvalidated suspicion and is now measured.
- `docs/validation.md` records the first live run of XTest input injection
  (`X11Backend`'s input half), the last major path in the X11::GUITest
  lineage with no live evidence. A purpose-built probe scored 2 of 21
  checks; the cause was the probe, not XTest -- it set X-level input focus,
  and Mutter delivers injected XTest events by *compositor*-level focus
  instead, which no plain X11 client (this probe, `wmctrl`, `xdotool
  windowactivate`) can set. The run still landed real evidence by accident:
  every event sent arrived, in order, in the terminal that already held
  focus -- a native Wayland client, confirming XTest injection is not
  filtered by client type at delivery on Mutter, only that `X11Backend` has
  no way to aim it at a chosen Wayland window. The existing "XWayland:
  reaches X11 clients only" documentation is unchanged, since it states
  the correct practical limit (X11Backend cannot target such a window)
  rather than the delivery mechanism this run actually measured.
  `examples/_xtest_input_validate.py` -- the probe -- is kept and its
  docstring now says why it fails on Mutter rather than what it was
  written to expect.
- `pyguitest inspect` (plus `--json` and `--window TITLE_REGEX`): the
  accessible tree of every open window, grouped by application, as an
  indented tree or machine-readable data. The tool for seeing what
  `gui.button(...)` or `gui.element(role=..., name=...)` actually has to
  match against without writing a script first — `doctor` answers "what
  can this desktop do" and `debug` answers "what does this machine look
  like", and neither could answer "what is actually in front of me".
  `src/pyguitest/inspect.py` holds `tree_data`/`format_tree` as a pair,
  the same single-source-of-truth split `debug` already uses so the two
  output formats cannot disagree; it is a module rather than CLI-local
  code so the failure bundle below can call `tree_data` directly. It
  reaches windows the way the AT-SPI backend's own `windows()` does —
  `elements(role=...)` for each of `Role.WINDOW_ROLES`, then each match's
  parent is the owning application — rather than through `Window.handle`,
  which is documented backend-private.
- A real locator language on `Session.elements()`/`element()`, which
  previously filtered on `role`/`name` alone: `enabled`, `visible` and
  `description` filters, `name`/`description` accepting a compiled regex
  as well as an exact string (the same `.search()` convention
  `find_window` already used for titles), and `predicate`, an arbitrary
  `Element -> bool`. `predicate` is the escape hatch that makes a
  dedicated relation syntax unnecessary for now: `Element.parent`,
  `.children` and `.is_ancestor_of` already exist, so an
  ancestor/descendant query is `elements(predicate=lambda e:
  label.is_ancestor_of(e))` today. Implemented as a single closure passed
  to dogtail's `findChildren`, which already accepts a plain function as
  readily as its own `GenericPredicate` — so this replaced the old
  predicate construction rather than adding a second code path. Also
  `Session.window_element(title)`, returning the `Element` for a window
  rather than the `Window` handle `find_window` gives, so a search can be
  scoped to one window through the existing `within=`.
- `capture_on_failure` now captures a failure bundle rather than a lone
  screenshot: the accessibility tree, the active window and the focused
  element land beside it, each attached to the exception under its own
  name (`accessibility_tree`, `active_window`, `focused_element`) as a
  file path, or `None` plus `<name>_error` when it could not be taken.
  Each artifact is attempted independently, so one unsupported mechanism
  no longer costs the others — confirmed live on a GNOME Wayland session
  with no capture backend, which produced a full tree/window/focus bundle
  while the screenshot alone failed. The files share the screenshot's
  stem, timestamp and pid rather than moving into a subdirectory, so the
  existing `.screenshot` path contract is unchanged.
- `Session.wait_for_file()`, `wait_for_process()` and `wait_for_idle()`,
  completing the "synchronise against observable state, not against
  time" family that `wait_until`/`wait_for_window`/`wait_for_element`
  already formed. `wait_for_process` matches a regex against a process's
  full `/proc/<pid>/cmdline`, not `comm`, which Linux truncates at 15
  characters and which would therefore silently fail to match most real
  application names. `wait_for_idle` means *CPU*-idle — sampled from
  `/proc/<pid>/stat`'s utime+stime across consecutive polls — and its
  docstring says so plainly, because the "wait until the UI stops
  changing" reading it invites cannot be honoured: no backend here has an
  AT-SPI event stream to watch for that, and inventing one is a
  subsystem, not a convenience method.
- Focus as a first-class concept: `Session.focused()`, `assert_focused()`
  (taking the same filter set as `elements()`), `press_tab()` and
  `assert_tab_order()`, plus a new `FocusMismatch` error for the case the
  existing `*NotFound` errors do not cover — the element exists, it
  simply is not the focused one. `assert_tab_order` focuses the first
  name directly (no injection needed for that step), presses Tab for the
  rest, and waits for focus to settle after each step rather than reading
  it the instant the key is sent, since a focus change is not guaranteed
  to be synchronous with the event causing it.
- `Session.focus_tracking_works()`: a live probe for whether this desktop
  publishes per-widget keyboard focus at all, reported by `pyguitest
  debug` too. It exists because of a negative result recorded in
  `docs/validation.md` — on GNOME Shell 50.4 (Wayland), AT-SPI's FOCUSED
  state was carried by exactly one element on the whole desktop, the
  shell's own toplevel, and by no widget in any application, across
  Ptyxis/VTE, gnome-text-editor/GTK4 and zenity/GTK3, whichever window
  was active and whether or not it had been activated first. The three
  focus methods above are therefore correct and unit-tested but cannot
  match a real widget there, and a caller who does not know that reads
  `FocusMismatch` as a test failure rather than as the
  unsupported-desktop answer it is. Deliberately a method and not a
  `Capability`: capabilities are static, per-backend facts, and this one
  can only be answered by looking at the tree at that moment.
  `active_window()` is unaffected throughout — it reads STATE_ACTIVE on
  frames, a different mechanism, and stayed correct while no widget
  anywhere reported focus.

### Changed

- The `eiinput` backend no longer negotiates the RemoteDesktop portal
  itself. The whole `CreateSession` → `SelectDevices` → `Start` →
  `ConnectToEIS` sequence — about 190 lines of Gio Request/Response
  plumbing, including the fd-returning `ConnectToEIS` call that needs
  `call_with_unix_fd_list_sync` — now lives in python-libei as
  `libei.portal`, released there as 0.3.0 and live-validated end to end on
  both GNOME/Mutter and KDE/KWin before this landed, and `eiinput.py` calls
  `RemoteDesktopSession.negotiate()` and translates that library's
  failures into pyguitest's own (`PortalDeniedError` → `PermissionRequired`,
  a too-old portal or any other portal-side failure → `BackendUnavailable`,
  `PortalTimeoutError` → `PortalTimeout`). No behaviour changes for
  callers: `persist_mode`/`restore_token` worked before this and work now.
  What changes is who owns the code — the two hard-won fixes in it (the
  subscribe-before-call race, and the `session_handle_token` workaround for
  the xdg-desktop-portal 1.22.1 SIGABRT) are now available to anything
  using python-libei rather than being pyguitest's private copy, and this
  package's `eiinput` extra can declare the PyGObject dependency it always
  had (see Fixed). `portalrequest.py` stays exactly where it is: the
  `portal` and `screenshot` backends talk to xdg-desktop-portal with no
  libei involvement at all, so routing them through a libei dependency to
  share one request helper would be backwards. One consequence worth
  knowing before upgrading: an existing install pinned to python-libei
  0.1/0.2 has no `libei.portal`, so `eiinput` now reports itself
  unavailable there — accurately, since there is no longer any negotiation
  code in this package for it to fall back on — until python-libei is
  upgraded to 0.3.0.
- `Application`, returned by `start_app()` in place of a bare
  `subprocess.Popen`: `with gui.start_app([EDITOR]) as app:` terminates the
  program on the way out and kills it if that does not take, plus
  `is_running()` and `restart()`. Stopping a GUI program properly is four
  steps — terminate, bounded wait, kill, wait again — and the fourth is not
  decorative: an editor holding unsaved text answers SIGTERM by opening a
  "Save changes?" dialog instead of exiting, so a bare `terminate()` leaves
  it running. That dance was copied by hand into five example scripts here,
  two of which never got past `terminate()`; all five now delegate. Nothing
  breaks — `Application` forwards the members callers actually used (`pid`,
  `wait`, `terminate`, `kill`, `poll`, `returncode`, the pipes), so existing
  scripts and every documented snippet keep working, and `app.process` is
  the `Popen` itself. Only `isinstance(x, subprocess.Popen)` would notice.
- `connect(backend=[...])`: a sequence of backend names composes exactly
  those, in the caller's order. Forcing a backend by name used to give you
  only that one, so anything wanting an opt-in input backend *and* element
  or window access had to open two sessions and remember which answered
  what — `connect(backend=["eiinput", "atspi"])` is one session that
  injects through the first and finds elements through the second. Three
  things follow from a name being a request rather than a survey of what
  happens to be installed: the caller's order is the precedence (automatic
  composition still orders by registry priority, because nobody expressed a
  preference), a named backend that cannot build raises `BackendUnavailable`
  instead of being quietly skipped, and `backend_options` is keyed by
  backend name — `{"eiinput": {"persist_mode": 2}}` — since a flat dict
  cannot say which backend an option was for. A single name keeps every one
  of its old behaviours, flat options included.
- `examples/_eiinput_validate.py` now uses that form —
  `connect(backend=["eiinput", "windows"])` in place of the two separate
  sessions it opened to pair an input backend with window discovery.
  `_eiinput_portal_validate.py` deliberately keeps two: what it wants is
  "whatever this desktop composes automatically, plus `eiinput`", and a
  fixed list would pin it to one desktop, since `gnomeshell` declines off
  Mutter and a named backend that cannot build raises.
- `CompositeBackend.member(name)`: reach one member's own extras through a
  composite — `gui.backend.member("eiinput").restore_token`, the composed
  spelling of the `gui.backend.restore_token` that `connect()` documents for
  a single named backend. It takes the registry name even for the four
  backends that report the tool they found (`imagesearch:compare`,
  `input:wtype`, `capture:grim`, `clipboard:wl-paste`), so reaching one does
  not mean knowing which tool the machine had. Deliberately explicit rather
  than a `__getattr__` forwarding unknown attributes to whichever member
  happens to have one, which would answer `restore_token` from whichever
  backend came first in the list.
- `examples/_eiinput_portal_validate.py`: the live re-validation for the
  change above — a fresh negotiation, typed text read back through AT-SPI
  (proof the characters reached a native Wayland client, not just that no
  exception was raised), and a second negotiation presenting the
  `restore_token`, which must raise no dialog. `--preflight` verifies the
  cutover with no D-Bus traffic at all, and `--rehearse` runs the
  window/typing half through the default session, so a consent dialog is
  only spent once everything around it is known to work.
- `LibeiBackend.close()` now closes the portal session it negotiated.
  Nothing did before: a portal session lives in xdg-desktop-portal until
  `Session.Close()` or until the D-Bus connection that created it drops,
  and that connection is GLib's shared session-bus singleton, which
  outlives any one session. A long-running process that connected
  repeatedly accumulated live portal sessions until it exited. A
  construction that fails *after* negotiating — the reachable case being
  `_wait_for_devices()` giving up when no device resumes within
  `_DEVICE_TIMEOUT` — now closes that session too, rather than stranding an
  approved session, and the standing input grant it carries, for the life
  of the process: `close()` is never reached on an object a constructor
  never returned.

### Fixed

- `pip install pyguitest[eiinput]` produced a backend that could not
  construct. The extra pulled in python-libei but never declared
  PyGObject, which `eiinput` needed directly to negotiate the portal — it
  worked only where the unrelated `atspi` extra happened to have installed
  it. The extra is now `python-libei[portal]>=0.3.0`, and that library
  declares PyGObject for the module that actually uses it.
- Every portal request could hang the calling thread forever. The wait for
  a `Response` signal ran an unbounded `GLib.MainLoop`, so a portal that
  accepted the call and then stopped answering — a crash mid-request, or a
  `Response` delivered to a path nobody was listening on — left the caller
  blocked with no fd to poll and nothing to interrupt it. Both copies of
  that wait are now bounded and raise the new `PortalTimeout` (60s default,
  overridable; `timeout=None` restores the old unbounded behaviour for a
  caller who wants it). This affected `portalrequest.py`, and so the
  RemoteDesktop *and* Screenshot backends that share it, as well as
  `eiinput.py`'s own copy — where `_PORTAL_TIMEOUT = 60` had been defined
  with a docstring explaining the cap, and then never actually referenced,
  so the constant read as protection that was not there.
- `eiinput.py` ran its main loop unconditionally after issuing a portal
  call, where `portalrequest.py` guards with `if not result`. A response
  that arrives synchronously — during `call_sync`, before `run()` is
  reached — calls `quit()` on a loop that is not running yet, which does
  not stop the subsequent `run()`; the loop then blocked with the reply
  already delivered and nothing left to wake it. Now guarded the same way.
- `hints.advice()` emitted each hint as one unbroken line, running to
  several hundred characters for the longer ones — a wall of text in any
  terminal, and `doctor` output is routinely pasted into bug reports. Now
  wrapped to a fixed width, with the command set off on its own line.
  Commands are never reflowed apart: long words and hyphens are left
  alone so a `pip install 'pyguitest[x11]'` stays copyable.
- The screenshot hint recommended `gnome-screenshot` on KDE, which cannot
  capture there — it reads the X root window (`tools.py`'s `x_root_only`),
  so on a KDE Wayland session it installs a tool that captures nothing.
  This was the same bug already fixed for wlroots/grim, left unfixed on
  the other side; the per-distro package table cannot express it, holding
  one capture name per distribution where the right answer is per desktop.
  KWin now gets `spectacle`.
- Hints on an unrecognised distribution said only "install it through your
  distribution", naming nothing to go looking for, even though the tool or
  project name was already known — the compositor decides `grim` vs.
  `spectacle` before the distro lookup runs, and ImageMagick and the
  AT-SPI stack have upstream names regardless of packaging. Every hint now
  names either a command or something to search for, which a test pins.
- `Session.start_app`/`run_app` documented neither that a string `command`
  runs through the shell (a list does not) nor that `env=` *replaces* the
  environment rather than extending it — the latter silently drops
  `DISPLAY`/`WAYLAND_DISPLAY` on a GUI session, so the launched app never
  appears. Both are now in the docstrings, with the merged-copy form.
- Documentation corrections found by auditing prose against the code:
  the README claimed the package was unpublished because `pyproject.toml`
  carries a `Private :: Do Not Upload` classifier — it carries no such
  classifier, and `CONTRIBUTING.md` says the package is on PyPI, so every
  reader was sent to a checkout install for no reason; `docs/validation.md`
  listed KWin under "not run live" in the same file that documents running
  `KdotoolBackend` live against KDE Plasma 6; `docs/structure.md` repeated
  that and also called `is_window_cursor` untested after it had been
  validated; and `docs/install.md` said `doctor` cannot report the GNOME
  Shell extension, which it now does.
- CI linted `src` and `tests` only, which is how `scripts/` drifted out of
  format unnoticed. `examples/` and `scripts/` — the code readers copy
  from — are now checked too.
- Removed hardcoded counts that had already gone stale (a test count in the
  CI header and another in `test_portal_dbusmock.py`, both roughly half the
  current figure) and the "four of the five tier-6 capabilities" phrasing in
  two documents. The audit distribution table in the README keeps its
  numbers: `tests/test_compat.py` pins those against `LEGACY`, so unlike
  the others they cannot drift silently.

- `hints.advice()`'s screen-capture hint said `(install it through your
  distribution)` even in the one case where nothing can be installed at
  all: a Wayland/Mutter session with no screenshot tool that can reach the
  compositor, where the actual fix is `connect(backend="portalcapture")`,
  not a package — the line directly contradicted the hint's own "no
  screenshot tool can work on this session" text. `Hint` gained an
  `installable` flag so the fallback line is skipped when there is
  genuinely nothing to install.
- The same fallback also named no tool at all when a distro's
  install-command prefix was unknown (an unrecognised `/etc/os-release`),
  even though which tool a given backend needs — `grim` on wlroots,
  `xdotool` on X11, `wtype` on wlroots for input — was already decided
  before the distro lookup ran. `Hint` gained a `packages` field so the
  fallback now names the actual tool even without a runnable install
  command.
- `ToolClipboardBackend.set_clipboard()` hung for the full 15-second
  subprocess timeout on every call. The write tools all fork into the
  background to keep serving the clipboard after the caller returns —
  necessary, and confirmed live independently by hand first — but
  `subprocess.run(..., capture_output=True)` pipes stdout/stderr, and a
  forked child inherits those pipes, so the daemonized grandchild held
  them open long after the tracked process had already exited 0.
  `communicate()` waits for the pipes to reach EOF as well as the process
  exiting, so it hung on a fork that had already succeeded. Fixed by
  giving the write call `DEVNULL` instead of `PIPE` for stdout/stderr
  (the read call, which never forks, is unaffected); confirmed live —
  0.07s instead of a 15s timeout. Caught before this ever shipped, by the
  same live-validation pass that added the capability.
- `Element.checked`/`.selected` were documented as returning `None` where
  the state does not apply, and callers filtering on that got nothing
  filtered: AT-SPI reports both as real booleans on *every* element, unset
  (`False`) on a plain panel exactly as on an unchecked check box, so the
  `None` the docstrings promised never arrives in practice. Caught by the
  first live run of `pyguitest inspect`, which rendered `[unchecked,
  unselected]` against every panel in the tree and buried the annotations
  that meant something. `Element.checkable`/`.selectable` were added —
  AT-SPI's separate CHECKABLE/SELECTABLE states, which is what "does this
  apply here" actually asks — and the docstrings now say to read those
  first. Note this reflects what the accessibility bus reports rather
  than papering over it: GNOME Shell's own toggle buttons do not set
  CHECKABLE, so `inspect` prints no checked/unchecked annotation for
  them, which is accurate rather than a gap to fill with a heuristic.
- `backends/base.py`'s `Element` protocol — the typed contract the
  concrete AT-SPI `Element` is statically checked against — was missing
  `checkable`/`selectable` after those were added to the class, so the
  protocol and its one implementation had silently diverged. Both are
  declared now, along with the new `focused`.
- `docs/validation.md` records a second negative result found while
  investigating the focus one: `window_element()` cannot see an
  Electron/VS Code window that `windows()` lists. The two AT-SPI
  traversal paths disagree for Chromium clients — `windows()` walks
  `root.applications()` and each application's own children, while a
  recursive descendant search from the tree root does not return that
  window at all — so `window_element("Visual Studio Code")` raised
  `WindowNotFound` for a window that was open, active and listed. Not yet
  root-caused, and recorded as such rather than left for the next person
  to rediscover.

## [0.1.1] — 2026-08-30

### Added

- `pyguitest debug` command (plus `--json`): a pasteable diagnostic dump
  for bug reports — package and Python versions, every environment probe
  (not only the ones that came back true), each detected tool's own
  `--version`, and whether the process is running inside a Flatpak,
  toolbox, or other container.
- `Capability.WINDOW_EVENTS` on the `gnomeshell` backend: window
  create/close/title-change, pushed by the Shell extension over a
  `WindowEvent` D-Bus signal rather than polled. `wait_for_window` and
  `wait_window_close` now work the same way on GNOME as they already did
  on sway and niri. Live-validated on GNOME Shell 50.4 — `new`, `title`
  and `close` all confirmed over a real D-Bus connection.
- `examples/09_gui_spy.py`: an element inspector — point at a screen
  coordinate and get back the `role=`/`name=` to script against, plus a
  ready-to-paste snippet. Works on any desktop by passing a coordinate
  directly; `--here` and `--watch` (report on every click, until Ctrl+C)
  are X11-only conveniences, since reading the pointer position or button
  state is a capability no Wayland compositor exposes.
- `examples/_kdotool_validate.py`: a live-validation script for
  `KdotoolBackend` on real KWin, forced rather than composited.

### Fixed

- `KdotoolBackend.geometry()` raised `ValueError` on a window reported
  mid-animation, because KWin can give `getwindowgeometry` a fractional
  position (confirmed live: `545,274.5403238932292`) and the parser assumed
  a plain integer pair. Since a hit-test reads every open window's
  geometry, this could crash `window_at()` over a window unrelated to the
  one under test. Now parsed as `float` and rounded. Live-validated on KDE
  Plasma 6 / KWin (XWayland) — see `docs/validation.md`.
- `CompositeBackend` never overrode `MODIFIER_KEYS`/`KEY_ALIASES`/
  `resolve_char_key`, so `send_keys()` built key names in the base class's
  inherited X11-keysym vocabulary regardless of which member actually
  presses them — e.g. `"Control_L"` for `^`, even when the composite's
  `KEY_EVENT` provider is `UinputBackend`, which only knows evdev names
  like `"LEFTCTRL"`. `send_keys("^(a)")` raised `ValueError: unknown key
  name 'Control_L'` the moment this ran through the default composite
  rather than a single forced backend — confirmed live on KDE/KWin, where
  uinput is the only `KEY_EVENT` provider, but not KWin-specific: the same
  crash can occur on any desktop where uinput ends up providing key
  events. Fixed by routing all three to the `KEY_EVENT` provider, the same
  way every other composite operation dispatches.
- `examples/07_keys_and_pointer.py` used `{BKSP}` for backspace, which is
  not a recognized `KEY_ALIASES` abbreviation (`BAC`/`BS`/`BKS`, or the
  unabbreviated `BackSpace`) and was rejected by every backend. Fixed to
  `{BAC}`.
- `ToolCaptureBackend.capture()` trusted a screenshot tool's exit code
  alone. On KDE/KWin, `spectacle` intermittently exits 0 while leaving the
  output file empty — confirmed with pyguitest entirely out of the loop:
  plain `spectacle -b -n -f -o path` reproduced it directly, printing
  `KWin screenshot request failed: The process is not authorized to take a
  screenshot` and still exiting 0. That is a KWin/spectacle bug, not a
  pyguitest one, and left open in `docs/validation.md` — but trusting exit
  code 0 meant callers got a path to a corrupt image under a name that
  claimed success. Fixed by checking the destination is non-empty before
  returning it (both stages of the capture-then-crop path too), so this
  now raises a clear, actionable error instead.
- `examples/06_a_real_test.py`'s `test_the_window_is_actually_showing`
  guarded `is_window_viewable()` with `gui.supports(Capability.WINDOW_STATE)`,
  but that capability is also declared for `active_window()`'s sake on
  `KdotoolBackend`, which still refuses `is_window_viewable()` itself
  (see above) — `supports()` cannot predict a per-verb refusal like that,
  so the test errored on KDE/KWin instead of skipping. Fixed by catching
  `CapabilityUnsupported` around the call and skipping on it.
- `examples/04_drive_an_editor.py` and `examples/06_a_real_test.py` both
  type text into the editor and then clean up with a bare
  `process.terminate()`. Once the document has unsaved text, gedit's
  response to SIGTERM is its own "Save changes?" dialog rather than
  exiting — confirmed live: the process was still running when the
  interpreter exited, reported as a `ResourceWarning` rather than anything
  pyguitest raised. Fixed both to `wait(timeout=5)` after `terminate()`,
  falling back to `kill()` so cleanup cannot hang on that dialog.

## [0.1.0] — 2026-08-29

First public release.

### Added

- Capability negotiation as public API: `connect()`, `supports()`,
  `Capability`, `CapabilitySet` and the T1–T6 tier scale from the
  X11::GUITest audit in `docs/wayland-audit.html`.
- Element automation over AT-SPI, the one layer that behaves identically
  under X11 and Wayland.
- Input injection backends: X11, XDG desktop portal (RemoteDesktop),
  GNOME Shell extension, libei (opt-in), uinput, and the `wdotool`/`wtype`
  command-line tools.
- Screen capture and image search: portal capture, `grim`, X11, and
  template matching through ImageMagick.
- Window management per compositor, including the GNOME Shell extension
  path, since Mutter implements no foreign-toplevel protocol.
- A `pyguitest` command-line entry point.
- PEP 561 type information (`py.typed`); no hard runtime dependencies.

[Unreleased]: https://github.com/ctrondlp/pyguitest/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/ctrondlp/pyguitest/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/ctrondlp/pyguitest/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/ctrondlp/pyguitest/tree/v0.1.0
