# Choosing a call

[api.md](api.md) tells you what a call does once you know its name. This page
is the step before that: given what you are trying to do, which name. Every
call links into the reference.

One fork is not here because it has a better home: how to *address* a control
in the first place — named element, constrained element, element scoped to a
window, image match, raw coordinate — is the locator ladder in
[getting-started.md](getting-started.md#which-api-should-i-use). Everything
below assumes you have chosen a rung on it.

## Find it now, or wait for it

| You want to… | For an element | For a window |
|---|---|---|
| Fail at this line if it is not there | [`button("Save")`](api.md#button), [`element(...)`](api.md#element) | [`find_window("Preferences")`](api.md#find_window) |
| Poll, then branch on what you got | [`wait_for_element(...)`](api.md#wait_for_element) | [`wait_for_window(...)`](api.md#wait_for_window) |
| Poll, then fail naming what was found | [`expect_element(...)`](api.md#expect_element) | [`expect_window(...)`](api.md#expect_window) |

The plain lookups never return `None` — they raise `ElementNotFound` or
`WindowNotFound` at the line that asked, so there is nothing to test for.
Reach for them when the thing should already be on screen, and for a `wait_*`
or `expect_*` when you have just done something that makes it appear.

The `wait_*` pair hands back a value or `None` and leaves the judgement to
you; the `expect_*` pair raises. Note the deadline: `expect_element` and
`expect_window` default to `timeout=None` and wait indefinitely, matching the
waits they wrap, so pass a `timeout` in any suite that must not hang.

## Checking state: `expect_` or `assert_`

| Family | The question it answers | Deadline |
|---|---|---|
| [`expect_*`](api.md#expectations) | *Does it become this?* | Polls until `timeout`, `5.0` seconds for the state checks |
| [`assert_*`](api.md#assertions) | *Is it this now?* | Reads once and fails; no polling |

Both raise on failure with a message naming what was actually found. The
difference is whether time is allowed to pass: after an action whose effect
arrives when the application gets round to it, use
[`expect_text`](api.md#expect_text), [`expect_value`](api.md#expect_value),
[`expect_checked`](api.md#expect_checked),
[`expect_enabled`](api.md#expect_enabled) or
[`expect_showing`](api.md#expect_showing). For something already settled, or
for a property of the session rather than a value in flight — where focus is,
what the clipboard holds, whether Tab visits the right controls, whether
anything is missing an accessible name — use the `assert_*` calls.

[`assert_tab_order`](api.md#assert_tab_order) is the one `assert_` with a
`timeout`, and it is not a polling deadline: it is how long each Tab press is
given for focus to settle before the order is judged.

## Which wait

| Waiting for… | Use |
|---|---|
| A window to appear | [`wait_for_window`](api.md#wait_for_window) / [`expect_window`](api.md#expect_window) |
| A window to close | [`wait_window_close`](api.md#wait_window_close) |
| A window to actually hold keyboard focus | [`wait_window_focus`](api.md#wait_window_focus) |
| A control to appear | [`wait_for_element`](api.md#wait_for_element) / [`expect_element`](api.md#expect_element) |
| A spinner, toast or "Saving…" label to go away | [`wait_until_gone`](api.md#wait_until_gone) |
| Any condition you can write as a function | [`wait_until(predicate)`](api.md#wait_until) |
| The compositor to have taken the input you injected | [`sync()`](api.md#sync) |
| A process to be running | [`wait_for_process`](api.md#wait_for_process) |
| A process to stop burning CPU | [`wait_for_idle`](api.md#wait_for_idle) |
| A file to be written | [`wait_for_file`](api.md#wait_for_file) |
| A real person to cross a screen edge | [`wait_for_pointer_activation`](api.md#wait_for_pointer_activation) |
| Nothing in particular | [`wait(seconds)`](api.md#wait) — the one to avoid |

`timeout` left `None` waits forever, and that is the default everywhere here
except `sync`. `sync` is the odd one out in what it waits *on*: it needs
`INPUT_SYNC` (libei today, via a ping/pong round trip) and raises
`CapabilityUnsupported` where no backend can answer, rather than no-op'ing
into exactly the false confidence it exists to remove. And it proves the
compositor consumed the events, never that the application processed or
repainted them — `wait_until` on the state you care about is what answers
that.

## Clicking: which route

| You want… | Use |
|---|---|
| The application's click handler, by whatever route works | `element.click()` |
| A real pointer press on the element's rectangle | [`click_element(element)`](api.md#click_element) |
| A press wherever the pointer already is | [`click(button)`](api.md#click) |
| A double click | `element.double_click()` or [`double_click_element(element)`](api.md#double_click_element) |

`element.click()` follows [`gui.click_via`](api.md#click_via): `"auto"` tries
the accessible action and falls back to the pointer, `"action"` uses the
action only, `"pointer"` uses the pointer only. Set it once and a whole
suite's policy changes.

`click()` takes no coordinates — move the pointer first with
[`move_mouse`](api.md#move_mouse) or [`glide`](api.md#glide). And two
`click()` calls are not a double click: each is a separate round trip, slower
than any toolkit's double-click interval, so the pair arrives as two singles
and the folder icon does not open.

## Moving the pointer

| You want to… | Use |
|---|---|
| Get the pointer somewhere so you can click | [`move_mouse`](api.md#move_mouse) — teleports |
| Cross things that watch the path: drag thresholds, hover reveals, tooltips, hot corners | [`glide`](api.md#glide) — a stream of events, straight unless `via` names waypoints |
| Make the motion look like a hand did it | [`move_mouse_naturally`](api.md#move_mouse_naturally) |
| Drag between two points, or two controls | [`drag`](api.md#drag) / [`drag_element`](api.md#drag_element) |

`glide` is machine-shaped on purpose — straight legs, constant velocity — and
is what you want whenever the *path* matters but its realism does not.
`move_mouse_naturally` is the deliberate exception, for a region that reacts
to the pointer being over it and for a take that has to look human.

## Typing and keys

| You want to… | Use |
|---|---|
| Set a field's value, where the value is the point | [`element.set_text(text)`](api.md#set_text) |
| Exercise the application's own key handling | [`type_text(text)`](api.md#type_text) |
| Send chords and named keys — `^(s)`, `{F12}`, `%({F4})` | [`send_keys(keys)`](api.md#send_keys) |
| Type to a human rhythm, or into a field that reacts to timing | [`type_text_naturally(text)`](api.md#type_text_naturally) |
| Press one key | [`tap_key(key)`](api.md#tap_key) |
| Hold a key down across other input | [`press_key`](api.md#press_key) + [`release_key`](api.md#release_key) |
| Pick an entry from a dropdown | [`element.choose("Option")`](api.md#choose) |

`set_text` and `choose` go through the accessibility interface and send no key
events; everything else goes to whatever holds keyboard focus, so focus a
window or a control first or the keystrokes land somewhere else. Typed text
also depends on the session keymap — [input.md](input.md) covers when that
matters.

## A window handle, or a window's elements

| You want to… | Use |
|---|---|
| Move, resize, close, focus or measure a window | The `Window` from [`find_window`](api.md#find_window) / [`wait_for_window`](api.md#wait_for_window) |
| Scope an element search to one window | The `Element` from [`window_element(title)`](api.md#window_element), passed as `within=` |
| Re-read a `Window` whose title may have changed | [`refresh_window`](api.md#refresh_window) |

A `Window` is the backend-agnostic handle the window operations take; a
window's `Element` is the root of its accessible subtree. A `Window`'s
`title`, `app_id` and `pid` are read when it is made and do not update
themselves.

## Focus

| You want to… | Use |
|---|---|
| Ask the window manager and carry on | [`activate_window`](api.md#activate_window) |
| Ask, retry, wait for the geometry to settle, and confirm | [`focus_window`](api.md#focus_window) |
| Confirm focus landed after your own `activate_window` | [`wait_window_focus`](api.md#wait_window_focus) |
| Give one control keyboard focus | [`element.focus()`](api.md#focus) |
| Assert where focus is | [`assert_focused`](api.md#assert_focused) |

`focus_window` is the one to use before typing: `activate_window` asks once
and returns immediately, and a window manager may take a moment or refuse
outright under a focus-stealing policy. `focus_window` returns whether focus
ended up there rather than raising, so branch on it.

On an unknown desktop, ask [`focus_tracking_works()`](api.md#focus_tracking_works)
before depending on per-widget focus: where a desktop does not publish it at
all, `assert_focused` raises for every element, which reads as a test failure
rather than the unsupported-desktop answer it is.

## Capture, and finding an image

| You want to… | Use |
|---|---|
| One image, now | [`screenshot`](api.md#screenshot) |
| Everything needed to diagnose a failure | [`capture_on_failure(directory)`](api.md#capture_on_failure) |
| Find a known image on the live screen | [`locate_image(path)`](api.md#locate_image) |
| Find one inside an image you already have | [`locate(haystack, template)`](api.md#locate) |

`capture_on_failure` is the one for a test suite, and it is a `with` block:
it captures the screenshot, the accessible tree, the active window and
whatever had focus while the exception is still propagating, which is the
only moment the application is still up. `locate_image` raises `ImageNotFound`; `locate` returns `None` and
needs no display connection at all.

## Reading a list or a tree

| You want… | Use |
|---|---|
| The children one level down | [`element.children`](api.md#children) |
| The first match in a subtree, or `None` | [`element.child(role, name)`](api.md#child) |
| Every match in a subtree, by role and name | [`element.find(role, name)`](api.md#find) |
| Every match, with the full filter set | [`elements(within=element)`](api.md#elements) |
| Every row of a tree, including collapsed branches | [`tree_items(root)`](api.md#tree_items) |
| One row's cells as plain text | [`row_values(row)`](api.md#row_values) |

`elements(within=root)` only ever sees what the accessible tree already has
visible — a collapsed row's children are not there to find, the same way they
are not there on screen. `tree_items` expands every branch it meets on the
way down, so it is the one that answers "every row the user could reach".

## Asking what the desktop allows

| You want to… | Use |
|---|---|
| Declare what the suite cannot run without | [`require(...)`](api.md#require) |
| Branch on what this desktop can do | [`supports(...)`](api.md#supports) |
| Refuse a pyguitest older than the one a call needs | [`require_version(minimum)`](api.md#require_version) |
| Put a support table in a log or a bug report | [`report()`](api.md#report) |

`connect()` does not raise when nothing can drive the desktop — it falls back
to a `NullBackend` and refuses each call individually — so `require` at the
top of a run is what turns that into one early failure naming the missing
capability.

## Launching the application under test

| You want to… | Use |
|---|---|
| Start a GUI application and drive it | [`start_app(...)`](api.md#start_app) |
| Run a command that exits on its own | [`run_app(...)`](api.md#run_app) |

`start_app` hands back an `Application` to wait on and tear down. `run_app`
is `subprocess.run` underneath and **blocks until the program exits**, so
pointing it at a GUI application hangs the script until someone closes the
window.

## Where to go next

- [recipes.md](recipes.md) — the same ground task-first, with whole working
  snippets: waiting properly, filling a form, screenshots, clipboard, CI.
- [api.md](api.md) — the full signature, every parameter, and the capability
  each call needs.
- [troubleshooting.md](troubleshooting.md) — when the call you picked finds
  nothing, types nothing or captures nothing.
