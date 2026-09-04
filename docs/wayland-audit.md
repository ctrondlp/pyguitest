# X11::GUITest on Wayland

Every symbol in `@EXPORT_OK`, classified by what it would actually take to make it work in a Wayland session. Derived from the module source in `GUITest.pm` and `GUITest.xs` — not from the documented feature list, which turns out to describe a different module than the one in the tree. **Thirteen of fifty carry over unchanged.**

## The distribution is the finding

The largest block is not "impossible" — it is "possible, but you must write it once per desktop." Window management is where a portable Wayland implementation actually fails.

| Tier | Count | Share of 50 |
|---|---|---|
| **T1** Portable | 9 | 18% |
| **T2** Direct | 4 | 8% |
| **T3** Compositor | 19 | 38% |
| **T4** Privileged | 8 | 16% |
| **T5** Rework | 4 | 8% |
| **T6** No path | 6 | 12% |

- **13** — Port unchanged — process control, delays, string quoting, screen geometry.
- **19** — Need a separate backend for GNOME, KDE and wlroots. All window-related.
- **8** — Input injection. One mechanism, gated on consent or device permissions.
- **6** — No path for an unprivileged client on any compositor. Drop from the API.

## Six tiers, ordered by cost

The tiers are a scale, not a set of labels: each one costs strictly more to implement than the one above it, and T6 cannot be bought at any price without privileged access.

**T1 PORTABLE** — *No display server involved*  
  Pure Perl or libc. Runs identically on Linux, the BSDs, Solaris and Windows. Free.

**T2 DIRECT** — *Core Wayland protocol*  
  A stable, universally implemented interface gives a real equivalent. Write once.

**T3 COMPOSITOR** — *Per-desktop backend*  
  No universal protocol. Needs distinct code for wlroots, KWin and Mutter — and Mutter implements none of the foreign-toplevel protocols at all.

**T4 PRIVILEGED** — *Consent or device access*  
  Portal dialog, membership of the `input` group, or a wlroots-only protocol. Never silent on GNOME or KDE.

**T5 REWORK** — *Goal survives, model does not*  
  The X11 mechanism has no analogue, but the reason you called it is served better by AT-SPI. Same intent, different API shape.

**T6 NO PATH** — *Deliberately prevented*  
  Wayland exposes no way to do this, by design, on any compositor. Not a gap awaiting a protocol.

## Process control and module state

These never touched the X server. They are the free half of the port, and the only part that keeps X11::GUITest's existing FreeBSD and Solaris support intact.

| Function | X11 implementation | Wayland path | Tier |
|---|---|---|---|
| StartApp | fork / exec | *subprocess.Popen* — unchanged | T1 |
| RunApp | system() | *subprocess.run* — unchanged | T1 |
| WaitSeconds | sleep() | *time.sleep* — unchanged | T1 |
| QuoteStringForSendKeys | string substitution | Pure string handling. Keep only if the `{}` SendKeys grammar is kept | T1 |
| QSfSK | alias | Alias of the above. Drop the abbreviation in a new API | T1 |
| SetEventSendDelay | module global | Module state. Better expressed as a constructor argument | T1 |
| GetEventSendDelay | module global | Module state | T1 |
| SetKeySendDelay | module global | Module state | T1 |
| GetKeySendDelay | module global | Module state | T1 |

## Screen properties

The one area where Wayland's core protocol answers the same question X11 did. Every compositor implements `wl_output`; no permission, no backend.

| Function | X11 implementation | Wayland path | Tier |
|---|---|---|---|
| GetScreenRes | DisplayWidth / Height | *wl_output.mode* — direct equivalent, and reports scale, transform and refresh besides | T2 |
| ScreenCount | ScreenCount() | *wl_output* globals in the registry. Note the semantics shift: X11 screens are rare, Wayland outputs are just monitors | T2 |
| DefaultScreen | DefaultScreen() | No such concept. Return the first advertised output by convention | T2 |
| GetScreenDepth | DefaultDepth() | Not exposed — and no longer variable. Every compositor composites 8 bits per channel; return 24 or 32 as a constant, or drop it | T2 |

## Input injection

