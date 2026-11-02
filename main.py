"""juhkff 综合插件：主动群聊 / 卡片解析 / 偷表情 / 日报 / Pixiv 订阅 / 帮助图。

移植自 Yunzai 插件 juhkff-plugin（https://github.com/juhkff/juhkff-plugin）。

本文件只负责：注册插件、生命周期、事件分发、状态指令。
具体功能都在 juhkff_modules/ 下，每个模块可独立开关。

阅读顺序建议：
1. `JuhkffPlugin.__init__` —— 模块注册表；
2. `_dispatch` —— 一条消息如何分发给各模块；
3. `juhkff_modules/common.py` —— 配置、定时器、模块基类；
4. 各功能模块（active_chat / share_parse / emoji_steal / daily_report /
   pixiv_push / help_gen）。
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncGenerator

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
from juhkff_modules.active_chat import ActiveChatModule  # noqa: E402
from juhkff_modules.daily_report import DailyReportModule  # noqa: E402
from juhkff_modules.emoji_steal import EmojiStealModule  # noqa: E402
from juhkff_modules.help_gen import HelpGenModule  # noqa: E402
from juhkff_modules.pixiv_push import PixivPushModule  # noqa: E402
from juhkff_modules.share_parse import ShareParseModule  # noqa: E402

from astrbot.api import AstrBotConfig, logger  # noqa: E402
from astrbot.api.event import (  # noqa: E402
    AstrMessageEvent,
    MessageEventResult,
    filter,
)
from astrbot.api.star import Context, Star, register  # noqa: E402

# 版本号：开发期间固定为初代 0.1.0，不随功能变化调整。
VERSION = "0.1.0"


@register(
    "astrbot_plugin_juhkff",
    "juhkff",
    "综合功能插件：主动群聊 / 卡片解析 / 偷表情 / 日报 / Pixiv 订阅 / 帮助图",
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
                ActiveChatModule(self),
                ShareParseModule(self),
                EmojiStealModule(self),
                DailyReportModule(self),
                PixivPushModule(self),
                HelpGenModule(self),
            )
        }
        self._started = False

    # ------------------------------------------------------------ 配置

    @property
    def global_enabled(self) -> bool:
        """总开关：关掉后所有模块都不工作（配置页 `global.enabled`）。"""
        return bool((self.config.get("global") or {}).get("enabled", True))

    # ------------------------------------------------------------ 生命周期

    @filter.on_astrbot_loaded()
    async def _on_astrbot_loaded(self) -> None:
        """AstrBot 初始化完成（此时 Context/DB/定时器都已就绪）后启动各模块。"""
        await self._ensure_started()

    async def _ensure_started(self) -> None:
        """启动所有启用模块（幂等）。

        只启动 `enabled` 为真的模块：它们会在 `start()` 里注册定时任务、
        连 Redis、初始化状态文件等；单个模块失败不影响其它模块。

        为什么除了 `on_astrbot_loaded` 还要在消息入口再调一次：那个钩子只在
        AstrBot **进程启动**时触发，WebUI 热重载插件（保存配置、禁用再启用）
        不会重新触发。不补这一下，热重载后 Redis 不会连、定时任务不会注册，
        主动群聊的历史就永远是 0 条。
        """
        if self._started:
            return
        self._started = True

        started, skipped, failed = [], [], []
        for mod in self.modules.values():
            if not mod.enabled:
                skipped.append(mod.key)
                continue
            try:
                await mod.start()
                started.append(mod.key)
            except Exception:
                failed.append(mod.key)
                logger.exception(f"[juhkff] 模块 {mod.key} 启动失败")

        logger.info(
            f"[juhkff] v{VERSION} 已加载 | 启动: {started or '无'} | "
            f"停用: {skipped or '无'} | 失败: {failed or '无'}"
        )

    async def terminate(self) -> None:
        """插件卸载 / AstrBot 退出时调用：先停模块，再注销定时任务。"""
        for mod in self.modules.values():
            try:
                await mod.stop()
            except Exception:
                logger.exception(f"[juhkff] 模块 {mod.key} 停止时出错")
        await self.scheduler.shutdown()
        # 本地渲染器会常驻一个 Chromium 进程，卸载时一起关掉
        try:
            from .juhkff_modules.renderer import renderer as html_renderer

            await html_renderer.close()
        except Exception:
            logger.exception("[juhkff] 关闭本地渲染器失败")
        logger.info("[juhkff] 已卸载")

    # ------------------------------------------------------------ 消息分发

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _dispatch(self, event: AstrMessageEvent) -> AsyncGenerator[None, None]:
        """收到任意消息就分发给所有启用的模块。

        规则：
        - `intercepts()` 为真的模块（多轮交互）独占当前会话，其它模块本轮不参与，
          避免把用户的补充输入截走；
        - 否则所有启用的模块依次 `handle()`，谁返回了消息链就由这里直接发出去。

        为什么不用 `yield chain`：框架的 `call_handler` 只认 `MessageEventResult`
        （会 `event.set_result`），原样 yield 一个 `MessageChain` 没有任何阶段消费，
        消息不会真的发出去。这里改成 `await event.send(chain)` 立即发送，并
        `event.stop_event()` 结束事件——否则被 @ / 唤醒前缀触发的消息还会继续走
        框架自带的对话链路，用核心的人设再回一遍。
        """
        if not self.global_enabled:
            return
        # 热重载后钩子不会再触发，这里兜底把模块拉起来（幂等，只在第一次真正执行）
        await self._ensure_started()
        enabled = [mod for mod in self.modules.values() if mod.enabled]
        exclusive = [mod for mod in enabled if mod.intercepts(event)]
        sent = False
        for mod in exclusive or enabled:
            if not mod.enabled:
                continue
            try:
                chain = await mod.handle(event)
            except Exception:
                # 单个模块出错只记日志，不能影响其它模块和这条消息
                logger.exception(f"[juhkff] 模块 {mod.key} 处理消息出错")
                continue
            if chain is None:
                continue
            try:
                await event.send(chain)
                sent = True
            except Exception:
                logger.exception(f"[juhkff] 模块 {mod.key} 发送消息失败")
        if sent:
            # 已经有模块回过话，结束事件，别再让框架的 LLM 用核心人设补一句
            event.stop_event()
            return
        yield

    # ------------------------------------------------------------ 指令

    @filter.command("juhkff")
    async def _cmd_status(self, event: AstrMessageEvent) -> AsyncGenerator[MessageEventResult, None]:
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
