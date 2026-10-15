"""``config_writer`` 的測試：就地改值、保留註解、寫入前先驗證。"""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

from config import ConfigError, load_config, parse_config, with_max_strength
from config_writer import (
    config_values,
    format_value,
    save_config,
    save_config_values,
    update_toml_text,
)

SAMPLE = """\
# 最前面的註解
debug = false

[gsi]
host = "127.0.0.1"
port = 3000
token = "DGLAB"   # 要和 cfg 一致

# 這行註解不應該被動到
[socket]
uri = "wss://ws.dungeon-lab.cn/"

[dglab]
base_strength = 20
max_strength = 80
channels = "A"

[dglab.wave]
preset = "steady"
custom_samples = [
    [10, 100],
    [10, 40],
]
"""


class FormatValueTests(unittest.TestCase):
    def test_bool(self):
        self.assertEqual(format_value(True), "true")
        self.assertEqual(format_value(False), "false")

    def test_numbers(self):
        self.assertEqual(format_value(3000), "3000")
        self.assertEqual(format_value(0.25), "0.25")
        self.assertEqual(format_value(20.0), "20.0")

    def test_strings(self):
        self.assertEqual(format_value("DGLAB"), '"DGLAB"')
        self.assertEqual(format_value("ws://a:1"), '"ws://a:1"')
        self.assertEqual(format_value('含"引號'), '"含\\"引號"')

    def test_lists(self):
        self.assertEqual(format_value([[10, 100], [10, 40]]), "[[10, 100], [10, 40]]")

    def test_unsupported(self):
        with self.assertRaises(TypeError):
            format_value({"a": 1})


class UpdateTomlTextTests(unittest.TestCase):
    def test_replaces_value_in_the_right_section(self):
        result = update_toml_text(SAMPLE, {"gsi": {"port": 3001}})
        self.assertIn("port = 3001", result)
        self.assertNotIn("port = 3000", result)

    def test_keeps_trailing_comment(self):
        # 值變長了，對齊會被正規化成兩個空白，但註解本身要留著
        result = update_toml_text(SAMPLE, {"gsi": {"token": "NEW"}})
        self.assertIn('token = "NEW"  # 要和 cfg 一致', result)

    def test_keeps_unrelated_comments_and_lines(self):
        result = update_toml_text(SAMPLE, {"dglab": {"base_strength": 25}})
        self.assertIn("# 最前面的註解", result)
        self.assertIn("# 這行註解不應該被動到", result)
        self.assertIn("# 要和 cfg 一致", result)
        self.assertIn("base_strength = 25", result)

    def test_same_key_name_in_another_section_is_untouched(self):
        text = '[a]\nvalue = 1\n\n[b]\nvalue = 2\n'
        result = update_toml_text(text, {"b": {"value": 99}})
        self.assertIn("[a]\nvalue = 1", result)
        self.assertIn("[b]\nvalue = 99", result)

    def test_adds_missing_key_to_existing_section(self):
        result = update_toml_text(SAMPLE, {"gsi": {"extra": "x"}})
        block = result.split("[socket]")[0]
        self.assertIn('extra = "x"', block)

    def test_adds_missing_section_at_the_end(self):
        result = update_toml_text(SAMPLE, {"brand_new": {"key": 1}})
        self.assertIn("[brand_new]\nkey = 1", result)

    def test_top_level_values(self):
        result = update_toml_text(SAMPLE, {"": {"debug": True}})
        self.assertTrue(result.startswith("# 最前面的註解\ndebug = true"))

    def test_multiline_array_becomes_one_line(self):
        result = update_toml_text(
            SAMPLE, {"dglab.wave": {"custom_samples": [[10, 50], [20, 60]]}}
        )
        self.assertIn("custom_samples = [[10, 50], [20, 60]]", result)
        self.assertNotIn("[10, 40]", result)

    def test_result_is_still_valid_toml(self):
        result = update_toml_text(
            SAMPLE,
            {
                "": {"debug": True},
                "gsi": {"port": 3005},
                "dglab": {"max_strength": 55},
                "dglab.wave": {"preset": "pulse"},
                "brand_new": {"key": 1},
            },
        )
        raw = tomllib.loads(result)
        self.assertTrue(raw["debug"])
        self.assertEqual(raw["gsi"]["port"], 3005)
        self.assertEqual(raw["dglab"]["max_strength"], 55)
        self.assertEqual(raw["dglab"]["wave"]["preset"], "pulse")
        self.assertEqual(raw["brand_new"]["key"], 1)

    def test_trailing_newline_is_preserved(self):
        self.assertTrue(update_toml_text("debug = false\n", {"": {"debug": True}}).endswith("\n"))
        self.assertFalse(update_toml_text("debug = false", {"": {"debug": True}}).endswith("\n"))

    def test_hash_inside_string_is_not_a_comment(self):
        text = '[a]\nkey = "has # inside"  # 真註解\n'
        result = update_toml_text(text, {"a": {"key": "new # value"}})
        self.assertIn('key = "new # value"  # 真註解', result)


