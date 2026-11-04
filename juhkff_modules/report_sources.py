"""Collect the four report sections from ordered, live editorial sources."""

from __future__ import annotations

import asyncio
import hashlib
import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from io import BytesIO
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlencode, urljoin, urlsplit

import aiohttp
from bs4 import BeautifulSoup
from PIL import Image, ImageStat, UnidentifiedImageError

from astrbot.api import logger

from .http_utils import public_request

_CST = timezone(timedelta(hours=8))
_MAX_ITEMS = 12
_FEED_TIMEOUT = 5.0
_MEDIA_TIMEOUT = 6.0
_PHOTO_SECTION_TIMEOUT = 24.0
_MAX_DOCUMENT = 3 * 1024 * 1024
_MAX_IMAGE = 10 * 1024 * 1024
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"


class _ImageProvenance(TypedDict, total=False):
    image_url: str
    image_article_url: str


class NewsItem(_ImageProvenance):
    """An attributed article with a real publication date and optional local photo."""

    title: str
    source: str
    url: str
    published: str
    image_path: str | None
    image_source: str | None


@dataclass(frozen=True)
class Source:
    """An editorial source; tuple order controls fallback priority."""

    name: str
    url: str
    kind: str = "rss"
    publisher_domain: str = ""


# Edit these tuples to change priorities. A nonempty result stops the fallback;
# neither old dates nor a short result cause a lower-priority source to be used.
POLITICS_SOURCES = (
    Source(
        "中国新闻网",
        "https://www.chinanews.com.cn/scroll-news/news1.html",
        "cn_rolling",
    ),
    Source("新华网", "https://www.news.cn/politics/", "page"),
    Source("人民网", "https://www.people.com.cn/rss/politics.xml"),
)
SOCIETY_SOURCES = (
    Source("中国新闻网", "https://www.chinanews.com.cn/rss/society.xml"),
    Source("新华网", "https://www.news.cn/society/", "page"),
    Source("人民网", "https://www.people.com.cn/rss/society.xml"),
)
TECH_SOURCES = (
    Source("IT之家", "https://www.ithome.com/rss/"),
    Source("超能网", "https://www.expreview.com/rss.php"),
    Source("爱范儿", "https://www.ifanr.com/feed"),
)
# Keep every official source before any Google News fallback. The latter retains
# Google's aggregate article URL and checks the publisher's domain on each item.
GAMES_SOURCES = (
    Source("游民星空", "https://www.gamersky.com/news/", "page"),
    Source("3DM", "https://www.3dmgame.com/news/", "page"),
    Source("机核GCORES", "https://www.gcores.com/rss"),
    Source("游研社", "https://www.yystv.cn/rss/feed"),
    Source("触乐", "https://www.chuapp.com/category/new", "page"),
    Source("17173", "https://news.17173.com/", "page"),
    *(
        Source(
            name,
            "https://news.google.com/rss/search?"
            + urlencode(
                {
                    "q": f"site:{domain}{path} when:2d",
                    "hl": "zh-CN",
                    "gl": "CN",
                    "ceid": "CN:zh-Hans",
                }
            ),
            "google",
            domain,
        )
        for name, domain, path in (
            ("游民星空", "gamersky.com", "/news"),
            ("3DM", "3dmgame.com", "/news"),
            ("机核GCORES", "gcores.com", "/articles"),
            ("游研社", "yystv.cn", ""),
            ("触乐", "chuapp.com", "/article/"),
            ("17173", "17173.com", ""),
        )
    ),
)
SECTION_SOURCES = {
    "politics": POLITICS_SOURCES,
    "society": SOCIETY_SOURCES,
    "games": GAMES_SOURCES,
    "tech": TECH_SOURCES,
}

