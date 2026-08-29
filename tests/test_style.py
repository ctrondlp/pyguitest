"""House style that no linter enforces.

Ruff covers PEP 8 and PEP 257, but not the two comment conventions this project
settled on. They were inconsistent until someone read the source and noticed, so
they are pinned here rather than left to memory.
"""

import pathlib
import re
import unittest

SOURCE = sorted(pathlib.Path("src/pyguitest").rglob("*.py"))
DIVIDER = re.compile(r"^\s*# -- .+? -+\s*$")
DIVIDER_WIDTH = 76


class TestCommentConventions(unittest.TestCase):
    def test_source_files_were_found(self):
        # Guard against the glob silently matching nothing.
        self.assertGreater(len(SOURCE), 10)

    def test_no_sphinx_attribute_comments(self):
        """Reject the Sphinx attribute-comment form.

        `#:` means something only to Sphinx, which this project does not
        run. Attribute docstrings do the same job as real objects.
        """
        for path in SOURCE:
            for number, line in enumerate(path.read_text().splitlines(), 1):
                with self.subTest(file=path.name, line=number):
                    self.assertFalse(
                        line.lstrip().startswith("#:"),
                        f"{path}:{number} uses '#:'; use an attribute docstring",
                    )

    def test_section_dividers_are_one_width(self):
        """Dividers pad to a fixed column so they line up down the file."""
        for path in SOURCE:
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if DIVIDER.match(line):
                    with self.subTest(file=path.name, line=number):
                        self.assertEqual(
                            len(line),
                            DIVIDER_WIDTH,
                            f"{path}:{number} divider is {len(line)} columns, "
                            f"expected {DIVIDER_WIDTH}",
                        )

    def test_dividers_are_actually_present(self):
        found = sum(
            1
            for path in SOURCE
            for line in path.read_text().splitlines()
            if DIVIDER.match(line)
        )
        self.assertGreater(found, 20)


if __name__ == "__main__":
    unittest.main()