class SaveConfigTests(unittest.TestCase):
    def _scratch(self, name: str) -> Path:
        path = Path(__file__).parent / f"_scratch_cfg_{name}.toml"
        path.write_text(SAMPLE, encoding="utf-8")
        self.addCleanup(path.unlink, missing_ok=True)
        self.addCleanup(
            path.with_suffix(path.suffix + ".bak").unlink, missing_ok=True
        )
        return path

    def test_writes_and_validates(self):
        path = self._scratch("basic")
        save_config_values(
            path,
            {"gsi": {"port": 3010}, "dglab": {"max_strength": 60, "base_strength": 20}},
        )
        config = parse_config(tomllib.loads(path.read_text(encoding="utf-8")))
        self.assertEqual(config.gsi.port, 3010)
        self.assertEqual(config.dglab.max_strength, 60)

    def test_creates_backup(self):
        path = self._scratch("backup")
        save_config_values(path, {"gsi": {"port": 3011}})
        backup = path.with_suffix(path.suffix + ".bak")
        self.assertTrue(backup.exists())
        self.assertIn("port = 3000", backup.read_text(encoding="utf-8"))

    def test_invalid_values_are_not_written(self):
        path = self._scratch("invalid")
        before = path.read_text(encoding="utf-8")
        # base_strength > max_strength 會讓 parse_config 失敗
        with self.assertRaises(ConfigError):
            save_config_values(
                path, {"dglab": {"base_strength": 90, "max_strength": 40}}
            )
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            save_config_values("no-such-file.toml", {"gsi": {"port": 1}})

    def test_backup_can_be_disabled(self):
        path = self._scratch("nobackup")
        save_config_values(path, {"gsi": {"port": 3012}}, backup=False)
        self.assertFalse(path.with_suffix(path.suffix + ".bak").exists())


class ShippedConfigRoundTripTests(unittest.TestCase):
    """真的拿出貨的 config.toml 來繞一圈。"""

    def test_load_modify_save_reload(self):
        source = Path("config.toml")
        scratch = Path(__file__).parent / "_scratch_cfg_roundtrip.toml"
        scratch.write_bytes(source.read_bytes())
        self.addCleanup(scratch.unlink, missing_ok=True)
        self.addCleanup(
            scratch.with_suffix(scratch.suffix + ".bak").unlink, missing_ok=True
        )

        config = load_config(scratch)
        config = with_max_strength(config, 42)
        save_config(config, scratch)

        reloaded = load_config(scratch)
        self.assertEqual(reloaded.dglab.policy.max_strength, 42.0)
        self.assertEqual(reloaded.dglab.max_strength, 42)
        # 原本的註解還在
        text = scratch.read_text(encoding="utf-8")
        self.assertIn("郊狼 3.0", text)
        self.assertIn("#   steady      - 連續穩定輸出", text)

    def test_config_values_covers_every_editable_key(self):
        values = config_values(load_config("config.toml"))
        self.assertIn("token", values["gsi"])
        self.assertIn("max_strength", values["dglab"])
        self.assertIn("stack_death_gain", values["dglab"])
        self.assertIn("output_mode", values["dglab"])
        self.assertIn("preset", values["dglab.wave"])
        # 每個 key 都必須是 TOML 寫得出來的型別
        for section, items in values.items():
            for key, value in items.items():
                with self.subTest(section=section, key=key):
                    self.assertIsInstance(format_value(value), str)


if __name__ == "__main__":
    unittest.main()
