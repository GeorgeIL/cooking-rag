#!/usr/bin/env python3
"""
Push Chef AI agent config to AWS Bedrock:
  - Agent instructions (from docs/bedrock_agent_instructions.md)
  - Knowledge Base association description (Mode A vs Mode B)
  - ORCHESTRATION maximumLength (8192 for full recipes)
  - prepare_agent so TSTALIASID (DRAFT) picks up changes

Usage:
  python3 scripts/sync_agent_config.py
  python3 scripts/sync_agent_config.py --agent-id B9KMGV3ZAV --also NX0NTSGABV
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AGENT_ID = "B9KMGV3ZAV"
DEFAULT_KB_ID = "DPIPRZU7PA"
DEFAULT_REGION = "us-east-1"
DEFAULT_MAX_LENGTH = 8192
INSTRUCTIONS_PATH = ROOT / "docs" / "bedrock_agent_instructions.md"

KB_DESCRIPTION = (
    "Mode A: existing cookbook recipes only — use KB as truth, don't invent. "
    "Mode B: skip KB when user asks to create/invent a new recipe — write original. "
    "Use tools for weather meals and buddy email."
)


def _load_instruction() -> str:
    text = INSTRUCTIONS_PATH.read_text(encoding="utf-8")
    if "---" in text:
        text = text.split("---", 1)[1].strip()
    if len(text) < 200:
        raise RuntimeError(f"Instruction text too short — check {INSTRUCTIONS_PATH}")
    return text + "\n\n<!-- config v7 -->"


def _wait_prepared(client, agent_id: str, timeout_s: int = 180) -> None:
    for _ in range(timeout_s // 5):
        status = client.get_agent(agentId=agent_id)["agent"]["agentStatus"]
        if status == "PREPARED":
            return
        if status == "FAILED":
            raise RuntimeError(client.get_agent(agentId=agent_id)["agent"].get("failureReasons"))
        time.sleep(5)
    raise TimeoutError(f"Agent {agent_id} not PREPARED after {timeout_s}s")


def _apply_max_tokens(poc: dict, max_length: int) -> dict:
    configs = [dict(cfg) for cfg in poc.get("promptConfigurations") or []]
    cleaned: list[dict] = []
    for cfg in configs:
        ptype = cfg.get("promptType")
        mode = cfg.get("promptCreationMode", "DEFAULT")
        if ptype == "ORCHESTRATION":
            ic = dict(cfg.get("inferenceConfiguration") or {})
            ic["maximumLength"] = max_length
            cleaned.append(
                {
                    "promptType": "ORCHESTRATION",
                    "promptCreationMode": "OVERRIDDEN",
                    "promptState": cfg.get("promptState", "ENABLED"),
                    "basePromptTemplate": cfg["basePromptTemplate"],
                    "inferenceConfiguration": ic,
                    "parserMode": cfg.get("parserMode", "DEFAULT"),
                }
            )
        elif mode == "DEFAULT":
            cleaned.append(
                {
                    "promptType": ptype,
                    "promptCreationMode": "DEFAULT",
                    "parserMode": cfg.get("parserMode", "DEFAULT"),
                }
            )
        else:
            cleaned.append(cfg)
    poc["promptConfigurations"] = cleaned
    return poc


def sync_agent(
    client,
    agent_id: str,
    instruction: str,
    kb_id: str,
    max_length: int,
) -> None:
    agent = client.get_agent(agentId=agent_id)["agent"]
    poc = _apply_max_tokens(dict(agent.get("promptOverrideConfiguration") or {}), max_length)

    print(f"\n=== Agent {agent_id} ({agent['agentName']}) ===")
    print(f"  model: {agent['foundationModel']}")
    print(f"  instruction chars: {len(instruction)}")

    kwargs = {
        "agentId": agent_id,
        "agentName": agent["agentName"],
        "instruction": instruction,
        "foundationModel": agent["foundationModel"],
        "agentResourceRoleArn": agent["agentResourceRoleArn"],
        "promptOverrideConfiguration": poc,
    }
    for key in (
        "description",
        "idleSessionTTLInSeconds",
        "orchestrationType",
        "agentCollaboration",
        "guardrailConfiguration",
        "memoryConfiguration",
    ):
        if agent.get(key) is not None:
            kwargs[key] = agent[key]

    client.update_agent(**kwargs)

    try:
        client.associate_agent_knowledge_base(
            agentId=agent_id,
            agentVersion="DRAFT",
            knowledgeBaseId=kb_id,
            description=KB_DESCRIPTION,
            knowledgeBaseState="ENABLED",
        )
    except client.exceptions.ConflictException:
        client.update_agent_knowledge_base(
            agentId=agent_id,
            agentVersion="DRAFT",
            knowledgeBaseId=kb_id,
            description=KB_DESCRIPTION,
            knowledgeBaseState="ENABLED",
        )
    print("  KB description updated")

    print("  Preparing agent (TSTALIASID routes to DRAFT)...")
    client.prepare_agent(agentId=agent_id)
    _wait_prepared(client, agent_id)
    print("  Done.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Chef AI Bedrock agent config")
    parser.add_argument("--agent-id", default=DEFAULT_AGENT_ID)
    parser.add_argument("--also", action="append", default=[], help="Extra agent IDs")
    parser.add_argument("--kb-id", default=DEFAULT_KB_ID)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--max-length", type=int, default=DEFAULT_MAX_LENGTH)
    args = parser.parse_args()

    instruction = _load_instruction()
    client = boto3.client("bedrock-agent", region_name=args.region)

    agent_ids = [args.agent_id, *args.also]
    for aid in agent_ids:
        sync_agent(client, aid, instruction, args.kb_id, args.max_length)

    print("\nActive app config uses BEDROCK_AGENT_ID + BEDROCK_AGENT_ALIAS_ID=TSTALIASID (DRAFT).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
