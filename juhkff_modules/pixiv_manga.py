"""Translate manga images using an operator-configured manga-image-translator."""

import asyncio
import hashlib
import io
import ipaddress
import json
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from PIL import Image

MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_SIDE = 16384
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Codes match manga-image-translator's translators/common.py VALID_LANGUAGES.
_LANGUAGE_GROUPS = {
    "CHS": "zh zh-cn zh-hans Chinese 中文 简体中文 简体",
    "CHT": "zh-tw zh-hk zh-hant traditional-chinese 繁体中文 繁體中文 繁体 繁體",
    "CSY": "cs Czech 捷克语",
    "NLD": "nl Dutch 荷兰语",
    "ENG": "en English 英语 英文",
    "FRA": "fr French 法语",
    "DEU": "de German 德语",
    "HUN": "hu Hungarian 匈牙利语",
    "ITA": "it Italian 意大利语",
    "JPN": "ja Japanese 日语 日本語 日文",
    "KOR": "ko Korean 韩语 韓語",
    "POL": "pl Polish 波兰语",
    "PTB": "pt pt-br Portuguese 葡萄牙语",
    "ROM": "ro Romanian 罗马尼亚语",
    "RUS": "ru Russian 俄语",
    "ESP": "es Spanish 西班牙语",
    "TRK": "tr Turkish 土耳其语",
    "UKR": "uk Ukrainian 乌克兰语",
    "VIN": "vi Vietnamese 越南语",
    "ARA": "ar Arabic 阿拉伯语",
    "CNR": "cnr Montenegrin 黑山语",
    "SRP": "sr Serbian 塞尔维亚语",
    "HRV": "hr Croatian 克罗地亚语",
    "THA": "th Thai 泰语",
    "IND": "id Indonesian 印度尼西亚语 印尼语",
    "FIL": "tl fil Filipino Tagalog 菲律宾语",
}
LANGUAGES = {
    alias.casefold(): code
    for code, aliases in _LANGUAGE_GROUPS.items()
    for alias in (code, *aliases.split())
}
LANGUAGES.update(
    {
        "simplified chinese": "CHS",
        "chinese (simplified)": "CHS",
        "traditional chinese": "CHT",
        "chinese (traditional)": "CHT",
        "portuguese (brazil)": "PTB",
        "filipino (tagalog)": "FIL",
    }
)


def _read_bounded(path: Path) -> bytes:
    """Read a file without allocating more than the configured image limit.

    Args:
        path: File to read.

    Returns:
        Complete bounded file contents.

    Raises:
        ValueError: The file is empty or exceeds the limit.
    """
    with path.open("rb") as source:
        data = source.read(MAX_IMAGE_BYTES + 1)
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Manga image must be nonempty and at most 64 MiB")
    return data


def _verify_png(data: bytes) -> None:
    """Verify PNG integrity and dimensions without changing image data.

    Args:
        data: Bounded PNG bytes.

    Raises:
        ValueError: PNG data is invalid or dimensions exceed the limits.
    """
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("Manga translator response is not a PNG image")
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if (
                image.format != "PNG"
                or min(width, height) < 1
                or max(width, height) > MAX_SIDE
                or width * height > MAX_PIXELS
            ):
                raise ValueError("Manga PNG exceeds the permitted image dimensions")
            image.verify()
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError("Manga translator returned an invalid PNG image") from exc


async def translate_image(
    image: Path, language: str, base_url: str, translator: str
) -> Path:
    """Translate an image via the official multipart endpoint and cache its PNG.

    Args:
        image: Local source image; its bytes are never modified.
        language: Supported language name, alias, or official language code.
        base_url: Trusted operator HTTP(S) backend URL, including optional base path.
        translator: Backend translator identifier, such as ``offline``.

    Returns:
        Path to a verified, atomically published cached PNG.

    Raises:
        ValueError: Configuration, input size, or returned PNG is invalid.
        RuntimeError: The backend returns a non-200 HTTP status.
        aiohttp.ClientError: The HTTP request fails.
        TimeoutError: The request exceeds 300 seconds.
    """
    if not isinstance(language, str) or language.strip().casefold() not in LANGUAGES:
        raise ValueError(f"Unsupported manga target language: {language!r}")
    target = LANGUAGES[language.strip().casefold()]
    if not isinstance(translator, str) or not translator.strip():
        raise ValueError("Manga translator identifier must be a nonempty string")
    translator = translator.strip()
    try:
        if (
            not isinstance(base_url, str)
            or any(char.isspace() or ord(char) < 32 for char in base_url)
            or any(char in base_url for char in "\\?#")
        ):
            raise ValueError("Invalid URL characters")
        parsed = urlsplit(base_url)
        host = parsed.hostname
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port == 0
            or not re.fullmatch(r"(?:\[[^\]]+\]|[^:\[\]]+)(?::[0-9]+)?", parsed.netloc)
        ):
            raise ValueError("Invalid HTTP URL")
        if ":" in host:
            ipaddress.IPv6Address(host)
        else:
            ascii_host = host.encode("idna").decode("ascii").rstrip(".")
            if len(ascii_host) > 253 or not all(
                re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9_-]{0,61}[a-zA-Z0-9])?", label)
                for label in ascii_host.split(".")
            ):
                raise ValueError("Invalid hostname")
        if re.search(r"%(?![0-9a-fA-F]{2})", parsed.path):
            raise ValueError("Invalid URL path escaping")
    except (ValueError, UnicodeError) as exc:
        raise ValueError(
            "Manga backend URL must be a valid HTTP(S) URL without credentials, query, or fragment"
        ) from exc
    endpoint = base_url.rstrip("/") + "/translate/with-form/image"
    image = Path(image)
    source = await asyncio.to_thread(_read_bounded, image)
    digest = hashlib.sha256(source)
    digest.update(
        json.dumps([target, endpoint, translator], ensure_ascii=False).encode()
    )
    output = image.with_name(f".manga-{digest.hexdigest()}.png")
    if output.resolve() == image.resolve():
        raise ValueError("Manga cache path must differ from the source image")

    def cached() -> bool:
        """Check the cached PNG in a worker thread.

        Returns:
            Whether a valid cache file already exists.
        """
        try:
            _verify_png(_read_bounded(output))
            output.chmod(0o644)
            return True
        except (OSError, ValueError):
            return False

    if await asyncio.to_thread(cached):
        return output
    form = aiohttp.FormData()
    form.add_field(
        "image", source, filename=image.name, content_type="application/octet-stream"
    )
    form.add_field(
        "config",
        json.dumps({"translator": {"translator": translator, "target_lang": target}}),
    )
    async with aiohttp.ClientSession(
        trust_env=False, timeout=aiohttp.ClientTimeout(total=300)
    ) as session:
        async with session.post(endpoint, data=form, allow_redirects=False) as response:
            if response.status != 200:
                raise RuntimeError(f"Manga translator returned HTTP {response.status}")
            if (
                response.content_length is not None
                and response.content_length > MAX_IMAGE_BYTES
            ):
                raise ValueError("Manga translator response exceeds 64 MiB")
            result = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                if len(result) + len(chunk) > MAX_IMAGE_BYTES:
                    raise ValueError("Manga translator response exceeds 64 MiB")
                result.extend(chunk)

    def publish() -> None:
        """Verify and atomically save the returned PNG in a worker thread.

        Raises:
            ValueError: The backend returned invalid image data.
            OSError: The cache cannot be written.
        """
        _verify_png(result)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=output.parent, prefix=".manga-tmp-", suffix=".png", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(result)
            temporary.chmod(0o644)
            temporary.replace(output)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    await asyncio.to_thread(publish)
    return output
