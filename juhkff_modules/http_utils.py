"""Bounded GET requests to public HTTP endpoints, including through a proxy."""

from __future__ import annotations

import asyncio
import ipaddress
import math
import re
import socket
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp
from aiohttp.helpers import get_env_proxy_for_url
from yarl import URL

_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def validate_url(url: str, *, allowed_hosts: set[str] | None = None) -> str:
    """Normalize an HTTP URL and reject unsafe hosts and URL syntax.

    Args:
        url: Absolute HTTP(S) URL without credentials, using port 80 or 443.
        allowed_hosts: Optional exact host allowlist, normalized with IDNA,
            lowercase, and removal of a trailing dot. No suffix matching occurs.

    Returns:
        Normalized logical URL without its fragment. This only checks URL syntax
        and literal IPs; public_request also resolves and checks every DNS result.

    Raises:
        ValueError: The URL or host is invalid, forbidden, or outside the allowlist.
    """
    if not isinstance(url, str) or re.search(r"[\s\x00-\x20\x7f\\]", url):
        raise ValueError("Invalid HTTP URL")
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"}:
            raise ValueError("Only HTTP(S) URLs are allowed")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("URL credentials are not allowed")
        if parsed.port is not None and parsed.port not in {80, 443}:
            raise ValueError("Only ports 80 and 443 are allowed")
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
        host = host.rstrip(".")
        if not host or "%" in host:
            raise ValueError("Invalid HTTP host")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if len(host) > 253 or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in host.split(".")
            ):
                raise ValueError("Invalid HTTP host") from None
            if any(
                host == suffix or host.endswith("." + suffix)
                for suffix in ("localhost", "local", "internal")
            ):
                raise ValueError("Local HTTP hosts are not allowed")
        else:
            if not address.is_global or address.is_multicast or address.is_reserved:
                raise ValueError("Non-public HTTP addresses are not allowed")
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
                if not address.ipv4_mapped.is_global:
                    raise ValueError("Non-public HTTP addresses are not allowed")
            host = address.compressed
        if allowed_hosts is not None:
            normalized_allowed = {
                name.encode("idna").decode("ascii").lower().rstrip(".")
                for name in allowed_hosts
            }
            if host not in normalized_allowed:
                raise ValueError("HTTP host is not allowed")
        authority = f"[{host}]" if ":" in host else host
        if parsed.port is not None:
            authority += f":{parsed.port}"
        return str(
            URL(
                urlunsplit(
                    (
                        parsed.scheme.lower(),
                        authority,
                        parsed.path or "/",
                        parsed.query,
                        "",
                    )
                )
            )
        )
    except (UnicodeError, TypeError) as exc:
        raise ValueError("Invalid HTTP URL") from exc


