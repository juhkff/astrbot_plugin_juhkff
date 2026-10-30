"""群聊统计。

由原「日报」功能里的群聊统计部分拆出（上游 juhkff-plugin 的日报
其实是每日新闻简报，见 daily_report.py；本模块是额外保留的统计能力）。

设计：
- 收到群消息时把元数据落进自建 SQLite（report.db）：会话、发送人、时间、
  类型、文本长度、是否带图、截断后的文本（只用于高频词统计）；
- 每天 `push_time` 统计当天数据：消息数 / 活跃人数 / 发言榜 / 图片数 /
  按小时分布 / 高频词；
- `render_mode=text` 发纯文本；`render_mode=image` 优先用 AstrBot 内置的
  文转图服务（`context.html_renderer.render_custom_template`）渲染 HTML 卡片，
  失败时退回本地 Pillow，再失败退回纯文本；
- 支持 `/统计` 手动查看当前群当天数据。
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image
from astrbot.api.platform import MessageType

from .common import (
    BaseModule,
    PluginLike,
    data_dir,
    download_image_bytes,
    get_tz,
    message_text,
    truncate,
)

_WORD_RE = re.compile(r"[\u4e00-\u9fa5]{2,}|[A-Za-z]{3,}")

# 高频词里没有信息量的词，够用即可
_STOPWORDS = {
    "什么",
    "怎么",
    "这个",
    "那个",
    "可以",
    "就是",
    "没有",
    "不是",
    "还是",
    "自己",
    "现在",
    "感觉",
    "已经",
    "时候",
    "因为",
    "所以",
    "但是",
    "然后",
    "如果",
    "这样",
    "知道",
    "一个",
    "我们",
    "你们",
    "他们",
    "哈哈",
    "哈哈哈",
    "哈哈哈哈哈",
    "草",
    "的",
    "了",
    "在",
    "是",
    "我",
    "你",
    "他",
    "the",
    "and",
    "for",
    "you",
}

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    umo TEXT NOT NULL,
    sender_id TEXT,
    sender_name TEXT,
    ts INTEGER NOT NULL,
    msg_type TEXT,
    text_len INTEGER DEFAULT 0,
    has_image INTEGER DEFAULT 0,
    text TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_umo_ts ON messages (umo, ts);
"""


