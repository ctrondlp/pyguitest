"""The X11::GUITest SendKeys grammar, one call at a time.

Lifted out of `Session.send_keys` rather than written fresh. That method was
one function holding three closures over four pieces of mutable state --
which keys are held, whether a group is open, and two per-call lookups from
the backend -- and it scored 26 on cyclomatic complexity, the single worst
in the package and the reason the ceiling exists at all.

Nothing about the grammar changed in the move. The state the closures shared
is this object's attributes, and each branch of the old loop is a method, so
the pieces can be read (and their comments kept) without holding the whole
tokenizer in mind at once. `Session.send_keys` keeps the documentation,
because that is where a caller looks for it.

Everything routes back through the `Session` rather than its backend:
`tap_key`/`press_key`/`release_key`/`wait` apply the session's configured
delays, and skipping them would make a refactor silently change timing.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle, types only
    from . import Session

__all__ = ["KeySender"]


class KeySender:
    """One `send_keys()` call, and the state it carries while running."""

    def __init__(self, session: Session) -> None:
        """Bind to `session` and read the backend's key tables once."""
        self.session = session
        self.modifiers = session.backend.MODIFIER_KEYS
        self.aliases = session.backend.KEY_ALIASES
        # Not .get(): every backend's MODIFIER_KEYS names a Shift key, and
        # press_literal cannot type a shifted character without one. Missing
        # it is a broken backend, and better reported here than as a
        # TypeError inside its key lookup halfway through the string.
        self.shift_name = self.modifiers["+"]
        self.held: list[str] = []
        self.grouped = False

    # -- the pieces the old closures were ----------------------------------

    def release_held(self) -> None:
        """Release every held modifier, newest first, and end any group."""
        for name in reversed(self.held):
            self.session.release_key(name)
        self.held.clear()
        self.grouped = False

    def press_literal(self, char: str) -> None:
        """Type one character, adding Shift only when it is not already held."""
        name, needs_shift = self.session.backend.resolve_char_key(char)
        auto_shift = needs_shift and self.shift_name not in self.held
        if auto_shift:
            self.session.press_key(self.shift_name)
        self.session.tap_key(name)
        if auto_shift:
            self.session.release_key(self.shift_name)

    @staticmethod
    def scan_brace(keys: str, start: int) -> tuple[str, int]:
        """The contents of the `{...}` opening at `start`, and what follows it.

        `{}}` is the escape for a literal closing brace, so a `}` directly
        after the terminator belongs to this token rather than starting the
        next one.
        """
        end = keys.find("}", start + 1)
        if end == -1:
            raise ValueError(f"unterminated '{{' in send_keys at position {start}")
        if keys[end + 1 : end + 2] == "}":
            end += 1
        return keys[start + 1 : end], end + 1

    def brace(self, content: str) -> None:
        """Run one `{...}`: key names, repeat counts and pauses."""
        tokens = [t for t in content.split(" ") if t]
        if not tokens:
            raise ValueError("empty {} in send_keys")
        pending_pause = False
        # (callable, key argument) for a repeat count. Annotated because
        # the two callables assigned below name their parameter
        # differently, which is enough to stop the type being inferred.
        last_action: tuple[Callable[[str], None], str] | None = None
        for token in tokens:
            if token.isdigit():
                pending_pause = self.count(token, pending_pause, last_action)
                continue
            if token.upper() == "PAUSE":
                pending_pause = True
                continue
            pending_pause = False
            if len(token) == 1:
                last_action = (self.press_literal, token)
            else:
                last_action = (
                    self.session.tap_key,
                    self.aliases.get(token.upper(), token),
                )
            last_action[0](last_action[1])

    def count(
        self,
        token: str,
        pending_pause: bool,
        last_action: tuple[Callable[[str], None], str] | None,
    ) -> bool:
        """Apply a numeric token -- a pause in ms, or a repeat of the last key.

        Returns the new `pending_pause`, which a pause consumes.
        """
        count = int(token)
        if count <= 0:
            raise ValueError(f"non-positive repeat count {token!r} in send_keys")
        if pending_pause:
            self.session.wait(count / 1000)
            return False
        if last_action is None:
            raise ValueError(f"repeat count {token!r} with no preceding key")
        action, arg = last_action
        for _ in range(count - 1):
            action(arg)
        return False

    # -- the loop ----------------------------------------------------------

    def send(self, keys: str) -> None:
        """Walk `keys`, acting on each character of the grammar."""
        i, n = 0, len(keys)
        while i < n:
            char = keys[i]
            if char == "{":
                content, i = self.scan_brace(keys, i)
                self.brace(content)
                continue
            if char == ")":
                self.release_held()
                i += 1
                continue
            step = self.character(char, keys, i, n)
            if step is not None:
                i = step
                continue
            i += 1
            if not self.grouped:
                self.release_held()

    def character(self, char: str, keys: str, i: int, n: int) -> int | None:
        """Act on one ordinary character.

        Returns the index to continue from when it consumed more than
        itself -- a modifier opening a group -- and None when the caller
        should advance by one and close any ungrouped modifiers, which is
        what makes `%(f)q` press Alt-f and then a plain q.
        """
        if char == "~":
            self.session.tap_key(self.aliases["ENT"])
            return None
        if char in self.modifiers:
            name = self.modifiers[char]
            if i + 1 < n and keys[i + 1] == "(":
                self.session.press_key(name)
                self.held.append(name)
                self.grouped = True
                return i + 2
            self.session.tap_key(name)
            return None
        if char == "(":
            self.grouped = True
            return None
        self.press_literal(char)
        return None
