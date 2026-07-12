#!/usr/bin/env python3
"""
Point the app at the Bedrock DRAFT alias (TSTALIASID).

Why: The AWS console "Test" button uses TSTALIASID → DRAFT, which has the current
tools (SuggestDishForTimeAndWeather, ShareRecipeWithBuddy) and Knowledge Base.
Custom prod aliases (e.g. rag_prod2 / GL2MCCRYP2) route to old *prepared* versions
that only had GetTime/GetWeather and no KB — hence recipe questions fail locally
while console tests pass.

Custom aliases cannot be repointed to DRAFT; only TSTALIASID can.

Usage:
  python3 scripts/use_draft_agent_alias.py
  python3 scripts/use_draft_agent_alias.py --write-env
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRAFT_ALIAS = "TSTALIASID"
DEFAULT_AGENT = "B9KMGV3ZAV"


def _patch_env(path: Path, agent_id: str, alias_id: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if line.startswith("BEDROCK_AGENT_ID="):
            out.append(f"BEDROCK_AGENT_ID={agent_id}")
            seen.add("BEDROCK_AGENT_ID")
        elif line.startswith("BEDROCK_AGENT_ALIAS_ID="):
            out.append(f"BEDROCK_AGENT_ALIAS_ID={alias_id}")
            seen.add("BEDROCK_AGENT_ALIAS_ID")
        else:
            out.append(line)
    if "BEDROCK_AGENT_ID" not in seen:
        out.append(f"BEDROCK_AGENT_ID={agent_id}")
    if "BEDROCK_AGENT_ALIAS_ID" not in seen:
        out.append(f"BEDROCK_AGENT_ALIAS_ID={alias_id}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Use Bedrock TSTALIASID (DRAFT) for Chef AI")
    parser.add_argument("--agent-id", default=DEFAULT_AGENT)
    parser.add_argument("--write-env", action="store_true", help="Update .env and env.ec2")
    args = parser.parse_args()

    print("Set these in your environment (then restart Flask / redeploy EC2):")
    print(f"  BEDROCK_AGENT_ID={args.agent_id}")
    print(f"  BEDROCK_AGENT_ALIAS_ID={DRAFT_ALIAS}")

    if args.write_env:
        for name in (".env", "env.ec2"):
            path = ROOT / name
            if path.exists():
                _patch_env(path, args.agent_id, DRAFT_ALIAS)
                print(f"  updated {path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