class ChatStatsModule(BaseModule):
    """群聊活跃度统计：消息落库 → 定时 / `/统计` 出榜。

    落库只存元数据 + 截断文本（高频词用），不存原图；
    `render_mode=image` 时优先用 AstrBot 的文转图，失败退回本地 Pillow。
    """

    key = "chat_stats"
    title = "群聊统计"

    def __init__(self, plugin: PluginLike) -> None:
        super().__init__(plugin)
        self._db_path: Path | None = None

    def help_entries(self):
        return [("/统计", "生成本群今天的活跃度统计（发言榜/时段/热词）")]


    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        """建库、注册「每日推送」和「清理过期数据」两个定时任务。"""
        self._db_path = data_dir() / "report.db"
        await asyncio.to_thread(self._init_db)
        await self.scheduler.daily(
            self.cfg.get("push_time", "23:59"),
            self.push_all,
            "daily-report",
            self.tz_name,
        )
        await self.scheduler.every_minutes(
            360, self._cleanup, "daily-report-cleanup", self.tz_name
        )
        logger.info(f"[juhkff] 群聊统计已就绪：{self._db_path}")

    def _connect(self) -> sqlite3.Connection:
        db_path = self._db_path
        if db_path is None:
            raise RuntimeError("daily report database is not initialized")
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        # WAL 模式：读写并发时不容易被锁住
        with self._connect() as conn:
            conn.executescript(_CREATE_SQL)
            conn.execute("PRAGMA journal_mode=WAL")

    async def _cleanup(self) -> None:
        """按保留天数删旧数据（最少保留 7 天）。"""
        days = max(7, int(self.cfg.get("retention_days", 90)))
        cutoff = int(time.time()) - days * 86400

        def delete_expired() -> int:
            with self._connect() as conn:
                return conn.execute(
                    "DELETE FROM messages WHERE ts < ?", (cutoff,)
                ).rowcount

        deleted = await asyncio.to_thread(delete_expired)
        if deleted:
            logger.info(f"[juhkff] 统计数据清理：删除 {deleted} 条过期记录")

    # ------------------------------------------------------------ 消息

    async def handle(self, event: AstrMessageEvent) -> MessageChain | None:
        # 先看是不是统计指令；其它以 / 开头的消息不当聊天记录
        text = message_text(event).strip()
        if text in ("/统计", "统计", "/juhkff统计"):
            return await self._manual_report(event)
        if text.startswith("/"):
            return None
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return None
        except Exception:
            return None
        await self._record(event, text)
        return None

    async def _record(self, event: AstrMessageEvent, text: str) -> None:
        """把一条群消息的元数据写进 SQLite（异步线程里执行，不阻塞事件循环）。"""
        umo = event.unified_msg_origin
        if not umo:
            return
        groups = self.cfg.get("groups") or []
        if groups and umo not in groups:
            return
        if event.get_self_id() == event.get_sender_id():
            return

        chain = event.message_obj.message or []
        has_image = any(isinstance(seg, Image) for seg in chain)
        msg_type = chain[0].type.value.lower() if chain else "text"

        row = (
            umo,
            str(event.get_sender_id() or ""),
            str(event.get_sender_name() or ""),
            int(time.time()),
            msg_type,
            len(text),
            int(has_image),
            truncate(text, 200),
        )
        try:
            await asyncio.to_thread(self._insert, row)
        except Exception as exc:
            logger.warning(f"[juhkff] 统计写入失败: {exc}")

    def _insert(self, row: tuple[str, str, str, int, str, int, int, str]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO messages "
                "(umo, sender_id, sender_name, ts, msg_type, text_len, has_image, text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )

    # ------------------------------------------------------------ 统计

    def _day_range(self) -> tuple[int, int]:
        """今天 00:00 到明天 00:00 的 Unix 时间戳区间（按配置时区）。"""
        tz = get_tz(self.tz_name)
        start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        return int(start.timestamp()), int(start.timestamp()) + 86400

    def _stats(self, umo: str, start: int, end: int) -> dict[str, Any]:
        """在 SQL 里做聚合：总数、去重人数、带图数、发言榜、时段分布、高频词。"""
        with self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE umo=? AND ts>=? AND ts<?",
                (umo, start, end),
            ).fetchone()[0]
            if not total:
                return {"umo": umo, "total": 0}

            users = conn.execute(
                "SELECT COUNT(DISTINCT sender_id) FROM messages "
                "WHERE umo=? AND ts>=? AND ts<?",
                (umo, start, end),
            ).fetchone()[0]
            images = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE umo=? AND ts>=? AND ts<? "
                "AND has_image=1",
                (umo, start, end),
            ).fetchone()[0]
            top = conn.execute(
                "SELECT sender_id, sender_name, COUNT(*) AS c FROM messages "
                "WHERE umo=? AND ts>=? AND ts<? "
                "GROUP BY sender_id ORDER BY c DESC LIMIT ?",
                (umo, start, end, max(1, int(self.cfg.get("top_n", 5)))),
            ).fetchall()
            hours = conn.execute(
                "SELECT (ts/3600) % 24 AS h, COUNT(*) AS c FROM messages "
                "WHERE umo=? AND ts>=? AND ts<? GROUP BY h",
                (umo, start, end),
            ).fetchall()
            texts = conn.execute(
                "SELECT text FROM messages WHERE umo=? AND ts>=? AND ts<? "
                "AND text IS NOT NULL AND text != ''",
                (umo, start, end),
            ).fetchall()

        return {
            "umo": umo,
            "total": total,
            "users": users,
            "images": images,
            "top": [
                (row["sender_name"] or row["sender_id"] or "?", row["c"]) for row in top
            ],
            "hours": sorted(
                ((row["h"], row["c"]) for row in hours),
                key=lambda x: x[1],
                reverse=True,
            ),
            # 把当天所有文本拼起来再分词统计高频词
            "words": _top_words(["".join(row["text"] for row in texts)]),
        }

    # ------------------------------------------------------------ 推送

    async def push_all(self) -> None:
        """每日定时推送：给每个有数据的群发一份统计。"""
        start, end = self._day_range()
        groups = list(self.cfg.get("groups") or [])
        # 没配置固定群时，自动挑今天有聊天记录的群
        if not groups:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT DISTINCT umo FROM messages WHERE ts>=? AND ts<?",
                    (start, end),
                ).fetchall()
            groups = [row["umo"] for row in rows]
        if not groups:
            logger.info("[juhkff] 统计：今天没有可推送的数据")
            return

        for umo in groups:
            try:
                data = await asyncio.to_thread(self._stats, umo, start, end)
                if not data.get("total"):
                    continue
                chain = await self._build_chain(data)
                await self.plugin.context.send_message(umo, chain)
                await asyncio.sleep(1.5)
            except Exception:
                logger.exception(f"[juhkff] 统计推送失败: {umo}")

    async def _manual_report(self, event: AstrMessageEvent) -> MessageChain | None:
        umo = event.unified_msg_origin
        if not umo:
            return None
        start, end = self._day_range()
        data = await asyncio.to_thread(self._stats, umo, start, end)
        if not data.get("total"):
            return event.plain_result("今天还没有聊天记录，攒点数据再来吧～")
        chain = await self._build_chain(data)
        return event.chain_result(list(chain.chain))

    async def _build_chain(self, data: dict[str, Any]) -> MessageChain:
        """text 模式直接发文字；image 模式在文字后附图（图挂了不影响文字）。"""
        text = self._render_text(data)
        if self.cfg.get("render_mode", "text") == "image":
            path = await self._render_image(data)
            if path is not None:
                return MessageChain().message(text).file_image(str(path))
        return MessageChain().message(text)

    # ------------------------------------------------------------ 渲染

    async def _render_image(self, data: dict[str, Any]) -> Path | None:
        """优先用 AstrBot 内置文转图，失败退回本地 Pillow。"""
        path = await self._render_via_astrbot(data)
        if path is not None and Path(path).exists():
            return path
        try:
            return await asyncio.to_thread(self._render_png, data)
        except Exception as exc:
            logger.warning(f"[juhkff] 统计图片渲染失败，退回纯文本: {exc}")
            return None

    async def _render_via_astrbot(self, data: dict[str, Any]) -> Path | None:
        """走 AstrBot 的 html_renderer（Jinja2 模板 + 内置 T2I 服务）。"""
        try:
            renderer = self.plugin.context.html_renderer
        except AttributeError:
            return None
        try:
            out = await asyncio.wait_for(
                renderer.render_custom_template(
                    _HTML_TEMPLATE,
                    self._template_data(data),
                    return_url=False,
                    options={"type": "png", "quality": 95, "full_page": True},
                ),
                timeout=45,
            )
        except Exception as exc:
            logger.warning(f"[juhkff] AstrBot 文转图失败，改用本地 Pillow: {exc}")
            return None
        if not out:
            return None
        if str(out).startswith(("http://", "https://")):
            try:
                payload = await download_image_bytes(str(out))
            except Exception as exc:
                logger.warning(f"[juhkff] 下载 AstrBot 渲染结果失败: {exc}")
                return None
            path = self._report_path(data, ".png")
            path.write_bytes(payload)
            return path
        return Path(out)

    def _report_path(self, data: dict[str, Any], suffix: str) -> Path:
        """报告落盘路径：reports/daily_<群>_<日期>.png。"""
        report_dir = data_dir() / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^0-9A-Za-z]+", "_", str(data.get("umo", "group")))[:40]
        stamp = datetime.now(get_tz(self.tz_name)).strftime("%Y%m%d")
        return report_dir / f"daily_{safe}_{stamp}{suffix}"

    def _template_data(self, data: dict[str, Any]) -> dict[str, Any]:
        """给模板算好百分比：发言榜按最高者归一，时段按峰值归一。"""
        top = data.get("top") or []
        top_max = max((count for _, count in top), default=1) or 1
        by_hour = {hour: count for hour, count in (data.get("hours") or [])}
        hour_max = max(by_hour.values(), default=1) or 1
        return {
            "day": datetime.now(get_tz(self.tz_name)).strftime("%Y-%m-%d"),
            "total": data.get("total", 0),
            "users": data.get("users", 0),
            "images": data.get("images", 0),
            "top": [
                {
                    "name": name,
                    "count": count,
                    "pct": round(count * 100 / top_max, 1),
                }
                for name, count in top
            ],
            "hours": [
                {
                    "hour": hour,
                    "count": by_hour.get(hour, 0),
                    "pct": round(by_hour.get(hour, 0) * 100 / hour_max, 1),
                }
                for hour in range(24)
            ],
            "words": [
                {"word": word, "count": count}
                for word, count in (data.get("words") or [])
            ],
        }

    def _render_text(self, data: dict[str, Any]) -> str:
        day = datetime.now(get_tz(self.tz_name)).strftime("%m-%d")
        lines = [
            f"📊 群聊统计 {day}",
            f"消息 {data['total']} 条 · 活跃 {data.get('users', 0)} 人 · "
            f"图片 {data.get('images', 0)} 张",
        ]
        if data.get("top"):
            lines.append("发言榜：")
            for index, (name, count) in enumerate(data["top"], 1):
                lines.append(f"  {index}. {name} — {count} 条")
        if data.get("hours"):
            clock = "、".join(f"{h}时({c})" for h, c in data["hours"][:3])
            lines.append(f"最活跃时段：{clock}")
        if data.get("words"):
            lines.append("高频词：" + "、".join(word for word, _ in data["words"][:8]))
        return "\n".join(lines)

    def _render_png(self, data: dict[str, Any]) -> Path:
        """本地 Pillow 兜底：把纯文本逐行画到白底卡片上。"""
        from PIL import Image as PILImage
        from PIL import ImageDraw

        width, padding, line_height = 720, 32, 40
        lines = self._render_text(data).split("\n")
        height = padding * 2 + line_height * len(lines) + 20
        canvas = PILImage.new("RGB", (width, height), "#f6f7f9")
        draw = ImageDraw.Draw(canvas)
        font = _load_font(self.cfg.get("font_path", ""))
        y = padding
        for index, line in enumerate(lines):
            draw.text((padding, y), line, fill="#202020", font=font)
            y += line_height if index else line_height + 6

        report_dir = data_dir() / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        path = self._report_path(data, ".png")
        canvas.save(path)
        return path


