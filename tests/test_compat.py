import inspect
import unittest

import pyguitest
from pyguitest import Screen, Session, Window
from pyguitest.backends.atspi import Element
from pyguitest.backends.base import GUIBackend
from pyguitest.backends.composite import _DISPATCH
from pyguitest.capabilities import Capability, Tier
from pyguitest.compat import LEGACY, by_tier, portable, unavailable

# The audit's distribution. If these change, docs/wayland-audit.md is stale.
AUDITED = {
    Tier.PORTABLE: 9,
    Tier.DIRECT: 4,
    Tier.COMPOSITOR: 19,
    Tier.PRIVILEGED: 8,
    Tier.REWORK: 4,
    Tier.NO_PATH: 6,
}


class TestMigrationTable(unittest.TestCase):
    def test_all_fifty_exports_accounted_for(self):
        self.assertEqual(len(LEGACY), 50)
        self.assertEqual(sum(AUDITED.values()), 50)

    def test_distribution_matches_audit(self):
        for tier, expected in AUDITED.items():
            with self.subTest(tier=tier.name):
                self.assertEqual(len(by_tier(tier)), expected)

    def test_names_unique_and_self_consistent(self):
        for name, fn in LEGACY.items():
            with self.subTest(name=name):
                self.assertEqual(name, fn.name)
                self.assertTrue(fn.x11)

    def test_capabilities_are_real(self):
        for fn in LEGACY.values():
            with self.subTest(name=fn.name):
                if fn.capability is not None:
                    self.assertIsInstance(fn.capability, Capability)

    def test_no_path_functions_have_no_replacement(self):
        # The audit's instruction: leave them out rather than stub them.
        for fn in unavailable():
            with self.subTest(name=fn.name):
                self.assertIsNone(fn.replacement)
        self.assertEqual(len(unavailable()), 6)

    def test_portable_functions_all_have_replacements(self):
        for fn in portable():
            with self.subTest(name=fn.name):
                self.assertIsNotNone(fn.replacement)

    def test_known_landmarks(self):
        self.assertEqual(LEGACY["GetMousePos"].tier, Tier.NO_PATH)
        self.assertEqual(LEGACY["IsWindowCursor"].tier, Tier.NO_PATH)
        self.assertEqual(LEGACY["SendKeys"].capability, Capability.KEY_EVENT)
        self.assertEqual(LEGACY["GetChildWindows"].capability, Capability.ELEMENT_TREE)
        self.assertEqual(LEGACY["GetWindowPos"].tier, Tier.COMPOSITOR)


# The one namespace a "Name.attr"-shaped replacement can point into.
_CLASSES = {"Session": Session, "Window": Window, "Screen": Screen, "Element": Element}

# Two SCREEN_INFO entries are worded as expressions over what `screens`
# returns ("len(screens)", "screens[0]") rather than as a name -- informative
# prose the table's own `note` already explains, not something to resolve.
_EXPRESSIONS = {"len(screens)", "screens[0]"}

_SESSION_PARAMS = set(inspect.signature(Session.__init__).parameters)


def _names_a_real_operation(replacement: str) -> bool:
    """Whether `replacement` resolves against the live package.

    A name is real if Session defines it directly (most tier-1/2/4
    operations do), if it is a Session.__init__ keyword (the
    "Session(x=)" constructor-argument form), if it is `Class.attr` for one
    of the small set of classes compat.py references, or if it is a
    backend operation reachable through Session's dynamic delegation --
    which is not visible to a plain hasattr(Session, ...) check, so it is
    checked against GUIBackend's own interface and the composite dispatch
    table instead, the two places that enumerate what is actually
    delegatable.
    """
    if replacement in _EXPRESSIONS:
        return True
    name = replacement.split("(", 1)[0]  # "minimize_window(...)" -> "minimize_window"
    if "." in name:
        owner, attr = name.split(".", 1)
        cls = _CLASSES.get(owner)
        if cls is None:
            return False
        # Session.event_delay/.key_delay are set in __init__, not declared
        # on the class, so they exist only on instances -- a constructor
        # parameter of the same name is the class-level evidence available.
        return hasattr(cls, attr) or (cls is Session and attr in _SESSION_PARAMS)
    return (
        hasattr(Session, name)
        or name in _SESSION_PARAMS
        or hasattr(GUIBackend, name)
        or name in _DISPATCH
        or hasattr(pyguitest, name)
    )


class TestReplacementsResolve(unittest.TestCase):
    """Every non-None replacement must name something that actually exists.

    Regression: the migration table pointed at ten names with no definition
    anywhere in the package (Window.geometry, .move, .resize, .activate,
    .minimize, .restore, .alive, .visible, .wait_closed, a bare `desktop`)
    plus two real names given fictitious keyword arguments
    (find_windows(pid=), wait_for_window(visible=True)) -- the one table a
    migrating user is told to trust, silently wrong.
    """

    def test_every_non_none_replacement_exists_in_the_live_package(self):
        for fn in LEGACY.values():
            if fn.replacement is None:
                continue
            with self.subTest(name=fn.name, replacement=fn.replacement):
                self.assertTrue(
                    _names_a_real_operation(fn.replacement),
                    f"{fn.name}'s replacement {fn.replacement!r} resolves to nothing",
                )


if __name__ == "__main__":
    unittest.main()
