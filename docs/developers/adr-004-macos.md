# ADR 004 — macOS: PyObjC in one extra, three backends, both opt-in behind TCC

Status: accepted · 2026-09-22 · follows [ADR 001](adr-001-dependencies.md),
[ADR 002](adr-002-transports.md) and [ADR 003](adr-003-windows.md)

## Context

macOS is the fourth desktop this package is being taught, and the third to need
its own ADR. The capability model is unchanged and needs no new member and no
new tier: a backend declares what it can do, a caller asks before depending on
it, and a desktop that cannot do something says so. `GUIBackend` is
capability-shaped rather than X11-shaped, every unimplemented operation already
raises `CapabilityUnsupported`, and `errors.py` already carries
`PermissionRequired` beside it. A partial macOS backend is a valid backend on
the day it is written.

Three things make the decision worth recording. First, there is no CLI tool
worth adapting for the part that matters, so ADR 001's house rule has to be
re-argued rather than applied — but it fails here for a *different* reason than
it failed on Windows, and the difference is the whole of the capture decision.
Second, macOS has a permission system with no analogue anywhere else in this
package: TCC grants are per-binary, permanent, and answerable "no" exactly once.
Third, [ADR 003](adr-003-windows.md) already argues with this design in public —
it was written against the research notes of 2026-09-13 and names them "the
macOS plan" in four places, with the README index, `structure.md` and
`CHANGELOG.md` citing the disagreement. Until now a reader could find the
argument and not the position. This file is the position.

The same constraint that shaped ADR 003 shapes this one, harder: **nothing here
has been run on a Mac**, and unlike Windows there is not yet a Mac to run it on.
The decisions below are the ones that can be made from Apple's documentation and
from this repo's own source; the measurements that only hardware settles belong
in [docs/validation.md](../../docs/validation.md), and the phase order puts
everything reviewable-without-a-machine first for exactly the reason it did
there.

## Decision

**1. Bind the platform's frameworks through PyObjC, in one optional extra.**

| Layer | Mechanism | Dependency |
|---|---|---|
| Elements, element actions, element geometry, hit test | Accessibility — `AXUIElementCreateApplication`, `AXUIElementCopyAttributeValue`, `AXUIElementPerformAction`, `AXUIElementCopyElementAtPosition` | `pyobjc-framework-ApplicationServices`, via the `macos` extra |
| Window enumeration, placement, state, stacking | `kAXWindowsAttribute` and the writable position/size attributes, joined to `CGWindowListCopyWindowInfo` | both PyObjC distributions |
| Window events | `AXObserver` plus `NSWorkspace` notifications, on a `CFRunLoop` thread this backend owns for the life of one subscription | both, in a later phase |
| Screens, scale, coordinate space | `CGGetActiveDisplayList`, `CGDisplayBounds`, `CGDisplayModeGetPixelWidth` | `pyobjc-framework-Quartz` |
| Input injection | `CGEventCreateMouseEvent`, `CGEventCreateScrollWheelEvent`, `CGEventCreateKeyboardEvent`, `CGEventPost` | `pyobjc-framework-Quartz` |
| Pointer query | `CGEventCreate(NULL)` + `CGEventGetLocation` — **no grant at all** | `pyobjc-framework-Quartz` |
| Screen and window capture | `screencapture -x` / `-R` / `-l`, with ScreenCaptureKit as a second implementation behind the same capability | none — the tool ships with macOS |
| Clipboard | `NSPasteboard.general`, `NSPasteboardTypeString` | `pyobjc-framework-Cocoa`, pulled in by Quartz |
| Image search | `imagesearch` and ImageMagick, unchanged from Linux and Windows | none (pip cannot supply the tool) |

The extra carries a `sys_platform == 'darwin'` marker on each *requirement*
rather than on the extra's name, because PEP 508 has no way to mark the latter.
`pip install pyguitest[macos]` on Linux therefore resolves to two skipped
requirements and installs cleanly, which is what a Linux contributor following
the README's install line should get, and which `tests/test_docs.py` can keep
honest the same way it already checks the README against the packaging metadata.

**2. Three backends, split along framework and permission rather than along
read versus write.** `macos` (elements, actions, element geometry, hit-testing,
windows, screens, clipboard) registers at **90**, in the read-only band beside
`atspi` and `uia`; `macquartz` (pointer, buttons, scroll, keys, text) registers
at **70**, beside the Linux tool-backed input backends and `win32`;
`capture:screencapture` registers as an ordinary tool backend with no extra at
all. A suite that only reads state never gets event injection built into its
session, and the capture half genuinely is tool-shaped — `screencapture` is a
binary with an argv, and `capture:grim`/`capture:import` are the pattern it
slots into without a new concept.

