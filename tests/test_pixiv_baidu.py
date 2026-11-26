"""Local HTTP integration tests for Baidu picture translation."""

import asyncio
import base64
import hashlib
import io
import json
import struct
import tempfile
import threading
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from juhkff_modules import pixiv_baidu as baidu
from PIL import Image


class PixivBaiduTests(unittest.IsolatedAsyncioTestCase):
    """Exercise real multipart requests with an isolated local Baidu fixture."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "source.png"
        Image.new("RGB", (40, 30), "white").save(self.source)
        self.original = self.source.read_bytes()
        buffer = io.BytesIO()
        Image.new("RGB", (40, 30), "blue").save(buffer, format="PNG")
        self.png = buffer.getvalue()
        self.body = self.response(self.png)
        self.status = 200
        self.requests = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.mode = "normal"
        app = web.Application(client_max_size=5 * 1024 * 1024)
        app.router.add_post("/api/trans/sdk/picture", self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/api/trans/sdk/picture"
        endpoint = patch.object(baidu, "ENDPOINT", self.url)
        endpoint.start()
        self.addCleanup(endpoint.stop)

    async def asyncTearDown(self):
        self.release.set()
        await self.runner.cleanup()

    def response(self, image):
        """Build a successful API fixture.

        Args:
            image: Encoded image bytes.

        Returns:
            JSON response bytes.
        """
        return json.dumps(
            {"error_code": "0", "data": {"pasteImg": base64.b64encode(image).decode()}}
        ).encode()

    async def handle(self, request):
        """Read multipart fields and serve the configured response.

        Args:
            request: Incoming HTTP request.

        Returns:
            Configured HTTP response.
        """
        self.assertFalse(request.query)
        parts = {}
        reader = await request.multipart()
        async for part in reader:
            parts[part.name] = bytes(await part.read())
        self.requests.append(parts)
        self.started.set()
        if self.mode == "wait":
            await self.release.wait()
        if self.mode == "chunked":
            response = web.StreamResponse()
            await response.prepare(request)
            await response.write(self.body)
            await response.write_eof()
            return response
        return web.Response(
            body=self.body, status=self.status, headers={"Location": self.url}
        )

    async def translate(self, language="中文", app_id="123456", app_key="secret-key"):
        """Call the real client with fixture credentials.

        Args:
            language: Target language alias.
            app_id: Fixture application ID.
            app_key: Fixture signing secret.

        Returns:
            Cached result path.
        """
        return await baidu.translate_image(self.source, language, app_id, app_key)

    async def test_signed_multipart_and_unchanged_source(self):
        output = await self.translate()
        self.assertEqual(output.read_bytes(), self.png)
        self.assertEqual(output.suffix, ".png")
        self.assertEqual(output.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.source.read_bytes(), self.original)
        parts = self.requests[0]
        self.assertEqual(parts["image"], self.original)
        for field, expected in {
            "from": "auto",
            "to": "zh",
            "appid": "123456",
            "cuid": "APICUID",
            "mac": "mac",
            "paste": "1",
            "version": "3",
        }.items():
            self.assertEqual(parts[field].decode(), expected)
        expected = hashlib.md5(
            (
                "123456"
                + hashlib.md5(self.original).hexdigest()
                + parts["salt"].decode()
                + "APICUIDmacsecret-key"
            ).encode()
        ).hexdigest()
        self.assertEqual(parts["sign"].decode(), expected)
        self.assertNotIn(b"secret-key", b"".join(parts.values()))
        self.assertFalse(list(self.source.parent.glob(".baidu-tmp-*")))

    async def test_cache_normalizes_aliases_and_ignores_secret_rotation(self):
        first = await self.translate("中文")
        first.chmod(0o600)
        second = await self.translate("zh", app_key="rotated-key")
        self.assertEqual(first, second)
        self.assertEqual(second.stat().st_mode & 0o777, 0o644)
        self.assertEqual(len(self.requests), 1)

    async def test_cache_varies_by_source_language_app_id_and_version(self):
        outputs = {await self.translate()}
        outputs.add(await self.translate("en"))
        outputs.add(await self.translate(app_id="654321"))
        with patch.object(baidu, "API_VERSION", "4"):
            outputs.add(await self.translate())
        Image.new("RGB", (40, 30), "red").save(self.source)
        outputs.add(await self.translate())
        self.assertEqual(len(outputs), 5)
        self.assertEqual(len(self.requests), 5)

    async def test_corrupt_cache_is_replaced(self):
        output = await self.translate()
        output.write_bytes(b"not an image")
        self.assertEqual(await self.translate(), output)
        self.assertEqual(output.read_bytes(), self.png)
        self.assertEqual(len(self.requests), 2)

    async def test_concurrent_publications_are_atomic(self):
        outputs = await asyncio.gather(*(self.translate() for _ in range(4)))
        self.assertEqual(len(set(outputs)), 1)
        self.assertEqual(outputs[0].read_bytes(), self.png)
        self.assertFalse(list(self.source.parent.glob(".baidu-tmp-*")))

    async def test_jpeg_input_and_output_preserve_bytes_and_suffix(self):
        buffer = io.BytesIO()
        Image.new("RGB", (40, 30), "blue").save(buffer, format="JPEG")
        jpeg = buffer.getvalue()
        self.source.write_bytes(jpeg)
        self.body = self.response(jpeg)
        output = await self.translate()
        self.assertEqual(output.suffix, ".jpg")
        self.assertEqual(output.read_bytes(), jpeg)
        self.assertEqual(self.requests[0]["image"], jpeg)
        self.assertEqual(await self.translate(), output)
        self.assertEqual(len(self.requests), 1)

    async def test_language_aliases_and_picture_codes(self):
        for alias, expected in [
            ("简体中文", "zh"),
            ("English", "en"),
            ("英语", "en"),
            ("日本語", "jp"),
            ("ja", "jp"),
            ("kor", "kor"),
            ("fr", "fra"),
            ("西班牙语", "spa"),
            ("丹麦语", "dan"),
        ]:
            with self.subTest(alias=alias):
                self.assertEqual(baidu.LANGUAGES[alias.casefold()], expected)
        self.assertEqual(len(baidu._LANGUAGE_GROUPS), 21)
        self.assertEqual(baidu.LANGUAGES["traditional chinese"], "cht")
        self.assertEqual(baidu.LANGUAGES["繁體中文"], "cht")

    async def test_invalid_configuration_never_sends_requests(self):
        for language in ["Klingon", "translate into Chinese please", "", None, "auto"]:
            with (
                self.subTest(language=language),
                self.assertRaisesRegex(ValueError, "Unsupported"),
            ):
                await self.translate(language)
        for app_id, app_key in [
            ("", "secret"),
            ("123", ""),
            (None, "secret"),
            ("123", "  "),
        ]:
            with self.subTest(app_id=app_id), self.assertRaises(ValueError):
                await self.translate(app_id=app_id, app_key=app_key)
        self.assertEqual(self.requests, [])

    async def test_non_200_and_redirect_are_not_retried(self):
        for status in [302, 400, 500]:
            self.status = status
            with (
                self.subTest(status=status),
                self.assertRaisesRegex(RuntimeError, str(status)),
            ):
                await self.translate()
        self.assertEqual(len(self.requests), 3)
        self.assertFalse(list(self.source.parent.glob(".baidu-*")))

    async def test_api_errors_redact_messages_and_untrusted_codes(self):
        for code in ["54001", "secret-key"]:
            self.body = json.dumps(
                {"error_code": code, "error_msg": "secret-key 123456"}
            ).encode()
            with self.subTest(code=code), self.assertRaises(RuntimeError) as caught:
                await self.translate()
            self.assertNotIn("secret-key", str(caught.exception))
            self.assertNotIn("123456", str(caught.exception))
            if code == "54001":
                self.assertIn("54001", str(caught.exception))
        self.assertEqual(len(self.requests), 2)

    async def test_malformed_json_and_rendered_images_never_publish(self):
        for body in [
            b"secret-key",
            b"[]",
            b"{}",
            b'{"data":null}',
            b'{"data":{"pasteImg":12}}',
            b'{"data":{"pasteImg":"%%%"}}',
            self.response(b"<html>error</html>"),
            self.response(self.png[:40]),
        ]:
            self.body = body
            with self.subTest(body=body), self.assertRaises(ValueError) as caught:
                await self.translate()
            self.assertNotIn("secret-key", str(caught.exception))
        self.assertFalse(list(self.source.parent.glob(".baidu-*")))

    async def test_empty_oversized_and_invalid_inputs_are_rejected(self):
        with patch.object(baidu, "MAX_IMAGE_BYTES", len(self.original) - 1):
            with self.assertRaisesRegex(ValueError, "MiB"):
                await self.translate()
        for data in [b"", b"invalid-image"]:
            self.source.write_bytes(data)
            with self.assertRaises(ValueError):
                await self.translate()
        self.assertEqual(self.requests, [])

    async def test_tall_images_use_broad_local_safety_bounds(self):
        Image.new("RGB", (10, 5000), "white").save(self.source)
        await self.translate()
        self.assertEqual(len(self.requests), 1)

    async def test_declared_and_streamed_response_limits(self):
        self.body = self.response(self.png) + b" " * 1000
        for mode in ["normal", "chunked"]:
            self.mode = mode
            with (
                self.subTest(mode=mode),
                patch.object(baidu, "MAX_RESPONSE_BYTES", 200),
            ):
                with self.assertRaisesRegex(ValueError, "response exceeds"):
                    await self.translate()
        self.assertFalse(list(self.source.parent.glob(".baidu-*")))

    async def test_decoded_image_size_limit(self):
        with patch.object(baidu, "MAX_OUTPUT_BYTES", len(self.png) - 1):
            with self.assertRaisesRegex(ValueError, "image exceeds"):
                await self.translate()
        self.assertFalse(list(self.source.parent.glob(".baidu-*")))

    async def test_png_dimension_bomb_is_rejected_without_pixel_decode(self):
        for width, height in [(16385, 1), (8000, 6000)]:
            data = bytearray(self.png)
            data[16:24] = struct.pack(">II", width, height)
            data[29:33] = struct.pack(">I", zlib.crc32(data[12:29]))
            self.body = self.response(bytes(data))
            with (
                self.subTest(size=(width, height)),
                self.assertRaisesRegex(ValueError, "dimensions"),
            ):
                await self.translate()
        self.assertFalse(list(self.source.parent.glob(".baidu-*")))

    async def test_cancellation_propagates_without_retry_or_output(self):
        self.mode = "wait"
        task = asyncio.create_task(self.translate())
        await asyncio.wait_for(self.started.wait(), timeout=3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.release.set()
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(list(self.source.parent.glob(".baidu-*")))

    async def test_atomic_publication_failure_cleans_temporary_file(self):
        with patch.object(Path, "replace", side_effect=OSError("disk failed")):
            with self.assertRaisesRegex(OSError, "disk failed"):
                await self.translate()
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertFalse(list(self.source.parent.glob(".baidu-*")))

    async def test_file_io_decoding_and_verification_run_in_worker_threads(self):
        event_thread = threading.get_ident()
        calls = []
        originals = {
            "read": baidu._read_bounded,
            "verify": baidu._verify_image,
            "decode": baidu.base64.b64decode,
            "replace": Path.replace,
        }

        def checked(name):
            def invoke(*args, **kwargs):
                self.assertNotEqual(threading.get_ident(), event_thread)
                calls.append(name)
                return originals[name](*args, **kwargs)

            return invoke

        with (
            patch.object(baidu, "_read_bounded", checked("read")),
            patch.object(baidu, "_verify_image", checked("verify")),
            patch.object(baidu.base64, "b64decode", checked("decode")),
            patch.object(Path, "replace", checked("replace")),
        ):
            await self.translate()
            await self.translate()
        self.assertTrue({"read", "verify", "decode", "replace"}.issubset(calls))


if __name__ == "__main__":
    unittest.main()
