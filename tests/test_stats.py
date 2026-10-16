"""本場統計與連殺獎勵的測試。

兩者都是純資料處理，不需要網路、裝置或 UI。
"""

from __future__ import annotations

import unittest

from stats import MAX_STEP_SECONDS, SessionStats, format_duration
from strength import KillStreak, StrengthStack


class FormatDurationTests(unittest.TestCase):
    def test_minutes_and_seconds(self):
        self.assertEqual(format_duration(0), "0:00")
        self.assertEqual(format_duration(9), "0:09")
        self.assertEqual(format_duration(75), "1:15")

    def test_hours(self):
        self.assertEqual(format_duration(3600), "1:00:00")
        self.assertEqual(format_duration(3725), "1:02:05")

    def test_negative_is_clamped(self):
        self.assertEqual(format_duration(-5), "0:00")


class SessionStatsTests(unittest.TestCase):
    def test_starts_empty(self):
        stats = SessionStats()
        self.assertFalse(stats.has_data)
        self.assertEqual(stats.average_strength, 0.0)
        self.assertEqual(stats.shock_ratio, 0.0)
        self.assertEqual(stats.duration_label, "0:00")

    def test_accumulates_events(self):
        stats = SessionStats()
        stats.record(0.0, strength=20, output_active=True)
        stats.record(0.5, strength=40, output_active=True)
        stats.record(
            1.0, strength=60, output_active=False, damage=32, kills=1, streak=1
        )
        stats.record(1.5, strength=0, output_active=False, died=True, streak=0)

        self.assertTrue(stats.has_data)
        self.assertEqual(stats.hits, 1)
        self.assertEqual(stats.damage_taken, 32)
        self.assertEqual(stats.kills, 1)
        self.assertEqual(stats.deaths, 1)
        self.assertEqual(stats.max_strength, 60)
        self.assertAlmostEqual(stats.average_strength, 30.0)
        self.assertEqual(stats.best_streak, 1)

    def test_shock_seconds_only_counts_active_periods(self):
        stats = SessionStats()
        stats.record(0.0, output_active=True)
        stats.record(1.0, output_active=True)  # 這 1 秒有出力
        stats.record(2.0, output_active=False)  # 這 1 秒沒出力
        self.assertAlmostEqual(stats.shock_seconds, 1.0)
        self.assertAlmostEqual(stats.elapsed_seconds, 2.0)
        self.assertAlmostEqual(stats.shock_ratio, 0.5)

    def test_long_gap_is_clamped(self):
        """GSI 中斷之後第一筆資料不該把通電時間一次灌進去。"""
        stats = SessionStats()
        stats.record(0.0, output_active=True)
        stats.record(600.0, output_active=True)
        self.assertAlmostEqual(stats.elapsed_seconds, MAX_STEP_SECONDS)

    def test_time_going_backwards_is_ignored(self):
        stats = SessionStats()
        stats.record(10.0, output_active=True)
        stats.record(5.0, output_active=True)
        self.assertEqual(stats.elapsed_seconds, 0.0)

    def test_disabled_does_not_accumulate(self):
        stats = SessionStats(enabled=False)
        stats.record(0.0, strength=50, damage=10, kills=2)
        self.assertFalse(stats.has_data)
        self.assertEqual(stats.damage_taken, 0)

    def test_best_streak_keeps_the_maximum(self):
        stats = SessionStats()
        for streak in (1, 2, 3, 1, 2):
            stats.record(0.0, streak=streak)
        self.assertEqual(stats.best_streak, 3)
        self.assertEqual(stats.current_streak, 2)

    def test_kill_death_ratio(self):
        stats = SessionStats()
        self.assertEqual(stats.kill_death_ratio, 0.0)
        stats.record(0.0, kills=3)
        self.assertEqual(stats.kill_death_ratio, 3.0)
        stats.record(0.1, died=True)
        self.assertAlmostEqual(stats.kill_death_ratio, 3.0)

    def test_summary_labels_match_the_gui(self):
        """介面用標籤當索引，所以標籤順序不能亂改。"""
        from gui import DEFAULT_STATS_SUMMARY

        labels = [label for label, _value in SessionStats().summary()]
        gui_labels = [label for label, _placeholder in DEFAULT_STATS_SUMMARY]
        self.assertEqual(labels, gui_labels)

    def test_reset(self):
        stats = SessionStats()
        stats.record(0.0, strength=50, damage=10, kills=2, died=True)
        stats.reset()
        self.assertFalse(stats.has_data)
        self.assertEqual(stats.kills, 0)
        self.assertEqual(stats.deaths, 0)
        self.assertEqual(stats.max_strength, 0)
        self.assertIsNone(stats.started_at)