`_window_family_provider()` decides the `macos` backend's internals rather than
the other way round: whichever member answers `windows()` must also serve
`move_window`/`resize_window`/`minimize_window` for the `Window` objects it
returned, because a `Window` is backend-private. Window *listing* wants
CGWindowList and window *placement* wants AX, so the two are joined inside one
member — the `Window` carries the AX element handle, and the CG window number
beside it for the capture join. Splitting them across members would route
placement to a member holding handles it cannot act on, which is the invariant
Xfce already taught this package once.

**3. Both PyObjC backends register `opt_in=True`.** This is the decision ADR 003
reverses for Windows, and the reason is the one `register()`'s docstring names:
`opt_in` is for a factory whose *construction* raises an interactive consent
dialog and blocks until someone answers it. On macOS that is literally true —
`AXIsProcessTrustedWithOptions` with `kAXTrustedCheckOptionPrompt` is a
system-modal prompt, and §4.3's point is that it can be answered "Don't Allow"
once, permanently, by a developer who was not expecting it. `eiinput` is the
existing precedent and the shape is identical.

**4. The preflight/prompt split is the design, not an implementation detail.**
Every TCC service has a non-prompting counterpart — `AXIsProcessTrusted()`,
`CGPreflightScreenCaptureAccess()`, `CGPreflightListenEventAccess()` — and
`detect()` calls only those. The prompting forms are reachable only from a named
backend's constructor, never from an import, never from `detect()`, and never
from a test that did not ask for the backend by name. A denial is reported in
`Environment.notes` and in `summary()`, because "empty element tree" and "not
trusted" are indistinguishable from the caller's side otherwise, and because
the APIs' failure mode under denial is silence — an empty tree, or a black
image — rather than an error.

**5. `SessionType.DARWIN` and `Compositor.QUARTZ`, with the platform test first
in `_classify()`.** These are the third and fourth members added for a
non-Linux desktop, after `WIN32` and `DWM`, and they follow the same rule:
spelled the way `sys.platform` spells it, so the two connect without a table.
The branch is keyed on `sys.platform == "darwin"` and placed *before* the
environment-variable chain, because a Mac running XQuartz has a `DISPLAY` and is
still not an X11 session — the failure otherwise is that `X11Backend` is handed
a desktop it can see a fraction of. `UNKNOWN`/`OTHER` is rejected in the table
below.

**6. The capability set is permission-dependent, and computed at construction.**
`capabilities` is a property `Session` and `--debug` both trust, so a `macos`
backend built without Accessibility declares the CGWindowList-shaped subset —
`WINDOW_LIST`/`WINDOW_STATE`/`WINDOW_GEOMETRY`/`WINDOW_ACTIVATE`/`WINDOW_PID`/
`WINDOW_AT_POINT` and nothing more — rather than a set it cannot honour. This is
the same rule `tools.py` already applies when it refuses to select `wtype` on
Mutter: installed is not the same as able.

**7. Four refusals are permanent, and are refusals for the same reason Wayland's
are.** `WINDOW_TITLE_SET` (`kAXTitleAttribute` is read-only for a foreign
window), `WINDOW_LOWER` (`kAXRaiseAction` has no counterpart),
`WINDOW_CURSOR_QUERY` (the window server's cursor is not queryable;
`NSCursor.current` is this process's own idea of it) and `INPUT_SYNC` (posting
is asynchronous and nothing reports consumption). The `primary=True` clipboard
spelling is refused rather than aliased to the general pasteboard, because
aliasing changes the meaning of a test that asked for it. `&` (AltGr) is refused
rather than aliased to Option, on the same grounds.

**8. Capture is the tool first and ScreenCaptureKit second, and a black image is
a refusal.** `screencapture` is the smaller change, ships with the OS, and
reuses the CGWindowID the window join already reads; ScreenCaptureKit lands
behind the same capability, gated on `SCShareableContent`, so a modern Mac gets
the in-process route and an older one keeps the tool. The part that is not
optional either way: a capture taken while Screen Recording is denied *succeeds*
and returns a uniformly black image. The backend detects that and raises
`PermissionRequired`, rather than handing a caller a screenshot that silently
means nothing. A fake-driven test pins it.

