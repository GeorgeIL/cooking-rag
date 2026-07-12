#!/usr/bin/env bash
# =============================================================================
#  Smart Cookbook — one-shot AWS provisioner
#
#  Reads ./.env and creates every AWS resource the app needs, then writes the
#  generated IDs (KB, data source, RDS endpoint) back into ./.env.
#
#  Everything is idempotent: re-running skips resources that already exist, so
#  it is safe to run again after a failure.
#
#  Usage:
#     cp .env.example .env      # then fill in the <...> values
#     ./setup_aws.sh            # provision everything
#     ./setup_aws.sh --reuse-kb <KB_ID> <DS_ID>   # skip the costly KB creation
#     ./setup_aws.sh --yes      # don't ask for cost confirmation
#
#  Cost warning: this creates an Aurora PostgreSQL cluster and an OpenSearch
#  Serverless collection. OpenSearch Serverless has a HIGH minimum monthly cost
#  (roughly hundreds of USD). Use --reuse-kb to point at an existing Knowledge
#  Base and skip that charge. Run ./teardown_aws.sh to remove everything.
# =============================================================================
set -euo pipefail

# ── Names (override via env if you like) ─────────────────────────────────────
PROJECT="${PROJECT:-cooking-rag}"
ROLE_NAME="${PROJECT}-ec2-role"
PROFILE_NAME="${PROJECT}-ec2-profile"
SG_NAME="${PROJECT}-sg"
KB_ROLE_NAME="${PROJECT}-kb-role"
DB_CLUSTER_ID="${PROJECT}-db"
DB_INSTANCE_ID="${PROJECT}-db-1"
AOSS_COLLECTION="${PROJECT}-vec"
EMBED_MODEL="${EMBED_MODEL:-amazon.titan-embed-text-v2:0}"
EMBED_DIM="${EMBED_DIM:-1024}"
INDEX_NAME="bedrock-knowledge-base-index"
VEC_FIELD="bedrock-knowledge-base-default-vector"
TXT_FIELD="AMAZON_BEDROCK_TEXT_CHUNK"
META_FIELD="AMAZON_BEDROCK_METADATA"

# ── Pretty logging ───────────────────────────────────────────────────────────
c_g=$'\e[32m'; c_y=$'\e[33m'; c_r=$'\e[31m'; c_b=$'\e[1m'; c_0=$'\e[0m'
log()  { printf '%s\n' "${c_b}▶ $*${c_0}"; }
ok()   { printf '%s\n' "${c_g}  ✓ $*${c_0}"; }
warn() { printf '%s\n' "${c_y}  ! $*${c_0}"; }
die()  { printf '%s\n' "${c_r}✗ $*${c_0}" >&2; exit 1; }

REUSE_KB=""; REUSE_DS=""; ASSUME_YES=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --reuse-kb) REUSE_KB="${2:-}"; REUSE_DS="${3:-}"; shift 3 ;;
    --yes|-y)   ASSUME_YES=1; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done

# ── Load .env ────────────────────────────────────────────────────────────────
[[ -f .env ]] || die "No .env found. Run:  cp .env.example .env  and fill it in."
set -a; source ./.env; set +a
export AWS_PAGER=""

: "${AWS_ACCESS_KEY_ID:?set AWS_ACCESS_KEY_ID in .env}"
: "${AWS_SECRET_ACCESS_KEY:?set AWS_SECRET_ACCESS_KEY in .env}"
: "${AWS_REGION:?set AWS_REGION in .env}"
: "${S3_BUCKET_NAME:?set S3_BUCKET_NAME in .env}"
export AWS_DEFAULT_REGION="$AWS_REGION"

command -v aws     >/dev/null || die "aws CLI not found"
command -v python3 >/dev/null || die "python3 not found"
python3 -c "import boto3, psycopg2, botocore" 2>/dev/null \
  || die "pip install boto3 psycopg2-binary  (needed by this script)"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ok "AWS account $ACCOUNT_ID / region $AWS_REGION"

# Generate a SECRET_KEY if the user left it blank.
if [[ -z "${SECRET_KEY:-}" || "${SECRET_KEY}" == "<long_random_string>" ]]; then
  SECRET_KEY="$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
  ok "generated a random SECRET_KEY"
