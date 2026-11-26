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
                self.assertEqual(settings["r18_policy"], "")
                self.assertEqual(settings["content_translation"]["method"], "")
                self.assertEqual(settings["random"]["pool_pages"], 0)
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
            }
        )
        before = deepcopy(dict(config))
        self.assertFalse(migrate_pixiv_config(config))
        self.assertEqual(dict(config), before)

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
