#!/usr/bin/env bash
# One-time AWS setup for kap_aws.py: S3 bucket, the scraper and proxy images in ECR, IAM roles, log group,
# ECS cluster, task definitions and security groups. None of it costs anything while no task runs, apart
# from ~0.10 $ a month for the images in ECR. Run it again after changing the code, to push new images.
# Needs Docker running and the AWS CLI signed in (aws login).
set -euo pipefail
cd "$(dirname "$0")"
source config.sh

echo "Bucket $BUCKET"
if ! aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
    aws s3api create-bucket --bucket "$BUCKET" --create-bucket-configuration LocationConstraint="$AWS_DEFAULT_REGION" >/dev/null
    aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
        BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
fi

echo "Images"
aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY"
for repo in "$PROJECT" "$PROJECT-proxy"; do
    aws ecr describe-repositories --repository-names "$repo" >/dev/null 2>&1 \
        || aws ecr create-repository --repository-name "$repo" >/dev/null
done
docker build --platform linux/amd64 -t "$REGISTRY/$PROJECT:latest" ..
docker build --platform linux/amd64 -t "$REGISTRY/$PROJECT-proxy:latest" -f proxy.Dockerfile .
docker push "$REGISTRY/$PROJECT:latest"
docker push "$REGISTRY/$PROJECT-proxy:latest"

echo "Roles"
TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
for role in execution task; do
    aws iam get-role --role-name "$PROJECT-$role" >/dev/null 2>&1 \
        || aws iam create-role --role-name "$PROJECT-$role" --assume-role-policy-document "$TRUST" >/dev/null
done
aws iam attach-role-policy --role-name "$PROJECT-execution" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
aws iam put-role-policy --role-name "$PROJECT-task" --policy-name s3 --policy-document '{"Version":"2012-10-17","Statement":[
    {"Effect":"Allow","Action":["s3:GetObject","s3:PutObject"],"Resource":"arn:aws:s3:::'"$BUCKET"'/*"},
    {"Effect":"Allow","Action":"s3:ListBucket","Resource":"arn:aws:s3:::'"$BUCKET"'"}]}'

echo "Logs and cluster"
aws logs create-log-group --log-group-name "/ecs/$PROJECT" >/dev/null 2>&1 || true
aws logs put-retention-policy --log-group-name "/ecs/$PROJECT" --retention-in-days 30
aws ecs create-cluster --cluster-name "$PROJECT" >/dev/null

echo "Security groups"
SCRAPER_SG=$(sg "$PROJECT-scrapers" "KAP scrapers, no inbound")
PROXY_SG=$(sg "$PROJECT-proxies" "squid proxies, port 3128 from the scrapers only")
aws ec2 authorize-security-group-ingress --group-id "$PROXY_SG" --protocol tcp --port 3128 \
    --source-group "$SCRAPER_SG" >/dev/null 2>&1 || true  # fails only if the rule is there already

echo "Task definitions"
CIDR=$(aws ec2 describe-vpcs --vpc-ids "$VPC" --query 'Vpcs[0].CidrBlock' --output text)
export PROJECT BUCKET REGISTRY ACCOUNT AWS_DEFAULT_REGION CONF="$(sed "s|VPC_CIDR|$CIDR|" squid.conf)"
for kind in scraper proxy; do
    aws ecs register-task-definition --cli-input-json "$(python - "$kind" <<'EOF'
import json, os, sys
kind, project = sys.argv[1], os.environ["PROJECT"]
role = f"arn:aws:iam::{os.environ['ACCOUNT']}:role/{project}"
logs = {"logDriver": "awslogs", "options": {"awslogs-group": f"/ecs/{project}", "awslogs-stream-prefix": kind,
                                            "awslogs-region": os.environ["AWS_DEFAULT_REGION"]}}
if kind == "scraper":  # 1 vCPU and 4 GB, as the 4 GB Apify runs had; peak memory there was ~1 GB
    size, image = ("1024", "4096"), f"{os.environ['REGISTRY']}/{project}:latest"
    container = {"command": ["python", "kap_aws.py"], "environment": [{"name": "BUCKET", "value": os.environ["BUCKET"]}],
                 "stopTimeout": 120}  # time to upload the file when stopped
else:
    size, image = ("256", "512"), f"{os.environ['REGISTRY']}/{project}-proxy:latest"
    container = {"environment": [{"name": "CONF", "value": os.environ["CONF"]}]}
print(json.dumps({
    "family": f"{project}-{kind}", "networkMode": "awsvpc", "requiresCompatibilities": ["FARGATE"],
    "cpu": size[0], "memory": size[1], "runtimePlatform": {"cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX"},
    "executionRoleArn": f"{role}-execution", "taskRoleArn": f"{role}-task",
    "containerDefinitions": [{"name": kind, "image": image, "essential": True, "logConfiguration": logs, **container}],
}))
EOF
)" --query 'taskDefinition.taskDefinitionArn' --output text
done
echo "Done. Start a range with aws/run.sh."
