"""docs/api.md is generated; this keeps it from going stale.

The same guard TestMigrationTable puts on the audit's distribution. A
capability added to a backend, or a method added to Session, changes the
reference -- and nothing else would notice that the committed file no
longer matches the code it describes.
"""

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "gen-api-docs.py"
DOC = ROOT / "docs" / "api.md"


def load_generator():
    """Import the generator, whose filename is not a valid module name."""
    spec = importlib.util.spec_from_file_location("gen_api_docs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestApiDocs(unittest.TestCase):
    def test_committed_file_matches_the_source(self):
        generated = load_generator().render()
        self.assertEqual(
            generated,
            DOC.read_text(),
            "docs/api.md is out of date -- run python3 scripts/gen-api-docs.py",
        )

    def test_every_dispatched_operation_is_documented(self):
        """Nothing routed by the composite is missing from the reference.

        The mirror of the composite-dispatch invariant: a capability whose
        method is reachable but undocumented is as good as absent.
        """
        from pyguitest.backends.composite import _DISPATCH

        text = DOC.read_text()
        for attr in _DISPATCH:
            with self.subTest(attr=attr):
                self.assertIn(f"`{attr}(", text)


if __name__ == "__main__":
    unittest.main()
