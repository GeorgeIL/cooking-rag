#!/usr/bin/env python3
"""
Bulk-convert recipes.csv rows into individual Markdown files and upload them to
S3 under recipes/catalog/{slug}.md so Bedrock indexes one document per recipe.

Run from the project root:
    python scripts/csv_to_catalog_md.py
    python scripts/csv_to_catalog_md.py --dry-run
    python scripts/csv_to_catalog_md.py --limit 100
    python scripts/csv_to_catalog_md.py --wipe-catalog

Requires AWS credentials and S3_BUCKET_NAME / S3_RECIPES_PREFIX in the
environment (or .env loaded automatically via python-dotenv).
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import boto3
import pandas as pd

S3_BUCKET = os.environ.get("S3_BUCKET_NAME")
S3_PREFIX = os.environ.get("S3_RECIPES_PREFIX", "recipes/")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "recipes.csv"
CATALOG_PREFIX = f"{S3_PREFIX}catalog/"
MANIFEST_KEY = f"{CATALOG_PREFIX}manifest.json"
_NUMERIC_SLUG_RE = re.compile(r"^\d+\.md$")


def _slugify(text: str) -> str:
    text = str(text).lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-") or "recipe"


def _is_unnamed_column(col: str) -> bool:
    return bool(re.match(r"^unnamed", col.strip(), re.IGNORECASE))


def _find_column(columns: list[str], *candidates: str) -> str | None:
    lowered = {c.lower(): c for c in columns}
    for candidate in candidates:
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
    for col in columns:
        if _is_unnamed_column(col):
            continue
        col_lower = col.lower()
        for candidate in candidates:
            if candidate.lower() in col_lower:
                return col
    return None


def _normalize_tags(raw_tags: list[str]) -> list[str]:
    seen: set[str] = set()
    tags: list[str] = []
    for tag in raw_tags:
        normalized = re.sub(r"\s+", " ", tag.strip()).lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            tags.append(normalized)
    return tags


def _tags_from_cuisine(cuisine_value: str) -> list[str]:
    if not cuisine_value or cuisine_value.lower() == "nan":
        return []
    parts = [
        part.strip()
        for part in re.split(r"[,/|]", cuisine_value)
        if part.strip()
    ]
    return _normalize_tags(parts)


_QUANTITY_INGREDIENT_SPLIT_RE = re.compile(
    r",\s*(?=(?:\d+(?:[./]\d+)?|\d+\s*/|\d+\s-\s*\d+|½|¼|⅓|⅔|¾|⅛|⅜|⅝|⅞)\s)",
    re.IGNORECASE,
)


def _split_ingredients(text: str) -> list[str]:
    """Split a CSV ingredient string into one item per line."""
    text = " ".join(text.split())
    if not text:
        return []
    parts = _QUANTITY_INGREDIENT_SPLIT_RE.split(text)
    if len(parts) <= 1:
        return _split_on_commas_outside_parens(text)
    return [part.strip() for part in parts if part.strip()]


def _split_on_commas_outside_parens(text: str) -> list[str]:
    """Split ingredient string on commas not inside parentheses."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            piece = "".join(buf).strip()
            if piece:
                parts.append(piece)
            buf = []
            continue
        buf.append(ch)
    piece = "".join(buf).strip()
    if piece:
        parts.append(piece)
    return parts


def _parse_ingredients(raw) -> list[str]:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return []
    if isinstance(raw, list):
        items: list[str] = []
        for item in raw:
            items.extend(_split_ingredients(str(item).strip()))
        return [part for part in items if part]
    text = str(raw).strip()
    if not text or text.lower() in {"nan", "none"}:
        return []
    if text.startswith("[") and text.endswith("]"):
        parsed = _parse_list_value(raw)
        if len(parsed) > 1:
            return parsed
        if parsed:
            return _split_ingredients(parsed[0])
    return _split_ingredients(text)


def _parse_directions(raw) -> list[str]:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return []
    text = str(raw).strip()
    if not text or text.lower() in {"nan", "none"}:
        return []

    if text.startswith("[") and text.endswith("]"):
        parsed = _parse_list_value(raw)
        if len(parsed) > 1:
            return [step for step in parsed if step]
        if parsed:
            text = parsed[0]

    steps: list[str] = []
    for chunk in re.split(r"\n+", text):
        chunk = " ".join(chunk.split())
        if not chunk:
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", chunk):
            sentence = sentence.strip()
            if sentence:
                steps.append(sentence)
    return steps


def _parse_list_value(raw) -> list[str]:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw).strip()
    if not text or text.lower() in {"nan", "none"}:
        return []

    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text.replace("'", '"'))
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except json.JSONDecodeError:
            pass
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except (SyntaxError, ValueError):
            pass

    parts = re.split(r"[\n;|]+", text)
    return [part.strip(" \"'") for part in parts if part.strip(" \"'")]


