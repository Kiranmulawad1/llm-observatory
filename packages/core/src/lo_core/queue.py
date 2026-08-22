"""Queue introspection.

Arq stores queued jobs in a Redis sorted set scored by their earliest run time,
so depth is a `ZCARD`. Reading it directly rather than through arq's client
keeps this usable from the API process, which has a Redis pool but is not an
arq worker.
"""

from __future__ import annotations

from lo_core.ratelimit import get_client

# arq's default. Deferred and retried jobs live in the same structure, so this
# counts "work the queue is holding", which is the number worth alerting on.
DEFAULT_QUEUE = "arq:queue"


async def queue_depth(queue: str = DEFAULT_QUEUE) -> dict[str, int]:
    """Jobs currently waiting, keyed by queue name."""
    client = get_client()
    return {queue: int(await client.zcard(queue))}
