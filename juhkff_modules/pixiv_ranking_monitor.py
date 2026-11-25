"""Track ranking membership and deliver newly listed Pixiv works."""

from __future__ import annotations

import asyncio
import re
from copy import deepcopy
from typing import Any

from .common import save_json
from .pixiv_browse import _R18_RANKING_MODES, _bounded
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
        """Register polling and fetch fresh baselines without delaying startup."""
        self._ranking_monitor_running = False
        self._ranking_monitor_ids: set[str] | None = None
        self._ranking_monitor_source: tuple[str, int] | None = None
        self._ranking_monitor_pending: dict[str, dict[str, int | None]] = {}
        self._ranking_monitor_r18_ids: set[str] | None = None
        self._ranking_monitor_r18_source: tuple[str, int] | None = None
        self._ranking_monitor_r18_pending: dict[str, dict[str, int | None]] = {}
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
        """Compare independent source snapshots and deliver one batch per group.

        Failed sources preserve their previous snapshot and pending works. Each
        source establishes its own silent baseline. Successful deliveries update
        shared recent history only after the complete group message was sent.
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
            policies = {origin: self._content_policy(origin) for origin in groups}
            mode = settings.get("mode", "daily")
            if mode not in ("daily", "weekly", "monthly"):
                mode = "daily"
            pages = _bounded(settings.get("pages"), 10, 10)
            sources = [(mode, False)]
            if mode in _R18_RANKING_MODES and any(
                not policy[0] for policy in policies.values()
            ):
                sources.append((_R18_RANKING_MODES[mode], True))
            else:
                # Resume with a fresh baseline after adult monitoring was inactive.
                self._ranking_monitor_r18_ids = None
                self._ranking_monitor_r18_source = None
                self._ranking_monitor_r18_pending = {}
            ready = []
            for source_mode, adult in sources:
                prefix = "_ranking_monitor_r18" if adult else "_ranking_monitor"
                source_groups = [
                    origin for origin in groups if not adult or not policies[origin][0]
                ]
                try:
                    candidates = await self._browse_candidates(source_mode, pages)
                    if not active():
                        return
                    if not candidates:
                        raise ValueError(
                            "Pixiv ranking monitor received an empty snapshot"
                        )
                except Exception:
                    self.log.exception(
                        "[juhkff] Pixiv ranking monitor fetch failed: mode=%s",
                        source_mode,
                    )
                    continue
                current = dict(candidates)
                source = (source_mode, pages)
                previous = getattr(self, prefix + "_ids", None)
                old_pending = getattr(self, prefix + "_pending", {})
                if (
                    previous is None
                    or getattr(self, prefix + "_source", None) != source
                ):
                    pending = {origin: {} for origin in source_groups}
                    self.log.info(
                        "[juhkff] Pixiv ranking monitor baseline: mode=%s, works=%s",
                        source_mode,
                        len(current),
                    )
                else:
                    added = set(current) - previous
                    # Rebuild in current ranking order, including retained retries.
                    pending = {
                        origin: {
                            iid: rank
                            for iid, rank in candidates
                            if iid in added or iid in old_pending.get(origin, {})
                        }
                        for origin in source_groups
                    }
                setattr(self, prefix + "_ids", set(current))
                setattr(self, prefix + "_source", source)
                setattr(self, prefix + "_pending", pending)
                ready.append((adult, pending))
            for origin in groups:
                policy = policies[origin]

                def delivery_active() -> bool:
                    return active() and self._content_policy(origin) == policy

                if not delivery_active():
                    return
                selected = {}
                ranks = {}
                owners = {}
                for adult, pending_by_group in ready:
                    if adult and policy[0]:
                        continue
                    pending = pending_by_group.get(origin, {})
                    for iid, rank in list(pending.items()):
                        if iid in self._random_push_recent.get(origin, []):
                            pending.pop(iid, None)
                            continue
                        owners.setdefault(iid, []).append(pending)
                        if iid in selected:
                            continue
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
                            selected[iid] = item
                            if rank is not None:
                                ranks[iid] = rank
                        except Exception:
                            self.log.exception(
                                "[juhkff] Pixiv ranking detail failed: artwork=%s", iid
                            )
                try:
                    while selected:
                        if not delivery_active():
                            return
                        # Render outside the history lock; model calls can be slow.
                        failed_work_ids: set[str] = set()
                        chain = await self._render_items(
                            list(selected.values()),
                            origin,
                            ranks=ranks,
                            delivery_active=delivery_active,
                            ranking_sections=True,
                            failed_work_ids=failed_work_ids,
                        )
                        # Failed works remain pending and never enter sent history.
                        for iid in failed_work_ids:
                            selected.pop(iid, None)
                            ranks.pop(iid, None)
                        if not delivery_active():
                            return
                        if chain is None:
                            for iid in selected:
                                for pending in owners[iid]:
                                    pending.pop(iid, None)
                            break
                        async with self._push_history_lock:
                            if not delivery_active():
                                return
                            recent = self._random_push_recent.get(origin, [])
                            duplicates = set(selected).intersection(recent)
                            if duplicates:
                                for iid in duplicates:
                                    selected.pop(iid, None)
                                    ranks.pop(iid, None)
                                    for pending in owners[iid]:
                                        pending.pop(iid, None)
                                # A concurrent random send changed this batch.
                                # Release the lock and render the remaining works.
                                continue
                            sent = await self.plugin.context.send_message(origin, chain)
                            if sent is False:
                                raise RuntimeError(
                                    "Pixiv ranking monitor destination is unavailable"
                                )
                            self._random_push_recent[origin] = (
                                recent + list(selected)
                            )[-_RANDOM_HISTORY_LIMIT:]
                            for iid in selected:
                                for pending in owners[iid]:
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
                            break
                except Exception:
                    self.log.exception(
                        "[juhkff] Pixiv ranking update delivery failed: origin=%s",
                        origin,
                    )
        except Exception:
            self.log.exception("[juhkff] Pixiv ranking monitor check failed")
        finally:
            self._ranking_monitor_running = False
            if task is not None:
                self._checks.discard(task)
