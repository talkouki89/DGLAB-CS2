"""Discord Rich Presence 服務層的測試。

「顯示什麼」用純函式驗證；「什麼時候送、送失敗怎麼辦」用假時鐘 + 假連線驗證，
完全不需要 Discord，也不會開任何執行緒。
"""

from __future__ import annotations

import dataclasses
import unittest

from config import AppConfig, DiscordConfig, load_config
from engine_status import EngineState, EngineStatus
from presence import DiscordPresence, PresenceView, build_view, describe_phase
from tests.support import FakeDiscordConnector, FakeIPCFactory


def status(**changes) -> EngineStatus:
    base = EngineStatus(state=EngineState.RUNNING, paired=True, health_known=True)
    return dataclasses.replace(base, **changes)


class PhaseTests(unittest.TestCase):
    def phase(self, **changes) -> tuple[str, bool]:
        return describe_phase(status(**changes))

    def test_idle(self):
        self.assertEqual(self.phase(state=EngineState.IDLE), ("待機中", False))

    def test_stopped(self):
        self.assertEqual(self.phase(state=EngineState.STOPPED), ("已停止", False))

    def test_starting(self):
        self.assertEqual(self.phase(state=EngineState.STARTING), ("連線中…", False))

    def test_waiting_for_pairing(self):
        self.assertEqual(
            self.phase(state=EngineState.WAITING_PAIR, paired=False),
            ("等待 App 配對", False),
        )

    def test_running_but_not_paired(self):
        self.assertEqual(self.phase(paired=False), ("等待配對", False))

    def test_running_without_game_data(self):
        self.assertEqual(
            self.phase(gsi_seconds_since=None, health_known=False),
            ("等待 CS2 資料", False),
        )

    def test_playing(self):
        self.assertEqual(self.phase(gsi_seconds_since=0.5), ("CS2 對戰中", True))

    def test_stale_game_data_is_not_playing(self):
        self.assertEqual(self.phase(gsi_seconds_since=30.0), ("等待 CS2 資料", False))

    def test_paused_state(self):
        self.assertEqual(self.phase(state=EngineState.PAUSED), ("已暫停", False))

    def test_paused_flag_wins_over_running(self):
        self.assertEqual(self.phase(paused=True), ("已暫停", False))

    def test_error(self):
        self.assertEqual(self.phase(error="壞了"), ("發生錯誤", False))


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config("config.toml")

    def view(self, config=None, **changes) -> PresenceView:
        return build_view(status(**changes), config or self.config)

    def test_shows_strength_and_health(self):
        view = self.view(strength=42, health=68, stack=0.0)
        self.assertIn("強度 42／80", view.state)
        self.assertIn("血量 68", view.state)

    def test_shows_stack_only_when_non_zero(self):
        self.assertNotIn("疊加", self.view(stack=0.0).state)
        self.assertIn("疊加 +5", self.view(stack=5.0).state)

    def test_unknown_health(self):
        self.assertIn("血量 ？", self.view(health_known=False, strength=10).state)

    def test_toggles(self):
        quiet = dataclasses.replace(
            self.config,
            discord=DiscordConfig(
                enabled=True,
                client_id="1",
                show_health=False,
                show_strength=False,
                show_stack=False,
            ),
        )
        view = self.view(quiet, strength=42, health=68, stack=5.0)
        self.assertEqual(view.state, view.details)

    def test_strength_only(self):
        config = dataclasses.replace(
            self.config,
            discord=DiscordConfig(
                enabled=True, client_id="1", show_health=False, show_stack=False
            ),
        )
        self.assertEqual(self.view(config, strength=33, health=10).state, "強度 33／80")

    def test_max_strength_comes_from_config(self):
        config = dataclasses.replace(
            self.config,
            dglab=dataclasses.replace(
                self.config.dglab,
                max_strength=55,
                policy=dataclasses.replace(self.config.dglab.policy, max_strength=55.0),
            ),
        )
        self.assertIn("強度 20／55", self.view(config, strength=20).state)

    def test_details_is_the_phase(self):
        self.assertEqual(self.view(gsi_seconds_since=0.1).details, "CS2 對戰中")
        self.assertTrue(self.view(gsi_seconds_since=0.1).active)


