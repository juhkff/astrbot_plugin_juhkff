"""Render the five approved daily report layouts with local, collected content."""

from __future__ import annotations

import base64
import html
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple, TypedDict
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from lunar_python import Lunar, Solar
from PIL import Image

REPORT_TITLE = "今日日报"
ReportSections = Mapping[str, Sequence[Mapping[str, Any]]]
_RESOURCE_DIR = Path(__file__).resolve().parent.parent / "resources" / "daily_report"
_NAMES = {
    "politics": ("时政", "WORLD & CHINA"),
    "society": ("国内社会", "CHINA SOCIETY"),
    "games": ("游戏", "GAME DISPATCH"),
    "tech": ("数码", "TECH & DEVICES"),
}
_EMPTY_SECTION = '<p class="empty">暂无资讯</p>'
_IMAGE_MIME = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}


class Template(NamedTuple):
    """Describe an immutable daily report template.

    Attributes:
        id: Stable configuration value used to select the layout.
        label: Chinese name displayed in the settings interface.
        width: Required browser viewport width in CSS pixels.
    """

    id: str
    label: str
    width: int


TEMPLATES: tuple[Template, ...] = (
    Template("crosspoint", "红黑索引", 960),
    Template("nightwire", "深色电讯", 960),
    Template("dayletter", "纸张手帐", 960),
    Template("dailydrop", "撞色网格", 960),
    Template("greenfolio", "青绿四栏", 1440),
)


class _Holiday(TypedDict):
    """Describe a dated calendar reminder.

    Attributes:
        name: Chinese reminder label.
        target: ISO date of the reminder.
        short: Compact month and day for display.
        days: Number of days until the reminder.
        en: English reminder label.
    """

    name: str
    target: str
    short: str
    days: int
    en: str


class _Calendar(TypedDict):
    """Describe the date fields shared by the approved layouts.

    Attributes:
        day: Report date in the caller's local calendar.
        iso: ISO report date.
        year: Gregorian year.
        month: Two-digit Gregorian month.
        date: Two-digit Gregorian day.
        week: Chinese weekday name.
        week_en: English weekday name.
        lunar: Chinese lunar month and day.
        holidays: Weekend, solar term, New Year, and Spring Festival reminders.
    """

    day: date
    iso: str
    year: int
    month: str
    date: str
    week: str
    week_en: str
    lunar: str
    holidays: list[_Holiday]


def get_template(style_id: str) -> Template:
    """Return the metadata for a stable template identifier.

    Args:
        style_id: One of the identifiers published in TEMPLATES.

    Returns:
        The immutable metadata for the requested layout.

    Raises:
        ValueError: If the identifier does not name a registered template.
    """
    for template in TEMPLATES:
        if template.id == style_id:
            return template
    raise ValueError(f"Unknown daily report template: {style_id!r}")


def esc(value: object) -> str:
    """Escape an untrusted value for HTML text or a quoted attribute.

    Args:
        value: Text or another printable value.

    Returns:
        HTML-escaped text with both quote characters escaped.
    """
    return html.escape(str(value), quote=True)


