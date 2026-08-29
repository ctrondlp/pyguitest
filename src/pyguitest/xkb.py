"""Resolve characters to keycodes through a compositor-supplied XKB keymap.

This is what makes libei typing keymap-*safe*, and the reason `eiinput.py`
can offer TEXT_ENTRY at all. Every other injection path in this package
either sends abstract keysyms and lets the compositor resolve them
(`portal.py`) or sends raw scancodes from a hardcoded US-layout table and
hopes (`uinput.py`, ydotool) -- the latter types the wrong characters on a
non-US layout, silently.

libei takes neither. `ei_device_keyboard_key()` wants a keycode, and the
compositor applies *the keymap it handed the client* to interpret it. So
the mapping is not a guess: read that keymap, and which keycode produces
which character is a fact you can look up. That is all this module does.

A ctypes binding rather than a dependency: the whole surface needed is a
dozen libxkbcommon calls, and every desktop this package targets already
has libxkbcommon.so.0 loaded -- adding a build-time Python dependency
(python-xkbcommon) to reach functions already resident in the process is a
poor trade. Importing is always safe where the library is absent;
`available()` reports it, matching the pattern in `portal.py`/`uinput.py`.

Keycode bases differ by one constant and it bites every time: XKB keycodes
are evdev keycodes plus 8. libei speaks evdev, xkbcommon speaks XKB, so
every value crossing this boundary is converted exactly once, here.
"""

from __future__ import annotations

import ctypes

__all__ = ["Keymap", "available"]

_XKB_CONTEXT_NO_FLAGS = 0
_XKB_KEYMAP_FORMAT_TEXT_V1 = 1
_XKB_KEYMAP_COMPILE_NO_FLAGS = 0

EVDEV_OFFSET = 8
"""XKB keycode minus evdev keycode. Fixed by the X11 protocol's history."""

_MODIFIER_KEYSYMS = {
    "Shift": ("Shift_L", "Shift_R"),
    "Control": ("Control_L", "Control_R"),
    "Mod1": ("Alt_L", "Alt_R", "Meta_L"),
    "Mod2": ("Num_Lock",),
    "Mod3": ("ISO_Level3_Shift", "Alt_R"),
    "Mod4": ("Super_L", "Super_R"),
    "Mod5": ("ISO_Level3_Shift", "Alt_R"),
    "Lock": ("Caps_Lock",),
}
"""XKB modifier name -> keysyms that can produce it, best first. A keymap
names its modifiers abstractly ("Mod5"), but a key has to be *pressed* to
engage one, so each is resolved back to a real key on this keymap."""

_CONVENTIONAL_MODIFIERS = frozenset(
    {
        42,  # KEY_LEFTSHIFT
        54,  # KEY_RIGHTSHIFT
        29,  # KEY_LEFTCTRL
        97,  # KEY_RIGHTCTRL
        56,  # KEY_LEFTALT
        100,  # KEY_RIGHTALT -- AltGr, and so ISO_Level3_Shift in practice
        125,  # KEY_LEFTMETA
        126,  # KEY_RIGHTMETA
        58,  # KEY_CAPSLOCK
        69,  # KEY_NUMLOCK
    }
)
"""Evdev codes for the keys a person would actually press for a modifier.

A keymap often binds one modifier keysym to several keycodes -- a French
layout reaches ISO_Level3_Shift through both RIGHTALT and a synthetic
<LVL3> key -- and the lowest-numbered match can be one no physical keyboard
has. Both work, since the compositor resolves whatever it is sent through
this same keymap, but pressing an obscure code is a poor bet against
compositors that sanity-check input. These are only ever used after the
keymap itself confirms they produce the modifier."""


def _library():
    """Load libxkbcommon, or return None where it is not present."""
    try:
        return ctypes.CDLL("libxkbcommon.so.0")
    except OSError:
        return None


def available() -> bool:
    """Whether libxkbcommon can be loaded here."""
    return _library() is not None


