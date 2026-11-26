"""Translate images with the Baidu Translation Open Platform picture API."""

import asyncio
import base64
import binascii
import hashlib
import io
import json
import secrets
import tempfile
from pathlib import Path

import aiohttp
from PIL import Image

ENDPOINT = "https://fanyi-api.baidu.com/api/trans/sdk/picture"
API_VERSION = "3"
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 24 * 1024 * 1024
MAX_SIDE = 16384
MAX_PIXELS = 40_000_000

# Picture language codes, including the traditional Chinese target variant.
_LANGUAGE_GROUPS = {
    "zh": "zh-cn zh-hans Chinese 中文 简体中文 简体",
    "cht": "zh-tw zh-hk zh-hant traditional-chinese 繁体中文 繁體中文 繁体 繁體",
    "en": "English 英语 英文",
    "jp": "ja Japanese 日语 日本語 日文",
    "kor": "ko Korean 韩语 韓語",
    "fra": "fr French 法语",
    "de": "German 德语",
    "pt": "Portuguese 葡萄牙语",
    "it": "Italian 意大利语",
    "spa": "es Spanish 西班牙语",
    "ru": "Russian 俄语",
    "dan": "da Danish 丹麦语",
    "nl": "Dutch 荷兰语",
    "may": "ms Malay 马来语",
    "swe": "sv Swedish 瑞典语",
    "id": "Indonesian 印度尼西亚语 印尼语",
    "pl": "Polish 波兰语",
    "rom": "ro Romanian 罗马尼亚语",
    "tr": "Turkish 土耳其语",
    "el": "Greek 希腊语",
    "hu": "Hungarian 匈牙利语",
}
LANGUAGES = {
    alias.casefold(): code
    for code, aliases in _LANGUAGE_GROUPS.items()
    for alias in (code, *aliases.split())
}
LANGUAGES.update(
    {
        "simplified chinese": "zh",
        "chinese (simplified)": "zh",
        "traditional chinese": "cht",
        "chinese (traditional)": "cht",
    }
)


def _read_bounded(path: Path, limit: int) -> bytes:
    """Read bounded image bytes in a worker thread.

    Args:
        path: Local image path.
        limit: Maximum permitted byte count.

    Returns:
        Complete image bytes.

    Raises:
        ValueError: The file is empty or too large.
    """
    with path.open("rb") as source:
        data = source.read(limit + 1)
    if not data or len(data) > limit:
        raise ValueError(
            f"Baidu image must be nonempty and at most {limit // (1024 * 1024)} MiB; "
            "resize or compress the source image before translating"
        )
    return data


def _verify_image(data: bytes) -> str:
    """Check image integrity and dimensions without decoding a full pixel buffer.

    Args:
        data: Bounded PNG or JPEG image bytes.

    Returns:
        File extension matching the image's actual format.

    Raises:
        ValueError: Image format, integrity, or dimensions are invalid.
    """
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if image.format not in {"PNG", "JPEG"}:
                raise ValueError("Baidu image must be a PNG or JPEG")
            if (
                min(width, height) < 1
                or max(width, height) > MAX_SIDE
                or width * height > MAX_PIXELS
            ):
                raise ValueError(
                    "Baidu image exceeds local safety dimensions (16384px per side "
                    "or 40 million pixels); resize or crop the image before translating"
                )
            extension = ".png" if image.format == "PNG" else ".jpg"
            if extension == ".jpg" and b"\xff\xd9" not in data:
                raise ValueError("Baidu returned an incomplete JPEG image")
            image.verify()
            return extension
    except (OSError, SyntaxError, Image.DecompressionBombError):
        raise ValueError("Baidu image is not a valid PNG or JPEG") from None


