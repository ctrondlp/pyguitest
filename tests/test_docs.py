"""Keep the user documentation in step with the code.

Every gap found so far has been the same shape: something true of the code that
was never written down, or written down once and left behind. Nothing checks
prose, so these do.
"""

import pathlib
import re
import subprocess
import unittest

from pyguitest import hints, tools

ROOT = pathlib.Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
INSTALL = (ROOT / "docs" / "install.md").read_text(encoding="utf-8")
INPUT = (ROOT / "docs" / "input.md").read_text(encoding="utf-8")
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

# The README is the overview; the detail it would otherwise drown in lives in
# docs/. A reader following one link from the front page still counts as
# having been told, so "is this documented" is asked of the set, not of the
# README alone. Claims the README itself must carry are checked against
# README directly.
DOCS = "\n".join((README, INSTALL, INPUT))

_INLINE_LINK = re.compile(r"\]\(([^)\s]+)\)")
_CONTENTS_ENTRY = re.compile(r"^- \[[^\]]+\]\(#([^)]+)\)$", re.MULTILINE)
_HEADING = re.compile(r"^#{1,6} (.+)$", re.MULTILINE)
# An explicit anchor, `<a id="...">`, which is the other way a page declares a
# link target. api.md puts one on each row of its capability table so the ~95
# `Needs` cells can link to the row defining the capability they name -- a
# heading per capability would add 32 entries to the page outline to say what
# one table row already says. These resolve exactly as a heading anchor does,
# so a link to one is not a broken link and the checks below accept both.
_EXPLICIT_ANCHOR = re.compile(r"""<a\s+(?:id|name)=["']([^"']+)["']""")

# The canonical URL for a file in this repository. The README writes its
# cross-references this way because the same file is also the PyPI long
# description, where a relative link 404s rather than resolving -- so the
# links have to be checked as URLs instead.
_REPO_URL = "https://github.com/ctrondlp/pyguitest/"
_ENV_VAR = re.compile(r"\bPYGUITEST_[A-Z0-9_]+\b")


def heading_slug(heading):
    """The anchor a heading gets, insensitive to how a slugger trims.

    Runs of dashes are collapsed and both ends stripped, so a heading naming a
    `--flag` matches a contents entry written with one leading dash as well as
    the two a strict slugger would produce. What is asked here is "is there a
    section for this", not "is the anchor byte-exact".
    """
    text = re.sub(r"[^a-z0-9 \-_]", "", heading.strip().lower())
    return re.sub(r"-+", "-", text.replace(" ", "-")).strip("-")


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
            if tool.compositors:
                with self.subTest(tool=tool.name):
                    self.assertRegex(INSTALL, rf"{tool.name}[^\n]*\*[^\n]*wlroots")
            if tool.x11_only:
                with self.subTest(tool=tool.name):
                    self.assertRegex(INSTALL, rf"{tool.name}[^\n]*X11 only")


class TestExamplesAreListed(unittest.TestCase):
    def test_each_example_script_appears_in_its_readme(self):
        examples = ROOT / "examples"
        listing = (examples / "README.md").read_text(encoding="utf-8")
        scripts = sorted(p.name for p in examples.glob("*.py"))
        self.assertGreater(len(scripts), 3)
        for script in scripts:
            with self.subTest(script=script):
                self.assertIn(script, listing)


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


