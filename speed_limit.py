# speed_limit.py
# Token-bucket throttle — zero overhead when speed_limit is disabled (0 / unlimited).
import asyncio
import time

from main import LINKS

_buckets: dict = {}

# Soft floor so a misconfigured tiny limit still works without freezing the event loop.
MIN_RATE = 8 * 1024          # 8 KB/s minimum
MIN_BURST = 64 * 1024        # 64 KB burst


class _Bucket:
    __slots__ = ("rate", "capacity", "tokens", "last")

    def __init__(self, rate_bytes_per_sec: float):
        self.rate = max(rate_bytes_per_sec, MIN_RATE)
        # Burst = 1 second of traffic (or at least MIN_BURST) so short spikes stay smooth.
        self.capacity = max(self.rate, MIN_BURST)
        self.tokens = self.capacity
        self.last = time.monotonic()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self.last
        if elapsed > 0:
            self.last = now
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)

    async def consume(self, n: int):
        while True:
            self._refill()
            if self.tokens >= n:
                self.tokens -= n
                return
            deficit = n - self.tokens
            wait = deficit / self.rate
            # Cap sleep so we stay responsive if rate is tiny.
            await asyncio.sleep(min(max(wait, 0.002), 0.25))


def _get_bucket(uuid: str, rate: int) -> _Bucket:
    b = _buckets.get(uuid)
    if b is None or b.rate != max(rate, MIN_RATE):
        b = _Bucket(rate)
        _buckets[uuid] = b
    return b


async def throttle(uuid: str, nbytes: int):
    """No-op when the link has no speed limit (the common case)."""
    if nbytes <= 0:
        return
    link = LINKS.get(uuid)
    if not link:
        return
    rate = int(link.get("speed_limit_bytes") or 0)
    if rate <= 0:
        return
    bucket = _get_bucket(uuid, rate)
    await bucket.consume(nbytes)


def reset_bucket(uuid: str):
    _buckets.pop(uuid, None)
