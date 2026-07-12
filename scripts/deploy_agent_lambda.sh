#!/usr/bin/env bash
# Deploy lmbda.py to the Bedrock Agent action group Lambda.
# The function handler in AWS is dummy_lambda.lambda_handler — we zip lmbda.py under that name.
set -euo pipefail

FUNCTION_NAME="${1:-action_group_quick_start_38hbb-hwq2r}"
REGION="${AWS_REGION:-us-east-1}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

# Load optional env defaults from project .env (never commit secrets).
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

cp "$ROOT/lmbda.py" "$TMPDIR/dummy_lambda.py"
(cd "$TMPDIR" && zip -j agent_action.zip dummy_lambda.py)

echo "Uploading code to $FUNCTION_NAME ($REGION)..."
aws lambda update-function-code \
  --function-name "$FUNCTION_NAME" \
  --region "$REGION" \
  --zip-file "fileb://$TMPDIR/agent_action.zip" >/dev/null

echo "Updating environment..."
aws lambda update-function-configuration \
  --function-name "$FUNCTION_NAME" \
  --region "$REGION" \
  --environment "Variables={
    BEDROCK_KB_ID=${BEDROCK_KB_ID:-},
    S3_BUCKET_NAME=${S3_BUCKET_NAME:-},
    S3_RECIPES_PREFIX=${S3_RECIPES_PREFIX:-recipes/},
    METEOSOURCE_API_KEY=${METEOSOURCE_API_KEY:-},
    BUDDY_EMAIL_LAMBDA_NAME=${BUDDY_EMAIL_LAMBDA_NAME:-cooking-rag-buddy-email},
    AGENT_TOOL_SECRET=${AGENT_TOOL_SECRET:-},
    FLASK_TOOL_URL=${APP_BASE_URL:-http://127.0.0.1:5001}/chat/agent/share-recipe
  }" >/dev/null

ROLE_ARN=$(aws lambda get-function-configuration \
  --function-name "$FUNCTION_NAME" \
  --region "$REGION" \
  --query 'Role' --output text)
ROLE_NAME="${ROLE_ARN##*/}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo "Ensuring action Lambda can invoke buddy email Lambda..."
aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name InvokeBuddyEmailLambda \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [{
      \"Effect\": \"Allow\",
      \"Action\": \"lambda:InvokeFunction\",
      \"Resource\": \"arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${BUDDY_EMAIL_LAMBDA_NAME:-cooking-rag-buddy-email}\"
    }]
  }" >/dev/null

S3_BUCKET="${S3_BUCKET_NAME:-}"
S3_PREFIX="${S3_RECIPES_PREFIX:-recipes/}"
if [[ -n "$S3_BUCKET" ]]; then
  echo "Ensuring action Lambda can read recipe catalog from S3..."
  aws iam put-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-name ReadRecipeCatalogForTools \
    --policy-document "{
      \"Version\": \"2012-10-17\",
      \"Statement\": [{
        \"Sid\": \"ReadRecipeCatalogForTool\",
        \"Effect\": \"Allow\",
        \"Action\": [\"s3:GetObject\"],
        \"Resource\": \"arn:aws:s3:::${S3_BUCKET}/${S3_PREFIX%/}/*\"
      }]
    }" >/dev/null
fi

echo "Done. Run scripts/ensure_agent_lambda_access.py to wire the Bedrock agent IAM."
echo "ShareRecipeWithBuddy invokes ${BUDDY_EMAIL_LAMBDA_NAME:-cooking-rag-buddy-email} directly."