fi

# Helper: set-or-append a KEY=VALUE in ./.env
set_env() { python3 - "$1" "$2" <<'PY'
import sys,re,io
key,val=sys.argv[1],sys.argv[2]
p=".env"; t=open(p).read()
if re.search(rf'^{re.escape(key)}=.*$',t,re.M):
    t=re.sub(rf'^{re.escape(key)}=.*$',f'{key}={val}',t,flags=re.M)
else:
    t=t.rstrip()+f'\n{key}={val}\n'
open(p,'w').write(t)
PY
}

if [[ -z "$ASSUME_YES" && -z "$REUSE_KB" ]]; then
  warn "This will create an Aurora cluster AND an OpenSearch Serverless collection."
  warn "OpenSearch Serverless has a high minimum monthly cost (hundreds of USD)."
  read -r -p "  Continue? [y/N] " a; [[ "$a" == "y" || "$a" == "Y" ]] || die "aborted"
fi

# =============================================================================
log "1/7  S3 bucket  ($S3_BUCKET_NAME)"
# =============================================================================
if aws s3api head-bucket --bucket "$S3_BUCKET_NAME" 2>/dev/null; then
  ok "bucket already exists"
else
  if [[ "$AWS_REGION" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "$S3_BUCKET_NAME" >/dev/null
  else
    aws s3api create-bucket --bucket "$S3_BUCKET_NAME" \
      --create-bucket-configuration LocationConstraint="$AWS_REGION" >/dev/null
  fi
  ok "bucket created"
fi
if [[ -d data/recipes ]]; then
  aws s3 cp data/recipes/ "s3://$S3_BUCKET_NAME/${S3_RECIPES_PREFIX:-recipes/}" \
    --recursive --exclude "*" --include "*.md" >/dev/null
  ok "seed recipes uploaded"
fi

# =============================================================================
log "2/7  IAM role + instance profile for the EC2 app  ($ROLE_NAME)"
# =============================================================================
if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
  ok "role created"
else ok "role exists"; fi

for arn in \
  arn:aws:iam::aws:policy/AmazonBedrockFullAccess \
  arn:aws:iam::aws:policy/AmazonS3FullAccess \
  arn:aws:iam::aws:policy/AmazonRDSFullAccess ; do
  aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn "$arn" >/dev/null
done
# rds-db:connect is NOT covered by AmazonRDSFullAccess — grant it explicitly.
aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name RdsIamConnect \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"rds-db:connect\",\"Resource\":\"arn:aws:rds-db:${AWS_REGION}:${ACCOUNT_ID}:dbuser:*/${RDS_USER:-postgres}\"}]}" >/dev/null
ok "policies attached (Bedrock, S3, RDS, rds-db:connect)"

if ! aws iam get-instance-profile --instance-profile-name "$PROFILE_NAME" >/dev/null 2>&1; then
  aws iam create-instance-profile --instance-profile-name "$PROFILE_NAME" >/dev/null
  aws iam add-role-to-instance-profile --instance-profile-name "$PROFILE_NAME" --role-name "$ROLE_NAME" >/dev/null
  ok "instance profile created"
else ok "instance profile exists"; fi

# =============================================================================
log "3/7  Security group  ($SG_NAME)"
# =============================================================================
SG_ID="$(aws ec2 describe-security-groups --filters "Name=group-name,Values=$SG_NAME" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)"
if [[ -z "$SG_ID" || "$SG_ID" == "None" ]]; then
  SG_ID="$(aws ec2 create-security-group --group-name "$SG_NAME" \
    --description "Smart Cookbook app" --query 'GroupId' --output text)"
  aws ec2 authorize-security-group-ingress --group-id "$SG_ID" --protocol tcp --port 22   --cidr 0.0.0.0/0 >/dev/null
  aws ec2 authorize-security-group-ingress --group-id "$SG_ID" --protocol tcp --port 5001 --cidr 0.0.0.0/0 >/dev/null
  ok "security group created ($SG_ID)  ports 22, 5001 open"
else ok "security group exists ($SG_ID)"; fi
set_env SG_ID "$SG_ID"

# =============================================================================
log "4/7  Aurora PostgreSQL cluster  ($DB_CLUSTER_ID)"
# =============================================================================
if ! aws rds describe-db-clusters --db-cluster-identifier "$DB_CLUSTER_ID" >/dev/null 2>&1; then
  : "${RDS_MASTER_PASSWORD:?set RDS_MASTER_PASSWORD in .env to create the cluster}"
  aws rds create-db-cluster \
    --db-cluster-identifier "$DB_CLUSTER_ID" \
    --engine aurora-postgresql --engine-version 17.4 \
    --master-username postgres --master-user-password "$RDS_MASTER_PASSWORD" \
    --database-name postgres \
    --enable-iam-database-authentication \
    --serverless-v2-scaling-configuration MinCapacity=0.5,MaxCapacity=2 >/dev/null
  aws rds create-db-instance \
    --db-instance-identifier "$DB_INSTANCE_ID" \
    --db-cluster-identifier "$DB_CLUSTER_ID" \
    --engine aurora-postgresql --db-instance-class db.serverless >/dev/null
  ok "cluster + instance creating"
else ok "cluster exists"; fi

log "     waiting for the cluster to become available (this can take ~10 min)…"
aws rds wait db-instance-available --db-instance-identifier "$DB_INSTANCE_ID"
RDS_HOST="$(aws rds describe-db-clusters --db-cluster-identifier "$DB_CLUSTER_ID" \
  --query 'DBClusters[0].Endpoint' --output text)"
ok "cluster available at $RDS_HOST"

# =============================================================================
log "5/7  Grant rds_iam to the postgres user + apply schema"
# =============================================================================
RDS_HOST="$RDS_HOST" RDS_MASTER_PASSWORD="${RDS_MASTER_PASSWORD:-}" python3 - <<'PY'
import os, psycopg2, pathlib
host=os.environ["RDS_HOST"]; pw=os.environ.get("RDS_MASTER_PASSWORD","")
if not pw:
    print("  ! RDS_MASTER_PASSWORD not set — skipping grant/schema (app will apply schema on boot)"); raise SystemExit
conn=psycopg2.connect(host=host,port=5432,dbname="postgres",user="postgres",
                      password=pw,sslmode="require")
conn.autocommit=True
cur=conn.cursor()
cur.execute("GRANT rds_iam TO postgres;")
sql=pathlib.Path("migrations/schema.sql")
if sql.exists(): cur.execute(sql.read_text())
print("  ✓ rds_iam granted + schema applied")
conn.close()
PY

# =============================================================================
log "6/7  Bedrock Knowledge Base"
# =============================================================================
if [[ -n "$REUSE_KB" ]]; then
  KB_ID="$REUSE_KB"; DS_ID="$REUSE_DS"
  ok "reusing existing KB $KB_ID / DS $DS_ID"
else
  # 6a. IAM service role Bedrock assumes to read S3 + call the embedding model + query OSS
  if ! aws iam get-role --role-name "$KB_ROLE_NAME" >/dev/null 2>&1; then
    aws iam create-role --role-name "$KB_ROLE_NAME" \
      --assume-role-policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"bedrock.amazonaws.com\"},\"Action\":\"sts:AssumeRole\"}]}" >/dev/null
  fi
  aws iam put-role-policy --role-name "$KB_ROLE_NAME" --policy-name kb-access \
    --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[
      {\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:ListBucket\"],\"Resource\":[\"arn:aws:s3:::${S3_BUCKET_NAME}\",\"arn:aws:s3:::${S3_BUCKET_NAME}/*\"]},
      {\"Effect\":\"Allow\",\"Action\":[\"bedrock:InvokeModel\"],\"Resource\":\"arn:aws:bedrock:${AWS_REGION}::foundation-model/${EMBED_MODEL}\"},
      {\"Effect\":\"Allow\",\"Action\":[\"aoss:APIAccessAll\"],\"Resource\":\"*\"}]}" >/dev/null
  KB_ROLE_ARN="$(aws iam get-role --role-name "$KB_ROLE_NAME" --query 'Role.Arn' --output text)"
  ok "KB service role ready"

  # 6b. OpenSearch Serverless collection + policies
  if ! aws opensearchserverless batch-get-collection --names "$AOSS_COLLECTION" \
        --query 'collectionDetails[0].id' --output text 2>/dev/null | grep -qE '\w'; then
    aws opensearchserverless create-security-policy --name "${PROJECT}-enc" --type encryption \
      --policy "{\"Rules\":[{\"ResourceType\":\"collection\",\"Resource\":[\"collection/${AOSS_COLLECTION}\"]}],\"AWSOwnedKey\":true}" >/dev/null
    aws opensearchserverless create-security-policy --name "${PROJECT}-net" --type network \
      --policy "[{\"Rules\":[{\"ResourceType\":\"collection\",\"Resource\":[\"collection/${AOSS_COLLECTION}\"]},{\"ResourceType\":\"dashboard\",\"Resource\":[\"collection/${AOSS_COLLECTION}\"]}],\"AllowFromPublic\":true}]" >/dev/null
    CALLER_ARN="$(aws sts get-caller-identity --query Arn --output text)"
    aws opensearchserverless create-access-policy --name "${PROJECT}-data" --type data \
      --policy "[{\"Rules\":[{\"ResourceType\":\"index\",\"Resource\":[\"index/${AOSS_COLLECTION}/*\"],\"Permission\":[\"aoss:*\"]},{\"ResourceType\":\"collection\",\"Resource\":[\"collection/${AOSS_COLLECTION}\"],\"Permission\":[\"aoss:*\"]}],\"Principal\":[\"${KB_ROLE_ARN}\",\"${CALLER_ARN}\"]}]" >/dev/null
    aws opensearchserverless create-collection --name "$AOSS_COLLECTION" --type VECTORSEARCH >/dev/null
    ok "OpenSearch Serverless collection creating"
  else ok "OpenSearch Serverless collection exists"; fi

  log "     waiting for the collection to become ACTIVE…"
  for _ in $(seq 1 60); do
    ST="$(aws opensearchserverless batch-get-collection --names "$AOSS_COLLECTION" --query 'collectionDetails[0].status' --output text 2>/dev/null || true)"
    [[ "$ST" == "ACTIVE" ]] && break; sleep 10
  done
  [[ "$ST" == "ACTIVE" ]] || die "collection did not become ACTIVE"
  COLL_ARN="$(aws opensearchserverless batch-get-collection --names "$AOSS_COLLECTION" --query 'collectionDetails[0].arn' --output text)"
  COLL_EP="$(aws opensearchserverless batch-get-collection --names "$AOSS_COLLECTION" --query 'collectionDetails[0].collectionEndpoint' --output text)"
  ok "collection ACTIVE"

  # 6c. Create the vector index (SigV4-signed PUT to the collection endpoint)
  log "     creating vector index '$INDEX_NAME'…"
  COLL_EP="$COLL_EP" INDEX_NAME="$INDEX_NAME" EMBED_DIM="$EMBED_DIM" \
  VEC_FIELD="$VEC_FIELD" TXT_FIELD="$TXT_FIELD" META_FIELD="$META_FIELD" \
  AWS_REGION="$AWS_REGION" python3 - <<'PY'
import os, json, time, urllib.request
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
import boto3
ep=os.environ["COLL_EP"].rstrip("/"); idx=os.environ["INDEX_NAME"]
body=json.dumps({
  "settings":{"index":{"knn":True}},
  "mappings":{"properties":{
    os.environ["VEC_FIELD"]:{"type":"knn_vector","dimension":int(os.environ["EMBED_DIM"]),
      "method":{"name":"hnsw","engine":"faiss","space_type":"l2"}},
    os.environ["TXT_FIELD"]:{"type":"text"},
    os.environ["META_FIELD"]:{"type":"text","index":False}}}}).encode()
url=f"{ep}/{idx}"
sess=boto3.Session(); creds=sess.get_credentials().get_frozen_credentials()
req=AWSRequest(method="PUT",url=url,data=body,headers={"Content-Type":"application/json"})
SigV4Auth(creds,"aoss",os.environ["AWS_REGION"]).add_auth(req)
r=urllib.request.Request(url,data=body,method="PUT")
for k,v in req.headers.items(): r.add_header(k,v)
try:
    urllib.request.urlopen(r,timeout=30); print("  ✓ index created")
except urllib.error.HTTPError as e:
    msg=e.read().decode()
    if "resource_already_exists" in msg or e.code==400 and "already exists" in msg:
        print("  ✓ index already exists")
    else:
        print("  ! index create returned",e.code,msg[:200])
time.sleep(30)  # give the index a moment to be queryable before KB creation
PY

  # 6d. Create the Knowledge Base + S3 data source
  KB_ID="$(aws bedrock-agent list-knowledge-bases --query "knowledgeBaseSummaries[?name=='${PROJECT}-kb'].knowledgeBaseId" --output text 2>/dev/null || true)"
  if [[ -z "$KB_ID" || "$KB_ID" == "None" ]]; then
    KB_ID="$(aws bedrock-agent create-knowledge-base --name "${PROJECT}-kb" \
      --role-arn "$KB_ROLE_ARN" \
      --knowledge-base-configuration "{\"type\":\"VECTOR\",\"vectorKnowledgeBaseConfiguration\":{\"embeddingModelArn\":\"arn:aws:bedrock:${AWS_REGION}::foundation-model/${EMBED_MODEL}\"}}" \
      --storage-configuration "{\"type\":\"OPENSEARCH_SERVERLESS\",\"opensearchServerlessConfiguration\":{\"collectionArn\":\"${COLL_ARN}\",\"vectorIndexName\":\"${INDEX_NAME}\",\"fieldMapping\":{\"vectorField\":\"${VEC_FIELD}\",\"textField\":\"${TXT_FIELD}\",\"metadataField\":\"${META_FIELD}\"}}}" \
      --query 'knowledgeBase.knowledgeBaseId' --output text)"
    ok "knowledge base created ($KB_ID)"
  else ok "knowledge base exists ($KB_ID)"; fi

  DS_ID="$(aws bedrock-agent list-data-sources --knowledge-base-id "$KB_ID" \
    --query "dataSourceSummaries[?name=='${PROJECT}-s3'].dataSourceId" --output text 2>/dev/null || true)"
  if [[ -z "$DS_ID" || "$DS_ID" == "None" ]]; then
    DS_ID="$(aws bedrock-agent create-data-source --knowledge-base-id "$KB_ID" --name "${PROJECT}-s3" \
      --data-source-configuration "{\"type\":\"S3\",\"s3Configuration\":{\"bucketArn\":\"arn:aws:s3:::${S3_BUCKET_NAME}\",\"inclusionPrefixes\":[\"${S3_RECIPES_PREFIX:-recipes/}\"]}}" \
      --query 'dataSource.dataSourceId' --output text)"
    ok "data source created ($DS_ID)"
  else ok "data source exists ($DS_ID)"; fi

  aws bedrock-agent start-ingestion-job --knowledge-base-id "$KB_ID" --data-source-id "$DS_ID" >/dev/null || true
  ok "ingestion started (recipes will be searchable in a minute or two)"
fi

# =============================================================================
log "7/7  Writing generated values back into .env"
# =============================================================================
set_env SECRET_KEY       "$SECRET_KEY"
set_env BEDROCK_KB_ID    "$KB_ID"
set_env BEDROCK_KB_DS_ID "$DS_ID"
set_env RDS_HOST         "$RDS_HOST"
set_env RDS_PORT         "5432"
set_env RDS_DB           "postgres"
set_env RDS_USER         "postgres"
ok ".env updated"

echo
printf '%s\n' "${c_g}${c_b}════════════════════════════════════════════════════════════════${c_0}"
printf '%s\n' "${c_g}${c_b}  AWS stack ready 🎉${c_0}"
printf '%s\n' "  KB_ID          $KB_ID"
printf '%s\n' "  DS_ID          $DS_ID"
printf '%s\n' "  RDS_HOST       $RDS_HOST"
printf '%s\n' "  S3_BUCKET      $S3_BUCKET_NAME"
printf '%s\n' "  SG_ID          $SG_ID"
printf '%s\n' "  EC2 profile    $PROFILE_NAME"
echo
printf '%s\n' "  Next:  run locally →  python app.py"
printf '%s\n' "         deploy      →  ./deploy.sh"
printf '%s\n' "${c_g}${c_b}════════════════════════════════════════════════════════════════${c_0}"
