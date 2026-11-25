"""Event-driven coverage for the shared Pixiv processing pool and ordering."""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from PIL import Image as PILImage
from pixiv_test_utils import page_nodes
from test_pixiv_browse import BrowseHost, item, must


class ObservedSemaphore(asyncio.Semaphore):
    """Expose queued acquisition so revocation tests need no timing guesses."""

    def __init__(self, value):
        super().__init__(value)
        self.queued = asyncio.Event()

    async def acquire(self):
        if self.locked():
            self.queued.set()
        return await super().acquire()


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
        self.host.cfg["processing_concurrency"] = 2
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

    def start(self, pages=4, origin=None, works=None, **kwargs):
        task = asyncio.create_task(
            self.host._render_items(
                works if works is not None else [item(pages=pages)],
                origin or self.origin,
                **kwargs,
            )
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
        blocked = asyncio.Event()
        active = calls = 0
        slots = ObservedSemaphore(2)
        self.host._pixiv_processing_slots = slots
        self.host._pixiv_processing_concurrency = 2

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

        self.host._translate_image_content = AsyncMock(side_effect=translate)
        first = self.start()
        await asyncio.wait_for(saturated.wait(), 1)
        slots.queued.clear()
        second = self.start()
        await asyncio.wait_for(slots.queued.wait(), 1)
        second.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await second
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual(calls, 2)
        self.assertEqual(active, 0)
        self.assertEqual(slots._value, 2)
        self.host._translate_image_content.side_effect = None
        self.host._translate_image_content.return_value = "Recovered"
        await asyncio.wait_for(self.start(2), 1)
        self.assertIs(self.host._pixiv_processing_slots, slots)

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

    async def test_queued_work_rechecks_disabled_or_revoked_delivery_before_network(
        self,
    ):
        for revoke_subscription in (False, True):
            with self.subTest(revoke_subscription=revoke_subscription):
                self.host.enabled = True
                active = [True]
                slots = ObservedSemaphore(0)
                self.host._pixiv_processing_slots = slots
                self.host._pixiv_processing_concurrency = 2
                self.host._translate_image_content = AsyncMock(
                    return_value="Must not translate"
                )
                task = self.start(1, delivery_active=lambda: active[0])
                await asyncio.wait_for(slots.queued.wait(), 1)
                if revoke_subscription:
                    active[0] = False
                else:
                    self.host.enabled = False
                slots.release()
                with self.assertRaisesRegex(RuntimeError, "delivery stopped"):
                    await asyncio.wait_for(task, 1)
                self.host._fetch_pages.assert_not_awaited()
                self.host._download.assert_not_awaited()
                self.host._translate_image_content.assert_not_awaited()

    async def test_queued_work_rechecks_changed_policy_before_network_or_model(self):
        slots = ObservedSemaphore(0)
        self.host._pixiv_processing_slots = slots
        self.host._pixiv_processing_concurrency = 2
        self.host._translate_image_content = AsyncMock(
            return_value="Must not translate"
        )
        task = self.start(1)
        await asyncio.wait_for(slots.queued.wait(), 1)
        self.host._content_policy.return_value = (False, "image")
        slots.release()
        with self.assertRaisesRegex(RuntimeError, "policy changed"):
            await asyncio.wait_for(task, 1)
        self.host._download.assert_not_awaited()
        self.host._translate_image_content.assert_not_awaited()

    async def test_default_ten_distinct_works_overlap_and_inflight_config_keeps_pool(
        self,
    ):
        self.host.cfg.pop("processing_concurrency")
        self.host.cfg["auto_translate"] = {"enable": True}
        entered = set()
        saturated = asyncio.Event()
        release = asyncio.Event()
        active = peak = 0

        async def metadata(title, description, *args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            entered.add(title)
            if active == 10:
                saturated.set()
            try:
                await release.wait()
                return title, description
            finally:
                active -= 1

        works = [dict(item(str(i)), title=f"work-{i}") for i in range(11)]
        self.host._translate_artwork_text = AsyncMock(side_effect=metadata)
        self.host._translate_image_content = AsyncMock(return_value="page")
        first = self.start(works=works)
        await asyncio.wait_for(saturated.wait(), 1)
        self.assertEqual(len(entered), 10)
        slots = self.host._pixiv_processing_slots
        self.host.cfg["processing_concurrency"] = 32
        second = self.start(
            works=[dict(item("99"), title="other-group")], origin="test:GroupMessage:2"
        )
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 2)
        self.assertEqual(peak, 10)
        self.assertEqual(len(entered), 12)
        self.assertIs(self.host._pixiv_processing_slots, slots)

    async def test_config_parsing_bounds_actual_overlap_and_pool_survives_idle(self):
        for configured, capacity in (
            (None, 10),
            (True, 10),
            (False, 10),
            ({}, 10),
            ("invalid", 10),
            (-5, 1),
            (0, 1),
            (1, 1),
            (3, 3),
            (32, 32),
            (99, 32),
        ):
            with self.subTest(configured=configured):
                self.host = BrowseHost(self.images[0])
                self.host.cfg["processing_concurrency"] = configured
                self.host.cfg["image_transport"] = "file"
                saturated = asyncio.Event()
                release = asyncio.Event()
                active = peak = 0

                async def fetch_pages(work):
                    nonlocal active, peak
                    active += 1
                    peak = max(peak, active)
                    if active == capacity:
                        saturated.set()
                    try:
                        await release.wait()
                        return [work]
                    finally:
                        active -= 1

                self.host._fetch_pages.side_effect = fetch_pages
                task = self.start(works=[item(str(i)) for i in range(capacity + 1)])
                await asyncio.wait_for(saturated.wait(), 1)
                self.assertEqual(active, capacity)
                slots = self.host._pixiv_processing_slots
                release.set()
                await asyncio.wait_for(task, 2)
                self.assertEqual(peak, capacity)
                self.assertEqual(active, 0)
                self.assertEqual(self.host._pixiv_processing_concurrency, capacity)
                self.host.cfg["processing_concurrency"] = 7
                await asyncio.wait_for(self.start(1), 1)
                self.assertIs(self.host._pixiv_processing_slots, slots)
                self.assertEqual(self.host._pixiv_processing_concurrency, capacity)

    async def test_metadata_and_page_text_share_custom_capacity_across_groups(self):
        self.host.cfg["auto_translate"] = {"enable": True}
        page_entered = asyncio.Event()
        metadata_entered = asyncio.Event()
        release = asyncio.Event()
        active = peak = 0
        stages = []

        async def metadata(title, description, *args):
            nonlocal active, peak
            if title == "first":
                return title, description
            active += 1
            peak = max(peak, active)
            stages.append("metadata")
            metadata_entered.set()
            try:
                await release.wait()
                return title, description
            finally:
                active -= 1

        async def translate(*args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            stages.append("page")
            page_entered.set()
            try:
                await release.wait()
                return "page"
            finally:
                active -= 1

        self.host._translate_artwork_text = AsyncMock(side_effect=metadata)
        self.host._translate_image_content = AsyncMock(side_effect=translate)
        first = self.start(works=[dict(item("1"), title="first")])
        await asyncio.wait_for(page_entered.wait(), 1)
        second = self.start(works=[item("2"), item("3")], origin="test:GroupMessage:2")
        await asyncio.wait_for(metadata_entered.wait(), 1)
        self.assertEqual(stages, ["page", "metadata"])
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 1)
        self.assertEqual(peak, 2)
        self.assertEqual(active, 0)

    async def test_later_work_completes_before_first_metadata_and_output_stays_ordered(
        self,
    ):
        self.host.cfg["auto_translate"] = {"enable": True}
        first_entered = asyncio.Event()
        second_page_done = asyncio.Event()
        release_first = asyncio.Event()

        async def metadata(title, description, *args):
            if title == "first":
                first_entered.set()
                await release_first.wait()
            else:
                await first_entered.wait()
            return title, description

        async def translate(image, *args):
            second_page_done.set()
            return "page"

        self.host._translate_artwork_text = AsyncMock(side_effect=metadata)
        self.host._translate_image_content = AsyncMock(side_effect=translate)
        task = self.start(
            works=[dict(item("1"), title="first"), dict(item("2"), title="second")]
        )
        await asyncio.wait_for(second_page_done.wait(), 1)
        self.assertFalse(task.done())
        release_first.set()
        result = must(await asyncio.wait_for(task, 1))
        pages = page_nodes(result.chain[0].nodes)
        self.assertIn("artworks/1", pages[0].content[0].text)
        self.assertIn("artworks/2", pages[1].content[0].text)

    async def test_capacity_one_multiple_works_and_pages_has_no_nested_deadlock(self):
        self.host.cfg["processing_concurrency"] = 1
        self.host.cfg["auto_translate"] = {"enable": True}
        self.host._translate_artwork_text = AsyncMock(
            side_effect=lambda title, description, *args: (title, description)
        )
        self.host._translate_image_content = AsyncMock(return_value="page")
        result = must(
            await asyncio.wait_for(
                self.start(works=[item("1", pages=3), item("2", pages=2)]), 1
            )
        )
        self.assertEqual(len(page_nodes(result.chain[0].nodes)), 5)
        self.assertEqual(self.host._translate_artwork_text.await_count, 2)
        self.assertEqual(self.host._translate_image_content.await_count, 5)
        self.assertEqual(self.host._pixiv_processing_slots._value, 1)

    async def test_collector_discards_every_page_of_failed_work_and_keeps_healthy(self):
        first_page_done = asyncio.Event()
        self.host._translate_image_content = AsyncMock(return_value="page")

        async def download(work, source, index, **kwargs):
            if work["id"] == "bad":
                if index == 0:
                    first_page_done.set()
                else:
                    await first_page_done.wait()
                    return None
            return self.images[index]

        self.host._download.side_effect = download
        failed = set()
        result = must(
            await asyncio.wait_for(
                self.start(
                    works=[item("bad", pages=2), item("good", pages=2)],
                    failed_work_ids=failed,
                ),
                1,
            )
        )
        self.assertEqual(failed, {"bad"})
        pages = page_nodes(result.chain[0].nodes)
        self.assertEqual(len(pages), 2)
        pending = list(result.chain[0].nodes)
        captions = []
        while pending:
            node = pending.pop()
            for part in node.content:
                if hasattr(part, "content"):
                    pending.append(part)
                if getattr(part, "text", ""):
                    captions.append(part.text)
        text = "\n".join(captions)
        self.assertIn("artworks/good", text)
        self.assertNotIn("artworks/bad", text)
        self.assertEqual(self.host._pixiv_processing_slots._value, 2)
