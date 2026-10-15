"""GSI 伺服器的整合測試（真的打 HTTP，但不需要 CS2）。"""

from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.request

from config import GsiConfig
from gsi_server import GSIServer, describe_payload_source, extract_token, serve_forever

PLAYER_PAYLOAD = {
    "auth": {"token": "DGLAB"},
    "provider": {
        "name": "Counter-Strike: Global Offensive",
        "appid": 730,
        "version": 1,
        "steamid": "76561197990087679",
        "timestamp": 1,
    },
    "round": {"phase": "live"},
    "map": {
        "mode": "competitive",
        "name": "de_dust2",
        "phase": "live",
        "round": 3,
        "team_ct": {"score": 2},
        "team_t": {"score": 1},
    },
    "player": {
        "steamid": "76561197990087679",
        "name": "tester",
        "observer_slot": 1,
        "activity": "playing",
        "state": {
            "health": 37,
            "armor": 100,
            "helmet": True,
            "flashed": 0,
            "smoked": 0,
            "burning": 90,
            "money": 800,
            "round_kills": 1,
            "round_killhs": 0,
            "equip_value": 1000,
        },
        "weapons": {},
        "match_stats": {"kills": 1, "assists": 0, "deaths": 0, "mvps": 0, "score": 1},
    },
}

#: CS2 在沒有變化時送出的 heartbeat：只有 provider，沒有 player_state
HEARTBEAT_PAYLOAD = {
    "auth": {"token": "DGLAB"},
    "provider": PLAYER_PAYLOAD["provider"],
}


def payload_with(health: int, burning: int = 0, kills: int = 0) -> dict:
    payload = json.loads(json.dumps(PLAYER_PAYLOAD))
    payload["player"]["state"]["health"] = health
    payload["player"]["state"]["burning"] = burning
    # 一次擊殺在遊戲裡會同時反映在「每回合擊殺」與「整場累計擊殺」上，
    # 兩個都更新才和真實資料一致。
    payload["player"]["state"]["round_kills"] = kills
    payload["player"]["match_stats"]["kills"] = kills
    return payload


class GsiServerTestCase(unittest.TestCase):
    #: 這個測試類別使用的 token；子類別可以覆寫
    TOKEN = "DGLAB"

    def setUp(self):
        # port = 0 -> 由作業系統指派一個空閒的埠
        self.server = GSIServer(GsiConfig(host="127.0.0.1", port=0, token=self.TOKEN))
        self.thread = serve_forever(self.server)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def post(self, payload, raw: bytes | None = None):
        body = raw if raw is not None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def get(self):
        with urllib.request.urlopen(self.url, timeout=5) as response:
            return response.status, response.read().decode("utf-8")


class AuthenticatedPayloadTests(GsiServerTestCase):
    def test_player_state_is_parsed(self):
        status, _ = self.post(PLAYER_PAYLOAD)
        self.assertEqual(status, 200)
        snapshot = self.server.snapshot()
        self.assertEqual(snapshot.health, 37)
        self.assertEqual(snapshot.burning, 90)
        self.assertEqual(snapshot.round_kills, 1)
        self.assertTrue(snapshot.health_known)
        self.assertEqual(snapshot.round_phase, "live")
        self.assertEqual(self.server.payload_count, 1)
        self.assertEqual(self.server.rejected_count, 0)

    def test_steamids_are_parsed_so_spectating_can_be_detected(self):
        """``provider.steamid`` 是自己的、``player.steamid`` 會跟著觀戰對象變。"""
        self.post(PLAYER_PAYLOAD)
        snapshot = self.server.snapshot()
        self.assertEqual(snapshot.provider_steamid, "76561197990087679")
        self.assertEqual(snapshot.steamid, "76561197990087679")
        self.assertTrue(snapshot.is_mine)

        spectating = payload_with(100)
        spectating["player"]["steamid"] = "76561198000000001"
        self.post(spectating)
        snapshot = self.server.snapshot()
        self.assertFalse(
            snapshot.is_mine,
            "player.steamid 和 provider.steamid 不同 -> 這是被觀戰者的資料",
        )

    def test_match_kills_are_parsed(self):
        self.post(PLAYER_PAYLOAD)
        snapshot = self.server.snapshot()
        self.assertTrue(snapshot.match_kills_known)
        self.assertEqual(snapshot.match_kills, 1)

    def test_spectarget_is_parsed(self):
        payload = payload_with(100)
        payload["player"]["spectarget"] = "76561198000000001"
        self.post(payload)
        self.assertEqual(self.server.snapshot().spectarget, "76561198000000001")

    def test_round_kills_going_back_to_zero_is_stored(self):
        """回合重置時 round_kills 必須寫回 0，否則下一回合第一殺會漏掉。"""
        self.post(payload_with(100, kills=3))
        self.assertEqual(self.server.snapshot().round_kills, 3)
        self.post(payload_with(100, kills=0))
        self.assertEqual(self.server.snapshot().round_kills, 0)

    def test_last_payload_time_is_recorded(self):
        self.assertIsNone(self.server.seconds_since_last_payload())
        self.post(PLAYER_PAYLOAD)
        elapsed = self.server.seconds_since_last_payload()
        self.assertIsNotNone(elapsed)
        self.assertLess(elapsed, 5)

    def test_heartbeat_does_not_fake_a_health_value(self):
        status, _ = self.post(HEARTBEAT_PAYLOAD)
        self.assertEqual(status, 200)
        self.assertFalse(
            self.server.snapshot().health_known,
            "heartbeat 不含 player_state，血量不可信",
        )
        self.assertIsNotNone(self.server.seconds_since_last_payload())

    def test_health_known_flips_after_real_state(self):
        self.post(HEARTBEAT_PAYLOAD)
        self.post(PLAYER_PAYLOAD)
        self.assertTrue(self.server.snapshot().health_known)

    def test_response_has_valid_http_status_line(self):
        # 原版先 send_header 才 send_response，回應會是壞掉的一行；
        # urllib 能解析就代表格式正確
        status, body = self.post(PLAYER_PAYLOAD)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"ok")

    def test_sequence_of_health_values(self):
        for health in (100, 80, 55, 0, 100):
            self.post(payload_with(health))
            self.assertEqual(self.server.snapshot().health, health)