**9. Process: the machine-free half lands first, with its own tests and CI.**
The vocabulary, the probes, the hints, the packaging, the role table and this
ADR are written from the documentation, tested against faked `Quartz` and
`ApplicationServices` modules in `sys.modules` — the shape `tests/test_x11.py`
already establishes for `Xlib` — and run on a `macos-latest` job for
collectability before anything macOS-specific exists. The gate
(`scripts/pre-commit-test.sh`) is unchanged. Live tests are guarded by an
environment variable and skipped by default, never skipped silently, the way
`tests/test_portal_dbusmock.py` already is.

## Rejected alternatives

| Alternative | Why not |
|---|---|
| `atomacos` as the element layer | A wrapper over the same PyObjC calls, so it removes no translation work; latest release 3.3.0 on 2021-05-24, itself existing because its ancestor `atomac` had not released since 2013; its error vocabulary is its own (`AXErrorAPIDisabled` would need translating into `PermissionRequired`, the step calling the preflight functions directly avoids); and it has no story for CGEvent injection, capture, or observer threads, so three of the four areas needed are outside it |
| `osascript` / System Events as the primary element route | Works, and needs no binding at all — but it is a subprocess per operation, a string-based query language with no element coordinates or geometry, a notoriously slow tree walk, and it adds the *Automation* grant per target app on top of Accessibility. It survives below as a possible degraded tier, not as the design |
| `cliclick` for input | A third-party Homebrew binary with its own keycode vocabulary, to replace `CGEventPost`, which the extra already provides |
| `pyautogui`, `pynput` | `CGEventPost` and `CGEventTapCreate` under another name. `pynput`'s macOS module is the best available reference for correct event-tap and run-loop setup and should be *read* when the recorder's backend is written; neither is worth depending on |
| `rubicon-objc` as a second bridge | `pyautogui` uses it, so it is proven, but PyObjC is the only route to the AX wrappers this design needs. Taking both would give the project two Objective-C bridges, of which one is always the wrong one to reach for |
| One backend instead of three | The AX and Quartz halves need different distributions and grant different powers, and capture needs no dependency at all. A single class would drag the whole extra onto every install and hide which half is missing |
| Neither backend `opt_in`, for parity with ADR 003 | See below: constructing these *can* raise a system-modal prompt, which is the exact case `opt_in` is reserved for. Windows has no TCC, so it has nothing to avoid |
| `SessionType.UNKNOWN` with `Compositor.OTHER` | Both are printed by `Environment.summary()` and read by `hints_for()`, so this routes a Mac to the Linux advice path while telling a developer nothing |
| ScreenCaptureKit first | An async completion-handler API from Python, ahead of a CLI that ships with the OS and already fits `tools.py`. It belongs behind the capability, not in front of the tool |
| A `Hint` variant for permissions | The research notes left "`command=None` plus new prose, or a change to the `Hint` type" open. It closed itself: `Hint.command` is already `str | None`, so the TCC advice needs no type change |
| Automating the grant | `tccutil reset` can withdraw a permission; nothing supported can grant one. A "just click the toggle" helper is not buildable, however often it is asked for |
| A signed helper bundle for unattended CI | An MDM-delivered PPPC profile pre-approving a signed, stable binary is the only route, and a virtualenv interpreter is neither signed nor stable. That is a distribution project, not a backend one |

`docs/install.md` and `docs/troubleshooting.md` carry the user-facing half of
the permission story, and the README's capability table carries the one place
macOS is strictly *better* than a Wayland session: `pointer_position()` needs no
grant at all.

## Where this and ADR 003 differ, and why

ADR 003 named two disagreements with the research notes this file is drawn from.
Both stand, and both are answered here from the same rule rather than from the
other platform's conclusion. A third has appeared since, because ADR 003 landed
after those notes were written.

**`opt_in` is not symmetric, and should not be.** ADR 003 registers neither
Windows backend opt-in because constructing a `SendInput` wrapper does nothing
at all — the side effect is per call. Constructing a macOS backend that checks
trust with the prompting form puts a system-modal dialog on the developer's
screen, and §4.3's asymmetry is the sharp end: a Wayland portal dialog is
per-activation and can be re-asked, while a TCC "Don't Allow" is remembered
forever and turns every later run into a silent empty tree. The rule applied on
both platforms is `register()`'s; the platforms simply answer it differently,
and copying either conclusion across would be the error.

