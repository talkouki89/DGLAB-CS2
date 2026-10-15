"""決策層（:mod:`controller`）的測試。

這一層負責「事件 → 疊加／輸出閘門／強度」與「把值送去裝置」，
CLI 與 GUI 共用，所以行為一定要鎖住。
"""

from __future__ import annotations

import dataclasses
import unittest

from config import AppConfig, DeathStopConfig, DglabConfig, TaserConfig, load_config
from controller import ControlDecision, DeviceWriter, PlayerController
from game_events import GameEvents, PlayerSnapshot
from strength import MODE_HIT_ONLY, OutputGate, StrengthPolicy, StrengthStack
from tests.support import FakeSocketTransport


def snap(health: int = 100, burning: int = 0, kills: int = 0) -> PlayerSnapshot:
    return PlayerSnapshot(health=health, burning=burning, health_known=True)


def config_with(**changes) -> AppConfig:
    base = load_config("config.toml")
    dglab = dataclasses.replace(base.dglab, **changes)
    return dataclasses.replace(base, dglab=dglab)


class Base(unittest.TestCase):
    def make(self, config: AppConfig | None = None) -> PlayerController:
        self.controller = PlayerController(config or load_config("config.toml"))
        return self.controller

    def feed(self, controller: PlayerController, now: float, **kwargs):
        return controller.update(snap(**kwargs), now)


# ------------------------------------------------------------------ 基本行為


class BasicTests(Base):
    def test_strength_follows_health(self):
        controller = self.make()
        controller.update(snap(100), 0.0)
        decision = controller.update(snap(60), 1.0)
        self.assertEqual(decision.strength, 20 + 40 * 0.2)
        self.assertTrue(decision.output_active)

    def test_unknown_health_returns_none(self):
        controller = self.make()
        decision = controller.update(
            PlayerSnapshot(health=50, health_known=False), 0.0
        )
        self.assertIsNone(decision)

    def test_first_sample_is_flagged(self):
        controller = self.make()
        decision = controller.update(snap(100), 0.0)
        self.assertTrue(decision.first_sample)

    def test_death_adds_to_the_stack(self):
        controller = self.make()
        controller.update(snap(100), 0.0)
        decision = controller.update(snap(0), 1.0)
        self.assertTrue(decision.events.died)
        self.assertAlmostEqual(decision.stack, 5.0)

    def test_hit_only_stays_silent_until_hit(self):
        config = config_with(
            output=OutputGate(mode=MODE_HIT_ONLY, hit_duration=0.3)
        )
        controller = self.make(config)
        controller.update(snap(100), 0.0)
        decision = controller.update(snap(100), 1.0)
        self.assertFalse(decision.output_active)
        decision = controller.update(snap(70), 1.1)
        self.assertTrue(decision.output_active)
        self.assertTrue(decision.fire_burst)


# ------------------------------------------------------------- 功能：電擊槍


