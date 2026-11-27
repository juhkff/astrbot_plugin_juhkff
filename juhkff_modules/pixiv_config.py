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
    # The previous subscription migration ran after the preference migration.
    subscription = settings.get("artist_subscription")
    previous_migration = isinstance(subscription, dict) and subscription.get(
        "migrated", False
    )
    first_preferences_migration = not (
        settings.get("preferences_migrated", False) or previous_migration
    )
    filtered = settings.get("filter_r18", True) is not False
    display = settings.get("r18_display", "link")
    policy = settings.get("r18_policy")
    if policy not in ("block", "link", "image") or (
        first_preferences_migration and policy == "block"
    ):
        resolved_policy = (
            "block" if filtered else "image" if display == "image" else "link"
        )
        if policy != resolved_policy:
            settings["r18_policy"] = resolved_policy
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
    if isinstance(translation, dict) and (
        translation.get("method") not in ("off", "text", "overlay", "builtin")
        or (first_preferences_migration and translation.get("method") == "off")
    ):
        resolved_method = (
            "off"
            if not translation.get("enable", False)
            else "overlay"
            if translation.get("mode") == "overlay"
            else "text"
        )
        if translation.get("method") != resolved_method:
            translation["method"] = resolved_method
            changed = True
    random_settings = settings.get("random")
    if isinstance(random_settings, dict):
        pages = random_settings.get("pool_pages")
        if (
            type(pages) is not int
            or pages < 1
            or (first_preferences_migration and pages == 5)
        ):
            ranking = settings.get("ranking") or {}
            legacy_pages = (
                ranking.get("pool_pages", 5) if isinstance(ranking, dict) else 5
            )
            if type(legacy_pages) is not int or legacy_pages < 1:
                legacy_pages = 5
            resolved_pages = min(legacy_pages, 10)
            if type(pages) is not int or pages != resolved_pages:
                random_settings["pool_pages"] = resolved_pages
                changed = True
    if not settings.get("preferences_migrated", False):
        settings["preferences_migrated"] = True
        changed = True
    subscription = settings.get("artist_subscription")
    if isinstance(subscription, dict):
        first_migration = not subscription.get("migrated", False)
        for key, default in (("check_minutes", 60), ("retry_interval_minutes", 2)):
            value = subscription.get(key)
            # Framework defaults may already be present before legacy import.
            if value in (None, 0) or (first_migration and value == default):
                resolved = settings.get(key, default) or default
                if value != resolved:
                    subscription[key] = resolved
                    changed = True
        if first_migration:
            # Import once so clearing the new list never restores legacy rows.
            if not subscription.get("list"):
                subscription["list"] = deepcopy(settings.get("subscriptions", []))
            subscription["migrated"] = True
            changed = True
    if changed:
        save = getattr(config, "save_config", None)
        if callable(save):
            save()
    return changed