def _bind(lib):
    """Declare the argument and return types ctypes cannot infer.

    Omitting these is the classic ctypes bug: pointers get truncated to
    32 bits on a 64-bit build and the failure looks like a corrupt keymap
    rather than a missing declaration.
    """
    p = ctypes.c_void_p
    u32 = ctypes.c_uint32
    lib.xkb_context_new.argtypes = [ctypes.c_int]
    lib.xkb_context_new.restype = p
    lib.xkb_context_unref.argtypes = [p]
    lib.xkb_context_unref.restype = None
    lib.xkb_keymap_new_from_string.argtypes = [
        p,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_int,
    ]
    lib.xkb_keymap_new_from_string.restype = p
    lib.xkb_keymap_unref.argtypes = [p]
    lib.xkb_keymap_unref.restype = None
    lib.xkb_keymap_min_keycode.argtypes = [p]
    lib.xkb_keymap_min_keycode.restype = u32
    lib.xkb_keymap_max_keycode.argtypes = [p]
    lib.xkb_keymap_max_keycode.restype = u32
    lib.xkb_keymap_num_layouts_for_key.argtypes = [p, u32]
    lib.xkb_keymap_num_layouts_for_key.restype = u32
    lib.xkb_keymap_num_levels_for_key.argtypes = [p, u32, u32]
    lib.xkb_keymap_num_levels_for_key.restype = u32
    lib.xkb_keymap_key_get_syms_by_level.argtypes = [
        p,
        u32,
        u32,
        u32,
        ctypes.POINTER(ctypes.POINTER(u32)),
    ]
    lib.xkb_keymap_key_get_syms_by_level.restype = ctypes.c_int
    lib.xkb_keymap_key_get_mods_for_level.argtypes = [
        p,
        u32,
        u32,
        u32,
        ctypes.POINTER(u32),
        ctypes.c_size_t,
    ]
    lib.xkb_keymap_key_get_mods_for_level.restype = ctypes.c_size_t
    lib.xkb_keymap_num_mods.argtypes = [p]
    lib.xkb_keymap_num_mods.restype = u32
    lib.xkb_keymap_mod_get_name.argtypes = [p, u32]
    lib.xkb_keymap_mod_get_name.restype = ctypes.c_char_p
    lib.xkb_utf32_to_keysym.argtypes = [u32]
    lib.xkb_utf32_to_keysym.restype = u32
    lib.xkb_keysym_from_name.argtypes = [ctypes.c_char_p, ctypes.c_int]
    lib.xkb_keysym_from_name.restype = u32
    return lib


