"""Redis-backed rolling context retained until trimmed or explicitly cleared."""

from __future__ import annotations

import asyncio
import hashlib


class RedisGroupChatStore:
    """Keep only this plugin's context in a dedicated Redis namespace."""

    def __init__(self, url, prefix):
        self.url = url
        self.prefix = prefix.rstrip(":")
        if not self.prefix or any(char in self.prefix for char in "*?[]\\"):
            raise ValueError(
                "Redis context prefix must be nonempty and contain no glob characters"
            )
        self.client = None
        self.lock = asyncio.Lock()

    async def start(self):
        """Connect and remove legacy expirations without deleting stored history."""
        async with self.lock:
            if self.client is not None:
                return
            from redis.asyncio import Redis

            client = Redis.from_url(
                self.url,
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=3,
            )
            try:
                await client.ping()
                # Migrate surviving keys from the former expiring history format.
                async for key in client.scan_iter(
                    match=f"{self.prefix}:messages:*", count=100
                ):
                    await client.persist(key)
            except BaseException:
                await client.aclose()
                raise
            self.client = client

    async def snapshot_append(self, umo, record_id, tick, text, limit):
        """Atomically return prior messages and append one arrival.

        Args:
            umo: Official session identity.
            record_id: Unique record identifier for later caption replacement.
            tick: Unix timestamp of message arrival.
            text: Formatted text or provisional fallback.
            limit: Maximum number of retained messages.

        Returns:
            Prior context strings, excluding this arrival.
        """
        key = f"{self.prefix}:messages:{hashlib.sha256(umo.encode()).hexdigest()}"
        return await self.client.eval(
            """
            local rows = redis.call('LRANGE', KEYS[1], -tonumber(ARGV[4]), -1)
            local result = {}
            for _, raw in ipairs(rows) do
                table.insert(result, cjson.decode(raw).text)
            end
            redis.call('RPUSH', KEYS[1], cjson.encode({id=ARGV[1], tick=tonumber(ARGV[2]), text=ARGV[3]}))
            redis.call('LTRIM', KEYS[1], -tonumber(ARGV[4]), -1)
            redis.call('PERSIST', KEYS[1])
            return result
            """,
            1,
            key,
            record_id,
            tick,
            text,
            limit,
        )

    async def replace(self, umo, record_id, text):
        """Replace a provisional record without changing ordering or recreating deleted records.

        Args:
            umo: Official session identity.
            record_id: Previously appended record identifier.
            text: Completed caption and metadata.
        """
        key = f"{self.prefix}:messages:{hashlib.sha256(umo.encode()).hexdigest()}"
        await self.client.eval(
            """
            for i, raw in ipairs(redis.call('LRANGE', KEYS[1], 0, -1)) do
                local row = cjson.decode(raw)
                if row.id == ARGV[1] then
                    row.text = ARGV[2]
                    redis.call('LSET', KEYS[1], i - 1, cjson.encode(row))
                    break
                end
            end
            return 1
            """,
            1,
            key,
            record_id,
            text,
        )

    async def clear(self, umo):
        """Clear one session following an explicit reset.

        Args:
            umo: Official session identity.
        """
        key = f"{self.prefix}:messages:{hashlib.sha256(umo.encode()).hexdigest()}"
        await self.client.unlink(key)

    async def close(self):
        """Close the connection without deleting stored records."""
        if self.client is not None:
            await self.client.aclose()
            self.client = None