_AD_OR_GUIDE = re.compile(
    r"攻略|教程|全收集|全任务|全结局|达成条件|获取方法|怎么(?:玩|下载|获得|通过|预购)|"
    r"玩法介绍|图文详解|通关|配装|武器推荐|最强\d+|图鉴|谜题解法|机制说明|评测|解决方法|"
    r"囧图|图赏|美女|写真|福利图|抽奖|礼包码|兑换码|优惠券|到手价|限时抢|"
    r"下载安装|官网下载|官方版下载|最新版下载|下载游玩|首页|当前标签|单机游戏门户"
)
_POLITICS_EXCLUDE = re.compile(r"游戏(?:\s*IP|产业|行业|生态)|电子竞技|电竞产业", re.I)
_GAME_TOPIC = re.compile(
    r"《[^》]+》|游戏|玩家|开发商|工作室|电竞|主机|任天堂|暴雪|育碧|"
    r"Steam|PlayStation|Xbox|Switch|PS\s*(?:[3456]|Plus|\+|中国之星)|"
    r"Epic|Nintendo|RPG|Skyblivion|DLSS|GTA\s*\d",
    re.I,
)
_GAME_EVENT = re.compile(
    r"发布|发售|上线|官宣|宣布|公布|确认|推出|定档|延期|跳票|销量|售出|"
    r"开发|总监|制作人|访谈|采访|更新|新作|预告|实机|演示|重制|复刻|"
    r"分级|会免|免费领|首发|新史低|DLC|资料片|营收|收入|财报|预算|"
    r"裁员|解散|关闭|停服|收购|合作|赛事|晋级|夺冠|冠军|在线|玩家数|"
    r"卖出|里程碑|发布会|试玩|联动",
    re.I,
)
_TECH_TOPIC = re.compile(
    r"手机|智能机|平板|笔记本|迷你主机|台式机|工作站|服务器|处理器|芯片|半导体|"
    r"显卡|显存|内存|硬盘|主板|显示器|面板|屏幕|相机|镜头|耳机|音箱|扬声器|"
    r"音响|充电器|充电宝|电源|手表|手环|路由器|交换机|扩展坞|雷电|键盘|鼠标|"
    r"机箱|风扇|散热|电视|投影|智能眼镜|传感器|电路板|电容器|存储|掌机|"
    r"CPU|GPU|SSD|OLED|Mini\s*LED|USB\s*[34]|iPhone|iPad|MacBook|"
    r"Radeon|GeForce|Ryzen|Galaxy|Pura|NAS\b",
    re.I,
)
_TECH_EXCLUDE = re.compile(
    r"《[^》]+》|游戏(?!本|机)|网游|手游|票房|电影|综艺|电视剧|短剧|"
    r"大模型|搜索引擎|AI\s*应用|金融|博主|黄牛|囤货|电商大促|双\s*11|"
    r"红包|返现|满减|补贴后|严选好货|好价|今日优惠|优惠汇总|11[.·]11|"
    r"车身|内饰|新车|汽车销量|Robotaxi",
    re.I,
)
_BAD_IMAGE = re.compile(
    r"logo|qrcode|qr[-_]?code|erweima|ewm[._/-]|placeholder|loading|"
    r"default[._/-]|avatar|favicon|sprite|banner|advert|icon[._/-]|"
    r"二维码|公众号|扫一扫|广告|占位|头像|标志|商标",
    re.I,
)
_DATE_RE = re.compile(
    r"(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})日?"
    r"(?:[T\s]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)


@dataclass
class _Article:
    item: NewsItem
    images: list[str] = field(default_factory=list)


def _published(value: str) -> str:
    """Normalize a publisher's date without substituting the collection time.

    Args:
        value: RFC 822, ISO 8601, or a Chinese numeric publication date.

    Returns:
        A full ISO timestamp in UTC+08:00, or an empty string if unparseable.
    """
    value = value.strip()
    if not value:
        return ""
    try:
        dt = parsedate_to_datetime(value)
    except (ValueError, TypeError, OverflowError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            match = _DATE_RE.search(value)
            if not match:
                return ""
            try:
                year, month, day, hour, minute, second = (
                    int(part or 0) for part in match.groups()
                )
                dt = datetime(year, month, day, hour, minute, second)
            except ValueError:
                return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_CST)
    return dt.astimezone(_CST).isoformat(timespec="seconds")


def _story_key(title: str, section: str) -> str:
    """Identify duplicate headlines and repeated game sales milestones.

    Args:
        title: Publisher headline.
        section: Report section key.

    Returns:
        A normalized title or an exact named-game sales milestone key.
    """
    game = re.search(r"《([^》]+)》", title)
    milestone = re.search(r"(\d+(?:\.\d+)?)\s*(亿|万)(?:份|套)?", title)
    if (
        section == "games"
        and game
        and milestone
        and re.search(r"销量|售出|卖出", title)
    ):
        name = re.sub(r"\s+", "", game.group(1)).casefold()
        return f"sales:{name}:{milestone.group(1)}{milestone.group(2)}"
    return re.sub(r"[\W_]+", "", title).casefold()


def _images(markup: str, base_url: str, *, article_body: bool = False) -> list[str]:
    """Extract editorial images while excluding page furniture and promotions.

    Args:
        markup: An RSS content fragment or an article document.
        base_url: The originating article URL for relative image paths.
        article_body: Whether markup is a whole page requiring a body selector.

    Returns:
        At most four distinct image URLs found in editorial content.
    """
    soup = BeautifulSoup(markup, "html.parser")
    if article_body:
        body = soup.select_one(
            ".left_zw, #Content, #article-content, .article-content, .article_content, "
            ".content_detail, .content-detail, .entry-content, .mid2L_con, "
            ".news_warp_center, .content-desc, .content_desc, #chan_newsDetail, "
            "#detail, article"
        )
        if body is None:
            return []
    else:
        body = soup
    result: list[str] = []
    for node in body.select("img"):
        raw = node.get("data-original") or node.get("data-src") or node.get("src")
        if not isinstance(raw, str):
            continue
        context = " ".join(
            str(node.get(attr, "")) for attr in ("alt", "title", "class", "id")
        )
        url = urljoin(base_url, html.unescape(raw).strip())
        if _BAD_IMAGE.search(url + " " + context):
            continue
        if urlsplit(url).scheme not in {"https", "http"}:
            continue
        if url not in result:
            result.append(url)
        if len(result) == 4:
            break
    return result


async def _request(
    session: aiohttp.ClientSession,
    url: str,
    proxy: str | None,
    timeout: float,
    *,
    image: bool = False,
    referer: str | None = None,
) -> tuple[bytes, str]:
    """Fetch a bounded public HTTP resource, validating every redirect target.

    Args:
        session: Shared HTTP session.
        url: Resource URL from an editorial source.
        proxy: Optional caller-controlled proxy.
        timeout: Total wall-clock budget, including redirects and DNS.
        image: Whether to apply the image byte limit.
        referer: Originating article URL for image hotlink protection.

    Returns:
        Response bytes and the final resource URL.

    Raises:
        ValueError: A URL is private, malformed, or too large.
        aiohttp.ClientError: The server rejects the request.
        asyncio.TimeoutError: The complete operation exceeds its budget.
    """

    headers = dict(session.headers)
    if referer:
        headers["Referer"] = referer
    try:
        return await public_request(
            url,
            proxy=proxy,
            timeout=timeout,
            max_bytes=_MAX_IMAGE if image else _MAX_DOCUMENT,
            headers=headers,
            trust_env=session.trust_env,
        )
    except RuntimeError as exc:
        # Editorial fallbacks already classify HTTP failures as ClientError.
        raise aiohttp.ClientError(str(exc)) from exc


def _parse_feed(document: bytes, source: Source, section: str) -> list[_Article]:
    """Read RSS/Atom entries and apply section-specific editorial filters.

    Args:
        document: The freshly downloaded feed.
        source: Source attribution and publisher restrictions.
        section: Report section key.

    Returns:
        At most twelve dated, deduplicated articles in feed order.
    """
    root = ET.fromstring(document)
    entries = root.findall("./channel/item")
    if not entries:
        entries = root.findall("{http://www.w3.org/2005/Atom}entry")
    result: list[_Article] = []
    seen: set[str] = set()
    for entry in entries[:200]:
        fields = {child.tag.rsplit("}", 1)[-1]: child for child in entry}
        title = (
            html.unescape("".join(fields["title"].itertext())).strip()
            if "title" in fields
            else ""
        )
        link = fields.get("link")
        url = (link.text or link.get("href", "")) if link is not None else ""
        if not url.strip():
            continue
        url = urljoin(source.url, url.strip())
        if url == source.url or urlsplit(url).path in {"", "/"}:
            continue
        if source.kind == "google":
            publisher = fields.get("source")
            if publisher is None:
                continue
            host = (urlsplit(publisher.get("url", "")).hostname or "").lower()
            if host != source.publisher_domain and not host.endswith(
                "." + source.publisher_domain
            ):
                continue
            name = (publisher.text or source.name).strip()
            title = title.removesuffix(" - " + name).strip()
            if urlsplit(url).hostname != "news.google.com":
                continue
        date_text = next(
            (
                (fields[key].text or "")
                for key in ("pubDate", "published", "date", "updated")
                if key in fields
            ),
            "",
        )
        published = _published(date_text)
        key = _story_key(title, section)
        if (
            len(title) < 6
            or not published
            or _AD_OR_GUIDE.search(title)
            or not urlsplit(url).hostname
            or urlsplit(url).scheme not in {"http", "https"}
            or key in seen
            or url in seen
        ):
            continue
        if section == "politics" and _POLITICS_EXCLUDE.search(title):
            continue
        if section == "games" and not (
            _GAME_TOPIC.search(title) and _GAME_EVENT.search(title)
        ):
            continue
        if section == "tech" and (
            not _TECH_TOPIC.search(title) or _TECH_EXCLUDE.search(title)
        ):
            continue
        seen.update((key, url))
        item = NewsItem(
            title=title,
            source=source.name,
            url=url,
            published=published,
            image_path=None,
            image_source=None,
        )
        markup = "\n".join(
            "".join(fields[key].itertext())
            for key in ("encoded", "content", "description", "summary")
            if key in fields
        )
        images = _images(markup, url) if markup else []
        for child in entry.iter():
            if child.tag.rsplit("}", 1)[-1] in {"enclosure", "thumbnail", "content"}:
                image_url = child.get("url", "")
                if image_url and (
                    child.get("type", "").startswith("image/")
                    or child.get("medium") == "image"
                    or child.tag.rsplit("}", 1)[-1] == "thumbnail"
                ):
                    image_url = urljoin(url, image_url)
                    if not _BAD_IMAGE.search(image_url) and image_url not in images:
                        images.append(image_url)
        result.append(_Article(item, images[:4]))
        if len(result) == _MAX_ITEMS:
            break
    return result


async def _parse_page(
    session: aiohttp.ClientSession,
    document: bytes,
    source: Source,
    section: str,
    proxy: str | None,
) -> list[_Article]:
    """Parse official news lists, resolving missing dates from article metadata.

    Args:
        session: Shared HTTP session.
        document: Freshly downloaded news index.
        source: Official editorial page.
        section: Report section key.
        proxy: Optional caller-controlled proxy.

    Returns:
        At most twelve attributed articles; undated entries are excluded.
    """
    encoding = (
        "gb18030"
        if re.search(rb"charset\s*=\s*[\"']?gb", document[:3000], re.I)
        else "utf-8"
    )
    soup = BeautifulSoup(document.decode(encoding, errors="replace"), "html.parser")
    if source.kind == "cn_rolling":
        anchors = soup.select(".content_list .dd_bt a[href]")
    else:
        anchors = soup.select("a[href]")
    candidates: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for anchor in anchors:
        url = urljoin(source.url, str(anchor.get("href", "")))
        title = anchor.get_text(" ", strip=True)
        path = urlsplit(url).path
        if source.kind == "cn_rolling":
            valid = bool(re.fullmatch(r"/(gn|gj)/20\d{2}/\d{2}-\d{2}/\d+\.shtml", path))
        elif source.name == "17173":
            valid = bool(re.fullmatch(r"/content/\d{8}/\d+\.shtml", path))
        elif section == "games":
            valid = bool(
                re.search(
                    r"/(?:news/(?:20\d{2}|\d)|articles/\d|article/\d)|/20\d{2}\d{4}/",
                    path,
                )
            )
        else:
            valid = bool(
                re.search(r"/20\d{2}(?:/\d{2}/\d{2}|\d{4})/[^/]+(?:/c)?\.html?$", path)
            )
        host = urlsplit(url).hostname or ""
        source_host = urlsplit(source.url).hostname or ""
        domain = source_host.removeprefix("www.")
        if not valid or (host != domain and not host.endswith("." + domain)):
            continue
        key = _story_key(title, section)
        if len(title) < 6 or key in seen or url in seen or _AD_OR_GUIDE.search(title):
            continue
        if section == "politics" and _POLITICS_EXCLUDE.search(title):
            continue
        if section == "games" and not (
            _GAME_TOPIC.search(title) and _GAME_EVENT.search(title)
        ):
            continue
        published = ""
        if source.kind == "cn_rolling":
            row = anchor.find_parent("li")
            stamp = row.select_one(".dd_time") if row else None
            date_match = re.search(r"/(20\d{2})/(\d{2})-(\d{2})/", path)
            time_match = re.search(
                r"\b(\d{1,2}:\d{2})(?::\d{2})?\b", stamp.get_text() if stamp else ""
            )
            if date_match and time_match:
                published = _published(
                    "-".join(date_match.groups()) + " " + time_match.group(0)
                )
        seen.update((key, url))
        candidates.append((title, url, published))
        if len(candidates) == 16:
            break

    semaphore = asyncio.Semaphore(3)

    async def complete(title: str, url: str, published: str) -> _Article | None:
        images: list[str] = []
        if not published:
            try:
                async with semaphore:
                    data, _ = await _request(session, url, proxy, _MEDIA_TIMEOUT)
                page = BeautifulSoup(data, "html.parser")
                date_node = page.select_one(
                    'meta[property="article:published_time"], meta[name="publishdate"], '
                    'meta[name="pubdate"], meta[name="PubDate"], meta[name="date"], '
                    'meta[name="publish_time"], time[datetime]'
                )
                if date_node:
                    published = _published(
                        str(date_node.get("content") or date_node.get("datetime") or "")
                    )
                if not published:
                    date_node = page.select_one(
                        ".time, .date, .pubtime, .publish-time, .source, .info, .news-time"
                    )
                    if date_node:
                        published = _published(date_node.get_text(" ", strip=True))
                images = _images(str(page), url, article_body=True)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError):
                return None
        if not published:
            return None
        return _Article(
            NewsItem(
                title=title,
                source=source.name,
                url=url,
                published=published,
                image_path=None,
                image_source=None,
            ),
            images,
        )

    return [
        entry
        for entry in await asyncio.gather(*(complete(*row) for row in candidates))
        if entry
    ][:_MAX_ITEMS]


