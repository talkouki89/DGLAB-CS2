"""config.toml 解析與驗證的測試。"""

from __future__ import annotations

import unittest
from pathlib import Path

from config import ConfigError, DiscordConfig, load_config, parse_config
from coyote.socket_protocol import Channel
from strength import MODE_HIT_ONLY

MINIMAL = {
    "debug": False,
    "gsi": {"host": "127.0.0.1", "port": 3000, "token": "DGLAB"},
    "socket": {"uri": "wss://ws.dungeon-lab.cn/"},
    "dglab": {},
}


def with_dglab(**overrides) -> dict:
    return {**MINIMAL, "dglab": overrides}


def with_socket(**overrides) -> dict:
    return {**MINIMAL, "socket": overrides}


def with_discord(**overrides) -> dict:
    return {**MINIMAL, "discord": overrides}


class DefaultsTests(unittest.TestCase):
    def test_minimal_config_uses_safe_defaults(self):
        config = parse_config(MINIMAL)
        self.assertFalse(config.debug)
        self.assertEqual(config.gsi.port, 3000)
        self.assertEqual(config.socket.uri, "wss://ws.dungeon-lab.cn/")
        self.assertTrue(config.socket.qr_open)
        self.assertEqual(config.dglab.channels, frozenset({Channel.A}))
        self.assertEqual(config.dglab.policy.base_strength, 20)
        self.assertEqual(config.dglab.policy.max_strength, 80)
        self.assertTrue(config.dglab.respect_app_limit)
        self.assertEqual(config.dglab.wave.preset, "steady")
        self.assertTrue(config.dglab.wave.build_frames())

    def test_empty_config_is_valid(self):
        config = parse_config({})
        self.assertEqual(config.gsi.host, "127.0.0.1")
        self.assertEqual(config.dglab.output.mode, "continuous")


class GsiTests(unittest.TestCase):
    def test_empty_token_means_accept_anything(self):
        # 空字串是合法的，代表「接受任何來源」，方便排除 token 問題
        config = parse_config({**MINIMAL, "gsi": {"token": ""}})
        self.assertEqual(config.gsi.token, "")

    def test_table_must_be_table(self):
        with self.assertRaises(ConfigError):
            parse_config({**MINIMAL, "gsi": "127.0.0.1"})


class SocketTests(unittest.TestCase):
    def test_uri_must_be_websocket(self):
        for uri in ("http://example.com", "ws.dungeon-lab.cn", ""):
            with self.subTest(uri=uri):
                with self.assertRaises(ConfigError) as ctx:
                    parse_config(with_socket(uri=uri))
                self.assertIn("socket.uri", str(ctx.exception))

    def test_local_server_uri(self):
        config = parse_config(with_socket(uri="ws://192.168.1.10:9999"))
        self.assertEqual(config.socket.uri, "ws://192.168.1.10:9999")

    def test_png_path_can_be_disabled(self):
        config = parse_config(with_socket(qr_png=""))
        self.assertIsNone(config.socket.png_path)
        config = parse_config(with_socket(qr_png="  my-qr.png  "))
        self.assertEqual(config.socket.png_path, "my-qr.png")

    def test_register_timeout_bounds(self):
        with self.assertRaises(ConfigError):
            parse_config(with_socket(register_timeout=0))

    def test_wrong_type(self):
        with self.assertRaises(ConfigError):
            parse_config(with_socket(qr_open="yes"))


class ChannelTests(unittest.TestCase):
    def test_channel_values(self):
        for value, expected in (
            ("A", {Channel.A}),
            ("b", {Channel.B}),
            ("AB", {Channel.A, Channel.B}),
            ("a b", {Channel.A, Channel.B}),
        ):
            with self.subTest(value=value):
                config = parse_config(with_dglab(channels=value))
                self.assertEqual(config.dglab.channels, frozenset(expected))

    def test_invalid_channel(self):
        with self.assertRaises(ConfigError) as ctx:
            parse_config(with_dglab(channels="C"))
        self.assertIn("channels", str(ctx.exception))


class WaveTests(unittest.TestCase):
    def test_custom_samples(self):
        config = parse_config(
            with_dglab(
                wave={"preset": "custom", "custom_samples": [[10, 100], [200, 50]]}
            )
        )
        frames = config.dglab.wave.build_frames()
        self.assertEqual([frame.frequency for frame in frames], [10, 120])
        self.assertEqual([frame.strength for frame in frames], [100, 50])

    def test_custom_without_samples(self):
        with self.assertRaises(ConfigError) as ctx:
            parse_config(with_dglab(wave={"preset": "custom"}))
        self.assertIn("custom_samples", str(ctx.exception))

    def test_unknown_preset(self):
        with self.assertRaises(ConfigError) as ctx:
            parse_config(with_dglab(wave={"preset": "noise"}))
        self.assertIn("preset", str(ctx.exception))

    def test_official_app_preset_can_be_selected(self):
        config = parse_config(with_dglab(wave={"preset": "app_tide"}))
        self.assertEqual(len(config.dglab.wave.build_frames()), 23)

    def test_bad_custom_sample_shape(self):
        with self.assertRaises(ConfigError):
            parse_config(
                with_dglab(wave={"preset": "custom", "custom_samples": [[10]]})
            )

    def test_custom_frequency_out_of_range(self):
        with self.assertRaises(ConfigError):
            parse_config(
                with_dglab(wave={"preset": "custom", "custom_samples": [[5000, 50]]})
            )


