"""A lexical pass over Perl source, for `pyguitest migrate`.

Perl cannot be parsed without running it, but the question `migrate` asks --
which X11::GUITest calls does this file make, and on which lines -- does not
need a parse. It needs to know which spans of text are code, and a tokenizer
that gets that right most of the time is far better than the regex it
replaces, which counted a name wherever its letters appeared: in a comment,
a string, a heredoc, POD, a hash key, a variable of the same name, a method
on some other object, another module's function of the same name. Each of
those inflated the cost of a port, and a name in a comment that happens to be
one of the six with no Wayland path turned the CI exit status red.

What is skipped, because it is not a call:

    comments, POD blocks, everything after __END__ / __DATA__
    '...' "..." `...` and q qq qw qr m s tr y with any delimiter, heredocs
    $name @name %name $#name ${name} (a variable shares nothing with a sub)
    ->name (a method), name-> (a class), Other::Module::name, name::,
    name => ..., $h{name} and $h{-name} (a bareword subscript is a string),
    labels and `goto name`
    `sub name`, `use constant name`, `package name` (a definition, not a use)

What is counted: a bare `name`, `&name` (also as a reference), and the fully
qualified `X11::GUITest::name`. A name the same file defines for itself is
reported as shadowed instead, since the calls to it are not X11::GUITest's --
except the fully qualified ones, which can only be.

This is a heuristic lexer, not Perl's. Two of its guesses are inherently
ambiguous in Perl itself (is this `/` a division or a pattern, is this `{` a
block or a subscript) and are settled by the token before them. Every
construct it cannot close -- an unterminated quote, a heredoc with no
terminator line -- falls back to being ordinary characters, so a wrong guess
costs a misread span rather than the rest of the file. It does not look inside
interpolated strings or `eval "..."`, and it does not track `use` import
lists; the cost of both is a call it never sees, which the per-file report
makes visible by saying when a file never names the module at all.
"""

import bisect
import dataclasses
import functools
import re

from .compat import CONSTANTS, LEGACY

_QUALIFIER = "X11::GUITest::"
_MODULE = "X11::GUITest"

_WORD = re.compile(r"[^\W\d]\w*(?:::[^\W\d]\w*)*(?:::)?")
_NUMBER = re.compile(
    r"0[xX][0-9a-fA-F_]+|0[bB][01_]+|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?"
)
_BLANKS = re.compile(r"[ \t\r\f\v]+")
_NAMED_HASH = re.compile(r"(?:::)?[^\W\d]\w*(?:::\w+)*|\{\s*\^?\w+\s*\}")
_NAMED_VARIABLE = re.compile(r"(?:::)?[^\W\d]\w*(?:::\w+)*|\d+|\^\w|\{\s*\^?\w+\s*\}")
_HEREDOC = re.compile(r"""<<(~?)(?:"([^"\n]*)"|'([^'\n]*)'|([^\W\d]\w*))""")
_FAT_COMMA = re.compile(r"\s*=>")
_BRACE_CLOSE = re.compile(r"\s*\}")
_LABEL = re.compile(r"\s*:(?!:)")
_ARROW = re.compile(r"\s*->")
_POD_START = re.compile(r"=[A-Za-z]")
_POD_END = re.compile(r"^=cut\b.*$", re.MULTILINE)
_LINE_PATTERN = re.compile(r"\\.|[/\n]")
_CURSOR_SHAPE = re.compile(r"XC_[A-Z0-9_]+")

# The scalar punctuation variables a `$` can be glued to. Listed because most
# of them are quote characters or comment openers that must not be mistaken
# for one: `$"`, `$'`, `$/`, `$\`, `$#`.
_SCALAR_PUNCTUATION = frozenset("&`'+!@/\\,;.:?|\"-<>")
_ARRAY_PUNCTUATION = frozenset("-+")
_HASH_PUNCTUATION = frozenset("+-!")

_BRACKETS = {"(": ")", "[": "]", "{": "}", "<": ">"}
_QUOTE_OPS = frozenset({"q", "qq", "qw", "qr", "m", "s", "tr", "y"})
_TWO_PART_OPS = frozenset({"s", "tr", "y"})
_PATTERN_OPS = frozenset({"m", "qr", "s", "tr", "y"})
# A character after q/s/m/y that means the word was not a quote operator: a
# hash key (`y => 1`), a closing bracket. A comma is a delimiter only when it
# touches the word (`m,/,`), never in a list that happens to hold the word.
_NOT_A_DELIMITER = frozenset(";)]}>=-")
# Where a `{` opens a subscript or a dereference, so its `}` ends an operand.
_BRACE_OPENS_VALUE = frozenset({"->", "]", "$", "@", "%", "*", "&"})
_END_WORDS = frozenset({"__END__", "__DATA__"})
_JUMPS = frozenset({"goto", "last", "next", "redo"})
_STATEMENT_STARTS = frozenset({"", ";", "{", "}"})
# Each unterminated quote or heredoc is searched for to the end of the file
# before it is given up on, so a file full of them (binary data, minified
# code) would cost quadratic time. Past this many, they are not looked for.
_MAX_UNTERMINATED = 64
_DECLARING = frozenset({"use", "no", "require", "package"})

