#!/usr/bin/env python3
"""
Ensure the Bedrock Chef AI agent can invoke the action-group Lambda and the Lambda
can read S3 catalog metadata (required for SuggestDishForTimeAndWeather titles).

Does NOT modify lmbda.py — only IAM, Lambda resource policy, action group wiring,
and prepare_agent.

Usage:
  python3 scripts/ensure_agent_lambda_access.py
  python3 scripts/ensure_agent_lambda_access.py --agent-id B9KMGV3ZAV
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_AGENT_ID = "B9KMGV3ZAV"
DEFAULT_REGION = "us-east-1"
DEFAULT_ACTION_GROUP_ID = "SJWZQLZ5EL"
DEFAULT_LAMBDA_NAME = "action_group_quick_start_38hbb-hwq2r"


def _load_env_defaults() -> dict[str, str]:
    env_path = ROOT / ".env"
    out: dict[str, str] = {}
    if not env_path.exists():
        return out
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip()
    return out


def _wait_prepared(client, agent_id: str, timeout_s: int = 180) -> None:
    for _ in range(timeout_s // 5):
        status = client.get_agent(agentId=agent_id)["agent"]["agentStatus"]
        if status == "PREPARED":
            return
        if status == "FAILED":
            agent = client.get_agent(agentId=agent_id)["agent"]
            raise RuntimeError(agent.get("failureReasons"))
        time.sleep(5)
    raise TimeoutError(f"Agent {agent_id} not PREPARED after {timeout_s}s")


def ensure_agent_lambda_invoke(
    iam,
    *,
    agent_role_name: str,
    lambda_arn: str,
    agent_id: str,
    region: str,
    account_id: str,
) -> None:
    policy_name = "BedrockAgentInvokeActionLambda"
    document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "InvokeActionGroupLambda",
                "Effect": "Allow",
                "Action": "lambda:InvokeFunction",
                "Resource": lambda_arn,
            }
        ],
    }
    iam.put_role_policy(
        RoleName=agent_role_name,
        PolicyName=policy_name,
        PolicyDocument=json.dumps(document),
    )
    print(f"  Agent role {agent_role_name}: attached {policy_name}")


def ensure_lambda_resource_policy(
    lam,
    *,
    lambda_name: str,
    agent_id: str,
    region: str,
    account_id: str,
) -> None:
    agent_arn = f"arn:aws:bedrock:{region}:{account_id}:agent/{agent_id}"
    statement = {
        "Sid": "BedrockAgentInvokeActionLambda",
        "Effect": "Allow",
        "Principal": {"Service": "bedrock.amazonaws.com"},
        "Action": "lambda:InvokeFunction",
        "Resource": f"arn:aws:lambda:{region}:{account_id}:function:{lambda_name}",
        "Condition": {
            "ArnLike": {"AWS:SourceArn": agent_arn},
        },
    }
    policy = {"Version": "2012-10-17", "Id": "default", "Statement": [statement]}
    lam.add_permission(
        FunctionName=lambda_name,
        StatementId="BedrockAgentInvokeActionLambda",
        Action="lambda:InvokeFunction",
        Principal="bedrock.amazonaws.com",
        SourceArn=agent_arn,
    )
    print(f"  Lambda {lambda_name}: bedrock invoke permission (SourceArn={agent_id})")


def ensure_lambda_s3_read(
    iam,
    *,
    lambda_role_name: str,
    bucket: str,
    prefix: str,
) -> None:
    prefix = prefix.rstrip("/") + "/*"
    document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ReadRecipeCatalogForTool",
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": f"arn:aws:s3:::{bucket}/{prefix}",
            }
        ],
    }
    iam.put_role_policy(
        RoleName=lambda_role_name,
        PolicyName="ReadRecipeCatalogForTools",
        PolicyDocument=json.dumps(document),
    )
    print(f"  Lambda role {lambda_role_name}: s3:GetObject on s3://{bucket}/{prefix}")


def verify_action_group(
    client,
    *,
    agent_id: str,
    expected_lambda_arn: str,
    action_group_id: str,
) -> None:
    ag = client.get_agent_action_group(
        agentId=agent_id,
        agentVersion="DRAFT",
        actionGroupId=action_group_id,
    )["agentActionGroup"]
    linked = (ag.get("actionGroupExecutor") or {}).get("lambda", "")
    state = ag.get("actionGroupState")
    functions = [
        fn.get("name")
        for fn in (ag.get("functionSchema") or {}).get("functions") or []
    ]
    print(f"  Action group {ag.get('actionGroupName')}: state={state}, lambda={linked}")
    print(f"  Tools: {functions}")
    if linked != expected_lambda_arn:
        raise RuntimeError(
            f"DRAFT action group points to {linked!r}, expected {expected_lambda_arn!r}. "
            "Update in Bedrock console or run create_chef_agent.py."
        )
    if state != "ENABLED":
        raise RuntimeError(f"Action group state is {state}, expected ENABLED")
    required = {"SuggestDishForTimeAndWeather", "ShareRecipeWithBuddy"}
    missing = required - set(functions)
    if missing:
        raise RuntimeError(f"Action group missing tools: {sorted(missing)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Wire Bedrock agent to action Lambda")
    parser.add_argument("--agent-id", default=DEFAULT_AGENT_ID)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--action-group-id", default=DEFAULT_ACTION_GROUP_ID)
    parser.add_argument("--lambda-name", default=DEFAULT_LAMBDA_NAME)
    parser.add_argument("--skip-prepare", action="store_true")
    args = parser.parse_args()

    env = _load_env_defaults()
    bucket = env.get("S3_BUCKET_NAME", "us-bucket-giora")
    prefix = env.get("S3_RECIPES_PREFIX", "recipes/")

    sts = boto3.client("sts", region_name=args.region)
    account_id = sts.get_caller_identity()["Account"]
    lambda_arn = (
        f"arn:aws:lambda:{args.region}:{account_id}:function:{args.lambda_name}"
    )

    agent_client = boto3.client("bedrock-agent", region_name=args.region)
    lam_client = boto3.client("lambda", region_name=args.region)
    iam_client = boto3.client("iam")

    agent = agent_client.get_agent(agentId=args.agent_id)["agent"]
    agent_role_arn = agent["agentResourceRoleArn"]
    agent_role_name = agent_role_arn.split("/")[-1]

    fn_cfg = lam_client.get_function_configuration(FunctionName=args.lambda_name)
    lambda_role_name = fn_cfg["Role"].split("/")[-1]

    print(f"Agent {args.agent_id} ({agent['agentName']})")
    print(f"Action Lambda {args.lambda_name}")

    print("\n1. Verify DRAFT action group …")
    verify_action_group(
        agent_client,
        agent_id=args.agent_id,
        expected_lambda_arn=lambda_arn,
        action_group_id=args.action_group_id,
    )

    print("\n2. Agent execution role → Lambda invoke …")
    ensure_agent_lambda_invoke(
        iam_client,
        agent_role_name=agent_role_name,
        lambda_arn=lambda_arn,
        agent_id=args.agent_id,
        region=args.region,
        account_id=account_id,
    )

    print("\n3. Lambda resource policy for Bedrock agent …")
    try:
        ensure_lambda_resource_policy(
            lam_client,
            lambda_name=args.lambda_name,
            agent_id=args.agent_id,
            region=args.region,
            account_id=account_id,
        )
    except lam_client.exceptions.ResourceConflictException:
        print("  Lambda permission already present — OK")

    print("\n4. Lambda execution role → S3 catalog read …")
    ensure_lambda_s3_read(
        iam_client,
        lambda_role_name=lambda_role_name,
        bucket=bucket,
        prefix=prefix,
    )

    if not args.skip_prepare:
        print("\n5. Prepare agent (TSTALIASID → DRAFT) …")
        agent_client.prepare_agent(agentId=args.agent_id)
        _wait_prepared(agent_client, args.agent_id)
        print("  Agent PREPARED.")

    print("\nDone. Test: ask Chef AI 'What should I cook in Paris right now?'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
