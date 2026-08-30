#!/usr/bin/env python3
"""Isolate the GetImage BadMatch: raw python-xlib, no pyguitest involved."""

import os
import traceback

from Xlib import X, display

d = display.Display()
screen = d.screen()
root = screen.root

print(
    f"session   XDG_SESSION_TYPE={os.environ.get('XDG_SESSION_TYPE')!r} "
    f"WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY')!r} "
    f"DISPLAY={os.environ.get('DISPLAY')!r}"
)
print(
    f"setup     screen.width_in_pixels x height = "
    f"{screen.width_in_pixels}x{screen.height_in_pixels}"
)
print(
    f"setup     root_depth={screen.root_depth} "
    f"root_visual=0x{screen.root_visual:x} white=0x{screen.white_pixel:x}"
)
print(f"screens   screen_count={d.screen_count()}")

g = root.get_geometry()
print(
    f"live      root.get_geometry() = {g.width}x{g.height}+{g.x}+{g.y} depth={g.depth}"
)
print(f"live      root id = 0x{root.id:x} ({root.id})")

print(f"byteorder image_byte_order={d.display.info.image_byte_order} (0=LSBFirst)")
formats = [
    (f.depth, f.bits_per_pixel, f.scanline_pad) for f in d.display.info.pixmap_formats
]
print(f"formats   {formats}")

attrs = root.get_attributes()
print(f"root      map_state={attrs.map_state} (2=IsViewable)")


def attempt(label, drawable, x, y, w, h, fmt=X.ZPixmap, mask=0xFFFFFFFF):
    """Try one GetImage call and report whether it succeeded."""
    try:
        r = drawable.get_image(x, y, w, h, fmt, mask)
        data = r.data if isinstance(r.data, bytes) else bytes(r.data)
        stride = len(data) / h if h else 0
        print(
            f"OK   {label:<42} depth={r.depth} bytes={len(data)} (stride={stride:.2f})"
        )
        return True
    except Exception as exc:
        print(f"FAIL {label:<42} {type(exc).__name__}: {exc}")
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
    print(
        f"     id=0x{child.id:<9x} {cg.width:>5}x{cg.height:<5} depth={cg.depth:<3} "
        f"map_state={a.map_state} wm_class={cls}"
    )
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
        label = f"child 0x{child.id:x} {cls[0] if cls else '?'}"
        attempt(label + " 1x1", child, 0, 0, 1, 1)
        attempt(label + " full", child, 0, 0, cg.width, cg.height)

print("\n-- composite overlay / other roots --")
for i in range(d.screen_count()):
    s = d.screen(i)
    try:
        sg = s.root.get_geometry()
        ok = attempt(f"screen {i} root 1x1", s.root, 0, 0, 1, 1)
        print(f"     screen {i} root=0x{s.root.id:x} {sg.width}x{sg.height} ok={ok}")
    except Exception:
        traceback.print_exc()
