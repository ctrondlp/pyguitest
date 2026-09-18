# ADR 003 — Windows: `ctypes` in-process, one `comtypes` extra, two backends

Status: accepted · 2026-09-14 · follows [ADR 001](adr-001-dependencies.md) and
[ADR 002](adr-002-transports.md)

## Context

Windows is the third desktop this package is being taught, and the capability
model already says what that means: a backend declares what it can do, a caller
asks before depending on it, and a desktop that cannot do something says so
rather than failing quietly. Nothing about that changes here. What does change
is where the mechanisms live, and that is all this ADR decides.

Two things make the decision awkward enough to write down. First, the house rule
from ADR 001 and ADR 002 — **adapt a maintained command-line tool rather than
bind a library** — does not carry over, because on Linux the API sits behind a
socket or a compositor daemon while on Windows it is in DLLs every process has
already loaded. Second, a sibling analysis of the macOS port reached two
conclusions this plan deliberately reverses, and a reviewer who has read that
one will ask about both; they have their own section below.

One constraint shaped the order of the work rather than its content: nothing in
this phase has been run on a Windows machine. What is written from the
documentation — the key tables, the control-type mapping, the probes, the
packaging, this ADR — is written to be reviewable and correctable *before*
anything depends on it, which is why it lands first and why the tests for it are
driven by fakes.

## Decision

**1. Bind the platform's own API, in-process, with no dependency where that is
possible.**

| Layer | Mechanism | Dependency |
|---|---|---|
| Elements, element actions, element geometry, hit test | UI Automation (`GetRootElement`, `ControlViewWalker`, control patterns) | `comtypes`, via the `windows` extra |
| Window enumeration, placement, state, title, stacking | `EnumWindows`, `SetWindowPos`, `ShowWindow`, `SetWindowTextW` | none — `ctypes` |
| Window events | `SetWinEventHook` (`WINEVENT_OUTOFCONTEXT`), on a pump thread this backend owns for the life of one subscription | none |
| Screens, work areas, DPI | `EnumDisplayMonitors`, `GetMonitorInfoW`, `GetDpiForMonitor` | none |
| Input injection | `SendInput` (`KEYBDINPUT`, `MOUSEINPUT`) | none |
| Pointer and key state, cursor | `GetCursorPos`, `GetCursorInfo`, `GetAsyncKeyState` | none |
| Screen capture | GDI `BitBlt` with `CAPTUREBLT`, into the existing PNG encoder | none |
| Clipboard | `OpenClipboard`/`GetClipboardData` with `CF_UNICODETEXT` | none |
| Image search | `imagesearch` and ImageMagick, unchanged from Linux and macOS | none (pip cannot supply the tool) |

`ctypes` is in the standard library, so the whole of that column is free, and
`comtypes` is the single exception because COM needs an apartment, a vtable
binding and a type library, which is real work rather than a call.

**2. Two backends, split along COM versus `ctypes` rather than along
accessibility versus input.** `uia` (elements, actions, element geometry,
hit-testing) registers at **90**, in the read-only band beside `atspi`;
`win32` (windows, screens, DPI, input, capture, clipboard) registers at **70**,
beside the Linux tool-backed input backend. `win32` and only `win32` answers
`windows()`, even though `uia` could: the objects it would hand back are
elements, not window handles, so every placement capability granted to it would
have nothing to point at. The composite's rule that one member owns a capability
family is satisfied by construction.

**3. Neither backend is `opt_in`.** This reverses the macOS plan, and
`register()`'s docstring is the reason: `opt_in` is reserved for a factory whose
*construction* raises an interactive consent dialog and blocks until someone
answers it. Constructing a `SendInput` wrapper does nothing at all; the side
effect is per call, exactly as it is for the Linux tool adapters, which register
at 70 without `opt_in`. Making the Windows input path opt-in would leave a
Windows session less capable by default than a Linux one for no reason a user
could observe.

