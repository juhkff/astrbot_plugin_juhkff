"""Preserve Pixiv preferences through real framework schema normalization."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from juhkff_modules.pixiv_config import migrate_pixiv_config
from juhkff_modules.pixiv_push import PixivPushModule

from astrbot.api import AstrBotConfig


class PixivConfigTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "plugin.json"
        self.schema = json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text()
        )

    def load(self, pixiv):
        self.path.write_text(json.dumps({"pixiv_push": pixiv}), encoding="utf-8")
        return AstrBotConfig(config_path=str(self.path), schema=self.schema)

    def test_schema_retains_legacy_values_until_constructor_migration(self):
        for filtered, display, expected in (
            (True, "image", "block"),
            (False, "link", "link"),
            (False, "image", "image"),
        ):
            with self.subTest(expected=expected):
                config = self.load(
                    {
                        "filter_r18": filtered,
                        "r18_display": display,
                        "content_translation": {"enable": True, "mode": "overlay"},
                        "ranking": {"pool_pages": 8},
                    }
                )
                settings = config["pixiv_push"]
                self.assertEqual(settings["r18_policy"], "block")
                self.assertEqual(settings["content_translation"]["method"], "off")
                self.assertEqual(settings["random"]["pool_pages"], 5)
                with patch.object(
                    AstrBotConfig,
                    "save_config",
                    autospec=True,
                    side_effect=AstrBotConfig.save_config,
                ) as save:
                    module = PixivPushModule(SimpleNamespace(config=config))
                    save.assert_called_once_with(config)
                    self.assertFalse(migrate_pixiv_config(config))
                    save.assert_called_once_with(config)
                self.assertEqual(settings["r18_policy"], expected)
                self.assertEqual(settings["content_translation"]["method"], "overlay")
                self.assertEqual(settings["random"]["pool_pages"], 8)
                self.assertEqual(
                    module._content_policy("test:GroupMessage:1")[0], filtered
                )
                reloaded = AstrBotConfig(config_path=str(self.path), schema=self.schema)
                self.assertFalse(migrate_pixiv_config(reloaded))

    def test_group_legacy_inheritance_first_match_and_explicit_precedence(self):
        config = self.load(
            {
                "filter_r18": False,
                "r18_display": "image",
                "group_content_rules": [
                    {"groups": [], "filter_r18": True},
                    {"groups": ["1"], "r18_display": "link"},
                    {"groups": ["test:GroupMessage:1"], "filter_r18": True},
                    {"groups": ["2"], "r18_policy": "image", "filter_r18": True},
                    {"groups": ["3"], "filter_r18": True},
                ],
            }
        )
        module = PixivPushModule(SimpleNamespace(config=config))
        self.assertEqual(module._content_policy("test:GroupMessage:1"), (False, "link"))
        self.assertEqual(
            module._content_policy("test:GroupMessage:2"), (False, "image")
        )
        self.assertEqual(module._content_policy("test:GroupMessage:3"), (True, "link"))
        self.assertEqual(
            module._content_policy("test:GroupMessage:4"), (False, "image")
        )

    def test_new_choices_win_and_unrelated_settings_are_preserved(self):
        config = self.load(
            {
                "preferences_migrated": True,
                "r18_policy": "image",
                "filter_r18": True,
                "content_translation": {
                    "method": "off",
                    "enable": True,
                    "mode": "overlay",
                    "language": "英语",
                    "provider_id": "vision",
                    "typesetting": {"app_id": "example", "app_key": "secret"},
                },
                "auto_translate": {"enable": True, "language": "日语"},
                "random": {"pool_pages": 2},
                "ranking": {"pool_pages": 9},
                "random_push": {"enable": False, "groups": ["test:GroupMessage:9"]},
                "artist_subscription": {
                    "list": [],
                    "check_minutes": 60,
                    "retry_interval_minutes": 2,
                    "migrated": True,
                },
            }
        )
        before = deepcopy(dict(config))
        self.assertFalse(migrate_pixiv_config(config))
        self.assertEqual(dict(config), before)

    def test_constructor_migrates_legacy_artist_subscriptions_and_intervals(self):
        subscriptions = [
            {"user_id": "7", "groups": ["test:GroupMessage:1"]},
            {"user_id": "8", "groups": ["test:GroupMessage:2"]},
        ]
        config = self.load(
            {
                "subscriptions": subscriptions,
                "check_minutes": 137,
                "retry_interval_minutes": 11,
            }
        )
        settings = config["pixiv_push"]
        self.assertEqual(settings["subscriptions"], subscriptions)
        self.assertEqual(settings["artist_subscription"]["list"], [])
        self.assertEqual(settings["artist_subscription"]["check_minutes"], 60)
        self.assertEqual(settings["artist_subscription"]["retry_interval_minutes"], 2)
        self.assertFalse(settings["artist_subscription"]["migrated"])
        with patch.object(
            AstrBotConfig,
            "save_config",
            autospec=True,
            side_effect=AstrBotConfig.save_config,
        ) as save:
            module = PixivPushModule(SimpleNamespace(config=config))
            save.assert_called_once_with(config)
            self.assertFalse(migrate_pixiv_config(config))
            save.assert_called_once_with(config)
        expected = {
            "list": subscriptions,
            "check_minutes": 137,
            "retry_interval_minutes": 11,
            "migrated": True,
        }
        self.assertEqual(settings["artist_subscription"], expected)
        self.assertEqual(
            module._subscriptions(),
            {"7": ["test:GroupMessage:1"], "8": ["test:GroupMessage:2"]},
        )
        reloaded = AstrBotConfig(config_path=str(self.path), schema=self.schema)
        self.assertEqual(reloaded["pixiv_push"]["artist_subscription"], expected)
        self.assertFalse(migrate_pixiv_config(reloaded))

    def test_clearing_migrated_artist_list_survives_reload(self):
        config = self.load(
            {"subscriptions": [{"user_id": "7", "groups": ["test:GroupMessage:1"]}]}
        )
        PixivPushModule(SimpleNamespace(config=config))
        settings = config["pixiv_push"]
        self.assertTrue(settings["artist_subscription"]["migrated"])
        self.assertTrue(settings["subscriptions"])
        settings["artist_subscription"]["list"] = []
        config.save_config()
        reloaded = AstrBotConfig(config_path=str(self.path), schema=self.schema)
        self.assertFalse(migrate_pixiv_config(reloaded))
        module = PixivPushModule(SimpleNamespace(config=reloaded))
        self.assertTrue(reloaded["pixiv_push"]["subscriptions"])
        self.assertEqual(reloaded["pixiv_push"]["artist_subscription"]["list"], [])
        self.assertEqual(module._subscriptions(), {})

    def test_explicit_artist_subscription_values_win_over_legacy(self):
        subscriptions = [{"user_id": "8", "groups": ["test:GroupMessage:2"]}]
        config = self.load(
            {
                "subscriptions": [{"user_id": "7", "groups": ["test:GroupMessage:1"]}],
                "check_minutes": 137,
                "retry_interval_minutes": 11,
                "artist_subscription": {
                    "list": subscriptions,
                    "check_minutes": 23,
                    "retry_interval_minutes": 4,
                },
            }
        )
        module = PixivPushModule(SimpleNamespace(config=config))
        expected = {
            "list": subscriptions,
            "check_minutes": 23,
            "retry_interval_minutes": 4,
            "migrated": True,
        }
        self.assertEqual(config["pixiv_push"]["artist_subscription"], expected)
        self.assertEqual(module._subscriptions(), {"8": ["test:GroupMessage:2"]})
        reloaded = AstrBotConfig(config_path=str(self.path), schema=self.schema)
        self.assertFalse(migrate_pixiv_config(reloaded))
        self.assertEqual(reloaded["pixiv_push"]["artist_subscription"], expected)

    def test_migrated_intervals_preserve_defaults_and_repair_zero_placeholders(self):
        for check, retry, expected in ((60, 2, (60, 2)), (0, 0, (137, 11))):
            with self.subTest(check=check, retry=retry):
                config = self.load(
                    {
                        "check_minutes": 137,
                        "retry_interval_minutes": 11,
                        "artist_subscription": {
                            "migrated": True,
                            "check_minutes": check,
                            "retry_interval_minutes": retry,
                        },
                    }
                )
                PixivPushModule(SimpleNamespace(config=config))
                subscription = config["pixiv_push"]["artist_subscription"]
                self.assertEqual(
                    (
                        subscription["check_minutes"],
                        subscription["retry_interval_minutes"],
                    ),
                    expected,
                )
                reloaded = AstrBotConfig(config_path=str(self.path), schema=self.schema)
                self.assertFalse(migrate_pixiv_config(reloaded))
                self.assertEqual(
                    reloaded["pixiv_push"]["artist_subscription"], subscription
                )

    def test_migrated_preferences_keep_explicit_default_choices(self):
        for marker in (
            {"preferences_migrated": True},
            {"artist_subscription": {"migrated": True}},
        ):
            with self.subTest(marker=marker):
                config = self.load(
                    {
                        **marker,
                        "filter_r18": False,
                        "r18_display": "image",
                        "r18_policy": "block",
                        "content_translation": {
                            "method": "off",
                            "enable": True,
                            "mode": "overlay",
                        },
                        "ranking": {"pool_pages": 8},
                        "random": {"pool_pages": 5},
                    }
                )
                PixivPushModule(SimpleNamespace(config=config))
                settings = config["pixiv_push"]
                self.assertEqual(settings["r18_policy"], "block")
                self.assertEqual(settings["content_translation"]["method"], "off")
                self.assertEqual(settings["random"]["pool_pages"], 5)
                self.assertFalse(migrate_pixiv_config(config))

    def test_zero_random_pool_recovers_legacy_and_later_reset_survives_reload(self):
        config = self.load({"random": {"pool_pages": 0}, "ranking": {"pool_pages": 8}})
        PixivPushModule(SimpleNamespace(config=config))
        self.assertEqual(config["pixiv_push"]["random"]["pool_pages"], 8)
        config["pixiv_push"]["random"]["pool_pages"] = 5
        config.save_config()
        reloaded = AstrBotConfig(config_path=str(self.path), schema=self.schema)
        self.assertFalse(migrate_pixiv_config(reloaded))
        self.assertEqual(reloaded["pixiv_push"]["random"]["pool_pages"], 5)

    def test_artist_subscription_defaults_are_resolved_on_first_migration(self):
        config = self.load({})
        self.assertEqual(config["pixiv_push"]["random"]["pool_pages"], 5)
        self.assertEqual(config["pixiv_push"]["r18_policy"], "block")
        self.assertEqual(config["pixiv_push"]["content_translation"]["method"], "off")
        subscription = config["pixiv_push"]["artist_subscription"]
        self.assertEqual(subscription["check_minutes"], 60)
        self.assertEqual(subscription["retry_interval_minutes"], 2)
        PixivPushModule(SimpleNamespace(config=config))
        self.assertEqual(
            config["pixiv_push"]["artist_subscription"],
            {
                "list": [],
                "check_minutes": 60,
                "retry_interval_minutes": 2,
                "migrated": True,
            },
        )
        self.assertFalse(migrate_pixiv_config(config))

    def test_legacy_translation_off_text_and_overlay(self):
        for enabled, mode, expected in (
            (False, "overlay", "off"),
            (True, "text", "text"),
            (True, "overlay", "overlay"),
        ):
            with self.subTest(expected=expected):
                config = self.load(
                    {
                        "content_translation": {
                            "enable": enabled,
                            "mode": mode,
                        }
                    }
                )
                self.assertTrue(migrate_pixiv_config(config))
                self.assertEqual(
                    config["pixiv_push"]["content_translation"]["method"], expected
                )

    def test_legacy_plain_configuration_still_resolves_without_migration(self):
        config = {"pixiv_push": {"filter_r18": False, "r18_display": "image"}}
        module = PixivPushModule(SimpleNamespace(config=config))
        self.assertNotIn("r18_policy", config["pixiv_push"])
        self.assertEqual(
            module._content_policy("test:GroupMessage:1"), (False, "image")
        )
        config["pixiv_push"]["filter_r18"] = True
        self.assertEqual(module._content_policy("test:GroupMessage:1"), (True, "image"))
