"""Exercise request-body logging through real HTTPX clients without network I/O."""

from __future__ import annotations

import asyncio
import json
from itertools import count
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import httpx
import test_group_chat as fixtures
from openai import AsyncOpenAI

from astrbot.api import logger as api_logger
from astrbot.core.config.runtime import current_message_config
from juhkff_modules.group_chat_request_log import GroupChatRequestLogger


class GroupChatRequestLogTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Reuse real AstrBot event construction without starting the group store.
        self.events = fixtures.GroupChatTests()
        self.events.ids = count(1)
        self.event = self.events.event(addressed=True)
        self.event.set_extra("juhkff_group_owned", True)
        self.scope = SimpleNamespace(active=True, umo=self.event.unified_msg_origin)
        token = current_message_config.set(self.scope)
        self.addCleanup(current_message_config.reset, token)
        self.received = []
        self.original_requests = []
        self.original_responses = []

        async def original_request(request):
            self.original_requests.append(request)

        async def original_response(response):
            self.original_responses.append(response)

        self.original_request = original_request
        self.original_response = original_response
        self.client = httpx.AsyncClient(
            transport=httpx.MockTransport(self.receive),
            event_hooks={
                "request": [original_request],
                "response": [original_response],
            },
        )
        self.addAsyncCleanup(self.client.aclose)
        self.providers = [SimpleNamespace(_http_client=self.client)]
        self.module = SimpleNamespace(
            enabled=True,
            log_enabled=True,
            plugin=SimpleNamespace(
                context=SimpleNamespace(get_all_providers=lambda: self.providers)
            ),
        )
        self.request_logger = GroupChatRequestLogger(self.module)
        self.addCleanup(self.request_logger.close)
        info = patch.object(api_logger, "info")
        warning = patch.object(api_logger, "warning")
        self.info = info.start()
        self.warning = warning.start()
        self.addCleanup(info.stop)
        self.addCleanup(warning.stop)

    async def receive(self, request):
        self.received.append(request)
        return httpx.Response(200, json={"ok": True})

    @staticmethod
    def rendered(call):
        # One INFO call carries the entire readable, multiline request block.
        return " ".join(str(arg) for arg in call.args)

    def entries(self):
        prefix = "[juhkff] LLM request body:\n"
        blocks = [self.rendered(call) for call in self.info.call_args_list]
        return [block for block in blocks if block.startswith(prefix)]

    def assert_metadata(self, entry, event=None, path="/v1/chat/completions"):
        event = event or self.event
        message_id = str(event.message_obj.message_id or "")
        self.assertTrue(
            entry.startswith(
                "[juhkff] LLM request body:\n"
                f"session: {event.unified_msg_origin}\n"
                f"message_id: {message_id}\n"
                f"path: {path}\n"
            )
        )
        # Conversation content must not be duplicated as the original JSON body.
        self.assertNotIn('"messages":', entry)
        self.assertNotIn('"body":', entry)

    def assert_role_order(self, entry, roles):
        cursor = 0
        for role in roles:
            marker = f"\n{role}："
            position = entry.find(marker, cursor)
            self.assertGreaterEqual(
                position, cursor, f"Missing or out-of-order {role} in {entry}"
            )
            cursor = position + len(marker)

    async def post(self, body=None):
        return await self.client.post(
            "https://url-user:url-password@offline.invalid/v1/chat/completions?api_key=query-secret",
            json=(
                body
                if body is not None
                else {"messages": [{"role": "user", "content": "hello"}]}
            ),
            headers={
                "Authorization": "Bearer header-secret",
                "X-Private": "private-header",
            },
        )

    async def test_logs_readable_roles_and_tool_followup_without_changing_wire(self):
        payload = "QUJD" * 6000 + "=="
        body = {
            "model": "offline-model",
            "stream": True,
            "messages": [
                {"role": "system", "content": "系统要求\n保留换行"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "原始文本\nsecond line"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64," + payload},
                        },
                    ],
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "parameters": {
                            "type": "object",
                            "properties": {"key": {"type": "string"}},
                        },
                    },
                }
            ],
            "tool_choice": "auto",
            "temperature": 0.37,
            "extra_vendor_option": {"nested": [1, None, False, "unchanged"]},
        }
        self.assertIsNone(self.request_logger.bind(self.event))
        await self.post(body)
        followup = {
            "model": "offline-model",
            "stream": False,
            "messages": body["messages"]
            + [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": '{"key":"value"}',
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call-1", "content": '{"result":42}'},
            ],
            "tools": body["tools"],
        }
        await self.post(followup)
        entries = self.entries()
        self.assertEqual(len(entries), 2)
        self.assertEqual(self.info.call_count, 2)
        for entry, received, expected in zip(entries, self.received, (body, followup)):
            self.assertEqual(json.loads(received.content), expected)
            self.assert_metadata(entry)
            self.assertIn("\nmodel: offline-model\n", entry)
            self.assertIn("\nparameters: ", entry)
            self.assertIn("\ntools: [\n", entry)
            self.assertIn("\n\nsystem：系统要求\n保留换行", entry)
            self.assertIn("\nuser：原始文本\nsecond line", entry)
            self.assertIn(
                f"[image: image/png, base64 omitted ({len(payload)} chars)]", entry
            )
        self.assertRegex(entries[0], r'"temperature"\s*:\s*0\.37')
        self.assertRegex(entries[0], r'"extra_vendor_option"\s*:\s*\{')
        self.assertRegex(entries[0], r'"tool_choice"\s*:\s*"auto"')
        self.assertIn("\nassistant：", entries[1])
        self.assertIn("lookup", entries[1])
        self.assertIn("call-1", entries[1])
        self.assertIn("arguments", entries[1])
        self.assertIn("key", entries[1])
        self.assertIn("value", entries[1])
        self.assertIn('\ntool：{"result":42}', entries[1])
        self.assert_role_order(entries[0], ("system", "user"))
        self.assert_role_order(entries[1], ("system", "user", "assistant", "tool"))
        logged = "\n".join(self.rendered(call) for call in self.info.call_args_list)
        for secret in (
            "url-user",
            "url-password",
            "query-secret",
            "header-secret",
            "private-header",
            "Authorization",
            "X-Private",
        ):
            self.assertNotIn(secret, logged)
        self.assertNotIn(payload, logged)
        self.assertNotIn(payload[:100], logged)
        self.warning.assert_not_called()

    async def test_openai_sdk_logs_final_serialized_body_with_extra_parameters(self):
        async def respond(request):
            self.received.append(request)
            return httpx.Response(
                200,
                json={
                    "id": "offline-completion",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "offline-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "done"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

        sdk_http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        self.addAsyncCleanup(sdk_http.aclose)
        sdk = AsyncOpenAI(
            api_key="sdk-key-secret",
            base_url="https://offline.invalid/v1",
            http_client=sdk_http,
            max_retries=0,
        )
        self.providers[:] = [SimpleNamespace(client=sdk)]
        self.request_logger.bind(self.event)
        completion = await sdk.chat.completions.create(
            model="offline-model",
            messages=[{"role": "user", "content": "SDK request"}],
            stream=False,
            temperature=0.25,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            extra_body={"vendor_option": {"reasoning": True, "limits": [2, 8]}},
        )
        self.assertEqual(completion.choices[0].message.content, "done")
        self.assertEqual(len(self.received), 1)
        entries = self.entries()
        self.assertEqual(len(entries), 1)
        wire_body = json.loads(self.received[0].content)
        self.assert_metadata(entries[0])
        self.assertIn("\nmodel: offline-model\n", entries[0])
        self.assertIn("\n\nuser：SDK request", entries[0])
        self.assertRegex(entries[0], r'"vendor_option"\s*:\s*\{')
        self.assertRegex(entries[0], r'"reasoning"\s*:\s*true')
        self.assertRegex(entries[0], r'"limits"\s*:\s*\[\s*2,\s*8\s*\]')
        self.assertEqual(wire_body["model"], "offline-model")
        self.assertEqual(
            wire_body["messages"], [{"role": "user", "content": "SDK request"}]
        )
        self.assertEqual(wire_body["temperature"], 0.25)
        self.assertEqual(
            wire_body["vendor_option"], {"reasoning": True, "limits": [2, 8]}
        )
        self.assertNotIn("extra_body", wire_body)
        self.assertFalse(wire_body["stream"])
        self.assertEqual(wire_body["tools"][0]["function"]["name"], "lookup")
        self.assertIn("\ntools: [\n", entries[0])
        self.assertIn("lookup", entries[0])
        self.assertNotIn("sdk-key-secret", self.rendered(self.info.call_args))

    async def test_multiple_images_omit_only_base64_in_logs(self):
        png = "QUJD" * 6000 + "=="
        jpeg = "ZGVm" * 7000 + "=="
        image_url = "https://images.example.invalid/photo.png?size=large"
        body = {
            "model": "vision-model",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "比较这些图片\n并保留文本"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64," + png},
                        },
                        {"type": "image_url", "image_url": {"url": image_url}},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/jpeg;base64," + jpeg},
                        },
                    ],
                }
            ],
        }
        self.request_logger.bind(self.event)
        await self.post(body)
        self.assertEqual(json.loads(self.received[0].content), body)
        self.assertIn(png.encode(), self.received[0].content)
        self.assertIn(jpeg.encode(), self.received[0].content)
        self.assertEqual(self.info.call_count, 1)
        (entry,) = self.entries()
        self.assert_metadata(entry)
        self.assertIn("\n\nuser：比较这些图片\n并保留文本", entry)
        for mime, payload in (("image/png", png), ("image/jpeg", jpeg)):
            self.assertIn(
                f"[image: {mime}, base64 omitted ({len(payload)} chars)]", entry
            )
            self.assertNotIn(payload[:100], entry)
        self.assertIn(image_url, entry)
        self.assertLess(len(entry), 2000)
        self.warning.assert_not_called()

    async def test_gemini_system_instruction_contents_and_inline_image(self):
        payload = "R0lG" * 2000
        body = {
            "systemInstruction": {"parts": [{"text": "Gemini system\nnext line"}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": "Gemini question"},
                        {"inlineData": {"mimeType": "image/webp", "data": payload}},
                    ],
                },
                {"role": "model", "parts": [{"text": "Gemini previous answer"}]},
            ],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 120},
        }
        path = "/v1beta/models/gemini-offline:generateContent"
        self.request_logger.bind(self.event)
        await self.client.post("https://offline.invalid" + path, json=body)
        self.assertEqual(json.loads(self.received[0].content), body)
        self.assertEqual(self.info.call_count, 1)
        (entry,) = self.entries()
        self.assert_metadata(entry, path=path)
        self.assertIn("\n\nsystem：Gemini system\nnext line", entry)
        self.assertIn("\nuser：Gemini question", entry)
        self.assertIn("\nassistant：Gemini previous answer", entry)
        self.assert_role_order(entry, ("system", "user", "assistant"))
        self.assertNotIn("\nmodel：", entry)
        self.assertIn(
            f"[image: image/webp, base64 omitted ({len(payload)} chars)]", entry
        )
        self.assertNotIn(payload[:100], entry)
        self.assertRegex(entry, r'"generationConfig"\s*:\s*\{')
        self.assertRegex(entry, r'"maxOutputTokens"\s*:\s*120')
        self.assertNotIn('"contents":', entry)
        self.assertNotIn('"systemInstruction":', entry)
        self.warning.assert_not_called()

    async def test_anthropic_system_messages_and_source_base64_image(self):
        payload = "YWJj" * 2000
        body = {
            "model": "claude-offline",
            "system": [{"type": "text", "text": "Anthropic system\nnext line"}],
            "max_tokens": 256,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Anthropic question"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": payload,
                            },
                        },
                    ],
                },
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Previous answer"}],
                },
            ],
        }
        self.request_logger.bind(self.event)
        await self.client.post("https://offline.invalid/v1/messages", json=body)
        self.assertEqual(json.loads(self.received[0].content), body)
        self.assertEqual(self.info.call_count, 1)
        (entry,) = self.entries()
        self.assert_metadata(entry, path="/v1/messages")
        self.assertIn("\nmodel: claude-offline\n", entry)
        self.assertIn("\n\nsystem：Anthropic system\nnext line", entry)
        self.assertIn("\nuser：Anthropic question", entry)
        self.assertIn("\nassistant：Previous answer", entry)
        self.assert_role_order(entry, ("system", "user", "assistant"))
        self.assertIn(
            f"[image: image/jpeg, base64 omitted ({len(payload)} chars)]", entry
        )
        self.assertNotIn(payload[:100], entry)
        self.assertRegex(entry, r'"max_tokens"\s*:\s*256')
        self.assertNotIn('"system":', entry)
        self.warning.assert_not_called()

    async def test_responses_instructions_messages_and_function_followup(self):
        body = {
            "model": "responses-offline",
            "instructions": "Responses system\nnext line",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Responses question\nsecond line",
                        },
                    ],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "Checking the tool"},
                    ],
                },
                {
                    "type": "function_call",
                    "name": "lookup_response",
                    "call_id": "resp-call-7",
                    "id": "fc-7",
                    "arguments": '{"query":"weather"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "resp-call-7",
                    "output": '{"degrees":21}',
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "lookup_response",
                    "parameters": {"type": "object"},
                }
            ],
            "parallel_tool_calls": False,
            "reasoning": {"effort": "low"},
        }
        self.request_logger.bind(self.event)
        await self.client.post("https://offline.invalid/v1/responses", json=body)
        self.assertEqual(json.loads(self.received[0].content), body)
        self.assertEqual(self.info.call_count, 1)
        (entry,) = self.entries()
        self.assert_metadata(entry, path="/v1/responses")
        self.assertIn("\nmodel: responses-offline\n", entry)
        self.assertIn("\n\nsystem：Responses system\nnext line", entry)
        self.assertIn("\nuser：Responses question\nsecond line", entry)
        self.assertIn("\nassistant：Checking the tool", entry)
        self.assertIn("lookup_response", entry)
        self.assertIn("resp-call-7", entry)
        self.assertIn("fc-7", entry)
        self.assertIn("arguments", entry)
        self.assertIn("weather", entry)
        self.assertIn('\ntool：{"degrees":21}', entry)
        self.assert_role_order(
            entry, ("system", "user", "assistant", "assistant", "tool")
        )
        self.assertIn("\ntools: [\n", entry)
        self.assertRegex(entry, r'"parallel_tool_calls"\s*:\s*false')
        self.assertRegex(entry, r'"effort"\s*:\s*"low"')
        self.assertNotIn('"instructions":', entry)
        self.assertNotIn('"input":', entry)
        self.warning.assert_not_called()

    async def test_accepts_all_supported_provider_client_locations(self):
        for provider in (
            SimpleNamespace(_http_client=self.client),
            SimpleNamespace(client=SimpleNamespace(_client=self.client)),
            SimpleNamespace(client=self.client),
        ):
            with self.subTest(provider=provider):
                self.request_logger.close()
                self.providers[:] = [provider]
                self.request_logger = GroupChatRequestLogger(self.module)
                self.addCleanup(self.request_logger.close)
                self.info.reset_mock()
                self.request_logger.bind(self.event)
                await self.post()
                self.assertEqual(len(self.entries()), 1)

    async def test_logging_and_module_switches_gate_binding_and_sending(self):
        for enabled, log_enabled in ((True, False), (False, True), (False, False)):
            with self.subTest(enabled=enabled, log_enabled=log_enabled):
                self.module.enabled = enabled
                self.module.log_enabled = log_enabled
                self.request_logger.bind(self.event)
                await self.post()
                self.info.assert_not_called()
        self.module.enabled = self.module.log_enabled = True
        self.request_logger.bind(self.event)
        await self.post()
        self.assertEqual(len(self.entries()), 1)
        self.info.reset_mock()
        for setting in ("enabled", "log_enabled"):
            with self.subTest(disabled_after_binding=setting):
                setattr(self.module, setting, False)
                await self.post()
                self.info.assert_not_called()
                setattr(self.module, setting, True)

    async def test_nonowned_missing_inactive_or_wrong_scope_does_not_bind(self):
        self.event.set_extra("juhkff_group_owned", False)
        self.request_logger.bind(self.event)
        await self.post()
        self.info.assert_not_called()
        self.event.set_extra("juhkff_group_owned", True)
        for scope in (
            None,
            SimpleNamespace(active=False, umo=self.scope.umo),
            SimpleNamespace(active=True, umo="other-group"),
        ):
            with self.subTest(scope=scope):
                token = current_message_config.set(scope)
                try:
                    self.request_logger.bind(self.event)
                    await self.post()
                    self.info.assert_not_called()
                finally:
                    current_message_config.reset(token)

    async def test_revoked_ownership_stops_logging_after_binding(self):
        self.request_logger.bind(self.event)
        self.event.set_extra("juhkff_group_owned", False)
        await self.post()
        self.info.assert_not_called()
        self.assertEqual(len(self.received), 1)

    async def test_scope_identity_and_lifetime_isolate_bound_requests(self):
        self.request_logger.bind(self.event)
        for scope in (
            None,
            SimpleNamespace(active=True, umo="other-group"),
            SimpleNamespace(active=True, umo=self.scope.umo),
        ):
            with self.subTest(scope=scope):
                token = current_message_config.set(scope)
                try:
                    await self.post()
                    self.info.assert_not_called()
                finally:
                    current_message_config.reset(token)
        self.scope.active = False
        await self.post()
        self.info.assert_not_called()
        self.scope.active = True
        await self.post()
        self.assertEqual(len(self.entries()), 1)

    async def test_concurrent_owned_scopes_keep_their_own_message_metadata(self):
        async def send(event, marker):
            event.set_extra("juhkff_group_owned", True)
            scope = SimpleNamespace(active=True, umo=event.unified_msg_origin)
            token = current_message_config.set(scope)
            try:
                self.request_logger.bind(event)
                await asyncio.sleep(0)
                await self.post({"marker": marker})
            finally:
                current_message_config.reset(token)

        other = self.events.event(group="group-b")
        await asyncio.gather(send(self.event, "first"), send(other, "second"))
        entries = self.entries()
        self.assertEqual(len(entries), 2)
        self.assertEqual(self.info.call_count, 2)
        for marker, event in (("first", self.event), ("second", other)):
            matching = [entry for entry in entries if f'"{marker}"' in entry]
            self.assertEqual(len(matching), 1)
            self.assert_metadata(matching[0], event)
            self.assertRegex(matching[0], rf'"marker"\s*:\s*"{marker}"')

    async def test_install_is_idempotent_and_close_preserves_original_hooks(self):
        self.providers[:] = [
            SimpleNamespace(_http_client=self.client),
            SimpleNamespace(client=SimpleNamespace(_client=self.client)),
        ]
        for _ in range(3):
            self.request_logger.bind(self.event)
        self.assertEqual(len(self.client.event_hooks["request"]), 2)
        await self.post()
        self.assertEqual(len(self.entries()), 1)
        self.assertEqual(len(self.original_requests), 1)
        self.assertEqual(len(self.original_responses), 1)
        self.assertIsNone(self.request_logger.close())
        self.request_logger.close()
        self.assertEqual(self.client.event_hooks["request"], [self.original_request])
        self.assertEqual(self.client.event_hooks["response"], [self.original_response])
        self.info.reset_mock()
        await self.post()
        self.info.assert_not_called()
        self.assertEqual(len(self.original_requests), 2)
        self.assertEqual(len(self.original_responses), 2)

    async def test_invalid_json_warns_without_blocking_transport(self):
        self.request_logger.bind(self.event)
        response = await self.client.post(
            "https://offline.invalid/v1/chat/completions",
            content=b'{"incomplete":',
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.received[0].content, b'{"incomplete":')
        self.assertEqual(self.entries(), [])
        self.warning.assert_called_once()

    async def test_non_json_and_non_post_requests_are_ignored(self):
        self.request_logger.bind(self.event)
        await self.client.get("https://offline.invalid/models")
        await self.client.put("https://offline.invalid/config", json={"value": 1})
        await self.client.post(
            "https://offline.invalid/upload",
            content=b"plain text",
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(len(self.received), 3)
        self.info.assert_not_called()
        self.warning.assert_not_called()

    async def test_missing_message_id_is_allowed(self):
        self.event.message_obj.message_id = None
        self.request_logger.bind(self.event)
        await self.post()
        entries = self.entries()
        self.assertEqual(len(entries), 1)
        self.assert_metadata(entries[0])
        self.assertIn("\nmessage_id: \n", entries[0])

    async def test_unsupported_clients_are_skipped_without_side_effects(self):
        self.providers[:] = [
            SimpleNamespace(),
            SimpleNamespace(client=object()),
            SimpleNamespace(_http_client=object()),
            SimpleNamespace(client=SimpleNamespace(_client=object())),
        ]
        self.request_logger.bind(self.event)
        self.assertEqual(self.client.event_hooks["request"], [self.original_request])
        await self.post()
        self.info.assert_not_called()
