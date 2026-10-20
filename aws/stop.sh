#!/usr/bin/env bash
# Stop the running tasks: all of them, or only the proxies with `aws/stop.sh proxy` once the scrapers are done.
# A stopped scraper uploads its file first, so starting its range again continues where it stopped.
set -euo pipefail
cd "$(dirname "$0")"
source config.sh
for kind in ${1:-scraper proxy}; do
    for task in $(aws ecs list-tasks --cluster "$PROJECT" --family "$PROJECT-$kind" --query 'taskArns' --output text); do
        aws ecs stop-task --cluster "$PROJECT" --task "$task" --query 'task.taskArn' --output text
    done
done
