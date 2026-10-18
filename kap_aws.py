"""Fetch the KAP disclosures of a date range on AWS (an ECS Fargate task), as kap_history.py does on a computer.

The JSON Lines file is copied to S3 every UPLOAD_INTERVAL seconds and at the end, under a key named after
the range. A task that finds that key in S3 downloads it first and skips its disclosures, so starting the
same range again continues where an earlier task stopped; a task that is killed loses at most the
disclosures of its last UPLOAD_INTERVAL seconds.

Requests go through the proxies in PROXIES, one after another, as KAP blocks an IP that sends too many.
aws/run.sh starts the proxies and the tasks.

Usage:
    BUCKET=my-bucket PROXIES=http://172.31.1.10:3128,http://172.31.1.11:3128 \\
        python kap_aws.py --start 01.01.2025 --end 31.12.2025
"""
import argparse
import os
import signal
import threading

import boto3
from botocore.exceptions import ClientError

from kap_history import fetch_range, parse_day
from kap_output import DATA_DIR, JSONL_PATH, append_record, read_written, written_indexes
from kap_watch import log

UPLOAD_INTERVAL = 300  # seconds
# As on Apify: many downloads at once make up for KAP's ~10 KB/s per connection, and reading the files is
# limited separately (kap_watch.EXTRACT_SLOTS), so memory stays bounded.
WORKERS = 48


def stop(signum, frame) -> None:
    raise KeyboardInterrupt  # ECS stops a task with SIGTERM; stop as on Ctrl+C, so the file is uploaded


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch the KAP disclosures of a date range into S3.")
    parser.add_argument("--start", metavar="DD.MM.YYYY", type=parse_day, required=True, help="first day")
    parser.add_argument("--end", metavar="DD.MM.YYYY", type=parse_day, required=True, help="last day")
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start is after --end")
    bucket = os.environ["BUCKET"]
    proxies = [p for p in os.environ.get("PROXIES", "").split(",") if p] or None
    key = f"disclosures/{args.start:%Y-%m-%d}_{args.end:%Y-%m-%d}.jsonl"
    s3 = boto3.client("s3")
    signal.signal(signal.SIGTERM, stop)

    DATA_DIR.mkdir(exist_ok=True)
    try:
        s3.download_file(bucket, key, str(JSONL_PATH))
    except ClientError as e:
        if e.response["Error"]["Code"] != "404":
            raise
    done = written_indexes()
    log(f"{len(done)} disclosures are already in s3://{bucket}/{key}; they are skipped. "
        f"{len(proxies or [])} proxies.")

    def upload() -> None:
        s3.put_object(Bucket=bucket, Key=key, Body=read_written())

    finished = threading.Event()

    def upload_regularly() -> None:
        while not finished.wait(UPLOAD_INTERVAL):
            try:
                upload()
            except Exception as e:  # tried again at the next interval and at the end
                log(f"Upload to S3 failed: {e!r}")

    threading.Thread(target=upload_regularly, daemon=True).start()
    added = []

    def write(record: dict) -> None:
        append_record(record)
        added.append(record["disclosure_index"])

    log(f"Fetching disclosures published from {args.start:%d.%m.%Y} to {args.end:%d.%m.%Y}.")
    try:
        fetch_range(args.start, args.end, done, write, proxies, WORKERS)
        log("Done.")
    except KeyboardInterrupt:
        log("Stopping. Start the same range again to continue.")
    finally:
        finished.set()
        upload()
    log(f"Added {len(added)} disclosures to s3://{bucket}/{key}.")


if __name__ == "__main__":
    main()