def calendar_for(day: date) -> _Calendar:
    """Calculate the lunar date and upcoming reminders for a report date.

    Args:
        day: Gregorian date selected by the caller.

    Returns:
        Display fields and dynamically calculated calendar reminders.
    """
    lunar = Solar.fromYmd(day.year, day.month, day.day).getLunar()
    saturday = (
        day + timedelta(days=(5 - day.weekday()) % 7) if day.weekday() < 5 else day
    )
    newyear = date(day.year if day <= date(day.year, 1, 1) else day.year + 1, 1, 1)
    cny_solar = Lunar.fromYmd(day.year, 1, 1).getSolar()
    cny = date(cny_solar.getYear(), cny_solar.getMonth(), cny_solar.getDay())
    if cny < day:
        cny_solar = Lunar.fromYmd(day.year + 1, 1, 1).getSolar()
        cny = date(cny_solar.getYear(), cny_solar.getMonth(), cny_solar.getDay())
    term = lunar.getNextJieQi()
    if term is None:
        raise ValueError("No upcoming solar term was returned for the report date")
    term_name = term.getName()
    if not term_name:
        raise ValueError("The upcoming solar term has no name")
    solar = term.getSolar()
    term_date = date(solar.getYear(), solar.getMonth(), solar.getDay())
    holidays = [
        ("周末", saturday, "WEEKEND"),
        (term_name, term_date, "SOLAR TERM"),
        ("元旦", newyear, "NEW YEAR"),
        ("春节", cny, "SPRING FESTIVAL"),
    ]
    return {
        "day": day,
        "iso": day.isoformat(),
        "year": day.year,
        "month": f"{day.month:02}",
        "date": f"{day.day:02}",
        "week": "星期" + "一二三四五六日"[day.weekday()],
        "week_en": (
            "MONDAY",
            "TUESDAY",
            "WEDNESDAY",
            "THURSDAY",
            "FRIDAY",
            "SATURDAY",
            "SUNDAY",
        )[day.weekday()],
        "lunar": f"{lunar.getMonthInChinese()}月{lunar.getDayInChinese()}",
        "holidays": [
            {
                "name": name,
                "target": target.isoformat(),
                "short": target.strftime("%m.%d"),
                "days": (target - day).days,
                "en": en,
            }
            for name, target, en in holidays
        ],
    }


def picture(item: Mapping[str, Any], cls: str = "") -> str:
    """Inline a local article image without fetching any remote resource.

    Args:
        item: Collected article with an optional local image_path.
        cls: Additional image classes used by the approved layout.

    Returns:
        An image element, or an empty string for an unavailable image.
        Portrait images receive the shared contain rule to avoid cropping.
    """
    raw_path = item.get("image_path")
    if not raw_path:
        return ""
    try:
        path = Path(raw_path).expanduser()
        with Image.open(path) as photo:
            mime = _IMAGE_MIME.get(photo.format or "")
            if not mime:
                return ""
            if photo.height > photo.width:
                cls += " document-photo"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    except (OSError, ValueError, Image.DecompressionBombError):
        return ""
    return (
        f'<img class="news-photo {esc(cls)}" src="data:{mime};base64,{encoded}" '
        f'alt="{esc(item.get("title", ""))}">'
    )


def feature_index(arr: Sequence[Mapping[str, Any]]) -> int:
    """Select the first article with a usable image in the original news order.

    Args:
        arr: Articles in their original collection order.

    Returns:
        The index of the first usable image, regardless of orientation,
        or zero when no image is available. Callers handle empty sections.
    """
    for index, item in enumerate(arr):
        raw_path = item.get("image_path")
        if not raw_path:
            continue
        try:
            with Image.open(Path(raw_path).expanduser()) as photo:
                if photo.format not in _IMAGE_MIME:
                    continue
                return index
        except (OSError, ValueError, Image.DecompressionBombError):
            continue
    return 0


def meta(item: Mapping[str, Any], *, photo: bool = False) -> str:
    """Render escaped source, publication time and optional image attribution.

    Args:
        item: Collected article with source and optional ISO published time.
        photo: Whether the article displays a photo needing its own credit.

    Returns:
        Source and time markup, using Shanghai time for aware timestamps.
    """
    stamp = str(item.get("published") or "")
    if stamp:
        try:
            published = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if published.tzinfo:
                published = published.astimezone(ZoneInfo("Asia/Shanghai"))
            stamp = published.strftime("%m.%d %H:%M")
        except ValueError:
            stamp = stamp[:16].replace("T", " ")
    credit = item.get("image_source") or item.get("source", "")
    return (
        f'<span class="source">{esc(item.get("source", ""))}</span>'
        + (f"<time>{esc(stamp)}</time>" if stamp else "")
        + (f'<span class="photo-source">图 / {esc(credit)}</span>' if photo else "")
    )


