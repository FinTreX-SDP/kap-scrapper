#!/usr/bin/env bash
# Running scraper and proxy tasks, and the files the scrapers have written to S3 so far.
set -euo pipefail
cd "$(dirname "$0")"
source config.sh
for kind in scraper proxy; do
    echo "$kind tasks running: $(aws ecs list-tasks --cluster "$PROJECT" --family "$PROJECT-$kind" --query 'length(taskArns)' --output text)"
done
aws s3 ls "s3://$BUCKET/disclosures/" --human-readable || true
