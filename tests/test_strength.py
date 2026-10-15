"""強度策略、強度疊加與輸出閘門的測試。"""

from __future__ import annotations

import unittest

from strength import (
    BURNING_MAX,
    FULL_HEALTH,
    MODE_CONTINUOUS,
    MODE_HIT_ONLY,
    OUTPUT_MODES,
    OutputGate,
    StrengthPolicy,
    StrengthStack,
)


class TargetStrengthTests(unittest.TestCase):
    def setUp(self):
        self.policy = StrengthPolicy(
            base_strength=20,
            strength_per_hp=0.2,
            burning_multiplier=0.2,
            max_strength=200,
        )

    def test_full_health_keeps_base(self):
        self.assertEqual(self.policy.target(100, 0), 20)

    def test_damage_increases_strength(self):
        # 這是原版最嚴重的 bug：受傷時完全不會更新強度
        self.assertEqual(self.policy.target(90, 0), 22)
        self.assertEqual(self.policy.target(50, 0), 30)
        self.assertEqual(self.policy.target(25, 0), 35)

    def test_death_reaches_the_cap(self):
        self.assertEqual(self.policy.target(0, 0), 40)

    def test_strength_is_monotonic_with_damage(self):
        values = [self.policy.target(health) for health in range(100, -1, -1)]
        self.assertEqual(values, sorted(values))
        self.assertEqual(values[0], 20)
        self.assertEqual(values[-1], 40)

    def test_burning_adds_a_percentage(self):
        self.assertEqual(self.policy.target(50, BURNING_MAX), 32)
        self.assertEqual(self.policy.target(50, BURNING_MAX // 2), 31)
        self.assertEqual(self.policy.target(100, BURNING_MAX), 20)

    def test_out_of_range_input_is_clamped(self):
        self.assertEqual(self.policy.target(150), self.policy.target(100))
        self.assertEqual(self.policy.target(-10), self.policy.target(0))
        self.assertEqual(
            self.policy.target(50, 999), self.policy.target(50, BURNING_MAX)
        )

    def test_stack_is_added_on_top(self):
        self.assertEqual(self.policy.target(100, 0, 10), 30)
        self.assertEqual(self.policy.target(50, 0, 10), 40)
        self.assertEqual(self.policy.target(100, 0, -5), 15)

    def test_max_strength_is_a_hard_cap(self):
        policy = StrengthPolicy(base_strength=20, strength_per_hp=5.0, max_strength=80)
        self.assertEqual(policy.target(0, BURNING_MAX), 80)
        self.assertEqual(policy.target(0, 0, stack=100), 80)

    def test_integer_result(self):
        self.assertIsInstance(self.policy.target(73, 41, 3.4), int)

    def test_raw_target_is_a_float(self):
        self.assertAlmostEqual(self.policy.raw_target(50, 0), 30.0)


class KeepStrengthTests(unittest.TestCase):
    def test_disabled_means_zero_at_full_health(self):
        policy = StrengthPolicy(keep_strength_while_not_injured=False)
        self.assertEqual(policy.target(FULL_HEALTH), 0)
        self.assertEqual(policy.target(FULL_HEALTH, BURNING_MAX), 0)

    def test_stack_still_applies_at_full_health(self):
        policy = StrengthPolicy(keep_strength_while_not_injured=False)
        self.assertEqual(policy.target(FULL_HEALTH, 0, stack=20), 20)

    def test_disabled_still_hurts_when_damaged(self):
        # 原版 bug：keep=false 時即使掉血也不會增加強度
        policy = StrengthPolicy(
            base_strength=20, strength_per_hp=0.2, keep_strength_while_not_injured=False
        )
        self.assertEqual(policy.target(99), 20)
        self.assertEqual(policy.target(50), 30)


class ExplainTests(unittest.TestCase):
    def test_explain_mentions_inputs(self):
        text = StrengthPolicy().explain(60, 128, stack=4)
        self.assertIn("血量 60", text)
        self.assertIn("燃燒 128", text)
        self.assertIn("疊加 +4.0", text)

    def test_explain_for_full_health_zero_mode(self):
        policy = StrengthPolicy(keep_strength_while_not_injured=False)
        self.assertIn("0", policy.explain(100, 0))


class PolicyValidationTests(unittest.TestCase):
    def test_negative_values_rejected(self):
        with self.assertRaises(ValueError):
            StrengthPolicy(base_strength=-1)
        with self.assertRaises(ValueError):
            StrengthPolicy(strength_per_hp=-0.1)
        with self.assertRaises(ValueError):
            StrengthPolicy(burning_multiplier=-1)

    def test_max_strength_bounds(self):
        with self.assertRaises(ValueError):
            StrengthPolicy(max_strength=201)
        with self.assertRaises(ValueError):
            StrengthPolicy(max_strength=-1)

    def test_base_above_max_rejected(self):
        with self.assertRaises(ValueError):
            StrengthPolicy(base_strength=90, max_strength=80)


class StrengthStackTests(unittest.TestCase):
    def test_death_raises_and_kill_lowers(self):
        stack = StrengthStack(death_gain=5, kill_reduction=2, maximum=30)
        self.assertEqual(stack.apply_death(), 5)
        self.assertEqual(stack.apply_death(), 10)
        self.assertEqual(stack.apply_kills(), 8)
        self.assertEqual(stack.offset(), 8)

    def test_multiple_at_once(self):
        stack = StrengthStack(death_gain=5, kill_reduction=2)
        self.assertEqual(stack.apply_death(3), 15)
        self.assertEqual(stack.apply_kills(2), 11)

    def test_clamped_to_range(self):
        stack = StrengthStack(death_gain=100, kill_reduction=100, maximum=30)
        self.assertEqual(stack.apply_death(), 30)
        self.assertEqual(stack.apply_kills(), 0)

    def test_minimum_can_be_negative(self):
        stack = StrengthStack(death_gain=5, kill_reduction=10, minimum=-20, maximum=30)
        self.assertEqual(stack.apply_kills(), -10)
        self.assertEqual(stack.apply_kills(), -20)

    def test_disabled_always_zero(self):
        stack = StrengthStack(enabled=False, death_gain=5)
        self.assertEqual(stack.apply_death(), 0.0)
        self.assertEqual(stack.offset(), 0.0)

    def test_reset(self):
        stack = StrengthStack()
        stack.apply_death(4)
        stack.reset()
        self.assertEqual(stack.offset(), 0.0)

    def test_validation(self):
        with self.assertRaises(ValueError):
            StrengthStack(death_gain=-1)
        with self.assertRaises(ValueError):
            StrengthStack(kill_reduction=-1)
        with self.assertRaises(ValueError):
            StrengthStack(minimum=10, maximum=0)


class OutputGateTests(unittest.TestCase):
    def test_continuous_is_always_active(self):
        gate = OutputGate(mode=MODE_CONTINUOUS)
        self.assertTrue(gate.is_active(0))
        self.assertTrue(gate.is_active(10_000))

    def test_hit_only_is_silent_until_hit(self):
        gate = OutputGate(mode=MODE_HIT_ONLY, hit_duration=0.5)
        self.assertFalse(gate.is_active(100))
        gate.on_hit(100)
        self.assertTrue(gate.is_active(100))
        self.assertTrue(gate.is_active(100.4))
        self.assertFalse(gate.is_active(100.5))
        self.assertFalse(gate.is_active(200))

    def test_repeated_hits_extend_the_window(self):
        gate = OutputGate(mode=MODE_HIT_ONLY, hit_duration=0.5)
        gate.on_hit(100)
        gate.on_hit(100.4)
        self.assertTrue(gate.is_active(100.8))
        self.assertEqual(gate.hit_count, 2)

    def test_death_counts_as_a_hit(self):
        gate = OutputGate(mode=MODE_HIT_ONLY, hit_duration=0.5)
        gate.on_death(50)
        self.assertTrue(gate.is_active(50.2))

    def test_hit_boost(self):
        gate = OutputGate(mode=MODE_CONTINUOUS, hit_duration=0.5, hit_boost=15)
        self.assertEqual(gate.boost(100), 0)
        gate.on_hit(100)
        self.assertEqual(gate.boost(100.2), 15)
        self.assertEqual(gate.boost(100.6), 0)

    def test_zero_hit_boost_is_free(self):
        gate = OutputGate(mode=MODE_HIT_ONLY, hit_duration=0.1)
        gate.on_hit(0)
        self.assertEqual(gate.boost(0.05), 0)

    def test_remaining_and_reset(self):
        gate = OutputGate(mode=MODE_HIT_ONLY, hit_duration=1.0)
        gate.on_hit(10)
        self.assertAlmostEqual(gate.remaining(10.25), 0.75)
        gate.reset()
        self.assertEqual(gate.remaining(10.25), 0)
        self.assertEqual(gate.hit_count, 0)

    def test_mode_validation(self):
        self.assertIn(MODE_CONTINUOUS, OUTPUT_MODES)
        self.assertIn(MODE_HIT_ONLY, OUTPUT_MODES)
        with self.assertRaises(ValueError):
            OutputGate(mode="weird")
        with self.assertRaises(ValueError):
            OutputGate(hit_duration=-1)
        with self.assertRaises(ValueError):
            OutputGate(hit_boost=-1)

    def test_mode_is_normalised(self):
        self.assertEqual(OutputGate(mode=" HIT_ONLY ").mode, MODE_HIT_ONLY)


if __name__ == "__main__":
    unittest.main()
