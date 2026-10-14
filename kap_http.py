"""HTTP helper shared by the scrapers: spaces out requests, retries transient errors and waits out KAP's rate limit."""
import threading
import time

import httpx
from tenacity import retry, retry_if_exception_type, retry_if_not_exception_type, stop_after_attempt, wait_exponential
from tqdm import tqdm

# KAP answers 429 after roughly 100 page requests in a few minutes and blocks for several minutes.
# Its exact limit is unknown, so requests start REQUEST_INTERVAL apart and move further apart after each 429.
REQUEST_INTERVAL = 1.5  # seconds between the starts of two requests, across all threads
RATE_LIMIT_PAUSE = 300  # seconds
MAX_RATE_LIMIT_PAUSES = 3  # consecutive pauses before giving up
# KAP may also throttle a response to a trickle (~10 KB/s) instead of answering 429. The client's
# read timeout never fires while bytes keep arriving, so each download gets a total time limit.
# Attachments always come slowly (~10 KB/s per connection, less with many at once), so a big one, such
# as a bank's 4.7 MB financial report, gets as long as it needs at MIN_DOWNLOAD_RATE.
DOWNLOAD_DEADLINE = 180  # seconds
MIN_DOWNLOAD_RATE = 4 * 1024  # bytes per second
# KAP sometimes stops sending in the middle of a file. A download that gets no byte for 30 s is dropped
# and tried again, instead of waiting out a long read timeout.
ATTACHMENT_TIMEOUT = httpx.Timeout(120, read=30)
JAVA_BYTE_ARRAY_HEADER = b"\xac\xed\x00\x05ur\x00\x02[B"  # KAP sends files as a serialized Java byte[]


class RateLimited(Exception):
    pass


class TooSlow(Exception):
    pass


class Throttle:
    """Spaces out the requests of all threads, and holds them all while KAP rate limits."""

    def __init__(self, interval: float):
        self.interval = interval
        self.next_start = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        """Block until this thread may send its request."""
        with self.lock:
            start = max(time.monotonic(), self.next_start)
            self.next_start = start + self.interval
        time.sleep(max(0.0, start - time.monotonic()))

    def pause(self, seconds: float) -> None:
        """Hold every thread for `seconds`, then send requests 1.5 times further apart."""
        with self.lock:
            now = time.monotonic()
            if self.next_start > now + seconds / 2:
                return  # another thread already paused for the same block
            self.next_start = now + seconds
            self.interval *= 1.5
            tqdm.write(f"KAP rate limit hit, pausing {seconds // 60} minutes; "
                       f"then one request every {self.interval:.1f} s.")


throttle = Throttle(REQUEST_INTERVAL)


def file_size(data: bytes) -> int | None:
    """Size of the file in a KAP download: the 4-byte length after the 23-byte Java header. None if not a file."""
    if data.startswith(JAVA_BYTE_ARRAY_HEADER) and len(data) >= 27:
        return int.from_bytes(data[23:27], "big")
    return None


@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=2, max=30),  # never retry Ctrl+C (not an Exception)
       retry=retry_if_exception_type(Exception) & retry_if_not_exception_type((RateLimited, TooSlow)), reraise=True)
def _get_once(client: httpx.Client, url: str) -> bytes:
    throttle.wait()
    started = time.monotonic()
    limit = DOWNLOAD_DEADLINE
    with client.stream("GET", url) as r:
        if r.status_code == 429:
            raise RateLimited
        r.raise_for_status()
        body = bytearray()
        for chunk in r.iter_bytes():
            body += chunk
            if (size := file_size(body)) is not None:
                limit = max(DOWNLOAD_DEADLINE, size / MIN_DOWNLOAD_RATE)
            if time.monotonic() - started > limit:
                raise TooSlow(f"download took longer than {limit:.0f} s; KAP is throttling")
        return bytes(body)


def kap_get(client: httpx.Client, url: str) -> bytes:
    """GET a KAP url and return the body. On 429, pause and retry; raise RateLimited if KAP keeps blocking."""
    for _ in range(MAX_RATE_LIMIT_PAUSES):
        try:
            return _get_once(client, url)
        except RateLimited:
            throttle.pause(RATE_LIMIT_PAUSE)
    return _get_once(client, url)
