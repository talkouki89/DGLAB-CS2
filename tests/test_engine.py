"""執行引擎的測試。

引擎跑在自己的執行緒裡，所以這裡用假的 WebSocket 傳輸層 + 真的 GSI HTTP POST
來驗證整條鏈路，不需要網路、不需要 App、也不需要郊狼。
"""

from __future__ import annotations

import dataclasses
import json
import socket
import time
import unittest
import urllib.request
from unittest import mock

from config import AppConfig, DglabConfig, GsiConfig
from engine import ControlEngine, EngineState, build_client, describe_wave
from coyote.socket_protocol import Channel
from strength import MODE_HIT_ONLY, OutputGate, StrengthPolicy
from tests.support import FakeSocketTransport
from tests.test_gsi import HEARTBEAT_PAYLOAD, payload_with


def free_port() -> int:
    """要一個目前空閒的埠。

    用 0 讓作業系統指派也行，但這裡先取得號碼才能組出 GSI 的 URL。
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def post_gsi(address: str, payload) -> int:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"http://{address}/", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status


class EngineTestCase(unittest.TestCase):
    """共用的啟動／等待工具。"""

    HIT_ONLY = False
    POLL_INTERVAL = 0.05

    def setUp(self):
        self.transport = FakeSocketTransport()
        self.transport.feed_initial_bind()
        self.port = free_port()
        self.config = AppConfig(
            gsi=GsiConfig(host="127.0.0.1", port=self.port, token="DGLAB"),
            dglab=DglabConfig(
                channels=frozenset({Channel.A}),
                max_strength=80,
                poll_interval=self.POLL_INTERVAL,
                stop_when_no_data_seconds=0.0,
                policy=StrengthPolicy(
                    base_strength=20, strength_per_hp=0.5, max_strength=80
                ),
                output=OutputGate(
                    mode=MODE_HIT_ONLY if self.HIT_ONLY else "continuous",
                    hit_duration=0.3,
                ),
            ),
        )
        self.logs: list[tuple[str, str]] = []
        self.engine = ControlEngine(
            self.config,
            dry_run=True,
            on_log=lambda message, level: self.logs.append((level, message)),
        )
        # 換掉傳輸層，測試才能自己扮演服務端與 App
        patcher = mock.patch(
            "engine.build_transport",
            return_value=(self.transport, None),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.engine.stop(timeout=10)

    # --------------------------------------------------------------- 輔助

    @property
    def status(self):
        return self.engine.status

    def address(self) -> str:
        return self.status.gsi_address

    def wait_for(self, predicate, timeout: float = 10.0, message: str = "") -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        if message:
            self.fail(f"{message}（目前狀態：{self.status}）")
        return False

    def start_and_pair(self) -> None:
        self.engine.start()
        self.assertTrue(
            self.wait_for(
                lambda: self.status.gsi_address.endswith(str(self.port)),
                message="GSI 伺服器沒有起來",
            )
        )
        self.assertTrue(
            self.wait_for(
                lambda: self.status.state == EngineState.WAITING_PAIR,
                message="引擎沒有進入等待配對",
            )
        )
        self.transport.feed_bind_success()
        self.assertTrue(
            self.wait_for(
                lambda: self.status.state == EngineState.RUNNING,
                message="配對後沒有進入執行中",
            )
        )

    def wait_strength(self, value: int, timeout: float = 8.0) -> bool:
        return self.wait_for(
            lambda: self.engine.status.strength == value
            and self.engine.status.output_active,
            timeout,
            message=f"強度沒有變成 {value}",
        )

    def wait_first_sample(self, health: int = 100, timeout: float = 8.0) -> bool:
        """等到控制迴圈真的取樣過第一筆資料。

        注意不能只用 ``status.health == 100`` 判斷 —— 那是預設值，
        會在第一筆資料進來之前就成立，後面的掉血就不會被算成「受擊」。
        """
        return self.wait_for(
            lambda: self.engine.status.health_known
            and self.engine.status.health == health,
            timeout,
            message=f"控制迴圈沒有取樣到血量 {health}",
        )


class EventDrivenTests(EngineTestCase):
    """收到 GSI 資料要立刻處理，不能等下一次輪詢。

    這裡故意把輪詢間隔設成 2 秒：如果還在用固定輪詢，下面這些斷言都會逾時。
    """

    POLL_INTERVAL = 2.0

    def test_gsi_payload_wakes_the_control_loop_immediately(self):
        self.start_and_pair()
        started = time.monotonic()
        post_gsi(self.address(), payload_with(100))
        self.assertTrue(
            self.wait_strength(20, timeout=1.5),
            "收到資料就該立刻處理（輪詢間隔是 2 秒，等輪詢一定會逾時）",
        )
        self.assertLess(time.monotonic() - started, 1.5)

    def test_single_hit_reacts_within_a_poll_interval(self):
        self.start_and_pair()
        post_gsi(self.address(), payload_with(100))
        self.assertTrue(self.wait_strength(20, timeout=1.5))

        started = time.monotonic()
        post_gsi(self.address(), payload_with(60))
        self.assertTrue(self.wait_strength(40, timeout=1.5))
        self.assertLess(
            time.monotonic() - started,
            self.POLL_INTERVAL,
            "受擊的反應時間必須小於輪詢間隔，才代表是事件驅動",
        )

    def test_stop_does_not_wait_for_the_poll_interval(self):
        self.start_and_pair()
        post_gsi(self.address(), payload_with(100))
        self.assertTrue(self.wait_strength(20, timeout=1.5))

        started = time.monotonic()
        self.assertTrue(self.engine.stop(timeout=5))
        self.assertLess(
            time.monotonic() - started, 1.5, "按停止應該馬上生效，不該等輪詢"
        )

    def test_wake_is_safe_before_start(self):
        # 還沒啟動時沒有 event loop，叫醒不應該出錯
        self.engine._wake_control_loop()


class EngineLifecycleTests(EngineTestCase):
    def test_qr_is_published_before_pairing(self):
        from coyote import qr as qr_module

        self.engine.start()
        self.assertTrue(
            self.wait_for(lambda: self.status.state == EngineState.WAITING_PAIR)
        )
        self.assertEqual(self.status.client_id, "client-1")
        self.assertIsNotNone(self.status.qr_url)
        self.assertIn("DGLAB-SOCKET", self.status.qr_url)
        if not qr_module.qr_available():
            self.skipTest("沒有安裝 qrcode，只能驗證連結")
        self.assertIsNotNone(self.status.qr_matrix)
        self.assertGreater(len(self.status.qr_matrix), 20)
        self.assertFalse(self.status.paired)

    def test_pairing_switches_to_running(self):
        self.start_and_pair()
        self.assertTrue(self.status.paired)
        self.assertEqual(self.status.target_id, "app-1")

    def test_stop_shuts_everything_down(self):
        self.start_and_pair()
        self.assertTrue(self.engine.stop(timeout=10))
        self.assertFalse(self.engine.running)
        self.assertEqual(self.status.state, EngineState.STOPPED)
        self.assertEqual(self.transport.close_count, 1)
        # 結束前應該有把裝置靜音
        self.assertIn("clear-1", self.transport.messages)

    def test_port_in_use_reports_error(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", self.port))
        blocker.listen(1)
        self.addCleanup(blocker.close)
        self.engine.start()
        self.assertTrue(
            self.wait_for(
                lambda: self.status.state == EngineState.ERROR,
                message="埠被佔用時應該回報錯誤",
            )
        )
        self.assertIn("GSI 伺服器", self.status.error or "")

    def test_logs_are_forwarded(self):
        self.start_and_pair()
        levels = {level for level, _ in self.logs}
        self.assertIn("success", levels)
        self.assertTrue(any("配對" in message for _, message in self.logs))

    def test_shutdown_closes_presence(self):
        """換掉引擎之前要能把 Discord 連線收乾淨。"""
        presence = self.engine.presence
        calls: list[str] = []
        presence.close = lambda: calls.append("close")  # type: ignore[method-assign]
        self.engine.shutdown()
        self.assertEqual(calls, ["close"])
        self.assertFalse(self.engine.running)


class EngineControlTests(EngineTestCase):
    def test_strength_follows_health(self):
        self.start_and_pair()
        post_gsi(self.address(), payload_with(100))
        self.assertTrue(self.wait_strength(20))

        post_gsi(self.address(), payload_with(60))
        self.assertTrue(self.wait_strength(40))

    def test_status_reports_cs2_data(self):
        self.start_and_pair()
        post_gsi(self.address(), payload_with(73, burning=120, kills=2))
        self.assertTrue(self.wait_for(lambda: self.status.health == 73))
        self.assertEqual(self.status.burning, 120)
        self.assertEqual(self.status.round_kills, 2)
        self.assertTrue(self.status.health_known)
        self.assertTrue(self.status.cs2_connected)
        self.assertGreaterEqual(self.status.gsi_payloads, 1)

    def test_heartbeat_does_not_energize(self):
        self.start_and_pair()
        post_gsi(self.address(), HEARTBEAT_PAYLOAD)
        time.sleep(0.3)
        self.assertEqual(self.status.strength, 0)

    def test_pause_silences_and_resume_restores(self):
        self.start_and_pair()
        post_gsi(self.address(), payload_with(100))
        self.assertTrue(self.wait_strength(20))

        sent_before = len(self.transport.messages)
        self.engine.pause()
        self.assertTrue(
            self.wait_for(lambda: self.status.state == EngineState.PAUSED)
        )
        self.assertTrue(
            self.wait_for(
                lambda: "strength-1+2+0" in self.transport.messages[sent_before:],
                message="暫停時應該把強度歸零",
            )
        )
        self.assertFalse(self.status.output_active)

        self.engine.resume()
        self.assertTrue(self.wait_strength(20), "恢復後應該重新套用強度")

    def test_pause_keeps_the_connection(self):
        self.start_and_pair()
        self.engine.pause()
        self.assertTrue(self.wait_for(lambda: self.engine.paused))
        time.sleep(0.3)
        self.assertTrue(self.status.paired, "暫停不應該斷線")
        self.assertEqual(self.transport.close_count, 0)

    def test_reset_stack(self):
        self.start_and_pair()
        post_gsi(self.address(), payload_with(100))
        self.assertTrue(self.wait_strength(20))
        post_gsi(self.address(), payload_with(0))
        self.assertTrue(self.wait_for(lambda: self.status.stack == 5.0))
        self.engine.reset_stack()
        self.assertTrue(self.wait_for(lambda: self.status.stack == 0.0))

    def test_apply_config_changes_strength(self):
        self.start_and_pair()
        post_gsi(self.address(), payload_with(100))
        self.assertTrue(self.wait_strength(20))

        new_policy = dataclasses.replace(self.config.dglab.policy, base_strength=35)
        new_config = dataclasses.replace(
            self.config,
            dglab=dataclasses.replace(self.config.dglab, policy=new_policy),
        )
        self.engine.apply_config(new_config)
        self.assertTrue(self.wait_strength(35))


class EngineHitOnlyTests(EngineTestCase):
    HIT_ONLY = True

    def test_silent_until_hit(self):
        self.start_and_pair()
        post_gsi(self.address(), payload_with(100))
        self.assertTrue(self.wait_first_sample(100))
        time.sleep(0.3)
        self.assertFalse(
            self.status.output_active, "hit_only 模式不受擊不應該輸出"
        )

    def test_hit_produces_a_burst(self):
        self.start_and_pair()
        post_gsi(self.address(), payload_with(100))
        # 一定要等控制迴圈真的取樣到滿血，否則後面那筆會被當成第一筆
        self.assertTrue(self.wait_first_sample(100))
        before = len(self.transport.messages_of("clear-1"))
        post_gsi(self.address(), payload_with(70))
        self.assertTrue(
            self.wait_for(
                lambda: len(self.transport.messages_of("clear-1")) > before,
                message="受擊時應該先清空佇列再補爆發波形",
            )
        )
        self.assertTrue(self.status.output_active)


class EngineBuilderTests(unittest.TestCase):
    def test_describe_wave(self):
        config = AppConfig(dglab=DglabConfig())
        text = describe_wave(config)
        self.assertIn("steady", text)

    def test_build_client_uses_the_preset(self):
        from coyote import resolve_preset

        config = AppConfig(dglab=DglabConfig())
        client = build_client(FakeSocketTransport(), config)
        self.assertEqual(
            len(resolve_preset("steady")), 1
        )
        self.assertIn(Channel.A, client.channels)


if __name__ == "__main__":
    unittest.main()
