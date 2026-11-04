"""Generate news reports with five selectable local Chromium templates."""

from __future__ import annotations

import asyncio
import random
from datetime import date, datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.platform import MessageType

from .common import (
    BaseModule,
    PluginLike,
    data_dir,
    get_tz,
    load_json,
    resolve_umo,
    save_json,
)
from .renderer import renderer
from .report_sources import NewsItem, collect_sections
from .report_templates import TEMPLATES, build_report, get_template

_SECTION_KEYS = ("politics", "society", "games", "tech")


def _style_id(value: str) -> str | None:
    """Resolve a configured style ID, displayed name, or numeric alias."""
    value = value.strip().lower()
    if value in ("random", "随机"):
        return "random"
    for index, template in enumerate(TEMPLATES, 1):
        if value in (template.id, template.label, str(index)):
            return template.id
    return None


class DailyReportModule(BaseModule):
    """Collect four news sections once daily and cache each rendered style."""

    key = "daily_report"
    title = "日报"

    def __init__(self, plugin: PluginLike) -> None:
        super().__init__(plugin)
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[None] | None = None

    def help_entries(self) -> list[tuple[str, str]]:
        return [("#日报", "生成今日日报：时政、国内社会、游戏、数码")]

    async def start(self) -> None:
        """Register optional pre-generation and scheduled delivery jobs."""
        if bool(self.cfg.get("pre_handle", False)):
            await self.scheduler.daily(
                self._hhmm("pre_handle_hour", "pre_handle_minute", 10, 0),
                self._refresh_cache,
                "report-prehandle",
                tz_name=self.tz_name,
            )
        if bool(self.cfg.get("push", True)) and self.cfg.get("push_group_list"):
            await self.scheduler.daily(
                self._hhmm("push_hour", "push_minute", 12, 0),
                self._push,
                "report-push",
                tz_name=self.tz_name,
            )
        logger.info(
            "[juhkff] Daily report ready: style=%s, push=%s, pre_handle=%s",
            self.cfg.get("style", "random"),
            bool(self.cfg.get("push", True)),
            bool(self.cfg.get("pre_handle")),
        )

    def _hhmm(self, hour_key: str, minute_key: str, hour: int, minute: int) -> str:
        """Format the two schedule sliders as HH:MM."""
        try:
            h = int(self.cfg.get(hour_key, hour))
            m = int(self.cfg.get(minute_key, minute))
        except (TypeError, ValueError):
            return f"{hour:02d}:{minute:02d}"
        return f"{h % 24:02d}:{m % 60:02d}"

    async def stop(self) -> None:
        """Cancel a pending pre-generation retry when unloading the module."""
        task = self._refresh_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def intercepts(self, event: AstrMessageEvent) -> bool:
        """Reserve report commands before passive message processing.

        Args:
            event: Incoming message, possibly with its wake prefix removed.

        Returns:
            Whether this module owns the report command.
        """
        text = (event.message_str or "").strip()
        return text in ("#日报", "/日报", "日报")

    async def handle(self, event: AstrMessageEvent) -> MessageChain | None:
        """Generate a report using the style selected in settings."""
        if not self.intercepts(event):
            return None
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return MessageChain().message("日报只对群聊开放")
        return await self._build_chain()

    def _today(self) -> date:
        """Use the configured timezone for both the calendar and cache date."""
        return datetime.now(get_tz(self.tz_name)).date()

    async def _build_chain(self) -> MessageChain:
        """Return the configured report image or a generation error."""
        path = await self._generate()
        if path is not None:
            return MessageChain().file_image(str(path))
        return MessageChain().message(
            "日报生成失败，详细原因看 AstrBot 日志（[juhkff] 开头）"
        )

    async def _generate(self, style: str | None = None) -> Path | None:
        """Choose a style for this request and reuse that day's news and image.

        The image cache is indexed by style as well as date. A random request
        therefore still chooses from all five styles after earlier requests.
        Pre-generation may select a template without changing the settings.
        """
        selected = style or _style_id(str(self.cfg.get("style", "random")))
        if selected is None:
            logger.warning("[juhkff] Invalid report style; using random selection")
        if selected in (None, "random"):
            selected = random.choice(TEMPLATES).id
        template = get_template(selected)
        day = self._today()
        folder = data_dir() / "daily_report" / day.isoformat()
        path = folder / f"{template.id}.png"
        # Cached requests need not wait for another style's download or render.
        try:
            if path.is_file() and path.stat().st_size:
                return path
        except OSError:
            pass
        async with self._lock:
            try:
                if path.is_file() and path.stat().st_size:
                    return path
                folder.mkdir(parents=True, exist_ok=True)
                sections = await self._collect(day, folder)
                document = build_report(sections, day, template.id)
                png = await renderer.render(
                    document,
                    scale=2.5,
                    viewport={"width": template.width, "height": 1000},
                )
                if not png:
                    raise RuntimeError("Local renderer returned an empty report image")
                temporary = path.with_suffix(".tmp")
                temporary.write_bytes(png)
                temporary.replace(path)
                logger.info(
                    "[juhkff] Report rendered: day=%s, style=%s, counts=%s",
                    day.isoformat(),
                    template.id,
                    {key: len(items) for key, items in sections.items()},
                )
                return path
            except Exception:
                logger.exception("[juhkff] Daily report generation failed")
                return None

    async def _collect(self, day: date, folder: Path) -> dict[str, list[NewsItem]]:
        """Load a validated daily snapshot or collect current relevant news."""
        snapshot = folder / "news.json"
        cached = load_json(snapshot, {})
        if isinstance(cached, dict) and cached.get("date") == day.isoformat():
            raw = cached.get("sections")
            if isinstance(raw, dict):
                sections: dict[str, list[NewsItem]] = {}
                for key in _SECTION_KEYS:
                    rows = raw.get(key)
                    if not isinstance(rows, list):
                        break
                    items: list[NewsItem] = []
                    for row in rows[:12]:
                        if not isinstance(row, dict) or not all(
                            isinstance(row.get(field), str)
                            for field in ("title", "source", "url", "published")
                        ):
                            break
                        item = NewsItem(
                            title=row["title"],
                            source=row["source"],
                            url=row["url"],
                            published=row["published"],
                            image_path=row.get("image_path")
                            if isinstance(row.get("image_path"), str)
                            else None,
                            image_source=row.get("image_source")
                            if isinstance(row.get("image_source"), str)
                            else None,
                        )
                        if isinstance(row.get("image_url"), str):
                            item["image_url"] = row["image_url"]
                        if isinstance(row.get("image_article_url"), str):
                            item["image_article_url"] = row["image_article_url"]
                        items.append(item)
                    else:
                        sections[key] = items
                        continue
                    break
                else:
                    if any(sections.values()):
                        return sections
        sections = await collect_sections(self.proxy or None, folder / "images")
        if not any(sections.values()):
            raise RuntimeError("No usable news was returned by any report source")
        save_json(snapshot, {"date": day.isoformat(), "sections": sections})
        return sections

    async def _refresh_cache(self) -> None:
        """Warm all random choices or one fixed style, retrying failures five times."""
        if self._refresh_task is not None:
            return
        self._refresh_task = asyncio.current_task()
        try:
            try:
                delay = max(30, int(self.cfg.get("pre_handle_retry_sec", 120) or 120))
            except (TypeError, ValueError):
                delay = 120
            selected = _style_id(str(self.cfg.get("style", "random")))
            pending = (
                [template.id for template in TEMPLATES]
                if selected in (None, "random")
                else [selected]
            )
            for attempt in range(6):
                failed = []
                for style in pending:
                    if await self._generate(style) is None:
                        failed.append(style)
                if not failed:
                    logger.info("[juhkff] Daily report pre-generation completed")
                    return
                pending = failed
                if attempt < 5:
                    await asyncio.sleep(delay)
            logger.warning("[juhkff] Report pre-generation failed after five retries")
        finally:
            self._refresh_task = None

    async def _push(self) -> None:
        """Send one selected report image to all configured delivery groups."""
        if not bool(self.cfg.get("push", True)):
            return
        groups = [
            str(g).strip()
            for g in (self.cfg.get("push_group_list") or [])
            if g is not None and str(g).strip()
        ]
        if not groups:
            logger.info("[juhkff] Report delivery skipped: no groups configured")
            return
        path = await self._generate()
        if path is None:
            logger.error("[juhkff] Report delivery skipped: image generation failed")
            return
        for target in groups:
            try:
                umo = resolve_umo(self.plugin.context, target)
                await self.plugin.context.send_message(
                    umo, MessageChain().file_image(str(path))
                )
            except Exception:
                logger.exception("[juhkff] Report delivery failed: %s", target)
