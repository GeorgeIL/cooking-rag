#!/usr/bin/env bash
# =============================================================================
#  Smart Cookbook — one-shot EC2 deploy
#
#  Launches an Ubuntu EC2 instance that clones this repo, builds the Docker
#  image locally (native amd64 — no cross-compile) and runs the container.
#  Config comes from your ./.env; AWS credentials come from the instance role,
#  so nothing secret is copied to the server.
#
#  Prereqs:  ./setup_aws.sh has run (so the IAM instance profile, security
#            group and Knowledge Base exist) and this repo is pushed & public.
#
#  Usage:    ./deploy.sh                 # uses KEY_NAME / SG_ID from .env
#            KEY_NAME=my-key ./deploy.sh
# =============================================================================
set -euo pipefail

PROJECT="${PROJECT:-cooking-rag}"
PROFILE_NAME="${PROFILE_NAME:-${PROJECT}-ec2-profile}"
REPO_URL="${REPO_URL:-https://github.com/GeorgeIL/cooking-rag.git}"
INSTANCE_TYPE="${INSTANCE_TYPE:-t3.micro}"

c_g=$'\e[32m'; c_b=$'\e[1m'; c_r=$'\e[31m'; c_0=$'\e[0m'
log(){ printf '%s\n' "${c_b}▶ $*${c_0}"; }
ok(){ printf '%s\n' "${c_g}  ✓ $*${c_0}"; }
die(){ printf '%s\n' "${c_r}✗ $*${c_0}" >&2; exit 1; }

[[ -f .env ]] || die "No .env — run ./setup_aws.sh first."
set -a; source ./.env; set +a
export AWS_PAGER=""; export AWS_DEFAULT_REGION="${AWS_REGION:?}"

: "${KEY_NAME:?set KEY_NAME in .env (an EC2 key pair name) or pass KEY_NAME=…}"
: "${BEDROCK_KB_ID:?run ./setup_aws.sh first}"
: "${RDS_HOST:?run ./setup_aws.sh first}"
SG_ID="${SG_ID:-}"
[[ -n "$SG_ID" ]] || SG_ID="$(aws ec2 describe-security-groups \
  --filters "Name=group-name,Values=${PROJECT}-sg" --query 'SecurityGroups[0].GroupId' --output text)"
[[ "$SG_ID" != "None" && -n "$SG_ID" ]] || die "security group not found — run ./setup_aws.sh"

# ── Build the env file that will live on the instance (NO AWS keys) ───────────
RUNTIME_ENV="$(cat <<EOF
SECRET_KEY=${SECRET_KEY}
AWS_REGION=${AWS_REGION}
BEDROCK_KB_ID=${BEDROCK_KB_ID}
BEDROCK_KB_DS_ID=${BEDROCK_KB_DS_ID}
S3_BUCKET_NAME=${S3_BUCKET_NAME}
S3_RECIPES_PREFIX=${S3_RECIPES_PREFIX:-recipes/}
RDS_HOST=${RDS_HOST}
RDS_PORT=${RDS_PORT:-5432}
RDS_DB=${RDS_DB:-postgres}
RDS_USER=${RDS_USER:-postgres}
EOF
)"

# ── cloud-init user-data: install docker, clone, build, run ───────────────────
USER_DATA="$(cat <<EOF
#!/bin/bash
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y ca-certificates curl git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=\$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \$(. /etc/os-release && echo \$VERSION_CODENAME) stable" > /etc/apt/sources.list.d/docker.list
apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io
systemctl enable --now docker
git clone ${REPO_URL} /opt/app
cat > /opt/app/.env <<'ENVEOF'
${RUNTIME_ENV}
ENVEOF
cd /opt/app
docker build -t ${PROJECT}:latest .
docker run -d --restart=always -p 5001:5001 --env-file /opt/app/.env --name ${PROJECT} ${PROJECT}:latest
EOF
)"

# ── Latest Ubuntu 24.04 amd64 AMI (Canonical) ────────────────────────────────
log "resolving latest Ubuntu 24.04 AMI…"
AMI_ID="$(aws ec2 describe-images --owners 099720109477 \
  --filters "Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*" \
            "Name=state,Values=available" \
  --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text)"
ok "AMI $AMI_ID"

log "launching $INSTANCE_TYPE …"
INSTANCE_ID="$(aws ec2 run-instances \
  --image-id "$AMI_ID" --instance-type "$INSTANCE_TYPE" \
  --key-name "$KEY_NAME" --security-group-ids "$SG_ID" \
  --iam-instance-profile "Name=${PROFILE_NAME}" \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":20,"VolumeType":"gp3","DeleteOnTermination":true}}]' \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=${PROJECT}}]" \
  --user-data "$USER_DATA" \
  --query 'Instances[0].InstanceId' --output text)"
ok "instance $INSTANCE_ID"

aws ec2 wait instance-running --instance-ids "$INSTANCE_ID"
PUBLIC_IP="$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)"
ok "running at $PUBLIC_IP"

log "waiting for the app to boot (installs Docker + builds image, ~3-5 min)…"
URL="http://${PUBLIC_IP}:5001"
for i in $(seq 1 60); do
  code="$(curl -s -o /dev/null -m 4 -w '%{http_code}' "$URL/" 2>/dev/null || echo 000)"
  if [[ "$code" != "000" ]]; then ok "app responded (HTTP $code) after ~$((i*15))s"; break; fi
  sleep 15
done

echo
printf '%s\n' "${c_g}${c_b}Deployed 🎉  →  ${URL}${c_0}"
printf '%s\n' "  instance:  $INSTANCE_ID   ip: $PUBLIC_IP"
printf '%s\n' "  ssh:       ssh -i ~/Downloads/${KEY_NAME}.pem ubuntu@${PUBLIC_IP}"
printf '%s\n' "  logs:      ssh … 'sudo docker logs ${PROJECT} --tail 40'"
printf '%s\n' "  terminate: aws ec2 terminate-instances --instance-ids ${INSTANCE_ID}"
