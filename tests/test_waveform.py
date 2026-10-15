"""波形數學與 V3 波形單位編碼的測試。"""

from __future__ import annotations

import unittest

from coyote.waveform import (
    FRAME_SECONDS,
    PRESETS,
    PRESET_NAMES,
    SAMPLES_PER_FRAME,
    WAVE_FREQ_MAX,
    WAVE_FREQ_MIN,
    WAVE_STRENGTH_MAX,
    ChannelWave,
    WaveCycle,
    WaveFrame,
    decode_wave_freq,
    encode_wave_freq,
    frame_from_input,
    from_v2,
    pulse_frequency_hz,
    resolve_preset,
)


class FrequencyMappingTests(unittest.TestCase):
    """官方「波形頻率 <-> 實際輸入值」對照表。"""

    def test_official_table(self):
        # 官方波形文件裡的對照表（650ms -> 204 那筆與公式不符，官方文件本身有筆誤，
        # 公式算出來是 205；以下幾筆都能被公式驗證）
        cases = {
            10: 10,
            20: 20,
            50: 50,
            100: 100,
            110: 102,
            150: 110,
            680: 208,
            750: 215,
            1000: 240,
        }
        for period_ms, expected in cases.items():
            with self.subTest(period_ms=period_ms):
                self.assertEqual(encode_wave_freq(period_ms), expected)

    def test_official_example_1(self):
        expected = [240, 180, 146, 130, 120, 113, 108, 105, 102, 100]
        actual = [
            encode_wave_freq(value)
            for value in (1000, 500, 333, 250, 200, 166, 142, 125, 111, 100)
        ]
        self.assertEqual(actual, expected)

    def test_official_example_2(self):
        expected = [100, 120, 140, 160, 180, 200, 210, 220, 230, 240]
        actual = [encode_wave_freq(value) for value in range(100, 1001, 100)]
        self.assertEqual(actual, expected)

    def test_boundaries(self):
        self.assertEqual(encode_wave_freq(0), 10)
        self.assertEqual(encode_wave_freq(-5), 10)
        self.assertEqual(encode_wave_freq(101), 100)
        self.assertEqual(encode_wave_freq(600), 200)
        self.assertEqual(encode_wave_freq(601), 200)
        self.assertEqual(encode_wave_freq(99999), 240)

    def test_round_trip_for_every_device_value(self):
        for value in range(10, 241):
            with self.subTest(value=value):
                self.assertEqual(encode_wave_freq(decode_wave_freq(value)), value)

    def test_pulse_frequency(self):
        self.assertAlmostEqual(pulse_frequency_hz(10), 100.0)
        self.assertAlmostEqual(pulse_frequency_hz(100), 10.0)
        self.assertAlmostEqual(pulse_frequency_hz(200), 1000.0 / 600)


class ChannelWaveTests(unittest.TestCase):
    def test_steady_matches_official_example(self):
        # 官方 example.md 的「呼吸」波形：freq = 10, strength = 100
        self.assertEqual(
            ChannelWave.steady(10, 100).to_hex(), "0A0A0A0A64646464"
        )

    def test_pause_matches_official_example(self):
        # 官方用 0A0A0A0A00000000 代表不輸出
        self.assertEqual(ChannelWave.pause().to_hex(), "0A0A0A0A00000000")
        self.assertTrue(ChannelWave.pause().is_pause)

    def test_from_samples(self):
        wave = ChannelWave.from_samples([(10, 0), (10, 20), (10, 40), (10, 60)])
        self.assertEqual(wave.frequencies, (10, 10, 10, 10))
        self.assertEqual(wave.strengths, (0, 20, 40, 60))
        self.assertEqual(wave.to_hex(), "0A0A0A0A0014283C")

    def test_from_samples_pads_with_last_value(self):
        wave = ChannelWave.from_samples([(10, 50), (20, 60)])
        self.assertEqual(wave.frequencies, (10, 20, 20, 20))
        self.assertEqual(wave.strengths, (50, 60, 60, 60))

    def test_from_samples_truncates(self):
        wave = ChannelWave.from_samples([(10, 1), (20, 2), (30, 3), (40, 4), (50, 5)])
        self.assertEqual(wave.frequencies, (10, 20, 30, 40))

    def test_from_samples_empty_is_pause(self):
        self.assertTrue(ChannelWave.from_samples([]).is_pause)

    def test_official_tide_example(self):
        # 官方 example.md 的「潮汐」第 2 組：freq = 11, strength = 16
        self.assertEqual(ChannelWave.steady(11, 16).to_hex(), "0B0B0B0B10101010")

    def test_to_bytes_length(self):
        self.assertEqual(len(ChannelWave.steady(10, 100).to_bytes()), 8)
        self.assertEqual(SAMPLES_PER_FRAME, 4)

    def test_validation(self):
        with self.assertRaises(ValueError):
            ChannelWave((241, 10, 10, 10), (0, 0, 0, 0))
        with self.assertRaises(ValueError):
            ChannelWave((10, 10, 10), (0, 0, 0, 0))
        with self.assertRaises(ValueError):
            ChannelWave((10, 10, 10, 10), (0, 0, 0, 101))

    def test_describe(self):
        text = ChannelWave.steady(10, 100).describe()
        self.assertIn("100.0Hz", text)
        self.assertIn("波形強度 100", text)


