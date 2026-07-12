#!/usr/bin/env bash
# =============================================================================
#  Smart Cookbook — teardown
#
#  Removes the billable resources created by setup_aws.sh / deploy.sh so you
#  stop paying for them. S3 objects and the IAM roles are left in place by
#  default (cheap / reusable); pass --all to remove those too.
#
#  Usage:  ./teardown_aws.sh          # deletes EC2 instances, Aurora, OpenSearch KB
#          ./teardown_aws.sh --all    # also deletes S3 bucket + IAM roles
# =============================================================================
set -uo pipefail
PROJECT="${PROJECT:-cooking-rag}"
ALL=""; [[ "${1:-}" == "--all" ]] && ALL=1
[[ -f .env ]] && { set -a; source ./.env; set +a; }
export AWS_PAGER=""; export AWS_DEFAULT_REGION="${AWS_REGION:-us-east-1}"
say(){ printf '▶ %s\n' "$*"; }

say "terminating EC2 instances tagged Name=${PROJECT}"
IDS="$(aws ec2 describe-instances --filters "Name=tag:Name,Values=${PROJECT}" \
  "Name=instance-state-name,Values=pending,running,stopped" \
  --query 'Reservations[].Instances[].InstanceId' --output text)"
[[ -n "$IDS" ]] && aws ec2 terminate-instances --instance-ids $IDS >/dev/null && echo "  $IDS"

say "deleting Bedrock Knowledge Base + data source (${PROJECT}-kb)"
KB="$(aws bedrock-agent list-knowledge-bases --query "knowledgeBaseSummaries[?name=='${PROJECT}-kb'].knowledgeBaseId" --output text 2>/dev/null)"
if [[ -n "$KB" && "$KB" != "None" ]]; then
  for ds in $(aws bedrock-agent list-data-sources --knowledge-base-id "$KB" --query 'dataSourceSummaries[].dataSourceId' --output text 2>/dev/null); do
    aws bedrock-agent delete-data-source --knowledge-base-id "$KB" --data-source-id "$ds" >/dev/null 2>&1 || true
  done
  aws bedrock-agent delete-knowledge-base --knowledge-base-id "$KB" >/dev/null 2>&1 || true
  echo "  $KB"
fi

say "deleting OpenSearch Serverless collection (${PROJECT}-vec)  <- stops the big charge"
aws opensearchserverless delete-collection --id \
  "$(aws opensearchserverless batch-get-collection --names "${PROJECT}-vec" --query 'collectionDetails[0].id' --output text 2>/dev/null)" >/dev/null 2>&1 || true
for t in encryption network; do aws opensearchserverless delete-security-policy --name "${PROJECT}-${t:0:3}" --type "$t" >/dev/null 2>&1 || true; done
aws opensearchserverless delete-access-policy --name "${PROJECT}-data" --type data >/dev/null 2>&1 || true

say "deleting Aurora cluster (${PROJECT}-db)"
aws rds delete-db-instance --db-instance-identifier "${PROJECT}-db-1" --skip-final-snapshot >/dev/null 2>&1 || true
aws rds delete-db-cluster --db-cluster-identifier "${PROJECT}-db" --skip-final-snapshot >/dev/null 2>&1 || true

if [[ -n "$ALL" ]]; then
  say "--all: deleting S3 bucket + IAM roles + security group"
  aws s3 rb "s3://${S3_BUCKET_NAME}" --force >/dev/null 2>&1 || true
  for r in "${PROJECT}-ec2-role" "${PROJECT}-kb-role"; do
    for p in $(aws iam list-attached-role-policies --role-name "$r" --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null); do
      aws iam detach-role-policy --role-name "$r" --policy-arn "$p" >/dev/null 2>&1 || true; done
    for p in $(aws iam list-role-policies --role-name "$r" --query 'PolicyNames' --output text 2>/dev/null); do
      aws iam delete-role-policy --role-name "$r" --policy-name "$p" >/dev/null 2>&1 || true; done
    aws iam remove-role-from-instance-profile --instance-profile-name "${PROJECT}-ec2-profile" --role-name "$r" >/dev/null 2>&1 || true
    aws iam delete-role --role-name "$r" >/dev/null 2>&1 || true
  done
  aws iam delete-instance-profile --instance-profile-name "${PROJECT}-ec2-profile" >/dev/null 2>&1 || true
  aws ec2 delete-security-group --group-name "${PROJECT}-sg" >/dev/null 2>&1 || true
fi
say "done."
