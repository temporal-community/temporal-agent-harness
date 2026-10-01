"""Read the rules of a Dogwood policy file: their order, effect, annotations and actions.

Dogwood reports the rules that determined a decision by their 0-based position in the policy
file. Playpen needs more than the position — the ``@id`` to name the rule, the ``@reason`` to
explain it, and ``@on_deny("escalate")`` to send a call to a person instead of failing it — so
this module splits the file into statements the way Dogwood counts them: a ``permit`` or
``forbid`` is a rule; a ``def`` (a macro) is not.

The split is lexical: ``;`` ends a statement only outside strings, comments and brackets. That
is all this module needs from the grammar; Dogwood itself parses and validates the policy on
every decision, so a file this module misreads is a file Dogwood rejects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

# The annotations Playpen gives meaning to, and the values each may take. Any other annotation
# is carried through untouched; a known one with an unknown value is an error, because a typo
# in a security policy must not quietly turn an escalation into a hard deny or the reverse.
ON_DENY_ESCALATE = "escalate"
_ALLOWED_VALUES = {"on_deny": {ON_DENY_ESCALATE}}

_ANNOTATION = re.compile(r'@([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(\s*"((?:[^"\\]|\\.)*)"\s*\))?\s*')
_ACTION_ID = re.compile(r'Action::"((?:[^"\\]|\\.)*)"')


@dataclass(frozen=True)
class PolicyRule:
    """One ``permit`` or ``forbid`` rule, as Dogwood numbers it.

    ``actions`` are the action ids the rule's scope names (``action == NS::Action::"x"`` or
    ``action in [...]``); empty means the scope does not constrain the action."""

    index: int
    effect: Literal["permit", "forbid"]
    annotations: dict[str, str] = field(default_factory=dict)
    actions: frozenset[str] = frozenset()

    @property
    def id(self) -> str:
        return self.annotations.get("id", f"rule {self.index}")

    @property
    def escalates_on_deny(self) -> bool:
        return self.annotations.get("on_deny") == ON_DENY_ESCALATE

    def covers(self, action: str) -> bool:
        return not self.actions or action in self.actions


def parse_rules(source: str) -> list[PolicyRule]:
    """The rules of a policy file, in the order Dogwood numbers them.

    Raises ``ValueError`` for a statement that is neither a rule nor a macro, an unterminated
    string or bracket, or a Playpen annotation with a value it does not define."""
    rules: list[PolicyRule] = []
    for statement in _statements(source):
        annotations, rest = _leading_annotations(statement)
        keyword = rest.split(None, 1)[0] if rest else ""
        if keyword == "def":
            continue
        if keyword not in ("permit", "forbid"):
            raise ValueError(f"policy statement is neither a rule nor a macro: {statement!r}")
        rules.append(
            PolicyRule(
                index=len(rules),
                effect=keyword,  # type: ignore[arg-type]  (narrowed by the check above)
                annotations=annotations,
                actions=frozenset(_ACTION_ID.findall(_scope(rest))),
            )
        )
    return rules


def _statements(source: str) -> list[str]:
    """Split ``source`` at top-level ``;``, dropping ``//`` comments."""
    statements: list[str] = []
    current: list[str] = []
    depth = 0
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        if ch == '"':
            end = _string_end(source, i)
            current.append(source[i:end])
            i = end
            continue
        if source.startswith("//", i):
            newline = source.find("\n", i)
            i = n if newline == -1 else newline
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:
                raise ValueError(f"unbalanced {ch!r} at offset {i} of the policy")
        elif ch == ";" and depth == 0:
            text = "".join(current).strip()
            if text:
                statements.append(text)
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    if depth != 0:
        raise ValueError("the policy ends inside an open bracket")
    if "".join(current).strip():
        raise ValueError(f"the policy ends with an unterminated statement: {''.join(current)!r}")
    return statements


def _string_end(source: str, start: int) -> int:
    """The offset just past the string literal opening at ``start``."""
    i = start + 1
    while i < len(source):
        if source[i] == "\\":
            i += 2
            continue
        if source[i] == '"':
            return i + 1
        i += 1
    raise ValueError(f"unterminated string at offset {start} of the policy")


def _leading_annotations(statement: str) -> tuple[dict[str, str], str]:
    annotations: dict[str, str] = {}
    pos = 0
    while (match := _ANNOTATION.match(statement, pos)) is not None:
        key = match.group(1)
        value = _unescape(match.group(2) or "")
        allowed = _ALLOWED_VALUES.get(key)
        if allowed is not None and value not in allowed:
            raise ValueError(
                f"@{key}({value!r}) is not a value Playpen defines; use one of "
                f"{sorted(allowed)}"
            )
        annotations[key] = value
        pos = match.end()
    return annotations, statement[pos:].strip()


def _scope(rule_text: str) -> str:
    """The parenthesized scope that follows a rule's ``permit``/``forbid`` keyword."""
    open_at = rule_text.find("(")
    if open_at == -1:
        raise ValueError(f"rule has no scope: {rule_text!r}")
    depth = 0
    i = open_at
    while i < len(rule_text):
        ch = rule_text[i]
        if ch == '"':
            i = _string_end(rule_text, i)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return rule_text[open_at : i + 1]
        i += 1
    raise ValueError(f"rule scope is not closed: {rule_text!r}")


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)
