"""HTTP integration tests for the external manga translation adapter."""

import asyncio
import io
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from juhkff_modules import pixiv_manga as manga
from PIL import Image


class PixivMangaTests(unittest.IsolatedAsyncioTestCase):
    """Exercise actual multipart requests against an isolated local backend."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "source.png"
        Image.new("RGB", (4, 3), "white").save(self.source)
        self.original = self.source.read_bytes()
        buffer = io.BytesIO()
        Image.new("RGB", (4, 3), "blue").save(buffer, format="PNG")
        self.png = buffer.getvalue()
        self.body = self.png
        self.status = 200
        self.requests = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.mode = "normal"
        app = web.Application(client_max_size=70 * 1024 * 1024)
        app.router.add_post("/backend/translate/with-form/image", self.handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/backend"

    async def asyncTearDown(self):
        self.release.set()
        await self.runner.cleanup()

    async def handle(self, request):
        """Capture real multipart fields and return the chosen fixture response.

        Args:
            request: Incoming aiohttp request.

        Returns:
            Configured HTTP response.
        """
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
            body=self.body,
            status=self.status,
            headers={"Location": self.url + "/translate/with-form/image"},
        )

    async def translate(self, language="中文", translator="offline"):
        """Call the adapter with the current fixture backend.

        Args:
            language: Target language alias or code.
            translator: Backend translator identifier.

        Returns:
            Cached output path.
        """
        return await manga.translate_image(self.source, language, self.url, translator)

    async def test_official_multipart_preserves_source_and_output_bytes(self):
        output = await self.translate()
        self.assertNotEqual(output, self.source)
        self.assertEqual(output.read_bytes(), self.png)
        self.assertEqual(output.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(self.requests[0]["image"], self.original)
        self.assertEqual(
            json.loads(self.requests[0]["config"]),
            {"translator": {"translator": "offline", "target_lang": "CHS"}},
        )
        self.assertFalse(list(self.source.parent.glob(".manga-tmp-*")))

    async def test_cache_reuses_verified_output_and_normalizes_aliases(self):
        first = await self.translate("中文")
        first.chmod(0o600)
        second = await self.translate("CHS")
        self.assertEqual(first, second)
        self.assertEqual(second.stat().st_mode & 0o777, 0o644)
        self.assertEqual(len(self.requests), 1)

    async def test_concurrent_publications_leave_a_complete_shared_cache(self):
        outputs = await asyncio.gather(*(self.translate() for _ in range(4)))
        self.assertEqual(len(set(outputs)), 1)
        self.assertEqual(outputs[0].read_bytes(), self.png)
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertFalse(list(self.source.parent.glob(".manga-tmp-*")))

    async def test_corrupt_cache_is_replaced(self):
        output = await self.translate()
        output.write_bytes(b"not a PNG")
        self.assertEqual(await self.translate(), output)
        self.assertEqual(output.read_bytes(), self.png)
        self.assertEqual(len(self.requests), 2)

    async def test_cache_varies_by_source_language_translator_and_backend(self):
        outputs = {await self.translate()}
        outputs.add(await self.translate("en"))
        outputs.add(await self.translate(translator="sugoi"))
        other_url = self.url.replace("127.0.0.1", "localhost")
        outputs.add(
            await manga.translate_image(self.source, "CHS", other_url, "offline")
        )
        Image.new("RGB", (4, 3), "red").save(self.source)
        outputs.add(await self.translate())
        self.assertEqual(len(outputs), 5)
        self.assertEqual(len(self.requests), 5)

    async def test_language_aliases_and_official_codes(self):
        for alias, expected in [
            ("简体中文", "CHS"),
            ("traditional Chinese", "CHT"),
            ("繁体中文", "CHT"),
            ("zh-tw", "CHT"),
            ("English", "ENG"),
            ("英语", "ENG"),
            ("日本語", "JPN"),
            ("ja", "JPN"),
            ("法语", "FRA"),
            ("THA", "THA"),
            ("FIL", "FIL"),
        ]:
            with self.subTest(alias=alias):
                self.assertEqual(manga.LANGUAGES[alias.casefold()], expected)
        self.assertEqual(len(manga._LANGUAGE_GROUPS), 26)

    async def test_invalid_configuration_never_sends_requests(self):
        for url in [
            "",
            "ftp://localhost",
            "http:///bad",
            "http://user:pass@localhost",
            "http://localhost?x=1",
            "http://localhost#x",
            "http://localhost?",
            "http://localhost:",
            "http://localhost:0",
            "http://localhost:99999",
            "http://[bad]",
            "http://local host",
            "http://host\\path",
            "http://[::1]garbage",
            "http://.",
            "http://host..local",
            "http://-host",
            "http://host:abc",
            "http://host/base%zz",
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                await manga.translate_image(self.source, "CHS", url, "offline")
        for language in ["Klingon", "translate into Chinese please", "", None]:
            with (
                self.subTest(language=language),
                self.assertRaisesRegex(ValueError, "Unsupported"),
            ):
                await self.translate(language)
        for translator in ["", "   ", None]:
            with self.subTest(translator=translator), self.assertRaises(ValueError):
                await self.translate(translator=translator)
        self.assertEqual(self.requests, [])

    async def test_valid_operator_urls_allow_private_hosts_ports_and_base_paths(self):
        # Validation should accept these URLs; the mocked session stops before DNS.
        for url in [
            "http://localhost:5003/api",
            "http://192.168.1.5:8080/manga",
            "http://[::1]:5003/base",
            "https://example.org/base/",
        ]:
            with (
                self.subTest(url=url),
                patch.object(
                    manga.aiohttp,
                    "ClientSession",
                    side_effect=RuntimeError("validated"),
                ) as session,
            ):
                with self.assertRaisesRegex(RuntimeError, "validated"):
                    await manga.translate_image(self.source, "CHS", url, "offline")
                self.assertFalse(session.call_args.kwargs["trust_env"])
                self.assertEqual(session.call_args.kwargs["timeout"].total, 300)

    async def test_non_200_and_redirect_are_not_retried(self):
        for status in [302, 400, 500]:
            self.status = status
            with (
                self.subTest(status=status),
                self.assertRaisesRegex(RuntimeError, str(status)),
            ):
                await self.translate()
        self.assertEqual(len(self.requests), 3)
        self.assertFalse(list(self.source.parent.glob(".manga-*.png")))

    async def test_malformed_or_truncated_png_is_not_published(self):
        for body in [b"<html>error</html>", manga.PNG_SIGNATURE, self.png[:40]]:
            self.body = body
            with self.subTest(body=body), self.assertRaises(ValueError):
                await self.translate()
        self.assertFalse(list(self.source.parent.glob(".manga-*.png")))
        self.assertEqual(self.source.read_bytes(), self.original)

    async def test_oversized_input_is_rejected_before_request(self):
        with patch.object(manga, "MAX_IMAGE_BYTES", len(self.original) - 1):
            with self.assertRaisesRegex(ValueError, "64 MiB"):
                await self.translate()
        self.assertEqual(self.requests, [])

    async def test_empty_input_is_rejected(self):
        self.source.write_bytes(b"")
        with self.assertRaises(ValueError):
            await self.translate()
        self.assertEqual(self.requests, [])

    async def test_declared_and_streamed_oversized_response_is_rejected(self):
        self.body = self.png + b"x" * 1000
        for mode in ["normal", "chunked"]:
            self.mode = mode
            with self.subTest(mode=mode), patch.object(manga, "MAX_IMAGE_BYTES", 200):
                with self.assertRaisesRegex(ValueError, "response exceeds"):
                    await self.translate()
        self.assertFalse(list(self.source.parent.glob(".manga-*.png")))

    async def test_png_dimension_limits(self):
        for width, height in [(16385, 1), (8000, 6000)]:
            data = bytearray(self.png)
            data[16:24] = struct.pack(">II", width, height)
            data[29:33] = struct.pack(">I", zlib.crc32(data[12:29]))
            self.body = bytes(data)
            with (
                self.subTest(size=(width, height)),
                self.assertRaisesRegex(ValueError, "dimensions"),
            ):
                await self.translate()
        self.assertFalse(list(self.source.parent.glob(".manga-*.png")))

    async def test_cancellation_propagates_without_retry_or_output(self):
        self.mode = "wait"
        task = asyncio.create_task(self.translate())
        await asyncio.wait_for(self.started.wait(), timeout=3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.release.set()
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(list(self.source.parent.glob(".manga-*.png")))

    async def test_atomic_publication_failure_cleans_unique_temporary_file(self):
        with patch.object(Path, "replace", side_effect=OSError("disk failed")):
            with self.assertRaisesRegex(OSError, "disk failed"):
                await self.translate()
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertFalse(list(self.source.parent.glob(".manga-*.png")))


if __name__ == "__main__":
    unittest.main()
