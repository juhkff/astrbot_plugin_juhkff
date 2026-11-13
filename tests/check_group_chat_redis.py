"""Exercise actual Lua storage on a temporary Redis over an isolated Unix socket."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from juhkff_modules.group_chat_store import RedisGroupChatStore
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError


async def check(url):
    """Verify persistent storage, legacy TTL migration and namespace isolation.

    Args:
        url: URL of the isolated test server.
    """
    probe = Redis.from_url(url, decode_responses=True)
    try:
        for attempt in range(100):
            try:
                await probe.ping()
                break
            except RedisConnectionError:
                if attempt == 99:
                    raise
                await asyncio.sleep(0.02)
            except OSError:
                if attempt == 99:
                    raise
                await asyncio.sleep(0.02)
        now = time.time()
        key_a = f"test:group:messages:{hashlib.sha256(b'a').hexdigest()}"
        key_b = f"test:group:messages:{hashlib.sha256(b'b').hexdigest()}"
        await probe.set("test:group:owner", "previous-process")
        await probe.set("other:messages:keep", "untouched", ex=600)
        await probe.set("test:group:settings", "retain", ex=600)
        unrelated_expiry = {
            key: await probe.pexpiretime(key)
            for key in ("other:messages:keep", "test:group:settings")
        }
        legacy = json.dumps({"id": "legacy", "tick": 1, "text": "old context"})
        await probe.rpush(key_a, legacy)
        await probe.expire(key_a, 600)
        store = RedisGroupChatStore(url, "test:group")
        await store.start()
        assert await probe.lrange(key_a, 0, -1) == [legacy]
        assert await probe.ttl(key_a) == -1
        assert await probe.get("test:group:owner") == "previous-process"
        assert await store.snapshot_append("a", "1", now, "first", 2) == ["old context"]
        await store.replace("a", "1", "caption")
        assert await store.snapshot_append("a", "2", now, "second", 2) == [
            "old context",
            "caption",
        ]
        assert await probe.llen(key_a) == 2
        await store.replace("a", "legacy", "must not revive trimmed record")
        assert [json.loads(row)["id"] for row in await probe.lrange(key_a, 0, -1)] == [
            "1",
            "2",
        ]
        # Snapshot reads the last limit rows even when the previous cap was larger.
        assert await store.snapshot_append("a", "3", now, "third", 1) == ["second"]
        assert await probe.llen(key_a) == 1
        assert await store.snapshot_append("b", "4", 1, "ancient", 2) == []
        assert await store.snapshot_append("b", "5", now, "fresh", 2) == ["ancient"]
        # Appending also cancels a TTL introduced after startup.
        await probe.expire(key_a, 600)
        assert await store.snapshot_append("a", "6", now, "latest", 2) == ["third"]
        assert await probe.ttl(key_a) == -1
        before_reset = await probe.lrange(key_a, 0, -1)
        await store.clear("b")
        await store.replace("b", "5", "must not revive reset record")
        assert not await probe.exists(key_b)
        assert await probe.lrange(key_a, 0, -1) == before_reset
        await store.close()
        hot = RedisGroupChatStore(url, "test:group")
        await hot.start()
        assert await hot.snapshot_append("a", "7", now, "hot", 2) == [
            "third",
            "latest",
        ]
        assert await hot.snapshot_append("b", "8", now, "after reset", 2) == []
        keys = [key async for key in probe.scan_iter(match="test:group:messages:*")]
        before_restart = {key: await probe.lrange(key, 0, -1) for key in keys}
        assert len(keys) == 2
        assert all([await probe.ttl(key) == -1 for key in keys])
        await hot.close()
        # A fresh interpreter must read existing context without clearing it.
        code = """
import asyncio
import sys
from juhkff_modules.group_chat_store import RedisGroupChatStore
async def run():
    store = RedisGroupChatStore(sys.argv[1], 'test:group')
    await store.start()
    assert await store.snapshot_append('a', '9', 1, 'child', 2) == ['latest', 'hot']
    await store.close()
asyncio.run(run())
"""
        child = await asyncio.create_subprocess_exec(sys.executable, "-c", code, url)
        assert await child.wait() == 0
        assert await probe.lrange(key_b, 0, -1) == before_restart[key_b]
        assert [
            json.loads(row)["text"] for row in await probe.lrange(key_a, 0, -1)
        ] == [
            "hot",
            "child",
        ]
        assert all([await probe.ttl(key) == -1 for key in keys])
        assert await probe.get("test:group:owner") == "previous-process"
        assert await probe.get("other:messages:keep") == "untouched"
        assert await probe.get("test:group:settings") == "retain"
        for key, expiry in unrelated_expiry.items():
            assert await probe.pexpiretime(key) == expiry
        print(
            "Redis integration passed: legacy TTL migration, old timestamps, "
            "snapshot/replace, bounds, reset isolation, reload, fresh process "
            "retention, unrelated TTL preservation"
        )
    finally:
        await probe.aclose()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="juhkff-redis-") as directory:
        socket = str(Path(directory) / "redis.sock")
        server = subprocess.Popen(
            [
                "redis-server",
                "--port",
                "0",
                "--unixsocket",
                socket,
                "--save",
                "",
                "--appendonly",
                "no",
                "--dir",
                directory,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=os.environ.copy(),
        )
        try:
            asyncio.run(check(f"unix://{socket}"))
        finally:
            server.terminate()
            server.wait(timeout=5)
