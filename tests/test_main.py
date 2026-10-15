"""主程式層級的測試：命令列覆寫、以及「CS2 狀態 → Socket 指令」的端到端整合。"""

from __future__ import annotations

import asyncio
import json
import time
import unittest
import urllib.request

import main
from config import AppConfig, DglabConfig, GsiConfig
from coyote.socket_client import DglabSocketClient
from coyote.socket_protocol import Channel
from coyote.waveform import WaveFrame
from gsi_server import GSIServer, serve_forever
from strength import MODE_CONTINUOUS, MODE_HIT_ONLY, OutputGate, StrengthPolicy, StrengthStack
from tests.support import FakeSocketTransport
from tests.test_gsi import HEARTBEAT_PAYLOAD, payload_with

URI = "wss://example.invalid/"


class CommandLineTests(unittest.TestCase):
    def parse(self, *argv):
        args = main.build_argument_parser().parse_args(list(argv))
        return main.apply_overrides(main.load_config("config.toml"), args)

    def test_socket_uri_override(self):
        config = self.parse("--socket-uri", "ws://192.168.1.5:9999")
        self.assertEqual(config.socket.uri, "ws://192.168.1.5:9999")

    def test_bad_socket_uri_is_rejected(self):
        from config import ConfigError

        with self.assertRaises(ConfigError):
            self.parse("--socket-uri", "http://example.com")

    def test_channels_override(self):
        self.assertEqual(
            self.parse("--channels", "B").dglab.channels, frozenset({Channel.B})
        )
        self.assertEqual(
            self.parse("--channels", "AB").dglab.channels,
            frozenset({Channel.A, Channel.B}),
        )

    def test_output_mode_override(self):
        self.assertEqual(
            self.parse("--output-mode", "hit_only").dglab.output.mode, MODE_HIT_ONLY
        )

    def test_strength_overrides(self):
        config = self.parse(
            "--base-strength", "12", "--max-strength", "40", "--strength-per-hp", "1.5"
        )
        self.assertEqual(config.dglab.policy.base_strength, 12)
        self.assertEqual(config.dglab.policy.max_strength, 40)
        self.assertEqual(config.dglab.policy.strength_per_hp, 1.5)

    def test_wave_override(self):
        config = self.parse("--wave", "breathe")
        self.assertEqual(config.dglab.wave.preset, "breathe")
        self.assertGreater(len(config.dglab.wave.build_frames()), 1)

    def test_invalid_combination_is_rejected(self):
        from config import ConfigError

        with self.assertRaises(ConfigError):
            self.parse("--base-strength", "90", "--max-strength", "40")

    def test_unknown_wave_is_rejected(self):
        from config import ConfigError

        with self.assertRaises(ConfigError):
            self.parse("--wave", "noise")

    def test_missing_config_returns_error_code(self):
        self.assertEqual(main.main(["--config", "definitely-not-here.toml"]), 2)


class BuilderTests(unittest.TestCase):
    def test_describe_wave(self):
        config = AppConfig(
            dglab=DglabConfig(wave=main.WaveConfig(preset="steady"))
        )
        self.assertIn("steady", main.describe_wave(config))
        self.assertIn("Hz", main.describe_wave(config))

    def test_build_client_uses_the_preset(self):
        config = AppConfig(
            dglab=DglabConfig(
                channels=frozenset({Channel.A, Channel.B}),
                wave=main.WaveConfig(preset="steady"),
            )
        )
        client = main.build_client(FakeSocketTransport(), config)
        self.assertEqual(client.channels, frozenset({Channel.A, Channel.B}))
        self.assertIn("steady", main.describe_wave(config))

    def test_build_transport_dry_run(self):
        config = AppConfig()
        transport, task = main.build_transport(config, dry_run=True)
        self.assertIn("dry-run", transport.description)
        # dry-run 會排入「服務端分配 clientId」的假訊息
        self.assertTrue(task is not None)
        task.close()

    def test_apply_overrides_rebuilds_the_policy(self):
        config = AppConfig(
            dglab=DglabConfig(policy=StrengthPolicy(base_strength=20, max_strength=80))
        )
        args = main.build_argument_parser().parse_args(["--max-strength", "55"])
        updated = main.apply_overrides(config, args)
        self.assertEqual(updated.dglab.policy.max_strength, 55)