class Keymap:
    """A compiled XKB keymap, queried for how to type a character."""

    def __init__(self, text: str) -> None:
        """Compile `text`, the keymap string a compositor handed over."""
        lib = _library()
        if lib is None:
            raise RuntimeError("libxkbcommon.so.0 is not available")
        self._lib = _bind(lib)
        self._context = self._lib.xkb_context_new(_XKB_CONTEXT_NO_FLAGS)
        if not self._context:
            raise RuntimeError("xkb_context_new() failed")
        self._keymap = self._lib.xkb_keymap_new_from_string(
            self._context,
            text.encode("utf-8"),
            _XKB_KEYMAP_FORMAT_TEXT_V1,
            _XKB_KEYMAP_COMPILE_NO_FLAGS,
        )
        if not self._keymap:
            self._lib.xkb_context_unref(self._context)
            self._context = None
            raise RuntimeError("the keymap did not compile")
        self._by_keysym: dict[int, tuple[int, tuple[int, ...]]] = {}
        self._keycodes_by_keysym: dict[int, list[int]] = {}
        self._build_index()

    def close(self) -> None:
        """Release the keymap and context."""
        if getattr(self, "_keymap", None):
            self._lib.xkb_keymap_unref(self._keymap)
            self._keymap = None
        if getattr(self, "_context", None):
            self._lib.xkb_context_unref(self._context)
            self._context = None

    def __del__(self):
        self.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- index building ----------------------------------------------------

    def _mod_names(self):
        """Modifier index -> name, as this keymap names them."""
        names = {}
        for index in range(self._lib.xkb_keymap_num_mods(self._keymap)):
            raw = self._lib.xkb_keymap_mod_get_name(self._keymap, index)
            if raw:
                names[index] = raw.decode("utf-8")
        return names

    def _syms_at(self, keycode, layout, level):
        """The keysyms a key produces at one layout/level."""
        syms = ctypes.POINTER(ctypes.c_uint32)()
        count = self._lib.xkb_keymap_key_get_syms_by_level(
            self._keymap, keycode, layout, level, ctypes.byref(syms)
        )
        return [syms[i] for i in range(count)]

    def _mask_at(self, keycode, layout, level):
        """The simplest modifier mask that selects one level, or 0."""
        masks = (ctypes.c_uint32 * 8)()
        count = self._lib.xkb_keymap_key_get_mods_for_level(
            self._keymap, keycode, layout, level, masks, 8
        )
        if not count:
            return 0
        # Fewest modifiers held is the least surprising way to reach a
        # level, and avoids picking e.g. a Caps_Lock-based route to an
        # uppercase letter when plain Shift also works.
        return min(masks[i] for i in range(count))

    def _build_index(self):
        """Map every reachable keysym to a key and the modifiers it needs.

        Only layout 0 is indexed. A multi-layout keymap (a user with two
        languages configured) would need a layout switch to reach the
        others, which is a stateful operation this does not attempt --
        typing is resolved against the layout that is active.
        """
        mod_names = self._mod_names()
        first = self._lib.xkb_keymap_min_keycode(self._keymap)
        last = self._lib.xkb_keymap_max_keycode(self._keymap)
        for keycode in range(first, last + 1):
            if not self._lib.xkb_keymap_num_layouts_for_key(self._keymap, keycode):
                continue
            levels = self._lib.xkb_keymap_num_levels_for_key(self._keymap, keycode, 0)
            for level in range(levels):
                mask = self._mask_at(keycode, 0, level)
                needed = tuple(
                    mod_names[i]
                    for i in range(len(mod_names))
                    if mask & (1 << i) and i in mod_names
                )
                for keysym in self._syms_at(keycode, 0, level):
                    # Lowest level wins: an earlier, simpler way to produce
                    # the same keysym is always preferable.
                    self._by_keysym.setdefault(keysym, (keycode, needed))
                    self._keycodes_by_keysym.setdefault(keysym, []).append(keycode)

    # -- lookups -----------------------------------------------------------

    def keysym_for_char(self, char: str) -> int:
        """The keysym for a single character, or 0 if it has none."""
        return self._lib.xkb_utf32_to_keysym(ord(char))

    def keysym_for_name(self, name: str) -> int:
        """The keysym named `name` (e.g. 'Return'), or 0 if unknown."""
        return self._lib.xkb_keysym_from_name(name.encode("utf-8"), 0)

    def _resolve(self, keysym):
        entry = self._by_keysym.get(keysym)
        if entry is None:
            return None
        xkb_keycode, modifier_names = entry
        modifiers = []
        for name in modifier_names:
            keycode = self._modifier_keycode(name)
            if keycode is None:
                return None  # cannot press what this keymap cannot name
            modifiers.append(keycode)
        return (xkb_keycode - EVDEV_OFFSET, tuple(modifiers))

    def _modifier_keycode(self, name):
        """An evdev keycode that engages the named modifier, or None.

        Prefers a key a real keyboard has (see _CONVENTIONAL_MODIFIERS)
        among the several a keymap may offer for one modifier.
        """
        for keysym_name in _MODIFIER_KEYSYMS.get(name, ()):
            keysym = self.keysym_for_name(keysym_name)
            candidates = [
                code - EVDEV_OFFSET for code in self._keycodes_by_keysym.get(keysym, ())
            ]
            if not candidates:
                continue
            conventional = [c for c in candidates if c in _CONVENTIONAL_MODIFIERS]
            return conventional[0] if conventional else candidates[0]
        return None

    def for_char(self, char: str):
        """(evdev keycode, modifier evdev keycodes) to type `char`, or None.

        None means this keymap cannot produce the character at all -- on a
        US layout that is true of most accented letters, and the caller
        should say so rather than press something arbitrary.
        """
        keysym = self.keysym_for_char(char)
        return self._resolve(keysym) if keysym else None

    def for_name(self, name: str):
        """Same, for a named key such as 'Return' or 'F5'."""
        keysym = self.keysym_for_name(name)
        return self._resolve(keysym) if keysym else None
