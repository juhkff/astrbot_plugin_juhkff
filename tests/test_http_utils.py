"""Offline coverage of public HTTP validation, pinning, and response bounds."""

from __future__ import annotations

import asyncio
import socket
import unittest
from typing import Any, cast
from unittest.mock import AsyncMock, patch, sentinel

from juhkff_modules import http_utils
from yarl import URL


def dns_result(*addresses: str):
    return [
        (
            socket.AF_INET6 if ":" in address else socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            (address, 443),
        )
        for address in addresses
    ]


class FakeContent:
    def __init__(self, chunks=()):
        self.chunks = chunks
        self.reads = 0

    async def iter_chunked(self, _size):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk


class FakeResponse:
    def __init__(self, status=200, *, headers=None, chunks=(), content_length=None):
        self.status = status
        self.headers = headers or {}
        self.content_length = content_length
        self.content = FakeContent(chunks)
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.close()

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    def get(self, url, **kwargs):
        kwargs["headers"] = dict(kwargs["headers"])
        self.calls.append((URL(url), kwargs))
        return next(self.responses)


class ValidateURLTests(unittest.TestCase):
    def test_normalization_and_exact_allowlist(self):
        self.assertEqual(
            http_utils.validate_url(
                "HTTPS://EXAMPLE.COM./path?q=1#fragment", allowed_hosts={"Example.Com."}
            ),
            "https://example.com/path?q=1",
        )
        self.assertEqual(
            http_utils.validate_url("https://例子.测试/path"),
            "https://xn--fsqu00a.xn--0zwm56d/path",
        )
        for url in (
            "https://example.com.evil.test/",
            "https://sub.example.com/",
            "https://evil.test/example.com/",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                http_utils.validate_url(url, allowed_hosts={"example.com"})

    def test_rejects_unsafe_syntax_and_local_addresses(self):
        for url in (
            "file:///etc/passwd",
            "ftp://example.com/file",
            "//example.com/path",
            "https://user:password@example.com/",
            "https://user@example.com/",
            "https://example.com:8080/",
            "https://example.com:22/",
            "https://example.com:bad/",
            "https://localhost/",
            "https://LOCALHOST./",
            "https://foo.localhost/",
            "https://foo.local/",
            "https://foo.INTERNAL./",
            "https://foo。local/",
            "http://127.0.0.1/",
            "http://10.2.3.4/",
            "http://169.254.169.254/",
            "http://100.64.0.1/",
            "http://192.0.2.1/",
            "http://224.0.0.1/",
            "http://[::1]/",
            "http://[fc00::1]/",
            "http://[4000::1]/",
            "http://[::ffff:127.0.0.1]/",
            "http://[fe80::1%25eth0]/",
            "https://example.com\\@127.0.0.1/",
            "https://exam\nple.com/",
            "https://example.com/a b",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                http_utils.validate_url(url)

    def test_public_literals_and_permitted_ports(self):
        for url in (
            "http://8.8.8.8/",
            "https://[2001:4860:4860::8888]/",
            "https://example.com:80/",
            "http://example.com:443/",
        ):
            with self.subTest(url=url):
                self.assertTrue(http_utils.validate_url(url).startswith("http"))


class PublicRequestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sessions = []
        self.session_options = []
        self.dns = AsyncMock(return_value=dns_result("93.184.216.34"))
        self.patches = [
            patch.object(asyncio.get_running_loop(), "getaddrinfo", self.dns),
            patch.object(
                http_utils.aiohttp, "ClientSession", side_effect=self.make_session
            ),
            patch.object(
                http_utils.aiohttp, "TCPConnector", return_value=sentinel.connector
            ),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_session(self, **kwargs):
        self.session_options.append(kwargs)
        session = FakeSession(self.responses)
        self.sessions.append(session)
        return session

    async def test_dns_rebinding_pins_ip_direct_and_through_proxy(self):
        for proxy in (None, "http://127.0.0.1:7890"):
            with self.subTest(proxy=proxy):
                self.responses = [FakeResponse(chunks=[b"hello"])]
                # A second lookup would return a private address. The request
                # must carry only the first approved IP, including to a proxy.
                self.dns.reset_mock()
                self.dns.side_effect = [
                    dns_result("93.184.216.34"),
                    dns_result("127.0.0.1"),
                ]
                body, final = await http_utils.public_request(
                    "https://public.example/path?q=1",
                    proxy=proxy,
                    headers={"Host": "attacker.example", "X-Test": "ok"},
                )
                self.assertEqual(
                    (body, final), (b"hello", "https://public.example/path?q=1")
                )
                self.assertEqual(self.dns.await_count, 1)
                url, options = self.sessions[-1].calls[0]
                self.assertEqual(str(url), "https://93.184.216.34/path?q=1")
                self.assertEqual(options["headers"]["Host"], "public.example")
                self.assertEqual(options["headers"]["X-Test"], "ok")
                self.assertEqual(options["server_hostname"], "public.example")
                self.assertEqual(options["proxy"], proxy)
                self.assertFalse(options["allow_redirects"])
                self.assertNotIn("ssl", options)  # aiohttp's verified TLS default.
                self.assertFalse(self.session_options[-1]["trust_env"])
                self.assertTrue(self.session_options[-1]["auto_decompress"])
                cast(Any, http_utils.aiohttp.TCPConnector).assert_called_with(
                    force_close=True
                )

    async def test_environment_proxy_and_no_proxy_use_each_logical_url(self):
        self.responses = [
            FakeResponse(302, headers={"Location": "https://other.example/new"}),
            FakeResponse(chunks=[b"ok"]),
        ]
        auth = http_utils.aiohttp.BasicAuth("proxy-user", "proxy-password")
        with patch.object(
            http_utils,
            "get_env_proxy_for_url",
            side_effect=[(URL("http://proxy.example:7890"), auth), LookupError()],
        ) as environment_proxy:
            await http_utils.public_request("https://public.example/", trust_env=True)
        self.assertEqual(
            [call.args[0] for call in environment_proxy.call_args_list],
            [URL("https://public.example/"), URL("https://other.example/new")],
        )
        first, second = self.sessions[-1].calls
        self.assertEqual(first[0].host, "93.184.216.34")
        self.assertEqual(first[1]["proxy"], URL("http://proxy.example:7890"))
        self.assertEqual(first[1]["proxy_auth"], auth)
        self.assertIsNone(second[1]["proxy"])
        self.assertIsNone(second[1]["proxy_auth"])
        self.assertEqual(second[1]["headers"]["Host"], "other.example")
        self.assertFalse(self.session_options[-1]["trust_env"])

    async def test_explicit_proxy_overrides_environment_proxy(self):
        self.responses = [FakeResponse(chunks=[b"ok"])]
        with patch.object(http_utils, "get_env_proxy_for_url") as environment_proxy:
            await http_utils.public_request(
                "https://public.example/",
                proxy="http://127.0.0.1:7890",
                trust_env=True,
            )
        environment_proxy.assert_not_called()
        self.assertEqual(
            self.sessions[-1].calls[0][1]["proxy"], "http://127.0.0.1:7890"
        )

    async def test_environment_proxy_is_opt_in(self):
        self.responses = [FakeResponse(chunks=[b"ok"])]
        with patch.object(http_utils, "get_env_proxy_for_url") as environment_proxy:
            await http_utils.public_request("https://public.example/")
        environment_proxy.assert_not_called()
        self.assertIsNone(self.sessions[-1].calls[0][1]["proxy"])

    async def test_dual_stack_prefers_approved_ipv4_when_dns_lists_ipv6_first(self):
        self.dns.return_value = dns_result("2001:4860:4860::8888", "93.184.216.34")
        self.responses = [FakeResponse(chunks=[b"ok"])]
        await http_utils.public_request("https://public.example/")
        self.assertEqual(self.sessions[-1].calls[0][0].host, "93.184.216.34")

    async def test_ipv6_pin_retains_host_and_port(self):
        self.dns.return_value = dns_result("2001:4860:4860::8888")
        self.responses = [FakeResponse(chunks=[b"ok"])]
        await http_utils.public_request("https://public.example:80/a")
        url, options = self.sessions[-1].calls[0]
        self.assertEqual(str(url), "https://[2001:4860:4860::8888]:80/a")
        self.assertEqual(options["headers"]["Host"], "public.example:80")
        self.assertEqual(options["server_hostname"], "public.example")

    async def test_rejects_all_private_or_mixed_dns_before_http(self):
        for addresses in (
            ("127.0.0.1",),
            ("10.0.0.1", "192.168.0.1"),
            ("93.184.216.34", "127.0.0.1"),
            ("93.184.216.34", "::ffff:127.0.0.1"),
            ("224.0.0.1",),
        ):
            with self.subTest(addresses=addresses), self.assertRaises(ValueError):
                self.responses = []
                self.dns.return_value = dns_result(*addresses)
                await http_utils.public_request("https://public.example/")
            self.assertEqual(self.sessions[-1].calls, [])

    async def test_rejects_empty_dns_before_http(self):
        self.responses = []
        self.dns.return_value = []
        with self.assertRaisesRegex(ValueError, "no DNS addresses"):
            await http_utils.public_request("https://public.example/")
        self.assertEqual(self.sessions[-1].calls, [])

    async def test_private_redirect_never_contacts_private_target(self):
        self.responses = [
            FakeResponse(302, headers={"Location": "http://127.0.0.1/private"})
        ]
        with self.assertRaises(ValueError):
            await http_utils.public_request("https://public.example/")
        self.assertEqual(len(self.sessions[-1].calls), 1)
        self.assertEqual(self.dns.await_count, 1)

    async def test_redirect_rechecks_dns_including_same_hostname(self):
        self.responses = [FakeResponse(302, headers={"Location": "/next"})]
        self.dns.side_effect = [dns_result("93.184.216.34"), dns_result("10.0.0.2")]
        with self.assertRaisesRegex(ValueError, "non-public"):
            await http_utils.public_request("https://public.example/")
        self.assertEqual(len(self.sessions[-1].calls), 1)
        self.assertEqual(self.dns.await_count, 2)

    async def test_redirect_validation_and_exact_allowlist(self):
        for location in (
            "https://user:pass@public.example/",
            "https://public.example:9000/",
            "file:///tmp/private",
            "https://public.example.evil.test/",
        ):
            with self.subTest(location=location):
                self.responses = [FakeResponse(302, headers={"Location": location})]
                with self.assertRaises(ValueError):
                    await http_utils.public_request(
                        "https://public.example/", allowed_hosts={"public.example"}
                    )
                self.assertEqual(len(self.sessions[-1].calls), 1)

    async def test_relative_redirect_returns_logical_final_url(self):
        self.responses = [
            FakeResponse(302, headers={"Location": "/new?q=1"}),
            FakeResponse(chunks=[b"done"]),
        ]
        result = await http_utils.public_request("https://public.example/old")
        self.assertEqual(result, (b"done", "https://public.example/new?q=1"))
        self.assertEqual(self.dns.await_count, 2)

    async def test_cross_origin_redirect_drops_credentials(self):
        self.responses = [
            FakeResponse(302, headers={"Location": "https://other.example/new"}),
            FakeResponse(chunks=[b"ok"]),
        ]
        await http_utils.public_request(
            "https://public.example/",
            headers={
                "Authorization": "secret",
                "Cookie": "secret",
                "User-Agent": "test",
            },
        )
        options = self.sessions[-1].calls[1][1]
        self.assertNotIn("Authorization", options["headers"])
        self.assertNotIn("Cookie", options["headers"])
        self.assertEqual(options["headers"]["Host"], "other.example")
        self.assertEqual(options["headers"]["User-Agent"], "test")

    async def test_redirects_are_bounded(self):
        self.responses = [
            FakeResponse(302, headers={"Location": "/loop"}) for _ in range(6)
        ]
        with self.assertRaisesRegex(RuntimeError, "Too many HTTP redirects"):
            await http_utils.public_request("https://public.example/loop")
        self.assertEqual(len(self.sessions[-1].calls), 6)

    async def test_content_length_rejects_without_reading(self):
        response = FakeResponse(content_length=5, chunks=[b"large"])
        self.responses = [response]
        with self.assertRaisesRegex(RuntimeError, "max_bytes"):
            await http_utils.public_request("https://public.example/", max_bytes=4)
        self.assertEqual(response.content.reads, 0)
        self.assertTrue(response.closed)

    async def test_stream_limit_counts_decompressed_bytes(self):
        # aiohttp's auto_decompress=True makes the yielded chunks decoded bytes;
        # the compressed Content-Length need not describe the decoded size.
        response = FakeResponse(
            content_length=2,
            headers={"Content-Encoding": "gzip"},
            chunks=[b"123", b"456", b"unread"],
        )
        self.responses = [response]
        with self.assertRaisesRegex(RuntimeError, "max_bytes"):
            await http_utils.public_request("https://public.example/", max_bytes=5)
        self.assertEqual(response.content.reads, 2)
        self.assertTrue(response.closed)

    async def test_exact_byte_limit_is_allowed(self):
        self.responses = [FakeResponse(content_length=5, chunks=[b"123", b"45"])]
        body, _url = await http_utils.public_request(
            "https://public.example/", max_bytes=5
        )
        self.assertEqual(body, b"12345")

    async def test_http_429_compatibility_even_with_large_error_body(self):
        self.responses = [FakeResponse(429, content_length=1000)]
        with self.assertRaises(RuntimeError) as raised:
            await http_utils.public_request("https://public.example/", max_bytes=4)
        self.assertTrue(
            str(raised.exception).startswith("HTTP 429 https://public.example/")
        )

    async def test_total_timeout_includes_dns(self):
        blocked = asyncio.Event()

        async def lookup(*_args, **_kwargs):
            await blocked.wait()

        self.responses = []
        self.dns.side_effect = lookup
        with self.assertRaises(asyncio.TimeoutError):
            await http_utils.public_request("https://public.example/", timeout=0.01)
        self.assertEqual(self.sessions[-1].calls, [])

    async def test_one_deadline_wraps_entire_redirect_chain(self):
        self.responses = [
            FakeResponse(302, headers={"Location": "/new"}),
            FakeResponse(chunks=[b"ok"]),
        ]
        with patch.object(
            http_utils.asyncio, "wait_for", wraps=asyncio.wait_for
        ) as deadline:
            await http_utils.public_request("https://public.example/", timeout=7)
        deadline.assert_called_once()
        assert deadline.call_args is not None
        self.assertEqual(deadline.call_args.kwargs["timeout"], 7)

    async def test_invalid_limits_and_url_do_not_create_http_session(self):
        for kwargs in ({"timeout": 0}, {"timeout": float("inf")}, {"max_bytes": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await http_utils.public_request(
                    "https://public.example/", **cast(dict[str, Any], kwargs)
                )
        with self.assertRaises(ValueError):
            await http_utils.public_request("https://user:secret@public.example/")
        self.assertEqual(self.sessions, [])


if __name__ == "__main__":
    unittest.main()
