#!/usr/bin/env bash
# Start one scraper for FIRST..LAST through the proxies already running, e.g. again for a range whose task
# died. Use the range exactly as run.sh printed it, so the task continues the same file in S3.
# VCPUS, if given, overrides the task definition's 2 vCPU, with 4 GB of memory per vCPU.
# Usage: aws/resume.sh 16.05.2024 08.11.2024 [VCPUS]
set -euo pipefail
cd "$(dirname "$0")"
source config.sh
FIRST=$1 LAST=$2 VCPUS=${3:-}

PROXY_TASKS=$(aws ecs list-tasks --cluster "$PROJECT" --family "$PROJECT-proxy" --query 'taskArns' --output text)
[ -n "$PROXY_TASKS" ] || { echo "No proxies are running; start with run.sh."; exit 1; }
PROXIES=$(aws ecs describe-tasks --cluster "$PROJECT" --tasks $PROXY_TASKS \
    --query "tasks[].attachments[0].details[?name=='privateIPv4Address'].value[]" --output text \
    | tr '\t' '\n' | sed 's|.*|http://&:3128|' | paste -sd,)
SUBNETS=$(aws ec2 describe-subnets --filters Name=default-for-az,Values=true --query 'Subnets[].SubnetId' --output text | tr '\t' ',')
size=${VCPUS:+'"cpu":"'$((VCPUS * 1024))'","memory":"'$((VCPUS * 4096))'",'}
overrides='{'$size'"containerOverrides":[{"name":"scraper","command":["python","kap_aws.py","--start","'$FIRST'","--end","'$LAST'"],
    "environment":[{"name":"PROXIES","value":"'$PROXIES'"}]}]}'
aws ecs run-task --cluster "$PROJECT" --launch-type FARGATE --task-definition "$PROJECT-scraper" \
    --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$(sg "$PROJECT-scrapers")],assignPublicIp=ENABLED}" \
    --overrides "$overrides" --query 'tasks[0].taskArn' --output text
echo "  $FIRST - $LAST through $(echo "$PROXIES" | tr ',' '\n' | wc -l) proxies"