def _recipe_to_md(
    title: str,
    description: str,
    ingredients: list[str],
    steps: list[str],
    notes: str,
    tags: list[str],
) -> str:
    lines = [f"# {title}\n"]
    if description:
        lines += [f"## Description\n\n{description}\n"]
    if tags:
        lines += [f"**Tags:** {', '.join(tag for tag in tags if tag)}\n"]
    if ingredients:
        lines += ["## Ingredients\n"]
        lines += [f"- {item}" for item in ingredients if item]
        lines += [""]
    if steps:
        lines += ["## Steps\n"]
        lines += [f"{num}. {step}" for num, step in enumerate(steps, 1) if step]
        lines += [""]
    if notes:
        lines += [f"## Notes\n\n{notes}\n"]
    return "\n".join(lines)


def _build_notes(row: pd.Series, rating_col: str | None, extra_cols: dict[str, str]) -> str:
    notes: list[str] = []
    if rating_col:
        rating = row.get(rating_col)
        if rating is not None and not pd.isna(rating):
            notes.append(f"Rating: {rating}")
    for label, col in extra_cols.items():
        value = row.get(col)
        if value is not None and not pd.isna(value):
            notes.append(f"{label}: {value}")
    return "\n".join(notes)


def _normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", str(title).strip().lower())


def _unique_slug(base_slug: str, used: set[str]) -> str:
    slug = base_slug
    counter = 1
    while slug in used:
        slug = f"{base_slug}-{counter}"
        counter += 1
    used.add(slug)
    return slug


def _wipe_all_catalog_objects(s3, dry_run: bool) -> int:
    """Remove every object under the catalog prefix (full rebuild)."""
    deleted = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=CATALOG_PREFIX):
        for obj in page.get("Contents", []):
            key = obj.get("Key", "")
            if not key or key == CATALOG_PREFIX:
                continue
            if dry_run:
                print(f"[dry-run] delete {key}")
            else:
                s3.delete_object(Bucket=S3_BUCKET, Key=key)
            deleted += 1
    return deleted


