"""Fetch the KAP disclosures of a date range on the Apify platform, as kap_history.py does on a computer.

A run's disk is small and lost when the run ends, so kap_history.py runs one calendar month at a time,
newest first, each month with a fresh database. Every disclosure it appends to the JSON Lines file is
pushed to the named dataset DATASET_NAME, which keeps them across runs. Disclosures already in the
dataset are not fetched again, so running the same range again continues where an earlier run stopped;
a run that is aborted or timed out loses at most the disclosures it had not pushed yet.

Input (see .actor/input_schema.json):
    {"start": "01.09.2026", "end": "30.09.2026"}
"""
import asyncio
import contextlib
import json
import shutil
import sys
from pathlib import Path

import duckdb
from apify import Actor

from kap_disclosures import months
from kap_history import parse_day
from kap_output import DATA_DIR, DB_PATH, JSONL_PATH, JSONL_SCHEMA

DATASET_NAME = "kap-disclosures"
PUSH_INTERVAL = 10  # seconds between pushes of newly finished disclosures to the dataset


async def push_new_lines(dataset, offset: int) -> tuple[int, int]:
    """Push the lines appended to the JSON Lines file after byte `offset`. Returns the offset after them
    and how many were pushed."""
    if not JSONL_PATH.exists():
        return offset, 0
    with JSONL_PATH.open("rb") as f:
        f.seek(offset)
        data = f.read()
    data = data[:data.rfind(b"\n") + 1]  # a line still being written waits for the next push
    records = [json.loads(line) for line in data.splitlines()]
    if records:
        await dataset.push_data(records)
    return offset + len(data), len(records)


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}
        start, end = parse_day(actor_input["start"]), parse_day(actor_input["end"])
        if start > end:
            raise ValueError("start is after end")
        dataset = await Actor.open_dataset(name=DATASET_NAME)
        done = [item["disclosure_index"] async for item in dataset.iterate_items(fields=["disclosure_index"])]
        Actor.log.info(f"{len(done)} disclosures are already in the dataset '{DATASET_NAME}'; they are skipped.")

        pushed = 0
        for first, last in months(start, end):
            await Actor.set_status_message(f"Fetching {first:%d.%m.%Y}-{last:%d.%m.%Y}, {pushed} disclosures pushed so far.")
            shutil.rmtree(DATA_DIR, ignore_errors=True)
            DATA_DIR.mkdir()
            con = duckdb.connect(str(DB_PATH))
            con.execute(JSONL_SCHEMA)  # kap_history.py skips the disclosures in this table
            con.execute("INSERT INTO jsonl_exported SELECT unnest(?::INTEGER[])", [done])
            con.close()

            process = await asyncio.create_subprocess_exec(
                sys.executable, str(Path(__file__).with_name("kap_history.py")),
                "--start", f"{first:%d.%m.%Y}", "--end", f"{last:%d.%m.%Y}")
            offset = 0
            while process.returncode is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), PUSH_INTERVAL)
                offset, count = await push_new_lines(dataset, offset)
                pushed += count
            if process.returncode:
                raise RuntimeError(f"kap_history.py failed for {first:%d.%m.%Y}-{last:%d.%m.%Y}; "
                                   "run again to continue")

        Actor.log.info(f"Pushed {pushed} disclosures to the dataset '{DATASET_NAME}'.")
        await Actor.set_status_message(f"Done: {pushed} disclosures pushed to '{DATASET_NAME}'.")


if __name__ == "__main__":
    asyncio.run(main())
