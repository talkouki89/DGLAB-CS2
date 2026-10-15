"""圖形介面的表單邏輯測試。

只測「設定 ↔ 表單欄位」的轉換與驗證，不需要真的開視窗。
"""

from __future__ import annotations

import unittest

from config import load_config
from coyote.socket_protocol import Channel
from coyote.waveform import PRESET_NAMES
from gui import (
    FIELDS,
    FIELD_BY_KEY,
    connection_fields_changed,
    config_from_form,
    form_values,
    parse_custom_samples,
)
from strength import MODE_HIT_ONLY


class FormRoundTripTests(unittest.TestCase):
    """出貨的 config.toml 讀進表單再寫回去，結果必須一模一樣。"""

    def setUp(self):
        self.config = load_config("config.toml")
        self.values = form_values(self.config)

    def test_every_field_has_a_value(self):
        for field in FIELDS:
            with self.subTest(field=field.key):
                self.assertIn(field.key, self.values)

    def test_no_extra_values(self):
        self.assertEqual(set(self.values), set(FIELD_BY_KEY))

    def test_round_trip_is_lossless(self):
        rebuilt = config_from_form(self.config, self.values)
        self.assertEqual(rebuilt.gsi, self.config.gsi)
        self.assertEqual(rebuilt.socket, self.config.socket)
        self.assertEqual(rebuilt.dglab.channels, self.config.dglab.channels)
        self.assertEqual(rebuilt.dglab.policy, self.config.dglab.policy)
        self.assertEqual(rebuilt.dglab.stack, self.config.dglab.stack)
        self.assertEqual(rebuilt.dglab.output, self.config.dglab.output)
        self.assertEqual(rebuilt.dglab.wave, self.config.dglab.wave)
        self.assertEqual(
            rebuilt.dglab.stop_when_no_data_seconds,
            self.config.dglab.stop_when_no_data_seconds,
        )
        self.assertEqual(rebuilt.dglab.poll_interval, self.config.dglab.poll_interval)
        self.assertEqual(rebuilt.dglab.max_strength, self.config.dglab.max_strength)

    def test_tabs_are_all_used(self):
        tabs = {field.tab for field in FIELDS}
        self.assertEqual(tabs, {"基本", "疊加", "輸出", "波形", "連線", "Discord"})

    def test_discord_round_trip(self):
        config = config_from_form(
            self.config,
            {
                **self.values,
                "discord_enabled": True,
                "discord_client_id": "123456789012345678",
                "discord_update_interval": 7.5,
                "discord_show_health": False,
            },
        )
        self.assertTrue(config.discord.enabled)
        self.assertEqual(config.discord.client_id, "123456789012345678")
        self.assertAlmostEqual(config.discord.update_interval, 7.5)
        self.assertFalse(config.discord.show_health)
        self.assertTrue(config.discord.usable)


class EditTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config("config.toml")

    def edit(self, **changes):
        values = form_values(self.config)
        values.update(changes)
        return config_from_form(self.config, values)

    def test_change_max_strength_keeps_both_copies_in_sync(self):
        config = self.edit(max_strength="55")
        self.assertEqual(config.dglab.max_strength, 55)
        self.assertEqual(config.dglab.policy.max_strength, 55.0)

    def test_change_output_mode(self):
        config = self.edit(output_mode=MODE_HIT_ONLY, hit_duration="0.5")
        self.assertEqual(config.dglab.output.mode, MODE_HIT_ONLY)
        self.assertAlmostEqual(config.dglab.output.hit_duration, 0.5)

    def test_change_channels(self):
        self.assertEqual(
            self.edit(channels="AB").dglab.channels,
            frozenset({Channel.A, Channel.B}),
        )
        self.assertEqual(
            self.edit(channels="B").dglab.channels, frozenset({Channel.B})
        )

    def test_numeric_text_is_accepted(self):
        config = self.edit(strength_per_hp=" 0.35 ", gsi_port="3001")
        self.assertAlmostEqual(config.dglab.policy.strength_per_hp, 0.35)
        self.assertEqual(config.gsi.port, 3001)

    def test_custom_wave(self):
        config = self.edit(
            wave_preset="custom", wave_custom="10, 100\n200, 50\n# 註解會被忽略\n"
        )
        frames = config.dglab.wave.build_frames()
        self.assertEqual([frame.frequency for frame in frames], [10, 120])
        self.assertEqual([frame.strength for frame in frames], [100, 50])

    def test_all_presets_are_selectable(self):
        for preset in PRESET_NAMES:
            with self.subTest(preset=preset):
                values = form_values(self.config)
                values["wave_preset"] = preset
                if preset == "custom":
                    values["wave_custom"] = "10, 100"
                config = config_from_form(self.config, values)
                self.assertTrue(config.dglab.wave.build_frames())


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config("config.toml")

    def bad(self, **changes) -> str:
        values = form_values(self.config)
        values.update(changes)
        with self.assertRaises(ValueError) as ctx:
            config_from_form(self.config, values)
        return str(ctx.exception)

    def test_empty_number(self):
        self.assertIn("不能是空的", self.bad(base_strength=""))

    def test_not_a_number(self):
        self.assertIn("必須是數字", self.bad(base_strength="很多"))

    def test_base_above_max(self):
        self.assertIn("max_strength", self.bad(base_strength="90", max_strength="40"))

    def test_bad_socket_uri(self):
        self.assertIn("ws://", self.bad(socket_uri="http://example.com"))

    def test_bad_port(self):
        self.assertIn("數字", self.bad(gsi_port="abc"))

    def test_empty_custom_wave_is_rejected(self):
        self.assertIn("custom", self.bad(wave_preset="custom", wave_custom=""))

    def test_bad_custom_sample_line(self):
        self.assertIn("頻率, 強度", self.bad(wave_preset="custom", wave_custom="10"))

    def test_custom_sample_out_of_range(self):
        self.assertIn("10 ~ 1000", self.bad(wave_preset="custom", wave_custom="5000, 50"))
        self.assertIn("0 ~ 100", self.bad(wave_preset="custom", wave_custom="10, 200"))

    def test_empty_host(self):
        self.assertIn("不能是空的", self.bad(gsi_host=""))

    def test_stack_range(self):
        message = self.bad(stack_min="50", stack_max="10")
        self.assertIn("minimum", message)

    def test_empty_token_is_allowed(self):
        config = config_from_form(
            self.config, {**form_values(self.config), "gsi_token": ""}
        )
        self.assertEqual(config.gsi.token, "")

    def test_discord_id_must_be_numeric(self):
        message = self.bad(discord_client_id="abc123")
        self.assertIn("純數字", message)

    def test_discord_enabled_requires_id(self):
        message = self.bad(discord_enabled=True, discord_client_id="")
        self.assertIn("Application ID", message)

    def test_discord_disabled_without_id_is_fine(self):
        config = config_from_form(
            self.config,
            {**form_values(self.config), "discord_enabled": False, "discord_client_id": ""},
        )
        self.assertFalse(config.discord.enabled)
        self.assertFalse(config.discord.usable)


class CustomSampleParsingTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(parse_custom_samples("10, 100"), ((10, 100),))

    def test_blank_lines_and_comments(self):
        self.assertEqual(
            parse_custom_samples("\n# 說明\n10, 100\n\n20,50\n"),
            ((10, 100), (20, 50)),
        )

    def test_full_width_comma(self):
        self.assertEqual(parse_custom_samples("10，100"), ((10, 100),))

    def test_empty(self):
        self.assertEqual(parse_custom_samples(""), ())

    def test_float_input_is_rounded_down(self):
        self.assertEqual(parse_custom_samples("10.9, 99.9"), ((10, 99),))


class ConnectionChangeTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config("config.toml")

    def edit(self, **changes):
        values = form_values(self.config)
        values.update(changes)
        return config_from_form(self.config, values)

    def test_no_change(self):
        self.assertEqual(connection_fields_changed(self.config, self.config), [])

    def test_detects_connection_fields(self):
        changed = connection_fields_changed(
            self.config, self.edit(socket_uri="ws://127.0.0.1:9999", gsi_port="3010")
        )
        self.assertIn("Socket 服務端", changed)
        self.assertIn("GSI 連接埠", changed)

    def test_ignores_gameplay_fields(self):
        changed = connection_fields_changed(
            self.config, self.edit(base_strength="33", output_mode=MODE_HIT_ONLY)
        )
        self.assertEqual(changed, [])


if __name__ == "__main__":
    unittest.main()
