#!/usr/bin/env bash
# Start PROXIES squid proxies, then PARTS scraper tasks, one for each equal part of START..END. Each scraper
# writes s3://BUCKET/disclosures/<first day>_<last day>.jsonl; running the same command again continues
# where the tasks stopped. Proxies stop by themselves after 12 hours; stop.sh stops them sooner.
# Usage: aws/run.sh 18.10.2023 17.10.2026 [PARTS=8] [PROXIES=16]
set -euo pipefail
cd "$(dirname "$0")"
source config.sh
START=$1 END=$2 PARTS=${3:-8} PROXY_COUNT=${4:-16}

SUBNETS=$(aws ec2 describe-subnets --filters Name=default-for-az,Values=true --query 'Subnets[].SubnetId' --output text | tr '\t' ',')
network() { echo "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$(sg "$PROJECT-$1")],assignPublicIp=ENABLED}"; }

echo "Starting $PROXY_COUNT proxies"
PROXY_TASKS=()
for ((left = PROXY_COUNT; left > 0; left -= 10)); do  # run-task starts at most 10 at a time
    PROXY_TASKS+=($(aws ecs run-task --cluster "$PROJECT" --launch-type FARGATE --task-definition "$PROJECT-proxy" \
        --count $((left < 10 ? left : 10)) --network-configuration "$(network proxies)" --query 'tasks[].taskArn' --output text))
done
aws ecs wait tasks-running --cluster "$PROJECT" --tasks "${PROXY_TASKS[@]}"
PROXIES=$(aws ecs describe-tasks --cluster "$PROJECT" --tasks "${PROXY_TASKS[@]}" \
    --query "tasks[].attachments[0].details[?name=='privateIPv4Address'].value[]" --output text \
    | tr '\t' '\n' | sed 's|.*|http://&:3128|' | paste -sd,)
echo "Proxies: $PROXIES"
sleep 15  # squid listens a few seconds after its task is running

echo "Starting $PARTS scrapers"
for part in $(python - "$START" "$END" "$PARTS" <<'EOF'
import sys
from datetime import datetime, timedelta
start, end = (datetime.strptime(day, "%d.%m.%Y") for day in sys.argv[1:3])
parts, days = int(sys.argv[3]), (end - start).days + 1
for i in range(parts):
    first, last = start + timedelta(days=days * i // parts), start + timedelta(days=days * (i + 1) // parts - 1)
    print(f"{first:%d.%m.%Y}_{last:%d.%m.%Y}")
EOF
); do
    part=${part%$'\r'}
    first=${part%_*} last=${part#*_}
    overrides='{"containerOverrides":[{"name":"scraper","command":["python","kap_aws.py","--start","'$first'","--end","'$last'"],
        "environment":[{"name":"PROXIES","value":"'$PROXIES'"}]}]}'
    aws ecs run-task --cluster "$PROJECT" --launch-type FARGATE --task-definition "$PROJECT-scraper" \
        --network-configuration "$(network scrapers)" --overrides "$overrides" --query 'tasks[0].taskArn' --output text
    echo "  $first - $last"
done
echo "Follow:  aws logs tail /ecs/$PROJECT --follow --region $AWS_DEFAULT_REGION"
echo "Status:  aws/status.sh      Stop all: aws/stop.sh"
