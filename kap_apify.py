"""Fetch the KAP disclosures of a date range on the Apify platform, as kap_history.py does on a computer.

Each finished disclosure goes to the named dataset DATASET_NAME, which keeps them across runs, instead of
the JSON Lines file. Disclosures already in the dataset are skipped, so running the same range again
continues where an earlier run stopped; a run that is aborted or timed out loses only the disclosures it
had not finished.

Input (see .actor/input_schema.json):
    {"start": "01.09.2026", "end": "30.09.2026"}
"""
import asyncio
import json

from apify import Actor

from kap_history import fetch_range, parse_day
from kap_output import as_json

DATASET_NAME = "kap-disclosures"


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}
        start, end = parse_day(actor_input["start"]), parse_day(actor_input["end"])
        if start > end:
            raise ValueError("start is after end")
        dataset = await Actor.open_dataset(name=DATASET_NAME)
        done = {item["disclosure_index"] async for item in dataset.iterate_items(fields=["disclosure_index"])}
        Actor.log.info(f"{len(done)} disclosures are already in the dataset '{DATASET_NAME}'; they are skipped.")

        loop = asyncio.get_running_loop()
        pushed = []

        def push(record: dict) -> None:  # called from kap_history's worker threads
            data = json.loads(as_json(record))  # dates as text, as in the JSON Lines file
            asyncio.run_coroutine_threadsafe(dataset.push_data(data), loop).result()
            pushed.append(record["disclosure_index"])

        await Actor.set_status_message(f"Fetching {start:%d.%m.%Y}-{end:%d.%m.%Y}.")
        await asyncio.to_thread(fetch_range, start, end, done, push)
        Actor.log.info(f"Pushed {len(pushed)} disclosures to the dataset '{DATASET_NAME}'.")
        await Actor.set_status_message(f"Done: {len(pushed)} disclosures pushed to '{DATASET_NAME}'.")


if __name__ == "__main__":
    asyncio.run(main())