class ControlLoopTestCase(unittest.IsolatedAsyncioTestCase):
    """真的跑一次 CS2 GSI POST -> control_loop -> Socket 指令 的完整鏈路。"""

    OUTPUT_MODE = MODE_CONTINUOUS
    HIT_DURATION = 0.4
    HIT_BOOST = 0.0
    STACK_DEATH = 5.0
    STACK_KILL = 2.0
    WATCHDOG = 0.0

    async def asyncSetUp(self):
        self.server = GSIServer(GsiConfig(host="127.0.0.1", port=0, token="DGLAB"))
        self.thread = serve_forever(self.server)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"

        self.transport = FakeSocketTransport()
        self.transport.feed_initial_bind()
        self.client = DglabSocketClient(
            self.transport,
            uri=URI,
            channels=(Channel.A,),
            wave_frames=[WaveFrame(10, 100)],
            stream_tick=60.0,
        )
        await self.client.start(register_timeout=1.0)
        self.transport.feed_bind_success()
        for _ in range(100):
            if self.client.is_paired:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(self.client.is_paired, "測試用的假 App 應該要配對成功")

        self.config = AppConfig(
            dglab=DglabConfig(
                channels=frozenset({Channel.A}),
                max_strength=100,
                poll_interval=0.02,
                stop_when_no_data_seconds=self.WATCHDOG,
                policy=StrengthPolicy(
                    base_strength=20, strength_per_hp=0.5, max_strength=100
                ),
                stack=StrengthStack(
                    enabled=True,
                    death_gain=self.STACK_DEATH,
                    kill_reduction=self.STACK_KILL,
                    minimum=0,
                    maximum=100,
                ),
                output=OutputGate(
                    mode=self.OUTPUT_MODE,
                    hit_duration=self.HIT_DURATION,
                    hit_boost=self.HIT_BOOST,
                ),
            )
        )

        main._STOP.clear()
        self.unpaired = asyncio.Event()
        self.task = asyncio.create_task(
            main.control_loop(self.server, self.client, self.config, self.unpaired)
        )

    async def asyncTearDown(self):
        main._STOP.set()
        try:
            await asyncio.wait_for(self.task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self.task.cancel()
        await self.client.close()
        # shutdown() 會阻塞，丟到執行緒才不會卡住事件迴圈
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        self.thread.join(timeout=5)

    async def post(self, payload) -> int:
        def _send() -> int:
            request = urllib.request.Request(
                self.url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status

        return await asyncio.to_thread(_send)

    async def wait_for(self, predicate, timeout: float = 3.0, message: str = "") -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.02)
        if message:
            self.fail(f"{message} | DEBUG {self._debug_state()}")
        return False

    def _debug_state(self) -> str:
        done = self.task.done()
        exc = self.task.exception() if done and not self.task.cancelled() else "n/a"
        return (
            f"stop={main._STOP.is_set()} task_done={done} task_exc={exc!r} "
            f"running={self.client.running} unpaired={self.unpaired.is_set()} "
            f"payloads={self.server.payload_count} "
            f"strength={self.client.target_strength(Channel.A)}"
        )

    async def wait_strength(self, value: int, timeout: float = 3.0) -> bool:
        return await self.wait_for(
            lambda: self.client.target_strength(Channel.A) == value,
            timeout,
            f"等了 {timeout} 秒強度還是沒有變成 {value}"
            f"（目前 {self.client.target_strength(Channel.A)}）",
        )


class SpectatingTests(ControlLoopTestCase):
    """死亡後觀戰別人的完整鏈路。

    這是使用者回報的兩個症狀：

    1. 觀戰別人時血量變成對方的 → 被誤判成「我復活了」。
    2. 對方的每回合擊殺數變成基準點 → 復活後自己的擊殺被漏掉。
    """

    #: 被觀戰的人（和 provider.steamid 不同就代表這包不是我的）
    OTHER_STEAMID = "76561198000000001"

    async def spectate(self, health: int, kills: int = 0) -> None:
        payload = payload_with(health, kills=kills)
        payload["player"]["steamid"] = self.OTHER_STEAMID
        await self.post(payload)

    async def die(self) -> int:
        """讓自己陣亡，回傳死亡當下的強度。"""
        await self.post(payload_with(100))
        self.assertTrue(await self.wait_strength(20))
        await self.post(payload_with(0))
        self.assertTrue(
            await self.wait_for(
                lambda: self.client.target_strength(Channel.A) > 20,
                message="陣亡後強度應該提高",
            )
        )
        return self.client.target_strength(Channel.A)

    async def test_spectating_does_not_change_my_strength(self):
        dead_strength = await self.die()
        for health in (100, 12, 50, 100):
            await self.spectate(health)
            await asyncio.sleep(0.2)
            self.assertEqual(
                self.client.target_strength(Channel.A),
                dead_strength,
                f"觀戰別人（他 {health} 血）時，我的強度不該跟著變",
            )

    async def test_spectating_is_not_treated_as_respawning(self):
        dead_strength = await self.die()
        await self.spectate(100)
        await asyncio.sleep(0.3)
        # 如果被誤判成復活，疊加／強度都會回到滿血狀態（20）
        self.assertNotEqual(self.client.target_strength(Channel.A), 20)
        self.assertEqual(self.client.target_strength(Channel.A), dead_strength)

    async def test_real_respawn_after_spectating_works(self):
        await self.die()
        await self.spectate(100)
        await asyncio.sleep(0.2)
        await self.post(payload_with(100))  # 真的復活
        # 基礎 20 + 陣亡一次累積的 5
        self.assertTrue(
            await self.wait_strength(20 + int(self.STACK_DEATH)),
            "復活後應該用自己的血量重新計算",
        )

    async def test_own_kill_after_spectating_is_counted(self):
        """使用者回報的「有時候擊殺沒被接收到」。"""
        await self.die()
        # 被觀戰的人拿了 3 殺，那不算我的
        await self.spectate(90, kills=3)
        await asyncio.sleep(0.2)
        await self.post(payload_with(100, kills=0))  # 復活
        revived = 20 + int(self.STACK_DEATH)
        self.assertTrue(await self.wait_strength(revived))

        # 自己的第一殺一定要算到
        await self.post(payload_with(100, kills=1))
        self.assertTrue(
            await self.wait_strength(revived - int(self.STACK_KILL)),
            "復活後的第一個擊殺應該要降低疊加",
        )

    async def test_spectated_kills_do_not_reduce_the_stack(self):
        dead_strength = await self.die()
        for kills in (1, 2, 3):
            await self.spectate(80, kills=kills)
            await asyncio.sleep(0.15)
        self.assertEqual(
            self.client.target_strength(Channel.A),
            dead_strength,
            "別人拿的擊殺不該降低我的疊加",
        )


class ContinuousModeTests(ControlLoopTestCase):
    async def test_strength_follows_damage_and_healing(self):
        await self.post(payload_with(100))
        self.assertTrue(await self.wait_strength(20), "滿血應該是基礎強度 20")

        await self.post(payload_with(60))
        self.assertTrue(await self.wait_strength(40), "掉 40 滴血應該是 20 + 40*0.5 = 40")

        await self.post(payload_with(100))
        self.assertTrue(await self.wait_strength(20))

    async def test_burning_adds_strength(self):
        await self.post(payload_with(100))
        await self.wait_strength(20)
        await self.post(payload_with(80, burning=255))
        # 20 + (100-80)*0.5*1.2 = 32
        self.assertTrue(await self.wait_strength(32))

    async def test_output_is_always_active(self):
        await self.post(payload_with(100))
        self.assertTrue(await self.wait_strength(20))
        self.assertTrue(self.client.is_output_active(Channel.A))

    async def test_heartbeat_alone_does_not_energize(self):
        await self.post(HEARTBEAT_PAYLOAD)
        await asyncio.sleep(0.2)
        self.assertEqual(self.client.target_strength(Channel.A), 0)

    async def test_never_exceeds_the_cap(self):
        await self.post(payload_with(1, burning=255))
        await asyncio.sleep(0.2)
        self.assertLessEqual(self.client.target_strength(Channel.A), 100)


class StackTests(ControlLoopTestCase):
    async def test_death_adds_and_respawn_keeps_the_stack(self):
        await self.post(payload_with(100, kills=0))
        self.assertTrue(await self.wait_strength(20))

        # 陣亡：20 + 100*0.5 + 5 = 75
        await self.post(payload_with(0, kills=0))
        self.assertTrue(await self.wait_strength(75), "死亡應該讓強度疊加 +5")

        # 復活：疊加留著，所以是 20 + 5 = 25
        await self.post(payload_with(100, kills=0))
        self.assertTrue(await self.wait_strength(25), "復活後疊加應該保留")

    async def test_kill_reduces_the_stack(self):
        # 每一步都要等到 control_loop 真的取樣到，否則兩筆 POST 會被合併成一次
        await self.post(payload_with(100, kills=0))
        self.assertTrue(await self.wait_strength(20))

        await self.post(payload_with(0, kills=0))
        self.assertTrue(await self.wait_strength(75))

        await self.post(payload_with(100, kills=0))
        self.assertTrue(await self.wait_strength(25))

        # 擊殺 1 個：疊加 5 - 2 = 3，所以是 20 + 3 = 23
        await self.post(payload_with(100, kills=1))
        self.assertTrue(await self.wait_strength(23), "擊殺應該讓強度疊加降低")

    async def test_stack_is_clamped(self):
        await self.post(payload_with(100, kills=0))
        # 連續陣亡 20 次，疊加上限 100 但強度上限也是 100
        for _ in range(20):
            await self.post(payload_with(0, kills=0))
            await self.post(payload_with(100, kills=0))
        await asyncio.sleep(0.2)
        self.assertLessEqual(self.client.target_strength(Channel.A), 100)
        self.assertLessEqual(self.config.dglab.stack.offset(), 100)


class HitOnlyModeTests(ControlLoopTestCase):
    OUTPUT_MODE = MODE_HIT_ONLY
    HIT_DURATION = 0.4

    async def test_silent_until_hit(self):
        await self.post(payload_with(100))
        self.assertTrue(await self.wait_strength(20))
        self.assertFalse(
            self.client.is_output_active(Channel.A),
            "hit_only 模式下不受擊就不應該輸出",
        )

    async def test_hit_activates_output_then_goes_quiet(self):
        await self.post(payload_with(100))
        await self.wait_strength(20)

        await self.post(payload_with(70))
        self.assertTrue(
            await self.wait_for(
                lambda: self.client.is_output_active(Channel.A),
                message="受擊後應該立刻開始輸出",
            )
        )
        self.assertTrue(await self.wait_strength(35))

        # 時間窗過後要自己安靜下來
        self.assertTrue(
            await self.wait_for(
                lambda: not self.client.is_output_active(Channel.A),
                timeout=2.0,
                message="受擊時間窗過後應該停止輸出",
            )
        )

    async def test_death_also_triggers_output(self):
        await self.post(payload_with(100))
        await self.wait_strength(20)
        await self.post(payload_with(0))
        self.assertTrue(
            await self.wait_for(
                lambda: self.client.is_output_active(Channel.A),
                message="陣亡也應該觸發一次輸出",
            )
        )

    async def test_burst_frames_are_pushed(self):
        await self.post(payload_with(100))
        await self.wait_strength(20)
        before = len(self.transport.messages_of("clear-1"))
        await self.post(payload_with(50))
        self.assertTrue(
            await self.wait_for(
                lambda: len(self.transport.messages_of("clear-1")) > before,
                message="受擊時應該先清空佇列再補上爆發波形",
            )
        )
        self.assertTrue(self.transport.pulse_frames("A"))


class HitBoostTests(ControlLoopTestCase):
    """continuous 模式下的受擊加成（不繼承 hit_only 的測試）。"""

    OUTPUT_MODE = MODE_CONTINUOUS
    HIT_DURATION = 0.4
    HIT_BOOST = 10.0

    async def test_boost_is_applied_during_the_window(self):
        await self.post(payload_with(100))
        self.assertTrue(await self.wait_strength(20))

        await self.post(payload_with(80, burning=0))
        # 20 + 20*0.5 + 10 = 40
        self.assertTrue(await self.wait_strength(40))

        # 時間窗過後回到沒有加成的值
        self.assertTrue(await self.wait_strength(30, timeout=3.0))

    async def test_mode_is_continuous_so_output_stays_on(self):
        await self.post(payload_with(100))
        self.assertTrue(await self.wait_strength(20))
        self.assertTrue(self.client.is_output_active(Channel.A))

    async def test_no_extra_burst_in_continuous_mode(self):
        await self.post(payload_with(100))
        await self.wait_strength(20)
        clears_before = len(self.transport.messages_of("clear-1"))
        await self.post(payload_with(60))
        await self.wait_strength(40)
        self.assertEqual(
            len(self.transport.messages_of("clear-1")),
            clears_before,
            "continuous 模式不應該因為受擊而清空佇列",
        )


class WatchdogTests(ControlLoopTestCase):
    WATCHDOG = 0.05

    async def test_watchdog_zeroes_strength_when_data_stops(self):
        await self.post(payload_with(60))
        self.assertTrue(await self.wait_strength(40))
        self.assertTrue(
            await self.wait_strength(0, timeout=3.0),
            "沒有 GSI 資料時強度必須自動歸零",
        )
        self.assertFalse(self.client.is_output_active(Channel.A))


if __name__ == "__main__":
    unittest.main()
