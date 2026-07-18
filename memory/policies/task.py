"""Task state-machine policy."""

from __future__ import annotations

import re


ALLOWED_TRANSITIONS = {
    "todo": {"in_progress", "blocked", "done", "cancelled"},
    "in_progress": {"blocked", "done", "cancelled"},
    "blocked": {"in_progress", "done", "cancelled"},
    "done": {"in_progress"},
    "cancelled": {"todo", "in_progress"},
}


_IDENTIFIER_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#._-]*|\d+(?:[._-]\d+)*")


def task_identifiers(text: str) -> frozenset[str]:
    """Return explicit, code-like task identifiers without using prose words.

    Natural-language task titles often share verbs and templates, so similarity
    alone is not evidence that they describe one task.  Codes, ticket labels,
    versions, and all-caps abbreviations are concrete identity evidence.
    """
    identifiers: set[str] = set()
    for token in _IDENTIFIER_TOKEN_RE.findall(str(text or "")):
        has_code_punctuation = any(character in "#._-" for character in token)
        if any(character.isdigit() for character in token) or has_code_punctuation or token.isupper():
            identifiers.add(token.casefold())
    return frozenset(identifiers)


def identifiers_compatible(left_content: str, right_content: str) -> bool:
    """Avoid merging distinct coded tasks that merely share a prose template."""
    left = task_identifiers(left_content)
    right = task_identifiers(right_content)
    return not left or not right or left == right


def can_transition(old_status: str | None, new_status: str | None) -> bool:
    if new_status is None or old_status == new_status:
        return True
    if old_status is None:
        return True
    return new_status in ALLOWED_TRANSITIONS.get(old_status, set())


def is_terminal(status: str | None) -> bool:
    return status in {"done", "cancelled"}