Mechanically the easiest group to make work and the most consequential to get wrong. All eight reduce to one backend decision, discussed under [the keymap trap](#keymap) below.

| Function | X11 implementation | Wayland path | Tier |
|---|---|---|---|
| MoveMouseAbs | XTestFakeMotionEvent | *NotifyPointerMotionAbsolute* via the RemoteDesktop portal, libei, or a uinput device with `ABS_X`/`ABS_Y`. Relative-only injection cannot implement this | T4 |
| PressMouseButton | XTestFakeButtonEvent | *NotifyPointerButton* / `EV_KEY``BTN_LEFT` | T4 |
| ReleaseMouseButton | XTestFakeButtonEvent | As above, release state | T4 |
| ClickMouseButton | press + release pair | Composed from the two above. Buttons 4 and 5 must become *axis* events, not button events — scroll is not a button on Wayland | T4 |
| PressKey | XTestFakeKeyEvent | *NotifyKeyboardKeycode* / `EV_KEY`. Takes a keycode, so the keymap problem applies | T4 |
| ReleaseKey | XTestFakeKeyEvent | As above, release state | T4 |
| PressReleaseKey | press + release pair | Composed from the two above | T4 |
| SendKeys | XKeysymToKeycode + XGetKeyboardMapping | The `{}` grammar and modifier synthesis port cleanly; the keysym-to-keycode lookup does not. There is no Wayland call that answers "which keycode produces this character" | T4 |

## Windows: discovery and control

Two protocols cover part of this — `ext-foreign-toplevel-list-v1` (read-only: title, app id, identifier) and wlroots' `zwlr_foreign_toplevel_management_v1` (adds activate, minimize, close). KWin and wlroots implement them. **Mutter implements neither**, so on GNOME — the likely default session for most users — every row below needs a Shell extension or a D-Bus `Eval` call that is itself locked down. Neither protocol reports window *geometry* at any point.

| Function | X11 implementation | Wayland path | Tier |
|---|---|---|---|
| GetWindowName | XFetchName, _NET_WM_NAME | *toplevel.title* event. Arrives asynchronously and is pushed, not polled — a genuine improvement | T3 |
| FindWindowLike | XQueryTree + regex on WM_NAME | Regex over the toplevel list. Returns only toplevels — the recursive descent into child windows this relies on has no equivalent (see T5) | T3 |
| WaitWindowLike | poll FindWindowLike | Replace polling with the *toplevel* creation event. Faster and race-free | T3 |
| WaitWindowClose | poll IsWindow | *toplevel.closed* event. Strictly better than the polling loop it replaces | T3 |
| WaitWindowViewable | poll XGetWindowAttributes | Approximate via the `minimized` state flag. "Viewable" as X11 defines it — mapped, with all ancestors mapped — has no counterpart | T3 |
| IsWindow | XGetWindowAttributes | Liveness of a toplevel handle. Handles are objects, not reusable integer IDs — safer than X11 window IDs | T3 |
| IsWindowViewable | IsViewable attribute | `minimized` state flag, with the same caveat as WaitWindowViewable | T3 |
| GetWindowPid | _NET_WM_PID property | Not carried by either foreign-toplevel protocol. KWin scripting or a GNOME extension only. Frequently unavailable | T3 |
| GetWindowsFromPid | scan tree for _NET_WM_PID | Inherits the above. Consider matching on `app_id` instead — it is always present and more stable than a pid | T3 |
| GetWindowPos | XTranslateCoordinates + XGetWindowAttributes | **No protocol reports this.** A client is not told where it is on screen, and no protocol tells a third party either. Compositor scripting only | T3 |
| MoveWindow | XMoveWindow | No protocol. KWin scripting or a GNOME extension. Placement is the compositor's prerogative | T3 |
| ResizeWindow | XResizeWindow | No protocol. Same compositor-scripting escape hatch | T3 |
| RaiseWindow | XRaiseWindow | Approximate with *activate*, which also transfers focus. There is no raise-without-focus operation | T3 |
| SetInputFocus | XSetInputFocus | *toplevel.activate*, requires a seat. The compositor may refuse or defer it under focus-stealing policy | T3 |
| GetInputFocus | XGetInputFocus | The `activated` state flag on wlroots toplevels. Absent from `ext-foreign-toplevel-list-v1`, which carries no state at all | T3 |
| IconifyWindow | XIconifyWindow | *set_minimized*. One of the few window operations with a real protocol call | T3 |
| UnIconifyWindow | XMapWindow | *unset_minimized* | T3 |
| GetWindowFromPoint | stacking-order scan of child geometry | Requires per-window geometry and stacking order, neither of which is exposed. Compositor scripting only | T3 |
| ClickWindow | GetWindowPos + MoveMouseAbs + click | Inherits the geometry gap and the injection permission both. Replace with an AT-SPI element click, which needs neither | T3 |

## The window tree

X11 exposes every widget as a nestable window, and X11::GUITest is built on walking that tree. Wayland has no tree: one surface per toplevel, and what is inside it is the toolkit's private business. This is the deepest architectural break in the module — and the one already anticipated in the repository's own `ToDo` file, which names AT-SPI as the fix.

| Function | X11 implementation | Replacement | Tier |
|---|---|---|---|
| GetRootWindow | RootWindow() | No root window exists. The nearest concept is *the desktop*, an AT-SPI application collection | T5 |
| GetChildWindows | recursive XQueryTree | Recurse the *AT-SPI accessible tree* instead: real roles and labels rather than anonymous window IDs | T5 |
| GetParentWindow | XQueryTree parent | AT-SPI *parent* accessor | T5 |
| IsChild | scan GetChildWindows | AT-SPI ancestry check | T5 |

This break is not new with Wayland. Client-side decorations and toolkit-drawn widgets already made the X11 window tree mostly empty for modern GTK and Qt applications — a single window where X11::GUITest expects dozens. AT-SPI is the correct answer under X11 too.

## No path at any privilege short of the compositor

These are not awaiting a protocol. Each one is a capability Wayland removed on purpose, because it lets one client observe or impersonate another. Do not carry them into the new API with a stub that always fails — leave them out and document why.

| Function | X11 implementation | Why it cannot work | Tier |
|---|---|---|---|
| GetMousePos | XQueryPointer | No protocol reports the global pointer position; a client learns coordinates only inside its own surface, only while pointed at. *Injection is possible, readback is not* — track the position you last set | T6 |
| IsKeyPressed | XQueryKeymap | Global keyboard state is exactly what a keylogger reads. Only readable by opening `/dev/input/event*` directly, which is an OS bypass, not a Wayland path | T6 |
| IsMouseButtonPressed | XQueryPointer mask | As above — global input state is not observable | T6 |
| SetWindowName | XSetWMName, XSetWMIconName | A title belongs to the client that owns the surface. Nothing can rewrite another application's title — that is impersonation, and the protocol has no request for it | T6 |
| LowerWindow | XLowerWindow | No foreign-toplevel protocol offers a lower or restack operation. Only activate exists, and only upward | T6 |
| IsWindowCursor | XTestCompareCursorWithWindow | Cursor shape is negotiated privately between client and compositor. No protocol, no portal, no scripting interface reports it. No Wayland workaround exists — though X11 keeps `XTestCompareCursorWithWindow`, so this one survives on that backend alone | T6 |

## What the design note got wrong about the module

The capability list in the planning document does not match `@EXPORT_OK`. Four claims are wrong in a direction that matters for scoping.

1. **There is no screenshot function.** Nothing in the 50 exports captures pixels; there is no image handling in the module at all. Mapping "screenshot" to the desktop portal describes a *new feature*, not a port — which is good news, because the portal makes it easy, but it should not be counted as compatibility work.
2. **Windows are not matched by class.**`FindWindowLike` matches `WM_NAME` and `_NET_WM_NAME` only; `WM_CLASS` appears nowhere in the source. Wayland's `app_id` is in fact a *better* match key than anything the module has today — worth adding rather than reproducing.
3. **There is no application enumeration.** Discovery is a recursive `XQueryTree` walk plus `_NET_WM_PID` lookups. The distinction matters: what needs replacing is a tree walk, not an application list.
4. **About half the surface is unlisted.** Missing from the note: the four delay accessors, the three input-state queries, `IsWindowCursor`, `GetScreenDepth`, `ScreenCount`, `StartApp`, `RunApp` and the SendKeys quoting helpers — 15 of 50.

## Three mechanisms the note does not mention

The note proposes uinput as the input backend. That was the right answer several years ago and is now the fallback, not the primary.

### libei

The emulated-input stack Wayland compositors actually adopted — supported by Mutter and KWin, brokered through the portal so it needs no root and no device nodes. **This should be the primary input backend**, with uinput kept only for compositors and BSDs that lack it.

Reaches native Wayland clients, which XTest under XWayland never will.

### The RemoteDesktop portal

`org.freedesktop.portal.RemoteDesktop` grants pointer and keyboard injection after one consent dialog, and pairs with ScreenCast to get capture in the same session. Cross-desktop, unprivileged, and the only sanctioned route on GNOME.

Consent is per session — design the API so a test suite acquires it once at startup, not per call.

### Virtual device protocols

`zwp_virtual_keyboard_manager_v1` and `zwlr_virtual_pointer_manager_v1` give unprivileged injection on wlroots compositors with no dialog at all — the closest thing to XTest's old ergonomics that still exists.

Also the cleanest fix for the keymap problem, because the client supplies its own keymap.

### The keymap trap

uinput injects scancodes *below* the compositor, so the compositor applies whatever xkb layout is active. `SendKeys("Hello")` on an AZERTY or Dvorak session types different characters — and no protocol lets you ask which layout is in effect.

X11::GUITest avoided this with `XKeysymToKeycode` against the server's live map. libei and virtual-keyboard both restore that guarantee; raw uinput cannot.

## What this implies for the new package

- **Keep the X11 backend as a peer, not a legacy path.** It is the only backend that can serve FreeBSD and Solaris — libei and uinput are Linux interfaces, and neither of those systems runs a Wayland compositor you would target. The existing XS code keeps its value.
- **Make capability negotiation part of the public API.** With 19 functions varying by compositor and 6 unavailable everywhere, a call that silently returns zero — the current failure convention — becomes untestable. Let callers ask what the active backend supports before they depend on it.
- **Lead with AT-SPI, not coordinates.** It is the only layer that answers the question the window-tree functions were really being used for, it is unaffected by the T3 and T6 gaps entirely, and it works identically under X11 and Wayland — which makes it the one part of the design that does not need a backend matrix.
- **Do not reproduce the 1-to-1 API.** Of 50 functions, 13 port unchanged, 4 want new semantics, 6 should not exist, and the rest change shape. A faithful port would spend most of its effort on the parts worth redesigning.

Source of record: `GUITest.pm` (`@EXPORT_OK`, 50 symbols) and `GUITest.xs` (39 XS functions) at version 0.29.

Tier assignments reflect protocol availability as of the audit date; `ext-foreign-toplevel-list-v1` adoption in particular is still moving, and any Mutter change to it would move several T3 rows.
