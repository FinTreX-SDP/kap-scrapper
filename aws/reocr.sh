#!/usr/bin/env bash
# One-off: OCR the scanned pages that the OCR page limit left out (kap_reocr.py), in PARTS tasks of VCPUS
# each. TODO is the list made from the downloaded records; largest first, so the parts get similar work.
# Results: s3://BUCKET/reocr/results-<part>.jsonl. Running it again continues where the tasks stopped.
# Usage: aws/reocr.sh data/reocr_todo.json [PARTS=3] [VCPUS=8]
set -euo pipefail
cd "$(dirname "$0")"
source config.sh
TODO=$1 PARTS=${2:-3} VCPUS=${3:-8}

python -c "import json, sys; todo = json.load(open(sys.argv[1], encoding='utf-8')); \
    json.dump(sorted(todo, key=lambda a: -a['skipped_pages']), open(sys.argv[2], 'w', encoding='utf-8'))" \
    "../$TODO" sorted_todo.json
aws s3 cp sorted_todo.json "s3://$BUCKET/reocr/todo.json" --only-show-errors
rm sorted_todo.json

SUBNETS=$(aws ec2 describe-subnets --filters Name=default-for-az,Values=true --query 'Subnets[].SubnetId' --output text | tr '\t' ',')
for ((part = 0; part < PARTS; part++)); do
    overrides='{"cpu":"'$((VCPUS * 1024))'","memory":"'$((VCPUS * 2048))'","containerOverrides":[{"name":"scraper",
        "command":["python","kap_reocr.py"],"environment":[{"name":"PART","value":"'$part'"},{"name":"PARTS","value":"'$PARTS'"}]}]}'
    aws ecs run-task --cluster "$PROJECT" --launch-type FARGATE --task-definition "$PROJECT-scraper" \
        --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$(sg "$PROJECT-scrapers")],assignPublicIp=ENABLED}" \
        --overrides "$overrides" --query 'tasks[0].taskArn' --output text
done
echo "Follow: aws logs tail /ecs/$PROJECT --follow --region $AWS_DEFAULT_REGION"
