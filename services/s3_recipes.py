"""Unified recipe index from S3 catalog markdown files plus RDS metadata."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import TypedDict

import boto3

from config import Config

_INDEX_TTL_SECONDS = 600
_s3_client = None
_index_cache: dict[str, tuple[list["RecipeIndexEntry"], float]] = {}
_manifest_cache: dict[str, dict] | None = None
_manifest_expires_at = 0.0

_TITLE_RE = re.compile(r"^#\s+(.+)", re.MULTILINE)
_TAGS_RE = re.compile(r"^\*\*Tags:\*\*\s*(.+)", re.MULTILINE | re.IGNORECASE)
_CATALOG_PREFIX = "catalog/"
_USER_PREFIX = "users/"
_MANIFEST_KEY_SUFFIX = "catalog/manifest.json"


class RecipeIndexEntry(TypedDict):
    s3_key: str
    slug: str
    title: str
    source: str
    description: str
    tags: list
    image_url: str | None
    image_status: str
    author_username: str | None
    created_at: datetime | None
    last_modified: datetime | None


def _s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3", region_name=Config.AWS_REGION)
    return _s3_client


def catalog_s3_key(slug: str) -> str:
    return f"{Config.S3_RECIPES_PREFIX}{_CATALOG_PREFIX}{slug}.md"


def user_recipe_s3_key(user_id: str, slug: str) -> str:
    return f"{Config.S3_RECIPES_PREFIX}{_USER_PREFIX}{user_id}/{slug}.md"


def invalidate_index_cache() -> None:
    global _index_cache, _manifest_cache, _manifest_expires_at
    _index_cache = {}
    _manifest_cache = None
    _manifest_expires_at = 0.0


def parse_title(md_text: str) -> str:
    match = _TITLE_RE.search(md_text or "")
    if match:
        return match.group(1).strip()
    return ""


def parse_tags(md_text: str) -> list[str]:
    match = _TAGS_RE.search(md_text or "")
    if not match:
        return []
    raw = match.group(1).strip()
    tags = [part.strip().lower() for part in raw.split(",") if part.strip()]
    seen: set[str] = set()
    unique: list[str] = []
    for tag in tags:
        if tag not in seen:
            seen.add(tag)
            unique.append(tag)
    return unique


def _slug_from_key(key: str) -> str:
    prefix = Config.S3_RECIPES_PREFIX
    relative = key[len(prefix) :] if key.startswith(prefix) else key
    if relative.startswith(_USER_PREFIX) and relative.endswith(".md"):
        return relative.rsplit("/", 1)[-1][:-3]
    if relative.startswith(_CATALOG_PREFIX) and relative.endswith(".md"):
        return relative[len(_CATALOG_PREFIX) : -3]
    if relative.endswith(".md"):
        return relative[:-3]
    return relative


def _foreign_owned_slugs(conn, user_id: str) -> set[str]:
    """Slugs owned by other users (hide their private recipes from this user's index)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT slug FROM recipes WHERE author_id != %s",
            (user_id,),
        )
        return {row["slug"] for row in cur.fetchall()}


