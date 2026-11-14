"""docs/api.md is generated; this keeps it from going stale.

The same guard TestMigrationTable puts on the audit's distribution. A
capability added to a backend, or a method added to Session, changes the
reference -- and nothing else would notice that the committed file no
longer matches the code it describes.
"""

import importlib.util
import inspect
import re
import unittest
from pathlib import Path

import pyguitest

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
            DOC.read_text(encoding="utf-8"),
            "docs/api.md is out of date -- run python3 scripts/gen-api-docs.py",
        )

    def test_every_dispatched_operation_is_documented(self):
        """Nothing routed by the composite is missing from the reference.

        The mirror of the composite-dispatch invariant: a capability whose
        method is reachable but undocumented is as good as absent.
        """
        from pyguitest.backends.composite import _DISPATCH

        text = DOC.read_text(encoding="utf-8")
        for attr in _DISPATCH:
            with self.subTest(attr=attr):
                self.assertIn(f"`{attr}(", text)

    def test_the_extension_readme_quotes_the_current_version_name(self):
        """The build-identity line must not go stale -- again.

        gnome-shell keeps running the copy it loaded at login, so
        `version-name` is the one thing that tells an installed build from
        a loaded one, and the extension README quotes it as the diagnostic.
        The quote has gone stale once before (the CHANGELOG records the
        0.5.0-safewrite fix); this pins it to metadata.json so the next
        bump cannot ship without the README following.
        """
        import json

        root = DOC.parent.parent
        extension = root / "gnome-shell-extension"
        uuid = "pyguitest-window-control@pyguitest.local"
        metadata_path = extension / uuid / "metadata.json"
        if not metadata_path.exists():
            # The extension ships from the repository, not the sdist; the
            # build gate re-runs this suite from an unpacked sdist where
            # there is nothing to pin.
            self.skipTest("extension sources are not in this distribution")
        metadata = json.loads(metadata_path.read_text())
        readme = (extension / "README.md").read_text(encoding="utf-8")
        self.assertIn(metadata["version-name"], readme)


class TestReferenceQuality(unittest.TestCase):
    """What the byte-comparison above cannot notice.

    `test_committed_file_matches_the_source` only proves api.md equals
    `render()`; a blank description cell, a member dropped from every
    table, or a call filed under a catch-all heading all round-trip
    cleanly. Each of those was a real defect in this file: thirty-six rows
    carried an empty description, eight `Environment` fields were missing
    altogether, and twenty-seven of the hundred `Session` members sat in an
    "Other" bucket.
    """

    TEXT = DOC.read_text(encoding="utf-8")
    _EMPTY_CELL = re.compile(r"^\| `[^|]+` \|\s*\|$", re.MULTILINE)

    def test_no_table_row_has_an_empty_description(self):
        empty = self._EMPTY_CELL.findall(self.TEXT)
        self.assertEqual(
            empty,
            [],
            "these rows name something and then say nothing about it; give "
            "the field a PEP 257 attribute docstring in its class body: "
            + ", ".join(empty[:8]),
        )

    def test_no_catch_all_section(self):
        """Every member belongs under a heading that says what it is for."""
        self.assertNotIn(
            "## Other",
            self.TEXT,
            'api.md has an "Other" section again -- add the new calls to a '
            "GROUPS entry in scripts/gen-api-docs.py, or add a group",
        )

    def test_every_public_session_member_is_grouped(self):
        generator = load_generator()
        grouped = [name for _t, _b, names in generator.GROUPS for name in names]
        self.assertEqual(
            sorted(grouped),
            sorted(set(grouped)),
            "a name appears in two GROUPS entries",
        )
        public = {
            name
            for name, _ in inspect.getmembers(pyguitest.Session)
            if not name.startswith("_")
        }
        self.assertEqual(
            public - set(grouped),
            set(),
            "public Session members in no GROUPS entry, so they would fall "
            "into a catch-all section",
        )
        self.assertEqual(
            set(grouped) - public,
            set(),
            "GROUPS names something that is not a public Session member",
        )


if __name__ == "__main__":
    unittest.main()