async def translate_image(
    image: Path, language: str, app_id: str, app_key: str
) -> Path:
    """Translate an image and atomically cache the verified rendered result.

    Args:
        image: Source PNG or JPEG; its original bytes remain unchanged.
        language: Supported target language name, alias, or Baidu language code.
        app_id: Baidu Translation Open Platform application ID.
        app_key: Application secret for an account with picture translation enabled.

    Returns:
        Path to the cached PNG or JPEG result.

    Raises:
        ValueError: Configuration, input, or response is invalid or too large.
        RuntimeError: Baidu rejects the request or the request fails.
        OSError: Reading the source or writing the cache fails.
    """
    if not isinstance(language, str) or language.strip().casefold() not in LANGUAGES:
        raise ValueError("Unsupported Baidu picture target language")
    target = LANGUAGES[language.strip().casefold()]
    if not all(isinstance(value, str) and value.strip() for value in (app_id, app_key)):
        raise ValueError("Configure both Baidu application ID and application key")
    app_id, app_key = app_id.strip(), app_key.strip()
    image = Path(image)
    source = await asyncio.to_thread(_read_bounded, image, MAX_IMAGE_BYTES)
    extension = await asyncio.to_thread(_verify_image, source)
    digest = hashlib.sha256(source)
    digest.update(json.dumps([target, app_id, API_VERSION]).encode())
    stem = f".baidu-{digest.hexdigest()}"

    def cached() -> Path | None:
        """Find a valid existing cache without blocking the event loop.

        Returns:
            Existing verified image path, if present.
        """
        for suffix in (".png", ".jpg"):
            candidate = image.with_name(stem + suffix)
            if candidate.resolve() == image.resolve():
                continue
            try:
                data = _read_bounded(candidate, MAX_OUTPUT_BYTES)
                if _verify_image(data) == suffix:
                    candidate.chmod(0o644)
                    return candidate
            except (OSError, ValueError):
                pass
        return None

    existing = await asyncio.to_thread(cached)
    if existing is not None:
        return existing
    salt = secrets.token_hex(16)
    sign = hashlib.md5(
        (
            app_id
            + hashlib.md5(source).hexdigest()
            + salt
            + "APICUID"
            + "mac"
            + app_key
        ).encode()
    ).hexdigest()
    form = aiohttp.FormData()
    form.add_field(
        "image",
        source,
        filename="image" + extension,
        content_type="image/png" if extension == ".png" else "image/jpeg",
    )
    for name, value in {
        "from": "auto",
        "to": target,
        "appid": app_id,
        "salt": salt,
        "sign": sign,
        "cuid": "APICUID",
        "mac": "mac",
        "paste": "1",
        "version": API_VERSION,
    }.items():
        form.add_field(name, value)
    try:
        async with aiohttp.ClientSession(
            trust_env=False, timeout=aiohttp.ClientTimeout(total=300)
        ) as session:
            async with session.post(
                ENDPOINT, data=form, allow_redirects=False
            ) as response:
                if response.status != 200:
                    raise RuntimeError(
                        f"Baidu picture translation returned HTTP {response.status}"
                    )
                if (
                    response.content_length is not None
                    and response.content_length > MAX_RESPONSE_BYTES
                ):
                    raise ValueError("Baidu picture response exceeds the size limit")
                result = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    if len(result) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ValueError(
                            "Baidu picture response exceeds the size limit"
                        )
                    result.extend(chunk)
    except (aiohttp.ClientError, TimeoutError):
        raise RuntimeError(
            "Baidu picture translation request failed or timed out"
        ) from None

    def publish() -> Path:
        """Parse, validate, and atomically publish the bounded API response.

        Returns:
            Published image path.

        Raises:
            ValueError: The response is malformed or contains an invalid image.
            RuntimeError: The API reports an error.
            OSError: Cache publication fails.
        """
        try:
            payload = json.loads(result)
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError(
                "Baidu picture translation returned invalid JSON"
            ) from None
        if not isinstance(payload, dict):
            raise ValueError(
                "Baidu picture translation returned invalid JSON structure"
            )
        code = str(payload.get("error_code", "0"))
        if code != "0":
            # Never echo provider messages: they may contain credentials or input.
            label = (
                f" (code {code})"
                if code.isascii() and code.isdecimal() and len(code) <= 8
                else ""
            )
            raise RuntimeError(
                f"Baidu picture translation was rejected{label}; check credentials, "
                "picture API activation, language support, and account quota"
            )
        data = payload.get("data")
        encoded = data.get("pasteImg") if isinstance(data, dict) else None
        if not isinstance(encoded, str) or not encoded:
            raise ValueError(
                "Baidu picture response has no rendered image (data.pasteImg)"
            )
        if len(encoded) > 4 * ((MAX_OUTPUT_BYTES + 2) // 3):
            raise ValueError("Baidu rendered image exceeds the size limit")
        try:
            rendered = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("Baidu rendered image is not valid base64") from None
        if not rendered or len(rendered) > MAX_OUTPUT_BYTES:
            raise ValueError("Baidu rendered image exceeds the size limit")
        suffix = _verify_image(rendered)
        output = image.with_name(stem + suffix)
        if output.resolve() == image.resolve():
            raise ValueError("Baidu cache path must differ from the source image")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=output.parent, prefix=".baidu-tmp-", suffix=suffix, delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(rendered)
            temporary.chmod(0o644)
            temporary.replace(output)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return output

    return await asyncio.to_thread(publish)