def _wipe_legacy_catalog_objects(s3, dry_run: bool) -> int:
    """Remove numeric-slug catalog files and summary artifacts."""
    deleted = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=CATALOG_PREFIX):
        for obj in page.get("Contents", []):
            key = obj.get("Key", "")
            basename = key[len(CATALOG_PREFIX) :] if key.startswith(CATALOG_PREFIX) else key
            should_delete = (
                basename == "manifest.json"
                or basename.endswith("recipes-catalog-summary.md")
                or _NUMERIC_SLUG_RE.match(basename)
            )
            if not should_delete:
                continue
            if dry_run:
                print(f"[dry-run] delete {key}")
            else:
                s3.delete_object(Bucket=S3_BUCKET, Key=key)
            deleted += 1
    return deleted


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload one Markdown file per CSV recipe to S3 catalog prefix."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build files locally without uploading to S3",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Upload only the first N rows (0 = all rows)",
    )
    parser.add_argument(
        "--wipe-catalog",
        action="store_true",
        help="Delete legacy numeric-slug catalog files and old manifest before upload",
    )
    parser.add_argument(
        "--wipe-all-catalog",
        action="store_true",
        help="Delete ALL objects under recipes/catalog/ before upload (use with --half)",
    )
    parser.add_argument(
        "--half",
        action="store_true",
        help="Use every other CSV row (even indices 0,2,4…) and save data/recipes_half.csv",
    )
    parser.add_argument(
        "--half-parity",
        choices=("even", "odd"),
        default="even",
        help="With --half: even keeps rows 0,2,4…; odd keeps rows 1,3,5… (default: even)",
    )
    parser.add_argument(
        "--allow-duplicate-names",
        action="store_true",
        help="Upload duplicate recipe names (by default, only the first occurrence is kept)",
    )
    parser.add_argument(
        "--csv",
        type=str,
        default="",
        help="Path to CSV file (default: data/recipes.csv)",
    )
    args = parser.parse_args()

    csv_path = Path(args.csv) if args.csv else CSV_PATH
    if not csv_path.exists():
        print(f"ERROR: CSV not found at {csv_path}")
        print("Place your catalog file at data/recipes.csv and run again.")
        sys.exit(1)

    if not args.dry_run and not S3_BUCKET:
        print("ERROR: S3_BUCKET_NAME is not set in the environment.")
        sys.exit(1)

    df = pd.read_csv(csv_path)
    if args.half:
        if args.half_parity == "odd":
            df = df.iloc[1::2].copy()
        else:
            df = df.iloc[::2].copy()
        half_path = csv_path.parent / "recipes_half.csv"
        df.to_csv(half_path, index=False)
        print(
            f"Half dataset ({args.half_parity} indices): {len(df)} rows "
            f"(saved to {half_path})"
        )
    if args.limit and args.limit > 0:
        df = df.head(args.limit)

    columns = list(df.columns)
    title_col = _find_column(
        columns, "recipe_name", "title", "recipe_title", "name"
    )
    description_col = _find_column(columns, "description", "desc", "summary")
    ingredients_col = _find_column(
        columns,
        "ingredients",
        "cleaned_ingredients",
        "ingredient_parts",
        "ingredient_list",
    )
    steps_col = _find_column(
        columns,
        "steps",
        "instructions",
        "directions",
        "recipe_instructions",
        "method",
    )
    cuisine_col = _find_column(columns, "cuisine_path", "category", "cuisine", "tags")
    rating_col = _find_column(columns, "rating", "aggregatedrating", "score")
    notes_col = _find_column(columns, "notes", "note", "tips")
    prep_col = _find_column(columns, "prep_time", "preptime", "preparation_time")
    cook_col = _find_column(columns, "cook_time", "cooktime", "cooking_time")
    total_col = _find_column(columns, "total_time", "totaltime")
    image_col = _find_column(columns, "img_src", "image", "image_url", "photo")

    if not title_col:
        print(f"ERROR: Could not find a title/name column in CSV columns: {columns}")
        sys.exit(1)

    print(f"Using title column: {title_col}")

    s3 = (
        boto3.client("s3", region_name=AWS_REGION)
        if not args.dry_run or args.wipe_catalog or args.wipe_all_catalog
        else None
    )
    if args.wipe_all_catalog:
        if not s3:
            print("ERROR: Cannot wipe catalog without S3 access.")
            sys.exit(1)
        removed = _wipe_all_catalog_objects(s3, dry_run=args.dry_run)
        if args.dry_run:
            print(f"[dry-run] Would remove {removed} catalog object(s).")
        else:
            print(f"Removed {removed} catalog object(s).")
    elif args.wipe_catalog:
        if not s3:
            print("ERROR: Cannot wipe catalog without S3 access.")
            sys.exit(1)
        removed = _wipe_legacy_catalog_objects(s3, dry_run=args.dry_run)
        if args.dry_run:
            print(f"[dry-run] Would remove {removed} legacy catalog object(s).")
        else:
            print(f"Removed {removed} legacy catalog object(s).")

    used_slugs: set[str] = set()
    used_titles: set[str] = set()
    skip_duplicate_names = not args.allow_duplicate_names
    manifest_entries: list[dict] = []
    uploaded = 0
    skipped_duplicates = 0

    for idx, row in df.iterrows():
        title = str(row[title_col]).strip()
        if not title or title.lower() == "nan":
            continue

        title_key = _normalize_title(title)
        if skip_duplicate_names and title_key in used_titles:
            skipped_duplicates += 1
            continue
        used_titles.add(title_key)

        description = str(row.get(description_col, "")).strip() if description_col else ""
        if description.lower() == "nan":
            description = ""

        ingredients = _parse_ingredients(row.get(ingredients_col)) if ingredients_col else []
        steps = _parse_directions(row.get(steps_col)) if steps_col else []

        image_url = ""
        if image_col:
            image_url = str(row.get(image_col, "")).strip()
            if image_url.lower() in {"nan", "none"}:
                image_url = ""

        tags: list[str] = []
        if cuisine_col:
            tags = _tags_from_cuisine(str(row.get(cuisine_col, "")).strip())

        notes = str(row.get(notes_col, "")).strip() if notes_col else ""
        if notes.lower() == "nan":
            notes = ""
        extra_notes = _build_notes(
            row,
            rating_col,
            {
                k: v
                for k, v in {
                    "Prep time": prep_col,
                    "Cook time": cook_col,
                    "Total time": total_col,
                }.items()
                if v
            },
        )
        if extra_notes:
            notes = f"{notes}\n{extra_notes}".strip() if notes else extra_notes

        if not ingredients and not steps:
            continue

        base_slug = _slugify(title)
        if not base_slug:
            base_slug = f"recipe-{idx}"
        slug = _unique_slug(base_slug, used_slugs)
        md_content = _recipe_to_md(title, description, ingredients, steps, notes, tags)
        key = f"{CATALOG_PREFIX}{slug}.md"

        manifest_entries.append(
            {
                "slug": slug,
                "title": title,
                "tags": tags,
                "s3_key": key,
                "image_url": image_url,
            }
        )

        if args.dry_run:
            print(f"[dry-run] {key} — {title} ({len(md_content)} bytes)")
        elif s3:
            s3.put_object(
                Bucket=S3_BUCKET,
                Key=key,
                Body=md_content.encode("utf-8"),
                ContentType="text/markdown",
            )
        uploaded += 1
        if uploaded % 100 == 0:
            print(f"Uploaded {uploaded} recipes...")

    manifest_body = json.dumps({"recipes": manifest_entries}, indent=2)
    if args.dry_run:
        print(f"[dry-run] {MANIFEST_KEY} ({len(manifest_entries)} entries)")
    elif s3:
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=MANIFEST_KEY,
            Body=manifest_body.encode("utf-8"),
            ContentType="application/json",
        )

    print(f"Done. Prepared {uploaded} catalog recipe file(s) under {CATALOG_PREFIX}")
    if skip_duplicate_names and skipped_duplicates:
        print(f"Skipped {skipped_duplicates} duplicate recipe name(s).")
    if args.dry_run:
        print("Dry run only — no files were uploaded.")
    else:
        print(f"Wrote manifest with {len(manifest_entries)} entries to {MANIFEST_KEY}")
        print("Next step: open Chef AI and click Refresh index, or trigger sync from the app.")


if __name__ == "__main__":
    main()
