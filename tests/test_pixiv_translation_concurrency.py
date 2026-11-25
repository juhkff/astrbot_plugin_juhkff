"""Event-driven coverage for bounded translation workers and ordered results."""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from PIL import Image as PILImage
from pixiv_test_utils import page_nodes
from test_pixiv_browse import BrowseHost, item, must


class PixivTranslationConcurrencyTests(IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.images = []
        for index in range(4):
            image = Path(temporary.name) / f"page-{index}.png"
            PILImage.new("RGB", (4, 4), (index, 0, 0)).save(image)
            self.images.append(image)
        self.host = BrowseHost(self.images[0])
        self.host.cfg["image_transport"] = "file"
        self.host.cfg["content_translation"] = {"enable": True, "mode": "text"}
        self.host._download.side_effect = lambda work, source, index, **kwargs: (
            self.images[index]
        )
        self.origin = "test:GroupMessage:1"
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def start(self, pages=4, origin=None):
        task = asyncio.create_task(
            self.host._render_items([item(pages=pages)], origin or self.origin)
        )
        self.tasks.append(task)
        return task

    async def test_two_pages_overlap_and_reverse_completion_preserves_image_text_order(
        self,
    ):
        entered = [asyncio.Event() for _ in range(4)]
        release = [asyncio.Event() for _ in range(4)]
        active = peak = 0

        async def translate(image, *args):
            nonlocal active, peak
            index = self.images.index(image)
            active += 1
            peak = max(peak, active)
            entered[index].set()
            try:
                await release[index].wait()
                return f"Translation {index}"
            finally:
                active -= 1

        self.host._translate_image_content = AsyncMock(side_effect=translate)
        task = self.start()
        await asyncio.wait_for(entered[1].wait(), 1)
        self.assertTrue(entered[0].is_set())
        self.assertFalse(entered[2].is_set())
        release[1].set()
        await asyncio.wait_for(entered[2].wait(), 1)
        release[2].set()
        await asyncio.wait_for(entered[3].wait(), 1)
        release[3].set()
        release[0].set()
        result = must(await asyncio.wait_for(task, 1))
        self.assertEqual(peak, 2)
        self.assertEqual(active, 0)
        pages = page_nodes(result.chain[0].nodes)
        self.assertEqual(len(pages), 4)
        for index, page in enumerate(pages):
            self.assertEqual(
                page.content[-1].text, f"\n图片文字翻译：\nTranslation {index}"
            )
            self.assertIn(
                self.images[index].as_uri(),
                [getattr(part, "file", "") for part in page.content],
            )

    async def test_overlay_calls_share_capacity_across_concurrent_destinations(self):
        self.host.cfg["content_translation"]["mode"] = "overlay"
        saturated = asyncio.Event()
        release = asyncio.Event()
        active = peak = calls = 0

        async def typeset(image, *args):
            nonlocal active, peak, calls
            active += 1
            calls += 1
            peak = max(peak, active)
            if active == 2:
                saturated.set()
            try:
                await release.wait()
                return image, ""
            finally:
                active -= 1

        self.host._typeset_image_content = AsyncMock(side_effect=typeset)
        first = self.start(2)
        second = self.start(2, "test:GroupMessage:2")
        await asyncio.wait_for(saturated.wait(), 1)
        self.assertEqual(calls, 2)
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 1)
        self.assertEqual(calls, 4)
        self.assertEqual(peak, 2)
        self.assertEqual(active, 0)

    async def test_cancellation_drains_active_workers_and_queued_destination(self):
        saturated = asyncio.Event()
        waiting = asyncio.Event()
        blocked = asyncio.Event()
        active = 0
        calls = 0

        async def translate(*args):
            nonlocal active, calls
            active += 1
            calls += 1
            if active == 2:
                saturated.set()
            try:
                await blocked.wait()
                return "translated"
            finally:
                active -= 1

        async def download(work, source, index, **kwargs):
            if saturated.is_set():
                waiting.set()
            return self.images[index]

        self.host._translate_image_content = AsyncMock(side_effect=translate)
        self.host._download.side_effect = download
        first = self.start()
        await asyncio.wait_for(saturated.wait(), 1)
        second = self.start()
        await asyncio.wait_for(waiting.wait(), 1)
        second.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await second
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual(calls, 2)
        self.assertEqual(active, 0)
        # Released permits allow a later render to complete immediately.
        self.host._translate_image_content.side_effect = None
        self.host._translate_image_content.return_value = "Recovered"
        await asyncio.wait_for(self.start(2), 1)

    async def test_required_download_failure_cancels_sibling_translation(self):
        entered = asyncio.Event()
        exited = asyncio.Event()
        blocked = asyncio.Event()

        async def translate(*args):
            entered.set()
            try:
                await blocked.wait()
            finally:
                exited.set()

        async def download(work, source, index, **kwargs):
            if index == 1:
                await entered.wait()
                return None
            return self.images[index]

        self.host._download.side_effect = download
        self.host._translate_image_content = AsyncMock(side_effect=translate)
        with self.assertRaisesRegex(RuntimeError, "download failed"):
            await asyncio.wait_for(self.start(), 1)
        self.assertTrue(exited.is_set())
        self.host._translate_image_content.assert_awaited_once()

    async def test_waiting_for_slot_rechecks_disabled_delivery_before_model(self):
        self.host._content_translation_slots = asyncio.Semaphore(0)
        reached = asyncio.Event()

        async def download(work, source, index, **kwargs):
            reached.set()
            return self.images[index]

        self.host._download.side_effect = download
        self.host._translate_image_content = AsyncMock(
            return_value="Must not translate"
        )
        task = self.start(1)
        await asyncio.wait_for(reached.wait(), 1)
        self.host.enabled = False
        self.host._content_translation_slots.release()
        with self.assertRaisesRegex(RuntimeError, "delivery stopped"):
            await asyncio.wait_for(task, 1)
        self.host._translate_image_content.assert_not_awaited()

    async def test_waiting_for_slot_rechecks_changed_policy_before_model(self):
        self.host._content_translation_slots = asyncio.Semaphore(0)
        reached = asyncio.Event()

        async def download(work, source, index, **kwargs):
            reached.set()
            return self.images[index]

        self.host._download.side_effect = download
        self.host._translate_image_content = AsyncMock(
            return_value="Must not translate"
        )
        task = self.start(1)
        await asyncio.wait_for(reached.wait(), 1)
        self.host._content_policy.return_value = (False, "image")
        self.host._content_translation_slots.release()
        with self.assertRaisesRegex(RuntimeError, "policy changed"):
            await asyncio.wait_for(task, 1)
        self.host._translate_image_content.assert_not_awaited()
