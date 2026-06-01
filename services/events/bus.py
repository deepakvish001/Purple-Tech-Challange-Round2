"""Redis Streams event bus.

`EventBus.publish` writes an Envelope to the `events` stream. Consumers use
`consume` with a consumer-group name; messages are ack'd via `ack`. The
contract is at-least-once delivery; consumer-side idempotency is keyed on
`event_id`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

import redis.asyncio as redis

from services.events.schemas import Envelope

log = logging.getLogger(__name__)

STREAM = "events"
MAXLEN = 100_000  # approximate cap — `~` lets Redis trim in chunks


class EventBus:
    """Thin async wrapper around a single Redis Streams stream."""

    def __init__(self, url: str, *, stream: str = STREAM, maxlen: int = MAXLEN) -> None:
        self._client: redis.Redis = redis.from_url(url, decode_responses=True)
        self._stream = stream
        self._maxlen = maxlen

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def stream(self) -> str:
        return self._stream

    async def ping(self) -> bool:
        return bool(await self._client.ping())

    async def publish(self, env: Envelope) -> str:
        """Append an envelope to the stream. Returns the assigned stream ID."""
        fields = {"data": env.to_json(), "type": env.type}
        stream_id: str = await self._client.xadd(
            self._stream, fields, maxlen=self._maxlen, approximate=True
        )
        return stream_id

    async def ensure_group(self, group: str, start: str = "$") -> None:
        """Create the consumer group if it does not exist. Idempotent."""
        try:
            await self._client.xgroup_create(self._stream, group, id=start, mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def consume(
        self,
        group: str,
        consumer: str,
        *,
        block_ms: int = 5_000,
        count: int = 100,
    ) -> AsyncIterator[tuple[str, Envelope]]:
        """Yield (stream_id, envelope) tuples. Caller must `ack` each one."""
        await self.ensure_group(group)
        while True:
            resp = await self._client.xreadgroup(
                groupname=group,
                consumername=consumer,
                streams={self._stream: ">"},
                count=count,
                block=block_ms,
            )
            if not resp:
                continue
            for _stream, messages in resp:
                for stream_id, fields in messages:
                    try:
                        env = Envelope.from_json(fields["data"])
                    except Exception:
                        log.exception("dropping malformed event id=%s", stream_id)
                        await self.ack(group, stream_id)
                        continue
                    yield stream_id, env

    async def ack(self, group: str, stream_id: str) -> None:
        await self._client.xack(self._stream, group, stream_id)

    async def recent(self, n: int = 50) -> list[Envelope]:
        """Return up to `n` most recent envelopes from the stream.

        Used by the API's debug endpoint. Not for high-throughput paths.
        """
        raw: list[tuple[str, dict[str, Any]]] = await self._client.xrevrange(
            self._stream, count=n
        )
        out: list[Envelope] = []
        for _id, fields in raw:
            try:
                out.append(Envelope.from_json(fields["data"]))
            except Exception:
                log.exception("skipping malformed event during recent()")
        return out

    async def stream_length(self) -> int:
        return int(await self._client.xlen(self._stream))