class KillStreakTests(unittest.TestCase):
    def test_no_bonus_before_the_threshold(self):
        streak = KillStreak(starts_at=2, bonus_per_kill=1.0)
        self.assertEqual(streak.register_kills(), 0.0)
        self.assertEqual(streak.count, 1)

    def test_bonus_grows_with_the_streak(self):
        streak = KillStreak(starts_at=2, bonus_per_kill=1.0, max_bonus=6.0)
        bonuses = [streak.register_kills() for _ in range(7)]
        self.assertEqual(bonuses, [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0])

    def test_bonus_is_capped(self):
        streak = KillStreak(starts_at=2, bonus_per_kill=10.0, max_bonus=3.0)
        streak.register_kills()  # 第 1 殺
        self.assertEqual(streak.register_kills(), 3.0)  # 第 2 殺就被壓到上限
        self.assertEqual(streak.register_kills(), 3.0)

    def test_death_resets_the_streak(self):
        streak = KillStreak(starts_at=2, bonus_per_kill=1.0)
        streak.register_kills(3)
        self.assertEqual(streak.count, 3)
        streak.register_death()
        self.assertEqual(streak.count, 0)
        self.assertEqual(streak.register_kills(), 0.0)

    def test_batch_of_kills(self):
        streak = KillStreak(starts_at=2, bonus_per_kill=1.0, max_bonus=100.0)
        # 一次收到 3 殺：第 1 殺 0、第 2 殺 1、第 3 殺 2
        self.assertEqual(streak.register_kills(3), 3.0)
        self.assertEqual(streak.count, 3)

    def test_disabled(self):
        streak = KillStreak(enabled=False, starts_at=2)
        self.assertEqual(streak.register_kills(5), 0.0)
        self.assertEqual(streak.count, 5)

    def test_next_kill_bonus_preview(self):
        streak = KillStreak(starts_at=2, bonus_per_kill=1.0, max_bonus=6.0)
        self.assertEqual(streak.bonus_for_next_kill(), 0.0)  # 第 1 殺沒有
        streak.register_kills()
        self.assertEqual(streak.bonus_for_next_kill(), 1.0)  # 第 2 殺有 1
        streak.register_kills()
        self.assertEqual(streak.bonus_for_next_kill(), 2.0)  # 第 3 殺有 2

    def test_invalid_settings(self):
        with self.assertRaises(ValueError):
            KillStreak(starts_at=0)
        with self.assertRaises(ValueError):
            KillStreak(bonus_per_kill=-1)
        with self.assertRaises(ValueError):
            KillStreak(max_bonus=-1)

    def test_reset(self):
        streak = KillStreak(starts_at=1)
        streak.register_kills(4)
        streak.reset()
        self.assertEqual(streak.count, 0)


class StackWithStreakBonusTests(unittest.TestCase):
    def test_extra_reduction_is_applied(self):
        stack = StrengthStack(
            death_gain=5.0, kill_reduction=2.0, minimum=-50.0, maximum=30.0
        )
        stack.apply_death(4)  # +20
        self.assertAlmostEqual(stack.value, 20.0)
        stack.apply_kills(1, extra=3.0)
        self.assertAlmostEqual(stack.value, 15.0)

    def test_extra_is_never_negative(self):
        stack = StrengthStack(kill_reduction=2.0, minimum=-50.0)
        stack.value = 0.0
        stack.apply_kills(1, extra=-99.0)
        self.assertAlmostEqual(stack.value, -2.0)

    def test_disabled_stack_ignores_everything(self):
        stack = StrengthStack(enabled=False, kill_reduction=2.0, minimum=-50.0)
        stack.apply_kills(1, extra=10.0)
        self.assertEqual(stack.offset(), 0.0)


if __name__ == "__main__":
    unittest.main()
