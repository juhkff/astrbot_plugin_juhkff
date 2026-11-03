"""日报：每日新闻简报，对齐上游 `apps/dailyReport.ts`。

上游的「日报」不是群聊统计，而是每天一张简报图，内容包括：

- 一言（hitokoto，分类「动画」）；
- 60 秒读懂世界（有 alapi token 用 alapi 早报，否则用 60s-api）；
- B 站热搜；
- IT 之家 RSS 头条；
- 番剧日历（今天更新的番）；
- 摸鱼日历（距离下个节日/周末还有几天）；
- 日期与星期。

出图用**本机 Chromium**（playwright，参数对齐上游 puppeteer：578×5 倍），
失败就直接报错、不退回文字。`#日报` 随时生成；开启 `push` 后按
`push_hour`/`push_minute` 定时推送，`pre_handle` 可提前生成缓存。
"""

from __future__ import annotations

import asyncio
import base64
import html
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.platform import MessageType

from .common import (
    BaseModule,
    PluginLike,
    data_dir,
    get_tz,
    resolve_umo,
    save_temp_image,
)
from .renderer import render_template

HITOKOTO_URL = "https://v1.hitokoto.cn/?c=a"
ALAPI_URL = "https://v2.alapi.cn/api/zaobao"
SIX_URL = "https://60s-api.viki.moe/v2/60s"
BILI_URL = "https://s.search.bilibili.com/main/hotword"
IT_URL = "https://www.ithome.com/rss/"
ANILIST_URL = "https://graphql.anilist.co"
# 今日播出的番剧：按本机时区的「今天」取 airingAt 区间
_ANILIST_QUERY = (
    "query($from:Int,$to:Int){Page(perPage:20){airingSchedules("
    "airingAt_greater:$from,airingAt_lesser:$to,sort:TIME){episode airingAt "
    "media{title{native romaji} coverImage{large}}}}}"
)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)
_ITEM_RE = re.compile(r"<item>.*?<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.S)

# 只放公历固定的节日；农历节日（春节/端午/中秋）每年日期不同，不硬编码
_FIXED_HOLIDAYS = [
    ("元旦", (1, 1)),
    ("清明节", (4, 4)),
    ("劳动节", (5, 1)),
    ("儿童节", (6, 1)),
    ("国庆节", (10, 1)),
]


_CN_NUM = "〇一二三四五六七八九十"


def _cn_num(n: int) -> str:
    """1..31 的中文写法：十一、二十、三十一。"""
    if n <= 10:
        return _CN_NUM[n]
    if n < 20:
        return "十" + _CN_NUM[n - 10]
    if n % 10 == 0:
        return _CN_NUM[n // 10] + "十"
    return _CN_NUM[n // 10] + "十" + _CN_NUM[n % 10]


def _week_short(day: date) -> str:
    """模板右上角只能放两个字：周一…周日。"""
    return "周" + "一二三四五六日"[day.weekday()]


def _zh_date(day: date) -> str:
    """十一月三日：模板一行 15px，写不下「2026年11月3日」。"""
    return f"{_cn_num(day.month)}月{_cn_num(day.day)}日"


class DailyReportModule(BaseModule):
    """`#日报` 出简报图；可定时推送、可提前生成缓存。

    流程：`_collect()` 并发拉 5 个数据源 → `_render()` 用 HTML 模板出图
    （失败退回纯文本）。缓存按「当天」有效，同一天内不会重复拉接口。
    """

    key = "daily_report"
    title = "日报"

    def __init__(self, plugin: PluginLike) -> None:
        super().__init__(plugin)
        self._cache_path: Path | None = None
        self._retrying = False
        self._lock = asyncio.Lock()

    def help_entries(self):
        return [
            ("#日报", "生成今日简报：一言、60 秒读世界、B 站热搜、IT 之家、番剧日历"),
        ]

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        """注册两个可选定时任务：提前生成缓存、定时推送（配置是「时:分」滑块）。"""
        self._cache_path = data_dir() / "daily_report.png"
        if bool(self.cfg.get("pre_handle", False)):
            await self.scheduler.daily(
                self._hhmm("pre_handle_hour", "pre_handle_minute", 10, 0),
                self._refresh_cache,
                "report-prehandle",
            )
        if bool(self.cfg.get("push", False)):
            await self.scheduler.daily(
                self._hhmm("push_hour", "push_minute", 12, 0),
                self._push,
                "report-push",
            )
        logger.info(
            f"[juhkff] 日报已就绪：推送 "
            f"{'开' if self.cfg.get('push') else '关'}，"
            f"预处理 {'开' if self.cfg.get('pre_handle') else '关'}"
        )

    def _hhmm(self, hour_key: str, minute_key: str, hour: int, minute: int) -> str:
        """把「时 / 分」两个滑块拼成 Scheduler.daily() 要的 HH:MM。"""
        try:
            h = int(self.cfg.get(hour_key, hour))
            m = int(self.cfg.get(minute_key, minute))
        except (TypeError, ValueError):
            return f"{hour:02d}:{minute:02d}"
        return f"{h % 24:02d}:{m % 60:02d}"

    async def stop(self) -> None:
        pass

    # ------------------------------------------------------------ 指令

    async def handle(self, event: AstrMessageEvent) -> MessageChain | None:
        # 三个等价触发词，照顾不同习惯
        text = (event.message_str or "").strip()
        if text not in ("#日报", "/日报", "日报"):
            return None
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return MessageChain().message("日报只对群聊开放")
        except Exception:
            pass
        return await self._build_chain()

    # ------------------------------------------------------------ 生成

    async def _build_chain(self) -> MessageChain:
        """优先用当天的缓存图，没有就现场生成；生成失败直接报错，不退回文字。"""
        path = self._cache_path if self._cache_ok() else await self._generate()
        if path is not None:
            return MessageChain().file_image(str(path))
        return MessageChain().message(
            "日报生成失败，详细原因看 AstrBot 日志（[juhkff] 开头）"
        )

    def _cache_ok(self) -> bool:
        """缓存文件是今天生成的就复用，跨天自动失效。"""
        if self._cache_path is None or not self._cache_path.exists():
            return False
        stamp = self._cache_path.stat().st_mtime
        return date.fromtimestamp(stamp).isoformat() == date.today().isoformat()

    async def _generate(self) -> Path | None:
        async with self._lock:
            data = await self._collect()
            return await self._render(data)

    async def _refresh_cache(self) -> None:
        if await self._generate() is not None:
            logger.info("[juhkff] 日报已预生成")
            return
        # 对齐上游 preHandleRetryInterval：预生成失败就隔一会儿再试，最多 5 次
        delay = max(30, int(self.cfg.get("pre_handle_retry_sec", 120) or 120))
        if self._retrying:
            return
        self._retrying = True
        try:
            for _ in range(5):
                await asyncio.sleep(delay)
                if await self._generate() is not None:
                    logger.info("[juhkff] 日报预生成重试成功")
                    return
            logger.warning("[juhkff] 日报预生成多次失败，等到推送时再试")
        finally:
            self._retrying = False

    async def _push(self) -> None:
        """把日报发到配置的群。群号支持纯数字或完整 umo。"""
        groups = [str(g) for g in (self.cfg.get("push_group_list") or []) if str(g)]
        if not groups:
            logger.info("[juhkff] 日报推送：没有配置推送群")
            return
        path = self._cache_path if self._cache_ok() else await self._generate()
        for target in groups:
            umo = resolve_umo(self.plugin.context, target)
            if path is None:
                logger.error("[juhkff] 日报推送：图片生成失败，本次不推送")
                return
            try:
                chain = MessageChain().file_image(str(path))
                await self.plugin.context.send_message(umo, chain)
            except Exception:
                logger.exception(f"[juhkff] 日报推送失败: {umo}")

    # ------------------------------------------------------------ 数据

    async def _collect(self) -> dict[str, Any]:
        """并发拉取所有板块；单个接口失败不影响其它板块。"""
        hitokoto, six, bili, it, anime = await asyncio.gather(
            self._hitokoto(),
            self._news(),
            self._bili_hot(),
            self._it_news(),
            self._anime(),
            return_exceptions=True,
        )
        today = date.today()
        return {
            "hitokoto": hitokoto if isinstance(hitokoto, str) and hitokoto else "人生如逆旅，我亦是行人",
            "six": six if isinstance(six, list) else [],
            "bili": bili if isinstance(bili, list) else [],
            "it": it if isinstance(it, list) else [],
            "anime": anime if isinstance(anime, list) else [],
            "festivals": _festivals(today),
            "week": _week_short(today),
            "date": today.strftime("%Y-%m-%d"),
            "zh_date": _zh_date(today),
            "full_show": bool(self.cfg.get("daily_report_full_show", True)),
        }

    async def _get(self, url: str, *, fmt: str = "json", timeout_sec: float = 20) -> Any:
        """带 UA / 代理的 GET，非 200 直接抛异常交给调用方兜底。"""
        timeout = aiohttp.ClientTimeout(total=timeout_sec)
        kwargs: dict[str, Any] = {"headers": {"User-Agent": UA}}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, **kwargs) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status} {url}")
                if fmt == "json":
                    return await resp.json(content_type=None)
                return await resp.text(errors="ignore")

    async def _post_json(
        self, url: str, payload: dict[str, Any], *, timeout_sec: float = 20
    ) -> Any:
        """带 UA / 代理的 POST JSON（AniList 用 GraphQL，必须 POST）。"""
        timeout = aiohttp.ClientTimeout(total=timeout_sec)
        kwargs: dict[str, Any] = {
            "headers": {"User-Agent": UA, "Content-Type": "application/json"},
            "json": payload,
        }
        if self.proxy:
            kwargs["proxy"] = self.proxy
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, **kwargs) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status} {url}")
                return await resp.json(content_type=None)

    async def _hitokoto(self) -> str:
        try:
            data = await self._get(HITOKOTO_URL)
            return str(data.get("hitokoto") or "").strip()
        except Exception:
            logger.warning("[juhkff] 日报：一言获取失败")
            return ""

    async def _news(self) -> list[str]:
        """60 秒新闻：有 alapi token 先用 alapi，失败自动降级到免费接口。"""
        token = str(self.cfg.get("alapi_token") or "").strip()
        raw: list[str] = []
        if token:
            try:
                data = await self._get(f"{ALAPI_URL}?token={token}")
                if data.get("success") is False:
                    raise RuntimeError(str(data.get("message") or "alapi 返回失败"))
                raw = [str(x) for x in (data.get("data") or {}).get("news") or []]
            except Exception as exc:
                logger.warning(f"[juhkff] 日报：alapi 失败（{exc}），改用 60s-api")
        if not raw:
            try:
                data = await self._get(SIX_URL)
                raw = [str(x) for x in (data.get("data") or {}).get("news") or []]
            except Exception:
                logger.warning("[juhkff] 日报：60 秒新闻获取失败")
                return []
        # 清洗：去掉「1、」「2.」这类序号前缀和句尾标点，最多保留 11 条
        cleaned: list[str] = []
        for item in raw:
            item = item.strip()
            match = re.match(r"^\d+[、.．]\s*", item)
            if match:
                item = item[match.end():]
            item = item.rstrip("。！；？")
            if item:
                cleaned.append(item)
        return cleaned[:11]

    async def _bili_hot(self) -> list[dict[str, str]]:
        try:
            data = await self._get(BILI_URL)
        except Exception:
            logger.warning("[juhkff] 日报：B 站热搜获取失败")
            return []
        items = []
        for row in (data.get("list") or [])[:10]:
            keyword = str(row.get("keyword") or "").strip()
            if keyword:
                items.append({"keyword": keyword, "icon": str(row.get("icon") or "")})
        return items

    async def _it_news(self) -> list[str]:
        try:
            text = await self._get(IT_URL, fmt="text")
        except Exception:
            logger.warning("[juhkff] 日报：IT 之家 RSS 获取失败")
            return []
        # RSS 有时不是合法 XML（CDATA / 注释），解析失败就用正则兜底
        try:
            root = ET.fromstring(text)
            titles = [
                (item.findtext("title") or "").strip()
                for item in root.iter("item")
            ]
        except ET.ParseError:
            titles = [html.unescape(t.strip()) for t in _ITEM_RE.findall(text)]
        return [t for t in titles if t][:10]

    def _today_window(self) -> tuple[int, int]:
        """本机时区「今天」的 [00:00, 次日 00:00) 时间戳。"""
        tz = get_tz(self.plugin.config.get("global", {}).get("timezone") or None)
        start = datetime.combine(date.today(), time.min, tzinfo=tz)
        end = start + timedelta(days=1)
        return int(start.timestamp()), int(end.timestamp())

    async def _anime(self) -> list[dict[str, str]]:
        """今日新番：AniList 的每日放送（bgm.tv 在这台机器上 API 和图床都不通）。"""
        start, end = self._today_window()
        try:
            data = await self._post_json(
                ANILIST_URL,
                {"query": _ANILIST_QUERY, "variables": {"from": start, "to": end}},
                timeout_sec=15,
            )
        except Exception:
            logger.warning("[juhkff] 日报：番剧时间表获取失败")
            return []
        rows = ((data or {}).get("data") or {}).get("Page", {}).get("airingSchedules") or []
        items: list[dict[str, str]] = []
        seen: set[str] = set()
        for row in rows:
            media = row.get("media") or {}
            title = media.get("title") or {}
            name = str(title.get("native") or title.get("romaji") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            items.append(
                {"name": name, "image": str((media.get("coverImage") or {}).get("large") or "")}
            )
            if len(items) >= 8:
                break
        return items

    # ------------------------------------------------------------ 渲染

    def _template_data(self, data: dict[str, Any]) -> dict[str, Any]:
        """把采集结果整理成模板变量。

        `full_show` 对齐上游：条数不变，关掉时每条只显示一行（超出用省略号），
        打开时完整换行显示。
        """
        return {
            "data": {
                "week": data["week"],
                "date": data["date"],
                "zh_date": data["zh_date"],
                "data_hitokoto": data["hitokoto"],
                "data_six": data["six"],
                # 上游模板里 bili/anime/festival 都是 [文字, 图片]/[天数, 名称] 的两元组
                "data_bili": [
                    [b["keyword"], b["icon"] or _EMPTY_IMG] for b in data["bili"]
                ],
                "data_it": data["it"],
                "data_anime": [
                    [a["name"], a["image"] or _EMPTY_IMG] for a in data["anime"]
                ],
                "data_festival": [[f["days"], f["name"]] for f in data["festivals"]],
                "full_show": data["full_show"],
            }
        }

    async def _render(self, data: dict[str, Any]) -> Path | None:
        """用本机 Chromium 渲染日报图；失败返回 None，由调用方报错（不退回文字）。"""
        payload = self._template_data(data)
        png = await render_template(_load_html(), payload)
        if not png:
            logger.error("[juhkff] 日报：渲染失败")
            return None
        try:
            return Path(save_temp_image(png, ".png"))
        except Exception:
            logger.exception("[juhkff] 日报：保存图片失败")
            return None

def _festivals(today: date) -> list[dict[str, Any]]:
    """摸鱼日历：公历节日 + 下个周六，按剩余天数排序取前 4 个。"""
    marks: list[tuple[str, date]] = []
    for name, (month, day) in _FIXED_HOLIDAYS:
        for year in (today.year, today.year + 1):
            try:
                when = date(year, month, day)
            except ValueError:
                continue
            if when >= today:
                marks.append((name, when))
                break
    days_to_saturday = (5 - today.weekday()) % 7 or 7
    marks.append(("周末", today + timedelta(days=days_to_saturday)))
    marks.sort(key=lambda pair: pair[1])
    return [{"name": name, "days": (when - today).days} for name, when in marks[:4]]


_RES_DIR = Path(__file__).resolve().parent.parent / "resources" / "daily_report"
_HTML_CACHE: str | None = None
# 1x1 透明图：上游模板里 icon/封面为空的项不至于显示成裂图
_EMPTY_IMG = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"


def _data_uri(path: Path) -> str:
    """把本地图片转成 data URI，避免渲染端拿不到相对路径。"""
    suffix = path.suffix.lower().lstrip(".")
    if suffix in {"ttf", "otf", "woff", "woff2"}:
        mime = f"font/{suffix}"
    else:
        mime = f"image/{'jpeg' if suffix == 'jpg' else suffix}"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def _load_html() -> str:
    """加载上游「真寻日报」模板：内联 CSS、图片转 data URI。

    上游 3 个字体共 19MB，全部内联不现实：
    - 标题字体只保留「真寻日报」4 个字（subset 后 2KB），保住手写体标题；
    - 正文字体交给渲染端的系统中文字体。
    """
    global _HTML_CACHE
    if _HTML_CACHE is not None:
        return _HTML_CACHE
    html = (_RES_DIR / "main.html").read_text(encoding="utf-8")
    css = (_RES_DIR / "main.css").read_text(encoding="utf-8")
    title_uri = _data_uri(_RES_DIR / "res" / "font" / "title.ttf")

    def _face(block: str) -> str:
        if "SSFangTangTi" not in block:
            return ""
        return (
            '@font-face { font-family: "SSFangTangTi"; '
            f'src: url("{title_uri}") format("truetype"); }}'
        )

    css = re.sub(r"@font-face\s*\{[^}]*\}", lambda m: _face(m.group(0)), css)
    css += (
        '\nbody, .wrapper { font-family: "Noto Sans CJK SC", "Noto Sans SC", "WenQuanYi Zen Hei", sans-serif; }\n'
        # 卡片天然 608px 宽（wrapper 578 + 左右 padding 15），视口与之对齐即可；
        # 不能用 flex 居中：flex 子项会被拉到视口高度，截图底部会多出一大片空白
        "html, body { margin: 0; padding: 0; background: #fff; }\n"
    )
    html = html.replace(
        '<link rel="stylesheet" href="main.css" />', f"<style>{css}</style>"
    )
    for path in sorted((_RES_DIR / "res").rglob("*.png")):
        rel = "./" + path.relative_to(_RES_DIR).as_posix()
        html = html.replace(f'src="{rel}"', f'src="{_data_uri(path)}"')
    _HTML_CACHE = html
    return html
