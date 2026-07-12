"""Exact recipe lookup for Chef AI — manifest + RDS titles, full S3 markdown."""

from __future__ import annotations

import re

from services import s3_recipes

_FOLLOWUP_RE = re.compile(
    r"\b(this|that|the)\s+recipe\b|"
    r"tags?\s+of\b|"
    r"last\s+(question|recipe)\b|"
    r"what\s+was\s+my\b|"
    r"my\s+last\s+question\b|"
    r"\b(?:recipe\s*#?\s*\d+|(?:the\s+)?(?:first|second|third)\b(?:\s+(?:one|recipe|suggestion))?)\b",
    re.IGNORECASE,
)

_LIST_ITEM_RE = re.compile(r"^\s*(\d+)\.\s+(.+?)\s*$", re.MULTILINE)
_RECIPE_REF_RE = re.compile(
    r"\b(?:recipe\s*#?\s*(\d+)|(?:the\s+)?(first|second|third)\b(?:\s+(?:one|recipe|suggestion))?)\b",
    re.IGNORECASE,
)
_ORDINAL_TO_NUM = {"first": 1, "second": 2, "third": 3}
_PLACEHOLDER_TITLE_RE = re.compile(r"^suggested\s+recipe\s+\d+$", re.IGNORECASE)


def _normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", str(title).strip().lower())


def _title_pattern(title: str) -> re.Pattern[str]:
    words = re.escape(_normalize_title(title))
    return re.compile(rf"\b{words}\b", re.IGNORECASE)


def build_title_index(conn, user_id: str | None = None) -> list[tuple[str, str, str]]:
    """Return (title, slug, s3_key) sorted by title length descending."""
    manifest = s3_recipes._load_catalog_manifest()
    entries: dict[str, tuple[str, str, str]] = {}

    for slug, item in manifest.items():
        title = (item.get("title") or s3_recipes._title_from_slug(slug)).strip()
        s3_key = item.get("s3_key") or s3_recipes.catalog_s3_key(slug)
        if title:
            entries[slug] = (title, slug, s3_key)

    sql = "SELECT slug, title, s3_key FROM recipes"
    params: tuple = ()
    if user_id:
        sql += " WHERE author_id = %s"
        params = (user_id,)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        for row in cur.fetchall():
            slug = row["slug"]
            title = (row["title"] or "").strip()
            s3_key = row.get("s3_key") or s3_recipes.catalog_s3_key(slug)
            if title:
                entries[slug] = (title, slug, s3_key)

    return sorted(entries.values(), key=lambda item: len(item[0]), reverse=True)


def detect_recipe_slugs(
    text: str, conn, max_results: int = 2, user_id: str | None = None
) -> list[str]:
    """Find cookbook slugs whose titles appear in text (longest match wins)."""
    if not (text or "").strip():
        return []

    index = build_title_index(conn, user_id=user_id)
    matches: list[tuple[int, str]] = []

    for title, slug, _s3_key in index:
        if _title_pattern(title).search(text):
            matches.append((len(title), slug))

    if not matches:
        return []

    matches.sort(key=lambda item: item[0], reverse=True)
    slugs: list[str] = []
    for _length, slug in matches:
        if slug not in slugs:
            slugs.append(slug)
        if len(slugs) >= max_results:
            break
    return slugs


def is_followup_about_recipe(question: str) -> bool:
    return bool(_FOLLOWUP_RE.search(question or ""))


def last_user_message(history: list[dict]) -> str:
    for msg in reversed(history):
        if msg.get("role") == "user":
            return str(msg.get("content") or "")
    return ""


def last_assistant_message(history: list[dict]) -> str:
    for msg in reversed(history):
        if msg.get("role") == "assistant":
            return str(msg.get("content") or "")
    return ""


def parse_numbered_recipe_list(text: str) -> dict[int, str]:
    """Parse '1. Recipe Name' lines from an assistant suggestion list."""
    items: dict[int, str] = {}
    for match in _LIST_ITEM_RE.finditer(text or ""):
        number = int(match.group(1))
        title = match.group(2).strip()
        title = re.sub(r"\s*[-–—]\s*.*$", "", title).strip()
        if not title or _PLACEHOLDER_TITLE_RE.match(title):
            continue
        items[number] = title
    return items


def resolve_recipe_number_reference(
    question: str, history: list[dict], conn, max_results: int = 1, user_id: str | None = None
) -> list[str]:
    """Map 'recipe 1' / 'the first one' to a cookbook slug from the prior suggestion list."""
    match = _RECIPE_REF_RE.search(question or "")
    if not match:
        return []

    if match.group(1):
        number = int(match.group(1))
    else:
        number = _ORDINAL_TO_NUM.get((match.group(2) or "").lower(), 0)
    if number < 1:
        return []

    listed = parse_numbered_recipe_list(last_assistant_message(history))
    title = listed.get(number)
    if not title:
        return []

    return detect_recipe_slugs(title, conn, max_results=max_results, user_id=user_id)


def resolve_active_recipe_slugs(
    question: str, history: list[dict], conn, max_results: int = 2, user_id: str | None = None
) -> list[str]:
    """Resolve which recipe(s) the user is asking about (current or follow-up)."""
    slugs = detect_recipe_slugs(question, conn, max_results=max_results, user_id=user_id)
    if slugs:
        return slugs

    slugs = resolve_recipe_number_reference(
        question, history, conn, max_results=max_results, user_id=user_id
    )
    if slugs:
        return slugs

    if not is_followup_about_recipe(question):
        return []

    combined = " ".join(
        str(msg.get("content") or "")
        for msg in reversed(history)
        if msg.get("role") in ("user", "assistant")
    )
    return detect_recipe_slugs(combined, conn, max_results=max_results, user_id=user_id)


def load_recipe_context(slug: str, conn, user_id: str | None = None) -> str | None:
    """Load full recipe markdown as an authoritative context block."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT author_id::text AS author_id, s3_key FROM recipes WHERE slug = %s",
            (slug,),
        )
        owned = cur.fetchone()

    if owned and user_id and str(owned["author_id"]) != user_id:
        return None

    index = build_title_index(conn, user_id=user_id)
    title = slug.replace("-", " ").title()
    s3_key = s3_recipes.catalog_s3_key(slug)

    for entry_title, entry_slug, entry_key in index:
        if entry_slug == slug:
            title = entry_title
            s3_key = entry_key
            break

    try:
        md = s3_recipes.get_recipe_content(s3_key)
    except Exception:
        return None

    parsed_title = s3_recipes.parse_title(md) or title
    return (
        f"[Authoritative cookbook entry — {parsed_title} (slug: {slug})]\n"
        f"{md.strip()}"
    )


def build_authoritative_context(
    slugs: list[str], conn, user_id: str | None = None
) -> list[str]:
    blocks: list[str] = []
    for slug in slugs:
        block = load_recipe_context(slug, conn, user_id=user_id)
        if block:
            blocks.append(block)
    return blocks


def build_retrieval_query(
    question: str, history: list[dict], conn, user_id: str | None = None
) -> str:
    """Expand KB retrieval query with recipe names and recent user context."""
    parts = [question.strip()]
    slugs = resolve_active_recipe_slugs(question, history, conn, user_id=user_id)
    index = build_title_index(conn, user_id=user_id)
    slug_to_title = {slug: title for title, slug, _key in index}

    for slug in slugs:
        title = slug_to_title.get(slug)
        if title:
            parts.append(title)

    prev_user = last_user_message(history)
    if prev_user and prev_user.strip() != question.strip():
        parts.append(prev_user.strip())

    return " ".join(part for part in parts if part)
