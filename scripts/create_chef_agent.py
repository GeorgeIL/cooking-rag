#!/usr/bin/env python3
"""
Create a fresh Bedrock Chef AI agent from the working DRAFT config and a new prod alias.

Use when the production alias still routes to an old prepared version (GetTime/GetWeather)
and prepare-agent does not create a new version number.

Usage:
  python3 scripts/create_chef_agent.py
  python3 scripts/create_chef_agent.py --source-agent B9KMGV3ZAV --alias-name chef-ai-prod
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent.parent
REGION = "us-east-1"
DEFAULT_SOURCE = "B9KMGV3ZAV"
DEFAULT_KB = "DPIPRZU7PA"
DEFAULT_LAMBDA = (
    "arn:aws:lambda:us-east-1:827565901338:function:action_group_quick_start_38hbb-hwq2r"
)
SOURCE_ACTION_GROUP_ID = "SJWZQLZ5EL"


def _wait_agent_ready(client, agent_id: str, timeout_s: int = 180) -> None:
    """Wait until the agent is editable (NOT_PREPARED or PREPARED), not still CREATING."""
    for _ in range(timeout_s // 5):
        status = client.get_agent(agentId=agent_id)["agent"]["agentStatus"]
        if status in ("NOT_PREPARED", "PREPARED"):
            return
        if status == "FAILED":
            agent = client.get_agent(agentId=agent_id)["agent"]
            raise RuntimeError(f"Agent {agent_id} failed: {agent.get('failureReasons')}")
        time.sleep(5)
    raise TimeoutError(f"Agent {agent_id} not ready after {timeout_s}s")


def _wait_agent_prepared(client, agent_id: str, timeout_s: int = 180) -> None:
    for _ in range(timeout_s // 5):
        status = client.get_agent(agentId=agent_id)["agent"]["agentStatus"]
        if status == "PREPARED":
            return
        if status == "FAILED":
            agent = client.get_agent(agentId=agent_id)["agent"]
            raise RuntimeError(f"Agent {agent_id} failed: {agent.get('failureReasons')}")
        time.sleep(5)
    raise TimeoutError(f"Agent {agent_id} not PREPARED after {timeout_s}s")


def _latest_numbered_version(client, agent_id: str) -> str:
    versions = client.list_agent_versions(agentId=agent_id)["agentVersionSummaries"]
    nums = [v for v in versions if v["agentVersion"].isdigit()]
    if not nums:
        raise RuntimeError("No numbered agent version after prepare")
    return max(nums, key=lambda v: int(v["agentVersion"]))["agentVersion"]


def _load_instruction(client, source_agent_id: str) -> str:
    instructions_path = ROOT / "docs" / "bedrock_agent_instructions.md"
    if instructions_path.exists():
        text = instructions_path.read_text(encoding="utf-8")
        if "---" in text:
            text = text.split("---", 1)[1].strip()
        if len(text) > 500:
            return text
    draft = client.get_agent(agentId=source_agent_id)["agent"]
    return draft["instruction"]


def create_fresh_agent(
    *,
    source_agent_id: str,
    agent_name: str,
    alias_name: str,
    kb_id: str,
    lambda_arn: str,
) -> dict[str, str]:
    client = boto3.client("bedrock-agent", region_name=REGION)
    source = client.get_agent(agentId=source_agent_id)["agent"]
    instruction = _load_instruction(client, source_agent_id)
    role_arn = source["agentResourceRoleArn"]
    model = source["foundationModel"]

    ag_source = client.get_agent_action_group(
        agentId=source_agent_id,
        agentVersion="DRAFT",
        actionGroupId=SOURCE_ACTION_GROUP_ID,
    )["agentActionGroup"]
    function_schema = ag_source["functionSchema"]

    print(f"Creating agent {agent_name!r}...")
    created = client.create_agent(
        agentName=agent_name,
        foundationModel=model,
        instruction=instruction,
        agentResourceRoleArn=role_arn,
        idleSessionTTLInSeconds=600,
    )["agent"]
    new_agent_id = created["agentId"]
    print(f"  agentId={new_agent_id}")

    _wait_agent_ready(client, new_agent_id)

    print("Creating action group...")
    client.create_agent_action_group(
        agentId=new_agent_id,
        agentVersion="DRAFT",
        actionGroupName="chef_ai_tools",
        actionGroupExecutor={"lambda": lambda_arn},
        functionSchema=function_schema,
        actionGroupState="ENABLED",
    )

    print("Associating knowledge base...")
    client.associate_agent_knowledge_base(
        agentId=new_agent_id,
        agentVersion="DRAFT",
        knowledgeBaseId=kb_id,
        description=(
            "Cookbook recipes. Use for recipe Q&A. Use action tools for "
            "weather/location suggestions and buddy email."
        ),
        knowledgeBaseState="ENABLED",
    )

    print("Preparing agent...")
    client.prepare_agent(agentId=new_agent_id)
    _wait_agent_prepared(client, new_agent_id)

    version = ""
    alias_id = "TSTALIASID"
    try:
        version = _latest_numbered_version(client, new_agent_id)
        print(f"  prepared version={version}")
        print(f"Creating alias {alias_name!r}...")
        alias = client.create_agent_alias(
            agentId=new_agent_id,
            agentAliasName=alias_name,
            routingConfiguration=[{"agentVersion": version}],
        )["agentAlias"]
        alias_id = alias["agentAliasId"]
        print(f"  agentAliasId={alias_id}")
    except RuntimeError:
        print(
            "  No numbered version after prepare (AWS only exposes DRAFT). "
            "Use TSTALIASID — the built-in alias that routes to DRAFT."
        )

    return {
        "BEDROCK_AGENT_ID": new_agent_id,
        "BEDROCK_AGENT_ALIAS_ID": alias_id,
        "agent_version": version or "DRAFT",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Create fresh Bedrock Chef AI agent")
    parser.add_argument("--source-agent", default=DEFAULT_SOURCE)
    parser.add_argument("--agent-name", default="chef-ai-cookbook")
    parser.add_argument("--alias-name", default="chef-ai-prod")
    parser.add_argument("--kb-id", default=DEFAULT_KB)
    parser.add_argument("--lambda-arn", default=DEFAULT_LAMBDA)
    parser.add_argument("--write-env", action="store_true", help="Update .env agent IDs")
    args = parser.parse_args()

    ids = create_fresh_agent(
        source_agent_id=args.source_agent,
        agent_name=args.agent_name,
        alias_name=args.alias_name,
        kb_id=args.kb_id,
        lambda_arn=args.lambda_arn,
    )

    print("\n=== Add to .env / env.ec2 ===")
    print(f"BEDROCK_AGENT_ID={ids['BEDROCK_AGENT_ID']}")
    print(f"BEDROCK_AGENT_ALIAS_ID={ids['BEDROCK_AGENT_ALIAS_ID']}")

    if args.write_env:
        env_path = ROOT / ".env"
        lines = env_path.read_text(encoding="utf-8").splitlines()
        out: list[str] = []
        seen = set()
        for line in lines:
            if line.startswith("BEDROCK_AGENT_ID="):
                out.append(f"BEDROCK_AGENT_ID={ids['BEDROCK_AGENT_ID']}")
                seen.add("BEDROCK_AGENT_ID")
            elif line.startswith("BEDROCK_AGENT_ALIAS_ID="):
                out.append(f"BEDROCK_AGENT_ALIAS_ID={ids['BEDROCK_AGENT_ALIAS_ID']}")
                seen.add("BEDROCK_AGENT_ALIAS_ID")
            else:
                out.append(line)
        if "BEDROCK_AGENT_ID" not in seen:
            out.append(f"BEDROCK_AGENT_ID={ids['BEDROCK_AGENT_ID']}")
        if "BEDROCK_AGENT_ALIAS_ID" not in seen:
            out.append(f"BEDROCK_AGENT_ALIAS_ID={ids['BEDROCK_AGENT_ALIAS_ID']}")
        env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
        print(f"\nUpdated {env_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