def title(item: Mapping[str, Any]) -> str:
    """Render an escaped headline and an optional safe original-story link.

    Args:
        item: Collected article containing a title and optional source URL.

    Returns:
        A headline linked only to an HTTP or HTTPS URL with a hostname.
    """
    url = str(item.get("url") or "")
    text = esc(item.get("title", ""))
    try:
        parsed = urlsplit(url)
        linked = (
            parsed.scheme in ("http", "https")
            and bool(parsed.hostname)
            and not any(ord(character) < 32 for character in url)
        )
    except ValueError:
        linked = False
    return (
        f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{text}</a>'
        if linked
        else text
    )


def article(
    item: Mapping[str, Any], key: str, idx: int, cls: str = "", photo: bool = False
) -> str:
    """Render one numbered story in the shared approved article structure.

    Args:
        item: Collected article to display.
        key: Fixed section identifier.
        idx: Zero-based position within the collected section.
        cls: Additional layout classes.
        photo: Whether this story should display its local image.

    Returns:
        Escaped article markup with its original index and source credit.
    """
    image = picture(item) if photo else ""
    return (
        f'<article class="news {esc(cls)}" data-news="{esc(key)}" '
        f'data-index="{idx + 1}"><span class="index">{idx + 1:02}</span>'
        f'<div class="story">{image}'
        f'<h3>{title(item)}</h3><div class="meta">{meta(item, photo=bool(image))}</div></div></article>'
    )


def holiday_strip(cal: _Calendar, cls: str = "") -> str:
    """Render the four dynamically calculated calendar reminders.

    Args:
        cal: Calendar fields produced by calendar_for.
        cls: Additional layout classes for the reminder strip.

    Returns:
        Calendar reminder markup shared by the five layouts.
    """
    return (
        f'<div class="holidays {esc(cls)}">'
        + "".join(
            f'<div class="holiday"><div class="holiday-name">{esc(holiday["name"])}'
            f'<small>{esc(holiday["short"])}</small></div><div class="holiday-days">'
            f"{holiday['days']:02}<span>"
            f"{'就是今天' if holiday['days'] == 0 else '天后'}</span></div></div>"
            for holiday in cal["holidays"]
        )
        + "</div>"
    )


def source_line(sections: ReportSections) -> str:
    """List the distinct source credits in their first appearance order.

    Args:
        sections: Ordered article lists used in the report.

    Returns:
        Plain text source names, to be escaped when inserted into HTML.
    """
    names = dict.fromkeys(
        str(item.get("source") or "") for arr in sections.values() for item in arr
    )
    return " / ".join(name for name in names if name)


def footer(sections: ReportSections) -> str:
    """Render the production footer with the actual count and source credits.

    Args:
        sections: Ordered article lists used in the report.

    Returns:
        A production footer without design-preview wording.
    """
    return (
        f"<footer><span>{REPORT_TITLE} · {sum(map(len, sections.values()))} 则资讯</span>"
        f"<span>{esc(source_line(sections))}</span></footer>"
    )