**"There is no tool to adapt" was a Windows finding, not a portable one.** ADR
003 found that on Windows the rule from ADR 001 and ADR 002 stops applying,
because the DLLs are already loaded in every process and `ctypes` is in the
standard library, so no CLI can be a smaller dependency than nothing. macOS is
the middle case and keeps the rule in a narrow form: the element tree has no
tool worth adapting — AppleScript is a poor fit for a typed, geometry-aware
element API, which is the blocking objection to the whole `osascript` route —
but capture genuinely does, `screencapture` ships with the OS, and it stays the
first implementation for exactly ADR 001's reason. One tool, in the one place
the tool is the right shape.

**The anti-`atomacos` argument has been re-grounded.** The 2026-09-13 notes
rejected it partly on "the house ADRs prefer adapting a maintained CLI tool over
taking a dependency". ADR 003 then showed that rule lapses when the platform API
is in-process — which is PyObjC's situation exactly, so that leg no longer
holds. The rejection above stands on the legs that survive: the release date,
the error vocabulary, and the three of four areas it does not cover. Worth
stating rather than quietly editing, because a reader comparing the two
documents will otherwise find an argument that ADR 003 already dismantled.

## Consequences

- A bare `pip install pyguitest` on macOS gets detection, honest hints, the
  clipboard, process launch, timing and image search — and no elements, no
  windows, no input. The `macos` extra adds the first two families and the
  third; `doctor` names it. That is a real contrast with Windows, where only
  elements sit behind an extra.
- Nothing is automatic. Both PyObjC backends are `opt_in`, so a plain
  `connect()` on a Mac composes the capture tool and whatever needs no grant;
  a caller wanting AX or injection names the backend and accepts the prompt.
  This is the intended shape and it will read as a missing feature to someone
  arriving from the Windows side, so `doctor` has to say it in words.
- The tier-6 block reads oddly on macOS too, and is left that way. `POINTER_QUERY`
  is served with no permission whatsoever, under a heading whose description says
  "deliberately prevented". The tier is a *Wayland* ceiling, documented as one;
  a macOS `report()` with `[yes]` there is the model working.
- `Environment.summary()`'s `mechanisms` line gains `ax`, `quartz` and
  `screen-recording`, none of which exist off Darwin — the same platform-branch
  the Windows work already put there.
- `detect_distro()` gains a Darwin branch beside the Windows/Cygwin/MSYS2 one,
  and needs no new shape: it already takes a `platform` argument and already has
  the `_WINDOWS_PLATFORMS` seam for "this OS has no distribution". On macOS the
  answer is `platform.mac_ver()`, and like the Windows string it is deliberately
  not a key in `_PACKAGES`.
- The distribution stays a universal `py3-none-any` wheel. PyObjC's own wheels
  are platform-specific, but they are the extra's problem, not this package's.
- Grants do not travel. Not in a repo, not in a container, not in a tarball, and
  not reliably across a Homebrew Python upgrade or a recreated virtualenv, since
  TCC records consent against a binary. Every `PermissionRequired` this backend
  raises must name the resolved path of the binary it was running as, or a
  developer reads the second prompt as "I already did this".
- CI is the weak part and is written down as weak: GitHub-hosted macOS runners
  are shared, ephemeral and unsigned, and AX needs a GUI login session rather
  than an SSH one. Fake-driven unit tests run there; live AX and CGEvent tests
  do not, and a developer's own Mac is where they run.

## What has landed, and what has not

Nothing has landed. There is no `SessionType.DARWIN`, no probe, no extra, no
backend, and no `macos-latest` job; `pyproject.toml`'s description names Linux,
BSD and Windows, and that is currently true.

What exists is this decision and the research behind it — the 2026-09-13
analysis covering the AX and CoreGraphics API mapping, the TCC model, the
keyboard and role tables, and the phase plan, from which everything above is
drawn. The phases it proposes are ordered so that vocabulary, probes, hints,
packaging, docs and this file need no Mac at all, the AX read path is the first
hardware phase and the largest single unknown, and input, capture and the
recorder's event tap follow it.

**Nothing in this document has been run on a Mac, and no Mac has yet been
identified to run it on.** That is a stronger caveat than ADR 003 carried: the
Windows work was written blind and then measured within days. Every claim here
that hardware could settle — the AX-to-CGWindowList join, deep-tree cost,
whether `CGEventKeyboardSetUnicodeString` is honoured per toolkit, the TCC
attribution rules for a process launched from a terminal, and whether a
listen-only event tap is granted by Accessibility or by Input Monitoring — is
unmeasured. The last of those decides which permission the recorder asks for and
is a task for the phase that needs it, not an assumption to encode now.
[docs/validation.md](../../docs/validation.md) is where each is retired as it is
measured, which is why the phase order puts everything reviewable without a
machine first.
