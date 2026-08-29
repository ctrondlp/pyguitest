"""Keep the user documentation in step with the code.

Every gap found so far has been the same shape: something true of the code that
was never written down, or written down once and left behind. Nothing checks
prose, so these do.
"""

import pathlib
import re
import unittest

from pyguitest import hints, tools

ROOT = pathlib.Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()
INSTALL = (ROOT / "docs" / "install.md").read_text()
INPUT = (ROOT / "docs" / "input.md").read_text()
PYPROJECT = (ROOT / "pyproject.toml").read_text()

# The README is the overview; the detail it would otherwise drown in lives in
# docs/. A reader following one link from the front page still counts as
# having been told, so "is this documented" is asked of the set, not of the
# README alone. Claims the README itself must carry are checked against
# README directly.
DOCS = "\n".join((README, INSTALL, INPUT))


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
                self.assertIn(extra, DOCS, f"extra {extra!r} is documented nowhere")

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
                    DOCS,
                    f"tool {tool.name!r} is discovered at runtime but is "
                    "listed in no document",
                )

    def test_constrained_tools_are_flagged_in_the_docs(self):
        # A reader must be able to see why a tool they installed was ignored.
        for tool in tools.INPUT_TOOLS + tools.CAPTURE_TOOLS:
            if tool.wlroots_only:
                with self.subTest(tool=tool.name):
                    self.assertRegex(INSTALL, rf"{tool.name}[^\n]*wlroots only")
            if tool.x11_only:
                with self.subTest(tool=tool.name):
                    self.assertRegex(INSTALL, rf"{tool.name}[^\n]*X11 only")


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


class TestDistributionTableMatchesTheCode(unittest.TestCase):
    """The package table in docs/install.md must agree with hints.py.

    A document cannot know whose machine it is on, so the honest answer to
    "what do I install" is `pyguitest doctor`, which reads /etc/os-release
    and fills in the right names. The table exists for people who would
    rather look it up -- and a hand-maintained copy of data the code
    already holds is exactly the kind of thing that silently goes stale,
    which is what these docs did by being written entirely in `dnf` while
    hints.py had known four families all along.
    """

    def table_rows(self):
        """The generated table, as {row label: [cells]}.

        Reads only the contiguous run of table lines after the marker. An
        earlier version kept going and swallowed the external-tools table
        further down, which shares a row label -- a parser that reads too
        much fails in a way that looks like the thing it checks is wrong.
        """
        after = INSTALL.split("generated from pyguitest.hints._PACKAGES", 1)[1]
        rows = {}
        started = False
        for line in after.splitlines():
            stripped = line.strip()
            if not stripped.startswith("|"):
                if started:
                    break
                continue
            started = True
            if set(stripped) <= set("|- "):
                continue
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            rows[cells[0].strip("*").strip()] = cells[1:]
        return rows

    def test_the_parser_found_the_table(self):
        rows = self.table_rows()
        self.assertIn("install with", rows)
        self.assertIn("Elements (AT-SPI)", rows)

    def test_every_family_hints_knows_is_in_the_table(self):
        # Adding a distribution to hints.py without the table is how the
        # two drift apart.
        header = self.table_rows()["install with"]
        self.assertEqual(len(header), len(hints._PACKAGES))

    def test_install_commands_match(self):
        commands = {c.strip("`") for c in self.table_rows()["install with"]}
        self.assertEqual(commands, {p["install"] for p in hints._PACKAGES.values()})

    def test_package_names_match_for_every_component(self):
        rows = self.table_rows()
        for label, key in (
            ("Elements (AT-SPI)", "atspi"),
            ("Screenshots", "capture"),
            ("Input injection", "input"),
            ("Image search", "imagemagick"),
        ):
            with self.subTest(component=key):
                self.assertEqual(
                    {c.strip("`") for c in rows[label]},
                    {p[key] for p in hints._PACKAGES.values()},
                )

    def test_the_docs_do_not_hardcode_one_distribution(self):
        # Six `dnf` commands and no apt/pacman/zypper anywhere was the
        # original problem: correct for the author, wrong for most readers.
        outside = INSTALL.split("generated from pyguitest.hints._PACKAGES", 1)
        prose = README + outside[0] + outside[1].split("\n\n", 2)[-1]
        managers = [
            m for m in ("dnf install", "apt install", "pacman -S") if m in prose
        ]
        self.assertLessEqual(
            len(managers),
            1,
            f"prose outside the table names {managers}; prefer the table or "
            "`pyguitest doctor`, which knows the reader's distribution",
        )
