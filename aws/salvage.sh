#!/usr/bin/env bash
# One-off: read again, page by page, the attachments that crashed PyMuPDF (kap_salvage.py), in one task.
# TODO lists them ({disclosure_index, file_id, file_name, url}); result: s3://BUCKET/salvage/results.jsonl.
# Usage: aws/salvage.sh data/salvage_todo.json [VCPUS=8]
set -euo pipefail
cd "$(dirname "$0")"
source config.sh
TODO=$1 VCPUS=${2:-8}

aws s3 cp "../$TODO" "s3://$BUCKET/salvage/todo.json" --only-show-errors
SUBNETS=$(aws ec2 describe-subnets --filters Name=default-for-az,Values=true --query 'Subnets[].SubnetId' --output text | tr '\t' ',')
overrides='{"cpu":"'$((VCPUS * 1024))'","memory":"'$((VCPUS * 2048))'",
    "containerOverrides":[{"name":"scraper","command":["python","kap_salvage.py"]}]}'
aws ecs run-task --cluster "$PROJECT" --launch-type FARGATE --task-definition "$PROJECT-scraper" \
    --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$(sg "$PROJECT-scrapers")],assignPublicIp=ENABLED}" \
    --overrides "$overrides" --query 'tasks[0].taskArn' --output text
echo "Follow: aws logs tail /ecs/$PROJECT --follow --region $AWS_DEFAULT_REGION"