class TestLinksAndAnchorsResolve(unittest.TestCase):
    """Every cross-reference in the user documentation still lands.

    Both halves have been wrong in this tree already, and neither is visible
    to a reader until they are already lost: the README and docs/README.md
    keep two lists of the same pages, and a heading can be renamed without
    the contents entry pointing at it moving. The anchor comparison is
    deliberately forgiving about leading dashes -- a heading naming a
    `--flag` slugs with two, and a contents entry written with one is still
    pointing at the same section.
    """

    def pages(self):
        """The README plus every page under docs/, which is the user set."""
        return [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]

    def test_there_are_pages_to_check(self):
        self.assertGreater(len(self.pages()), 5)

    def test_relative_links_point_at_something_that_exists(self):
        for page in self.pages():
            for target in _INLINE_LINK.findall(page.read_text(encoding="utf-8")):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                path = target.split("#", 1)[0]
                if not path:
                    continue
                with self.subTest(page=page.name, target=target):
                    self.assertTrue(
                        (page.parent / path).exists(),
                        f"{page.name} links to {path}, which does not exist",
                    )

    def test_a_fragment_on_a_cross_page_link_resolves_too(self):
        """The half the test above drops on the floor: `other.md#section`.

        It splits the fragment off to find the file and then discards it, and
        `test_every_in_page_anchor_resolves` only looks at links whose whole
        target is an anchor -- so a heading renamed on page B broke every
        link into it from page A while both tests stayed green. Same failure
        as a broken in-page anchor, and just as invisible: the page renders,
        the click scrolls nowhere.
        """
        seen = 0
        for page in self.pages():
            for target in _INLINE_LINK.findall(page.read_text(encoding="utf-8")):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                path, _, fragment = target.partition("#")
                if not path or not fragment:
                    continue
                destination = page.parent / path
                if destination.suffix != ".md" or not destination.is_file():
                    continue
                text = destination.read_text(encoding="utf-8")
                headings = {heading_slug(h) for h in _HEADING.findall(text)}
                headings |= set(_EXPLICIT_ANCHOR.findall(text))
                seen += 1
                with self.subTest(page=page.name, target=target):
                    self.assertIn(
                        heading_slug(fragment),
                        headings,
                        f"{page.name} links to {target} with no such heading on {path}",
                    )
        # Guard against the filtering above silently matching nothing.
        self.assertGreater(seen, 5)

    def test_each_pages_own_contents_resolves(self):
        for page in self.pages():
            text = page.read_text(encoding="utf-8")
            headings = {heading_slug(h) for h in _HEADING.findall(text)}
            headings |= set(_EXPLICIT_ANCHOR.findall(text))
            for anchor in _CONTENTS_ENTRY.findall(text):
                with self.subTest(page=page.name, anchor=anchor):
                    self.assertIn(
                        heading_slug(anchor),
                        headings,
                        f"{page.name} lists #{anchor} in its contents with no "
                        "such heading",
                    )

    def test_every_in_page_anchor_resolves(self):
        """Not only the contents bullets -- prose links into a section too.

        The link and the heading have to move together, and a renamed heading
        breaks a prose link silently: the page still renders, the click just
        scrolls nowhere. The contents bullets were covered above;
        docs/validation.md's at-a-glance list is what prompted covering the
        rest, since it is a page of links into its own long record.
        """
        anchor = re.compile(r"\]\(#([^)]+)\)")
        seen = 0
        for page in self.pages():
            text = page.read_text(encoding="utf-8")
            headings = {heading_slug(h) for h in _HEADING.findall(text)}
            headings |= set(_EXPLICIT_ANCHOR.findall(text))
            for target in anchor.findall(text):
                seen += 1
                with self.subTest(page=page.name, target=target):
                    self.assertIn(
                        heading_slug(target),
                        headings,
                        f"{page.name} links to #{target} with no such heading",
                    )
        self.assertGreater(seen, 20)

    def test_absolute_links_into_this_repository_resolve(self):
        """The README's cross-references are absolute, so check those instead.

        They moved out of the relative check above and put the same question
        on a different string: `blob/main/<path>` names a file and
        `tree/main/<dir>` names a directory, and either is a 404 once what it
        names is renamed or unshipped.
        """
        checked = 0
        for page in self.pages():
            for target in _INLINE_LINK.findall(page.read_text(encoding="utf-8")):
                if not target.startswith(_REPO_URL):
                    continue
                rest = target[len(_REPO_URL) :].split("#", 1)[0]
                kind, _, path = rest.partition("/main/")
                if not path or kind not in ("blob", "tree"):
                    continue
                checked += 1
                with self.subTest(page=page.name, target=target):
                    linked = ROOT / path
                    self.assertTrue(
                        linked.exists(), f"{page.name} links to missing {path}"
                    )
                    if kind == "tree":
                        self.assertTrue(
                            (linked / "README.md").exists(),
                            f"{page.name} links to the directory {path}, which "
                            "has no README",
                        )
        self.assertGreater(checked, 10)

    def test_the_readme_carries_no_relative_file_link(self):
        """README.md is the PyPI long description, and PyPI serves it alone.

        A relative link is not a redirect there, it is a 404: the front page
        shipped to the index had every `docs/...` cross-reference in it
        pointing at nothing for anyone reading it on PyPI rather than in a
        checkout. In-page anchors are fine -- the headings come with the file.
        """
        for target in _INLINE_LINK.findall(README):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            with self.subTest(target=target):
                self.assertTrue(
                    target.startswith("#"),
                    f"README links to {target}; PyPI cannot serve that, so the "
                    "link has to be an absolute URL",
                )


