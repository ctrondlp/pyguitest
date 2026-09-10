# Troubleshooting

Symptom first, then what causes it, then what to do. If your problem is not
here, `pyguitest debug` collects everything a bug report needs — see
[Which tool to reach for](#which-tool-to-reach-for) at the end.

- [`gui.button("Save")` raises ElementNotFound](#guibuttonsave-raises-elementnotfound)
- [One application has no accessible elements at all](#one-application-has-no-accessible-elements-at-all)
- [Injected input does nothing](#injected-input-does-nothing)
- [The wrong characters get typed](#the-wrong-characters-get-typed)
- [CapabilityUnsupported on a window operation](#capabilityunsupported-on-a-window-operation)
- [`geometry()` reports a position nowhere near the window](#geometry-reports-a-position-nowhere-near-the-window)
- [Pointer and key-state reads look stale](#pointer-and-key-state-reads-look-stale)
- [Focus assertions never match anything](#focus-assertions-never-match-anything)
- [The clipboard reads back empty](#the-clipboard-reads-back-empty)
- [Screenshots fail, or contain the wrong thing](#screenshots-fail-or-contain-the-wrong-thing)
- [`connect()` raises BackendUnavailable](#connect-raises-backendunavailable)
- [A test passes locally and fails in CI](#a-test-passes-locally-and-fails-in-ci)
- [Which tool to reach for](#which-tool-to-reach-for)

## `gui.button("Save")` raises ElementNotFound

Three causes, in the order worth checking.

**The name is not what you think.** Accessible names are not always the
visible label — a toolbar button may be named "Save document", or carry a
tooltip as its description instead. Look before guessing:

```sh
pyguitest inspect --window "Text Editor"
```

**The role is not what you think.** `gui.button()` looks for a push button.
A toolbar item may be a toggle button, a menu item, or a plain image inside a
clickable container. `gui.element(name="Save")` without a role tells you
whether the name exists at all; the tree dump above tells you what it is.

**The application is slow to publish it.** A control that appears after a
dialog opens is not there when the line runs. That is what the waits are
for:

```python
gui.wait_for_element(name="Save", timeout=10).click()
```

If nothing in the application is findable, the next entry is the one you
want.

## One application has no accessible elements at all

Chromium, Electron, VS Code, Slack and anything else on that stack publish
**nothing** to the accessibility bus until something announces that an
assistive technology is running. The symptom is confusing rather than
obvious: `gui.windows()` lists the window (that comes from the compositor)
while the application has no node in the tree whatsoever — so
`window_element()` and every element query behave as though it were not
running.

Ask directly:

```python
from pyguitest.session import assistive_technology_enabled

print(assistive_technology_enabled())  # False is the usual answer
```

`pyguitest debug` reports the same thing on its `chromium a11y` line. The fix
depends on who starts the application: launch it with
`--force-renderer-accessibility` for a test that starts it itself, or set
`org.a11y.Status.IsEnabled` true session-wide, which works everywhere but
costs those applications real performance.
[install.md](install.md#chromium-and-electron-apps-need-an-at-to-be-announced)
has the exact commands.

## Injected input does nothing

Work down this list; it is ordered by how often each one is the answer.

1. **Is there an input path at all?** `pyguitest doctor` says what is missing
   and how to install it. Injection needs either `/dev/uinput` permission, a
   `ydotool` daemon, libei, or portal consent — none of which is present by
   default on every desktop.
2. **Did a consent dialog appear and get dismissed?** Portal-based input
   raises one, and a dismissed prompt looks exactly like injection silently
   failing.
3. **Is something else driving the pointer?** On a VirtualBox guest, *Mouse
   Integration* continuously overrides every injected pointer position —
   clicks land and hover fires, but the cursor never visibly moves. Host+I
   turns it off.
4. **Did the events reach the kernel and the session?**

   ```sh
   sudo libinput debug-events --device /dev/input/eventN
   loginctl seat-status seat0
   ```

[input.md](input.md#when-injected-input-appears-to-do-nothing) is the full
version of this list, with the per-backend detail behind each step.

## The wrong characters get typed

`type_text("Hello")` producing something else means the injection path is
below the layer that knows your keyboard layout. `ydotool` and raw `uinput`
inject scancodes *underneath* the compositor, and no protocol reports the
active layout back to them, so on a non-US layout the characters differ.

pyguitest ranks input tools by exactly this property and warns during
detection when only keymap-unsafe ones are present. Prefer `wdotool`,
`wtype`, or the libei backend; on X11 the X11 backend resolves characters
through the server's own keymap. [input.md](input.md#keymap-safety) explains
the ranking, and `pyguitest doctor` says which tools you have.

A separate, narrower case, now fixed: characters that need a group switch
(AltGr on many layouts) rather than Shift. `type_text()` holds the server's
own group-switch key for those, not Shift, and raises `CapabilityUnsupported`
for a character in a keyboard group the server has no switch key for at all,
rather than typing the wrong (group-1) character silently. If a character
still comes out wrong, check `gui._group_switch_keycode()` for `None` first —
that means this X server has no `ISO_Level3_Shift` or `Mode_switch` key at
all, which no workaround here can supply.

## CapabilityUnsupported on a window operation

This is the library working as designed: the desktop you are on does not
offer that operation, and pyguitest refuses to pretend otherwise rather than
doing nothing quietly.

```python
print(gui.report())  # the whole support table for this session
gui.supports(Capability.WINDOW_PLACEMENT)
```

Window control is the area that varies most. GNOME, KDE, sway, Hyprland and
niri each expose a different subset, and Mutter implements neither
foreign-toplevel protocol — which is why GNOME needs the optional
`pyguitest-window-control` extension for the full set. `pyguitest doctor`
says whether it is installed.

Write against the capability rather than the desktop name:

```python
if gui.supports(Capability.WINDOW_PLACEMENT):
    gui.move_window(window, 100, 100)
else:
    ...  # skip the test, or take a different route
```

## `geometry()` reports a position nowhere near the window

Confirmed live on **GNOME's XWayland specifically**: `move_window()` visibly
moves the window to the right place, and a `geometry()` call immediately
after reads back a position nowhere near it.

The working hypothesis is that Mutter's XWayland integration does not keep
the decoration-frame position it reports over X11 in sync with where the
window actually renders — the same mechanism `xdotool` and `wmctrl` read, so
they are affected identically. It is one diagnostic session on one machine,
not an independently confirmed root cause.

Practical advice: treat `geometry()` results as suspect on GNOME's XWayland,
and do not build a test on reading a position back after setting it. Under a
native X11 session and on other compositors this does not apply. Full write-up
in [validation.md](validation.md#known-caveat-geometry-on-gnomes-xwayland).

## Pointer and key-state reads look stale

`pointer_position()`, `is_key_pressed()` and `is_button_pressed()` are X11
only by design — no Wayland compositor allows reading global input state,
because that is what a keylogger reads.

Under **XWayland** they exist and answer, but only for X's world, and where
they cannot answer they return **the last value they had rather than an
error**. Measured on GNOME Shell 51.beta: commanded over an X surface, the
pointer query matched to within rounding; commanded over a native Wayland
surface, it went on reporting the previous position indefinitely. Key state
is accurate only while an X client holds focus.

A stale coordinate is a well-formed answer, so this fails silently — a test
asserting on it fails somewhere else entirely, or passes for the wrong
reason. Trust a tier-6 read under XWayland only when you know an X client is
under the pointer or holding focus. On a real X11 session none of this
applies. Detail in
[validation.md](validation.md#known-caveat-the-tier-6-queries-under-xwayland).

## Focus assertions never match anything

`focused()`, `assert_focused()` and `assert_tab_order()` depend on the
desktop publishing per-widget keyboard focus to the accessibility bus. Some
do not. On GNOME Shell, measured across GTK3, GTK4 and VTE applications,
`FOCUSED` was carried by exactly one element session-wide — the shell's own
toplevel — and by no widget in any application.

The assertions are correct and unit-tested; there is simply nothing for them
to match. Probe before relying on them:

```python
if gui.focus_tracking_works():
    gui.assert_focused(name="Email")
```

`pyguitest debug` reports the same probe. Note this does not affect
`active_window()`, which reads a different mechanism and stays correct.

## The clipboard reads back empty

The failure mode worth knowing: on the portal backend, `set_clipboard()`
hands the portal **no bytes**. It declares ownership and then has to answer a
`SelectionTransfer` signal once per paste, from a GLib loop on a daemon
thread. When that answering machinery does not run, nothing raises — the
clipboard simply reads as empty to every application.

Check with a *separate process*, not with `get_clipboard()` in the same one:

```sh
xclip -selection clipboard -o     # X11 / XWayland
wl-paste                          # native Wayland
```

The write path is confirmed working on GNOME Wayland (a real `xclip` in
another process pasted back exactly what was written). If yours reads empty,
capture `pyguitest debug` output and the clipboard tools it lists —
`ToolClipboardBackend` and the portal backend fail in different ways, and
which one you are on decides the fix. See
[validation.md](validation.md) for what has been measured on which desktop.

## Screenshots fail, or contain the wrong thing

**A window screenshot includes what is on top of it.** There are two
mechanisms and the difference is visible in the image: under X11 the
window's own pixels are read, so anything stacked over it is absent;
everywhere else the rectangle is looked up and cut out of a full-screen
capture, which includes whatever covers it.

```python
gui.supports(Capability.WINDOW_CAPTURE)  # True = the native, un-occluded path
```

**Capture failed entirely.** Capture needs either a tool, a portal, or the
X11 backend. `pyguitest doctor` reports which are available and
[install.md](install.md#how-capture-picks-a-path) explains the order they are
tried in. Portal capture raises a consent dialog, which — dismissed — looks
like failure.

## `connect()` raises BackendUnavailable

`connect()` is deliberately hard to fail: a session with almost no
capabilities is normal and returns a working object. `BackendUnavailable`
means no backend at all could drive this session — typically no display
server (a bare ssh session), or a container that cannot reach the host's
buses and sockets.

```sh
pyguitest debug     # says which of $DISPLAY, $WAYLAND_DISPLAY, the buses
                    # and the sockets it could see, and whether it is
                    # running inside a Flatpak or container
```

Container and Flatpak sandboxes change what every other probe sees, which is
why `debug` reports the sandbox explicitly rather than leaving you to infer
it from a wall of `False`.

## A test passes locally and fails in CI

Almost always a capability difference rather than a timing one. The runner
has a different display server, usually no compositor, and none of your
desktop's optional pieces.

Put this in the job and keep it as an artifact:

```sh
pyguitest debug --json > pyguitest-debug.json
```

Then compare it against the same file from your machine. The three usual
answers: the runner has no accessibility bus (so every element query fails),
no capture path, or no input permission. [recipes.md](recipes.md#running-in-ci)
covers the three arrangements that do work.

## Which tool to reach for

| Question | Command |
|---|---|
| What can this desktop do? | `pyguitest` |
| What should I install to unlock more? | `pyguitest doctor` |
| What do I paste into a bug report? | `pyguitest debug` (`--json` for a file) |
| What is there for my script to match on? | `pyguitest inspect --window "..."` |
| What would porting this Perl script involve? | `pyguitest migrate script.pl` |

All of them also work as `python3 -m pyguitest …` from a checkout, without
installing anything.