def _build_crosspoint(sections: ReportSections, calendar: _Calendar) -> str:
    """Render the approved crosspoint body without changing its layout.

    Args:
        sections: Four ordered lists of collected articles.
        calendar: Dynamic date and reminder fields for this report.

    Returns:
        The complete body of the selected layout.
    """
    total = sum(map(len, sections.values()))
    top = f"""<header><div class="topline"><b>DAILY BRIEF / 每日资讯</b><span>DAILY NEWS INDEX · {calendar["year"]}</span></div><div class="masthead"><div><div class="kicker">时政 / 国内社会 / 游戏 / 数码</div><h1>{REPORT_TITLE[:2]}<span>{REPORT_TITLE[2:]}</span></h1><p>把世界的新变化，读成一页。</p></div><div class="dateblock"><div>{calendar["year"]} / {calendar["month"]}</div><strong>{calendar["date"]}</strong><p>{calendar["week"]} · 农历{calendar["lunar"]}</p></div></div><div class="edition"><span>NEWS / {total:02} 则</span><span>{calendar["iso"]} · {calendar["week_en"]}</span></div></header>"""
    body = top + holiday_strip(calendar)
    for num, (key, arr) in enumerate(sections.items(), 1):
        name, en = _NAMES[key]
        head = f'<div class="sectionhead"><span class="chapter">{num:02}</span><h2>{name}</h2><span>{en}</span><b>{len(arr):02} 则</b></div>'
        if not arr:
            body += f'<section class="section s-{key}">{head}{_EMPTY_SECTION}</section>'
            continue
        featured = feature_index(arr)
        item = arr[featured]
        photo = picture(item)
        lead = (
            f'<article class="news lead" data-news="{key}" data-index="{featured + 1}"><div class="leadpic">{photo}</div><div class="leadcopy"><span class="index">{featured + 1:02} / FOCUS</span><h3>{title(item)}</h3><div class="meta">{meta(item, photo=True)}</div></div></article>'
            if photo
            else article(item, key, featured, "lead textlead")
        )
        rest = [article(x, key, i) for i, x in enumerate(arr) if i != featured]
        cut = (len(rest) + 1) // 2
        body += f"""<section class="section s-{key}">{head}{lead}<div class="columns"><div>{"".join(rest[:cut])}</div><div>{"".join(rest[cut:])}</div></div></section>"""
    body += footer(sections)
    return body


def _build_nightwire(sections: ReportSections, calendar: _Calendar) -> str:
    """Render the approved nightwire body without changing its layout.

    Args:
        sections: Four ordered lists of collected articles.
        calendar: Dynamic date and reminder fields for this report.

    Returns:
        The complete body of the selected layout.
    """
    total = sum(map(len, sections.values()))
    body = f"""<header><div class="skyline"><span><i></i> DAILY BRIEF / 每日资讯</span><span>DAILY EDITION · {total:02} STORIES</span></div><div class="night-title"><div><h1>{REPORT_TITLE[:2]}<span>{REPORT_TITLE[2:]}</span></h1><p>收集今天，正在发生的事。</p></div><div class="nightdate"><b>{calendar["month"]}<em>/</em>{calendar["date"]}</b><div>{calendar["year"]} · {calendar["week"]} · 农历{calendar["lunar"]}</div></div></div><div class="horizon"><span>下一个时间坐标</span>{holiday_strip(calendar)}</div></header><main class="wire">"""
    for num, (key, arr) in enumerate(sections.items(), 1):
        name, en = _NAMES[key]
        body += f'<section class="channel"><div class="channelhead"><div class="node">{num:02}</div><div><span class="kicker">{en}</span><h2>{name}频道</h2></div><div class="channelcount">{len(arr):02} 条信号</div></div>'
        if not arr:
            body += _EMPTY_SECTION + "</section>"
            continue
        chosen = [i for i, x in enumerate(arr) if x.get("image_path")][:2]
        if not chosen:
            chosen = list(range(min(2, len(arr))))
        body += (
            '<div class="signals">'
            + "".join(article(arr[i], key, i, "signal", True) for i in chosen)
            + "</div>"
        )
        body += (
            '<div class="wirelist">'
            + "".join(
                (article(x, key, i) for i, x in enumerate(arr) if i not in chosen)
            )
            + "</div></section>"
        )
    body += "</main>" + footer(sections)
    return body