class RejectedPayloadTests(GsiServerTestCase):
    def test_wrong_token(self):
        payload = {**PLAYER_PAYLOAD, "auth": {"token": "WRONG"}}
        status, _ = self.post(payload)
        self.assertEqual(status, 401)
        self.assertEqual(self.server.rejected_count, 1)
        self.assertEqual(self.server.payload_count, 0)

    def test_missing_auth(self):
        payload = {key: value for key, value in PLAYER_PAYLOAD.items() if key != "auth"}
        status, _ = self.post(payload)
        self.assertEqual(status, 401)

    def test_invalid_json(self):
        status, _ = self.post(None, raw=b"{not json")
        self.assertEqual(status, 400)

    def test_empty_body(self):
        status, _ = self.post(None, raw=b"")
        self.assertEqual(status, 400)


class AuthFailureDiagnosticsTests(GsiServerTestCase):
    """token 不符時要說清楚「收到什麼、誰送的」，而且不能洗版。"""

    def test_extract_token(self):
        self.assertEqual(extract_token({"auth": {"token": "ABC"}}), "ABC")
        self.assertIsNone(extract_token({}))
        self.assertIsNone(extract_token({"auth": {}}))
        self.assertIsNone(extract_token({"auth": "not a dict"}))
        self.assertIsNone(extract_token({"auth": {"token": 123}}))
        self.assertIsNone(extract_token("not a dict"))

    def test_describe_payload_source(self):
        self.assertIn("Counter-Strike", describe_payload_source(PLAYER_PAYLOAD))
        self.assertIn("730", describe_payload_source(PLAYER_PAYLOAD))
        self.assertIn("沒有 provider", describe_payload_source({}))
        self.assertIn("不是 JSON 物件", describe_payload_source([]))

    def test_every_rejection_is_counted(self):
        for _ in range(5):
            self.post({**PLAYER_PAYLOAD, "auth": {"token": "WRONG"}})
        self.assertEqual(self.server.rejected_count, 5)

    def test_warning_is_throttled(self):
        self.post({**PLAYER_PAYLOAD, "auth": {"token": "WRONG"}})
        # 第一次之後的短時間內不應該再要求印警告，但次數還是要累加
        should_warn, count = self.server.note_auth_failure()
        self.assertFalse(should_warn)
        self.assertEqual(count, 2)
        should_warn, count = self.server.note_auth_failure()
        self.assertFalse(should_warn)
        self.assertEqual(count, 3)

    def test_successful_payload_resets_the_throttle(self):
        self.post({**PLAYER_PAYLOAD, "auth": {"token": "WRONG"}})
        self.post(PLAYER_PAYLOAD)
        should_warn, _ = self.server.note_auth_failure()
        self.assertTrue(should_warn, "收到合法資料之後應該重新提醒")


class NoTokenRequiredTests(GsiServerTestCase):
    """``gsi.token = ""`` 時接受任何來源。"""

    TOKEN = ""

    def test_any_token_is_accepted(self):
        status, _ = self.post(PLAYER_PAYLOAD)
        self.assertEqual(status, 200)
        status, _ = self.post({**PLAYER_PAYLOAD, "auth": {"token": "隨便什麼都好"}})
        self.assertEqual(status, 200)
        self.assertEqual(self.server.rejected_count, 0)
        self.assertEqual(self.server.payload_count, 2)

    def test_payload_without_auth_is_accepted(self):
        payload = {key: value for key, value in PLAYER_PAYLOAD.items() if key != "auth"}
        status, _ = self.post(payload)
        self.assertEqual(status, 200)
        self.assertEqual(self.server.snapshot().health, 37)

    def test_still_rejects_non_objects(self):
        status, _ = self.post(None, raw=b"[1,2,3]")
        self.assertEqual(status, 401)


class HealthCheckTests(GsiServerTestCase):
    def test_get_returns_status(self):
        self.post(PLAYER_PAYLOAD)
        status, text = self.get()
        self.assertEqual(status, 200)
        self.assertIn("DGLAB-CS2 GSI server", text)
        self.assertIn("本回合擊殺", text)

    def test_get_before_any_payload(self):
        status, text = self.get()
        self.assertEqual(status, 200)
        self.assertIn("從未收到", text)
        self.assertIn("尚未收到玩家狀態", text)


if __name__ == "__main__":
    unittest.main()
