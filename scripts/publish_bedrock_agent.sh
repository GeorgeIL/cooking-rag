#!/usr/bin/env bash
# Prepare the Bedrock agent DRAFT and point a production alias at the new version.
set -euo pipefail

AGENT_ID="${1:-B9KMGV3ZAV}"
ALIAS_ID="${2:-GL2MCCRYP2}"
REGION="${AWS_REGION:-us-east-1}"

echo "Preparing agent $AGENT_ID..."
aws bedrock-agent prepare-agent --agent-id "$AGENT_ID" --region "$REGION" >/dev/null

echo "Waiting for PREPARED status..."
for _ in $(seq 1 36); do
  STATUS=$(aws bedrock-agent get-agent --agent-id "$AGENT_ID" --region "$REGION" \
    --query 'agent.agentStatus' --output text)
  if [[ "$STATUS" == "PREPARED" ]]; then
    break
  fi
  if [[ "$STATUS" == "FAILED" ]]; then
    aws bedrock-agent get-agent --agent-id "$AGENT_ID" --region "$REGION" \
      --query 'agent.failureReasons' --output json
    exit 1
  fi
  sleep 5
done

LATEST=$(aws bedrock-agent list-agent-versions --agent-id "$AGENT_ID" --region "$REGION" \
  --query 'agentVersionSummaries[?agentVersion!=`DRAFT`]|[-1].agentVersion' --output text)

if [[ -z "$LATEST" || "$LATEST" == "None" ]]; then
  echo "No numbered version was created. In the Bedrock console: Agents → Prepare, then re-run this script."
  exit 1
fi

ALIAS_NAME=$(aws bedrock-agent get-agent-alias --agent-id "$AGENT_ID" --agent-alias-id "$ALIAS_ID" \
  --region "$REGION" --query 'agentAlias.agentAliasName' --output text)

echo "Updating alias $ALIAS_ID ($ALIAS_NAME) → version $LATEST"
aws bedrock-agent update-agent-alias \
  --agent-id "$AGENT_ID" \
  --agent-alias-id "$ALIAS_ID" \
  --agent-alias-name "$ALIAS_NAME" \
  --routing-configuration "agentVersion=$LATEST" \
  --region "$REGION"

echo "Done. Alias $ALIAS_ID now routes to version $LATEST."
