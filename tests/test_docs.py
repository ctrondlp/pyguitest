"""Keep the user documentation in step with the code.

Every gap found so far has been the same shape: something true of the code that
was never written down, or written down once and left behind. Nothing checks
prose, so these do.
"""

import pathlib
import re
import unittest

from pyguitest import tools

ROOT = pathlib.Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()
PYPROJECT = (ROOT / "pyproject.toml").read_text()


def declared_extras():
    """Extra names from [project.optional-dependencies], without a TOML parser.

    tomllib is 3.11+, and this project supports 3.10.
    """
    block = PYPROJECT.split("[project.optional-dependencies]", 1)
    if len(block) == 1:
        return set()
    body = block[1].split("\n[", 1)[0]
    return set(re.findall(r"^(\w[\w-]*)\s*=\s*\[", body, re.MULTILINE))


class TestReadmeMatchesPackaging(unittest.TestCase):
    def test_the_parser_found_something(self):
        self.assertIn("atspi", declared_extras())

    def test_every_extra_is_documented(self):
        for extra in declared_extras():
            with self.subTest(extra=extra):
                self.assertIn(
                    extra, README, f"extra {extra!r} is not mentioned in README.md"
                )

    def test_the_no_dependencies_claim_matches_the_metadata(self):
        # If a hard dependency is ever added, the README must stop saying there
        # are none.
        declared = re.search(
            r"^dependencies\s*=\s*\[(.*?)\]", PYPROJECT, re.MULTILINE | re.DOTALL
        )
        self.assertIsNotNone(declared)
        empty = not declared.group(1).strip()
        if empty:
            self.assertIn("None are required", README)
        else:
            self.assertNotIn("None are required", README)

    def test_requires_python_is_stated(self):
        floor = re.search(r'requires-python\s*=\s*">=([\d.]+)"', PYPROJECT)
        self.assertIsNotNone(floor)
        self.assertIn(floor.group(1), README)


class TestReadmeMatchesToolRegistry(unittest.TestCase):
    def test_every_external_tool_is_listed(self):
        every = (
            tools.INPUT_TOOLS
            + tools.CAPTURE_TOOLS
            + tools.WINDOW_TOOLS
            + tools.IMAGE_TOOLS
        )
        self.assertGreater(len(every), 8)
        for tool in every:
            with self.subTest(tool=tool.name):
                self.assertIn(
                    tool.name,
                    README,
                    f"tool {tool.name!r} is discovered at runtime but is not "
                    "listed in README.md",
                )

    def test_constrained_tools_are_flagged_in_the_docs(self):
        # A reader must be able to see why a tool they installed was ignored.
        for tool in tools.INPUT_TOOLS + tools.CAPTURE_TOOLS:
            if tool.wlroots_only:
                with self.subTest(tool=tool.name):
                    self.assertRegex(README, rf"{tool.name}[^\n]*wlroots only")
            if tool.x11_only:
                with self.subTest(tool=tool.name):
                    self.assertRegex(README, rf"{tool.name}[^\n]*X11 only")


class TestExamplesAreListed(unittest.TestCase):
    def test_each_example_script_appears_in_its_readme(self):
        examples = ROOT / "examples"
        listing = (examples / "README.md").read_text()
        scripts = sorted(p.name for p in examples.glob("*.py"))
        self.assertGreater(len(scripts), 3)
        for script in scripts:
            with self.subTest(script=script):
                self.assertIn(script, listing)


if __name__ == "__main__":
    unittest.main()
