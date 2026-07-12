#!/usr/bin/env python3
"""Raise Bedrock Chef AI agent output token limits (ORCHESTRATION was 1024 — too short for full recipes)."""

from __future__ import annotations

import argparse
import sys
import time

import boto3

DEFAULT_AGENT_ID = "B9KMGV3ZAV"
DEFAULT_REGION = "us-east-1"
DEFAULT_MAX_LENGTH = 8192
PROMPT_TYPES = ("ORCHESTRATION",)


def _wait_prepared(client, agent_id: str, timeout_s: int = 180) -> None:
    for _ in range(timeout_s // 5):
        status = client.get_agent(agentId=agent_id)["agent"]["agentStatus"]
        if status == "PREPARED":
            return
        if status == "FAILED":
            raise RuntimeError(client.get_agent(agentId=agent_id)["agent"].get("failureReasons"))
        time.sleep(5)
    raise TimeoutError(f"Agent {agent_id} not PREPARED after {timeout_s}s")


def update_max_tokens(agent_id: str, region: str, max_length: int) -> None:
    client = boto3.client("bedrock-agent", region_name=region)
    agent = client.get_agent(agentId=agent_id)["agent"]

    poc = dict(agent.get("promptOverrideConfiguration") or {})
    configs = [dict(cfg) for cfg in poc.get("promptConfigurations") or []]

    updated: list[str] = []
    cleaned: list[dict] = []
    for cfg in configs:
        ptype = cfg.get("promptType")
        mode = cfg.get("promptCreationMode", "DEFAULT")

        if ptype == "ORCHESTRATION":
            ic = dict(cfg.get("inferenceConfiguration") or {})
            old = ic.get("maximumLength")
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
            updated.append(f"ORCHESTRATION: {old} -> {max_length}")
            continue

        # DEFAULT prompts cannot include template/inference overrides in UpdateAgent.
        if mode == "DEFAULT":
            cleaned.append(
                {
                    "promptType": ptype,
                    "promptCreationMode": "DEFAULT",
                    "parserMode": cfg.get("parserMode", "DEFAULT"),
                }
            )
        else:
            cleaned.append(cfg)

    if not updated:
        raise RuntimeError("ORCHESTRATION prompt config not found on agent")

    poc["promptConfigurations"] = cleaned

    kwargs = {
        "agentId": agent_id,
        "agentName": agent["agentName"],
        "instruction": agent["instruction"],
        "foundationModel": agent["foundationModel"],
        "agentResourceRoleArn": agent["agentResourceRoleArn"],
        "promptOverrideConfiguration": poc,
    }
    if agent.get("description"):
        kwargs["description"] = agent["description"]
    if agent.get("idleSessionTTLInSeconds") is not None:
        kwargs["idleSessionTTLInSeconds"] = agent["idleSessionTTLInSeconds"]
    if agent.get("orchestrationType"):
        kwargs["orchestrationType"] = agent["orchestrationType"]
    if agent.get("agentCollaboration"):
        kwargs["agentCollaboration"] = agent["agentCollaboration"]
    if agent.get("guardrailConfiguration"):
        kwargs["guardrailConfiguration"] = agent["guardrailConfiguration"]
    if agent.get("memoryConfiguration"):
        kwargs["memoryConfiguration"] = agent["memoryConfiguration"]

    print(f"Updating agent {agent_id}...")
    for line in updated:
        print(f"  {line}")
    client.update_agent(**kwargs)
    print("Preparing agent (applies to DRAFT / TSTALIASID)...")
    client.prepare_agent(agentId=agent_id)
    _wait_prepared(client, agent_id)
    print("Done.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Increase Bedrock agent maximumLength")
    parser.add_argument("--agent-id", default=DEFAULT_AGENT_ID)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--max-length", type=int, default=DEFAULT_MAX_LENGTH)
    args = parser.parse_args()
    update_max_tokens(args.agent_id, args.region, args.max_length)
    return 0


if __name__ == "__main__":
    sys.exit(main())