class TestChangelogVersionLinksResolve(unittest.TestCase):
    """Every version heading links somewhere a reader can actually go.

    0.22.1 was written up as a release but never tagged, never pushed and
    never published, so two compare links -- its own, and 0.23.0's, whose
    range started at it -- pointed through a tag GitHub does not have and
    404'd. Five docs pages also told readers a fix landed in a version
    `pip install` cannot resolve. Nothing checked either, because a dead
    reference-style link still renders as a link.
    """

    CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    _VERSION_HEADING = re.compile(r"^## \[([0-9][^\]]*)\]", re.MULTILINE)
    _LINK_DEFINITION = re.compile(r"^\[([0-9][^\]]*)\]:\s*(\S+)$", re.MULTILINE)
    _COMPARE = re.compile(r"compare/v(\d+\.\d+\.\d+)\.\.\.v(\S+)$")

    def headings(self):
        """Released versions, newest first -- the order they appear in."""
        found = self._VERSION_HEADING.findall(self.CHANGELOG)
        # Guard against the regex silently matching nothing and every
        # assertion below passing over an empty list.
        self.assertGreater(len(found), 20, "the version headings stopped parsing")
        return found

    def definitions(self):
        return dict(self._LINK_DEFINITION.findall(self.CHANGELOG))

    def test_every_version_heading_has_a_link_definition(self):
        defined = self.definitions()
        for version in self.headings():
            with self.subTest(version=version):
                self.assertIn(
                    version,
                    defined,
                    f"## [{version}] is written as a reference link with no "
                    f"[{version}]: definition, so it renders as dead brackets",
                )

    def test_no_link_definition_outlives_its_heading(self):
        headings = set(self.headings())
        for version in sorted(self.definitions()):
            with self.subTest(version=version):
                self.assertIn(
                    version,
                    headings,
                    f"[{version}]: is defined with no ## [{version}] heading "
                    "above it -- a leftover from a renamed or dropped release",
                )

    def test_each_compare_range_starts_at_the_release_before_it(self):
        """Which is what makes the tags in the URL ones that exist.

        A range may only reach back to the previous *released* version. An
        unreleased one named here is a tag no clone has, and the link 404s.
        """
        order = self.headings()
        defined = self.definitions()
        for index, version in enumerate(order):
            url = defined.get(version, "")
            match = self._COMPARE.search(url)
            if match is None:
                # The oldest release has nothing to compare against and
                # links to its tree instead.
                self.assertEqual(version, order[-1], f"{version}: {url}")
                continue
            lower, upper = match.groups()
            previous = order[index + 1] if index + 1 < len(order) else None
            with self.subTest(version=version):
                self.assertEqual(upper, version, f"{version} compares to v{upper}")
                if previous is not None:
                    self.assertEqual(
                        lower,
                        previous,
                        f"[{version}] compares from v{lower}, but the release "
                        f"before it is {previous} -- v{lower} is not a tag",
                    )

    def test_every_released_heading_was_actually_tagged(self):
        """The invariant the internal checks above cannot see.

        0.22.1 was *self-consistently* documented -- its own heading, its
        own definition, and 0.23.0's range reaching back to it all agreed
        -- so no amount of cross-checking the file against itself could
        notice that the tag behind all three was never created. Only
        ground truth outside the document can, so this one asks git.

        Skipped where the clone has no tags to ask about: CI checks out at
        depth 1 and fetches none. It runs where releases are prepared,
        which is where a heading can be written for a tag that never
        follows.
        """
        tags = subprocess.run(
            ["git", "-C", str(ROOT), "tag", "--list", "v*"],
            capture_output=True,
            text=True,
            check=False,
        )
        if tags.returncode != 0 or not tags.stdout.strip():
            self.skipTest("no tags in this clone (a shallow or archived checkout)")
        present = {line.strip().lstrip("v") for line in tags.stdout.splitlines()}
        headings = self.headings()
        # The newest heading is the release being prepared: its version is
        # bumped in the working tree before the tag it describes exists.
        for version in headings[1:]:
            with self.subTest(version=version):
                self.assertIn(
                    version,
                    present,
                    f"## [{version}] is written as a released version, but "
                    f"v{version} is not a tag -- so its compare link 404s and "
                    "`pip install` cannot resolve it. Tag it, or mark the "
                    "heading unreleased the way 0.22.1 is",
                )

    def test_no_page_sends_a_reader_to_an_unreleased_version(self):
        """The 0.22.1 docs half of the same defect.

        A version with no bracketed heading was never released, so telling
        a reader a change landed there leaves them nothing to install.
        """
        released = set(self.headings())
        unreleased = re.compile(r"\b0\.22\.1\b")
        pages = sorted((ROOT / "docs").rglob("*.md")) + [ROOT / "README.md"]
        self.assertNotIn("0.22.1", released)
        for page in pages:
            with self.subTest(page=page.name):
                self.assertIsNone(
                    unreleased.search(page.read_text(encoding="utf-8")),
                    f"{page.name} names 0.22.1, which was never published -- "
                    "the change it describes shipped in 0.23.0",
                )