def _build_dayletter(sections: ReportSections, calendar: _Calendar) -> str:
    """Render the approved dayletter body without changing its layout.

    Args:
        sections: Four ordered lists of collected articles.
        calendar: Dynamic date and reminder fields for this report.

    Returns:
        The complete body of the selected layout.
    """
    body = f"""<div class="letter"><header><div class="letterlabel">DAILY BRIEF <span>每日资讯</span></div><div class="lettertitle"><div><span class="greeting">今日份的世界，已寄到。</span><h1 class="serif">{REPORT_TITLE[:2]}<span>{REPORT_TITLE[2:]}</span></h1><p>时政见闻 · 社会热点 · 游戏新事 · 数码动态</p></div><div class="stamp"><span>{calendar["year"]} / {calendar["month"]}</span><strong>{calendar["date"]}</strong><b>{calendar["week"]}</b><small>农历{calendar["lunar"]}</small></div></div><div class="lettercalendar"><span class="calendar-note">日子里的小期待</span>{holiday_strip(calendar)}</div></header>"""
    body += '<main class="letterboard">'
    for num, (key, arr) in enumerate(sections.items(), 1):
        name, en = _NAMES[key]
        body += f'<section class="lettersection {key}"><div class="labeltab"><span>{num:02}</span><h2 class="serif">{name}札记</h2><i>{en}</i><b>{len(arr):02} 则</b></div>'
        if not arr:
            body += _EMPTY_SECTION + "</section>"
            continue
        feature = feature_index(arr)
        item = arr[feature]
        photo = picture(item)
        polaroid = (
            f'<div class="polaroid">{photo}<span>图 / '
            f"{esc(item.get('image_source') or item.get('source', ''))}</span></div>"
            if photo
            else ""
        )
        body += f"""<article class="news letterfeature" data-news="{key}" data-index="{feature + 1}">{polaroid}<div class="featuretext"><div class="eyebrow">TODAY’S NOTE / {feature + 1:02}</div><h3 class="serif">{title(item)}</h3><div class="meta">{meta(item)}</div><div class="flourish">· · ·</div></div></article>"""
        remaining = [article(x, key, i) for i, x in enumerate(arr) if i != feature]
        if key == "games":
            body += '<div class="letterlist full">' + "".join(remaining) + "</div>"
        else:
            cut = (len(remaining) + 1) // 2
            body += (
                '<div class="spread"><div class="letterlist">'
                + "".join(remaining[:cut])
                + '</div><div class="letterlist">'
                + "".join(remaining[cut:])
                + "</div></div>"
            )
        body += "</section>"
    body += "</main>" + footer(sections) + "</div>"
    return body


def _build_dailydrop(sections: ReportSections, calendar: _Calendar) -> str:
    """Render the approved dailydrop body without changing its layout.

    Args:
        sections: Four ordered lists of collected articles.
        calendar: Dynamic date and reminder fields for this report.

    Returns:
        The complete body of the selected layout.
    """
    body = f"""<header><div class="drop-top"><b>DAILY BRIEF</b><span>资讯补给 / {calendar["year"]}.{calendar["month"]}.{calendar["date"]}</span></div><div class="drop-mast"><div><div class="micro">每天一份，新鲜事。</div><h1>{REPORT_TITLE[:2]}<span>{REPORT_TITLE[2:]}</span></h1><div class="tags"><b>时政</b><b>社会</b><b>游戏</b><b>数码</b></div></div><div class="ticket"><div>DATE / {calendar["year"]}</div><strong>{calendar["month"]}<em>{calendar["date"]}</em></strong><p>{calendar["week"]}</p><small>农历{calendar["lunar"]}</small><i></i></div></div><div class="drop-note">NEXT UP <span>接下来的好日子 →</span></div>{holiday_strip(calendar)}</header>"""
    for num, (key, arr) in enumerate(sections.items(), 1):
        name, en = _NAMES[key]
        body += f'<section class="drop-section {key}"><div class="drop-head"><b>{num:02}</b><h2>{name}<span>{en}</span></h2><i>{len(arr):02} NEWS</i></div>'
        if not arr:
            body += _EMPTY_SECTION + "</section>"
            continue
        featured = feature_index(arr)
        body += article(arr[featured], key, featured, "drop-feature", True)
        remain = [article(x, key, i) for i, x in enumerate(arr) if i != featured]
        body += '<div class="drop-grid">' + "".join(remain) + "</div></section>"
    body += footer(sections)
    return body