async def public_request(
    url: str,
    *,
    proxy: str | None = None,
    trust_env: bool = False,
    timeout: float = 20.0,
    max_bytes: int = 3 * 1024 * 1024,
    headers: dict[str, Any] | None = None,
    allowed_hosts: set[str] | None = None,
) -> tuple[bytes, str]:
    """GET a public URL with pinned DNS, bounded redirects, time, and bytes.

    Args:
        url: Absolute public HTTP(S) URL; query parameters must already be encoded.
        proxy: Explicit trusted proxy configuration, including global.proxy when set.
        trust_env: When no explicit proxy is set, resolve environment proxies and
            NO_PROXY against each logical hostname, including proxy BasicAuth.
        timeout: Total wallclock seconds for DNS, redirects, and body consumption.
        max_bytes: Maximum Content-Length and streamed, decompressed body size.
        headers: Optional request headers. Host is always set to the logical host;
            authorization and cookie headers are removed on cross-origin redirects.
        allowed_hosts: Optional exact host allowlist checked on every redirect.

    Returns:
        Response bytes and the final logical URL, never the pinned IP URL.

    Raises:
        ValueError: URL, DNS addresses, limits, or redirect target are unsafe.
        RuntimeError: HTTP failure, oversized response, or too many redirects.
        asyncio.TimeoutError: The total request deadline was exceeded.
        aiohttp.ClientError: The transport or verified TLS connection failed.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    if not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    logical_url = validate_url(url, allowed_hosts=allowed_hosts)

    async def request() -> tuple[bytes, str]:
        current = logical_url
        request_headers = {
            str(key): str(value)
            for key, value in (headers or {}).items()
            if str(key).lower() != "host"
        }
        # Pinned URL keys omit the logical TLS hostname. Never pool connections
        # between requests: two different hostnames can resolve to the same IP.
        connector = aiohttp.TCPConnector(force_close=True)
        async with aiohttp.ClientSession(
            connector=connector,
            cookie_jar=aiohttp.DummyCookieJar(),
            timeout=aiohttp.ClientTimeout(total=None),
            trust_env=False,
            auto_decompress=True,
        ) as session:
            for hop in range(_MAX_REDIRECTS + 1):
                logical = URL(current)
                host = logical.raw_host
                assert host is not None
                addresses = await asyncio.get_running_loop().getaddrinfo(
                    host, logical.port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
                )
                if not addresses:
                    raise ValueError("HTTP host has no DNS addresses")
                approved = []
                for family, _type, _proto, _name, sockaddr in addresses:
                    if family not in {socket.AF_INET, socket.AF_INET6}:
                        raise ValueError("Unsupported HTTP address family")
                    address = ipaddress.ip_address(sockaddr[0])
                    if (
                        not address.is_global
                        or address.is_multicast
                        or address.is_reserved
                    ):
                        raise ValueError("HTTP DNS contains a non-public address")
                    if (
                        isinstance(address, ipaddress.IPv6Address)
                        and address.ipv4_mapped
                    ):
                        if not address.ipv4_mapped.is_global:
                            raise ValueError("HTTP DNS contains a non-public address")
                    approved.append(address.compressed)
                # Prefer IPv4 on dual-stack hosts, including deployments without
                # working IPv6 egress. All DNS answers were checked above.
                approved.sort(key=lambda address: ":" in address)
                # The proxy must receive an IP destination too. A custom resolver
                # alone cannot pin an origin resolved by an HTTP proxy.
                pinned = logical.with_host(approved[0])
                request_proxy = proxy or None
                proxy_auth = None
                if trust_env and not request_proxy:
                    try:
                        request_proxy, proxy_auth = await asyncio.to_thread(
                            get_env_proxy_for_url, logical
                        )
                    except LookupError:
                        pass  # No proxy configured, or logical host matches NO_PROXY.
                request_headers["Host"] = logical.raw_authority
                async with session.get(
                    pinned,
                    proxy=request_proxy,
                    proxy_auth=proxy_auth,
                    headers=request_headers,
                    server_hostname=host,
                    allow_redirects=False,
                ) as response:
                    if response.status not in _REDIRECT_STATUSES and not (
                        200 <= response.status < 300
                    ):
                        raise RuntimeError(f"HTTP {response.status} {current}")
                    if response.content_length is not None:
                        if response.content_length > max_bytes:
                            raise RuntimeError("HTTP response exceeds max_bytes")
                    if response.status in _REDIRECT_STATUSES:
                        location = response.headers.get("Location")
                        if not location:
                            raise RuntimeError(f"HTTP {response.status} {current}")
                        if hop == _MAX_REDIRECTS:
                            raise RuntimeError("Too many HTTP redirects")
                        target = validate_url(
                            urljoin(current, location), allowed_hosts=allowed_hosts
                        )
                        if URL(target).origin() != logical.origin():
                            request_headers = {
                                key: value
                                for key, value in request_headers.items()
                                if key.lower()
                                not in {
                                    "authorization",
                                    "cookie",
                                    "proxy-authorization",
                                }
                            }
                        current = target
                        response.close()
                        continue
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        if len(body) + len(chunk) > max_bytes:
                            raise RuntimeError("HTTP response exceeds max_bytes")
                        body.extend(chunk)
                    return bytes(body), current
        raise RuntimeError("Too many HTTP redirects")

    return await asyncio.wait_for(request(), timeout=timeout)
