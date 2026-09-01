# What has actually been run

Most of this package has been exercised against real desktops rather than
only against its own tests. This file records which parts, on what, and —
more usefully — which parts have not, so nothing in the README has to be
read as a claim you cannot check.

## Run live on GNOME Shell 50.4 (Wayland)

- **`eiinput` after its negotiation moved into python-libei** (2026-09-01,
  python-libei 0.3.0, driven by `examples/_eiinput_portal_validate.py`,
  18/18). The whole reason for the run: `LibeiBackend` no longer speaks
  D-Bus itself, it calls `libei.portal.RemoteDesktopSession.negotiate()`,
  and the GNOME and KDE results below exercised the old in-tree copy. End
  to end on this host, with nothing stubbed:
  - **Fresh negotiation** raised a real consent dialog and completed in
    5.76s once answered (4.73s on a second run), offering all five input
    capabilities the backend can serve -- `KEY_EVENT`, `TEXT_ENTRY`,
    `POINTER_MOVE`, `POINTER_BUTTON`, `POINTER_SCROLL` -- and issuing a
    22-character `restore_token`.
  - **Typed text reached a native Wayland client and was read back**
    through AT-SPI from a GTK4 `gnome-text-editor`: not "nothing raised",
    the characters were found in the document. The marker is unique per
    run and the editor's text is read *before* typing too, because
    GNOME Text Editor restores unsaved drafts and an earlier version of
    the script would have passed on the previous run's text without
    injecting anything.
  - **`close()` released the portal session** -- the `Session.Close()`
    that nothing sent before this change.
  - **Restore presenting the token took 0.37s with no dialog** (0.45s on
    the second run), and answered with the *same* token both times, which
    matches what `eiinput.py`'s own docstring records for this portal and
    is still that portal's behaviour rather than a guarantee.

  `move_mouse`/`click`/`scroll` were commanded successfully but are not
  verified: this session has no `POINTER_QUERY` to read the pointer back
  with. Everything above went through the composed `gnomeshell+atspi+
  uinput+imagesearch` session for windows and elements, and the forced
  `eiinput` session for every injected event.
