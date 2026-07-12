#!/usr/bin/env python3
"""
One-time script: generate a Markdown summary of the recipes.csv catalog using
pandas, then upload it to S3 so the Bedrock Knowledge Base can index it.

Run from the project root:
    python scripts/generate_csv_summary.py

Requires AWS credentials and S3_BUCKET_NAME / S3_RECIPES_PREFIX set in the
environment (or .env loaded automatically via python-dotenv).
"""

import os
import sys
from pathlib import Path

# Allow imports from the project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import boto3
import pandas as pd

S3_BUCKET = os.environ["S3_BUCKET_NAME"]
S3_PREFIX = os.environ.get("S3_RECIPES_PREFIX", "recipes/")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "recipes.csv"
DEST_KEY = f"{S3_PREFIX}recipes-catalog-summary.md"


def main() -> None:
    if not CSV_PATH.exists():
        print(f"ERROR: CSV not found at {CSV_PATH}")
        sys.exit(1)

    df = pd.read_csv(CSV_PATH)
    total = len(df)

    lines: list[str] = [
        "# Recipe Catalog Summary",
        "",
        f"This knowledge base contains **{total} recipes** in total.",
        "",
    ]

    # ── Category breakdown ────────────────────────────────────────────────────
    if "cuisine_path" in df.columns:
        lines += ["## Recipes by Category", ""]
        # Extract the top-level category (first segment of the path)
        df["_category"] = (
            df["cuisine_path"].astype(str).str.split("/").str[0].str.strip()
        )
        counts = df["_category"].value_counts().head(20)
        for cat, cnt in counts.items():
            lines.append(f"- **{cat}**: {cnt} recipes")
        lines.append("")

    # ── Rating statistics ─────────────────────────────────────────────────────
    rating_col = next((c for c in df.columns if "rating" in c.lower()), None)
    if rating_col:
        valid = df[rating_col].dropna()
        if len(valid):
            lines += [
                "## Rating Statistics",
                "",
                f"- Average rating: **{valid.mean():.2f}**",
                f"- Highest rating: **{valid.max():.2f}**",
                f"- Lowest rating: **{valid.min():.2f}**",
                f"- Recipes with ratings: **{len(valid)}**",
                "",
            ]

    # ── Top 10 highest-rated recipes ─────────────────────────────────────────
    title_col = next(
        (c for c in df.columns if "title" in c.lower() or "name" in c.lower()), None
    )
    if rating_col and title_col:
        top10 = (
            df[[title_col, rating_col]]
            .dropna(subset=[rating_col])
            .sort_values(rating_col, ascending=False)
            .head(10)
        )
        lines += ["## Top 10 Highest-Rated Recipes", ""]
        for i, (_, row) in enumerate(top10.iterrows(), 1):
            lines.append(f"{i}. {row[title_col]} ({row[rating_col]:.1f})")
        lines.append("")

    lines += [
        "---",
        "_This summary is auto-generated from the recipe CSV catalog._",
    ]

    md_content = "\n".join(lines)

    # ── Upload to S3 ──────────────────────────────────────────────────────────
    s3 = boto3.client("s3", region_name=AWS_REGION)
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=DEST_KEY,
        Body=md_content.encode("utf-8"),
        ContentType="text/markdown",
    )
    print(f"Uploaded catalog summary to s3://{S3_BUCKET}/{DEST_KEY}")
    print(f"Total recipes in catalog: {total}")


if __name__ == "__main__":
    main()