**4. Module names: `backends/win32.py` and `backends/uia.py`.** The obvious name
— `backends/windows.py` — is taken, and by something else: it is the compositor
IPC module for sway, Hyprland, niri and KWin, registered as the `windows`
backend since long before this. No amount of `if sys.platform` in a docstring
makes that name mean what a Windows reader expects, so the platform backend is
`win32`, named after `sys.platform` as `SessionType.WIN32` is. It sits beside an
unrelated top-level `win32` package in a reader's mind when `pywin32` is
installed; absolute imports make that harmless, and nothing in this package ever
writes `import win32`.

**5. No Windows tool table in `tools.py`.** No CLI adapter, no
`_CAPTURE_TOOL_BY_COMPOSITOR` row, no PowerShell backend. A consequence worth
stating: a Windows session will never offer a `tool:name` provider the way
Linux does, and the docs say so where the tool names are listed, because a
reader who has learned the Linux composition model will look for the equivalent
and find none.

**6. DPI awareness: report it, then set it per thread — never process-wide.**
`Environment.dpi_awareness` carries the process's current mode, `notes` says
something when it is unaware, and the backend sets
`DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2` around its own calls, on the worker
thread it owns. Setting it process-wide at import is the option that looks
tidiest and is the worst: it changes how the *host application's* windows are
laid out, cannot be undone, and Windows may reset it anyway when a window tree
mixes modes — so a suite that imports pyguitest and then draws its own window
would be the victim. `Screen.scale` is then unambiguous: `GetDpiForMonitor`
divided by 96.

**7. Process: the machine-free half lands first, with its own tests and CI.**
The key tables, the control-type mapping, the probes and the packaging are
written from the documentation, tested against fakes, and run on a
`windows-latest` CI job for *collectability* before anything Windows-specific
exists — an import-time failure in a Linux-only module is the likeliest first
surprise, and finding it later costs more. The gate
(`scripts/pre-commit-test.sh`) is unchanged.

## Rejected alternatives

| Alternative | Why not |
|---|---|
| `pywin32` for the window, input, capture and clipboard calls | Every call needed here is one `ctypes` call away; it is a binary wheel whose availability tracks CPython releases; and taking both it and `comtypes` gives a project two COM initialisation strategies, of which one is always wrong |
| `pywinauto` as the element layer | It wraps the same APIs, so it does not remove the translation work; it has its own element vocabulary to translate into ours; and it is a tree-walking convenience rather than a capability model, which is the whole design here. Its value is as a reference implementation to read |
| `uiautomation` (the pure-Python wrapper) | Proof that UI Automation is reachable from `ctypes` + `comtypes` without a framework, and a good argument for not copying its element model |
| `pynput`, `keyboard` | Input hooks and injection that `SendInput` gives directly. `pynput`'s Windows module is worth reading when the recorder's backend is written; neither is worth depending on |
| `pyautogui`, `mss`, `Pillow.ImageGrab` | `SendInput` and `BitBlt` under another name; `png.py` already writes PNGs with no dependency at all |
| `dxcam` / `d3dshot` (DXGI Desktop Duplication) | A video-capture-rate tool for a screenshot API |
| `winsdk` / `pywinrt` (WinRT projections) | Only Windows.Graphics.Capture needs WinRT, so this belongs to a later phase and a decision made then |
| PowerShell through `Add-Type` | A working adapter in principle and a strictly worse one than in-process `ctypes`, which needs no marshalling at all. Its legitimate role is diagnostic output in `debug`, not a backend |
| `nircmd`, AutoHotkey | Third-party binaries: one not open source, the other a scripting host. For a library that can call the API, neither buys anything and both cost a packaging story |
| One backend instead of two | The `ctypes` half has no dependency and the COM half needs one, so a single backend would either drag `comtypes` onto every Windows install or hide which half is missing |
| `opt_in=True` for both, for parity with the macOS plan | See above: there is no consent dialog to avoid on Windows, and the cost would be a Windows session that does less by default than a Linux one |
| Registering both backends in the machine-free phase, with empty capability sets | A registered backend that answers `capabilities` with an empty set claims a session is supported while every call raises. Until `win32` can enumerate a window, the honest answer is the one already there: tier-1 capabilities and a reason |
| Setting per-monitor-v2 DPI awareness process-wide at import | Irreversible, affects the host application's own windows, and can be reset by the system anyway |