class StrengthTests(unittest.TestCase):
    def test_policy_is_built(self):
        config = parse_config(
            with_dglab(
                base_strength=15,
                strength_per_hp=0.5,
                burning_multiplier=0.4,
                keep_strength_while_not_injured=False,
                max_strength=100,
            )
        )
        policy = config.dglab.policy
        self.assertEqual(policy.base_strength, 15)
        self.assertEqual(policy.strength_per_hp, 0.5)
        self.assertFalse(policy.keep_strength_while_not_injured)
        self.assertEqual(policy.max_strength, 100)
        self.assertEqual(policy.target(100), 0)
        self.assertEqual(policy.target(50), 40)

    def test_base_above_max_rejected(self):
        with self.assertRaises(ConfigError):
            parse_config(with_dglab(base_strength=90, max_strength=50))

    def test_out_of_range_number(self):
        with self.assertRaises(ConfigError):
            parse_config(with_dglab(max_strength=500))

    def test_wrong_type(self):
        with self.assertRaises(ConfigError):
            parse_config(with_dglab(base_strength="很多"))

    def test_boolean_must_be_boolean(self):
        with self.assertRaises(ConfigError):
            parse_config(with_dglab(respect_app_limit="yes"))


class StackTests(unittest.TestCase):
    def test_stack_is_built(self):
        config = parse_config(
            with_dglab(
                stack_enabled=True,
                stack_death_gain=8,
                stack_kill_reduction=3,
                stack_min=-5,
                stack_max=25,
            )
        )
        stack = config.dglab.stack
        self.assertTrue(stack.enabled)
        self.assertEqual(stack.death_gain, 8)
        self.assertEqual(stack.kill_reduction, 3)
        self.assertEqual(stack.minimum, -5)
        self.assertEqual(stack.maximum, 25)

    def test_stack_can_be_disabled(self):
        config = parse_config(with_dglab(stack_enabled=False))
        self.assertFalse(config.dglab.stack.enabled)
        self.assertEqual(config.dglab.stack.offset(), 0.0)

    def test_invalid_range(self):
        with self.assertRaises(ConfigError):
            parse_config(with_dglab(stack_min=50, stack_max=10))


class OutputTests(unittest.TestCase):
    def test_modes(self):
        config = parse_config(with_dglab(output_mode="hit_only", hit_duration=0.5))
        self.assertEqual(config.dglab.output.mode, MODE_HIT_ONLY)
        self.assertAlmostEqual(config.dglab.output.hit_duration, 0.5)

    def test_invalid_mode(self):
        with self.assertRaises(ConfigError) as ctx:
            parse_config(with_dglab(output_mode="sometimes"))
        self.assertIn("output.mode", str(ctx.exception))

    def test_hit_boost(self):
        config = parse_config(with_dglab(hit_boost=10))
        self.assertEqual(config.dglab.output.hit_boost, 10)


class TaserTests(unittest.TestCase):
    def test_defaults(self):
        taser = parse_config(with_dglab()).dglab.taser
        self.assertTrue(taser.enabled)
        self.assertEqual(taser.strength, 50)
        self.assertAlmostEqual(taser.duration, 1.0)
        self.assertEqual(taser.min_damage, 100)

    def test_custom_values(self):
        config = parse_config(
            with_dglab(
                taser_enabled=False,
                taser_strength=65,
                taser_duration=2.5,
                taser_min_damage=80,
            )
        )
        self.assertFalse(config.dglab.taser.enabled)
        self.assertEqual(config.dglab.taser.strength, 65)
        self.assertAlmostEqual(config.dglab.taser.duration, 2.5)
        self.assertEqual(config.dglab.taser.min_damage, 80)

    def test_strength_out_of_range(self):
        with self.assertRaises(ConfigError) as ctx:
            parse_config(with_dglab(taser_strength=250))
        self.assertIn("taser_strength", str(ctx.exception))

    def test_negative_duration(self):
        with self.assertRaises(ConfigError):
            parse_config(with_dglab(taser_duration=-1))