class TaserTests(Base):
    def taser_config(self, **changes) -> AppConfig:
        taser = TaserConfig(**{"enabled": True, "strength": 50, "duration": 1.0, "min_damage": 100, **changes})
        return config_with(taser=taser)

    def test_big_hit_triggers_the_taser_boost(self):
        controller = self.make(self.taser_config())
        controller.update(snap(100), 0.0)
        decision = controller.update(snap(0), 1.0)  # 一次掉 100 血
        self.assertTrue(decision.taser_active)
        self.assertGreaterEqual(decision.strength, 50)
        self.assertTrue(decision.fire_burst)
        self.assertAlmostEqual(decision.burst_seconds, 1.0)

    def test_small_hit_does_not_trigger(self):
        controller = self.make(self.taser_config())
        controller.update(snap(100), 0.0)
        decision = controller.update(snap(60), 1.0)  # 只掉 40
        self.assertFalse(decision.taser_active)
        self.assertLess(decision.strength, 50)

    def test_boost_expires_after_the_duration(self):
        controller = self.make(self.taser_config())
        controller.update(snap(100), 0.0)
        controller.update(snap(0), 1.0)
        # 期間內仍然有效
        self.assertTrue(controller.update(snap(0), 1.5).taser_active)
        # 過了 1 秒就沒了
        self.assertFalse(controller.update(snap(0), 2.1).taser_active)

    def test_boost_does_not_lower_a_higher_strength(self):
        controller = self.make(
            self.taser_config(strength=30, min_damage=10),
        )
        controller.update(snap(100), 0.0)
        decision = controller.update(snap(20), 1.0)  # 一般強度會到 36
        self.assertTrue(decision.taser_active)
        self.assertGreater(decision.strength, 30)

    def test_boost_respects_max_strength(self):
        controller = self.make(self.taser_config(strength=200, min_damage=10))
        controller.update(snap(100), 0.0)
        decision = controller.update(snap(100), 1.0)
        self.assertLessEqual(decision.strength, controller.max_strength)

    def test_boost_forces_output_in_hit_only_mode(self):
        config = dataclasses.replace(
            self.taser_config(min_damage=10),
            dglab=dataclasses.replace(
                self.taser_config(min_damage=10).dglab,
                output=OutputGate(mode=MODE_HIT_ONLY, hit_duration=0.1),
            ),
        )
        controller = self.make(config)
        controller.update(snap(100), 0.0)
        controller.update(snap(90), 0.2)   # 受擊 -> 0.1 秒的窗
        decision = controller.update(snap(90), 0.5)  # 窗過了，但電擊槍還在
        self.assertTrue(decision.taser_active)
        self.assertTrue(decision.output_active)

    def test_disabled_taser_never_triggers(self):
        controller = self.make(self.taser_config(enabled=False))
        controller.update(snap(100), 0.0)
        self.assertFalse(controller.update(snap(0), 1.0).taser_active)

    def test_zero_threshold_disables_the_check(self):
        controller = self.make(self.taser_config(min_damage=0))
        controller.update(snap(100), 0.0)
        self.assertFalse(controller.update(snap(90), 1.0).taser_active)

    def test_threshold_is_configurable(self):
        controller = self.make(self.taser_config(min_damage=30))
        controller.update(snap(100), 0.0)
        self.assertTrue(controller.update(snap(60), 1.0).taser_active)

    def test_description_mentions_the_taser(self):
        controller = self.make(self.taser_config())
        controller.update(snap(100), 0.0)
        snapshot = snap(0)
        decision = controller.update(snapshot, 1.0)
        self.assertIn("電擊槍", controller.describe(snapshot, decision))


# ------------------------------------------------------- 功能：死亡後歸零


class DeathStopTests(Base):
    def stop_config(self, **changes) -> AppConfig:
        stop = DeathStopConfig(**{"enabled": True, "delay": 5.0, **changes})
        return config_with(death_stop=stop)

    def test_strength_is_kept_right_after_death(self):
        controller = self.make(self.stop_config())
        controller.update(snap(100), 0.0)
        decision = controller.update(snap(0), 1.0)
        self.assertFalse(decision.dead_stopped)
        self.assertGreater(decision.strength, 0)

    def test_strength_is_zeroed_after_the_delay(self):
        controller = self.make(self.stop_config(delay=5.0))
        controller.update(snap(100), 0.0)
        controller.update(snap(0), 1.0)
        decision = controller.update(snap(0), 6.1)
        self.assertTrue(decision.dead_stopped)
        self.assertEqual(decision.strength, 0)
        self.assertFalse(decision.output_active)

    def test_exactly_at_the_delay_it_stops(self):
        controller = self.make(self.stop_config(delay=5.0))
        controller.update(snap(100), 0.0)
        controller.update(snap(0), 1.0)
        self.assertFalse(controller.update(snap(0), 5.9).dead_stopped)
        self.assertTrue(controller.update(snap(0), 6.0).dead_stopped)

    def test_respawn_restores_strength(self):
        controller = self.make(self.stop_config(delay=1.0))
        controller.update(snap(100), 0.0)
        controller.update(snap(0), 1.0)
        self.assertTrue(controller.update(snap(0), 3.0).dead_stopped)
        decision = controller.update(snap(100), 4.0)
        self.assertTrue(decision.events.respawned)
        self.assertFalse(decision.dead_stopped)
        # 復活後強度回來：基礎 20 + 陣亡累積的疊加 5
        self.assertAlmostEqual(decision.stack, 5.0)
        self.assertEqual(decision.strength, 25)

    def test_disabled_never_zeroes(self):
        controller = self.make(self.stop_config(enabled=False, delay=1.0))
        controller.update(snap(100), 0.0)
        controller.update(snap(0), 1.0)
        decision = controller.update(snap(0), 60.0)
        self.assertFalse(decision.dead_stopped)
        self.assertGreater(decision.strength, 0)

    def test_joining_already_dead_still_zeroes(self):
        """程式在死亡後才啟動時，第一筆就是 0 血，也要能歸零。"""
        controller = self.make(self.stop_config(delay=2.0))
        controller.update(snap(0), 0.0)
        self.assertFalse(controller.update(snap(0), 1.0).dead_stopped)
        self.assertTrue(controller.update(snap(0), 2.5).dead_stopped)

    def test_delay_is_configurable(self):
        controller = self.make(self.stop_config(delay=30.0))
        controller.update(snap(100), 0.0)
        controller.update(snap(0), 1.0)
        self.assertFalse(controller.update(snap(0), 10.0).dead_stopped)
        self.assertTrue(controller.update(snap(0), 32.0).dead_stopped)

    def test_zero_delay_stops_immediately(self):
        controller = self.make(self.stop_config(delay=0.0))
        controller.update(snap(100), 0.0)
        self.assertTrue(controller.update(snap(0), 1.0).dead_stopped)

    def test_description_mentions_the_stop(self):
        controller = self.make(self.stop_config(delay=5.0))
        controller.update(snap(100), 0.0)
        controller.update(snap(0), 1.0)
        snapshot = snap(0)
        decision = controller.update(snapshot, 7.0)
        self.assertIn("歸零", controller.describe(snapshot, decision))

    def test_reset_tracking_clears_the_death_timer(self):
        controller = self.make(self.stop_config(delay=1.0))
        controller.update(snap(0), 0.0)
        controller.reset_tracking()
        controller.update(snap(0), 0.5)
        self.assertFalse(controller.update(snap(0), 0.6).dead_stopped)


