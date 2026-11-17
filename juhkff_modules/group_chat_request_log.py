"""Log serialized model JSON requests within an owned group message scope."""

from __future__ import annotations

import json
from contextvars import ContextVar

from astrbot.core.config.runtime import current_message_config

from .module_logging import ModuleLogger

_binding = ContextVar("juhkff_group_request_log", default=None)


class GroupChatRequestLogger:
    """Observe provider HTTP clients without replacing their request methods."""

    def __init__(self, module):
        self.module = module
        self.log = ModuleLogger(module)
        self.clients = []
        self.hook = self._log_request

    def bind(self, event):
        """Install hooks and bind logging to this message's configuration scope.

        Args:
            event: Owned incoming group message.
        """
        scope = current_message_config.get()
        if not (
            self.module.enabled
            and self.module.log_enabled
            and event.get_extra("juhkff_group_owned")
            and scope
            and scope.active
            and scope.umo == event.unified_msg_origin
        ):
            return
        _binding.set((self, scope, event))
        get_providers = getattr(self.module.plugin.context, "get_all_providers", None)
        if not callable(get_providers):
            return
        for provider in get_providers():
            sdk = getattr(provider, "client", None)
            for client in (
                getattr(provider, "_http_client", None),
                getattr(sdk, "_client", None),
                sdk,
            ):
                hooks = getattr(client, "event_hooks", None)
                if not isinstance(hooks, dict):
                    continue
                if any(existing is client for existing in self.clients):
                    continue
                hooks.setdefault("request", []).append(self.hook)
                self.clients.append(client)

    async def _log_request(self, request):
        """Observe an already serialized JSON body without changing the request.

        Args:
            request: HTTPX request immediately before transport dispatch.
        """
        binding = _binding.get()
        scope = current_message_config.get()
        if not (
            binding
            and binding[0] is self
            and binding[1] is scope
            and scope.active
            and self.module.enabled
            and self.module.log_enabled
            and binding[2].get_extra("juhkff_group_owned")
            and binding[2].unified_msg_origin == scope.umo
        ):
            return
        content_type = request.headers.get("content-type", "").split(";", 1)[0]
        if request.method != "POST" or not (
            content_type == "application/json" or content_type.endswith("+json")
        ):
            return
        try:
            body = json.loads(request.content)
            event = binding[2]
            message_id = (
                getattr(getattr(event, "message_obj", None), "message_id", "") or ""
            )
            lines = [
                "[juhkff] LLM request body:",
                f"session: {scope.umo}",
                f"message_id: {message_id}",
                f"path: {request.url.path}",
                self._format_body(body),
            ]
            self.log.info("\n".join(lines))
        except Exception:
            # Diagnostic logging must never prevent a model request from being sent.
            self.log.warning("[juhkff] Failed to log LLM JSON request body")

    @staticmethod
    def _format_content(value):
        """Render multimodal content while keeping binary payloads out of the log.

        Args:
            value: A provider's message content or content block.

        Returns:
            Readable text preserving content block order.
        """
        if value is None:
            return ""
        if isinstance(value, str):
            if value.startswith("data:") and ";base64," in value:
                header, data = value.split(",", 1)
                return f"[image: {header[5:].split(';')[0]}, base64 omitted ({len(data)} chars)]"
            if value.startswith("base64://"):
                return f"[image: base64 omitted ({len(value) - 9} chars)]"
            return value
        if isinstance(value, list):
            return "\n".join(
                GroupChatRequestLogger._format_content(item) for item in value
            )
        if isinstance(value, dict):
            kind = value.get("type", "")
            if "text" in value and kind in ("", "text", "input_text", "output_text"):
                return str(value["text"])
            if kind in ("image_url", "input_image"):
                ref = value.get("image_url", "")
                if isinstance(ref, dict):
                    ref = ref.get("url", "")
                rendered = GroupChatRequestLogger._format_content(ref)
                return (
                    rendered
                    if rendered.startswith("[image:")
                    else f"[image: {rendered}]"
                )
            image = value.get("inlineData", value.get("inline_data"))
            if kind == "image":
                image = value.get("source", {})
                if image.get("type") == "url":
                    return f"[image: {image.get('url', '')}]"
            if isinstance(image, dict):
                mime = image.get(
                    "mimeType", image.get("mime_type", image.get("media_type", "image"))
                )
                return f"[image: {mime}, base64 omitted ({len(image.get('data', ''))} chars)]"
            if kind == "input_audio":
                audio = value.get("input_audio", {})
                return f"[audio: {audio.get('format', '')}, base64 omitted ({len(audio.get('data', ''))} chars)]"
        return json.dumps(value, ensure_ascii=False, indent=2)

    @classmethod
    def _format_body(cls, body):
        """Display provider messages as role-labelled paragraphs.

        Args:
            body: Decoded outgoing JSON body, used only for display.

        Returns:
            Multiline model parameters, messages and tool definitions.
        """
        if not isinstance(body, dict):
            return cls._format_content(body)
        lines = []
        if "model" in body:
            lines.append(f"model: {body['model']}")
        content_keys = {
            "model",
            "messages",
            "system",
            "systemInstruction",
            "system_instruction",
            "instructions",
            "input",
            "contents",
            "tools",
        }
        parameters = {
            key: value for key, value in body.items() if key not in content_keys
        }
        if parameters:
            lines.append(
                "parameters: " + json.dumps(parameters, ensure_ascii=False, indent=2)
            )
        for key in (
            "system",
            "instructions",
            "systemInstruction",
            "system_instruction",
        ):
            if body.get(key):
                value = body[key]
                if isinstance(value, dict) and "parts" in value:
                    value = value["parts"]
                lines.append("\nsystem：" + cls._format_content(value))
        messages = body.get("messages", body.get("contents", body.get("input", [])))
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        if isinstance(messages, dict):
            messages = [messages]
        for message in messages or []:
            if not isinstance(message, dict):
                lines.append("\nuser：" + cls._format_content(message))
                continue
            kind = message.get("type", "")
            role = message.get("role", "user")
            if role == "model" or kind == "function_call":
                role = "assistant"
            elif kind == "function_call_output":
                role = "tool"
            content = message.get(
                "content", message.get("parts", message.get("output"))
            )
            text = cls._format_content(content)
            # Keep tool IDs and arguments so subsequent tool turns remain traceable.
            extras = {
                key: value
                for key, value in message.items()
                if key not in {"role", "content", "parts", "output", "type"}
            }
            if extras:
                text += ("\n" if text else "") + json.dumps(
                    extras, ensure_ascii=False, indent=2
                )
            lines.append(f"\n{role}：{text}")
        if body.get("tools"):
            lines.append(
                "\ntools: " + json.dumps(body["tools"], ensure_ascii=False, indent=2)
            )
        return "\n".join(lines)

    def close(self):
        """Remove only this instance's hooks when the plugin unloads."""
        for client in self.clients:
            hooks = client.event_hooks.get("request", [])
            hooks[:] = [hook for hook in hooks if hook is not self.hook]
        self.clients.clear()
        binding = _binding.get()
        if binding and binding[0] is self:
            _binding.set(None)
