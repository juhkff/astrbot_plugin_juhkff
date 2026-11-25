"""juhkff 综合插件：主动群聊 / 卡片解析 / 偷表情 / 日报 / Pixiv 订阅 / 帮助图。

移植自 Yunzai 插件 juhkff-plugin（https://github.com/juhkff/juhkff-plugin）。

本文件只负责：注册插件、生命周期、事件分发、状态指令。
具体功能都在 juhkff_modules/ 下，每个模块可独立开关。

阅读顺序建议：
1. `JuhkffPlugin.__init__` —— 模块注册表；
2. `_dispatch` —— 一条消息如何分发给各模块；
3. `juhkff_modules/common.py` —— 配置、定时器、模块基类；
4. 各功能模块（share_parse / emoji_steal / daily_report /
   pixiv_push / help_gen）。
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections.abc import AsyncGenerator
from copy import copy
from sys import maxsize
from typing import cast

# 让 `juhkff_modules` 无论被当作包还是普通模块加载都能导入。
# （AstrBot 加载插件时用 importlib 按文件路径导入 main.py，包名可能不是插件目录名）
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# AstrBot 的「重载插件」只会清 `data.plugins.<插件目录>.*` 的模块缓存；本插件把
# `juhkff_modules` 注册成了顶层包，不在它的清理范围内，不主动清就会一直用旧代码。
# 这里按文件路径确认归属，只丢本目录下的子模块，重载时才会重新读磁盘。
for _name, _module in list(sys.modules.items()):
    if _name != "juhkff_modules" and not _name.startswith("juhkff_modules."):
        continue
    if (getattr(_module, "__file__", "") or "").startswith(_HERE):
        del sys.modules[_name]

from juhkff_modules import common  # noqa: E402
from juhkff_modules.daily_report import DailyReportModule  # noqa: E402
from juhkff_modules.emoji_steal import EmojiStealModule  # noqa: E402
from juhkff_modules.group_chat import GroupChatModule  # noqa: E402
from juhkff_modules.help_gen import HelpGenModule  # noqa: E402
from juhkff_modules.module_logging import ModuleLogger  # noqa: E402
from juhkff_modules.pixiv_push import PixivPushModule  # noqa: E402
from juhkff_modules.renderer import renderer as html_renderer  # noqa: E402
from juhkff_modules.share_parse import ShareParseModule  # noqa: E402

from astrbot.api import AstrBotConfig  # noqa: E402
from astrbot.api.event import (  # noqa: E402
    AstrMessageEvent,
    MessageEventResult,
    filter,
)
from astrbot.api.platform import MessageType  # noqa: E402
from astrbot.api.star import Context, Star, register  # noqa: E402
from astrbot.core.config.runtime import current_message_config  # noqa: E402
from astrbot.core.pipeline.process_stage.method.agent_request import (  # noqa: E402
    AgentRequestSubStage,
)

# 版本号：开发期间固定为初代 0.1.0，不随功能变化调整。
VERSION = "0.1.0"
_PIXIV_FAILURE_NOTICE_TIMEOUT = 10.0
logger = ModuleLogger()


@register(
    "astrbot_plugin_juhkff",
    "juhkff",
    "综合插件：主动群聊 / 卡片解析 / 偷表情 / 日报 / Pixiv 订阅 / 帮助图",
    VERSION,
)
class JuhkffPlugin(Star):
    """插件入口：持有配置、模块表、定时器和数据目录。

    所有功能模块都通过 `plugin.config` 读自己的配置分组（`_conf_schema.json`
    里同名 object），通过 `plugin.context` 用 AstrBot 的上下文能力。
    """

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config: AstrBotConfig = config
        # 供 help_gen 等模块显示版本号（模块拿不到模块级的 VERSION 常量）
        self.version = VERSION
        self.data_dir = common.data_dir()
        # 定时器：薄封装 AstrBot 的 CronJobManager（APScheduler）
        self.scheduler = common.Scheduler(context, common.PLUGIN_NAME)
        # 模块注册表：key 与 `_conf_schema.json` 的分组名一一对应。
        # 顺序 = 消息分发顺序；具体某个模块是否参与由它自己的 enable 决定。
        self.modules = {
            mod.key: mod
            for mod in (
                ShareParseModule(self),
                EmojiStealModule(self),
                DailyReportModule(self),
                PixivPushModule(self),
                HelpGenModule(self),
                GroupChatModule(self),
            )
        }
        self._started = False

    # ------------------------------------------------------------ 配置

    @property
    def global_enabled(self) -> bool:
        """总开关：关掉后所有模块都不工作（配置页 `global.enabled`）。"""
        return bool((self.config.get("global") or {}).get("enabled", True))

    # ------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        """Start enabled modules on every plugin load, including hot reloads."""
        await self._ensure_started()

    @filter.on_astrbot_loaded()
    async def _on_astrbot_loaded(self) -> None:
        """Ensure startup without duplicating jobs registered by initialize."""
        await self._ensure_started()

    async def _ensure_started(self) -> None:
        """Start enabled modules once, isolating each module's startup failure.

        AstrBot calls initialize on both initial loading and hot reloads, with
        the database and cron manager already available. Module startup does
        not require initialized providers or platform adapters; delivery accesses
        them later. The loaded hook and message handler remain
        idempotent fallbacks and never register a second set of jobs.
        """
        if self._started:
            return
        self._started = True

        for mod in self.modules.values():
            if not mod.enabled:
                continue
            with ModuleLogger(mod).scope():
                try:
                    await mod.start()
                    logger.info("[juhkff] Module %s started", mod.key)
                except Exception:
                    logger.exception("[juhkff] Module %s failed to start", mod.key)

    async def terminate(self) -> None:
        """Stop modules and release jobs and browser resources on every exit path."""
        try:
            for mod in self.modules.values():
                with ModuleLogger(mod).scope():
                    try:
                        await mod.stop()
                        logger.info("[juhkff] Module %s stopped", mod.key)
                    except Exception:
                        logger.exception("[juhkff] Module %s failed to stop", mod.key)
        finally:
            try:
                await self.scheduler.shutdown()
            finally:
                # Keep the bound renderer reachable after a module-cache reload.
                owner = next(
                    (
                        mod
                        for key, mod in self.modules.items()
                        if key in ("help", "daily_report")
                        and getattr(mod, "enabled", False)
                        and getattr(mod, "log_enabled", False)
                    ),
                    None,
                )
                with ModuleLogger(owner).scope():
                    try:
                        await html_renderer.close()
                    except Exception:
                        logger.exception("[juhkff] Failed to close the local renderer")

    # ------------------------------------------------------------ 消息分发

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _dispatch(self, event: AstrMessageEvent) -> AsyncGenerator[None, None]:
        """Route commands before passive handlers.

        Args:
            event: Incoming message after AstrBot's wake and command checks.

        Yields:
            Control to AstrBot when no module consumes the message.
        """
        if not self.global_enabled or event.is_stopped():
            return
        await self._ensure_started()
        # The framework populates this map before invoking any handlers, even
        # for commands without arguments. Leave those commands to their owner.
        if event.get_extra("handlers_parsed_params"):
            return
        enabled = [mod for mod in self.modules.values() if mod.enabled]
        exclusive = [mod for mod in enabled if mod.intercepts(event)]
        for mod in exclusive or enabled:
            if not mod.enabled:
                continue
            with ModuleLogger(mod).scope():
                if mod.key == "pixiv_push" and exclusive:
                    logger.info(
                        "[juhkff] Pixiv command accepted: origin=%s",
                        event.unified_msg_origin,
                    )
                try:
                    chain = await mod.handle(event)
                except Exception:
                    logger.exception(
                        f"[juhkff] Module {mod.key} failed to handle a message"
                    )
                    if exclusive or event.is_stopped():
                        event.stop_event()
                        return
                    continue
                if chain is None:
                    # A consumed command must not fall through to another responder.
                    if exclusive or event.is_stopped():
                        event.stop_event()
                        return
                    continue
                try:
                    if mod.key == "pixiv_push":
                        logger.info(
                            "[juhkff] Pixiv reply sending: origin=%s",
                            event.unified_msg_origin,
                        )
                    await event.send(chain)
                    if mod.key == "pixiv_push":
                        logger.info(
                            "[juhkff] Pixiv reply sent: origin=%s",
                            event.unified_msg_origin,
                        )
                except Exception as exc:
                    logger.exception(
                        f"[juhkff] Module {mod.key} failed to send a message"
                    )
                    if mod.key == "pixiv_push" and mod.enabled and self.global_enabled:
                        # A missing acknowledgement can still arrive late. Report
                        # the uncertainty without retrying or changing the images.
                        timed_out = isinstance(exc, TimeoutError) or (
                            "timeout" in str(exc).casefold()
                        )
                        notice = (
                            "Pixiv 消息发送超时，尚未确认送达，请先检查群内是否已收到。"
                            if timed_out
                            else "Pixiv 消息发送失败，请检查 QQ 协议端日志。"
                        )
                        try:
                            await asyncio.wait_for(
                                event.send(event.plain_result(notice)),
                                timeout=_PIXIV_FAILURE_NOTICE_TIMEOUT,
                            )
                        except Exception:
                            logger.exception(
                                "[juhkff] Pixiv send failure notice could not be sent"
                            )
                event.stop_event()
                return
        yield

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=maxsize)
    async def _prepare_group_chat(self, event: AstrMessageEvent):
        """Claim group admission before the built-in empty-mention handler.

        Args:
            event: Incoming group event.
        """
        module = cast(GroupChatModule, self.modules["group_chat"])
        with ModuleLogger(module).scope():
            await module.prepare(event)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=-maxsize)
    async def _admit_group_chat(self, event: AstrMessageEvent):
        """Leave consumed messages to other plugins, then admit an agent turn.

        Args:
            event: Incoming group event.
        """
        module = cast(GroupChatModule, self.modules["group_chat"])
        with ModuleLogger(module).scope():
            request = await module.admit(event)
        if event.get_extra("juhkff_group_runtime_config") and event.get_extra(
            "juhkff_group_admitted"
        ):
            # Initialize per event so Core's cached presentation options use this snapshot.
            # Suppress the outer default branch even when the model produces no output.
            event.should_call_llm(True)
            if request is not None:
                event.set_extra("provider_request", request)
            scope = current_message_config.get()
            if (
                scope is None
                or not scope.active
                or scope.umo != event.unified_msg_origin
            ):
                raise RuntimeError("Group runtime lost its message configuration scope")
            stage = AgentRequestSubStage()
            await stage.initialize(scope.context)
            async for _ in stage.process(event):
                yield
        elif request is not None:
            yield request

    @filter.on_llm_request()
    async def _group_chat_context(self, event, request):
        """Supplement official requests with the event's context snapshot.

        Args:
            event: Current group event.
            request: Official model request.
        """
        module = cast(GroupChatModule, self.modules["group_chat"])
        with ModuleLogger(module).scope():
            await module.decorate(event, request)

    @filter.on_llm_response()
    async def _group_chat_response(self, event, response):
        """Capture generated text for the Redis group timeline.

        Args:
            event: Current group event.
            response: Official model response.
        """
        module = cast(GroupChatModule, self.modules["group_chat"])
        with ModuleLogger(module).scope():
            await module.capture_response(event, response)

    @filter.after_message_sent()
    async def _group_chat_cleanup(self, event):
        """Clear context following successful official reset commands.

        Args:
            event: Sent event.
        """
        module = cast(GroupChatModule, self.modules["group_chat"])
        with ModuleLogger(module).scope():
            await module.after_sent(event)

    async def _execute_pixiv_tool(self, event: AstrMessageEvent, command: str) -> str:
        """Run one normalized browse request and send its reply to the source group.

        Args:
            event: Original event used only for admission and final delivery.
            command: Canonical browse command built by one of the three tools.

        Returns:
            A concise delivery result or the browser's exact failure message.
        """
        module = cast(PixivPushModule, self.modules["pixiv_push"])
        with ModuleLogger(module).scope():
            if not self.global_enabled or not module.enabled or module._stopped:
                return "Pixiv 功能已停用。"
            if event.is_stopped():
                return "当前消息已停止处理，未执行 Pixiv 请求。"
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return "Pixiv 浏览仅限群聊使用。"
            try:
                await self._ensure_started()
                if not self.global_enabled or not module.enabled or module._stopped:
                    return "Pixiv 功能已停用。"
                if event.is_stopped():
                    return "当前消息已停止处理，未执行 Pixiv 请求。"
                if event.get_message_type() != MessageType.GROUP_MESSAGE:
                    return "Pixiv 浏览仅限群聊使用。"
                # The browser reads command text; isolate it from the LLM's event.
                browse_event = copy(event)
                browse_event._extras = event._extras.copy()
                browse_event.message_str = command
                chain = await module.handle_browse(browse_event)
                if not self.global_enabled or not module.enabled or module._stopped:
                    return "Pixiv 功能已停用，未发送浏览结果。"
                if event.is_stopped():
                    return "当前消息已停止处理，未发送 Pixiv 浏览结果。"
                if event.get_message_type() != MessageType.GROUP_MESSAGE:
                    return "Pixiv 浏览仅限群聊使用。"
                if chain is None or not chain.chain:
                    return "Pixiv 未返回可发送的浏览结果。"
                # Browser failures use plain_result; successful URL-only artwork
                # replies remain MessageChain and must count as successful browsing.
                failure = (
                    chain.get_plain_text()
                    if isinstance(chain, MessageEventResult)
                    else None
                )
            except Exception:
                logger.exception("[juhkff] Pixiv tool browse request failed")
                return "Pixiv 请求或图片下载失败，请稍后重试。"
            try:
                sent = await event.send(chain)
                if sent is False:
                    logger.warning("[juhkff] Pixiv tool delivery returned False")
                    return "Pixiv 消息发送失败，未确认送达。"
            except Exception as exc:
                logger.exception("[juhkff] Pixiv tool delivery failed")
                if isinstance(exc, TimeoutError) or "timeout" in str(exc).casefold():
                    return "Pixiv 消息发送超时，尚未确认送达，请先检查群内是否已收到。"
                return "Pixiv 消息发送失败，请检查 QQ 协议端日志。"
            return failure or "Pixiv 浏览结果已发送到当前群聊。"

    @filter.llm_tool(name="juhkff_pixiv_ranking")
    async def juhkff_pixiv_ranking(
        self, event: AstrMessageEvent, mode: str = "daily"
    ) -> str:
        """Send Pixiv's daily, weekly, or monthly artwork ranking to this group.

        Use when the user asks to browse a Pixiv ranking. The group's configured
        artwork content policy and display settings apply.

        Args:
            mode (string): Ranking period: daily, weekly, or monthly. Defaults to daily.
        """
        commands = {
            "daily": "#pixiv日榜",
            "weekly": "#pixiv周榜",
            "monthly": "#pixiv月榜",
        }
        if not isinstance(mode, str) or mode.strip().lower() not in commands:
            return "榜单类型无效，请使用 daily、weekly 或 monthly。"
        return await self._execute_pixiv_tool(event, commands[mode.strip().lower()])

    @filter.llm_tool(name="juhkff_pixiv_random")
    async def juhkff_pixiv_random(
        self, event: AstrMessageEvent, keyword: str = ""
    ) -> str:
        """Send random Pixiv artwork, optionally matching a tag or search keyword.

        Use for requests to discover artwork or illustrations. Results follow
        the current group's configured artwork content policy and display settings.

        Args:
            keyword (string): Optional artwork tag or search phrase; empty uses rankings.
        """
        if not isinstance(keyword, str):
            return "搜索关键词必须是字符串。"
        # Treat multiline text as one search phrase, never as extra commands.
        keyword = " ".join(keyword.split())
        return await self._execute_pixiv_tool(event, f"#pixiv随机 {keyword}".rstrip())

    @filter.llm_tool(name="juhkff_pixiv_artwork")
    async def juhkff_pixiv_artwork(
        self, event: AstrMessageEvent, artwork_id: str, page: int = 0
    ) -> str:
        """Send a specific Pixiv artwork by numeric ID or Pixiv artwork URL.

        Use when the user identifies an artwork to view. The current group's
        configured artwork content policy and display settings apply.

        Args:
            artwork_id (string): Numeric artwork ID or a pixiv.net/artworks URL.
            page (number): One-based image number; zero sends all pages. Defaults to zero.
        """
        if not isinstance(artwork_id, str):
            return "作品参数无效，请提供数字 ID 或 Pixiv 作品链接。"
        match = re.fullmatch(
            r"(?:([0-9]+)|https?://(?:www\.)?pixiv\.net/"
            r"(?:[a-z]{2}/)?artworks/([0-9]+)(?:\?[^\s]*)?)",
            artwork_id.strip(),
        )
        if match is None or not (iid := (match.group(1) or match.group(2)).lstrip("0")):
            return "作品参数无效，请提供数字 ID 或 Pixiv 作品链接。"
        if isinstance(page, bool) or not isinstance(page, int) or page < 0:
            return "页码无效，请使用非负整数（0 表示全部图片，1 表示第一张）。"
        suffix = f" 第{page}张" if page else ""
        return await self._execute_pixiv_tool(event, f"#pixiv作品 {iid}{suffix}")

    # ------------------------------------------------------------ 指令

    @filter.command("juhkff")
    async def _cmd_status(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """`/juhkff`：查看总开关、数据目录和每个模块的启停状态。"""
        lines = [
            f"【juhkff 综合插件】v{VERSION}",
            f"总开关：{'开' if self.global_enabled else '关'}",
            f"数据目录：{self.data_dir}",
            "",
        ]
        for mod in self.modules.values():
            lines.append(
                f"· {mod.title}（{mod.key}）：{'启用' if mod.enabled else '停用'}"
            )
        yield event.plain_result("\n".join(lines))