class PresenceServiceTests(unittest.TestCase):
    """用假時鐘與假連線驗證節流、重連與失敗處理。"""

    def setUp(self):
        self.now = 1000.0
        self.connector = FakeDiscordConnector()
        self.factory = FakeIPCFactory()
        self.logs: list[tuple[str, str]] = []
        self.view = PresenceView(details="CS2 對戰中", state="強度 20／80", active=True)
        self.config = DiscordConfig(enabled=True, client_id="123456789", update_interval=5.0)
        self.presence = DiscordPresence(
            self.config,
            view_source=lambda: self.view,
            connector=self.connector,
            ipc_factory=self.factory,
            on_log=lambda message, level: self.logs.append((level, message)),
            clock=lambda: self.now,
            wall_clock=lambda: 2_000_000.0,
            reconnect_delay=15.0,
        )

    def tick_at(self, seconds: float) -> None:
        self.now += seconds
        self.presence.tick()

    def test_disabled_never_connects(self):
        self.presence.configure(DiscordConfig(enabled=False))
        self.tick_at(1.0)
        self.assertEqual(self.connector.calls, 0)
        self.assertFalse(self.presence.connected)
        self.assertEqual(self.presence.status_text(), "未啟用")

    def test_enabled_without_client_id_does_not_connect(self):
        self.presence.configure(DiscordConfig(enabled=True, client_id=""))
        self.tick_at(1.0)
        self.assertEqual(self.connector.calls, 0)
        self.assertIn("Application ID", self.presence.status_text())
        self.assertTrue(any("Application ID" in m for _, m in self.logs))

    def test_connects_and_sends_activity(self):
        self.presence.tick()
        self.assertTrue(self.presence.connected)
        self.assertEqual(len(self.factory.last.activities), 1)
        self.assertEqual(self.factory.last.activities[0]["details"], "CS2 對戰中")
        self.assertEqual(self.factory.last.application_id, "123456789")

    def test_play_timer_is_included(self):
        self.presence.tick()
        self.assertIn("timestamps", self.factory.last.activities[0])

    def test_configured_images_are_sent(self):
        config = dataclasses.replace(
            self.config,
            large_image="logo",
            large_text="DGLAB-CS2",
            small_image="bolt",
            small_text="郊狼 3.0",
        )
        self.presence.configure(config)
        self.presence.tick()
        assets = self.factory.last.activities[0]["assets"]
        self.assertEqual(assets["large_image"], "logo")
        self.assertEqual(assets["large_text"], "DGLAB-CS2")
        self.assertEqual(assets["small_image"], "bolt")
        self.assertEqual(assets["small_text"], "郊狼 3.0")

    def test_builtin_images_are_sent_by_default(self):
        """預設就帶好圖片，所以不設定也會顯示作者的品牌。"""
        self.presence.tick()
        assets = self.factory.last.activities[0]["assets"]
        self.assertEqual(assets["large_image"], DiscordConfig().large_image)
        self.assertEqual(assets["small_image"], DiscordConfig().small_image)

    def test_images_can_be_disabled(self):
        self.presence.configure(
            dataclasses.replace(self.config, large_image="", small_image="")
        )
        self.presence.tick()
        self.assertNotIn("assets", self.factory.last.activities[0])

    def test_changing_the_image_resends(self):
        self.presence.tick()
        self.presence.configure(
            dataclasses.replace(self.config, large_image="logo")
        )
        self.tick_at(6.0)
        self.assertEqual(
            self.factory.instances[-1].activities[-1]["assets"]["large_image"],
            "logo",
        )

    def test_timer_absent_when_not_playing(self):
        self.view = PresenceView(details="待機中", state="待機中", active=False)
        self.presence.tick()
        self.assertNotIn("timestamps", self.factory.last.activities[0])

    def test_identical_state_is_not_resent(self):
        self.presence.tick()
        self.tick_at(60.0)
        self.assertEqual(len(self.factory.last.activities), 1)

    def test_changed_state_is_throttled(self):
        self.presence.tick()
        self.view = PresenceView(details="CS2 對戰中", state="強度 30／80", active=True)
        self.tick_at(1.0)  # 還沒到 update_interval
        self.assertEqual(len(self.factory.last.activities), 1)
        self.tick_at(5.0)  # 超過了
        self.assertEqual(len(self.factory.last.activities), 2)

    def test_status_text_when_connected(self):
        self.presence.tick()
        self.assertEqual(self.presence.status_text(), "已連線")
        self.assertEqual(self.presence.endpoint, self.connector.path)

    def test_handshake_failure_backs_off(self):
        self.factory = FakeIPCFactory(fail_handshake=True)
        self.presence = DiscordPresence(
            self.config,
            view_source=lambda: self.view,
            connector=self.connector,
            ipc_factory=self.factory,
            on_log=lambda m, l: self.logs.append((l, m)),
            clock=lambda: self.now,
            reconnect_delay=15.0,
        )
        self.presence.tick()
        self.assertFalse(self.presence.connected)
        self.assertIn("未連線", self.presence.status_text())
        # 退避期間內不再嘗試
        self.tick_at(5.0)
        self.assertEqual(self.connector.calls, 1)
        self.tick_at(11.0)
        self.assertEqual(self.connector.calls, 2)

    def test_connection_failure_is_logged_once(self):
        connector = FakeDiscordConnector(failures=99)
        self.presence = DiscordPresence(
            self.config,
            view_source=lambda: self.view,
            connector=connector,
            ipc_factory=self.factory,
            on_log=lambda m, l: self.logs.append((l, m)),
            clock=lambda: self.now,
            reconnect_delay=1.0,
        )
        for _ in range(6):
            self.tick_at(2.0)
        warnings = [m for l, m in self.logs if "連不上 Discord" in m]
        self.assertEqual(len(warnings), 1, "重複的連線錯誤不該一直洗版")

    def test_activity_failure_disconnects_and_retries(self):
        self.factory = FakeIPCFactory(fail_activity=True)
        self.presence = DiscordPresence(
            self.config,
            view_source=lambda: self.view,
            connector=self.connector,
            ipc_factory=self.factory,
            on_log=lambda m, l: self.logs.append((l, m)),
            clock=lambda: self.now,
            reconnect_delay=15.0,
        )
        self.presence.tick()
        self.assertFalse(self.presence.connected)
        self.assertTrue(self.factory.last.closed)
        self.tick_at(16.0)
        self.assertEqual(len(self.factory.instances), 2)

    def test_disabling_closes_the_connection(self):
        self.presence.tick()
        self.assertTrue(self.presence.connected)
        self.presence.configure(DiscordConfig(enabled=False))
        self.assertFalse(self.presence.connected)
        self.assertTrue(self.factory.last.closed)

    def test_changing_client_id_forces_a_resend(self):
        self.presence.tick()
        self.presence.configure(
            DiscordConfig(enabled=True, client_id="999999", update_interval=5.0)
        )
        self.tick_at(6.0)
        self.assertEqual(self.factory.instances[-1].application_id, "999999")

    def test_close_clears_the_connection(self):
        self.presence.tick()
        self.presence.close()
        self.assertFalse(self.presence.connected)
        self.assertTrue(self.factory.last.closed)

    def test_source_is_only_called_when_needed(self):
        """狀態是用拉的，而且 tick 一次只拉一次。"""
        calls = []
        self.presence = DiscordPresence(
            self.config,
            view_source=lambda: (calls.append(1), self.view)[1],
            connector=self.connector,
            ipc_factory=self.factory,
            clock=lambda: self.now,
        )
        self.presence.tick()
        self.assertEqual(len(calls), 1)

    def test_thread_can_start_and_stop(self):
        self.presence.start()
        self.presence.start()  # 重複呼叫沒關係
        self.presence.stop()
        self.presence.stop()

    def test_errors_never_escape_tick(self):
        def explode():
            raise RuntimeError("壞掉的狀態來源")

        self.presence = DiscordPresence(
            self.config,
            view_source=explode,
            connector=self.connector,
            ipc_factory=self.factory,
            clock=lambda: self.now,
        )
        with self.assertRaises(RuntimeError):
            self.presence.tick()
        # 但背景執行緒會把它吃掉
        self.presence.start()
        self.presence.stop()


