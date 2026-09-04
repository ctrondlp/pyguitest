# Two protocol gaps, written as issue text

Most of what pyguitest cannot do on Wayland, it cannot do for a good
reason: [the audit](wayland-audit.md) lists the capabilities Wayland
removed on purpose, and stubbing them would be dishonest. Two are
different. They are things Wayland could reasonably offer a consented
client, and the only reason they are missing is that nobody has asked
with a use case attached.

This file is the asking. Each section below is meant to be filed as-is —
the first against Mutter, the second against `wayland-protocols` — and
each states what we do today instead, so the cost of the gap is concrete
rather than theoretical.

Evidence for every claim about this machine is in
[validation.md](validation.md); the probes were re-run on 2026-09-04
against GNOME Shell / Mutter 51 on Fedora 45.

---

## 1. Mutter: implement `ext-image-copy-capture-v1`

**Summary.** Mutter implements neither `ext-image-copy-capture-v1` nor
`ext-image-capture-source-v1`. Both are merged in `wayland-protocols` and
shipped by wlroots, KWin and Cosmic. Together they cover output capture,
toplevel capture and pointer position, all under the compositor's own
consent — three things that currently need three different mechanisms on
GNOME, one of which does not exist.

**Evidence.** `strings` over `libmutter-51.so.0`, with `xdg_*` as a
positive control, finds exactly two `ext_*` interfaces:
`ext_background_effect_manager_v1` and `ext_background_effect_surface_v1`.
No image-copy-capture, no image-capture-source, no foreign-toplevel-list,
no data-control.

**What we do today.**

- *Screen capture* goes through `org.freedesktop.portal.Screenshot`. This
  works well and we are not asking to replace it.
- *Window capture* has no path at all, so pyguitest ships **its own GNOME
  Shell extension** and asks users to install it by hand, out of tree,
  ungated by any review. It calls `Meta.WindowActor.paint_to_content()`
  and `Shell.Screenshot.composite_to_stream()` — internal Shell API that
  broke once already between Shell 50 and 51, when
  `Meta.WindowActor.get_image` was removed. An extension is a poor place
  for this: it runs with the Shell's own privileges to do something a
  consented client should be able to ask for directly.
- *Pointer position* is served only by `X11Backend` through XWayland, so
  it works by accident on sessions that happen to run an X server and
  disappears on a pure Wayland one. `ext-image-copy-capture-v1`'s cursor
  session reports the pointer in the source's coordinate space, which
  would make this a real, consented capability instead.

**What we are asking for.** `ext-image-copy-capture-v1` with an output
source, gated however Mutter prefers — the same consent the Screenshot
portal already asks for would be fine. The toplevel source depends on
issue 2 below, so the output and cursor halves are useful on their own and
can land first.

---

## 2. `wayland-protocols`: a foreign-toplevel *geometry* protocol

**Summary.** No protocol tells a privileged client where a toplevel is on
screen. `ext-foreign-toplevel-list-v1` reports identity, title and app id,
and stops there; `zwlr-foreign-toplevel-management-v1` adds state and
activation but likewise no position. So a client that can see every window
still cannot say where any of them is.

**Why that one number matters.** Accessibility gives us the tree: an
assistive-technology client reads every widget of every application, with
each widget's extents. On Wayland those extents are *window-relative*,
because a client is never told its own position on screen. Add the
toplevel's origin and they become screen coordinates. Without it, a GUI
test can find the OK button and cannot click it — the two halves are one
addition apart.

**What we do today.** `AtspiBackend` refuses. On a Wayland session it
withdraws `WINDOW_GEOMETRY` from its own capability set rather than
returning coordinates that look plausible and are wrong
(`_screen_coords_trustworthy`, `src/pyguitest/backends/atspi.py`). Callers
get a typed refusal, which is honest and still means the feature is
unavailable. The alternatives all fail differently: XWayland reports
positions Mutter does not keep in sync (measured — see validation.md),
and the GNOME Shell extension route means every GUI test on GNOME depends
on an out-of-tree extension.

**What we are asking for.** A request on the foreign-toplevel handle that
reports the toplevel's position and size in the compositor's global
coordinate space, with a change event. Restricted exactly as
`ext-foreign-toplevel-list-v1` already is — this is for clients the
compositor has already decided may enumerate every window, and it tells
them nothing they could not learn by taking a screenshot they can also
already ask for.

**Why it has not been asked for.** Screen readers do not need it: they
speak to the user, not to the screen. Screen recorders do not need it:
they capture a source, not a location. GUI testing needs it and is barely
represented in this forum — which is, we think, the whole reason the gap
is still open.
