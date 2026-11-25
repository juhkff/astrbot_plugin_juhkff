"""Offline coverage for ordinary and restricted rankings in one forward."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from juhkff_modules import pixiv_browse as browse
from PIL import Image as PILImage
from pixiv_test_utils import page_nodes
from test_pixiv_browse import BrowseHost, event, item, must

from astrbot.api.message_components import ComponentType, Nodes, Plain

SEPARATOR = "---------------- NSFW ----------------"


class PixivDualRankingTests(IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        image = Path(temporary.name) / "fixture.png"
        PILImage.new("RGB", (2, 2), "red").save(image)
        self.host = BrowseHost(image)
        self.host._content_policy.return_value = (False, "image")
        self.origin = "test:GroupMessage:1"
        self.host._fetch_detail.side_effect = lambda iid: item(
            iid, restriction=1 if iid in {"8", "9", "10"} else 0
        )

    async def test_daily_and_weekly_request_both_modes_and_keep_one_ordered_forward(
        self,
    ):
        for mode, command in (("daily", "#pixiv日榜"), ("weekly", "#pixiv周榜")):
            with self.subTest(mode=mode):
                calls = []

                async def fetch(url, **kwargs):
                    requested = kwargs["params"]["mode"]
                    calls.append(requested)
                    return {
                        "contents": [
                            {
                                "illust_id": "8" if requested.endswith("_r18") else "7",
                                "rank": 3 if requested.endswith("_r18") else 2,
                            }
                        ]
                    }

                with patch.object(browse, "fetch_json", AsyncMock(side_effect=fetch)):
                    result = must(await self.host.handle_browse(event(command)))
                self.assertEqual(calls, [mode, mode + "_r18"])
                self.assertEqual(len(result.chain), 1)
                self.assertIsInstance(result.chain[0], Nodes)
                nodes = result.chain[0].nodes
                self.assertEqual(len(nodes), 3)
                self.assertIn("/7", nodes[0].content[0].text)
                self.assertEqual(nodes[1].content[0].text, SEPARATOR)
                self.assertIn("/8", nodes[2].content[0].text)
                self.assertIn("排名：#2", nodes[0].content[0].text)
                self.assertIn("排名：#3", nodes[2].content[0].text)

    async def test_monthly_and_filtered_groups_do_not_fetch_restricted_modes(self):
        for mode, policy in (
            ("monthly", (False, "image")),
            ("daily", (True, "image")),
            ("weekly", (True, "link")),
        ):
            with self.subTest(mode=mode, policy=policy):
                self.host._content_policy.return_value = policy
                self.host._browse_candidates = AsyncMock(return_value=[("7", 1)])
                result = must(await self.host._browse_selection(self.origin, mode=mode))
                self.host._browse_candidates.assert_awaited_once_with(mode, 1, "")
                self.assertFalse(any(isinstance(part, Nodes) for part in result.chain))

    async def test_counts_are_per_source_and_duplicate_or_wrong_section_ids_are_excluded(
        self,
    ):
        self.host._browse_candidates = AsyncMock(
            side_effect=[
                [("8", 1), ("7", 2), ("6", 3), ("5", 4)],
                [("7", 1), ("8", 2), ("8", 3), ("9", 4), ("10", 5)],
            ]
        )
        self.host._render_items = AsyncMock(return_value="rendered")
        self.assertEqual(
            await self.host._browse_selection(self.origin, mode="daily"), "rendered"
        )
        call = self.host._render_items.await_args
        self.assertEqual([work["id"] for work in call.args[0]], ["7", "6", "8", "9"])
        self.assertEqual(call.kwargs["ranks"], {"7": 2, "6": 3, "8": 2, "9": 4})
        self.assertTrue(call.kwargs["ranking_sections"])

    async def test_restricted_source_failure_still_returns_ordinary_work_without_separator(
        self,
    ):
        self.host._browse_candidates = AsyncMock(
            side_effect=[[("7", 1)], RuntimeError("R18 unavailable")]
        )
        result = must(await self.host._browse_selection(self.origin, mode="daily"))
        self.assertEqual(len(result.chain), 2)
        self.assertIn("/7", result.chain[0].text)
        self.assertNotIn(SEPARATOR, result.chain[0].text)
        self.host.log.exception.assert_called()

    async def test_link_policy_keeps_restricted_work_as_link_and_normal_work_as_image(
        self,
    ):
        self.host._content_policy.return_value = (False, "link")
        self.host._browse_candidates = AsyncMock(side_effect=[[("7", 1)], [("8", 1)]])
        result = must(await self.host._browse_selection(self.origin, mode="daily"))
        nodes = result.chain[0].nodes
        self.assertTrue(
            any(part.type == ComponentType.Image for part in nodes[0].content)
        )
        self.assertEqual(nodes[1].content[0].text, SEPARATOR)
        self.assertTrue(all(isinstance(part, Plain) for part in nodes[2].content))
        self.assertIn("https://i.pximg.net/8.png", nodes[2].content[0].text)
        self.host._download.assert_awaited_once()

    async def test_dual_ranking_ignores_base64_batch_threshold_and_preserves_album_pages(
        self,
    ):
        self.host._fetch_detail.side_effect = lambda iid: item(
            iid, pages=2, restriction=1 if iid == "8" else 0
        )
        self.host._browse_candidates = AsyncMock(side_effect=[[("7", 1)], [("8", 1)]])
        with patch.object(browse, "_FORWARD_BATCH_BYTES", 1):
            result = must(await self.host._browse_selection(self.origin, mode="daily"))
        self.assertEqual(len(result.chain), 1)
        nodes = result.chain[0].nodes
        self.assertEqual(len(nodes), 3)
        self.assertEqual(nodes[1].content[0].text, SEPARATOR)
        self.assertEqual(len(page_nodes(nodes)), 4)

    async def test_daily_push_calls_send_once_with_combined_rankings(self):
        self.host.cfg["ranking"]["daily_push"] = {
            "enable": True,
            "groups": [self.origin],
        }
        self.host._browse_candidates = AsyncMock(side_effect=[[("7", 1)], [("8", 1)]])
        await self.host.push_daily_ranking()
        send = self.host.plugin.context.send_message
        send.assert_awaited_once()
        result = send.await_args.args[1]
        self.assertEqual(len(result.chain), 1)
        self.assertEqual(result.chain[0].nodes[1].content[0].text, SEPARATOR)

    async def test_random_and_direct_artwork_do_not_request_additional_rankings(self):
        self.host._browse_candidates = AsyncMock(return_value=[("7", 1), ("8", 2)])
        with patch.object(browse.random, "shuffle", lambda rows: None):
            result = must(await self.host.handle_browse(event("#pixiv随机")))
        self.host._browse_candidates.assert_awaited_once_with("daily", 2, "")
        self.assertEqual(
            sum(
                isinstance(part, Plain) and part.text == SEPARATOR
                for node in result.chain[0].nodes
                for part in node.content
            ),
            1,
        )
        self.assertEqual(result.chain[0].nodes[1].content[0].text, SEPARATOR)
        self.host._browse_candidates.reset_mock()
        await self.host.handle_browse(event("#pixiv作品 8"))
        self.host._browse_candidates.assert_not_awaited()
