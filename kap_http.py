"""HTTP helper shared by the scrapers: retries transient errors and waits out KAP's rate limit."""
import itertools
import threading
import time

import httpx
from tenacity import retry, retry_if_exception_type, retry_if_not_exception_type, stop_after_attempt

# KAP blocks an IP that sends too many requests. It has answered 429, and its firewall drops every
# connection without an answer. On 15.10.2026 it blocked after ~100 requests, however fast they came,
# for ~3.5 minutes each time. So requests are not spaced out; at a block every thread pauses.
# Through proxies each request comes from another IP (see connection_options), so the gate is turned off
# and a blocked request is tried again at once.
BLOCKED_STATUSES = {403, 429, 503}  # how KAP or its firewall refuses too many requests
RATE_LIMIT_PAUSE = 240  # seconds
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


class Gate:
    """Holds every thread while KAP blocks."""

    def __init__(self):
        self.open_at = 0.0
        self.lock = threading.Lock()
        self.enabled = True

    def wait(self) -> None:
        """Block until the pause, if any, is over."""
        time.sleep(max(0.0, self.open_at - time.monotonic()))

    def pause(self, seconds: float, reason: str) -> None:
        """Hold every thread for `seconds`."""
        if not self.enabled:
            print(f"[{time.strftime('%H:%M:%S')}] KAP blocked a request ({reason}), trying again from another IP.",
                  flush=True)
            return
        with self.lock:
            now = time.monotonic()
            if self.open_at > now + seconds / 2:
                return  # another thread already paused for the same block
            self.open_at = now + seconds
        print(f"[{time.strftime('%H:%M:%S')}] KAP blocked a request ({reason}), pausing {seconds // 60} minutes.",
              flush=True)


gate = Gate()


class RotatingProxies(httpx.BaseTransport):
    """Sends each request through the next proxy in turn, on a new connection: Apify Proxy gives each new
    connection another IP, and a list of proxies spreads the requests over their IPs."""

    def __init__(self, proxies: list[str]):
        self.transports = [httpx.HTTPTransport(proxy=p, limits=httpx.Limits(max_keepalive_connections=0))
                           for p in proxies]
        self.turn = itertools.cycle(self.transports)
        self.lock = threading.Lock()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            transport = next(self.turn)
        return transport.handle_request(request)

    def close(self) -> None:
        for transport in self.transports:
            transport.close()


def connection_options(proxies: list[str] | None) -> dict:
    """httpx.Client options for requests through `proxies`, if any. HTTP/2, which would share one connection
    among all requests, is then not used: each request, a retry of a blocked one too, comes from a new IP."""
    if not proxies:
        return {"http2": True}
    return {"transport": RotatingProxies(proxies)}


def file_size(data: bytes) -> int | None:
    """Size of the file in a KAP download: the 4-byte length after the 23-byte Java header. None if not a file."""
    if data.startswith(JAVA_BYTE_ARRAY_HEADER) and len(data) >= 27:
        return int.from_bytes(data[23:27], "big")
    return None


def _log_retry(state) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] Retrying {state.args[1]}: {state.outcome.exception()!r}", flush=True)


@retry(stop=stop_after_attempt(5),  # never retry Ctrl+C (not an Exception)
       retry=retry_if_exception_type(Exception) & retry_if_not_exception_type((RateLimited, TooSlow)), reraise=True,
       before_sleep=_log_retry)
def _get_once(client: httpx.Client, url: str) -> bytes:
    gate.wait()
    started = time.monotonic()
    limit = DOWNLOAD_DEADLINE
    try:
        r = client.send(client.build_request("GET", url), stream=True)
    except httpx.RemoteProtocolError as e:  # dropped before any answer, unlike a stall in the middle of a file
        raise RateLimited("connection dropped") from e
    try:
        if r.status_code in BLOCKED_STATUSES:
            raise RateLimited(f"HTTP {r.status_code}")
        r.raise_for_status()
        body = bytearray()
        for chunk in r.iter_bytes():
            body += chunk
            if (size := file_size(body)) is not None:
                limit = max(DOWNLOAD_DEADLINE, size / MIN_DOWNLOAD_RATE)
            if time.monotonic() - started > limit:
                raise TooSlow(f"download took longer than {limit:.0f} s; KAP is throttling")
        return bytes(body)
    finally:
        r.close()


def kap_get(client: httpx.Client, url: str) -> bytes:
    """GET a KAP url and return the body. When KAP blocks, pause and retry; raise RateLimited if it keeps blocking."""
    for _ in range(MAX_RATE_LIMIT_PAUSES):
        try:
            return _get_once(client, url)
        except RateLimited as e:
            gate.pause(RATE_LIMIT_PAUSE, e.args[0])
    return _get_once(client, url)

