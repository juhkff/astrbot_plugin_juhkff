"""Exercise local image references through AstrBot's real OneBot serializers."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import unquote, urlsplit

from juhkff_modules import pixiv_browse as browse
from juhkff_modules import pixiv_push as pixiv
from PIL import Image as PILImage

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image
from astrbot.api.platform import MessageType
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)


class PixivFileTransportTests(IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name) / "图片 缓存"
        self.folder.mkdir()
        patcher = patch.object(pixiv, "data_dir", return_value=self.folder)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.images = []
        for index in range(26):
            image = self.folder / f"原图 {index}.png"
            PILImage.new("RGB", (2, 2), (index, 0, 0)).save(image)
            self.images.append(image)
        self.bot = SimpleNamespace(
            call_action=AsyncMock(return_value={"message_id": 1}),
            send_group_msg=AsyncMock(return_value={"message_id": 1}),
        )
        self.umo = "test:GroupMessage:1"

        async def deliver(origin, chain):
            await AiocqhttpMessageEvent.send_message(
                bot=cast(Any, self.bot),
                message_chain=chain,
                is_group=True,
                session_id=origin.split(":", 2)[-1],
            )
            return True

        self.plugin: Any = SimpleNamespace(
            global_enabled=True,
            config={
                "pixiv_push": {
                    "enable": True,
                    "print_log": False,
                    "image_transport": "file",
                    "ranking": {
                        "count": 1,
                        "pool_pages": 1,
                        "daily_push": {"enable": True, "groups": [self.umo]},
                    },
                    "random": {"count": 1},
                    "subscriptions": [{"user_id": "7", "groups": [self.umo]}],
                }
            },
            context=SimpleNamespace(
                send_message=AsyncMock(side_effect=deliver),
                get_platform_inst=Mock(
                    return_value=SimpleNamespace(
                        get_client=lambda: SimpleNamespace(
                            _wsr_api_clients={"123": object()}
                        )
                    )
                ),
            ),
        )
        self.module = pixiv.PixivPushModule(self.plugin)
        self.work: pixiv.Illustration = {
            "id": "7",
            "title": "Work",
            "author": "Artist",
            "page_count": 26,
            "x_restrict": 0,
            "original": "https://i.pximg.net/7_p0.png",
            "regular": "",
        }
        self.module._browse_candidates = AsyncMock(return_value=[("7", 1)])
        self.module._fetch_detail = AsyncMock(
            side_effect=lambda iid: {**self.work, "id": iid}
        )
        self.module._fetch_pages = AsyncMock(
            side_effect=lambda item: [item] * item["page_count"]
        )
        self.download = AsyncMock(
            side_effect=lambda item, source, index, **kwargs: self.images[index]
        )
        self.module._download = self.download

    async def asyncTearDown(self):
        await self.module.stop()

    async def test_manual_commands_send_one_forward_with_all_original_file_uris(self):
        with (
            patch.object(browse, "_FORWARD_BATCH_BYTES", 1),
            patch.object(
                Image,
                "convert_to_base64",
                AsyncMock(side_effect=AssertionError("Unexpected Base64 conversion")),
            ) as convert,
        ):
            for command in ("#pixiv日榜", "#pixiv随机", "#pixiv作品 7"):
                with self.subTest(command=command):
                    self.bot.call_action.reset_mock()
                    event = cast(
                        AstrMessageEvent,
                        SimpleNamespace(
                            message_str=command,
                            is_at_or_wake_command=True,
                            unified_msg_origin=self.umo,
                            get_message_type=lambda: MessageType.GROUP_MESSAGE,
                            get_self_id=lambda: "123",
                            plain_result=lambda text: MessageChain().message(text),
                        ),
                    )
                    result = await self.module.handle(event)
                    assert result is not None
                    await self.plugin.context.send_message(self.umo, result)
                    self.bot.call_action.assert_awaited_once()
                    call = self.bot.call_action.await_args
                    assert call is not None
                    self.assertEqual(call.args[0], "send_group_forward_msg")
                    payload = json.dumps(call.kwargs)
                    self.assertNotIn("base64://", payload)
                    self.assertLess(len(payload.encode()), 32 * 1024)
                    nodes = call.kwargs["messages"]
                    self.assertEqual(len(nodes), 1)
                    pages = nodes[0]["data"]["content"]
                    self.assertEqual(len(pages), 27)
                    header = pages[0]["data"]["content"]
                    self.assertEqual([part["type"] for part in header], ["text"])
                    self.assertIn("/artworks/7", header[0]["data"]["text"])
                    self.assertEqual(payload.count("/artworks/7"), 1)
                    self.assertNotRegex(header[0]["data"]["text"], r"第\s*\d+/\d+\s*张")
                    for index, node in enumerate(pages[1:]):
                        self.assertEqual(node["type"], "node")
                        content = node["data"]["content"]
                        self.assertEqual(len(content), 1)
                        uri = content[0]["data"]["file"]
                        self.assertEqual(uri, self.images[index].as_uri())
                        self.assertEqual(content[0]["type"], "image")
                        self.assertEqual(
                            Path(unquote(urlsplit(uri).path)).read_bytes(),
                            self.images[index].read_bytes(),
                        )
            convert.assert_not_awaited()

    async def test_single_page_uses_a_file_uri_in_normal_group_messages(self):
        work = {**self.work, "_browse_page": 4}
        with patch.object(
            Image,
            "convert_to_base64",
            AsyncMock(side_effect=AssertionError("Unexpected Base64 conversion")),
        ) as convert:
            result = await self.module._render_items([work], self.umo, bot_id="123")
            assert result is not None
            await self.plugin.context.send_message(self.umo, result)
            convert.assert_not_awaited()
        self.bot.call_action.assert_not_awaited()
        self.bot.send_group_msg.assert_awaited_once()
        call = self.bot.send_group_msg.await_args
        assert call is not None
        self.assertEqual(
            call.kwargs["message"][1],
            {"type": "image", "data": {"file": self.images[4].as_uri()}},
        )

    async def test_daily_ranking_keeps_one_forward_without_any_incoming_event(self):
        await self.module.push_daily_ranking()
        self.plugin.context.send_message.assert_awaited_once()
        self.bot.call_action.assert_awaited_once()
        call = self.bot.call_action.await_args
        assert call is not None
        self.assertEqual(call.args[0], "send_group_forward_msg")
        self.assertEqual(len(call.kwargs["messages"]), 1)
        self.assertEqual(len(call.kwargs["messages"][0]["data"]["content"]), 27)
        self.assertEqual(json.dumps(call.kwargs).count("/artworks/7"), 1)
        self.assertNotIn("base64://", json.dumps(call.kwargs))

    async def test_artist_push_uses_the_same_file_transport_and_advances_progress(self):
        self.module._artists["7"] = {"last_id": "1", "delivered": {}}
        self.module._fetch_ids = AsyncMock(return_value=["1", "2"])
        self.assertTrue(await self.module._check_artist("7"))
        self.bot.call_action.assert_awaited_once()
        call = self.bot.call_action.await_args
        assert call is not None
        self.assertEqual(len(call.kwargs["messages"]), 1)
        self.assertEqual(len(call.kwargs["messages"][0]["data"]["content"]), 27)
        self.assertEqual(json.dumps(call.kwargs).count("/artworks/2"), 1)
        self.assertNotIn("base64://", json.dumps(call.kwargs))
        self.assertEqual(self.module._artists["7"]["last_id"], "2")

    async def test_local_transport_keeps_restricted_filter_and_link_policy(self):
        work = {**self.work, "x_restrict": 1}
        self.assertIsNone(await self.module._render_items([work], self.umo))
        self.download.assert_not_awaited()
        self.module.cfg.update({"filter_r18": False, "r18_display": "link"})
        result = await self.module._render_items([work], self.umo, bot_id="123")
        assert result is not None
        await self.plugin.context.send_message(self.umo, result)
        self.bot.call_action.assert_awaited_once()
        call = self.bot.call_action.await_args
        assert call is not None
        pages = call.kwargs["messages"][0]["data"]["content"]
        self.assertEqual(len(pages), 27)
        self.assertIn("/artworks/7", pages[0]["data"]["content"][0]["data"]["text"])
        for node in pages[1:]:
            self.assertEqual(node["type"], "node")
            content = node["data"]["content"]
            self.assertEqual(len(content), 1)
            self.assertEqual(content[0]["type"], "text")
            self.assertTrue(
                content[0]["data"]["text"].startswith("https://i.pximg.net/")
            )
            self.assertNotIn("/artworks/7", content[0]["data"]["text"])
        self.download.assert_not_awaited()