class ConfigHotSwapTests(Base):
    def test_taser_can_be_switched_on_live(self):
        controller = self.make()
        controller.apply_config(config_with(taser=TaserConfig(enabled=False)))
        controller.update(snap(100), 0.0)
        self.assertFalse(controller.update(snap(0), 1.0).taser_active)

        controller.apply_config(config_with(taser=TaserConfig(enabled=True, min_damage=100)))
        controller.update(snap(100), 2.0)
        self.assertTrue(controller.update(snap(0), 3.0).taser_active)


# ------------------------------------------------------------------ 裝置寫入


class DeviceWriterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncio

        from coyote.socket_client import DglabSocketClient
        from coyote.waveform import WaveFrame

        self.config = load_config("config.toml")
        self.transport = FakeSocketTransport()
        self.transport.feed_initial_bind()
        self.client = DglabSocketClient(
            self.transport,
            uri="wss://example.invalid/",
            channels=self.config.dglab.channels,
            wave_frames=[WaveFrame(10, 100)],
            stream_tick=60.0,
        )
        await self.client.start(register_timeout=1.0)
        # 配對完成才送得出指令
        self.transport.feed_bind_success()
        for _ in range(100):
            if self.client.is_paired:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(self.client.is_paired)
        self.writer = DeviceWriter(self.client, self.config)

    async def asyncTearDown(self):
        await self.client.close()

    def decision(self, **changes) -> ControlDecision:
        base = ControlDecision(
            strength=20,
            output_active=True,
            fire_burst=False,
            events=GameEvents(),
            stack=0.0,
            boost=0.0,
        )
        return dataclasses.replace(base, **changes)

    async def test_only_sends_on_change(self):
        await self.writer.apply(self.decision(strength=20))
        self.assertEqual(len(self.transport.messages_of("strength-1+2+")), 1)
        await self.writer.apply(self.decision(strength=20))
        self.assertEqual(len(self.transport.messages_of("strength-1+2+")), 1)
        await self.writer.apply(self.decision(strength=30))
        self.assertEqual(self.transport.messages_of("strength-1+2+")[-1], "strength-1+2+30")

    async def test_forget_resends(self):
        await self.writer.apply(self.decision(strength=20))
        self.writer.forget()
        await self.writer.apply(self.decision(strength=20))
        self.assertEqual(len(self.transport.messages_of("strength-1+2+")), 2)

    async def test_burst_uses_the_requested_duration(self):
        await self.writer.apply(
            self.decision(fire_burst=True, burst_seconds=1.0)
        )
        self.assertTrue(self.transport.messages_of("clear-1"))
        frames = self.transport.pulse_frames("A")
        # 一個波形單位是 100ms，所以 1 秒 = 10 個單位
        self.assertEqual(len(frames), 10)

    async def test_burst_falls_back_to_hit_duration(self):
        await self.writer.apply(self.decision(fire_burst=True, burst_seconds=None))
        frames = self.transport.pulse_frames("A")
        self.assertGreater(len(frames), 0)


if __name__ == "__main__":
    unittest.main()
