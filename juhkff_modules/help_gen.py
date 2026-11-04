"""Render enabled help entries grouped by command and automatic behavior."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain

from .common import BaseModule, data_dir, message_text

_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html { background: #f4f2eb; }
  body {
    width: 880px; padding: 42px;
    color: #1e3034; background: #f4f2eb;
    font-family: "Noto Sans SC", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  header { position: relative; padding: 0 4px 30px; }
  .eyebrow {
    display: flex; align-items: center; gap: 9px;
    font-size: 13px; font-weight: 700; letter-spacing: 3px; color: #62736f;
  }
  .eyebrow::before { content: ""; width: 9px; height: 9px; background: #e4895f; border-radius: 50%; }
  .version {
    position: absolute; top: 0; right: 4px; border: 1px solid #d3dbd4;
    padding: 5px 12px; border-radius: 20px; font-size: 13px;
    letter-spacing: 1px; color: #61736c;
  }
  h1 {
    margin-top: 19px; max-width: 100%; font-size: 52px; line-height: 1.25;
    font-weight: 800; letter-spacing: 2px; overflow-wrap: anywhere;
  }
  .subtitle {
    margin-top: 12px; color: #6d7c74; font-size: 12px;
    font-weight: 500; letter-spacing: 3px; overflow-wrap: anywhere;
  }
  .intro { margin-top: 22px; font-size: 17px; line-height: 1.7; color: #66766d; }
  .summary { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 20px; }
  .summary span {
    padding: 7px 13px; font-size: 13px; background: #e7ebe2;
    border-radius: 8px; color: #53665b;
  }
  .summary b { font-size: 15px; color: #28483d; margin-right: 5px; }
  .section { margin-top: 20px; border-radius: 24px; padding: 28px; }
  .section.commands { color: #ecf3ef; background: #203b3d; }
  .section.automatic { border: 1px solid #d7dfd2; background: #eaf0e4; }
  .section-heading { display: flex; align-items: flex-start; gap: 14px; margin-bottom: 22px; }
  .section-icon {
    flex: 0 0 43px; display: grid; place-items: center;
    width: 43px; height: 43px; border-radius: 13px;
    background: #365454; color: #c8e8d5;
  }
  .automatic .section-icon { color: #466c51; background: #d4e2ce; }
  .section-icon svg { width: 23px; height: 23px; }
  .section-heading h2 { font-size: 25px; letter-spacing: 1px; line-height: 1.4; }
  .section-heading p { margin-top: 5px; font-size: 14px; line-height: 1.5; color: #a7c2b8; }
  .automatic .section-heading p { color: #6c806c; }
  .section-count {
    margin-left: auto; padding-top: 4px; font-size: 24px;
    font-weight: 500; letter-spacing: 1px; color: #87aca0;
  }
  .automatic .section-count { color: #81967b; }
  .command-item {
    display: grid; grid-template-columns: 30px minmax(0, 1fr);
    column-gap: 12px; padding: 18px 0;
    border-top: 1px solid #3b5554;
  }
  .command-index { padding-top: 5px; font-size: 12px; color: #8caaa0; font-variant-numeric: tabular-nums; }
  .command-item code {
    display: inline-block; max-width: 100%; padding: 5px 12px; border-radius: 7px;
    background: #315353; color: #d0f1de; font-size: 20px; line-height: 1.45;
    font-family: Consolas, "Noto Sans SC", "Noto Sans CJK SC", monospace;
    font-weight: 600; white-space: pre-wrap; overflow-wrap: anywhere;
  }
  .command-item p { margin-top: 9px; font-size: 16px; color: #bed0c7; line-height: 1.65; overflow-wrap: anywhere; }
  .command-item:last-child { padding-bottom: 0; }
  .automatic-items { display: grid; gap: 12px; }
  .automatic-item {
    display: grid; grid-template-columns: 43px minmax(0, 1fr);
    column-gap: 16px; padding: 20px; border-radius: 15px; background: #fafbf5;
    border: 1px solid #dde5d6;
  }
  .automatic-index {
    display: grid; place-items: center; width: 42px; height: 42px;
    border-radius: 12px; background: #e8eedf; color: #78906b;
    font-size: 15px; font-weight: 600; font-variant-numeric: tabular-nums;
  }
  .automatic-item h3 { font-size: 20px; font-weight: 700; line-height: 1.4; overflow-wrap: anywhere; }
  .automatic-item p { margin-top: 7px; color: #71816c; font-size: 16px; line-height: 1.7; overflow-wrap: anywhere; }
  footer {
    display: flex; justify-content: space-between; align-items: baseline; gap: 16px;
    margin: 25px 5px 0; font-size: 12px; line-height: 1.8; color: #829080;
  }
  footer code { padding: 4px 8px; border-radius: 5px; background: #e6eadd; color: #506950; overflow-wrap: anywhere; }
</style>
</head>
<body>
  <header>
    <div class="eyebrow">使用指南</div>
    <span class="version">v{{ version | e }}</span>
    <h1>{{ title_zh | e }}</h1>
    <div class="subtitle">{{ title_en | e }}</div>
    <p class="intro">指令操作与自动响应，一页查看。</p>
    <div class="summary">
      {% for section in sections %}
      <span><b>{{ section['items'] | length }}</b>{{ '条指令' if section.key == 'commands' else '项自动功能' }}</span>
      {% endfor %}
    </div>
  </header>
  <main>
    {% for section in sections %}
    <section class="section {{ section.key }}" data-group="{{ section.key }}">
      <div class="section-heading">
        <span class="section-icon">
          {% if section.key == 'commands' %}
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="m5 6 6 6-6 6M14 18h5"/></svg>
          {% else %}
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="m13 3-8 11h6l-1 7 9-12h-7l1-6Z"/></svg>
          {% endif %}
        </span>
        <div><h2>{{ section.name }}</h2><p>{{ section.description }}</p></div>
        <span class="section-count">{{ '%02d' | format(section['items'] | length) }}</span>
      </div>
      {% if section.key == 'commands' %}
        {% for item in section['items'] %}
        <article class="command-item" data-module="{{ item.module_key | e }}">
          <span class="command-index">{{ '%02d' | format(loop.index) }}</span>
          <div><code>{{ item.command | e }}</code><p>{{ item.description | e }}</p></div>
        </article>
        {% endfor %}
      {% else %}
      <div class="automatic-items">
        {% for item in section['items'] %}
        <article class="automatic-item" data-module="{{ item.module_key | e }}">
          <span class="automatic-index">{{ '%02d' | format(loop.index) }}</span>
          <div><h3>{{ item.module | e }}</h3><p>{{ item.description | e }}</p></div>
        </article>
        {% endfor %}
      </div>
      {% endif %}
    </section>
    {% endfor %}
  </main>
  <footer>
    <span>仅展示已开启的功能</span>
    <span>再次查看 <code>{{ help_command | e }}</code></span>
  </footer>
</body>
</html>"""