class TestSendKeysExamplesResolve(unittest.TestCase):
    """Every `{NAME}` in a shipped send_keys example must name a real key.

    `examples/07_keys_and_pointer.py` shipped
    `gui.send_keys("hello{SPACE}world")`. The grammar does not reject an
    unknown brace name -- it hands the name to the backend -- and `SPACE`
    is not one of the shared aliases (`SPC` and `SPA` are), not one of
    win32's long names, and not an X11 keysym name either, so the line
    raised `ValueError: unknown key name 'SPACE'` on both X11 and Windows.
    It happened to work on uinput alone, whose own table maps the name
    through to `KEY_SPACE`, which is why nothing noticed.
    """

    _BRACE = re.compile(r"\{([A-Za-z][A-Za-z0-9_]*)(\s+\d+)?\}")
    _CALL = re.compile(r"send_keys\(\s*([\"'])(.*?)\1")
    # `{PAUSE n}` is grammar, not a key name, and is checked in
    # tests/test_sendkeys.py instead.
    _RESERVED = {"PAUSE"}

    def pages(self):
        root = ROOT
        found = sorted((root / "examples").rglob("*.py"))
        found += sorted((root / "docs").rglob("*.md"))
        found.append(root / "README.md")
        return found

    def brace_names(self):
        """Every brace name inside a real send_keys(...) string literal."""
        seen = []
        for page in self.pages():
            text = page.read_text(encoding="utf-8")
            for line_number, line in enumerate(text.splitlines(), 1):
                for call in self._CALL.finditer(line):
                    for brace in self._BRACE.finditer(call.group(2)):
                        name = brace.group(1)
                        if name.upper() not in self._RESERVED:
                            seen.append((page, line_number, name))
        # Guard against the regexes silently matching nothing.
        self.assertTrue(seen, "no send_keys brace names found to check")
        return seen

    def test_every_brace_name_is_an_alias_or_a_keysym(self):
        from pyguitest.backends.base import GUIBackend

        try:
            from Xlib import XK
        except ImportError:  # pragma: no cover - depends on the platform
            self.skipTest(
                "python-xlib is not installed, so keysym names cannot be resolved here"
            )
        aliases = {name.upper() for name in GUIBackend.KEY_ALIASES}
        for page, line_number, name in self.brace_names():
            with self.subTest(page=page.name, name=name):
                self.assertTrue(
                    name.upper() in aliases or XK.string_to_keysym(name) != 0,
                    f"{page.name}:{line_number} sends {{{name}}}, which is "
                    "neither a shared key alias nor an X11 keysym name, so "
                    "the backend raises ValueError for it. The aliases are "
                    "short (SPC, BAC, ENT, ESC); a long name like SPACE is "
                    "not one of them",
                )


class TestEnvironmentVariablesAreDocumented(unittest.TestCase):
    """A knob nobody can find is a knob that does not exist.

    Read off the source rather than kept as a list here: both variables the
    package honours were described only in the docstring of the call that
    reads them, in the middle of a 3000-line module. The scan is what keeps
    the third one honest.
    """

    def variables(self):
        """Every `PYGUITEST_*` name the package's own source mentions."""
        found = set()
        for path in sorted((ROOT / "src").rglob("*.py")):
            found.update(_ENV_VAR.findall(path.read_text(encoding="utf-8")))
        return found

    def test_the_scan_finds_the_variables(self):
        # Guard against the glob silently matching nothing.
        self.assertIn("PYGUITEST_SCREENSHOT_DIR", self.variables())

    def test_every_variable_the_source_reads_is_documented(self):
        for name in sorted(self.variables()):
            with self.subTest(variable=name):
                self.assertIn(
                    name,
                    DOCS,
                    f"{name} is read by the source and named in no "
                    "user-facing page (README, docs/install.md, docs/input.md)",
                )


if __name__ == "__main__":
    unittest.main()