def user_can_access_recipe(conn, user_id: str, slug: str, s3_key: str = "") -> bool:
    """True if user may read/share this recipe (catalog or own user recipe)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT author_id::text AS author_id, s3_key FROM recipes WHERE slug = %s",
            (slug,),
        )
        row = cur.fetchone()
    if row:
        return str(row["author_id"]) == user_id
    user_prefix = f"{Config.S3_RECIPES_PREFIX}{_USER_PREFIX}"
    if s3_key.startswith(user_prefix):
        return f"/{user_id}/" in s3_key or s3_key.startswith(
            f"{user_prefix}{user_id}/"
        )
    return True


def _title_from_slug(slug: str) -> str:
    return slug.replace("-", " ").strip().title()


def _manifest_key() -> str:
    return f"{Config.S3_RECIPES_PREFIX}{_MANIFEST_KEY_SUFFIX}"


def _load_catalog_manifest() -> dict[str, dict]:
    global _manifest_cache, _manifest_expires_at

    now = time.monotonic()
    if _manifest_cache is not None and now < _manifest_expires_at:
        return _manifest_cache

    lookup: dict[str, dict] = {}
    if not Config.S3_BUCKET:
        _manifest_cache = lookup
        _manifest_expires_at = now + _INDEX_TTL_SECONDS
        return lookup

    try:
        response = _s3().get_object(Bucket=Config.S3_BUCKET, Key=_manifest_key())
        payload = json.loads(response["Body"].read().decode("utf-8"))
        for item in payload.get("recipes", []):
            slug = item.get("slug")
            if not slug:
                continue
            tags = item.get("tags") or []
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]
            lookup[slug] = {
                "title": item.get("title") or _title_from_slug(slug),
                "tags": tags,
                "s3_key": item.get("s3_key") or catalog_s3_key(slug),
                "image_url": (item.get("image_url") or "").strip() or None,
            }
    except Exception:
        pass

    _manifest_cache = lookup
    _manifest_expires_at = now + _INDEX_TTL_SECONDS
    return lookup


def get_image_url(slug: str, conn) -> str | None:
    from services import recipe_images

    return recipe_images.resolve_image_url(slug, conn)


def get_image_state(slug: str, conn) -> dict:
    from services import recipe_images

    return recipe_images.resolve_image_state(slug, conn)


def _fetch_md_header(key: str) -> str:
    try:
        response = _s3().get_object(
            Bucket=Config.S3_BUCKET,
            Key=key,
            Range="bytes=0-2047",
        )
        return response["Body"].read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def _load_rds_meta(conn, user_id: str | None = None) -> dict[str, dict]:
    sql = """
            SELECT slug, title, description, tags, author_username,
                   author_id::text AS author_id, created_at, s3_key
            FROM recipes
            """
    params: tuple = ()
    if user_id:
        sql += " WHERE author_id = %s"
        params = (user_id,)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        meta: dict[str, dict] = {}
        for row in cur.fetchall():
            r = dict(row)
            tags = r.get("tags") or []
            if isinstance(tags, str):
                tags = json.loads(tags)
            meta[r["slug"]] = {
                "title": r["title"],
                "description": r.get("description") or "",
                "tags": tags or [],
                "author_username": r.get("author_username"),
                "author_id": r.get("author_id"),
                "created_at": r.get("created_at"),
                "s3_key": r.get("s3_key") or catalog_s3_key(r["slug"]),
            }
        return meta


def build_recipe_index(conn, user_id: str | None = None) -> list[RecipeIndexEntry]:
    cache_key = user_id or "__public__"
    now = time.monotonic()
    cached = _index_cache.get(cache_key)
    if cached and now < cached[1]:
        return cached[0]

    if not Config.S3_BUCKET:
        return []

    rds_meta = _load_rds_meta(conn, user_id)
    foreign_slugs = _foreign_owned_slugs(conn, user_id) if user_id else set()
    manifest = _load_catalog_manifest()
    manifest_slugs = set(manifest.keys()) if manifest else set()
    from services import recipe_images

    image_rows = recipe_images.load_image_rows(conn)
    entries: list[RecipeIndexEntry] = []
    indexed_keys: set[str] = set()
    catalog_prefix = f"{Config.S3_RECIPES_PREFIX}{_CATALOG_PREFIX}"
    paginator = _s3().get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=Config.S3_BUCKET, Prefix=catalog_prefix):
        for obj in page.get("Contents", []):
            key = obj.get("Key", "")
            if not key.endswith(".md"):
                continue
            if key.endswith("recipes-catalog-summary.md"):
                continue

            slug = _slug_from_key(key)
            if slug in foreign_slugs:
                continue
            if manifest_slugs and slug not in manifest_slugs:
                continue
            # User saved a copy of this catalog recipe — show only their RDS entry.
            if slug in rds_meta:
                continue

            rds = rds_meta.get(slug, {})
            manifest_item = manifest.get(slug, {})

            title = (
                rds.get("title")
                or manifest_item.get("title")
                or _title_from_slug(slug)
            )
            tags = rds.get("tags") or manifest_item.get("tags") or []
            if not rds.get("tags") and not manifest_item.get("tags"):
                header = _fetch_md_header(key)
                if header:
                    parsed_title = parse_title(header)
                    if parsed_title and not manifest_item.get("title"):
                        title = parsed_title
                    tags = parse_tags(header)

            image_state = recipe_images.resolve_image(
                slug, manifest_item, image_rows.get(slug)
            )
            source = "catalog"
            indexed_keys.add(key)
            entries.append(
                {
                    "s3_key": key,
                    "slug": slug,
                    "title": title,
                    "source": source,
                    "description": rds.get("description", ""),
                    "tags": tags,
                    "image_url": image_state["image_url"],
                    "image_status": image_state["status"],
                    "author_username": rds.get("author_username"),
                    "created_at": rds.get("created_at"),
                    "last_modified": obj.get("LastModified"),
                }
            )

    for slug, rds in rds_meta.items():
        s3_key = rds.get("s3_key") or catalog_s3_key(slug)
        if s3_key in indexed_keys:
            continue
        manifest_item = manifest.get(slug, {})
        image_state = recipe_images.resolve_image(
            slug, manifest_item, image_rows.get(slug)
        )
        entries.append(
            {
                "s3_key": s3_key,
                "slug": slug,
                "title": rds.get("title") or _title_from_slug(slug),
                "source": "user",
                "description": rds.get("description", ""),
                "tags": rds.get("tags", []),
                "image_url": image_state["image_url"],
                "image_status": image_state["status"],
                "author_username": rds.get("author_username"),
                "created_at": rds.get("created_at"),
                "last_modified": None,
            }
        )

    entries.sort(key=lambda item: item["title"].lower())
    _index_cache[cache_key] = (entries, now + _INDEX_TTL_SECONDS)
    return entries


def _sort_key(item: RecipeIndexEntry, sort: str):
    ts = item.get("created_at") or item.get("last_modified")
    if ts is not None and hasattr(ts, "timestamp"):
        stamp = ts.timestamp()
    else:
        stamp = 0.0

    if sort in ("latest", "oldest"):
        return stamp
    return item["title"].lower()


def _parse_query(query: str) -> tuple[str, str | list[str]]:
    """Return ('text', lowered query) or ('tags', list of tag terms)."""
    raw = (query or "").strip()
    if raw.startswith("#"):
        tag_text = raw[1:].strip()
        if not tag_text:
            return ("text", "")
        tags = [part.strip().lower() for part in tag_text.split(",") if part.strip()]
        return ("tags", tags)
    return ("text", raw.lower())


def _matches_query(item: RecipeIndexEntry, parsed: tuple[str, str | list[str]]) -> bool:
    mode, value = parsed
    if mode == "tags":
        tags = value if isinstance(value, list) else []
        if not tags:
            return True
        item_tags = [str(tag).lower() for tag in item.get("tags") or []]
        return any(
            any(term in recipe_tag for recipe_tag in item_tags) for term in tags
        )

    q = value if isinstance(value, str) else ""
    if not q:
        return True
    if q in item["title"].lower() or q in item["slug"].lower():
        return True
    return any(q in str(tag).lower() for tag in item.get("tags") or [])


def _sort_entries(
    entries: list[RecipeIndexEntry], sort: str
) -> list[RecipeIndexEntry]:
    reverse = sort in ("za", "latest")
    return sorted(
        entries, key=lambda item: _sort_key(item, sort), reverse=reverse
    )


def search_recipes(
    conn,
    query: str = "",
    limit: int = 50,
    offset: int = 0,
    sort: str = "latest",
    favorite_slugs: set[str] | None = None,
    user_id: str | None = None,
) -> tuple[list[RecipeIndexEntry], list[RecipeIndexEntry], int]:
    """Search and sort recipes.

    When favorite_slugs is provided, returns (all_matching_favorites, paginated_others,
    total_other_count). Favorites are never paginated — only non-favorite matches are.

    When favorite_slugs is None, returns ([], paginated_all, total_all) for backward
    compatibility.
    """
    index = build_recipe_index(conn, user_id=user_id)
    parsed = _parse_query(query)

    if parsed[0] == "tags" or (parsed[0] == "text" and parsed[1]):
        filtered = [item for item in index if _matches_query(item, parsed)]
    else:
        filtered = list(index)

    filtered = _sort_entries(filtered, sort)

    if favorite_slugs is None:
        total = len(filtered)
        page = filtered[offset : offset + limit]
        return [], page, total

    favorites = [item for item in filtered if item["slug"] in favorite_slugs]
    others = [item for item in filtered if item["slug"] not in favorite_slugs]
    total_others = len(others)
    page = others[offset : offset + limit]
    return favorites, page, total_others


def is_valid_recipe_key(s3_key: str) -> bool:
    prefix = Config.S3_RECIPES_PREFIX
    if not s3_key or ".." in s3_key or not s3_key.endswith(".md"):
        return False
    if not s3_key.startswith(prefix):
        return False
    relative = s3_key[len(prefix) :]
    return relative.startswith(_CATALOG_PREFIX) or relative.startswith(_USER_PREFIX)


def get_recipe_content(s3_key: str) -> str:
    if not is_valid_recipe_key(s3_key):
        raise ValueError("Invalid recipe key")
    response = _s3().get_object(Bucket=Config.S3_BUCKET, Key=s3_key)
    return response["Body"].read().decode("utf-8", errors="replace")


def get_recipe_preview(s3_key: str, max_chars: int = 500) -> dict:
    content = get_recipe_content(s3_key)
    title = title_for_key(s3_key, content)
    preview = content[:max_chars]
    if len(content) > max_chars:
        preview += "..."
    return {"s3_key": s3_key, "title": title, "preview": preview}


def title_for_key(s3_key: str, md_text: str = "") -> str:
    title = parse_title(md_text)
    if title:
        return title
    slug = _slug_from_key(s3_key)
    return _title_from_slug(slug)


def build_email_context(md_text: str, personal_note: str = "") -> str:
    body = md_text.strip()
    note = (personal_note or "").strip()
    if note:
        return f"{body}\n\n---\nPersonal note from sender:\n{note}"
    return body
