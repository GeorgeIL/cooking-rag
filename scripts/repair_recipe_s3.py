#!/usr/bin/env python3
"""
Reconcile every RDS recipe with its S3 markdown so the Bedrock Knowledge Base can
index it per user.

Why this is needed:
- Some user recipes were saved with an older code path that wrote markdown to the
  shared ``recipes/catalog/`` prefix (breaks user separation) or whose markdown went
  missing entirely (RDS row + image exist, but the .md was never persisted). Either
  way the KB cannot return them for the owning user.

What it does, for each row in ``recipes``:
  1. Rebuilds the markdown from the RDS columns (title/description/ingredients/...).
  2. Writes it to the canonical per-user key ``recipes/users/{author_id}/{slug}.md``.
  3. Deletes a stale ``recipes/catalog/{slug}.md`` copy *only* when that slug is not a
     real catalog recipe (not in the catalog manifest) — never touches the ~catalog.
  4. Updates ``recipes.s3_key`` to the canonical key.

Finally it invalidates the in-process index cache and triggers a KB ingestion sync.

Run inside the container (has IAM creds + DB access):
  docker exec cooking-rag python scripts/repair_recipe_s3.py
"""

from __future__ import annotations

import json

import boto3
import psycopg2.extras

from config import Config
from db import _get_pool
from rag import engine as rag
from services import s3_recipes


def _as_list(value) -> list:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return [value] if value.strip() else []
    return value or []


def _recipe_to_md(title, description, ingredients, steps, notes, tags) -> str:
    lines = [f"# {title}\n"]
    if description:
        lines += [f"## Description\n\n{description}\n"]
    if tags:
        lines += [f"**Tags:** {', '.join(t for t in tags if t)}\n"]
    if ingredients:
        lines += ["## Ingredients\n"]
        lines += [f"- {i}" for i in ingredients if i]
        lines += [""]
    if steps:
        lines += ["## Steps\n"]
        lines += [f"{n}. {s}" for n, s in enumerate(steps, 1) if s]
        lines += [""]
    if notes:
        lines += [f"## Notes\n\n{notes}\n"]
    return "\n".join(lines)


def repair() -> None:
    s3 = boto3.client("s3", region_name=Config.AWS_REGION)
    bucket = Config.S3_BUCKET
    prefix = Config.S3_RECIPES_PREFIX
    catalog_prefix = f"{prefix}catalog/"

    manifest = s3_recipes._load_catalog_manifest()
    manifest_slugs = set(manifest.keys())

    conn = _get_pool().getconn()
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id::text AS id, slug, title, description, ingredients, "
                "steps, notes, tags, author_id::text AS author_id, s3_key FROM recipes"
            )
            rows = cur.fetchall()

        uploaded = 0
        moved = 0
        deleted = 0
        for r in rows:
            slug = r["slug"]
            author_id = r["author_id"]
            correct_key = f"{prefix}users/{author_id}/{slug}.md"
            content = _recipe_to_md(
                r["title"],
                r.get("description") or "",
                _as_list(r["ingredients"]),
                _as_list(r["steps"]),
                r.get("notes") or "",
                _as_list(r["tags"]),
            )

            s3.put_object(
                Bucket=bucket,
                Key=correct_key,
                Body=content.encode("utf-8"),
                ContentType="text/markdown",
            )
            uploaded += 1

            old_key = r["s3_key"] or ""
            if (
                old_key
                and old_key != correct_key
                and old_key.startswith(catalog_prefix)
                and slug not in manifest_slugs
            ):
                try:
                    s3.delete_object(Bucket=bucket, Key=old_key)
                    deleted += 1
                except Exception as exc:  # noqa: BLE001
                    print(f"  ! failed to delete stale {old_key}: {exc}")

            if old_key != correct_key:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE recipes SET s3_key = %s WHERE id = %s",
                        (correct_key, r["id"]),
                    )
                moved += 1
                print(f"  moved {slug}: {old_key or '(none)'} -> {correct_key}")
            else:
                print(f"  ok    {slug}")

        conn.commit()
    finally:
        _get_pool().putconn(conn)

    s3_recipes.invalidate_index_cache()
    print(
        f"\nReconciled {uploaded} recipe(s): {moved} key(s) updated, "
        f"{deleted} stale catalog file(s) removed."
    )

    print("Triggering Knowledge Base sync...")
    result = rag.sync_knowledge_base()
    print(f"  {result.get('message')}")
    if result.get("error"):
        print(f"  error: {result['error']}")


if __name__ == "__main__":
    repair()