class HelpGenModule(BaseModule):
    """`#帮助` → 收集各模块的 `help_entries()` 渲染成一张卡片。"""

    key = "help"
    title = "帮助图"

    def intercepts(self, event: AstrMessageEvent) -> bool:
        """Reserve the configured help command before passive handlers.

        Args:
            event: Incoming message, possibly with its wake prefix removed.

        Returns:
            Whether this module owns the help command.
        """
        command = str(self.cfg.get("command") or "帮助").strip().lstrip("#")
        text = message_text(event).strip()
        return bool(command) and (
            text == f"#{command}" or (event.is_at_or_wake_command and text == command)
        )

    async def handle(self, event: AstrMessageEvent):
        if not self.intercepts(event):
            return None
        return await self._reply(event)

    # ------------------------------------------------------------ Content

    def _sections(self) -> list[dict[str, Any]]:
        """Group enabled entries by whether they expose a command.

        Returns:
            Nonempty command and automatic sections, in that order.
        """
        commands: list[dict[str, str]] = []
        automatic: list[dict[str, str]] = []
        for module in self.plugin.modules.values():
            if not module.enabled:
                continue
            try:
                entries = list(module.help_entries())
            except Exception:
                continue
            # Classify entries individually: a module may expose both behaviors.
            for command, description in entries:
                item = {
                    "module_key": module.key,
                    "module": module.title,
                    "command": command,
                    "description": description,
                }
                if command.strip().startswith(("#", "/")):
                    commands.append(item)
                else:
                    automatic.append(item)
        return [
            section
            for section in (
                {
                    "key": "commands",
                    "name": "指令功能",
                    "description": "发送对应指令，按需调用",
                    "items": commands,
                },
                {
                    "key": "automatic",
                    "name": "自动功能",
                    "description": "无需指令，在群聊中自动触发",
                    "items": automatic,
                },
            )
            if section["items"]
        ]

    def _text(self) -> str:
        """Build text help with the same grouping as the image.

        Returns:
            Enabled help entries grouped by their interaction style.
        """
        title = str(self.cfg.get("title_zh") or "").strip() or str(
            self.cfg.get("command") or "帮助"
        ).strip().lstrip("#")
        lines = [f"【{title}】"]
        for section in self._sections():
            lines.append(f"\n▍{section['name']}")
            for item in section["items"]:
                name = (
                    item["command"] if section["key"] == "commands" else item["module"]
                )
                lines.append(f"  {name} — {item['description']}")
        return "\n".join(lines)

    def _prune_images(self, current: Path) -> None:
        """Retain recent generated images without interrupting queued sends.

        Args:
            current: Newly generated help image, which must never be removed.
        """
        target = data_dir().resolve() / "help"
        try:
            # Verify the exact directory before scanning or deleting anything.
            if (
                current.parent != target
                or target.is_symlink()
                or target.resolve() != target
            ):
                return
            generated: list[tuple[float, Path]] = []
            for path in target.iterdir():
                if not re.fullmatch(r"help_[0-9]+\.png", path.name):
                    continue
                try:
                    if not path.is_symlink() and path.is_file():
                        generated.append((path.stat().st_mtime, path))
                except OSError:
                    continue
            generated.sort(key=lambda item: item[0], reverse=True)
            cutoff = time.time() - 3600
            for modified, path in generated[20:]:
                if path == current or modified > cutoff:
                    continue
                # Recheck containment and symlinks immediately before deletion.
                if path.is_symlink() or path.resolve().parent != target:
                    continue
                try:
                    path.unlink()
                except OSError:
                    continue
        except (OSError, RuntimeError):
            logger.warning("[juhkff] Help image cleanup failed", exc_info=True)

    async def _reply(self, event: AstrMessageEvent):
        """Render grouped help or return explicitly selected text output.

        Args:
            event: Event requesting the help response.

        Returns:
            An image or text message chain, or a rendering error response.
        """
        sections = self._sections()
        if not sections:
            return event.plain_result("没有已启用的功能。")

        command = str(self.cfg.get("command") or "帮助").strip().lstrip("#")
        title_zh = str(self.cfg.get("title_zh") or "").strip() or command
        title_en = str(self.cfg.get("title_en") or "").strip() or "JUHKFF-PLUGIN"
        version = self.plugin.version

        if not self.cfg.get("render_image", True):
            return event.plain_result(self._text())

        data = {
            "title_zh": title_zh,
            "title_en": title_en,
            "version": version,
            "sections": sections,
            "help_command": f"#{command}",
        }
        from .renderer import render_template

        png = await render_template(
            _HTML, data, scale=2, viewport={"width": 880, "height": 900}
        )
        if not png:
            logger.error("[juhkff] Help image rendering failed")
            return event.plain_result(
                "帮助图生成失败，详细原因看 AstrBot 日志（[juhkff] 开头）"
            )

        target = data_dir().resolve() / "help"
        target.mkdir(parents=True, exist_ok=True)
        file_path = target / f"help_{time.time_ns()}.png"
        file_path.write_bytes(png)
        self._prune_images(file_path)
        return (
            MessageChain()
            .file_image(str(file_path))
            .message("这是当前已启用功能的使用说明。")
        )
