#!/usr/bin/env bash
# Remove everything setup.sh made, except the bucket with the scraped data. Download the data first:
#   aws s3 sync s3://<bucket>/disclosures data/aws
set -euo pipefail
cd "$(dirname "$0")"
source config.sh
./stop.sh
for kind in scraper proxy; do
    for arn in $(aws ecs list-task-definitions --family-prefix "$PROJECT-$kind" --query 'taskDefinitionArns' --output text); do
        aws ecs deregister-task-definition --task-definition "$arn" >/dev/null
    done
done
aws ecs delete-cluster --cluster "$PROJECT" >/dev/null || true
for repo in "$PROJECT" "$PROJECT-proxy"; do aws ecr delete-repository --repository-name "$repo" --force >/dev/null || true; done
aws logs delete-log-group --log-group-name "/ecs/$PROJECT" || true
aws iam detach-role-policy --role-name "$PROJECT-execution" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy || true
aws iam delete-role-policy --role-name "$PROJECT-task" --policy-name s3 || true
for role in execution task; do aws iam delete-role --role-name "$PROJECT-$role" || true; done
sleep 60  # the stopped tasks' network interfaces must be gone before their security groups can be deleted
PROXY_SG=$(sg "$PROJECT-proxies") SCRAPER_SG=$(sg "$PROJECT-scrapers")
aws ec2 revoke-security-group-ingress --group-id "$PROXY_SG" --protocol tcp --port 3128 --source-group "$SCRAPER_SG" >/dev/null || true
aws ec2 delete-security-group --group-id "$PROXY_SG" || true
aws ec2 delete-security-group --group-id "$SCRAPER_SG" || true
echo "Removed. The data stays in s3://$BUCKET."