# Words after which a term, not an operator, comes next -- which is what
# decides whether a following `/` opens a pattern or divides, and whether a
# following `%` is a hash or a modulus. Any other word is taken to be a value
# (a constant, a bareword call), after which the operator reading is right.
_TERM_TEXT = """
    and or not xor if elsif unless while until foreach for when given return
    x lt gt le ge eq ne cmp split grep map join push unshift splice print
    printf say sort reverse keys values each delete exists defined scalar ref
    lc uc lcfirst ucfirst length sprintf die warn do eval my our local open
    close wantarray unlink chomp chop
"""
_TERM_WORDS = frozenset(_TERM_TEXT.split())


@dataclasses.dataclass
class PerlScan:
    """What one Perl source mentions of X11::GUITest.

    `calls` and `constants` map a name to the lines it is used on, one entry
    per use; every `XC_<shape>` is folded under `XC_*`, the one row the
    constants table carries. `imports_guitest` is whether the file names the
    module at all (`use`, `require`, or a qualified call). `shadowed` lists
    the names the file defines for itself and calls unqualified, which are
    therefore left out of the other two.
    """

    calls: dict[str, list[int]]
    constants: dict[str, list[int]]
    imports_guitest: bool
    shadowed: list[str]


@functools.cache
def _delimiter_pattern(opener, closer):
    """A matcher for the characters that matter inside a quoted section."""
    chars = re.escape(opener) + (re.escape(closer) if closer != opener else "")
    return re.compile(r"\\.|[" + chars + "]", re.DOTALL)