- **Input injection through XTest** (`X11Backend`'s input half, forced
  rather than composited), with a caveat worth reading before trusting the
  headline. A purpose-built probe -- an X11 window created with
  python-xlib, given X-level input focus via `XSetInputFocus`, with
  `press_key`/`type_text`/`send_keys` driven against it and its event
  queue read back to check exactly what arrived -- scored 2 of 21 checks.
  That was the probe's own design being wrong, not XTest: Mutter tracks
  compositor-level focus independently of the X server's, and delivers
  injected XTest events by *that*, the same as it would a real keyboard --
  so events aimed at a window holding only X focus, never compositor
  focus, went to whatever the compositor actually considered focused
  instead (the terminal running the script), silently, with neither side
  raising an error. `XSetInputFocus`/`wmctrl`/`xdotool windowactivate`
  all have the identical gap: none of them move Mutter's own idea of
  focus.
  That accident still produced a real, live result: every event landed in
  the terminal (Ptyxis, a native Wayland client, not an X11 one) verbatim
  in its scrollback as `apyguitestX`, in the exact order sent --
  `press_key`/`release_key("a")`, `type_text("pyguitest")`,
  `type_text("X")` (the upper-case, shift-holding path), then
  `send_keys("^(a)")`, whose Ctrl-A landed as the modifier combo readline
  reads it as -- jump to line start -- rather than a literal `a`, itself
  confirmation Control was delivered correctly and not just the keysym.
  So on Mutter, a raw XTest event is not filtered by client type at
  delivery: it reaches whatever currently holds compositor focus, native
  Wayland windows included. What this does *not* show, and what the
  probe's own failure demonstrates directly, is that `X11Backend` can
  *aim* such an event at a chosen Wayland window: its `windows()`/
  `activate_window()` only see and manipulate X11 clients ([x11.py:424,
  552](../src/pyguitest/backends/x11.py#L424)), so there is no path from
  "type into this specific Wayland app" to a result -- the terminal
  received the keystrokes only because it already held focus when the
  script ran, not because anything here put it there. The "XWayland:
  reaches X11 clients only" note this project states in several places
  (`session.py`, `tools.py`, `docs/input.md`, `docs/install.md`, the
  `09_gui_spy.py` docstring) is still the accurate practical claim for
  that reason -- `X11Backend` cannot select a Wayland target regardless of
  what a stray event happens to reach -- and is left as-is. It remains
  exactly true of *reading* input state (`POINTER_QUERY`/
  `INPUT_STATE_QUERY`, both genuinely X-scoped, see the caveat below).
  KWin and the wlroots compositors are unmeasured and may route XTest
  differently; this result is Mutter-specific.

- **`eiinput`** (opt-in) — pointer and keyboard injection over libei.
  Pointing at GNOME's Activities button and clicking it opens the overview,
  reliably, and the cursor moves.
- **`gnomeshell`** — window control passed 8 of 8 checks, and per-window
  capture through the Shell extension produced a real PNG. That extension
  is the only prompt-free way to screenshot on this desktop.
- **`gnomeshell`'s `WINDOW_EVENTS`** (extension `0.3.0-events`) — all
  three event kinds (`new`, `title`, `close`) confirmed over the real
  D-Bus signal. `wait_for_window`/`window_events` were exercised through
  this, not just constructed. `new`/`close` came from
  `validate-gnome-extension.sh` deliberately spawning and killing a
  throwaway `gnome-text-editor` at step 6 rather than depending on a
  person's timing; `title` came along for free as GNOME sets the window's
  title after creation. Three real bugs surfaced getting a clean run, all
  in the *script*, not the extension or the backend: two stale-window
  crashes in the read-only checks after a window closed during a listen
  (`geometry()`/`is_window_viewable()` on a handle that no longer
  existed), and a race where the kill happened only after the first
  listen's D-Bus subscription had already been torn down, so a `close`
  firing in that gap was lost — fixed by using one continuous subscription
  across spawn, kill, and close instead of two separate ones. A
  subsequent run passed all 9 checks clean.
- **`portalcapture`** (opt-in) — captured the whole screen correctly.
- **`X11Backend` under XWayland** — per-window capture, which produced a
  correct image of a real window through this package's own PNG encoder.
- **AT-SPI, and the CLI-tool input and capture backends.**
- **Per-widget keyboard focus is not published at all**, which is a
  negative result worth as much as the positive ones. AT-SPI's FOCUSED
  state is what `focused()`, `assert_focused()` and `assert_tab_order()`
  read. Across the whole desktop, exactly one element ever carried it —
  GNOME Shell's own `Main stage` toplevel — and no widget in any
  application did, across three separate toolkits (Ptyxis/VTE,
  gnome-text-editor/GTK4, zenity/GTK3), whichever window was active and
  whether or not it had been activated first. `active_window()` was
  unaffected and stayed correct throughout: it reads STATE_ACTIVE on
  frames, a different mechanism, and it correctly named the real active
  window while no widget anywhere reported focus. So the three focus
  methods are exercised only by their unit tests; on this desktop they
  cannot match a real widget however the application behaves, and
  `Session.focus_tracking_works()` exists to say so at runtime rather
  than leaving a caller to read it as a test failure. `pyguitest debug`
  reports the same probe's answer.
- **`window_element()` cannot see an Electron window** that `windows()`
  can. Found while investigating the above: `gui.windows()` listed a
  running VS Code window (it walks `root.applications()` then each
  application's own children), while `gui.elements(role=FRAME/WINDOW/
  DIALOG)` — a recursive descendant search from the tree root, which
  `window_element()` uses — did not return it at all, so
  `window_element("Visual Studio Code")` raised WindowNotFound for a
  window that was open, active, and listed by `windows()`. The two
  traversal paths disagree for Chromium/Electron clients specifically.
  Not yet root-caused.
- **`windows()` and the accessibility tree disagree about *titles* too**,
  not only about membership (2026-09-01, GNOME Shell, `gnomeshell+atspi+
  uinput` composed session). Writing the `eiinput` re-validation script
  turned this up twice in one run. `windows()` is served by `gnomeshell`
  (priority 93, above `atspi`), and after `gnome-text-editor` renamed its
  own window from "New Document (Draft) - Text Editor" to the first line of
  the text just typed into it, `windows()` still reported the *old* title
  while the AT-SPI frame carried the new one. A `window_element()` lookup
  keyed on a title from `windows()` therefore searched for a name that no
  longer existed — and the miss is expensive rather than quick, because the
  lookup walks every window role on the desktop before raising. The same
  run also had the `Window` handle itself go stale: `geometry(window)`
  raised "no window with id 106" for a window the list had just returned.
  The lesson for a caller is a general one, and worth stating plainly:
  **a title from `windows()` is not a key into the element tree.** Track a
  window within one source — re-scan `elements(role="frame")` for the frame
  that was not there before — or re-resolve the `Window` immediately before
  using it for geometry. `examples/_eiinput_portal_validate.py` does both.

## Run live on KDE Plasma 6 / KWin (XWayland)

- **`KdotoolBackend`** — `windows()`, `geometry()`, `active_window()`,
  `activate_window()`, `move_window()`, `resize_window()`, `window_at()`
  (hit and miss), `minimize_window()`/restore, and `is_window_viewable()`'s
  deliberate `CapabilityUnsupported` refusal all confirmed against a real
  KWin, driven through `examples/_kdotool_validate.py` rather than only
  constructed. `resize_window()` needed a second pass: it first looked like
  a no-op against a `zenity` dialog, but running kdotool's own
  `windowsize` CLI directly against that same dialog showed the identical
  no-op, which pointed at the dialog (GTK message dialogs are not
  resizable) rather than the backend; switching the throwaway window to
  `gedit`, and to a target size guaranteed to differ from its default,
  confirmed the resize itself works.
- One real bug surfaced and was fixed: `geometry()` assumed
  `getwindowgeometry`'s position was always a plain integer pair and threw
  `ValueError` the moment it was not. A window mid-animation reported a
  genuinely fractional position live (`545,274.5403238932292`, from a
  `Konsole` window open at the time, not the window under test) —
  `_windows_with_geometry()` calls `geometry()` on every open window for a
  hit-test, so this crashed `window_at()` even though the window it was
  testing was unaffected. Fixed by parsing each component as `float` and
  rounding, covered by a regression test using that exact recorded value.
- **`UinputBackend`**, driven through the default composite (`atspi` +
  `uinput` + `capture:spectacle` + `imagesearch:compare`): `send_keys()`'s
  literal text, its `^(a)` (select-all) modifier notation, and `{BAC}`
  (backspace) all confirmed against a real `gedit` window.
- A real bug surfaced and was fixed: `CompositeBackend` never overrode
  `MODIFIER_KEYS`/`KEY_ALIASES`/`resolve_char_key`, so `send_keys()` (which
  reads them straight off `self.backend`) built key names in the base
  class's inherited X11-keysym vocabulary — `"Control_L"` for `^` — no
  matter which member actually presses them. On this session the
  composite's only `KEY_EVENT` provider is `UinputBackend`, which speaks
  evdev names (`"LEFTCTRL"`) and raised `ValueError: unknown key name
  'Control_L'` the moment `send_keys("^(a)")` ran — the first time any
  script had exercised modifier-key `send_keys()` through the default
  composite rather than a single forced backend. Fixed by routing all
  three to whichever member provides `Capability.KEY_EVENT`, the same way
  every other composite operation is dispatched — see `input.py`'s
  `ToolInputBackend`, which already did this per-tool and was the model for
  the fix. Not KWin-specific: the same crash would occur on any desktop
  where uinput ends up as the composite's key-event provider (for example,
  GNOME with no `xdotool`/`wtype` installed).
- A second, unrelated bug surfaced by the same run: the shipped
  `examples/07_keys_and_pointer.py` itself used `{BKSP}` for backspace,
  which is not one of `KEY_ALIASES`' abbreviations (only `BAC`/`BS`/`BKS`,
  or the unabbreviated `BackSpace`) — sent through to `press_key("BKSP")`
  unchanged and rejected by every backend. Fixed to `{BAC}`.
- **`ToolCaptureBackend` (`spectacle`)** — whole-screen capture confirmed
  against a real KWin, but not reliably: `spectacle -b -n -f -o path`
  intermittently exits 0 and leaves `path` at 0 bytes, interspersed with
  runs that produce a real 1920x1080 PNG, no code or environment change in
  between. Root cause confirmed with pyguitest entirely out of the loop --
  running plain `spectacle -b -n -f -o /tmp/test.png` directly in a
  terminal reproduced it with spectacle's own stdout explaining why:

      KWin screenshot request failed:
      The process is not authorized to take a screenshot
      - Method: CaptureScreen
      - Method specific arguments: "Virtual-1"

  KWin's `ScreenShot2` D-Bus interface intermittently refuses spectacle's
  own `CaptureScreen` request -- on the same system, the same binary, no
  configuration change -- and spectacle treats that refusal as a non-fatal
  condition: it prints the error and still exits 0 with no output file.
  This is a KWin/spectacle bug, not a pyguitest one, and not reproducible
  from outside that specific interactive session (well over a dozen
  attempts from this session's own shell, direct CLI and through
  `subprocess`, never hit it). Nothing useful in `journalctl` either --
  both KWin's and spectacle's sides of this print to stdout/stderr only,
  not through the service manager. Left as an open upstream question.
- A real bug this exposed, independent of the root cause above: `capture()`
  trusted the tool's exit code alone. Fixed by checking the destination is
  non-empty before returning its path — for `_capture_then_crop` too, on
  both the intermediate whole-screen shot and the final cropped file — so
  a repeat of the same silent failure now raises a clear, actionable error
  instead of handing back a path to a corrupt image under a name that
  claims success.
- **`Capability.TEXT_ENTRY`** through the default composite (`atspi` for
  window listing/activation, `uinput` for the typing itself) — confirmed
  against a real `gedit`, driven through `examples/04_drive_an_editor.py`
  rather than only constructed: `wait_for_window`, `activate_window`, and
  `type_text()` all worked against a real KWin session, and the typed text
  landed correctly. No bug found. Two warnings appeared alongside it,
  neither a pyguitest issue: `KeymapWarning` is pyguitest's own, expected
  notice that uinput injects raw scancodes below the compositor, so typed
  text depends on the session's keyboard layout matching what it assumes
  (it did, here); and a `Gtk-WARNING` about gedit's own call to the
  `org.freedesktop.portal.Inhibit` D-Bus portal failing with
  `AccessDenied: Unable to open /proc/<pid>/root` is gedit trying to block
  session sleep while editing, unrelated to pyguitest's input injection.
- **`examples/06_a_real_test.py`**, the package's own recommended
  `unittest` pattern, run end to end against real `gedit` on KDE/KWin —
  window resize, `is_window_viewable`, and typed-text-lands-in-the-tree all
  confirmed live.
- Two real bugs surfaced and were fixed. First: `test_the_window_is_actually_showing`
  guarded `is_window_viewable()` with `gui.supports(Capability.WINDOW_STATE)`,
  but `KdotoolBackend` declares `WINDOW_STATE` for `active_window()`'s sake
  and still refuses `is_window_viewable()` itself (see above) — `supports()`
  answers for the coarser capability, not this one verb, so the guard could
  not predict the refusal and the test errored instead of skipping. Fixed
  by catching `CapabilityUnsupported` around the call and skipping on it.
- Second: both this file and `examples/04_drive_an_editor.py` type text
  into the editor, then clean up with a bare `process.terminate()`. Once
  the document has unsaved text, gedit's response to SIGTERM is to raise
  its own "Save changes?" dialog rather than exit — confirmed live: the
  process was still running by the time the interpreter exited, reported
  as a `ResourceWarning` from `subprocess.Popen.__del__` rather than
  anything pyguitest raised itself. Fixed both examples to `wait(timeout=5)`
  after `terminate()`, falling back to `kill()` so cleanup cannot hang on
  that dialog. Re-run after the fix: no warning, `OK (skipped=1)`.
- **`eiinput` (`LibeiBackend`)** — previously validated only on GNOME Shell
  (below); confirmed on real KDE/KWin too, driven through
  `examples/_eiinput_validate.py`. `connect(backend="eiinput")` negotiated
  a `RemoteDesktop` portal session against `xdg-desktop-portal-kde`
  successfully -- KDE's own consent dialog appeared as expected and was
  clicked through by hand, confirming this is a real negotiation and not
  a silently-approved no-op -- and all five capabilities it can offer came
  back
  (`KEY_EVENT`, `POINTER_BUTTON`, `POINTER_MOVE`, `POINTER_SCROLL`,
  `TEXT_ENTRY`), and `move_mouse`, `click`, `scroll`, and `type_text` all
  worked against a real `gedit` window. No bug found. This answers the
  question ADR 002 and this file both left open: KDE's portal implements
  the same `RemoteDesktop`+libei path GNOME's does, at least for the
  device-capability surface this backend uses.
- **`Capability.CLIPBOARD`** (`ToolClipboardBackend`, new this session) --
  round trip, persistence past the call returning, and a second write
  replacing rather than appending, all confirmed against the real KDE
  clipboard through `examples/_clipboard_validate.py`. A by-hand spike
  came first: `echo -n text | wl-copy` followed by `wl-paste` round-tripped
  correctly on KWin, and the `wl-copy` process was still resident in `ps`
  afterwards -- proof both that wl-clipboard works on KWin at all (it is
  not a wlroots compositor, and this was previously unconfirmed) and that
  it persists a selection by forking into the background, the way it does
  on wlroots compositors.
- A real bug surfaced by the very next run, through pyguitest's own code
  rather than the by-hand spike, and fixed before ever shipping:
  `set_clipboard()` hung for the full 15-second subprocess timeout on
  every call. `_run` used `subprocess.run(..., capture_output=True)`
  unconditionally, which pipes stdout/stderr -- and a forked child
  inherits its parent's file descriptors, so the daemonized `wl-copy`
  grandchild ended up holding the write end of those pipes open long
  after the tracked process exited. `communicate()` waits for the pipes
  to reach EOF as well as the process to exit, so it hung on a fork that
  had already succeeded. The by-hand spike never hit this because a
  shell's `|` connects `wl-copy`'s stdout/stderr to the terminal, not to
  a pipe `communicate()` is waiting to drain -- which is exactly why the
  easy confirmation looked clean and the first real one did not. Fixed by
  giving the write call `DEVNULL` rather than `PIPE` for stdout/stderr
  (the read call, which never forks, keeps `PIPE`); re-run after the fix
  completed in 0.07s instead of timing out. The accepted cost is losing
  stderr detail on a write failure specifically, which is the right side
  to lose it on rather than hanging every successful write for 15 seconds.
- **`Capability.WINDOW_CURSOR_QUERY`** (`X11Backend.is_window_cursor()`,
  the fifth tier-6 capability, previously only run against a fake Xlib) --
  forced under XWayland on this session, through
  `examples/_cursor_validate.py`. The protocol call itself works: it
  returns a plain boolean for every shape queried, no error, no hang.
  What it returns is the interesting part. Five visually distinct classic
  cursor-font shapes (`XC_X_CURSOR`, `XC_CROSSHAIR`, `XC_HAND2`,
  `XC_LEFT_PTR`, `XC_XTERM`) were all queried at one point of plain
  desktop background, where an ordinary arrow was unambiguously being
  shown -- and every single one came back `False`, `XC_LEFT_PTR`
  included. Reading `Xlib.ext.xtest`'s source (there is no higher-level
  documentation for `CompareCursor` to check against) shows pyguitest
  always calls it with an explicit cursor object built from the classic X
  core cursor font, never the `CurrentCursor` sentinel -- so this is
  consistent with a themed desktop cursor (Xcursor/Breeze on this KDE
  session, and the default on essentially every modern desktop) simply
  never bitwise-matching a classic bitmap font cursor, regardless of
  shape or what is actually on screen. Not a crash and not something a
  code fix addresses -- matching X11::GUITest's own original `IsWindowCursor`
  is the whole point of this backend's contract -- but a real practical
  limitation worth being explicit about: `is_window_cursor()` should be
  expected to read `False` on most real, modern desktops no matter what
  cursor is actually showing.
- Getting to that point surfaced two things outside `is_window_cursor()`
  itself, both left as open questions rather than chased down further:
  a plain GTK app (`gedit`) rendered as a native Wayland surface with no
  XWayland-visible top-level at all, invisible to `X11Backend.windows()`
  until relaunched with `GDK_BACKEND=x11` forcing it through XWayland;
  and once visible that way, its `geometry()` came back with implausible
  negative coordinates resembling those of KWin's own internal "Wayland
  to X Recording bridge" window, hinting at a KWin-specific counterpart
  to the GNOME/Mutter `geometry()` caveat below -- unconfirmed, and
  deliberately not pursued further in this pass.

## Run live on a real X11 session

- Whole-screen capture — the one capability a real X11 session has that
  XWayland does not.
- Window control: move, resize, minimize, lower, retitle, hit-test,
  geometry and viewability.
- The tier-6 query capabilities: reading the pointer position, and the
  keyboard and button state. (`is_window_cursor` was validated separately,
  under XWayland on KDE/KWin — see the themed-cursor limitation above.)

Every python-xlib call the X11 backend makes has also been checked against
the installed library — names, signatures and return shapes.

## Not run live

- **`eiinput` on KDE since the negotiation moved into python-libei.** The
  GNOME half of that question is closed — see the 2026-09-01 entry at the
  top of this file, which ran the whole cutover live there. The KDE result
  below predates the swap and has not been re-run, so what is unverified is
  narrow: whether `xdg-desktop-portal-kde` behaves the same through
  `libei.portal` as it did through the in-tree copy. Nothing about the
  conversation on the wire changed, and the same script
  (`examples/_eiinput_portal_validate.py`) is what would answer it, needing
  a person at a KDE desktop to click Allow.
- **The wlroots compositor IPC backends** — sway, Hyprland, niri. Their
  tests replay recorded output and stand-ins, on a sandbox where none of
  those are available to test against. Running them against a live
  sway/Hyprland/niri session is next. Two names have left this list:
  `KdotoolBackend` has since run live on KWin, and `UinputBackend` on both
  KWin and GNOME — above, and in the tier-6 caveat below, where a
  commanded move was read back off a real X client 1px out from rounding.
  That readback is the proof the pointer physically moved.
- **`portal`, the input half.** Its CreateSession/SelectDevices/Start
  negotiation has been run against a real xdg-desktop-portal (1.22.1) and
  completes; the keyboard, pointer and scroll methods past that point have
  not. `Start()` raises an interactive consent dialog that blocks until a
  human clicks Allow, so every step beyond it needs a person at a real
  desktop.

`tests/test_portal_dbusmock.py` covers the wire plumbing under that gap. It
uses [python-dbusmock](https://github.com/martinpitt/python-dbusmock) the
way `xdg-desktop-portal`'s own test suite does: a private `dbus-daemon` in a
throwaway temp directory (never the ambient session bus —
`$DBUS_SESSION_BUS_ADDRESS` is saved before the test class runs and restored
after, pass or fail), a fake `org.freedesktop.portal.Desktop` on *that* bus
answering `CreateSession`/`SelectDevices`/`Start` the way an
already-approved real portal would, and `PortalBackend` driven against it
over a genuine Gio connection — real D-Bus calls and a real
`Request`/`Response` signal round trip, not the pure-Python stand-ins
`test_portal.py` uses. Both the happy path and a declined-consent path pass
against a real `dbus-daemon` and real PyGObject. That proves the plumbing
between `PortalBackend` and a portal-shaped service. It proves nothing about
the real daemon, GNOME's or KDE's backend implementation, or the dialog.

## Why the live runs mattered

They found bugs no unit test could have:

- backends declaring capabilities they could not deliver;
- a window handle passed to a backend that could not interpret it;
- a screenshot tool selected on a session where it can only hang;
- three backends leaking a bare exception from a dependency instead of a
  typed one (dogtail's ponytail helper, twice, and a tool's stdout being
  dropped from its own error message);
- two bugs in the GNOME Shell extension that the header-derived code had
  carried from the start — which is why nothing here is claimed from
  headers alone any more.

All are fixed and covered by regression tests. Capture in particular had to
meet a real compositor before it was trustworthy.

## Known caveat: `geometry()` on GNOME's XWayland

`X11Backend`'s `geometry()`, and by extension anything that reads a position
back after `move_window`, may report a window's location wildly wrong under
GNOME/Mutter specifically — confirmed live: `move_window` visibly moves the
window to the right place, but a `geometry()` call right after reads back a
position nowhere near it.

This looks like Mutter's XWayland integration not keeping the window's
decoration-frame position, as reported over X11, synced to where it actually
renders the window — the same `XTranslateCoordinates` mechanism
`xdotool`/`wmctrl` use has no other request to fall back on if that is what
is happening. Working hypothesis from one diagnostic session on one machine,
not an independently confirmed root cause or a survey of other window
managers. Be skeptical of `geometry()` results on GNOME's XWayland until
someone reproduces or debunks this.

## Known caveat: the tier-6 queries under XWayland

`X11Backend` declares `POINTER_QUERY` and `INPUT_STATE_QUERY` on an
XWayland session, and both are real — but they answer for X's world only,
and where they cannot answer they return a stale or idle value rather than
an error. Confirmed live on GNOME Shell 51.beta under XWayland, driving the
default composite (`gnomeshell` + `atspi` + `uinput` + `imagesearch` +
`x11`).

The first run looked like a flat failure: four commanded moves, and
`POINTER_QUERY` reported the same unchanged `(960, 540)` after every one;
a held Shift read back as unpressed throughout. Neither conclusion
survived a control. `validation.md` listed `UinputBackend` as never driven
live, so "X never saw the pointer move" and "the pointer never moved" were
the same observation — the experiment could not tell them apart. Creating a
real X11 client with python-xlib and repeating the reads against it
separated them:

- **`POINTER_QUERY`** — commanded to the probe window's centre `(960, 575)`,
  X reported `(959, 574)`: a match to within rounding, and proof the
  pointer physically moved. Commanded away to `(1560, 975)` over the
  Wayland desktop, X went on reporting `(959, 574)` — the last position it
  had any claim to know. Not an error, not a refusal: the previous answer,
  indefinitely.
- **`INPUT_STATE_QUERY`** — with X input focus on that same probe window, a
  held `Shift_L` read back as pressed, idle before and after. The earlier
  `False` was focus-dependent, not broken. Same shape as the pointer half.

So the rule for both is the X client's world, not the session's: an
XWayland pointer query is accurate over an X surface and stale over a
native Wayland one, and a key-state query is accurate while an X client
holds focus. What makes this worth a caveat rather than a footnote is that
the failure is silent — a stale coordinate is a perfectly well-formed
answer, and a test asserting on it fails somewhere else entirely, or passes
for the wrong reason. Treat a tier-6 reading under XWayland as trustworthy
only when you know an X client is under the pointer or holding focus; on a
real X11 session none of this applies.
