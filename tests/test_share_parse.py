"""Offline share-card tests without importing the plugin entrypoint or runtime."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import types
import unittest
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlsplit

from juhkff_modules import http_utils
from PIL import Image as PILImage


@dataclass
class FakePlain:
    text: str


@dataclass
class FakeJson:
    data: object


@dataclass
class FakeAt:
    qq: str


@dataclass
class FakeImage:
    file: str

    @staticmethod
    def fromBytes(payload):
        # Match AstrBot Image.fromBytes without bootstrapping its full runtime.
        return FakeImage("base64://" + base64.b64encode(payload).decode())

    @staticmethod
    def fromURL(_url):
        raise AssertionError("Share covers must never delegate remote fetching")


class FakeBaseModule:
    def __init__(self, plugin):
        self.plugin = plugin

    @property
    def cfg(self):
        return self.plugin.config.get(self.key) or {}

    @property
    def proxy(self):
        return (self.plugin.config.get("global") or {}).get("proxy", "") or ""

    def debug(self, _message):
        pass


def load_share_module():
    # Temporary, isolated package names prevent common.py or main.py imports and
    # avoid leaking AstrBot stubs into other tests in this shared workspace.
    package_name = "_juhkff_share_parse_tests"
    package = types.ModuleType(package_name)
    package.__path__ = []
    common = types.ModuleType(package_name + ".common")
    common.BaseModule = FakeBaseModule
    common.truncate = lambda text, limit: text[:limit]
    events = types.ModuleType("astrbot.api.event")
    events.AstrMessageEvent = object
    events.MessageChain = list
    components = types.ModuleType("astrbot.api.message_components")
    components.At = FakeAt
    components.Image = FakeImage
    components.Json = FakeJson
    components.Plain = FakePlain
    path = Path(__file__).resolve().parents[1] / "juhkff_modules" / "share_parse.py"
    spec = importlib.util.spec_from_file_location(package_name + ".share_parse", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {
            package_name: package,
            package_name + ".common": common,
            package_name + ".http_utils": http_utils,
            "astrbot.api.event": events,
            "astrbot.api.message_components": components,
        },
    ):
        spec.loader.exec_module(module)
    return module


share = load_share_module()
BV_ID = "BV1xx411c7mD"
VIDEO_URL = "https://www.bilibili.com/video/" + BV_ID
COVER_URL = "https://images.example/cover.png"


def image_bytes():
    output = BytesIO()
    PILImage.new("RGB", (2, 2), color="blue").save(output, format="PNG")
    return output.getvalue()


class PlatformTests(unittest.TestCase):
    def test_exact_bilibili_and_shortlink_hosts(self):
        for url in (
            VIDEO_URL,
            "https://BILIBILI.COM./video/av123",
            "https://m.bilibili.com/video/av123",
            "https://b23.tv/abc",
        ):
            with self.subTest(url=url):
                self.assertEqual(share._detect_platform({"url": url}), "bilibili")

    def test_deceptive_hosts_and_paths_stay_generic(self):
        for url in (
            "https://b23.tv.evil.example/abc",
            "https://evilb23.tv/abc",
            "https://www.bilibili.com.evil.example/video/" + BV_ID,
            "https://evil.example/b23.tv/abc",
            "https://evil.example/?url=https://b23.tv/abc",
            "https://evil.example/video/" + BV_ID,
            "https://b23.tv@evil.example/abc",
            "https://user@b23.tv/abc",
            "https://b23.tv:8443/abc",
        ):
            with self.subTest(url=url):
                card = {"url": url, "title": "哔哩哔哩 bilibili b23.tv"}
                self.assertEqual(share._detect_platform(card), "generic")

    def test_other_platform_and_plain_generic(self):
        self.assertEqual(
            share._detect_platform({"url": "https://other.example", "title": "小黑盒"}),
            "xiaoheihe",
        )
        self.assertEqual(
            share._detect_platform({"url": "https://other.example"}), "generic"
        )


class ShareParseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = types.SimpleNamespace(
            config={"global": {"proxy": "http://127.0.0.1:7890"}, "share_parse": {}}
        )
        self.module = share.ShareParseModule(self.plugin)
        self.module.debug = Mock()
        self.event = types.SimpleNamespace(
            chain_result=lambda parts: parts,
            get_sender_id=lambda: "user",
            get_self_id=lambda: "bot",
        )

    def assert_text_only(self, parts, title="Card title"):
        self.assertTrue(parts)
        self.assertFalse(any(isinstance(part, FakeImage) for part in parts))
        self.assertIn(
            title, "".join(part.text for part in parts if isinstance(part, FakePlain))
        )

    async def test_deceptive_urls_never_make_bilibili_requests(self):
        for url in (
            "https://b23.tv.evil.example/abc",
            "https://evil.example/b23.tv/abc",
            "https://evil.example/?b23.tv=/abc",
            "https://bilibili.com.evil.example/video/" + BV_ID,
            "https://user:pass@b23.tv/abc",
            "https://b23.tv:8080/abc",
            "file://b23.tv/abc",
        ):
            with (
                self.subTest(url=url),
                patch.object(
                    share, "public_request", new_callable=AsyncMock
                ) as request,
            ):
                self.assertIsNone(await self.module._bilibili_cover(url))
                request.assert_not_awaited()

    async def test_av_uses_aid_and_bv_uses_bvid(self):
        for video_id, parameter, value in (
            ("av123", "aid", "123"),
            (BV_ID, "bvid", BV_ID),
        ):
            with (
                self.subTest(video_id=video_id),
                patch.object(
                    share,
                    "public_request",
                    new=AsyncMock(
                        return_value=(
                            json.dumps(
                                {"code": 0, "data": {"pic": COVER_URL}}
                            ).encode(),
                            "api",
                        )
                    ),
                ) as request,
            ):
                cover = await self.module._bilibili_cover(
                    "https://www.bilibili.com/video/" + video_id
                )
                self.assertEqual(cover, COVER_URL)
                api = request.await_args.args[0]
                self.assertEqual(urlsplit(api).hostname, "api.bilibili.com")
                self.assertEqual(parse_qs(urlsplit(api).query), {parameter: [value]})
                self.assertEqual(
                    request.await_args.kwargs["allowed_hosts"], {"api.bilibili.com"}
                )
                self.assertEqual(request.await_args.kwargs["proxy"], self.module.proxy)
                self.assertEqual(request.await_args.kwargs["max_bytes"], 1024 * 1024)

    async def test_trusted_b23_resolution_has_exact_redirect_allowlist(self):
        api_body = json.dumps({"code": 0, "data": {"pic": COVER_URL}}).encode()
        with patch.object(
            share,
            "public_request",
            new=AsyncMock(side_effect=[(b"", VIDEO_URL), (api_body, "api")]),
        ) as request:
            self.assertEqual(
                await self.module._bilibili_cover("https://b23.tv/abc"), COVER_URL
            )
        short_request, api_request = request.await_args_list
        self.assertEqual(short_request.args[0], "https://b23.tv/abc")
        self.assertEqual(
            short_request.kwargs["allowed_hosts"],
            {"b23.tv", "bilibili.com", "www.bilibili.com", "m.bilibili.com"},
        )
        self.assertEqual(short_request.kwargs["headers"], {"Range": "bytes=0-0"})
        self.assertEqual(
            parse_qs(urlsplit(api_request.args[0]).query), {"bvid": [BV_ID]}
        )

    async def test_shortlink_private_redirect_failure_keeps_text(self):
        with patch.object(
            share,
            "public_request",
            new=AsyncMock(side_effect=ValueError("Non-public redirect")),
        ) as request:
            parts = await self.module._build_reply(
                self.event,
                {"url": "https://b23.tv/abc", "title": "Card title"},
                "bilibili",
            )
        self.assert_text_only(parts)
        self.assertEqual(request.await_count, 1)

    async def test_generic_public_cover_becomes_local_image_bytes(self):
        payload = image_bytes()
        with patch.object(
            share, "public_request", new=AsyncMock(return_value=(payload, COVER_URL))
        ) as request:
            parts = await self.module._build_reply(
                self.event,
                {
                    "url": "https://article.example/post",
                    "title": "Card title",
                    "preview": COVER_URL,
                },
                "generic",
            )
        image = next(part for part in parts if isinstance(part, FakeImage))
        self.assertEqual(image.file, "base64://" + base64.b64encode(payload).decode())
        self.assertFalse(image.file.startswith(("http://", "https://")))
        self.assertEqual(request.await_args.args[0], COVER_URL)
        self.assertEqual(request.await_args.kwargs["max_bytes"], 10 * 1024 * 1024)
        self.assertEqual(request.await_args.kwargs["proxy"], self.module.proxy)
        self.assertNotIn("allowed_hosts", request.await_args.kwargs)

    async def test_invalid_cover_keeps_title_description_and_link(self):
        with patch.object(
            share,
            "public_request",
            new=AsyncMock(return_value=(b"<html>not an image</html>", COVER_URL)),
        ):
            parts = await self.module._build_reply(
                self.event,
                {
                    "url": "https://article.example/post",
                    "title": "Card title",
                    "desc": "Description",
                    "preview": COVER_URL,
                },
                "generic",
            )
        self.assert_text_only(parts)
        text = "".join(part.text for part in parts)
        self.assertIn("Description", text)
        self.assertIn("https://article.example/post", text)

    async def test_oversized_cover_failure_keeps_text(self):
        with patch.object(
            share,
            "public_request",
            new=AsyncMock(side_effect=RuntimeError("HTTP response exceeds max_bytes")),
        ) as request:
            parts = await self.module._build_reply(
                self.event,
                {
                    "url": "https://article.example/post",
                    "title": "Card title",
                    "preview": COVER_URL,
                },
                "generic",
            )
        self.assert_text_only(parts)
        self.assertEqual(request.await_args.kwargs["max_bytes"], 10 * 1024 * 1024)

    async def test_private_preview_rejected_before_any_http(self):
        with patch.object(http_utils.aiohttp, "ClientSession") as session:
            parts = await self.module._build_reply(
                self.event,
                {
                    "url": "https://article.example/post",
                    "title": "Card title",
                    "preview": "http://127.0.0.1/private",
                },
                "generic",
            )
        self.assert_text_only(parts)
        session.assert_not_called()

    async def test_bilibili_api_cover_is_also_downloaded_and_decoded(self):
        payload = image_bytes()
        api_body = json.dumps({"code": 0, "data": {"pic": COVER_URL}}).encode()
        with patch.object(
            share,
            "public_request",
            new=AsyncMock(side_effect=[(api_body, "api"), (payload, COVER_URL)]),
        ) as request:
            parts = await self.module._build_reply(
                self.event, {"url": VIDEO_URL, "title": "Card title"}, "bilibili"
            )
        self.assertTrue(any(isinstance(part, FakeImage) for part in parts))
        self.assertEqual(request.await_count, 2)
        self.assertEqual(request.await_args_list[1].args[0], COVER_URL)

    async def test_malformed_api_response_has_no_cover(self):
        for body in (
            b"null",
            b"[]",
            b"not json",
            b'{"code":1}',
            b'{"code":0,"data":[]}',
            b'{"code":0,"data":{"pic":123}}',
        ):
            with (
                self.subTest(body=body),
                patch.object(
                    share, "public_request", new=AsyncMock(return_value=(body, "api"))
                ),
            ):
                self.assertIsNone(await self.module._bilibili_cover(VIDEO_URL))

    async def test_query_or_other_path_does_not_supply_video_id(self):
        for url in (
            "https://www.bilibili.com/?next=/video/" + BV_ID,
            "https://www.bilibili.com/not-video/" + BV_ID,
            "https://www.bilibili.com/video/av123suffix",
        ):
            with (
                self.subTest(url=url),
                patch.object(
                    share, "public_request", new_callable=AsyncMock
                ) as request,
            ):
                self.assertIsNone(await self.module._bilibili_cover(url))
                request.assert_not_awaited()

    async def test_fetch_cover_switch_and_platform_switch(self):
        self.plugin.config["share_parse"] = {"fetch_cover": False}
        self.event.message_obj = types.SimpleNamespace(
            message=[FakeJson(json.dumps({"url": VIDEO_URL, "title": "Card title"}))]
        )
        with patch.object(share, "public_request", new_callable=AsyncMock) as request:
            self.assert_text_only(await self.module.handle(self.event))
            request.assert_not_awaited()
        self.plugin.config["share_parse"]["parse_bilibili"] = False
        self.assertIsNone(await self.module.handle(self.event))

    async def test_own_messages_are_ignored(self):
        self.event.get_sender_id = lambda: "bot"
        self.assertIsNone(await self.module.handle(self.event))


if __name__ == "__main__":
    unittest.main()
