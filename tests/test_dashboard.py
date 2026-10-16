"""監看網頁伺服器的測試。

全部離線：伺服器綁在 127.0.0.1 的隨機埠，只打自己的 loopback。
"""

from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from dashboard import (
    DashboardOptions,
    DashboardServer,
    build_status_payload,
)
from engine_status import EngineState, EngineStatus
from stats import SessionStats


@dataclass
class FakeStatus:
    """假的 EngineStatus（只需要監看頁面會讀的欄位）。"""

    state_label: str = "執行中"
    paused: bool = False
    paired: bool = True
    cs2_connected: bool = True
    health_known: bool = True
    spectating: bool = False
    health: int = 23
    burning: int = 200
    strength: int = 38
    stack: float = 4.5
    streak: int = 2
    output_active: bool = True
    last_event: str = "受擊 -15"
    stats: SessionStats = field(default_factory=SessionStats)


class BuildStatusPayloadTests(unittest.TestCase):
    def test_public_fields(self):
        payload = build_status_payload(
            FakeStatus(), version="3.2.0", wave="steady", max_strength=80
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["version"], "3.2.0")
        self.assertEqual(payload["strength"], 38)
        self.assertEqual(payload["max_strength"], 80)
        self.assertEqual(payload["health"], 23)
        self.assertEqual(payload["wave"], "steady")
        self.assertEqual(payload["streak"], 2)

    def test_never_leaks_secrets(self):
        """監看頁面是公開的，絕對不能出現配對連結或 token。"""
        status = EngineStatus(
            qr_url="https://www.dungeon-lab.com/app-download.php#DGLAB-SOCKET#wss://x/abc",
            client_id="SECRET-CLIENT-ID",
            target_id="SECRET-TARGET-ID",
        )
        payload = build_status_payload(status, version="1.0", wave="steady")
        blob = json.dumps(payload, ensure_ascii=False)
        for secret in ("DGLAB-SOCKET", "SECRET-CLIENT-ID", "SECRET-TARGET-ID", "dungeon-lab"):
            self.assertNotIn(secret, blob)

    def test_stats_block(self):
        stats = SessionStats()
        stats.record(0.0, strength=40, output_active=True)
        stats.record(1.0, strength=40, output_active=True, damage=30, kills=2, streak=2)
        payload = build_status_payload(FakeStatus(stats=stats), max_strength=80)
        block = payload["stats"]
        self.assertEqual(block["kills"], 2)
        self.assertEqual(block["hits"], 1)
        self.assertEqual(block["damage_taken"], 30)
        self.assertEqual(block["best_streak"], 2)
        self.assertEqual(block["max_strength"], 40)
        self.assertIn("duration", block)

    def test_missing_stats_is_empty_dict(self):
        payload = build_status_payload(FakeStatus(stats=None))
        self.assertEqual(payload["stats"], {})

    def test_real_engine_status_works(self):
        payload = build_status_payload(EngineStatus(), version="3.2.0", max_strength=80)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["state"], "未啟動")
        self.assertEqual(payload["max_strength"], 80)


class DashboardServerTests(unittest.TestCase):
    """真的把伺服器拉起來，用 HTTP 打自己。"""

    def setUp(self):
        self.payload = {"ok": True, "strength": 42, "max_strength": 80}
        self.server = DashboardServer(lambda: dict(self.payload), DashboardOptions(
            enabled=True, host="127.0.0.1", port=0
        ))
        self.assertTrue(self.server.start(), "伺服器沒有啟動")
        self.addCleanup(self.server.stop)
        self.port = self.server._server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def get(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            return response.status, response.read().decode("utf-8")

    def test_status_endpoint(self):
        status, body = self.get("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["strength"], 42)

    def test_dashboard_page(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("DGLAB-CS2 即時狀態", body)
        self.assertIn("/api/status", body)

    def test_overlay_page_is_transparent(self):
        status, body = self.get("/overlay")
        self.assertEqual(status, 200)
        self.assertIn("background:transparent", body)

    def test_healthz(self):
        status, body = self.get("/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_unknown_path_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/nope")
        self.assertEqual(caught.exception.code, 404)

    def test_events_endpoint(self):
        server = DashboardServer(
            lambda: dict(self.payload),
            DashboardOptions(enabled=True, host="127.0.0.1", port=0),
            event_provider=lambda: [{"time": "12:00:00", "text": "測試"}],
        )
        self.assertTrue(server.start())
        self.addCleanup(server.stop)
        port = server._server.server_address[1]
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/events", timeout=5
        ) as response:
            data = json.loads(response.read().decode("utf-8"))
        self.assertEqual(data["events"][0]["text"], "測試")

    def test_token_is_required_when_set(self):
        server = DashboardServer(
            lambda: dict(self.payload),
            DashboardOptions(enabled=True, host="127.0.0.1", port=0, token="s3cret"),
        )
        self.assertTrue(server.start())
        self.addCleanup(server.stop)
        port = server._server.server_address[1]
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=5)
        self.assertEqual(caught.exception.code, 401)
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/status?token=s3cret", timeout=5
        ) as response:
            self.assertEqual(response.status, 200)

    def test_url_hint(self):
        self.assertIn("127.0.0.1", self.server.url)

    def test_second_start_is_a_noop(self):
        self.assertFalse(self.server.start())

    def test_port_in_use_reports_failure_without_raising(self):
        clashing = DashboardServer(
            lambda: dict(self.payload),
            DashboardOptions(enabled=True, host="127.0.0.1", port=self.port),
        )
        self.assertFalse(clashing.start(), "同一個埠應該要啟動失敗")
        self.assertFalse(clashing.running)

    def test_provider_exception_does_not_break_the_page(self):
        def exploding():
            raise RuntimeError("壞了")

        server = DashboardServer(exploding, DashboardOptions(
            enabled=True, host="127.0.0.1", port=0
        ))
        self.assertTrue(server.start())
        self.addCleanup(server.stop)
        port = server._server.server_address[1]
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/status", timeout=5
        ) as response:
            self.assertEqual(response.status, 200)

    def test_stop_is_idempotent(self):
        self.server.stop()
        self.server.stop()
        self.assertFalse(self.server.running)


if __name__ == "__main__":
    unittest.main()