`docs/input.md` and `docs/install.md` carry the user-facing half of two of
these: no tool to install on Windows, and the Unicode route for typing.

## The two places this disagrees with the macOS plan

Both are questions a reviewer who has read that sibling document will ask, and
both are deliberate answers rather than oversights.

**`opt_in` is not symmetric between the platforms.** The macOS plan registers
both of its backends opt-in, because constructing either can raise a TCC consent
prompt — the exact case `register()`'s docstring reserves `opt_in` for. Windows
has no TCC, and UIPI's answer to a privilege mismatch is silence rather than a
dialog, so there is nothing to avoid. Copying the conclusion without its reason
would leave a Windows session whose input capabilities are absent from a plain
`connect()` while the equivalent Linux session has them. The asymmetry is the
honest description of the two platforms; the rule applied in both is
`register()`'s, not the other platform's answer.

**"There is no tool to adapt" is a finding, not a gap to fill.** ADR 001 and
ADR 002 chose, twice, to adapt a maintained command-line tool rather than take a
dependency, and both gave the same reason: on Linux the API sits behind a socket
or a compositor daemon, so a tool is a *smaller* dependency than a binding.
Windows inverts that — the DLLs are already loaded in every process and `ctypes`
is in the standard library, so no CLI can be a smaller dependency than nothing.
The rule therefore stops applying rather than being bent: the two ways of
keeping it (PowerShell through `Add-Type`, `nircmd`/AutoHotkey) are rejected
above for adding a process boundary and a packaging story to save nothing. The
one tool that survives on Windows is ImageMagick, because there genuinely is no
in-process equivalent for sub-image search — and it is the same tool Linux and
macOS already use.

## Consequences

- A bare `pip install pyguitest` on Windows covers everything except elements:
  windows, screens, DPI, input, capture and the clipboard are all `ctypes`. The
  `windows` extra adds exactly one thing, and `doctor` names it.
- The distribution stays a universal `py3-none-any` wheel with no
  platform-specific artifact to publish — a real contrast with Linux, where a
  session needs distribution packages pip cannot supply, and with macOS, where
  PyObjC's wheels are platform-specific.
- The tier-6 block reads oddly on Windows and is left that way. `POINTER_QUERY`,
  `INPUT_STATE_QUERY`, `WINDOW_TITLE_SET` and `WINDOW_LOWER` are all served
  there, under a heading whose own description says "deliberately prevented".
  The tier is documented as a *Wayland* ceiling and the docs say why; a Windows
  `report()` full of `[yes]` in T6 is the model working, not the model breaking.
- Two `compat.py` notes become platform-conditional: `GetMousePos` and
  `IsKeyPressed`/`IsMouseButtonPressed` have no honest implementation on Wayland
  and a direct one on Windows, so the same `gui.supports(...)` check answers
  differently on the two platforms. That is what the capability model is for,
  and a Windows `compat` table — a later phase — has to say so in its own notes
  rather than pretending the answer is universal.
- A Windows session can never offer a `tool:name` provider, because there is no
  Windows tool table. Stated here because the Linux composition model leads a
  reader to expect one.

## What has landed, and what has not

Landed, machine-free, tested with fakes on every platform:

- `SessionType.WIN32` and `Compositor.DWM`, the Windows `Environment` probes and
  notes, and the corrected `can_inject_input`, `input_transport` and
  `can_use_atspi` answers — the properties that were Linux-shaped and would have
  reported a working Windows session as broken;