# ---------------------------------------------------------------- 工具


def _top_words(texts: list[str], limit: int = 10) -> list[tuple[str, int]]:
    """中文按 2 字以上连续片段、英文按 3 字母以上单词切，再去停用词。"""
    counter: Counter[str] = Counter()
    for text in texts:
        for word in _WORD_RE.findall(text or ""):
            if word in _STOPWORDS or len(word) < 2:
                continue
            counter[word] += 1
    return counter.most_common(limit)


def _load_font(font_path: str):
    """按「配置字体 → Noto → 文泉驿 → DejaVu」顺序找第一个可用中文字体。"""
    from PIL import ImageFont

    candidates = [
        font_path,
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, 24)
            except OSError:
                continue
    return ImageFont.load_default()


# ---------------------------------------------------------------- 模板

# 交给 AstrBot 内置文转图服务渲染的 HTML 卡片（Jinja2 语法）。
_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><style>
  body {margin:0;padding:32px;background:#eef2f7;font-family:-apple-system,"Noto Sans CJK SC","Microsoft YaHei",sans-serif;color:#1f2937;}
  .card {max-width:720px;margin:0 auto;background:#fff;border-radius:18px;padding:28px 30px;box-shadow:0 8px 28px rgba(15,23,42,.08);}
  .title {font-size:26px;font-weight:700;margin:0;}
  .day {color:#6b7280;font-size:14px;margin:6px 0 22px;}
  .stats {display:flex;gap:12px;}
  .stat {flex:1;background:#f8fafc;border-radius:14px;padding:14px;text-align:center;}
  .stat .num {font-size:26px;font-weight:700;color:#2563eb;}
  .stat .label {font-size:12px;color:#6b7280;margin-top:2px;}
  h3 {font-size:15px;margin:22px 0 10px;color:#374151;}
  .row {display:flex;align-items:center;gap:10px;margin:7px 0;font-size:14px;}
  .row .name {width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .bar {flex:1;height:10px;background:#eef2ff;border-radius:6px;overflow:hidden;}
  .bar i {display:block;height:100%;background:#4f46e5;border-radius:6px;}
  .row .val {width:60px;text-align:right;color:#4b5563;font-size:13px;}
  .hours {display:flex;align-items:flex-end;gap:5px;height:72px;background:#f8fafc;border-radius:12px;padding:10px 12px;}
  .hours div {flex:1;background:#a5b4fc;border-radius:4px 4px 2px 2px;min-height:3px;}
  .tags span {display:inline-block;background:#f1f5f9;color:#334155;border-radius:999px;padding:5px 11px;margin:3px 5px 0 0;font-size:13px;}
  .foot {margin-top:24px;color:#9ca3af;font-size:12px;text-align:right;}
</style></head>
<body><div class="card">
  <div class="title">📊 群聊统计</div>
  <div class="day">{{ day }}</div>
  <div class="stats">
    <div class="stat"><div class="num">{{ total }}</div><div class="label">消息</div></div>
    <div class="stat"><div class="num">{{ users }}</div><div class="label">活跃人数</div></div>
    <div class="stat"><div class="num">{{ images }}</div><div class="label">图片</div></div>
  </div>
  {% if top %}
  <h3>发言榜</h3>
  {% for item in top %}
  <div class="row"><div class="name">{{ item.name }}</div><div class="bar"><i style="width: {{ item.pct }}%"></i></div><div class="val">{{ item.count }} 条</div></div>
  {% endfor %}
  {% endif %}
  {% if hours %}
  <h3>活跃时段</h3>
  <div class="hours">{% for h in hours %}<div style="height: {{ h.pct }}%" title="{{ h.hour }}:00"></div>{% endfor %}</div>
  {% endif %}
  {% if words %}
  <h3>高频词</h3>
  <div class="tags">{% for w in words %}<span>{{ w.word }} · {{ w.count }}</span>{% endfor %}</div>
  {% endif %}
  <div class="foot">AstrBot · juhkff</div>
</div></body></html>"""