def _build_greenfolio(sections: ReportSections, calendar: _Calendar) -> str:
    """Render the approved greenfolio body without changing its layout.

    Args:
        sections: Four ordered lists of collected articles.
        calendar: Dynamic date and reminder fields for this report.

    Returns:
        The complete body of the selected layout.
    """
    body = f"""<header><div class="folio-top"><span>DAILY BRIEF / 每日资讯</span><span>THE WORLD, AT YOUR PACE.</span><span>{calendar["year"]}.{calendar["month"]}.{calendar["date"]}</span></div><div class="folio-mast"><div class="folio-title"><span class="kicker">一页之间，自有新知</span><h1 class="serif">{REPORT_TITLE[:2]}<span>{REPORT_TITLE[2:]}</span></h1></div><div class="foliodate"><b>{calendar["month"]}<span>月</span>{calendar["date"]}<span>日</span></b><p>{calendar["week"]} / 农历{calendar["lunar"]}</p></div></div><div class="folio-intro"><span>时政 · 国内社会 · 游戏 · 数码</span><span>四栏并读 · {sum(map(len, sections.values()))} 则资讯</span></div><div class="folio-cal"><div class="calcaption"><span class="serif">日有盼</span><small>UPCOMING</small></div>{holiday_strip(calendar)}</div></header><main class="folio-columns">"""
    for num, (key, arr) in enumerate(sections.items(), 1):
        name, en = _NAMES[key]
        body += f'<section class="folio-column {key}"><div class="folio-sectionhead"><span>0{num}</span><h2 class="serif">{name}</h2><div>{en}<b>{len(arr):02} 则</b></div></div>'
        if not arr:
            body += _EMPTY_SECTION + "</section>"
            continue
        chosen = feature_index(arr) if any(x.get("image_path") for x in arr) else None
        for i, item in enumerate(arr):
            body += article(
                item,
                key,
                i,
                "folio-item" + (" withphoto" if i == chosen else ""),
                i == chosen,
            )
        body += "</section>"
    body += "</main>" + footer(sections)
    return body


_BUILDERS: dict[str, Callable[[ReportSections, _Calendar], str]] = {
    "crosspoint": _build_crosspoint,
    "nightwire": _build_nightwire,
    "dayletter": _build_dayletter,
    "dailydrop": _build_dailydrop,
    "greenfolio": _build_greenfolio,
}


def build_report(sections: ReportSections, day: date, style_id: str) -> str:
    """Build a complete, safe HTML report using one approved visual design.

    Args:
        sections: Collected articles keyed by politics, society, games, and
            tech. Each item provides title, source, url, published,
            image_path, and image_source. Missing sections render empty.
            Local image paths may be absolute or relative to the working
            directory. Images are inlined; no remote resources are fetched.
        day: Local report date supplied by the caller.
        style_id: Stable template identifier from TEMPLATES.

    Returns:
        A self-contained HTML document ready for the native renderer. Use
        get_template(style_id).width for the browser viewport width.

    Raises:
        ValueError: If style_id does not name a registered template.
        OSError: If an installed CSS resource cannot be read.
    """
    template = get_template(style_id)
    ordered = {key: sections.get(key, ()) for key in _NAMES}
    calendar = calendar_for(day)
    body = _BUILDERS[template.id](ordered, calendar)
    css_dir = _RESOURCE_DIR / "templates"
    base_css = (css_dir / "base.css").read_text(encoding="utf-8")
    style_css = (css_dir / f"{template.id}.css").read_text(encoding="utf-8")
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width={template.width}">'
        f"<title>{REPORT_TITLE} · {esc(template.label)}</title><style>{base_css}\n"
        f"body{{width:{template.width}px}}\n{style_css}\n"
        ".news-photo.document-photo{object-fit:contain;background:#e9e9e1}\n"
        f'</style></head><body><div class="sheet">{body}</div></body></html>'
    )