class DeathStopTests(unittest.TestCase):
    def test_defaults(self):
        stop = parse_config(with_dglab()).dglab.death_stop
        self.assertTrue(stop.enabled)
        self.assertAlmostEqual(stop.delay, 5.0)

    def test_custom_values(self):
        config = parse_config(with_dglab(death_stop_enabled=False, death_stop_delay=12.5))
        self.assertFalse(config.dglab.death_stop.enabled)
        self.assertAlmostEqual(config.dglab.death_stop.delay, 12.5)

    def test_negative_delay(self):
        with self.assertRaises(ConfigError):
            parse_config(with_dglab(death_stop_delay=-5))

    def test_shipped_config_enables_both_features(self):
        config = load_config("config.toml")
        self.assertTrue(config.dglab.taser.enabled)
        self.assertTrue(config.dglab.death_stop.enabled)


class DiscordImageTests(unittest.TestCase):
    """Discord 的大圖示／小圖示。"""

    def test_builtin_defaults_are_the_project_author_s(self):
        """內建預設值：不設定也能直接用作者的應用程式與圖片。"""
        discord = DiscordConfig()
        self.assertEqual(discord.client_id, "1560163564839510086")
        self.assertEqual(discord.large_image, "dglad-logo")
        self.assertEqual(discord.small_image, "cs2-logo")
        self.assertTrue(discord.large_text)
        self.assertTrue(discord.small_text)

    def test_shipped_config_uses_the_builtin_defaults(self):
        config = load_config("config.toml")
        self.assertEqual(config.discord.client_id, DiscordConfig().client_id)
        self.assertEqual(config.discord.large_image, "dglad-logo")
        self.assertEqual(config.discord.small_image, "cs2-logo")

    def test_missing_discord_section_falls_back_to_the_defaults(self):
        config = parse_config(MINIMAL)
        self.assertEqual(config.discord.client_id, DiscordConfig().client_id)
        self.assertEqual(config.discord.large_image, DiscordConfig().large_image)

    def test_can_still_be_overridden_with_own_app(self):
        """其他人還是可以換成自己的應用程式與圖片。"""
        config = parse_config(
            with_discord(
                client_id="123456789012345678",
                large_image="my_logo",
                large_text="我的",
                small_image="my_bolt",
                small_text="我的小圖",
            )
        )
        self.assertEqual(config.discord.client_id, "123456789012345678")
        self.assertEqual(config.discord.large_image, "my_logo")
        self.assertEqual(config.discord.small_image, "my_bolt")

    def test_empty_strings_disable_the_images(self):
        config = parse_config(
            with_discord(large_image="", large_text="", small_image="", small_text="")
        )
        self.assertEqual(config.discord.large_image, "")
        self.assertEqual(config.discord.small_image, "")

    def test_images_are_parsed(self):
        config = parse_config(
            with_discord(
                large_image="logo",
                large_text="DGLAB-CS2",
                small_image="bolt",
                small_text="郊狼 3.0",
            )
        )
        self.assertEqual(config.discord.large_image, "logo")
        self.assertEqual(config.discord.large_text, "DGLAB-CS2")
        self.assertEqual(config.discord.small_image, "bolt")
        self.assertEqual(config.discord.small_text, "郊狼 3.0")

    def test_image_key_is_trimmed(self):
        self.assertEqual(
            parse_config(with_discord(large_image="  logo  ")).discord.large_image,
            "logo",
        )

    def test_overly_long_image_key_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            parse_config(with_discord(large_image="x" * 300))
        self.assertIn("large_image", str(ctx.exception))

    def test_overly_long_hover_text_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            parse_config(with_discord(small_text="x" * 200))
        self.assertIn("small_text", str(ctx.exception))

    def test_maximum_lengths_are_accepted(self):
        config = parse_config(
            with_discord(large_image="x" * 256, large_text="x" * 128)
        )
        self.assertEqual(len(config.discord.large_image), 256)


class LoadConfigTests(unittest.TestCase):
    def _scratch_config(self, name: str, data: bytes) -> Path:
        """在 tests/ 底下開一個臨時設定檔並登記清理。

        刻意不用 ``tempfile``：某些受限環境不允許它對系統 temp 目錄 chmod。
        """
        path = Path(__file__).parent / f"_scratch_{name}.toml"
        path.write_bytes(data)
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def test_missing_file(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config("no-such-file.toml")
        self.assertIn("找不到設定檔", str(ctx.exception))

    def test_shipped_config_is_valid(self):
        config = load_config("config.toml")
        self.assertTrue(config.dglab.wave.build_frames())
        self.assertGreater(config.dglab.policy.max_strength, 0)
        self.assertTrue(config.dglab.channels)

    def test_tolerates_utf8_bom(self):
        # 記事本等 Windows 編輯器常會寫入 BOM
        path = self._scratch_config(
            "bom", b"\xef\xbb\xbf" + Path("config.toml").read_bytes()
        )
        config = load_config(path)
        self.assertEqual(config.gsi.token, "DGLAB")

    def test_invalid_toml(self):
        path = self._scratch_config("broken", "this is not toml".encode("utf-8"))
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn("不是合法的 TOML", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
