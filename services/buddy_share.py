"""Detect share-to-buddy requests and extract recipe context from chat history."""

from __future__ import annotations

import re

from services import recipe_lookup

_SHARE_VERBS = ("send", "share", "email", "forward")

_SHARE_CONTEXT = (
    "recipe",
    "this",
    "it",
    "that",
    "buddy",
    "buddies",
    "friend",
    "cookbook",
    "dish",
    "meal",
    "one",
)

_NAME_STOPWORDS = {
    "this",
    "that",
    "the",
    "it",
    "my",
    "a",
    "an",
    "please",
    "thanks",
    "thank",
    "you",
    "can",
    "could",
    "would",
    "will",
    "him",
    "her",
    "them",
    "recipe",
}


def is_share_request(question: str) -> bool:
    lower = question.lower()
    if not any(verb in lower for verb in _SHARE_VERBS):
        return False
    return any(token in lower for token in _SHARE_CONTEXT)


def _normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def _extract_name_candidates(question: str) -> list[str]:
    patterns = [
        r"\b(?:my\s+)?buddy\s+([A-Za-z][A-Za-z'-]*(?:\s+[A-Za-z][A-Za-z'-]*){0,3})",
        r"\b(?:send|share|email|forward)(?:\s+\w+){0,8}?\s+to\s+(?:my\s+buddy\s+)?([A-Za-z][A-Za-z'-]*(?:\s+[A-Za-z][A-Za-z'-]*){0,3})",
        r"\bwith\s+(?:my\s+buddy\s+)?([A-Za-z][A-Za-z'-]*(?:\s+[A-Za-z][A-Za-z'-]*){0,3})",
    ]
    candidates: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, question, re.IGNORECASE):
            candidate = match.group(1).strip().rstrip("?.!,")
            if candidate and _normalize_name(candidate) not in _NAME_STOPWORDS:
                candidates.append(candidate)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for candidate in candidates:
        key = _normalize_name(candidate)
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def resolve_buddy_name(candidate: str, buddy_names: list[str]) -> str | None:
    """Match a partial or full buddy name to one saved cooking buddy."""
    if not candidate or not buddy_names:
        return None

    normalized_candidate = _normalize_name(candidate)
    for name in buddy_names:
        if _normalize_name(name) == normalized_candidate:
            return name

    lower_question = normalized_candidate
    for name in sorted(buddy_names, key=len, reverse=True):
        if _normalize_name(name) in lower_question or lower_question in _normalize_name(name):
            return name

    return _resolve_buddy(candidate, buddy_names)


def _resolve_buddy(candidate: str, buddy_names: list[str]) -> str | None:
    normalized = _normalize_name(candidate)
    if not normalized or normalized in _NAME_STOPWORDS:
        return None

    exact = [name for name in buddy_names if _normalize_name(name) == normalized]
    if len(exact) == 1:
        return exact[0]

    substring = [
        name
        for name in buddy_names
        if normalized in _normalize_name(name)
        or _normalize_name(name).startswith(f"{normalized} ")
    ]
    if len(substring) == 1:
        return substring[0]

    first_name = [
        name
        for name in buddy_names
        if _normalize_name(name).split()[0] == normalized
    ]
    if len(first_name) == 1:
        return first_name[0]

    return None


def detect_buddy_for_share(question: str, buddy_names: list[str]) -> str | None:
    if not buddy_names or not is_share_request(question):
        return None

    lower_question = question.lower()

    # Prefer longest full-name substring match ("Giora Glovatsky" in question)
    for name in sorted(buddy_names, key=len, reverse=True):
        if _normalize_name(name) in lower_question:
            return name

    # Partial names from phrasing ("buddy Giora", "send ... to Giora")
    for candidate in _extract_name_candidates(question):
        resolved = _resolve_buddy(candidate, buddy_names)
        if resolved:
            return resolved

    return None


def _parse_title(content: str) -> str:
    heading = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    if heading:
        return heading.group(1).strip()

    bold = re.search(r"\*\*([^*]+)\*\*", content)
    if bold:
        return bold.group(1).strip()

    numbered = re.search(
        r"^\d+\.\s+([^\n—\-]+?)(?:\s+[—\-]\s+|\s*$)", content, re.MULTILINE
    )
    if numbered:
        return numbered.group(1).strip()

    first_line = content.split("\n", 1)[0].strip()
    if first_line and len(first_line) <= 120:
        return first_line
    return "Recipe from Chef AI"


def extract_recipe_from_history(
    recent: list[dict], conn, user_id: str | None = None
) -> tuple[str, str] | None:
    """Return (title, body) for the most recent recipe discussed."""
    for msg in reversed(recent):
        if msg.get("role") != "assistant":
            continue
        content = str(msg.get("content") or "").strip()
        if len(content) < 40:
            continue
        if "couldn't send" in content.lower() or content.startswith("Done!"):
            continue
        return _parse_title(content), content

    slugs = recipe_lookup.resolve_active_recipe_slugs("", recent, conn, user_id=user_id)
    if slugs:
        blocks = recipe_lookup.build_authoritative_context(slugs[:1], conn, user_id=user_id)
        if blocks:
            block = blocks[0]
            return _parse_title(block), block

    return None
