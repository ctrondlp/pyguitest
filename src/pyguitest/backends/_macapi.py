"""The macOS frameworks that need no binding: the TCC preflights.

Four non-prompting functions answer every permission question this package
asks -- `AXIsProcessTrusted`, `CGPreflightScreenCaptureAccess`,
`CGPreflightPostEventAccess` and `CGPreflightListenEventAccess` -- and all
four are plain C in frameworks that ship with the OS. They are declared here
in `ctypes` rather than reached through PyObjC, and that is not a preference.

`detect()` asks them, and `detect()` must not pull in a binding: PyObjC's
Quartz distribution imports Cocoa with it, so a probe routed through it costs
an AppKit load on every `Environment`, which is not the cheap, non-invasive
probe `session.py` promises. More decisive than the cost: the answer has to
be available *before* the extra is installed. "Accessibility is not granted"
and "pyobjc-framework-ApplicationServices is not installed" are two different
problems with two different fixes, and a probe that can only speak after `pip
install` cannot tell a reader which of the two they have.

The *prompting* half is the opposite case and is deliberately not here. It
belongs to a named backend's constructor, where the binding is already
required -- and `AXIsProcessTrustedWithOptions` cannot be called at all
without building a CFDictionary, which is a CoreFoundation exercise PyObjC
exists to save. See `macos.py` and `macquartz.py` for the four request forms.

Nothing runs at import time. The frameworks are opened on first use, through
accessors, because `import pyguitest` has to work on Linux and Windows, where
these paths do not exist -- and because a framework that will not load is a
condition to report rather than an ImportError to raise. Every question below
answers `False` where it cannot be asked, which is the conservative reading:
a caller who is told "not granted" goes and grants it.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import functools

__all__ = [
    "application_services",
    "core_graphics",
    "accessibility_trusted",
    "screen_recording_allowed",
    "post_event_allowed",
    "listen_event_allowed",
]

_FRAMEWORK_PATHS = {
    "ApplicationServices": (
        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
    ),
    "CoreGraphics": "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics",
}
"""Where each framework lives, as dyld names it.

Hand-written rather than looked up, because the two halves of `_open` below
answer different questions and this is the half that is a fact: CoreGraphics
and ApplicationServices have been at these paths since the frameworks were
introduced, and no `find_library` call is more authoritative about that than
the path itself.
"""


@functools.cache
def _open(name: str):
    """The framework called `name`, or None if it will not load.

    Two routes, because neither is reliable alone. The path is what the
    framework *is*, and dlopen resolves it out of the dyld shared cache on
    every macOS since Big Sur -- where the file itself is usually not on disk
    any more, which is exactly why `os.path.exists` is deliberately not
    consulted first: the honest test of "can this be loaded" is loading it.
    `find_library` is the fallback for a framework resolved from somewhere
    else, and answers None rather than raising for the paths it cannot search.

    Cached, so the second caller pays nothing and one unloadable framework is
    not retried on every probe.
    """
    candidates = []
    path = _FRAMEWORK_PATHS.get(name)
    if path:
        candidates.append(path)
    try:
        found = ctypes.util.find_library(name)
    except Exception:  # noqa: BLE001 - a lookup that failed found nothing
        found = None
    if found:
        candidates.append(found)
    for candidate in candidates:
        try:
            return ctypes.CDLL(candidate)
        except OSError:
            continue
    return None


def application_services():
    """The ApplicationServices framework, or None where it will not load.

    The home of the Accessibility API's C entry points. Loaded on its own
    rather than through `core_graphics`, because a machine can have one and
    not the other, and the two halves this package asks about -- AX and
    Quartz -- live one per framework.
    """
    return _open("ApplicationServices")


def core_graphics():
    """The CoreGraphics framework, or None where it will not load.

    Falls back to ApplicationServices, which re-exports CoreGraphics and
    which dlopen pulls the sub-framework in alongside. The fallback is for a
    loader that resolves the umbrella and not the leaf; where neither loads,
    `_preflight` answers False and the reason surfaces as a note rather than
    as a traceback out of `detect()`.
    """
    return _open("CoreGraphics") or _open("ApplicationServices")


def _function(library, name: str, restype, argtypes):
    """`library.name` with its signature declared, or None if it is absent.

    `restype` and `argtypes` are set before the call and never after: ctypes
    defaults an undeclared return to C `int` and guesses the arguments, which
    truncates a 64-bit handle silently -- and is how a prototype error comes
    to appear somewhere else entirely. Every prototype here is a claim about
    a signature, so it is made in one place, where a reader can check it
    against Apple's documentation in a single pass.
    """
    if library is None:
        return None
    try:
        function = getattr(library, name)
    except AttributeError:
        return None
    function.restype = restype
    function.argtypes = argtypes
    return function


def accessibility_trusted() -> bool:
    """Whether this process holds the Accessibility grant.

    `kTCCServiceAccessibility`, which is what the AX attribute reads and
    writes need -- and *not* what event posting needs, which is its own
    service and its own question below. Conflating the two would refuse input
    on a machine that had granted exactly what input requires, and would make
    a backend withdraw the wrong half of its capability set.

    Non-prompting: `AXIsProcessTrusted` reports the status as it stands and
    asks the user nothing. The prompting form is a backend's, never this
    one's -- on a Darwin session `detect()` runs before any caller has
    decided to want elements, and a probe that raised a dialog would be doing
    it on behalf of someone who never asked.
    """
    function = _function(
        application_services(), "AXIsProcessTrusted", ctypes.c_bool, []
    )
    return bool(function()) if function is not None else False


def _preflight(name: str) -> bool:
    """Call the named CoreGraphics preflight, or answer False without one.

    False and not None for the missing case, because a caller here has
    nothing to do with a third answer. macOS 10.15 is the floor these exist
    from, every machine this package can be installed on has them, and the
    only way to reach this branch is a CoreGraphics that would not load at
    all -- where "not granted" is the useful thing to say, since it is what
    sends a reader to System Settings, and the alternative is a
    permission-shaped question with no answer to it.
    """
    function = _function(core_graphics(), name, ctypes.c_bool, [])
    return bool(function()) if function is not None else False


def screen_recording_allowed() -> bool:
    """`kTCCServiceScreenCapture`, which window and screen pixels come from.

    Its own service, preflighted separately from Accessibility throughout: a
    capture backend honours this answer and nothing else, and the AX
    backend's reduced capability set keeps `SCREEN_CAPTURE` for exactly this
    reason -- a denied Accessibility grant says nothing about it.
    """
    return _preflight("CGPreflightScreenCaptureAccess")


def post_event_allowed() -> bool:
    """`kTCCServicePostEvent`, which input injection needs.

    The one grant `CGEventPost` is gated on. Apple's guidance is explicit
    that the Accessibility privilege is not what posting requires, and
    System Settings files both under one pane anyway, which is how the two
    come to be conflated by everyone who has not read the two preflight
    function names side by side.
    """
    return _preflight("CGPreflightPostEventAccess")


def listen_event_allowed() -> bool:
    """`kTCCServiceListenEvent`, which watching the user's input needs.

    Asked by nothing here yet, and reported anyway: the recorder's event tap
    is the phase that consumes it, and whether that tap is gated on this or
    on Accessibility is the one TCC question ADR 004 leaves open rather than
    answers from documentation. Reporting the answer costs one call and
    settles it on the first real machine that prints it.
    """
    return _preflight("CGPreflightListenEventAccess")