- `backends/_winapi.py`: every DLL prototype, structure and constant the backend
  uses, plus the fixed-width field aliases that make the declared layout the
  Windows ABI rather than the host's — nothing loaded until a call needs it;
- `backends/win32.py` — `Win32Backend` itself, registered at 70, serving
  screens and per-monitor DPI, `SendInput` input, `EnumWindows` window control,
  the tier-6 state reads, GDI capture, the `CF_UNICODETEXT` clipboard, and
  `WINDOW_EVENTS` through `SetWinEventHook` and a pump thread installed for the
  life of one `window_events()` call; refusing `WINDOW_CAPTURE` and `sync` with
  the reason rather than with a generic "unsupported"; the virtual keys, the
  keysym mapping, the extended-key set and SendKeys' long names as data;
- `backends/uia.py` — `UiaBackend` itself, registered at 90, serving element
  search, element actions, element geometry and hit-testing through UI Automation
  by way of `comtypes`: the control types to role names, the property and pattern
  ids, the pattern actions, and the fold-pairwise condition builder that pushes
  every exact filter down to the providers while the predicate decides the
  answer. No `WINDOW_*` capability is claimed — that is `win32`'s half — and
  neither events nor `sync` are;
- the Windows hints, `detect_distro()`'s branch for Windows, Cygwin and MSYS2,
  the `windows` extra and its classifiers, the `windows-latest` collectability
  job, `import grp` moved out of module scope so the package imports at all on
  Windows, and this file.

Also landed: `Session.wait_for_idle` through `GetProcessTimes`, at a resolution
neither /proc nor `ps` can match, with the same exited-versus-unreadable
distinction the Linux implementation insists on (`OpenProcess`'s
`ERROR_ACCESS_DENIED` is the "cannot read" case; anything else failing the open
reads as "gone").

Not yet: the recorder's Windows backend and `WINDOW_CAPTURE` through
`PrintWindow`.

**A gap this document's own audit missed, now closed with a documented cut:**
`Session.wait_for_process` -- `PROCESS_LAUNCH`/`TIMING`, which section 6.4 of
the design document calls "✔ always" -- was built on the same `/proc`-or-`ps`
process table `wait_for_idle` was, and neither exists on Windows, so it raised
`PyGUITestError` there. It is now implemented through
`CreateToolhelp32Snapshot`/`Process32FirstW`/`Process32NextW`
(`_windows_process_table` in `pyguitest/__init__.py`, beside
`_windows_process_cpu_seconds` and under the same rule: these process probes
stay independent of any backend).

The decision this document left open was name-only versus WMI, and the answer
is **name-only**. Toolhelp reports `szExeFile` -- the executable's filename,
not the command line -- so on Windows the pattern is matched against
`"notepad.exe"` where every other platform matches the whole argument vector.
WMI's `Win32_Process.CommandLine` is the only route to the wider answer and it
costs a `wmic`/PowerShell subprocess of roughly a second, paid on *every poll*
of a loop whose default `interval` is 0.5s -- for a field most callers are not
matching on. Toolhelp, by contrast, opens no process handle, so it needs no
privilege and sees services and other users' sessions as readily as this
user's own; a table with holes in it would be worse than the narrower one.

The cost of the cut is that a pattern depending on an argument matches nothing
and times out rather than raising, which is strictly less loud than the old
refusal. That is paid for in documentation rather than in code -- the method's
own docstring, `docs/troubleshooting.md`'s section, and this paragraph -- on
the grounds that `wait_for_process("notepad")` is the common case and was
previously impossible, while the argument case has a good answer already
(start the process yourself and hand `wait_for_idle` the pid). Revisit only if
WMI's cost can be paid once rather than per poll.

**None of the above has been run on a Windows machine.** Every Windows-specific
prototype, structure layout and probe is transcribed from the documentation, and
[docs/validation.md](../../docs/validation.md) is where each claim is retired as
it is measured — which is why the phase order put everything that can be
reviewed without a machine first.


