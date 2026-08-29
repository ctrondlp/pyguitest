#!/usr/bin/env python3
"""Isolate the GetImage BadMatch: raw python-xlib, no pyguitest involved."""

import os
import traceback

from Xlib import X, display

d = display.Display()
screen = d.screen()
root = screen.root

print("session   XDG_SESSION_TYPE=%r WAYLAND_DISPLAY=%r DISPLAY=%r"
      % (os.environ.get("XDG_SESSION_TYPE"), os.environ.get("WAYLAND_DISPLAY"),
         os.environ.get("DISPLAY")))
print("setup     screen.width_in_pixels x height = %sx%s"
      % (screen.width_in_pixels, screen.height_in_pixels))
print("setup     root_depth=%s root_visual=0x%x white=0x%x"
      % (screen.root_depth, screen.root_visual, screen.white_pixel))
print("screens   screen_count=%s" % d.screen_count())

g = root.get_geometry()
print("live      root.get_geometry() = %sx%s+%s+%s depth=%s"
      % (g.width, g.height, g.x, g.y, g.depth))
print("live      root id = 0x%x (%d)" % (root.id, root.id))

print("byteorder image_byte_order=%s (0=LSBFirst)" % d.display.info.image_byte_order)
print("formats   %s" % [(f.depth, f.bits_per_pixel, f.scanline_pad)
                        for f in d.display.info.pixmap_formats])

attrs = root.get_attributes()
print("root      map_state=%s (2=IsViewable)" % attrs.map_state)


def attempt(label, drawable, x, y, w, h, fmt=X.ZPixmap, mask=0xFFFFFFFF):
    try:
        r = drawable.get_image(x, y, w, h, fmt, mask)
        data = r.data if isinstance(r.data, bytes) else bytes(r.data)
        print("OK   %-42s depth=%s bytes=%d (stride=%.2f)"
              % (label, r.depth, len(data), len(data) / h if h else 0))
        return True
    except Exception as exc:
        print("FAIL %-42s %s: %s" % (label, type(exc).__name__, exc))
        return False


print("\n-- root --")
attempt("root 1x1 at 0,0", root, 0, 0, 1, 1)
attempt("root 64 rows full width", root, 0, 0, g.width, 64)
attempt("root full screen", root, 0, 0, g.width, g.height)
attempt("root 1x1, plane_mask=0x00ffffff", root, 0, 0, 1, 1, mask=0x00FFFFFF)
attempt("root 1x1, XYPixmap", root, 0, 0, 1, 1, fmt=X.XYPixmap)

print("\n-- child windows --")
# depth 0 means InputOnly: a window with no content at all, for which
# GetImage is BadMatch by specification. The first version of this script
# sampled one and drew no conclusion from it, which is worse than useless
# -- it looked like evidence. Only InputOutput windows (depth > 0) can be
# captured, so they are what gets tried.
candidates = []
for child in root.query_tree().children:
    try:
        a = child.get_attributes()
        cg = child.get_geometry()
    except Exception:
        continue
    try:
        cls = child.get_wm_class()
    except Exception:
        cls = None
    print("     id=0x%-9x %5sx%-5s depth=%-3s map_state=%s wm_class=%s"
          % (child.id, cg.width, cg.height, cg.depth, a.map_state, cls))
    # No size floor. A 1x1 InputOutput window is a perfectly valid
    # GetImage target, and on a pure Wayland session it may be the only
    # one in existence -- an earlier `width > 8` filter excluded the
    # single testable window and left the question open.
    if cg.depth > 0 and a.map_state == X.IsViewable:
        candidates.append((child, cg, cls))

if not candidates:
    print("     NO InputOutput (depth > 0) viewable children.")
    print("     On a pure Wayland session that is expected: every window is a")
    print("     native Wayland surface, so there are no X11 client windows to")
    print("     capture. Run an X11 client to get a real target, e.g.")
    print("         GDK_BACKEND=x11 gnome-text-editor &")
    print("     or  xterm &")
else:
    for child, cg, cls in candidates[:5]:
        label = "child 0x%x %s" % (child.id, cls[0] if cls else "?")
        attempt(label + " 1x1", child, 0, 0, 1, 1)
        attempt(label + " full", child, 0, 0, cg.width, cg.height)

print("\n-- composite overlay / other roots --")
for i in range(d.screen_count()):
    s = d.screen(i)
    try:
        sg = s.root.get_geometry()
        ok = attempt("screen %d root 1x1" % i, s.root, 0, 0, 1, 1)
        print("     screen %d root=0x%x %sx%s ok=%s" % (i, s.root.id, sg.width, sg.height, ok))
    except Exception:
        traceback.print_exc()
