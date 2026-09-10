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
  - **Cross-process restore** (2026-09-02, `--token` in a second terminal)
    closed the one gap the runs above left open: every restore before this
    was still same-process -- negotiate, then restore, in the one
    interpreter -- where the actual use case for `persist_mode`/
    `restore_token` is a token saved today and presented from a cold
    process tomorrow. A second, independent process that never negotiated
    anything itself presented the token and restored in 0.25s with no
    dialog, 9/9 checks. The token is not tied to the negotiating process
    or D-Bus connection in any way that would have broken this.

  `move_mouse`/`click`/`scroll` were commanded successfully but are not
  verified: this session has no `POINTER_QUERY` to read the pointer back
  with. Everything above went through the composed `gnomeshell+atspi+
  uinput+imagesearch` session for windows and elements, and the forced
  `eiinput` session for every injected event.

  **A bug the same 2026-09-02 run found, in the script, not the library:**
  `wait_for_new_window`/`new_frame` tracked "is this a new window/frame" by
  *title*/*name*, and this desktop's own terminal rewrites its window title
  live to the foreground process or cwd -- so between the `before` snapshot
  and the poll, the terminal's title changed, satisfied "wasn't there a
  moment ago", and got mistaken for the just-launched editor: activated
  (trivially "successful", since it already had focus) and typed into
  instead. The portal half of that same run was unaffected and clean --
  6.73s fresh negotiation, 0.38s same-process restore, no second dialog --
  this was purely a detection bug in the validation script. Fixed by
  tracking real identity instead of a label: `Window.__eq__` (by handle and
  backend, added this same week for exactly this reason) for the window
  path, and the underlying dogtail node for the AT-SPI frame path, since
  pyguitest's own `Element` wrapper has no equality of its own but the node
  it wraps does.
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
  can. Found while investigating the above: `window_element("Visual Studio
  Code")` raised WindowNotFound for a window that was open, active, and
  listed by `windows()`.

  **Root-caused 2026-09-05, and the original diagnosis here was wrong.**
  This was recorded as "the two AT-SPI traversal paths disagree for
  Chromium/Electron clients". They do not. A read-only probe of a session
  with Google Chrome and VS Code both open found *neither application in
  the accessibility tree at all* — no application node, so no frames to
  disagree about. The tree held gnome-shell, ibus, two portal helpers and
  the terminal, and nothing else.

  The cause is `org.a11y.Status.IsEnabled`, which was `false`. Chromium
  builds no accessibility tree until something announces an assistive
  technology, and until then never registers with AT-SPI. `windows()` is
  unaffected because it comes from the compositor, which has no opinion
  about accessibility. Two ways round it: set that property true (every
  Chromium app then publishes, at a real performance cost to them), or
  launch the application with `--force-renderer-accessibility`, which is
  the better option for a test that starts the application itself.

  `pyguitest debug` reports the property now, on a `chromium a11y` line,
  because an empty element tree for an application that is plainly running
  is otherwise indistinguishable from a bug in this package.
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
  **a title from `windows()` is not a key into the element tree**, and a
  `Window` is a snapshot rather than a live handle.
  `examples/_eiinput_portal_validate.py` was written against that the hard
  way, and the API has since grown the pieces it was missing:
  `Session.refresh_window(window)` exchanges a handle held across time for
  the current one — or `None` if it has closed — and `Session.
  is_window_open(window)` answers the same question directly. `Window`
  now compares by handle-and-backend rather than by identity, so
  `window in gui.windows()` means what it looks like.

  What none of that fixes is the *cross-source* half: `windows()` and the
  accessibility tree are still two different views, and no amount of window
  identity bridges them. Track a window within one source — re-scan
  `elements(role="frame")` for the frame that was not there before, which is
  what that script does — rather than looking a title from one up in the
  other.

## Run live on GNOME Shell 51.beta (Wayland)

- **`gnomeshell`'s `WINDOW_CAPTURE`, repaired for a Mutter 51 regression**
  (2026-09-02, `scripts/validate-gnome-extension.sh`'s capture check, added
  this session). `Meta.WindowActor.get_image` — the only capture API the
  extension had ever used, and the one the 50.4 section above validates —
  is gone as of Mutter 51 with no drop-in replacement; the id-0 capability
  probe correctly reported `WINDOW_CAPTURE` unsupported rather than failing
  silently, but that left no capture at all on Shell ≥51. The replacement,
  `paint_to_content()` + `Shell.Screenshot.composite_to_stream()`, mirrors
  the pattern gnome-shell's own screenshot service uses internally, and
  needed a version fork in the extension (`_captureLegacy` for Shell ≤50,
  `_captureModern` for ≥51) since both APIs coexist across the supported
  shell-version range.

  The first live run surfaced a real bug that writing the replacement from
  typelib introspection alone could not have caught: the composited image
  came back 989×587 against a 939×537 window — +50px on *both* axes, not a
  scale factor (989/939=1.053 vs 587/537=1.093, inconsistent ratios).
  `paint_to_content()` renders the actor's whole allocation, shadow margin
  included, not just the visible frame. Fixed by cropping with
  `Meta.Window.get_buffer_rect()` (full allocation) against
  `get_frame_rect()` (visible window), scaled by texture-pixels-per-
  buffer-pixel so it stays correct on a HiDPI/fractional-scale monitor
  too. A second live run after the fix landed produced a 921×1035 PNG
  against a 921×1035 window exactly — "captured size tracks frame
  geometry uniformly" — 9 of 9 checks clean.

  One wrinkle worth recording for its own sake: an intermediate run, made
  without re-running `--install` first, still showed the old uncropped
  result even though the crop code had already been written to disk —
  GNOME Shell does not re-read extension JS on the next D-Bus call. The
  fix only took effect once `--install` copied the file and a subsequent
  logout/login actually reloaded the module, reconfirming that there is
  no way to hot-reload an extension on Wayland.

- **`gnomeshell`'s `SCREEN_INFO`** (2026-09-03) — the last failing tier-2
  line, and the only one that was ever a matter of not asking. Mutter's
  own `org.gnome.Mutter.DisplayConfig.GetCurrentState` answers any
  session-bus client with the full monitor, mode and scale list,
  unprompted: confirmed here by `gdbus call` before a line was written,
  and again through `gui.screens()` afterwards. Nothing about it needs the
  Shell extension — only this backend's *construction* does, which is the
  one caveat below.

  `screens()` returns `Screen(0, 1920x1080 'Virtual-1')` on this session,
  matching the connector and the `is-current` mode from the raw D-Bus
  reply. The cross-check that matters more is the coordinate space: a
  maximized window's `geometry()` came back `(0, 32, 1920, 1048)` from the
  extension, and 32 + 1048 = 1080 exactly. So `screens()` and `geometry()`
  agree, which is the whole reason this reads *logical* monitors and
  divides the panel's pixel mode by the scale rather than reporting the
  mode directly — get that wrong and the two disagree on precisely the
  fractional-scale desktops where a caller cannot spot it by eye.

  Unmeasured here, because this session has one unscaled, unrotated
  output: the fractional-scale divide, the physical-layout-mode branch
  that must *not* divide, the 90°/270° axis swap, and the mirrored-pair
  case. All four are unit-tested against shapes transcribed from this
  machine's real `GetCurrentState` reply, but none has been seen on a
  desktop actually configured that way.

  The caveat: `SCREEN_INFO` still rides on `GnomeShellBackend`, which
  refuses to construct without the `pyguitest-window-control` extension.
  So a plain GNOME session without it gets no `screens()` either, even
  though Mutter would answer. That is an artefact of where the method
  lives, not of what Mutter allows, and splitting it into its own
  extension-free backend is the fix if anyone wants outputs without window
  control.

- **`Capability.INPUT_SYNC`** (2026-09-04, `examples/_eiinput_validate.py`)
  — `sync()` returned `True` in **2.2ms**. That number is the point of the
  feature: it replaces the `time.sleep(0.3)` that sat after every injected
  event in this very script, so the guess it removes was roughly 130x
  longer than the answer it replaces — and unlike the sleep, it is a fact
  rather than a hope. libei's ping/pong cannot answer before EIS has read
  past everything queued ahead of the ping.

  Two things came free with the run. `INPUT_SYNC` appeared in the
  capabilities `eiinput` offered, so the libei-1.4 probe (which builds a
  throwaway `Ping` rather than reading a version, since the binding
  resolves the symbol lazily) works against a real library. And the call
  went through a **composite** — the session was `eiinput+gnomeshell` —
  so `CompositeBackend`'s `_DISPATCH` routed it to the one member that
  serves it. That is the same routing whose absence made the tier-6
  operations unreachable (see the caveat at the end of this file), now
  exercised live for a new operation rather than only unit-tested.

  **Measured 2026-09-05, and it corrects the 2.2ms above**
  (`examples/_sync_under_load_validate.py`, 50 samples per phase). One
  sample is not a latency: idle, the same round trip runs a **median of
  11.6ms, p95 43.8ms, max 61ms**. 2.2ms was the lucky end of that spread,
  and the honest comparison against the 0.3s sleep is roughly 26x at the
  median rather than 130x — still an enormous margin, and still a fact
  where the sleep was a hope.

  The load result is the interesting one, and it does **not** support the
  hypothesis it was built to test. With all 7 cores saturated the round
  trip did not degrade at all: median **6.8ms**, p95 23.4ms, max 71.2ms.
  Nothing exceeded 0.3s and nothing returned `False`. The median actually
  *improved* under load — almost certainly CPU frequency scaling, where an
  idle core has to wake from a low-power state and a loaded one is already
  boosted.

  So the claim "under load a fixed sleep is sometimes too short" remains
  unproven, and the reason is that **CPU saturation is the wrong lever**.
  The EIS round trip is IPC latency, not userspace CPU work, and Mutter is
  scheduled ahead of the load anyway. Testing the hypothesis properly needs
  the *compositor* busy — heavy damage and repaint, many windows, or a
  large batch of events queued ahead of the ping so the PONG has to wait
  for them. The script measures bare round trips and says so; that is a
  lower bound, and the queue is the part not yet measured.

  The run also confirmed `eiinput` composes with `gnomeshell` on GNOME at
  all — the script had only ever named `windows` alongside it, which is
  right on KDE and resolves to nothing on Mutter, so its first GNOME run
  failed with the registry's generic "cannot drive this session" before
  the consent dialog and looked like an `eiinput` fault. The script now
  picks the window member per compositor.

- **`Capability.CLIPBOARD` through the Clipboard portal** (2026-09-04,
  `examples/_portal_clipboard_validate.py`, run by the user). The one
  stage-1 item that had shipped without ever meeting a real desktop, and
  the one that least deserved the benefit of the doubt: `set_clipboard()`
  hands the portal no bytes at all — it declares ownership with
  `SetSelection` and then has to answer a `SelectionTransfer` signal once
  per paste, from a GLib loop on a daemon thread. Every unit test for it
  drives `_on_transfer` directly, so all twenty would still pass on a
  desktop where the signal never arrives, and the symptom there is not an
  exception but a clipboard that reads as **empty** to everything.

  **It works.** `xclip`, a separate process, pasted back exactly what
  `set_clipboard()` wrote — twice, and then the replacement value — with
  `SelectionTransfer` firing five times to produce them. The write path,
  the riskiest code in the stage, is confirmed.

  Three runs were needed, and each one corrected the last. Two failures
  came with the first, both real; fixing them exposed a third bug; and
  the third run then disproved the explanation given for the first:

  `get_clipboard()` raised `AttributeError` against a selection `xclip`
  owned. `SelectionRead` had answered with an ordinary `(h)` reply and
  **no file descriptor list at all**, so the code read `.get` off `None`.
  The first explanation was wrong, and a third run is what showed it.
  `xclip` advertises exactly `TARGETS` and `UTF8_STRING` — measured with
  `xclip -t TARGETS -o` — so the obvious reading was that asking for
  `text/plain;charset=utf-8` had asked for something the owner did not
  have, and a fallback list of four spellings went in on that basis. The
  third run then reported which spelling actually answered:
  **`text/plain;charset=utf-8`, first try, no refusals.** The type that
  failed in run one works. Mutter maps X11 target atoms onto MIME types
  on the way across, as it should, and the fallback list fixed nothing.

  What the failure most likely was, on the evidence: a **race**. The read
  came immediately after `xclip` claimed the selection, and a portal
  answering a hand-over still in flight replies with no descriptor. The
  script's own bug is what turned that into a reported failure — its
  retry loop broke out on the first exception instead of retrying to its
  deadline, so one transient refusal ended the phase. Both are fixed: the
  loop retries, and the typed error now says the condition is often
  transient and suggests retrying through `wait_until`.

  The fallback list stays, relabelled as defensive rather than
  load-bearing, since it costs nothing when the first type answers — but
  nothing here is evidence that any spelling below the first is ever
  needed.

  Phase 0 of the first run read the *shell's* own clipboard content
  perfectly, which is worth noting for its own sake: the read worked
  against a settled owner, and that is exactly how a hand-over race would
  survive a less careful check.

  **A third bug surfaced on the second run**, in the same few lines and
  invisible until the first two were out of the way: phase 0 raised
  `'NoneType' object has no attribute 'decode'`. The descriptor the
  portal hands back can be **non-blocking**, and `BufferedReader.read()`
  answers `None` rather than bytes when a non-blocking read has nothing
  ready yet — so `os.fdopen(fd).read()` was never safe here, and only
  ever worked because the owner had usually already written. Reading now
  waits on the descriptor with `select` and drains it to EOF, bounded at
  five seconds, because the writer is another application reached through
  the portal and a clipboard read that blocks forever inside a test run
  is the worst of the available failures. Three bugs in one code path,
  each hidden behind the one before it — this is the case for running the
  thing rather than reasoning about it.

  The clipboard also **survived `close()`** — `xclip` still pasted the
  marker afterwards, contradicting a documented claim. But the transfer
  counter did not move, so nothing was asked of this process: something
  downstream had cached the bytes, and on GNOME that is Mutter's
  XWayland selection bridge. The docstring said "a script that sets the
  clipboard and exits leaves nothing behind"; what it can honestly say is
  that the session stops *serving*, and that what a given application
  sees afterwards depends on caching it does not control. Corrected in
  `PortalBackend.set_clipboard`, and the script now separates the two
  cases with the counter instead of reporting a flat failure.

  `primary=True` was refused on both halves, as designed — the portal
  interface has no PRIMARY selection. The second run passed all seven
  checks.

  Still unmeasured: no *native Wayland* client has read a selection this
  backend owns. `wl-paste` cannot do it on Mutter, which is why the
  witness is `xclip` through XWayland — so every result above is really
  "as seen from X11".

## Run unattended in a headless GNOME session

New on 2026-09-04, and the first thing here that is a regression test
rather than a record of someone watching a screen. `scripts/headless-
session.sh` starts `gnome-shell --headless --virtual-monitor WxH` on its
own session bus, so it neither collides with a running shell nor shows
anything on the display, and runs a command inside it.

`./scripts/headless-session.sh ./scripts/validate-gnome-extension.sh` is
green end to end — on a Fedora desktop and, since 2026-09-05, on a GitHub
runner: 9 of 9 shell-level checks, and the whole
`GnomeShellBackend` battery under them — `move_window` leaving size
alone, `resize_window` leaving position alone, the two composing over six
rounds, a 600×400 PNG from `capture()` matching the window exactly, the
`new`/`title`/`close` window events, and `window_at()` hitting the right
window. Nobody clicked anything.

**Re-run 2026-09-08 (GNOME Shell 51.rc) after adding `app_id`.** The
extension's `ListWindows` gained a tenth field, `wm_class`, appended rather
than inserted so an older extension and a newer Python still agree on the
first nine — the validation script now checks for it two ways: the
introspected D-Bus signature (`a(uisiiiibbs)`) and, live, that a real window
actually carries a non-empty `app_id` (the spawned probe window answered
`'local.pyguitest.ProbeWindow'`). Both passed, and the whole battery around
them still did too — the same run is what caught that `GnomeShellBackend.
geometry()` had an exact 9-element tuple unpack that the new tenth field
would have broken outright; fixed to a trailing `*_rest` before this run,
confirmed by the geometry battery still passing with it in place. 10 of 10
shell-level checks; see `metadata.json`'s `"0.4.0-appid"`.

Two things it settles that the plan could only guess at. The headless
backend does **not** object to the seat situation — the open question that
decided whether pyguitest could have CI at all. And the GNOME Shell
extension loads and exports its D-Bus object in headless mode exactly as
it does in a real session, so the COMPOSITOR tier is testable without a
desktop.

`screens()` is worth its own line: it answers `Screen(0, 1280x720
'Meta-0')` against `--virtual-monitor 1280x720`. On a real monitor that
check can only confirm the number matches the compositor's own; here the
number was *chosen*, which is a stronger test of the same code.

Three findings came out of getting there, all now handled:

- **`connect()` aborted the process** on a session with no accessibility
  bus. Not an exception — libatspi answers an unreachable bus with
  `g_error()`, which is `abort()`, and `import dogtail.tree` reaches it at
  import time. No `try`/`except` can catch that, so `backends/atspi.py`
  now asks `gdbus` whether `org.a11y.Bus` answers before importing
  anything. This was reachable in any container or CI runner, not only
  here.
- **The shell takes its bus name about 4.4 seconds before its extensions
  are loaded.** A connect in that window fails with "Object does not exist
  at path" — the same error an extension that is not installed gives. The
  script waits for both.
- **A window can be listed before it has a size.** Mutter publishes a
  toplevel to `ListWindows` before the client's first configure round
  trip, so an unlucky first look gets a real window whose `frame_rect` is
  still 0×0. The validation script now waits for a sized window; a caller
  doing `wait_for_window()` then `geometry()` can see the same 0×0.

**The CI runner is GNOME Shell 46, not 51**, which turns out to be the
most useful thing about it. The two capture paths in the extension are
split by shell version -- `_captureLegacy` uses `Meta.WindowActor.
get_image()`, which Mutter 51 removed -- so a developer machine on 51 and
a runner on 46 exercise *different code*, and neither can run the other's.
That is how the legacy path's uncropped capture was finally caught after
months as an unconfirmed comment: nothing here could execute it. Read a
green CI run as covering Shell 46, and the desktop runs above as covering
51; they are complementary, not redundant.

What it does not cover: input injection reaching a client, anything
needing the portal's consent dialog (there is nobody to click it), and
every non-GNOME desktop.

## Run live on KDE Plasma 6 / KWin (XWayland)

- **`eiinput` after its negotiation moved into python-libei** (2026-09-01),
  the same `examples/_eiinput_portal_validate.py` run as the GNOME entry
  above, and it passed here too. Both portal implementations therefore
  behave the same through `libei.portal` as they did through the in-tree
  copy this package used to carry, which is what the cutover needed
  established and the last thing that was outstanding about it.
- **AT-SPI on KDE needs `toolkit-accessibility` turned on**, found while
  doing that run, and worth knowing because *nothing reports it*. GTK
  applications load their AT-SPI bridge only when the GNOME setting
  `org.gnome.desktop.interface toolkit-accessibility` is true. It is on by
  default in a GNOME session and off in a KDE one, and with it off the
  failure is silent in every direction: the packages are installed, `doctor`
  reports AT-SPI present, dogtail connects without complaint, and element
  queries just return nothing — indistinguishable from an application that
  genuinely has no widgets. `gsettings set org.gnome.desktop.interface
  toolkit-accessibility true` fixed it immediately. Written up in
  [install.md](install.md), and `doctor` now warns about it — but only on
  KWin. Measuring the same setting on the GNOME host above found it **off
  there too, with AT-SPI working perfectly**, so "off" is not by itself
  evidence of anything; the mechanism is unconfirmed (plausibly GNOME's
  session registers GTK applications with the accessibility bus regardless). The
  warning is therefore scoped to the one desktop where the failure was
  actually seen, and `pyguitest debug` reports the raw value everywhere,
  which is what would show it mattering somewhere else.
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
- **`Capability.WINDOW_EVENTS`** (`KWinEventsBackend`, new this session) --
  KDE's one remaining real capability gap before this: `kdotool` has no
  event-subscription mechanism at all (checked against `kdotool --help`
  directly -- purely query/action, unlike `xdotool behave`), so
  `wait_for_window`/`window_events` had no path here at all until now.
  Closed by an ad hoc KWin script (`_kwin_window_events.js`, shipped as
  package data and loaded via `org.kde.kwin.Scripting.loadScript()` +
  `Script.run()` at construction -- no install-and-enable step, unlike
  the GNOME Shell extension this mirrors in spirit but not in mechanism)
  that calls back into a small D-Bus service this backend hosts itself.
  The architecture inverts relative to GNOME's extension for a concrete
  reason: introspecting `org.kde.kwin.Scripting` live turned up
  `loadScript`/`Script.run` (load and run an arbitrary file, well
  documented) and `callDBus(...)` inside a script (call an *existing*
  service, also well documented) but no way for a script to register a
  new D-Bus interface or emit its own signal -- so the script became the
  client, not the server.
  Confirmed live end to end, both at the mechanism level
  (`scripts/validate-kwin-events.sh`, 7/7) and through the real backend
  class (`examples/_kwin_events_validate.py`): `wait_for_window("bash")`
  found an already-open Konsole window immediately via `kdotool search`
  (the existing-match check this backend needs since it has no
  `windows()` of its own), and `wait_for_window("gedit")` on a freshly
  spawned window returned only once a live `new` event arrived from the
  KWin script; a later `close` event also arrived correctly once that
  process was terminated. `window.internalId` (the KWin script's own
  window identifier) matches `kdotool`'s UUID handle format exactly
  (`{xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx}`), cross-checked against a
  real `kdotool search .` listing -- which is what lets a `Window` this
  backend yields interoperate with `KdotoolBackend`'s other operations
  (geometry, activate, ...) through the composite, since `CompositeBackend`
  reads `Window.handle` directly with no ownership check.
  One real bug surfaced building this and was fixed before it ever ran
  against a live KWin: an early draft of `window_events()` pumped
  `GLib.MainContext.default().iteration(True)` in a plain polling loop,
  which blocks indefinitely when nothing else is scheduled on the
  context -- exactly the state a `connect=False` test backend is in,
  deliberately, so unit tests could exercise the event queue without a
  real KWin -- silently ignoring `timeout` altogether. Fixed by mirroring
  `GnomeShellBackend.window_events()`'s proven `GLib.MainLoop` +
  `timeout_add` + wake-on-event shape instead.
  A second, narrower thing surfaced and was worked around rather than
  fixed, since there was nothing in this codebase to fix: the KWin
  script's own `workspace.windowList()`, called as a function, reliably
  broke script execution -- every statement after it, and even a
  `callDBus` placed *before* it in the same script, failed to arrive
  at the hosted service, though a bare `callDBus` and one wrapped in an
  ordinary function both worked fine in isolation. Enumerating
  `workspace`'s own property names (`for (var k in workspace)`) turned
  up `stackingOrder` alongside `windowList` -- and `stackingOrder`, read
  as a plain property rather than called as a function, worked without
  issue: iterating it and reading `.internalId`/`.caption` on each entry
  produced exactly the windows a real `kdotool search .` also listed.
  Used in the shipped script instead of `windowList()` for that reason.

## Run live on KDE Plasma 6 / KWin, genuinely native Wayland

A separate box from the KDE session above: a real graphical login
(`kwin_wayland`, not `_wrapper`'s XWayland-only mode -- `WAYLAND_DISPLAY` set,
`XDG_SESSION_TYPE=wayland`, `XDG_CURRENT_DESKTOP=KDE`), confirmed before
anything else ran by reading `kwin_wayland`'s own argv and the session's
`systemctl --user show-environment`. Answers roadmap item 13: every run
above this section forced things through XWayland (`X11Backend`, or GTK
apps launched under `GDK_BACKEND=x11`) or, for the section itself, ran in a
session labelled "(XWayland)" outright; nothing in this file had yet driven
a real KWin **Wayland** session end to end, and no Qt application had ever
met the AT-SPI backend at all.

**Note for reaching this session over SSH**: unlike a plain login shell, a
bare `ssh` here does not inherit `WAYLAND_DISPLAY`/`XDG_CURRENT_DESKTOP`/
`XDG_SESSION_TYPE` from the graphical session's systemd user environment --
`pyguitest doctor` reports `session unknown` / `compositor none` until those
three (plus `DBUS_SESSION_BUS_ADDRESS`, as on the GNOME 50 box) are exported
from `systemctl --user show-environment`, at which point it correctly
reports `session wayland` / `compositor kwin (KDE)`.

- **`KdotoolBackend`** -- `windows()`, `geometry()`, `active_window()`,
  `activate_window()`, `move_window()`, `resize_window()`, `window_at()`
  (hit and miss), `minimize_window()`/restore, and `is_window_viewable()`'s
  deliberate refusal, all re-confirmed against a real KWin **Wayland**
  session via `examples/_kdotool_validate.py`. No difference from the
  XWayland run above -- kdotool talks to KWin's own scripting interface
  either way, not to X.
- **`KWinEventsBackend`** -- re-confirmed both at the script level
  (`scripts/validate-kwin-events.sh`, 7/7) and through the backend class
  (`examples/_kwin_events_validate.py`): an already-open window found
  immediately with no event needed, a freshly spawned window's `new` event,
  and its `close` event after termination. Also unaffected by XWayland vs
  native Wayland, for the same reason.
- **Screen capture (spectacle)** and **clipboard (`wl-copy`)** -- both work:
  a whole-desktop screenshot, a window cropped from it via `WINDOW_GEOMETRY`,
  and a clipboard round trip, none of which had been re-checked specifically
  under native Wayland on this desktop before.
- **AT-SPI against a real Qt6/KF6 application (KCalc) -- the first time any
  Qt application has met this backend.** The accessible tree is fully
  reachable: 235 nodes, every expected role present (`button`, `check box`,
  `combo box`, `list`, `menu bar`, `radio button`, `scroll bar`, `text`, and
  more). Two real findings came out of driving it, not just listing it:
  - **Numeric and operator buttons publish their accessible name as the
    spelled-out English word, not the glyph on the button face**: `"Five"`,
    `"Add"`, `"Subtract"`, `"Multiply"`, `"Divide"`, `"Equals"`, `"Decimal
    point"`, `"Percentage"`, `"All clear"`, and so on -- never `"5"`, `"+"`,
    `"="`. GTK's own calculator does the opposite (`gui.button("C")` already
    documented above as matching a literal glyph). Anything written against
    Qt/KDE apps with `gui.button("5")`-style code needs the spelled-out name
    instead; this is a toolkit naming convention, not a bug, and confirmed
    specific to number/operator keys -- named function keys (`"Sine"`,
    `"log"`, `"mod"`, memory slots `"C1"`-`"C6"`) already read naturally.
  - **A real bug, found and fixed**: `Element.click()` raised a
    `gnome-ponytail-daemon` `RuntimeError` for every button tried. Root
    cause confirmed by reading dogtail's own source (`Node.click()` in
    `dogtail/tree.py`, `click()` in `dogtail/rawinput.py`): under Wayland it
    unconditionally routes a synthetic coordinate click through GNOME's
    ponytail daemon, with no other path, regardless of desktop -- so this is
    not KDE-specific, it would hit sway, Hyprland and niri identically, and
    nothing in this file had driven `Element.click()` under Wayland at all
    before now to notice. Confirmed via `AtspiBackend`'s own `_pyatspi`/
    `geometry()` precedent (which documents the same daemon for a different
    call, `Component.get_size`/`get_position`, and has no fallback because
    none exists there) that `click()` is different: AT-SPI's action
    interface reaches the same button with no coordinates and no daemon at
    all. `Element.click()` now tries `node.click()` first and, only on that
    specific ponytail `RuntimeError`, falls back to `doActionNamed("Press"
    or "click")` if the element offers one; an unrelated `RuntimeError` is
    still raised as-is. **Verified end to end, real click() this time, not
    the do_action() workaround**: pressed "Seven", "Multiply", "Two",
    "Equals" on a real KCalc window, and `7×2=14` read back correctly from
    the accessible tree afterward.
- **A second real bug, found and fixed, unrelated to Qt**: `window_element
  (window.title)` -- the natural way to scope an element search to a
  `Window` already in hand -- raised `WindowNotFound` against GNOME Text
  Editor's own default title, `"New Document (Draft) - Text Editor"`,
  because `find_window`/`wait_for_window`/`window_element` compiled `title`
  as a regex unconditionally, and `(Draft)` reads as a capture group: the
  literal parentheses never match themselves. `re.compile(title).search
  (title)` on that exact string reproduces it in isolation, `False`.
  Confirmed as a real, general footgun (not a one-off): `elements()`/
  `element()`'s own docstring already claimed to mirror "the same
  convention find_window uses" for `name`/`description` -- treat a plain
  string as a literal match, a compiled `re.Pattern` as real regex -- but
  find_window never actually did that; it always compiled a plain string as
  regex. Fixed to actually match: a plain string is escaped first (literal
  substring match), a compiled pattern is used as-is. Existing regex-reliant
  callers updated to pass one explicitly: `examples/02_find_windows.py`
  (built specifically to demonstrate FindWindowLike.pl-style regex title
  matching) and `examples/_eiinput_validate.py` (an `(?i)a|b` alternation).
- **A third, separate finding on the same GNOME Text Editor window, NOT
  fixed because there is nothing on pyguitest's side to fix**: even after
  the regex fix above, `window_element("Text Editor")` still could not find
  it. Direct inspection (`gui.elements(role="frame")`) showed why: this
  native-Wayland GTK4 window's AT-SPI **frame node has an empty name**
  entirely -- not a matching problem, there is nothing to match. Confirmed
  by process: every element belonging to the editor's own pid (97 of them)
  was enumerable and correct -- the running application, its "document
  restored" notification bar, its internal panels -- but none with a
  `frame`/`window`/`dialog` role carried this window's title, or any name
  at all. Contrast with KCalc immediately above, where `window_element
  ("KCalc")` worked correctly on the first try: a Qt6/KF6 application's
  AT-SPI frame under KWin publishes its title; this GTK4 application's does
  not. `type_text()` into the window still worked throughout (uinput
  injects independently of AT-SPI), so the practical impact is scoped to
  `window_element()`-based scoping for a foreign-toolkit app specifically,
  not to window management or input generally. Not root-caused further --
  plausibly the same class of gap as the already-documented Electron/AT-SPI
  membership disagreement below, where a toplevel a compositor tracks is
  not the same thing an accessibility bridge chooses to publish, though the
  mechanism here (an empty name rather than a missing node) is different
  enough that this is recorded as its own finding rather than assumed to be
  the same bug.
- **A live, human-observed note, not independently measured**: driving
  KCalc's buttons one at a time via AT-SPI actions was visibly slow to
  someone watching the session directly, more than the same sequence of
  calls felt on other boxes in this file. Recorded as reported rather than
  benchmarked -- it could be AT-SPI action dispatch, this VM's own display
  pipeline (VirtualBox, software rendering -- `VMware: No 3D enabled` /
  `libEGL warning: egl: failed to create dri2 screen` appeared in every run
  here), or something else; worth a real measurement if it matters later.
- **`eiinput` (`LibeiBackend`) against `xdg-desktop-portal-kde`, this time on
  genuinely native Wayland** -- every earlier KDE `eiinput` run in this file
  was on the session labelled "(XWayland)" above. `connect(backend=
  ["eiinput", "windows"])` negotiated a real `RemoteDesktop` portal session,
  a human watching the session clicked through KDE's consent dialog, and
  `move_mouse`/`click`/`scroll`/`type_text`/`sync` all completed against a
  real `gedit` window with no error (`sync()` returning `True` in 0.5ms).
  Offered capabilities matched the XWayland run: `INPUT_SYNC`, `KEY_EVENT`,
  `POINTER_BUTTON`, `POINTER_MOVE`, `POINTER_SCROLL`, `TEXT_ENTRY`.
- **The KWin private EIS bypass (`org.kde.KWin.EIS.RemoteDesktop.
  connectToEIS`) verified live for the first time.** Identified by reading
  `xdg-desktop-portal-kde`'s own `src/remotedesktop.cpp` months earlier, with
  no KDE box available to confirm it; this box is the first chance. Called
  directly on the plain session bus -- no portal, no `Session`/`Request`
  object, nothing `xdg-desktop-portal-kde` itself would go through --
  `connectToEIS(7)` (the `KEYBOARD|POINTER|TOUCHSCREEN` bitmask the portal's
  own `DeviceType` enum uses) returned a real Unix socket fd (confirmed via
  `fstat`: `S_IFSOCK`) and a cookie, **with no consent dialog of any kind**,
  in well under a second. `disconnect(cookie)` released it cleanly
  afterward. Confirms the finding exactly as read from source: this is a
  genuine, working, dialog-free path to an EIS connection, distinct from
  `eiinput`'s own portal-mediated `ConnectToEIS` immediately above. Not
  wired into `eiinput.py` or python-libei -- that is real integration work
  (an alternative fd source alongside the portal negotiation path,
  `backends/eiinput.py` docs the shape it would take), deliberately left as
  a follow-up rather than done in the same pass as verifying the bypass
  itself works at all.

## Run live on Ubuntu 24.04 LTS (GNOME Shell 46, Wayland)

Answers roadmap item 14 (Debian/Ubuntu LTS). A VirtualBox VM, not a physical
machine — `vmwgfx`/`gbm` software rendering, per Mutter's own startup log —
which matters below: this box is measurably slower and more timing-variable
than every other machine this file records, and that is exactly what
surfaced a real bug nothing else had. Ubuntu's own session mode
(`XDG_CURRENT_DESKTOP=ubuntu:GNOME`), not vanilla GNOME; reached over SSH, so
`DBUS_SESSION_BUS_ADDRESS`/`XDG_SESSION_TYPE`/`XDG_RUNTIME_DIR` needed
exporting by hand first, same as the GNOME 50 and KDE-native-Wayland boxes.

**Full gate green**: Python 3.12.3, the whole suite plus ruff/format/mypy,
via `pip install -e '.[dev,atspi,x11,uinput]'` in a `--system-site-packages`
venv (`python3-pyatspi`/`gir1.2-atspi-2.0`/`python3-evdev` all come from
`apt`, per docs/install.md's own table). `tests/test_portal_dbusmock.py` ran
for real too — 4/4, not skipped, against a genuine private `dbus-daemon`.

**Compositor tier, via `headless-session.sh`**: installed
`pyguitest-window-control`, then `validate-gnome-extension.sh` passed every
check — window list/move/resize/activate/minimize, a real captured PNG, and
real `new`/`close` window events from a spawned-and-killed `gnome-text-editor`.
No different from GNOME Shell 46 elsewhere in this file, except for how it
was reached: see the bug below.

**A real bug, found and fixed: `GnomeShellBackend` could hang `connect()`
forever.** Its D-Bus calls to the extension used `call_sync(..., -1, ...)`
— "wait forever" — on the assumption that a call over the local session bus
is always near-instant. On this VM it measurably is not, always: the
extension's D-Bus name and object can become reachable (satisfying
`headless-session.sh`'s own readiness wait) while Mutter's own
display/compositor state is still settling, and a call landing in that
window then blocks with no reply, ever. Confirmed **outside pyguitest and
outside Python entirely** — a bare `gdbus call ... CaptureWindow 0 ''`
against a freshly-started headless shell hung past a 15-second `timeout`,
proving this is a live GNOME Shell/Mutter timing race on this hardware, not
a PyGObject or `call_sync` usage bug. Non-deterministic across runs of the
identical command: a fresh headless shell sometimes answered inside
milliseconds, sometimes never. Fixed with a bounded 5-second timeout
(`_CALL_TIMEOUT_MS` in `gnomeshell.py`) — every caller already turns a raised
D-Bus error into a normal `BackendUnavailable`/"cannot capture" result, so
this trades an unrecoverable hang for a fast, catchable one. **Verified live
on this box**: re-running the exact scenario that hung before (a
just-auto-enabled extension, immediately probed) now returns in a few
seconds instead of never.

**AT-SPI window discovery, activation, and resize, confirmed correct against
a real `gnome-text-editor`**: `windows()`, `wait_for_window()`,
`activate_window()`, and `resize_window()`/`geometry()`'s settle-and-read-back
all matched a live window exactly as documented elsewhere in this file.

**A caveat, not a pyguitest bug: `dogtail`'s own import can hang inside
`headless-session.sh`'s default mode, on this VM specifically.**
`dogtail.tree` imports `gi.overrides.Gdk` at module load, and that import
hung (confirmed via `faulthandler.dump_traceback_later`, stuck inside
`Gdk.py`'s own module body) when `pyguitest.connect()` first pulled in
`AtspiBackend` immediately after a fresh headless shell came up **with
XWayland enabled** (`headless-session.sh`'s default — matching what CI's own
compositor job uses, which has never reported this). Passing `--no-x11`
made it disappear every time it was tried; the same command that hung also
succeeded instantly on other runs with X11 left enabled, so this reads as an
XWayland-readiness race that a real login session (XWayland already up for
the whole session's lifetime) or CI's comparatively faster runner rarely
hits, not a deterministic break. Nothing in this repository owns GDK's
import-time behavior, so this is recorded as a caveat for headless testing
on similarly slow/virtualized hardware — prefer `--no-x11` there when the
command does not need XWayland — rather than something fixed here.

**Typed input needs the `input` group, exactly as `pyguitest doctor` already
says.** This user account was not a member of it, so `ydotool`
(`type_text()`'s only available mechanism here — no `ydotoold`, no writable
`/dev/uinput`) failed with a clear, specific error rather than hanging or
misbehaving: `doctor`'s existing diagnostic was correct and sufficient,
nothing new to fix.

**One environment-specific footgun, not a pyguitest bug**: Ubuntu 24.04 does
not install `gedit` — it ships `gnome-text-editor` instead, whose window
title is `"... - Text Editor"`, not `"... - gnome-text-editor"`. Any script
that (like `examples/06_a_real_test.py`) reuses its launch command as the
`wait_for_window` title match — a fine assumption for `gedit`, where the
literal string appears in the title — needs a different string on a desktop
whose editor is `gnome-text-editor`; `wait_for_window` itself matched
correctly once given text the title actually contains.

## Run live on Xfce (xfwm4) — a real X11 session under a non-Mutter WM

Answers roadmap item 15. The same Ubuntu 24.04 VM as the section above,
switched from GNOME to Xfce: `lightdm` → `xfce4-session` → `xfwm4` on a real
`Xorg :0`, `XDG_SESSION_TYPE=x11`, no Wayland compositor anywhere, so
`pyguitest doctor` reports `session x11 / compositor none`. That last part is
the whole point of this run, and nothing else in this file has it: every
other desktop here has a *compositor-specific* window backend (the GNOME
Shell extension, kdotool, swaymsg) that outranks everything else and serves
the whole window family from one place. Xfce has none, which is what made
plain registration priority visible as the load-bearing thing it had
quietly become.

**The real bug this found: window control was unusable, and failed below the
level anything here could report.** A `Window` is backend-private, so the
member answering `windows()` also has to be able to answer
`move_window`/`resize_window`/`minimize_window` for that same `Window`.
AT-SPI lists windows and outranks `X11Backend` (90 vs 40), but cannot place
one — and with no compositor backend present to outrank both, `windows()`
handed out AT-SPI frame references while `WINDOW_PLACEMENT` had only
`X11Backend` to dispatch to. `move_window` then crashed inside python-xlib's
own struct packing (`struct.error: required argument is not an integer`),
not as a typed error this package could catch. This is the same hazard
`_issuer`/`_owner` already guard for `capture()`/`_geometry_of` — the
invariant `X11Backend`'s own capabilities docstring states, "only ever
receives windows it issued itself" — but the generated dispatch for the rest
of the window family never got the same protection, because on every desktop
tested before this one it held by construction. Fixed by preferring a member
that can place windows for the four capabilities both kinds offer
(`WINDOW_LIST`, `WINDOW_STATE`, `WINDOW_GEOMETRY`, `WINDOW_ACTIVATE`), with
plain priority as the fallback when nothing can place windows at all. GNOME,
KDE and sway are provably unaffected: their own backends already outrank
AT-SPI and offer placement.

**A second, smaller finding, in `doctor` rather than in a backend.** The
input-injection hint fired only when *no* input tool was present, so a
`ydotool` that was installed but could not open `/dev/uinput` counted as
"nothing to suggest" — while `xdotool`, which on X11 needs neither uinput nor
any permission setup, went unmentioned. Fixed to ask whether the tool that
suits *this session* is present. Worth knowing alongside it: on stock Ubuntu
24.04 `/dev/uinput` is `root:root 0600` with no udev rule shipped by either
`ydotool` or `python3-evdev`, so joining the `input` group changes nothing on
its own — `doctor` already documents that trap in full, and it is why
`xdotool` is the better answer on X11 rather than a workaround.

**Everything else passed, live, against a real `xfwm4` desktop** — one
`gnome-text-editor` driven end to end through the composed
`atspi+input:xdotool+capture+imagesearch+x11` session: the window found and
activated; `type_text()` reaching the document and read back through AT-SPI
(the first keymap-safe typed-input path validated in this file that needed no
uinput access at all); `move_window`+`resize_window` settling at exactly
`(50, 60, 700, 500)`; `window_at()` hitting it; minimize and restore changing
viewability both ways; a real whole-screen capture written to a PNG — the
capability a real X11 session has and XWayland does not; and all three tier-6
queries answering (`pointer_position`, `is_button_pressed`, `is_key_pressed`).

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

## Run live on a private X server (Xvfb) against GTK4

Fedora 45, at-spi2-core 2.61.1, gtk4 4.23.3, gnome-calculator 51~beta, on a
throwaway Xvfb with an accessibility bus of its own. Two findings, both of
which changed code.

**The role name for a push button has moved, and `button()` was matching
nothing.** at-spi2 renamed `ATSPI_ROLE_PUSH_BUTTON` to `ATSPI_ROLE_BUTTON`,
keeping the integer and the old symbol as an alias; the name string follows
the new nick:

```
Atspi.Role.PUSH_BUTTON     == 43                     # integer unchanged
Atspi.Role(43).value_name  == 'ATSPI_ROLE_BUTTON'    # renamed
Atspi.role_get_name(43)    == 'button'               # so the string moved
```

gnome-calculator publishes enum 43 for all thirty of its buttons, so it is
behaving correctly; `gui.button("C")` raised `ElementNotFound` for every one
while `gui.element(role="button", name="C")` found them. Since at-spi2
produces the string from the enum, this is version-dependent and
toolkit-independent — not a GTK4 or application quirk. Role lookups accept
both spellings now. **Confirmed fixed against the same live application.**

**Hit-testing cannot find a widget on GTK4, because extents carry no
position.** Every element reports `(0, 0, width, height)` — `'C'`, `'↑n'` and
`'7'` all `(0, 0, 64, 44)`. `element_at` therefore descends to the `frame` and
stops, for essentially every point: measured across three unrelated
applications, gnome-calculator 48/49 sampled points, baobab 42/49,
gnome-text-editor 48/49. The accessible tree itself is complete (108 nodes
for the calculator), so this is a Component-interface gap rather than a
missing bridge. `ELEMENT_GEOMETRY` is accurate about sizes and useless for
positions here, and nothing above it can recover what was never published.

**Per-widget focus, by contrast, works on a bare X server.**
`focus_tracking_works()` returns true and Tab walks real widgets — `text ''`
→ `button 'Backspace'` → `button 'C'` → `toggle button '↑n'`. The finding
recorded above for GNOME Shell 50.4, that only the shell's own toplevel
carries FOCUSED, is a property of the *shell* rather than of the toolkits:
with no shell running, applications publish it properly. Not established:
whether a click moves focus to the clicked widget — the test was invalidated
by the extents finding, since every button reported the same rectangle.

## Not run live

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
- **`inputcapture`'s consent negotiation only** — see the dedicated section
  below for the live activation attempts themselves, which are past this.
  A unit test already raised the real consent dialog by accident once
  during development: an early draft of a python-libei test omitted the
  "force the PyGObject import to fail" patch its sibling test already used,
  reached the real session bus instead of a fake one, and approved before
  the mistake was caught. No pointer barriers had been set and `Enable()`
  was never reached, so nothing was actually captured — but it is why
  `wait_for_pointer_activation` returns `None` on timeout rather than being
  able to hang a caller who did not know what they were starting, and why
  live-testing this deliberately, not by accident, mattered enough to do.

## `inputcapture`: two live attempts, negotiation confirmed, activation still open

2026-09-08, GNOME Shell (Wayland), run by the user directly —
`examples/_inputcapture_validate.py`, never by an agent session, for the
reason its own docstring gives: *approving* this consent dialog exclusively
diverts the approver's own pointer away from their desktop, which is a
trade only they should decide to make.

**What is confirmed working:** negotiation end to end (the consent dialog,
`InputCaptureSession.negotiate()`, `capabilities offered: ['INPUT_CAPTURE']`);
`zones()` reporting a real single-monitor layout (`(1920, 1080, 0, 0)`,
`zone_set=0`); the perimeter-barrier arithmetic, which matches the portal
spec's own worked examples for a 1920×1080 screen exactly —
`(1,0,0,1919,0)`, `(2,0,1080,1919,1080)`, `(3,0,0,0,1079)`,
`(4,1920,0,1920,1079)` for top/bottom/left/right; and the timeout path
itself, which returned cleanly (`disable()` ran, no hang, no crash).

**What did not happen, across four attempts:** activation. Runs 1-3 all
timed out at the full 30s window: all four screen corners; a single edge
(right side) under sustained pressure for several seconds rather than a
quick touch; and a third repeat after the `failed_barriers` fix below
landed, which raised no error — confirming every barrier really was
accepted, not silently refused.

**One real bug found and fixed as a direct result**, after run 2, before
run 3: `set_pointer_barriers()`'s `failed_barriers` return value — which
barrier ids the compositor refused — was being discarded entirely.
`wait_for_pointer_activation()` now raises `PyGUITestError` immediately if
every barrier was refused, rather than calling `Enable()` anyway and
waiting out the full timeout for an activation that could never come. Run
3 raised nothing and still timed out, which is what ruled this out as the
explanation for runs 1-2 rather than confirming it.

**Run 4 at the time: the compositor never emits `Activated` at all.**
`gdbus monitor --session --dest org.freedesktop.portal.Desktop` ran
throughout a fourth attempt and captured zero signal traffic beyond the
bus-name ownership announcement at startup. Read then as proof there was
nothing to subscribe to, which pointed at Mutter: it accepts
`SetPointerBarriers` and `Enable()` per spec, on GNOME Shell 51.rc, but its
own crossing-detection logic appeared to never decide a barrier was
crossed. A Mutter GitLab ticket (`work_items/5038`) was filed on that
conclusion.

**Correction: this was our own bug, not Mutter's — the ticket is now
closed.** An xdg-desktop-portal developer pointed out the actual mistake:
`InputCaptureSession.wait_for_activation()`/`wait_for_deactivation()` (in
python-libei) subscribed to `Activated`/`Deactivated` with the *session
handle* as the D-Bus object path, but the portal emits both signals on its
own object (`/org/freedesktop/portal/desktop`), identifying the session
from the signal's payload instead. That subscription matched nothing, on
every attempt, indistinguishable from the compositor's side staying silent
— which is exactly how "the compositor never emits `Activated`" got
misdiagnosed here, then carried into the ticket above. The `gdbus monitor`
evidence this section leaned on doesn't corroborate either reading any
more either: it was later found blind on this box's own session bus even
against a signal known to have fired, so "zero signal traffic" was never
proof of anything happening (or not) on the bus in the first place. Fixed
in python-libei (subscribes on the portal object now, filters the
payload's session handle) — merged, not yet independently re-run live
against a real compositor with the fix in place, so whether
`wait_for_pointer_activation()` now actually returns on a real edge
crossing here is still open. Treat the "Mutter never emits `Activated`"
claim above as superseded by this, not as a second, independent finding.

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
