"""帮助图模块：移植自上游 `src/apps/helpGen.ts`。

发送 `#帮助` 时，把插件里所有已启用的功能整理成一张 HTML 卡片，
用本机 Chromium 出图（参数 2 倍缩放，720px 模板足够清晰）；渲染失败直接报错。

上游会把 Yunzai 各插件的 help 声明收集起来渲染，这里改成从本插件
各模块的 `help_entries()` 收集，结构一致、内容更可控。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain

from .common import BaseModule, PluginLike, data_dir, message_text

_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    width: 720px; padding: 32px 36px 28px;
    background: linear-gradient(160deg, #1b1f2a 0%, #232838 55%, #2a2137 100%);
    color: #fff; font-family: "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
  }
  .title { font-size: 40px; font-weight: 700; letter-spacing: 2px; }
  .subtitle { margin-top: 6px; font-size: 15px; color: #9aa4bd; letter-spacing: 4px; }
  .section { margin-top: 26px; }
  .section-name {
    font-size: 20px; font-weight: 700; color: #f0c52b;
    border-left: 5px solid #f0c52b; padding-left: 10px;
  }
  .item { display: flex; align-items: baseline; margin-top: 12px; }
  .cmd {
    flex: 0 0 auto; min-width: 190px; padding: 4px 10px;
    background: rgba(111, 186, 243, 0.16); color: #8ec5ff;
    border-radius: 6px; font-family: Consolas, monospace; font-size: 15px;
  }
  .desc { margin-left: 14px; font-size: 15px; color: #d6dcee; line-height: 1.5; }
  .footer { margin-top: 30px; text-align: center; font-size: 13px; color: #7b849c; }
</style>
</head>
<body>
  <div class="title">{{ title_zh | e }}</div>
  <div class="subtitle">{{ title_en | e }} · v{{ version }}</div>
  {% for section in sections %}
  <div class="section">
    <div class="section-name">{{ section.name | e }}</div>
    {% for item in section['items'] %}
    <div class="item">
      <span class="cmd">{{ item[0] | e }}</span>
      <span class="desc">{{ item[1] | e }}</span>
    </div>
    {% endfor %}
  </div>
  {% endfor %}
  <div class="footer">发送 #juhkff 查看运行状态与各模块开关</div>
</body>
</html>"""


class HelpGenModule(BaseModule):
    """`#帮助` → 收集各模块的 `help_entries()` 渲染成一张卡片。"""

    key = "help"
    title = "帮助图"

    async def handle(self, event: AstrMessageEvent):
        # 触发词可配置：`help.command` 默认「帮助」，即 `#帮助`
        command = str(self.cfg.get("command") or "帮助").strip().lstrip("#")
        text = message_text(event).strip()
        if not command or text != f"#{command}":
            return None
        return await self._reply(event)

    # ------------------------------------------------------------ 内容

    def _sections(self) -> list[dict[str, Any]]:
        """只收集已启用模块的条目，未启用的功能不出现在帮助里。"""
        sections: list[dict[str, Any]] = []
        for module in self.plugin.modules.values():
            if not module.enabled:
                continue
            try:
                entries = list(module.help_entries())
            except Exception:
                continue
            if entries:
                sections.append({"name": module.title, "items": entries})
        return sections

    def _text(self) -> str:
        lines = ["【juhkff 综合插件】"]
        for section in self._sections():
            lines.append(f"\n▍{section['name']}")
            for cmd, desc in section["items"]:
                lines.append(f"  {cmd} — {desc}")
        return "\n".join(lines)

    async def _reply(self, event: AstrMessageEvent):
        """渲染帮助图；失败就直接报错，不退回文字（render_image=false 时除外）。"""
        sections = self._sections()
        if not sections:
            return event.plain_result("没有已启用的功能。")

        title_zh = str(self.cfg.get("title_zh") or "").strip() or "帮助"
        title_en = str(self.cfg.get("title_en") or "").strip() or "JUHKFF-PLUGIN"
        version = self.plugin.version

        if not self.cfg.get("render_image", True):
            return event.plain_result(self._text())

        data = {
            "title_zh": title_zh,
            "title_en": title_en,
            "version": version,
            "sections": sections,
        }
        from .renderer import render_template

        png = await render_template(_HTML, data, scale=2)  # 720px 模板，2 倍足够清晰
        if not png:
            logger.error("[juhkff] 帮助图渲染失败")
            return event.plain_result(
                "帮助图生成失败，详细原因看 AstrBot 日志（[juhkff] 开头）"
            )

        target = data_dir() / "help"
        target.mkdir(parents=True, exist_ok=True)
        file_path = target / f"help_{int(time.time())}.png"
        file_path.write_bytes(png)

        # 注意：模板里访问 dict 的 items 必须写 section['items']，
        # 写成 section.items 会命中 dict.items 方法
        return MessageChain().file_image(str(file_path)).message("这是本插件的全部功能～")