def _verified_photo(data: bytes) -> Image.Image | None:
    """Validate and decode a substantial editorial photo, rejecting flat graphics.

    Args:
        data: Bounded image bytes from the article or an existing local cache.

    Returns:
        An RGB image suitable for local rendering, or None for unsuitable media.
    """
    try:
        with Image.open(BytesIO(data)) as check:
            width, height = check.size
            if (
                width < 320
                or height < 180
                or width * height > 24_000_000
                or not 0.6 <= width / height <= 3.2
            ):
                return None
            check.verify()
        with Image.open(BytesIO(data)) as decoded:
            photo = decoded.convert("RGB")
        sample = photo.resize((96, 96))
        if max(ImageStat.Stat(sample).stddev) < 12:
            return None
        colors = sample.getcolors(96 * 96) or []
        if len(colors) < 128:
            return None
        # QR codes and monochrome placeholders have mostly near-black/white pixels.
        extremes = sum(
            count
            for count, rgb in colors
            if isinstance(rgb, tuple) and (max(rgb) < 35 or min(rgb) > 225)
        )
        if extremes / (96 * 96) > 0.88:
            return None
        return photo
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None


async def collect_sections(
    proxy: str | None, image_dir: Path
) -> dict[str, list[NewsItem]]:
    """Collect live sections and cache up to two verified related photos in each.

    Each section tries sources sequentially, stopping at the first usable result.
    Sections run concurrently. Entries retain publisher order and real dates;
    fewer than twelve items are returned honestly rather than filled or invented.

    Args:
        proxy: Optional HTTP proxy from the caller's existing configuration.
        image_dir: Caller-owned cache directory; no other location is written.

    Returns:
        Politics, society, games, and tech lists, each containing zero to twelve
        articles. Image paths are absolute; absent/unverified media remain None.
    """
    async with aiohttp.ClientSession(
        headers={"User-Agent": _UA},
        trust_env=True,
        connector=aiohttp.TCPConnector(limit=10, limit_per_host=4),
    ) as session:
        # Shared within this collection only, so game photo fallback does not
        # refetch IT Home and cannot accidentally reuse frozen report content.
        feed_tasks: dict[str, asyncio.Task[tuple[bytes, str]]] = {}

        async def feed(source: Source) -> bytes:
            task = feed_tasks.get(source.url)
            if task is None:
                task = asyncio.create_task(
                    _request(session, source.url, proxy, _FEED_TIMEOUT)
                )
                feed_tasks[source.url] = task
            return (await task)[0]

        async def section_news(section: str) -> list[_Article]:
            for source in SECTION_SOURCES[section]:
                try:
                    document = await feed(source)
                    if source.kind in {"rss", "google"}:
                        articles = _parse_feed(document, source, section)
                    else:
                        articles = await _parse_page(
                            session, document, source, section, proxy
                        )
                except (
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                    ET.ParseError,
                    ValueError,
                    OSError,
                ) as exc:
                    logger.warning(
                        f"[juhkff] Report {section} source {source.name} failed "
                        f"({type(exc).__name__})"
                    )
                    continue
                if articles:
                    logger.info(
                        f"[juhkff] Report {section} selected {source.name}: "
                        f"{len(articles)} items"
                    )
                    return articles
                logger.info(
                    f"[juhkff] Report {section} source {source.name} has no usable items"
                )
            return []

        keys = tuple(SECTION_SOURCES)
        collected = dict(
            zip(keys, await asyncio.gather(*(section_news(key) for key in keys)))
        )
        try:
            image_dir = image_dir.resolve()
            image_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                f"[juhkff] Report image directory unavailable ({type(exc).__name__})"
            )
            return {
                key: [entry.item for entry in entries]
                for key, entries in collected.items()
            }

        media_slots = asyncio.Semaphore(4)

        async def section_photos(section: str, entries: list[_Article]) -> None:
            count = 0
            used: set[str] = set()
            for entry in entries:
                if count == 2:
                    break
                item = entry.item
                image_urls = entry.images
                attribution = item["source"] + "（原文配图）"
                article_url = item["url"]
                # Google aggregate links are retained as URLs, never treated as
                # an article body or as evidence that a generic image is related.
                if (
                    not image_urls
                    and urlsplit(item["url"]).hostname != "news.google.com"
                ):
                    try:
                        async with media_slots:
                            data, _ = await _request(
                                session, item["url"], proxy, _MEDIA_TIMEOUT
                            )
                        image_urls = _images(
                            data.decode("utf-8", errors="replace"),
                            item["url"],
                            article_body=True,
                        )
                    except (
                        aiohttp.ClientError,
                        asyncio.TimeoutError,
                        ValueError,
                        OSError,
                    ) as exc:
                        logger.debug(
                            f"[juhkff] Report {section} article photos unavailable "
                            f"from {item['source']} ({type(exc).__name__})"
                        )
                if not image_urls and section == "games":
                    try:
                        document = await feed(TECH_SOURCES[0])
                        # Require an exact named game in both headlines. Keep
                        # provenance explicit: these are same-game photos, not
                        # purported photos fetched from the original publisher.
                        games = re.findall(r"《([^》]{2,50})》", item["title"])
                        for related in _parse_feed(document, TECH_SOURCES[0], "games"):
                            related_games = re.findall(
                                r"《([^》]{2,50})》", related.item["title"]
                            )
                            if set(games) & set(related_games) and related.images:
                                image_urls = related.images
                                attribution = "IT之家（同游戏报道配图）"
                                article_url = related.item["url"]
                                break
                    except (
                        aiohttp.ClientError,
                        asyncio.TimeoutError,
                        ET.ParseError,
                        ValueError,
                        OSError,
                    ):
                        pass
                for image_url in image_urls[:3]:
                    if image_url in used or _BAD_IMAGE.search(image_url):
                        continue
                    digest = hashlib.sha256(image_url.encode()).hexdigest()[:20]
                    path = image_dir / f"{section}-{digest}.jpg"
                    try:
                        photo = (
                            _verified_photo(path.read_bytes())
                            if path.is_file()
                            else None
                        )
                        if photo is None:
                            async with media_slots:
                                data, _ = await _request(
                                    session,
                                    image_url,
                                    proxy,
                                    _MEDIA_TIMEOUT,
                                    image=True,
                                    referer=article_url,
                                )
                            photo = _verified_photo(data)
                            if photo is None:
                                continue
                            photo.thumbnail((1600, 1200))
                            photo.save(path, format="JPEG", quality=88)
                        item["image_path"] = str(path)
                        item["image_source"] = attribution
                        item["image_url"] = image_url
                        item["image_article_url"] = article_url
                        used.add(image_url)
                        count += 1
                        break
                    except (
                        aiohttp.ClientError,
                        asyncio.TimeoutError,
                        ValueError,
                        OSError,
                    ):
                        continue
            logger.info(f"[juhkff] Report {section} cached {count} related photos")

        photo_results = await asyncio.gather(
            *(
                asyncio.wait_for(
                    section_photos(key, entries), timeout=_PHOTO_SECTION_TIMEOUT
                )
                for key, entries in collected.items()
            ),
            return_exceptions=True,
        )
        for key, outcome in zip(keys, photo_results):
            if isinstance(outcome, Exception):
                logger.warning(
                    f"[juhkff] Report {key} photo collection stopped "
                    f"({type(outcome).__name__})"
                )
        return {
            key: [entry.item for entry in entries] for key, entries in collected.items()
        }