class FromV2Tests(unittest.TestCase):
    """官方說明：V3 波形頻率 = V2 的 (X + Y) 再壓縮；V3 波形強度 = Z * 5。"""

    def test_upstream_default_wave(self):
        frame = from_v2(1, 9, 20)
        self.assertEqual(frame.frequency, 10)
        self.assertEqual(frame.strength, 100)
        self.assertEqual(frame.period_ms, 10)

    def test_pydglab_regression(self):
        # pydglab 的線性近似會把 (5, 35) 算成 16，官方換算應該是 40
        frame = from_v2(5, 35, 20)
        self.assertEqual(frame.frequency, 40)
        self.assertNotEqual(frame.frequency, 16)

    def test_strength_is_scaled_and_clamped(self):
        self.assertEqual(from_v2(1, 9, 0).strength, 0)
        self.assertEqual(from_v2(1, 9, 4).strength, 20)
        self.assertEqual(from_v2(1, 9, 30).strength, WAVE_STRENGTH_MAX)


class WaveFrameTests(unittest.TestCase):
    def test_frame_from_input(self):
        self.assertEqual(frame_from_input(200, 50).frequency, 120)
        self.assertEqual(frame_from_input(1000, 50).frequency, 240)
        self.assertEqual(frame_from_input(10, 999).strength, 100)

    def test_out_of_range_is_rejected(self):
        with self.assertRaises(ValueError):
            WaveFrame(WAVE_FREQ_MIN - 1, 50)
        with self.assertRaises(ValueError):
            WaveFrame(WAVE_FREQ_MAX + 1, 50)
        with self.assertRaises(ValueError):
            WaveFrame(10, WAVE_STRENGTH_MAX + 1)


class PresetTests(unittest.TestCase):
    def test_all_presets_are_valid(self):
        for name, frames in PRESETS.items():
            with self.subTest(preset=name):
                self.assertTrue(frames, "預設集不可為空")
                for frame in frames:
                    self.assertGreaterEqual(frame.frequency, WAVE_FREQ_MIN)
                    self.assertLessEqual(frame.frequency, WAVE_FREQ_MAX)
                    self.assertGreaterEqual(frame.strength, 0)
                    self.assertLessEqual(frame.strength, WAVE_STRENGTH_MAX)

    def test_official_app_presets_exist(self):
        self.assertIn("app_breathe", PRESET_NAMES)
        self.assertIn("app_tide", PRESET_NAMES)
        tide = PRESETS["app_tide"]
        self.assertEqual((tide[0].frequency, tide[0].strength), (10, 0))
        self.assertEqual((tide[1].frequency, tide[1].strength), (11, 16))
        self.assertEqual((tide[6].frequency, tide[6].strength), (19, 100))

    def test_resolve_returns_a_copy(self):
        first = resolve_preset("steady")
        first.clear()
        self.assertTrue(resolve_preset("steady"))

    def test_resolve_is_case_insensitive(self):
        self.assertEqual(resolve_preset(" Steady "), PRESETS["steady"])

    def test_custom_preset(self):
        frames = resolve_preset("custom", [[10, 100], [200, 50]])
        self.assertEqual([frame.frequency for frame in frames], [10, 120])
        self.assertEqual([frame.strength for frame in frames], [100, 50])

    def test_custom_without_samples_raises(self):
        with self.assertRaises(ValueError):
            resolve_preset("custom", [])

    def test_unknown_preset_raises(self):
        with self.assertRaises(ValueError) as ctx:
            resolve_preset("nope")
        self.assertIn("steady", str(ctx.exception))


class WaveCycleTests(unittest.TestCase):
    def test_advances_by_four_samples(self):
        cycle = WaveCycle(
            [WaveFrame(10, 100), WaveFrame(50, 20), WaveFrame(90, 60)]
        )
        first = cycle.next_wave()
        second = cycle.next_wave()
        self.assertEqual(first.frequencies, (10, 50, 90, 10))
        self.assertEqual(second.frequencies, (50, 90, 10, 50))
        self.assertEqual(first.strengths, (100, 20, 60, 100))
        self.assertEqual(second.strengths, (20, 60, 100, 20))

    def test_single_frame_is_steady(self):
        cycle = WaveCycle([WaveFrame(40, 70)])
        self.assertEqual(cycle.next_wave().frequencies, (40, 40, 40, 40))
        self.assertEqual(cycle.next_wave().frequencies, (40, 40, 40, 40))

    def test_empty_cycle_is_pause(self):
        cycle = WaveCycle([])
        self.assertFalse(cycle)
        self.assertTrue(cycle.next_wave().is_pause)

    def test_reset(self):
        cycle = WaveCycle([WaveFrame(10, 100), WaveFrame(20, 50)])
        cycle.next_wave()
        cycle.reset()
        self.assertEqual(cycle.cursor, 0)

    def test_frame_seconds(self):
        self.assertAlmostEqual(FRAME_SECONDS, 0.1)


if __name__ == "__main__":
    unittest.main()
