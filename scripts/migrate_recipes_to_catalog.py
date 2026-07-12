#!/usr/bin/env python3
"""
Move user-created recipes from legacy recipes/{slug}.md to recipes/catalog/{slug}.md.

Run from the project root:
    python3 scripts/migrate_recipes_to_catalog.py
    python3 scripts/migrate_recipes_to_catalog.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import boto3

from config import Config
from db import _checkout, _get_pool
from services import s3_recipes


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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy legacy user recipe objects into recipes/catalog/."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions without copying or updating RDS",
    )
    args = parser.parse_args()

    if not Config.S3_BUCKET:
        print("ERROR: S3_BUCKET_NAME is not set.")
        sys.exit(1)

    catalog_prefix = f"{Config.S3_RECIPES_PREFIX}catalog/"
    s3 = boto3.client("s3", region_name=Config.AWS_REGION)
    conn = _checkout()
    migrated = 0

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT slug, s3_key, title, description, ingredients, steps, notes, tags
                FROM recipes
                """
            )
            rows = [dict(r) for r in cur.fetchall()]

        for row in rows:
            slug = row["slug"]
            old_key = row.get("s3_key") or f"{Config.S3_RECIPES_PREFIX}{slug}.md"
            new_key = s3_recipes.catalog_s3_key(slug)

            if old_key == new_key:
                continue

            if args.dry_run:
                print(f"[dry-run] {old_key} -> {new_key}")
                migrated += 1
                continue

            copied = False
            try:
                s3.copy_object(
                    Bucket=Config.S3_BUCKET,
                    CopySource={"Bucket": Config.S3_BUCKET, "Key": old_key},
                    Key=new_key,
                    ContentType="text/markdown",
                )
                copied = True
            except Exception:
                ingredients = row.get("ingredients") or []
                steps = row.get("steps") or []
                tags = row.get("tags") or []
                if isinstance(ingredients, str):
                    ingredients = json.loads(ingredients)
                if isinstance(steps, str):
                    steps = json.loads(steps)
                if isinstance(tags, str):
                    tags = json.loads(tags)
                md_content = _recipe_to_md(
                    row.get("title") or slug,
                    row.get("description") or "",
                    ingredients,
                    steps,
                    row.get("notes") or "",
                    tags,
                )
                s3.put_object(
                    Bucket=Config.S3_BUCKET,
                    Key=new_key,
                    Body=md_content.encode("utf-8"),
                    ContentType="text/markdown",
                )
                copied = True

            if copied:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE recipes SET s3_key = %s WHERE slug = %s",
                        (new_key, slug),
                    )
                if old_key != new_key:
                    try:
                        s3.delete_object(Bucket=Config.S3_BUCKET, Key=old_key)
                    except Exception:
                        pass
                migrated += 1
                print(f"Migrated {slug}")

        if not args.dry_run:
            conn.commit()
            s3_recipes.invalidate_index_cache()
    finally:
        _get_pool().putconn(conn)

    print(f"Done. Migrated {migrated} recipe(s) to {catalog_prefix}")


if __name__ == "__main__":
    main()
