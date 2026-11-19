"""Offline coverage for Pixiv browse APIs, content policy, and rendering."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import AsyncMock, Mock, patch

from juhkff_modules import pixiv_browse as browse

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image, Nodes, Plain
from astrbot.api.platform import MessageType


class BrowseHost(browse.PixivBrowseMixin):
    def __init__(self):
        self.cfg: dict[str, Any] = {
            "ranking": {"count": 2, "pool_pages": 2},
            "random": {"count": 2},
        }
        self._stopped = False
        self.enabled = True
        self.proxy = ""
        self.tz_name = "Asia/Shanghai"
        self.log = Mock()
        self._checks = set()
        self._content_policy = Mock(return_value=(True, "link"))
        self._api_headers = Mock(return_value={"Cookie": "session=test"})
        self._fetch_detail = AsyncMock(side_effect=lambda iid: item(iid))
        self._fetch_pages = AsyncMock(
            side_effect=lambda work: [work] * work["page_count"]
        )
        self._download = AsyncMock(return_value=Path("/tmp/pixiv-fixture.png"))
        self._get_bot_id = AsyncMock(return_value="123")
        self.scheduler = SimpleNamespace(daily=AsyncMock())
        self.plugin = SimpleNamespace(
            context=SimpleNamespace(send_message=AsyncMock(return_value=True))
        )


def item(iid="7", pages=1, restriction=0):
    return {
        "id": iid,
        "title": "Title",
        "author": "Artist",
        "page_count": pages,
        "original": f"https://i.pximg.net/{iid}.png",
        "regular": "",
        "x_restrict": restriction,
    }


def event(text, group=True, wake=True) -> AstrMessageEvent:
    return cast(
        AstrMessageEvent,
        SimpleNamespace(
            message_str=text,
            is_at_or_wake_command=wake,
            unified_msg_origin="test:GroupMessage:1",
            get_self_id=lambda: "123",
            get_message_type=lambda: (
                MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
            ),
            plain_result=lambda text: MessageChain([Plain(text)]),
        ),
    )


def at(chain: MessageChain | None, index: int = 0) -> Any:
    assert chain is not None
    return chain.chain[index]


def must(chain: MessageChain | None) -> MessageChain:
    assert chain is not None
    return chain


def awaited(mock: Any) -> Any:
    called = mock.await_args
    assert called is not None
    return called


class PixivBrowseTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.host = BrowseHost()
        self.umo = "test:GroupMessage:1"

    async def test_commands_reject_arguments_private_and_disabled(self):
        for text in (
            "#pixiv日榜 4",
            "/pixiv周榜 all",
            "pixiv月榜 2",
            "#pixiv作品 0",
            "#pixiv作品 7 第0张",
        ):
            reply = await self.host.handle_browse(event(text))
            self.assertIn("用法", at(reply).text)
        reply = await self.host.handle_browse(event("#pixiv日榜", group=False))
        self.assertIn("群聊", at(reply).text)
        self.host.enabled = False
        reply = await self.host.handle_browse(event("#pixiv日榜"))
        self.assertIn("停用", at(reply).text)
        self.assertFalse(self.host.browse_intercepts(event("pixiv日榜", wake=False)))
        self.assertTrue(self.host.browse_intercepts(event("/pixiv日榜", wake=False)))

    async def test_prefixless_aliases_route_without_wakeup_and_respect_switch(self):
        self.host._browse_selection = AsyncMock(
            return_value=MessageChain([Plain("ok")])
        )
        self.host._render_items = AsyncMock(return_value=MessageChain([Plain("ok")]))
        for prefix in ("", "#", "/"):
            for command, mode in (
                ("日榜", "daily"),
                ("周榜", "weekly"),
                ("月榜", "monthly"),
            ):
                e = event(prefix + command, wake=False)
                await self.host.handle_browse(e)
                self.host._browse_selection.assert_awaited_with(
                    e.unified_msg_origin, mode=mode, bot_id="123"
                )
                self.assertEqual(e.message_str, prefix + command)
            await self.host.handle_browse(event(prefix + "随机 初音ミク", wake=False))
            self.host._browse_selection.assert_awaited_with(
                self.umo,
                mode="daily",
                keyword="初音ミク",
                randomized=True,
                bot_id="123",
            )
            await self.host.handle_browse(event(prefix + "作品 7 第1张", wake=False))
            self.assertEqual(
                awaited(self.host._render_items).args[0][0]["_browse_page"], 0
            )
        reply = await self.host.handle_browse(event("日榜 10", wake=False))
        self.assertIn("用法", at(reply).text)
        for text in ("日榜真好看", "今天看看日榜", "随机应变", "作品集", "订阅号"):
            self.assertFalse(self.host.browse_intercepts(event(text, wake=False)))
        self.host.cfg["allow_prefixless"] = False
        for prefix in ("", "#", "/"):
            for command in ("日榜", "周榜", "月榜", "随机 猫", "作品 7"):
                self.assertFalse(self.host.browse_intercepts(event(prefix + command)))
        self.assertTrue(self.host.browse_intercepts(event("#pixiv日榜", wake=False)))

    async def test_unknown_and_r18_filtered_before_fetch_or_download(self):
        unknown = item()
        unknown.pop("x_restrict")
        result = await self.host._render_items([unknown, item(restriction=1)], self.umo)
        self.assertIsNone(result)
        self.host._fetch_pages.assert_not_awaited()
        self.host._download.assert_not_awaited()

    async def test_single_image_direct_and_multiple_images_forward(self):
        result = await self.host._render_items([item()], self.umo)
        self.assertIsInstance(at(result, 1), Image)
        self.host._get_bot_id.assert_not_awaited()
        result = await self.host._render_items([item(pages=3)], self.umo)
        self.assertIsInstance(at(result), Nodes)
        self.assertEqual(len(at(result).nodes), 3)
        self.assertEqual(
            [call.args[2] for call in self.host._download.await_args_list[-3:]],
            [0, 1, 2],
        )
        self.assertTrue(
            all(
                call.kwargs["user_id"] is None
                for call in self.host._download.await_args_list
            )
        )

    async def test_r18_source_links_count_toward_forwarding_without_downloads(self):
        cases = (
            ("one link", [item(restriction=1)], 1, 0),
            ("two links", [item("7", restriction=1), item("8", restriction=1)], 2, 0),
            ("two linked pages", [item(pages=2, restriction=1)], 2, 0),
            ("image and link", [item("7"), item("8", restriction=1)], 2, 1),
        )
        for label, works, count, downloads in cases:
            with self.subTest(label):
                host = BrowseHost()
                host._content_policy.return_value = (False, "link")
                result = await host._render_items(works, self.umo)
                if count == 1:
                    self.assertEqual(len(must(result).chain), 1)
                    self.assertIsInstance(at(result), Plain)
                    self.assertIn("https://i.pximg.net/7.png", at(result).text)
                    host._get_bot_id.assert_not_awaited()
                else:
                    self.assertIsInstance(at(result), Nodes)
                    self.assertEqual(len(at(result).nodes), count)
                    host._get_bot_id.assert_awaited_once_with(self.umo)
                self.assertEqual(host._download.await_count, downloads)
        self.host._content_policy.return_value = (False, "image")
        result = await self.host._render_items([item(restriction=1)], self.umo)
        self.assertIsInstance(at(result, 1), Image)

    async def test_failed_download_raises_and_all_pages_preserve_original_rank(self):
        result = await self.host._render_items(
            [item(pages=4)], self.umo, ranks={"7": 19}
        )
        self.assertEqual(len(at(result).nodes), 4)
        for index, node in enumerate(at(result).nodes):
            self.assertIn("排名 #19", node.content[0].text)
            self.assertIn(f"第 {index + 1}/4 张", node.content[0].text)
        self.host._fetch_pages.assert_awaited_once()
        self.host._download.return_value = None
        with self.assertRaises(RuntimeError):
            await self.host._render_items([item()], self.umo)

    async def test_artwork_link_all_pages_and_explicit_page(self):
        self.host._fetch_detail.side_effect = lambda iid: item(iid, pages=3)
        reply = await self.host.handle_browse(
            event("#pixiv作品 https://www.pixiv.net/artworks/7 第2张")
        )
        self.assertIsInstance(at(reply, 1), Image)
        self.assertEqual(awaited(self.host._download).args[2], 1)
        reply = await self.host.handle_browse(event("#pixiv作品 7"))
        self.assertEqual(len(at(reply).nodes), 3)
        reply = await self.host.handle_browse(event("#pixiv作品 7 第4张"))
        self.assertIn("超出范围", at(reply).text)

    async def test_rankings_and_daily_push_show_all_pages_with_work_count_limit(self):
        for command in ("#pixiv日榜", "#pixiv周榜", "#pixiv月榜", "scheduled"):
            with self.subTest(command=command):
                host = BrowseHost()
                host._browse_candidates = AsyncMock(
                    return_value=[("7", 9), ("8", 20), ("9", 21)]
                )
                host._fetch_detail.side_effect = lambda iid: item(
                    iid, pages=2 if iid == "7" else 3
                )
                if command == "scheduled":
                    host.cfg["ranking"]["daily_push"] = {
                        "enable": True,
                        "groups": [self.umo],
                    }
                    await host.push_daily_ranking()
                    host.plugin.context.send_message.assert_awaited_once()
                    result = awaited(host.plugin.context.send_message).args[1]
                else:
                    result = await host.handle_browse(event(command))
                nodes = at(result).nodes
                self.assertEqual(len(nodes), 5)
                self.assertEqual(
                    [call.args[0] for call in host._fetch_detail.await_args_list],
                    ["7", "8"],
                )
                self.assertEqual(
                    [call.args[2] for call in host._download.await_args_list],
                    [0, 1, 0, 1, 2],
                )
                for node, iid, rank, page, total in zip(
                    nodes,
                    ["7", "7", "8", "8", "8"],
                    [9, 9, 20, 20, 20],
                    [1, 2, 1, 2, 3],
                    [2, 2, 3, 3, 3],
                ):
                    self.assertIn(f"排名 #{rank}\n", node.content[0].text)
                    self.assertIn(f"/artworks/{iid}\n", node.content[0].text)
                    self.assertIn(f"第 {page}/{total} 张", node.content[0].text)
                    self.assertIsInstance(node.content[1], Image)

    async def test_ranking_raw_json_pages_and_api_allowlist(self):
        responses = [
            {"contents": [{"illust_id": 7, "rank": 14}, {"illust_id": 8, "rank": 20}]},
            {"contents": [{"illust_id": 8, "rank": 20}, {"illust_id": 9, "rank": 24}]},
        ]
        with patch.object(
            browse, "fetch_json", AsyncMock(side_effect=responses)
        ) as fetch:
            candidates = await self.host._browse_candidates("monthly", 2)
        self.assertEqual(candidates, [("7", 14), ("8", 20), ("9", 24)])
        self.assertEqual(awaited(fetch).kwargs["allowed_hosts"], {"www.pixiv.net"})
        self.assertTrue(awaited(fetch).kwargs["proxy_resolve"])
        self.assertEqual(awaited(fetch).kwargs["headers"]["Cookie"], "session=test")
        self.assertEqual(awaited(fetch).kwargs["params"]["mode"], "monthly")

    async def test_random_search_quoted_and_filtered_pool_replenished(self):
        self.host.cfg["random"]["search_pages"] = 1
        response = {
            "error": False,
            "body": {"illustManga": {"data": [{"id": "7"}, {"id": "8"}, {"id": "9"}]}},
        }
        self.host._fetch_detail.side_effect = lambda iid: item(
            iid, restriction=1 if iid == "7" else 0
        )
        with (
            patch.object(
                browse, "fetch_json", AsyncMock(return_value=response)
            ) as fetch,
            patch.object(browse.random, "shuffle") as shuffle,
        ):
            result = await self.host.handle_browse(event("#pixiv随机 风景/天空"))
        shuffle.assert_called_once()
        self.assertIn("%E9%A3%8E%E6%99%AF%2F%E5%A4%A9%E7%A9%BA", awaited(fetch).args[0])
        self.assertEqual(awaited(fetch).kwargs["params"]["s_mode"], "s_tc")
        self.assertEqual(awaited(fetch).kwargs["params"]["order"], "date_d")
        self.assertEqual(awaited(fetch).kwargs["params"]["mode"], "all")
        self.assertEqual(len(at(result).nodes), 2)
        self.assertEqual(
            [call.args[0]["id"] for call in self.host._download.await_args_list],
            ["8", "9"],
        )

    async def test_random_without_keyword_uses_daily_multi_page_pool(self):
        self.host._browse_candidates = AsyncMock(return_value=[("7", 1), ("8", 2)])
        with patch.object(browse.random, "shuffle") as shuffle:
            await self.host.handle_browse(event("#pixiv随机"))
        self.host._browse_candidates.assert_awaited_once_with("daily", 2, "")
        shuffle.assert_called_once()

    async def test_daily_independent_groups_failures_and_task_tracking(self):
        self.host.cfg["ranking"]["daily_push"] = {
            "enable": True,
            "groups": [self.umo, "other:GroupMessage:2"],
            "time": "10:15",
        }
        await self.host.start_browse()
        self.assertEqual(awaited(self.host.scheduler.daily).args[0], "10:15")
        calls = []

        async def render(origin, **kwargs):
            self.assertIn(asyncio.current_task(), self.host._checks)
            calls.append(origin)
            if len(calls) == 1:
                raise RuntimeError("Fixture group failure")
            return MessageChain([Plain("ready")])

        self.host._browse_selection = render
        await self.host.push_daily_ranking()
        self.assertEqual(len(calls), 2)
        self.assertFalse(self.host._checks)
        self.host.plugin.context.send_message.assert_awaited_once()
        self.assertEqual(
            awaited(self.host.plugin.context.send_message).args[0],
            "other:GroupMessage:2",
        )

    async def test_daily_overlap_and_cancellation_cleanup(self):
        self.host.cfg["ranking"]["daily_push"] = {"enable": True, "groups": [self.umo]}
        entered = asyncio.Event()
        blocked = asyncio.Event()

        async def slow_selection(*args, **kwargs):
            entered.set()
            await blocked.wait()

        self.host._browse_selection = AsyncMock(side_effect=slow_selection)
        task = asyncio.create_task(self.host.push_daily_ranking())
        await entered.wait()
        self.assertIn(task, self.host._checks)
        await self.host.push_daily_ranking()
        self.host._browse_selection.assert_awaited_once()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.host._checks)
        self.assertFalse(self.host._browse_daily_running)

    async def test_unknown_stays_filtered_with_filter_off_and_original_required(self):
        self.host._content_policy.return_value = (False, "link")
        unknown = item(restriction=-1)
        self.assertIsNone(await self.host._render_items([unknown], self.umo))
        adult = item(restriction=1)
        adult["original"] = ""
        adult["regular"] = "https://i.pximg.net/preview.png"
        with self.assertRaisesRegex(RuntimeError, "original URL"):
            await self.host._render_items([adult], self.umo)
        self.host._download.assert_not_awaited()
        adult["original"] = "https://i.pximg.net/source.png"
        self.host._content_policy.return_value = (False, "image")
        await self.host._render_items([adult], self.umo)
        self.assertTrue(awaited(self.host._download).kwargs["source_only"])

    async def test_filtered_rank_keeps_original_position(self):
        self.host.cfg["ranking"]["count"] = 1
        self.host._browse_candidates = AsyncMock(return_value=[("7", 2), ("8", 13)])
        self.host._fetch_detail.side_effect = lambda iid: item(
            iid, restriction=1 if iid == "7" else 0
        )
        result = await self.host.handle_browse(event("#pixiv日榜"))
        self.assertIn("排名 #13", at(result).text)
        self.host._fetch_pages.assert_awaited_once()
        self.assertEqual(awaited(self.host._download).args[0]["id"], "8")

    async def test_network_error_is_clear_and_does_not_send(self):
        with patch.object(
            browse,
            "fetch_json",
            AsyncMock(side_effect=RuntimeError("Fixture network failure")),
        ):
            result = await self.host.handle_browse(event("#pixiv日榜"))
        self.assertIn("请求或图片下载失败", at(result).text)
        self.host.plugin.context.send_message.assert_not_awaited()

    async def test_forward_account_await_rechecks_policy_and_stopping(self):
        async def change_policy(*args):
            self.host._content_policy.return_value = (False, "image")
            return "123"

        self.host._get_bot_id.side_effect = change_policy
        with self.assertRaisesRegex(RuntimeError, "policy changed"):
            await self.host._render_items([item(pages=2)], self.umo)
        self.host._content_policy.return_value = (True, "link")

        async def stop_module(*args):
            self.host._stopped = True
            return "123"

        self.host._get_bot_id.side_effect = stop_module
        with self.assertRaisesRegex(RuntimeError, "delivery stopped"):
            await self.host._render_items([item(pages=2)], self.umo)

    async def test_configuration_clamping_is_read_only(self):
        self.host.cfg["ranking"]["count"] = 100
        self.host.cfg["ranking"]["pool_pages"] = 100
        self.host._browse_candidates = AsyncMock(return_value=[])
        await self.host._browse_selection(self.umo, mode="daily")
        self.host._browse_candidates.assert_awaited_once_with("daily", 1, "")
        self.host._browse_candidates.reset_mock()
        await self.host._browse_selection(self.umo, mode="daily", randomized=True)
        self.host._browse_candidates.assert_awaited_once_with("daily", 10, "")
        self.assertEqual(self.host.cfg["ranking"]["count"], 100)


if __name__ == "__main__":
    main()
