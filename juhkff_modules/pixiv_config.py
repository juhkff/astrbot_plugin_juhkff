"""Migrate Pixiv settings after AstrBot has injected schema defaults."""

from __future__ import annotations

from collections.abc import MutableMapping
from copy import deepcopy
from typing import Any


def migrate_pixiv_config(config: MutableMapping[str, Any]) -> bool:
    """Resolve new settings without discarding legacy preferences.

    Args:
        config: Complete plugin configuration, including retained legacy fields.

    Returns:
        Whether configuration changed. Persistent configurations are saved once.
    """
    settings = config.get("pixiv_push")
    if not isinstance(settings, dict):
        return False
    changed = False
    filtered = settings.get("filter_r18", True) is not False
    display = settings.get("r18_display", "link")
    if settings.get("r18_policy") not in ("block", "link", "image"):
        settings["r18_policy"] = (
            "block" if filtered else "image" if display == "image" else "link"
        )
        changed = True
    filtered = settings["r18_policy"] == "block"
    if not filtered:
        display = settings["r18_policy"]
    rules = settings.get("group_content_rules", [])
    for rule in rules if isinstance(rules, list) else []:
        if not isinstance(rule, dict) or rule.get("r18_policy") in (
            "block",
            "link",
            "image",
        ):
            continue
        # Missing legacy fields inherited the corresponding global legacy value.
        rule_filtered = rule.get("filter_r18", filtered) is not False
        rule_display = rule.get("r18_display", display)
        rule["r18_policy"] = (
            "block" if rule_filtered else "image" if rule_display == "image" else "link"
        )
        changed = True
    translation = settings.get("content_translation")
    if isinstance(translation, dict) and translation.get("method") not in (
        "off",
        "text",
        "overlay",
    ):
        translation["method"] = (
            "off"
            if not translation.get("enable", False)
            else "overlay"
            if translation.get("mode") == "overlay"
            else "text"
        )
        changed = True
    random_settings = settings.get("random")
    if isinstance(random_settings, dict):
        pages = random_settings.get("pool_pages")
        if type(pages) is not int or pages < 1:
            ranking = settings.get("ranking") or {}
            pages = ranking.get("pool_pages", 5) if isinstance(ranking, dict) else 5
            if type(pages) is not int or pages < 1:
                pages = 5
            random_settings["pool_pages"] = min(pages, 10)
            changed = True
    subscription = settings.get("artist_subscription")
    if isinstance(subscription, dict):
        if not subscription.get("migrated", False):
            # Import once so clearing the new list never restores legacy rows.
            if not subscription.get("list"):
                subscription["list"] = deepcopy(settings.get("subscriptions", []))
            subscription["migrated"] = True
            changed = True
        for key, default in (("check_minutes", 60), ("retry_interval_minutes", 2)):
            if subscription.get(key) in (None, 0):
                subscription[key] = settings.get(key, default) or default
                changed = True
    if changed:
        save = getattr(config, "save_config", None)
        if callable(save):
            save()
    return changed
