"""Track ranking membership and deliver newly listed Pixiv works."""

from __future__ import annotations

import asyncio
import re
from copy import deepcopy
from typing import Any

from .common import save_json
from .pixiv_browse import _bounded
from .pixiv_random import _RANDOM_HISTORY_LIMIT


class PixivRankingMonitorMixin:
    """Use the host's ranking, content policy, rendering, and delivery APIs."""

    def __getattr__(self, name: str) -> Any:
        """Describe attributes supplied by the host.

        Args:
            name: Host attribute name.

        Raises:
            AttributeError: The host does not supply the attribute.
        """
        raise AttributeError(name)

    async def start_ranking_monitor(self) -> None:
        """Register polling and fetch a fresh baseline without delaying startup."""
        self._ranking_monitor_running = False
        self._ranking_monitor_ids: set[str] | None = None
        self._ranking_monitor_source: tuple[str, int] | None = None
        self._ranking_monitor_pending: dict[str, dict[str, int | None]] = {}
        settings = (self.cfg.get("ranking") or {}).get("monitor") or {}
        if not isinstance(settings, dict) or not settings.get("enable", False):
            return
        await self.scheduler.every_minutes(
            _bounded(settings.get("check_minutes"), 30, 1440),
            self.check_ranking_updates,
            "pixiv-ranking-monitor",
            tz_name=self.tz_name,
        )
        task = asyncio.create_task(
            self.check_ranking_updates(), name="juhkff-pixiv-ranking-baseline"
        )
        self._checks.add(task)
        task.add_done_callback(self._checks.discard)

    async def check_ranking_updates(self) -> None:
        """Compare complete snapshots and retry deliveries independently per group.

        A failed or empty fetch preserves the last successful snapshot. Pending
        works are retained only while still listed. A source change establishes
        a new baseline, so unrelated rankings are never compared.
        """
        if getattr(self, "_ranking_monitor_running", False):
            return
        self._ranking_monitor_running = True
        task = asyncio.current_task()
        if task is not None:
            self._checks.add(task)
        try:
            settings = deepcopy((self.cfg.get("ranking") or {}).get("monitor") or {})
            if not isinstance(settings, dict):
                return

            def active() -> bool:
                return (
                    not self._stopped
                    and self.enabled
                    and settings.get("enable", False)
                    and (self.cfg.get("ranking") or {}).get("monitor") == settings
                )

            if not active():
                return
            configured_groups = settings.get("groups", [])
            if not isinstance(configured_groups, list):
                return
            groups = list(
                dict.fromkeys(
                    origin
                    for origin in configured_groups
                    if isinstance(origin, str)
                    and re.fullmatch(r"[^:\s]+:GroupMessage:[^:\s]+", origin)
                )
            )
            if not groups:
                return
            mode = settings.get("mode", "daily")
            if mode not in ("daily", "weekly", "monthly"):
                mode = "daily"
            pages = _bounded(settings.get("pages"), 10, 10)
            candidates = await self._browse_candidates(mode, pages)
            if not active():
                return
            if not candidates:
                raise ValueError("Pixiv ranking monitor received an empty snapshot")
            current = dict(candidates)
            source = (mode, pages)
            if (
                self._ranking_monitor_ids is None
                or self._ranking_monitor_source != source
            ):
                self._ranking_monitor_ids = set(current)
                self._ranking_monitor_source = source
                self._ranking_monitor_pending = {origin: {} for origin in groups}
                self.log.info(
                    "[juhkff] Pixiv ranking monitor baseline: mode=%s, works=%s",
                    mode,
                    len(current),
                )
                return
            added = set(current) - self._ranking_monitor_ids
            self._ranking_monitor_ids = set(current)
            self._ranking_monitor_pending = {
                origin: {
                    iid: current[iid]
                    for iid in self._ranking_monitor_pending.get(origin, {})
                    if iid in current
                }
                for origin in groups
            }
            for origin in groups:
                pending = self._ranking_monitor_pending[origin]
                pending.update((iid, rank) for iid, rank in candidates if iid in added)
            if added:
                self.log.info(
                    "[juhkff] Pixiv ranking monitor additions: mode=%s, works=%s",
                    mode,
                    len(added),
                )
            for origin in groups:
                pending = self._ranking_monitor_pending[origin]
                for iid, rank in list(pending.items()):
                    if iid in self._random_push_recent.get(origin, []):
                        pending.pop(iid, None)
                        continue
                    policy = self._content_policy(origin)

                    def delivery_active() -> bool:
                        return active() and self._content_policy(origin) == policy

                    if not delivery_active():
                        return
                    try:
                        item = await self._fetch_detail(iid)
                        if not delivery_active():
                            return
                        restriction = item.get("x_restrict", -1)
                        if restriction not in (0, 1, 2) or (
                            restriction != 0 and policy[0]
                        ):
                            pending.pop(iid, None)
                            continue
                        chain = await self._render_items(
                            [item],
                            origin,
                            ranks={iid: rank} if rank is not None else {},
                            delivery_active=delivery_active,
                        )
                        if not delivery_active():
                            return
                        if chain is None:
                            pending.pop(iid, None)
                            continue
                        async with self._push_history_lock:
                            if not delivery_active():
                                return
                            recent = self._random_push_recent.get(origin, [])
                            if iid in recent:
                                pending.pop(iid, None)
                                continue
                            sent = await self.plugin.context.send_message(origin, chain)
                            if sent is False:
                                raise RuntimeError(
                                    "Pixiv ranking monitor destination is unavailable"
                                )
                            self._random_push_recent[origin] = (recent + [iid])[
                                -_RANDOM_HISTORY_LIMIT:
                            ]
                            pending.pop(iid, None)
                            try:
                                save_json(
                                    self._random_push_history_path,
                                    self._random_push_recent,
                                )
                            except Exception:
                                self.log.exception(
                                    "[juhkff] Failed to save Pixiv push history"
                                )
                    except Exception:
                        self.log.exception(
                            "[juhkff] Pixiv ranking update delivery failed: origin=%s, artwork=%s",
                            origin,
                            iid,
                        )
        except Exception:
            self.log.exception("[juhkff] Pixiv ranking monitor check failed")
        finally:
            self._ranking_monitor_running = False
            if task is not None:
                self._checks.discard(task)
