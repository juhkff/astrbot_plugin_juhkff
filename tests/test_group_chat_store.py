"""Redis lifecycle and command boundaries without a running server."""

from __future__ import annotations

import hashlib
from fnmatch import fnmatchcase
from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import AsyncMock, MagicMock, patch

from juhkff_modules.group_chat_store import RedisGroupChatStore


class RedisGroupChatStoreTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.prefix = "juhkff:test:group"
        self.values = {
            f"{self.prefix}:owner": "previous-process",
            f"{self.prefix}:messages:one": "old",
            f"{self.prefix}:messages:two": "old",
            "another-plugin:messages:one": "unrelated",
            f"{self.prefix}:settings": "retain",
        }
        self.client = MagicMock()
        self.client.ping = AsyncMock()
        self.client.get = AsyncMock(side_effect=self.values.get)
        self.client.set = AsyncMock(side_effect=self.values.__setitem__)
        self.client.unlink = AsyncMock(
            side_effect=lambda key: self.values.pop(key, None)
        )
        self.client.persist = AsyncMock(return_value=True)
        self.client.eval = AsyncMock(return_value=["past"])
        self.client.aclose = AsyncMock()

        async def scan_iter(*, match, count):
            for key in tuple(self.values):
                if fnmatchcase(key, match):
                    yield key

        self.client.scan_iter.side_effect = scan_iter
        self.factory = patch("redis.asyncio.Redis.from_url", return_value=self.client)
        self.factory_mock = self.factory.start()
        self.addCleanup(self.factory.stop)
        self.store = RedisGroupChatStore("redis://localhost/15", self.prefix + ":")

    def test_invalid_prefix_is_rejected_before_connecting(self):
        for prefix in (
            "",
            ":",
            "::",
            "juhkff:*",
            "juhkff:?",
            "juhkff:[ab]",
            "juhkff:]",
            "juhkff:\\",
        ):
            with self.subTest(prefix=prefix):
                with self.assertRaisesRegex(
                    ValueError, "prefix must be nonempty and contain no glob characters"
                ):
                    RedisGroupChatStore("redis://localhost/15", prefix)
        self.factory_mock.assert_not_called()

    async def test_start_persists_only_namespaced_messages_without_owner_changes(self):
        original = self.values.copy()
        await self.store.start()
        self.assertEqual(self.values, original)
        self.client.scan_iter.assert_called_once_with(
            match=f"{self.prefix}:messages:*", count=100
        )
        self.assertEqual(
            [call.args[0] for call in self.client.persist.await_args_list],
            [f"{self.prefix}:messages:one", f"{self.prefix}:messages:two"],
        )
        self.client.get.assert_not_awaited()
        self.client.set.assert_not_awaited()
        self.client.unlink.assert_not_awaited()
        self.client.flushdb.assert_not_called()
        self.client.flushall.assert_not_called()
        await self.store.start()
        self.factory_mock.assert_called_once()
        self.assertEqual(self.client.persist.await_count, 2)

    async def test_hot_reload_retains_messages_and_repeats_ttl_migration(self):
        original = self.values.copy()
        await self.store.start()
        await self.store.close()
        self.client.aclose.assert_awaited_once()
        replacement = RedisGroupChatStore("redis://localhost/15", self.prefix)
        await replacement.start()
        self.assertEqual(self.client.scan_iter.call_count, 2)
        self.assertEqual(self.client.persist.await_count, 4)
        self.client.unlink.assert_not_awaited()
        self.assertEqual(self.values, original)
        await replacement.close()

    async def test_start_failure_closes_connection_and_can_retry(self):
        self.client.ping.side_effect = ConnectionError("Redis unavailable")
        with self.assertRaisesRegex(ConnectionError, "Redis unavailable"):
            await self.store.start()
        self.assertIsNone(self.store.client)
        self.client.aclose.assert_awaited_once()
        self.client.ping.side_effect = None
        await self.store.start()
        self.assertIs(self.store.client, self.client)

    async def test_migration_failure_closes_connection_and_can_retry(self):
        self.client.persist.side_effect = ConnectionError("Migration interrupted")
        with self.assertRaisesRegex(ConnectionError, "Migration interrupted"):
            await self.store.start()
        self.assertIsNone(self.store.client)
        self.client.aclose.assert_awaited_once()
        self.client.unlink.assert_not_awaited()
        self.client.persist.side_effect = None
        await self.store.start()
        self.assertIs(self.store.client, self.client)
        await self.store.close()

    async def test_snapshot_and_replace_share_session_key_and_pass_bounds(self):
        await self.store.start()
        umo = "adapter:GroupMessage:private/group"
        expected_key = (
            f"{self.prefix}:messages:{hashlib.sha256(umo.encode()).hexdigest()}"
        )
        result = await self.store.snapshot_append(umo, "record-id", 1000.5, "new", 7)
        self.assertEqual(result, ["past"])
        args = self.client.eval.await_args.args
        self.assertEqual(args[1:], (1, expected_key, "record-id", 1000.5, "new", 7))
        await self.store.replace(umo, "record-id", "captioned")
        self.assertEqual(
            self.client.eval.await_args.args[1:],
            (1, expected_key, "record-id", "captioned"),
        )
        self.client.unlink.reset_mock()
        await self.store.clear(umo)
        self.client.unlink.assert_awaited_once_with(expected_key)
        await self.store.close()
        self.assertIsNone(self.store.client)
        self.client.aclose.assert_awaited_once()


if __name__ == "__main__":
    main()
