"""HTTP helper shared by the scrapers: retries transient errors and waits out KAP's rate limit."""
import time

import httpx
from tenacity import retry, retry_if_exception_type, retry_if_not_exception_type, stop_after_attempt, wait_exponential
from tqdm import tqdm

# KAP answers 429 after roughly 100 page requests in a few minutes and blocks for several minutes.
RATE_LIMIT_PAUSE = 300  # seconds
MAX_RATE_LIMIT_PAUSES = 3  # consecutive pauses before giving up
# KAP may also throttle a response to a trickle (~10 KB/s) instead of answering 429. The client's
# read timeout never fires while bytes keep arriving, so each download gets a total time limit.
DOWNLOAD_DEADLINE = 180  # seconds


class RateLimited(Exception):
    pass


class TooSlow(Exception):
    pass


@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=2, max=30),  # never retry Ctrl+C (not an Exception)
       retry=retry_if_exception_type(Exception) & retry_if_not_exception_type((RateLimited, TooSlow)), reraise=True)
def _get_once(client: httpx.Client, url: str) -> bytes:
    deadline = time.monotonic() + DOWNLOAD_DEADLINE
    with client.stream("GET", url) as r:
        if r.status_code == 429:
            raise RateLimited
        r.raise_for_status()
        chunks = []
        for chunk in r.iter_bytes():
            chunks.append(chunk)
            if time.monotonic() > deadline:
                raise TooSlow(f"download took longer than {DOWNLOAD_DEADLINE} s; KAP is throttling")
        return b"".join(chunks)


def kap_get(client: httpx.Client, url: str) -> bytes:
    """GET a KAP url and return the body. On 429, pause and retry; raise RateLimited if KAP keeps blocking."""
    for _ in range(MAX_RATE_LIMIT_PAUSES):
        try:
            return _get_once(client, url)
        except RateLimited:
            tqdm.write(f"KAP rate limit hit, pausing {RATE_LIMIT_PAUSE // 60} minutes...")
            time.sleep(RATE_LIMIT_PAUSE)
    return _get_once(client, url)