class WireIntegrationTests(unittest.TestCase):
    """真的走一遍 DiscordPresence → DiscordIPC → 假管線，確認序列化結果。"""

    def build(self, config: DiscordConfig):
        from discord_rpc import DiscordIPC
        from tests.support import FakeDiscordPipe

        pipe = FakeDiscordPipe()
        ipc = DiscordIPC(pipe, application_id=config.client_id, timeout=5)
        ipc.handshake()
        presence = DiscordPresence(
            config,
            view_source=lambda: PresenceView(
                details="CS2 對戰中", state="強度 42／80", active=True
            ),
            connector=lambda: (object(), "fake"),
            ipc_factory=lambda connection, *, application_id: ipc,
            clock=lambda: 1000.0,
            wall_clock=lambda: 2_000_000.0,
        )
        presence.tick()
        return pipe

    def test_activity_with_images_reaches_the_wire(self):
        pipe = self.build(
            DiscordConfig(
                enabled=True,
                client_id="123",
                large_image="logo",
                large_text="DGLAB-CS2",
                small_image="bolt",
                small_text="郊狼 3.0",
            )
        )
        activity = pipe.activities[-1]
        self.assertEqual(activity["details"], "CS2 對戰中")
        self.assertEqual(activity["state"], "強度 42／80")
        self.assertEqual(activity["assets"]["large_image"], "logo")
        self.assertEqual(activity["assets"]["large_text"], "DGLAB-CS2")
        self.assertEqual(activity["assets"]["small_image"], "bolt")
        self.assertEqual(activity["assets"]["small_text"], "郊狼 3.0")
        self.assertIn("timestamps", activity)

    def test_activity_without_images_has_no_assets(self):
        pipe = self.build(
            DiscordConfig(
                enabled=True,
                client_id="123",
                large_image="",
                small_image="",
                large_text="",
                small_text="",
            )
        )
        self.assertNotIn("assets", pipe.activities[-1])

    def test_builtin_images_reach_the_wire(self):
        """完全不設定時送出的就是內建的品牌圖片。"""
        builtin = DiscordConfig(enabled=True)
        pipe = self.build(builtin)
        assets = pipe.activities[-1]["assets"]
        self.assertEqual(assets["large_image"], builtin.large_image)
        self.assertEqual(assets["small_image"], builtin.small_image)


class ConfigIntegrationTests(unittest.TestCase):
    def test_discord_section_is_parsed(self):
        config = load_config("config.toml")
        self.assertIsInstance(config.discord, DiscordConfig)

    def test_usable_requires_both(self):
        self.assertFalse(DiscordConfig().usable, "預設沒有開啟")
        self.assertFalse(DiscordConfig(enabled=True, client_id="").usable)
        self.assertFalse(DiscordConfig(client_id="1").usable)
        self.assertTrue(DiscordConfig(enabled=True, client_id="1").usable)
        self.assertTrue(DiscordConfig(enabled=True, client_id="1").missing_client_id is False)
        self.assertTrue(DiscordConfig(enabled=True, client_id="").missing_client_id)

    def test_app_config_default_is_disabled(self):
        self.assertFalse(AppConfig().discord.enabled)

    def test_app_config_defaults_to_the_builtin_application(self):
        """不設定也能用：預設就帶好作者自己的 Application ID 與圖片。"""
        discord = AppConfig().discord
        self.assertTrue(discord.client_id)
        self.assertTrue(discord.large_image)
        self.assertTrue(discord.small_image)


if __name__ == "__main__":
    unittest.main()