class _Lexer:
    """One pass over a source, recording the GUITest names found in code."""

    def __init__(self, source):
        self.src = source
        self.size = len(source)
        self.pos = 0
        self.line_starts = [0] + [m.end() for m in re.finditer("\n", source)]
        # Where a heredoc body ends, keyed by the newline that precedes it:
        # reaching that newline jumps over the body instead of lexing it.
        self.jumps: dict[int, int] = {}
        self.prev_kind = "op"
        self.prev_text = ""
        # One flag per open brace: whether its close ends an operand.
        self.braces: list[bool] = []
        self.hits: list[tuple[str, str, int, bool]] = []
        self.local_defs: set[str] = set()
        self.imports = False
        self.unterminated = 0
        self.constant_next = False
        self.constant_depth: int | None = None
        self.handlers = self._handlers()

    def _handlers(self):
        table = {
            "\n": self._newline,
            "#": self._comment,
            "$": self._dollar,
            "@": self._at,
            "%": self._percent,
            "*": self._star,
            "/": self._slash,
            "<": self._less_than,
            "'": self._quote,
            '"': self._quote,
            "`": self._quote,
            "{": self._open_brace,
            "}": self._close_brace,
        }
        for blank in " \t\r\f\v":
            table[blank] = self._blank
        for digit in "0123456789":
            table[digit] = self._number
        return table

    def run(self):
        """Lex the whole source, leaving the findings on the instance."""
        self._pod()
        while self.pos < self.size:
            char = self.src[self.pos]
            handler = self.handlers.get(char)
            if handler is None:
                handler = self._word if char.isalpha() or char == "_" else self._other
            handler()

    def result(self):
        """The findings, with names the file defines itself set aside."""
        calls: dict[str, list[int]] = {}
        constants: dict[str, list[int]] = {}
        shadowed = set()
        for raw, folded, line, qualified in self.hits:
            if not qualified and raw in self.local_defs:
                shadowed.add(raw)
                continue
            target = calls if folded in LEGACY else constants
            target.setdefault(folded, []).append(line)
        return PerlScan(calls, constants, self.imports, sorted(shadowed))

    # -- token bookkeeping -------------------------------------------------

    def _set(self, kind, text=""):
        self.prev_kind = kind
        self.prev_text = text
        self.constant_next = False

    def _prev_is_value(self):
        """Whether the last token ended an operand, so an operator comes next."""
        if self.prev_kind in ("var", "num", "str", "close"):
            return True
        return self.prev_kind == "word" and self.prev_text not in _TERM_WORDS

    def _line_of(self, offset):
        return bisect.bisect_right(self.line_starts, offset)

    # -- whitespace, comments, POD, heredoc bodies -------------------------

    def _blank(self):
        match = _BLANKS.match(self.src, self.pos)
        assert match is not None  # dispatched on a blank character
        self.pos = match.end()

    def _newline(self):
        resume = self.jumps.pop(self.pos, None)
        self.pos = self.pos + 1 if resume is None else resume
        self._pod()

    def _comment(self):
        end = self.src.find("\n", self.pos)
        self.pos = self.size if end < 0 else end

    def _pod(self):
        """Skip a POD block if one starts here; POD begins only at a line start."""
        if _POD_START.match(self.src, self.pos):
            end = _POD_END.search(self.src, self.pos)
            self.pos = self.size if end is None else end.end()

    def _heredoc(self, match):
        indented, double, single, bare = match.groups()
        tag = next(t for t in (double, single, bare) if t is not None)
        line_end = self.src.find("\n", match.end())
        if line_end < 0:
            return False
        body = self.jumps.get(line_end, line_end + 1)
        resume = self._terminator(tag, body, bool(indented))
        if resume is None:
            return False
        self.jumps[line_end] = resume
        self.pos = match.end()
        self._set("str")
        return True

    def _terminator(self, tag, start, indented):
        """Where the line after a heredoc's terminator starts, or None."""
        if self.unterminated >= _MAX_UNTERMINATED:
            return None
        pos = start
        while pos <= self.size:
            newline = self.src.find("\n", pos)
            end = self.size if newline < 0 else newline
            line = self.src[pos:end].rstrip("\r")
            if (line.strip() if indented else line) == tag:
                return min(end + 1, self.size)
            if newline < 0:
                break
            pos = newline + 1
        self.unterminated += 1
        return None

    # -- variables, which share nothing with a sub of the same name --------

    def _dollar(self):
        src, after = self.src, self.pos + 1
        if src.startswith("#", after):
            named = _NAMED_VARIABLE.match(src, after + 1)
            self.pos = named.end() if named else after + 1
            self._set("var" if named else "op")
            return
        named = _NAMED_VARIABLE.match(src, after)
        if named:
            self.pos = named.end()
            self._set("var")
        elif src[after : after + 1] in _SCALAR_PUNCTUATION:
            self.pos = after + 1
            self._set("var")
        else:
            self.pos = after
            self._set("op", "$")

    def _at(self):
        self._sigil(_NAMED_HASH, _ARRAY_PUNCTUATION, "@")

    def _percent(self):
        if self._prev_is_value():
            self.pos += 1
            self._set("op", "%")
        else:
            self._sigil(_NAMED_HASH, _HASH_PUNCTUATION, "%")

    def _star(self):
        if self._prev_is_value():
            self.pos += 1
            self._set("op", "*")
        else:
            self._sigil(_NAMED_HASH, frozenset(), "*")

    def _sigil(self, pattern, punctuation, text):
        after = self.pos + 1
        named = pattern.match(self.src, after)
        if named:
            self.pos = named.end()
            self._set("var")
        elif self.src[after : after + 1] in punctuation:
            self.pos = after + 1
            self._set("var")
        else:
            self.pos = after
            self._set("op", text)

    # -- quoted text -------------------------------------------------------

    def _section(self, pos, opener):
        """Offset just past the delimiter that closes a section starting at `pos`."""
        if self.unterminated >= _MAX_UNTERMINATED:
            return None
        closer = _BRACKETS.get(opener, opener)
        depth = 1
        for match in _delimiter_pattern(opener, closer).finditer(self.src, pos):
            token = match.group()
            if token == closer:
                depth -= 1
            elif token == opener:
                depth += 1
            if depth == 0:
                return match.end()
        self.unterminated += 1
        return None

    def _delimited(self, pos, parts):
        """Offset past `parts` delimited sections, the first opening at `pos`."""
        opener = self.src[pos]
        end = self._section(pos + 1, opener)
        if end is None or parts == 1:
            return end
        if opener not in _BRACKETS:
            return self._section(end, opener)
        while end < self.size and self.src[end].isspace():
            end += 1
        if end >= self.size:
            return None
        return self._section(end + 1, self.src[end])

    def _quote(self):
        """A '...', "..." or `...` string, possibly spanning lines."""
        end = self._section(self.pos + 1, self.src[self.pos])
        if end is None:
            self.pos += 1
            self._set("op", self.src[self.pos - 1])
        else:
            self.pos = end
            self._set("str")

    def _quote_like(self, word, start):
        """Skip a q// qq// qw// qr// m// s/// tr/// y/// form; False if not one."""
        src = self.src
        if self.prev_text in ("->", "sub"):
            return False
        if word == "s" and src[start - 1 : start] == "-":
            return False
        index = self.pos
        while index < self.size and src[index].isspace():
            index += 1
        if index >= self.size:
            return False
        delimiter = src[index]
        if delimiter in _NOT_A_DELIMITER or delimiter.isalnum() or delimiter == "_":
            return False
        if delimiter in "#," and index > self.pos:
            return False
        end = self._delimited(index, 2 if word in _TWO_PART_OPS else 1)
        if end is None:
            return False
        if word in _PATTERN_OPS:
            while end < self.size and src[end].isalpha():
                end += 1
        self.pos = end
        self._set("str")
        return True

    def _slash(self):
        """A division, `//`, or a /pattern/ -- told apart by what came before."""
        src, pos = self.src, self.pos
        if self._prev_is_value():
            self.pos += 2 if src.startswith("//", pos) else 1
            self._set("op", "/")
            return
        for match in _LINE_PATTERN.finditer(src, pos + 1):
            if match.group() == "\n":
                break
            if match.group() != "/":
                continue
            end = match.end()
            while end < self.size and src[end].isalpha():
                end += 1
            self.pos = end
            self._set("str")
            return
        self.pos += 1
        self._set("op", "/")

    def _less_than(self):
        match = _HEREDOC.match(self.src, self.pos)
        if match is None or not self._heredoc(match):
            self.pos += 1
            self._set("op", "<")

    # -- punctuation and numbers -------------------------------------------

    def _open_brace(self):
        value = (
            self.prev_kind == "var"
            or self.prev_text in _BRACE_OPENS_VALUE
            or (self.prev_kind == "close" and self.prev_text == "}")
        )
        self.braces.append(value)
        if self.constant_next:
            self.constant_depth = len(self.braces)
        self.pos += 1
        self._set("op", "{")

    def _close_brace(self):
        if self.constant_depth == len(self.braces):
            self.constant_depth = None
        value = self.braces.pop() if self.braces else True
        self.pos += 1
        self._set("close" if value else "end", "}")

    def _number(self):
        match = _NUMBER.match(self.src, self.pos)
        assert match is not None  # dispatched on a digit
        self.pos = match.end()
        self._set("num")

    def _other(self):
        char = self.src[self.pos]
        if self.src.startswith("->", self.pos):
            self.pos += 2
            self._set("op", "->")
            return
        self.pos += 1
        self._set("close" if char in ")]" else "op", char)

    # -- names -------------------------------------------------------------

    def _word(self):
        match = _WORD.match(self.src, self.pos)
        if match is None:
            self._other()
            return
        word, start = match.group(), self.pos
        self.pos = match.end()
        if word in _QUOTE_OPS and self._quote_like(word, start):
            return
        before_kind, before = self.prev_kind, self.prev_text
        pending = self.constant_next
        self._set("word", word)
        if word in _END_WORDS and before != "->":
            self.pos = self.size
        elif before == "->":
            return
        elif before_kind == "word" and before == "sub":
            self.local_defs.add(word)
        elif before_kind == "word" and before in _DECLARING:
            self._declared(word, before)
        elif pending:
            self.local_defs.add(word)
        else:
            self._reference(word, start, before)

    def _declared(self, word, keyword):
        """A name in `use X`, `require X`, `package X`: a module, not a call."""
        if word == _MODULE:
            self.imports = True
        elif keyword == "use" and word == "constant":
            self.constant_next = True

    def _reference(self, word, start, before):
        """A bareword in expression position: a call, a key, or something else."""
        end = start + len(word)
        if _FAT_COMMA.match(self.src, end):
            if self.constant_depth == len(self.braces):
                self.local_defs.add(word)
            return
        if (
            before in ("{", "-")
            and self.braces
            and self.braces[-1]
            and _BRACE_CLOSE.match(self.src, end)
        ):
            return
        if (
            before in _JUMPS
            or _ARROW.match(self.src, end)
            or (before in _STATEMENT_STARTS and _LABEL.match(self.src, end))
        ):
            return
        self._record(word, start)

    def _record(self, word, start):
        qualified = word.startswith(_QUALIFIER)
        name = word[len(_QUALIFIER) :] if qualified else word
        if "::" in name:
            return
        if name in LEGACY or (name in CONSTANTS and name != "XC_*"):
            folded = name
        elif _CURSOR_SHAPE.fullmatch(name):
            folded = "XC_*"
        else:
            return
        if qualified:
            self.imports = True
        self.hits.append((name, folded, self._line_of(start), qualified))


def scan(source: str) -> PerlScan:
    """Find the X11::GUITest functions and constants a Perl source uses."""
    lexer = _Lexer(source)
    lexer.run()
    return lexer.result()
