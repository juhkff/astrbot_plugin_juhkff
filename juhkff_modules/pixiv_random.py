"""Push popular works sampled from Pixiv discovery at random intervals."""

from __future__ import annotations

import asyncio
import random
import re
from copy import deepcopy
from datetime import timedelta
from typing import Any

from .common import fetch_json, get_tz, now, save_json

_RANDOM_HISTORY_LIMIT = 200


class PixivRandomMixin:
    """Use the host's detail, rendering, policy, and delivery implementations."""

    def __getattr__(self, name: str) -> Any:
        """Describe attributes supplied by the host.

        Args:
            name: Host attribute name.

        Raises:
            AttributeError: The host does not supply the attribute.
        """
        raise AttributeError(name)

    async def run_random_push(self) -> None:
        """Wait before every round and isolate failures between destinations."""
        while not self._stopped and self.enabled:
            settings = self.cfg.get("random_push") or {}
            if not isinstance(settings, dict):
                settings = {}
            lower = settings.get("min_minutes", 1)
            upper = settings.get("max_minutes", 120)
            if type(lower) is not int or lower < 1:
                lower = 1
            if type(upper) is not int or upper < 1:
                upper = 120
            lower, upper = sorted((lower, upper))
            minutes = random.randint(lower, upper)
            if settings.get("enable", False):
                next_run = (now("UTC") + timedelta(minutes=minutes)).astimezone(
                    get_tz(self.tz_name)
                )
                self.log.info(
                    "[juhkff] Pixiv random push scheduled: next=%s, wait_minutes=%s",
                    next_run.isoformat(sep=" ", timespec="seconds"),
                    minutes,
                )
            await asyncio.sleep(minutes * 60)
            if self._stopped or not self.enabled:
                return
            settings = deepcopy(self.cfg.get("random_push") or {})
            if not isinstance(settings, dict) or not settings.get("enable", False):
                continue
            groups = settings.get("groups", [])
            if not isinstance(groups, list):
                continue
            self.log.info("[juhkff] Pixiv random push triggered")
            sent = 0
            for origin in dict.fromkeys(
                group
                for group in groups
                if isinstance(group, str)
                and re.fullmatch(r"[^:\s]+:GroupMessage:[^:\s]+", group)
            ):
                try:
                    with self.log.scope():
                        sent += await self._push_random_group(origin, settings)
                except Exception:
                    self.log.exception("[juhkff] Pixiv random push failed: %s", origin)
            if sent:
                self.log.info("[juhkff] Pixiv random push completed: groups=%s", sent)

    async def _push_random_group(self, origin: str, settings: dict[str, Any]) -> int:
        """Sample up to five discovery batches and deliver one eligible work.

        Args:
            origin: Validated, fully qualified destination group.
            settings: Configuration snapshot for the current round.

        Returns:
            One for successful delivery, otherwise zero.

        Raises:
            ValueError: Discovery returned an invalid response.
            RuntimeError: The destination rejected the message.
        """
        policy = self._content_policy(origin)

        def delivery_active() -> bool:
            current = self.cfg.get("random_push") or {}
            return (
                not self._stopped
                and self.enabled
                and current == settings
                and current.get("enable", False)
                and origin in current.get("groups", [])
                and self._content_policy(origin) == policy
            )

        if not delivery_active():
            return 0
        minimum = settings.get("min_views", 10000)
        if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
            minimum = 10000
        seen: set[str] = set()
        stale_batches = 0
        for batch in range(1, 6):
            if not delivery_active():
                return 0
            data = await fetch_json(
                "https://www.pixiv.net/ajax/discovery/artworks",
                params={"mode": "all", "limit": 60},
                headers=self._api_headers(),
                proxy=self.proxy or None,
                timeout=25.0,
                allowed_hosts={"www.pixiv.net"},
                proxy_resolve=True,
            )
            if not delivery_active():
                return 0
            body = (
                data.get("body")
                if isinstance(data, dict) and not data.get("error")
                else None
            )
            thumbnails = body.get("thumbnails") if isinstance(body, dict) else None
            rows = thumbnails.get("illust") if isinstance(thumbnails, dict) else None
            if not isinstance(rows, list):
                raise ValueError("Pixiv discovery omitted its artwork collection")
            candidates = []
            new_ids = 0
            recent = self._random_push_recent.get(origin, [])
            for row in rows:
                if not isinstance(row, dict):
                    continue
                iid = str(row.get("id", ""))
                if not re.fullmatch(r"[0-9]+", iid) or not (iid := iid.lstrip("0")):
                    continue
                if iid in seen:
                    continue
                seen.add(iid)
                new_ids += 1
                if iid in recent or row.get("illustType") == 2:
                    continue
                if policy[0] and row.get("xRestrict", 0) != 0:
                    continue
                candidates.append(iid)
            stale_batches = stale_batches + 1 if new_ids == 0 else 0
            if stale_batches >= 2:
                self.log.info(
                    "[juhkff] Pixiv random push skipped: two batches without new IDs, "
                    "origin=%s, batches=%s",
                    origin,
                    batch,
                )
                return 0
            random.shuffle(candidates)
            for iid in candidates[:12]:
                if not delivery_active():
                    return 0
                if iid in self._random_push_recent.get(origin, []):
                    continue
                try:
                    item = await self._fetch_detail(iid)
                except Exception:
                    self.log.exception(
                        "[juhkff] Pixiv random candidate failed: %s", iid
                    )
                    continue
                if not delivery_active():
                    return 0
                views = item.get("view_count")
                restriction = item.get("x_restrict", -1)
                if (
                    isinstance(views, bool)
                    or not isinstance(views, int)
                    or views < minimum
                    or restriction not in (0, 1, 2)
                    or (restriction != 0 and policy[0])
                ):
                    continue
                chain = await self._render_items(
                    [item], origin, delivery_active=delivery_active
                )
                if not delivery_active():
                    return 0
                if chain is None:
                    continue
                async with self._push_history_lock:
                    if not delivery_active():
                        return 0
                    recent = self._random_push_recent.get(origin, [])
                    if iid in recent:
                        continue
                    sent = await self.plugin.context.send_message(origin, chain)
                    if sent is False:
                        raise RuntimeError("Pixiv random destination is unavailable")
                    # Remember confirmed sends even if settings changed during delivery.
                    self._random_push_recent[origin] = (recent + [iid])[
                        -_RANDOM_HISTORY_LIMIT:
                    ]
                    try:
                        save_json(
                            self._random_push_history_path, self._random_push_recent
                        )
                    except Exception:
                        self.log.exception("[juhkff] Failed to save Pixiv push history")
                    return 1
        self.log.info(
            "[juhkff] Pixiv random push skipped: no eligible work in five batches, origin=%s",
            origin,
        )
        return 0
