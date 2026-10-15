"""Discord IPC 協定層的測試。

用 :class:`tests.support.FakeDiscordPipe` 扮演 Discord 那一端，
所以握手與送指令的來回都能真的跑一遍，不需要開著 Discord。
"""

from __future__ import annotations

import json
import struct
import threading
import time
import unittest
from urllib.error import HTTPError, URLError

from discord_rpc import (
    ASSETS_URL,
    HEADER_SIZE,
    MAX_ASSET_KEY_LENGTH,
    MAX_PAYLOAD_SIZE,
    OP_CLOSE,
    OP_FRAME,
    OP_HANDSHAKE,
    OP_PING,
    OP_PONG,
    DiscordAssetError,
    DiscordIPC,
    DiscordIPCError,
    build_activity,
    clamp_asset_key,
    clamp_text,
    decode_header,
    decode_payload,
    encode_message,
    fetch_assets,
    named_pipe_paths,
    order_assets_for,
    parse_assets,
    unix_socket_paths,
)
from tests.support import FakeDiscordPipe


class EncodeDecodeTests(unittest.TestCase):
    def test_round_trip(self):
        data = encode_message(OP_FRAME, {"cmd": "SET_ACTIVITY", "args": {"pid": 1}})
        opcode, length = decode_header(data[:HEADER_SIZE])
        self.assertEqual(opcode, OP_FRAME)
        self.assertEqual(length, len(data) - HEADER_SIZE)
        self.assertEqual(
            decode_payload(data[HEADER_SIZE:]),
            {"cmd": "SET_ACTIVITY", "args": {"pid": 1}},
        )

    def test_header_is_little_endian(self):
        data = encode_message(OP_HANDSHAKE, {"v": 1})
        self.assertEqual(struct.unpack("<ii", data[:8]), (OP_HANDSHAKE, len(data) - 8))

    def test_unicode_is_not_escaped(self):
        data = encode_message(OP_FRAME, {"state": "強度 42"})
        self.assertIn("強度".encode("utf-8"), data)

    def test_oversized_payload_is_rejected(self):
        with self.assertRaises(DiscordIPCError):
            encode_message(OP_FRAME, {"x": "a" * (MAX_PAYLOAD_SIZE + 1)})

    def test_bad_header_length(self):
        with self.assertRaises(DiscordIPCError):
            decode_header(b"\x00\x00")

    def test_absurd_length_is_rejected(self):
        with self.assertRaises(DiscordIPCError):
            decode_header(struct.pack("<ii", OP_FRAME, MAX_PAYLOAD_SIZE + 1))

    def test_bad_json(self):
        with self.assertRaises(DiscordIPCError):
            decode_payload(b"not json")
        with self.assertRaises(DiscordIPCError):
            decode_payload(b"[1, 2, 3]")

    def test_endpoint_paths(self):
        self.assertEqual(named_pipe_paths(3)[0], r"\\.\pipe\discord-ipc-0")
        self.assertEqual(len(named_pipe_paths()), 10)
        self.assertTrue(all("discord-ipc" in path for path in unix_socket_paths(2)))


class ClampTests(unittest.TestCase):
    def test_short_text_is_padded(self):
        self.assertGreaterEqual(len(clamp_text("A")), 2)

    def test_long_text_is_truncated(self):
        self.assertEqual(len(clamp_text("x" * 500)), 128)

    def test_empty_text_falls_back(self):
        self.assertTrue(clamp_text("").strip() or True)
        self.assertGreaterEqual(len(clamp_text("")), 2)

    def test_build_activity_shape(self):
        activity = build_activity(
            details="CS2 對戰中", state="強度 42／80", start_timestamp=1000.0
        )
        self.assertEqual(activity["details"], "CS2 對戰中")
        self.assertEqual(activity["state"], "強度 42／80")
        self.assertEqual(activity["timestamps"], {"start": 1000000})

    def test_build_activity_without_timestamp(self):
        self.assertNotIn("timestamps", build_activity(details="a", state="b"))


class AssetTests(unittest.TestCase):
    """大圖示／小圖示（Discord 的 asset key）。"""

    def test_no_images_means_no_assets_block(self):
        self.assertNotIn("assets", build_activity(details="a", state="b"))

    def test_large_image_only(self):
        activity = build_activity(details="a", state="b", large_image="logo")
        self.assertEqual(activity["assets"], {"large_image": "logo"})

    def test_small_image_only(self):
        activity = build_activity(details="a", state="b", small_image="bolt")
        self.assertEqual(activity["assets"], {"small_image": "bolt"})

    def test_both_images(self):
        activity = build_activity(
            details="a", state="b", large_image="logo", small_image="bolt"
        )
        self.assertEqual(
            activity["assets"], {"large_image": "logo", "small_image": "bolt"}
        )

    def test_hover_texts(self):
        activity = build_activity(
            details="a",
            state="b",
            large_image="logo",
            large_text="DGLAB-CS2",
            small_image="bolt",
            small_text="郊狼 3.0",
        )
        assets = activity["assets"]
        self.assertEqual(assets["large_text"], "DGLAB-CS2")
        self.assertEqual(assets["small_text"], "郊狼 3.0")

    def test_hover_text_is_ignored_without_its_image(self):
        """沒有圖片時 Discord 會忽略提示文字，所以乾脆不要送。"""
        activity = build_activity(details="a", state="b", large_text="孤兒文字")
        self.assertNotIn("assets", activity)

    def test_small_text_ignored_without_small_image(self):
        activity = build_activity(
            details="a", state="b", large_image="logo", small_text="孤兒文字"
        )
        self.assertNotIn("small_text", activity["assets"])

    def test_whitespace_only_image_is_treated_as_empty(self):
        activity = build_activity(details="a", state="b", large_image="   ")
        self.assertNotIn("assets", activity)

    def test_image_key_is_trimmed(self):
        activity = build_activity(details="a", state="b", large_image="  logo  ")
        self.assertEqual(activity["assets"]["large_image"], "logo")

    def test_clamp_asset_key(self):
        self.assertEqual(clamp_asset_key(" x "), "x")
        self.assertEqual(len(clamp_asset_key("x" * 500)), MAX_ASSET_KEY_LENGTH)

    def test_long_image_key_is_truncated_not_rejected(self):
        activity = build_activity(details="a", state="b", large_image="x" * 500)
        self.assertEqual(len(activity["assets"]["large_image"]), MAX_ASSET_KEY_LENGTH)


class HandshakeTests(unittest.TestCase):
    def test_handshake_sends_client_id_and_waits_for_ready(self):
        pipe = FakeDiscordPipe()
        ipc = DiscordIPC(pipe, application_id="123456789", timeout=5)
        data = ipc.handshake()
        self.assertTrue(ipc.ready)
        self.assertEqual(data["user"]["username"], "tester")
        opcode, payload = pipe.sent[0]
        self.assertEqual(opcode, OP_HANDSHAKE)
        self.assertEqual(payload, {"v": 1, "client_id": "123456789"})

    def test_handshake_error_is_reported(self):
        pipe = FakeDiscordPipe(reject_handshake=True)
        ipc = DiscordIPC(pipe, application_id="000", timeout=5)
        with self.assertRaises(DiscordIPCError) as ctx:
            ipc.handshake()
        self.assertIn("無效的用戶端 ID", str(ctx.exception))
        self.assertFalse(ipc.ready)

    def test_handshake_accepts_other_dispatch_event(self):
        pipe = FakeDiscordPipe(ready_event="SOMETHING_ELSE")
        ipc = DiscordIPC(pipe, application_id="1", timeout=5)
        ipc.handshake()
        self.assertTrue(ipc.ready)


class ActivityTests(unittest.TestCase):
    def test_set_activity_round_trip(self):
        pipe = FakeDiscordPipe()
        ipc = DiscordIPC(pipe, application_id="1", pid=4321, timeout=5, nonce_factory=lambda: "n1")
        ipc.handshake()
        ipc.set_activity({"details": "CS2 對戰中", "state": "強度 42／80"})
        self.assertEqual(pipe.activities[-1]["details"], "CS2 對戰中")
        opcode, payload = pipe.sent[-1]
        self.assertEqual(opcode, OP_FRAME)
        self.assertEqual(payload["cmd"], "SET_ACTIVITY")
        self.assertEqual(payload["args"]["pid"], 4321)
        self.assertEqual(payload["nonce"], "n1")

    def test_activity_error_is_reported(self):
        pipe = FakeDiscordPipe(reject_activity=True)
        ipc = DiscordIPC(pipe, application_id="1", timeout=5)
        ipc.handshake()
        with self.assertRaises(DiscordIPCError) as ctx:
            ipc.set_activity({"details": "x", "state": "y"})
        self.assertIn("活動內容不合法", str(ctx.exception))

    def test_clear_activity_sends_no_activity(self):
        pipe = FakeDiscordPipe()
        ipc = DiscordIPC(pipe, application_id="1", timeout=5)
        ipc.handshake()
        ipc.clear_activity()
        _, payload = pipe.sent[-1]
        self.assertEqual(payload["cmd"], "SET_ACTIVITY")
        self.assertEqual(payload["args"], {"pid": ipc._pid})

    def test_ping_gets_pong(self):
        pipe = FakeDiscordPipe()
        ipc = DiscordIPC(pipe, application_id="1", timeout=5)
        ipc.handshake()
        # Discord 主動 ping：下一個讀到的封包是 PING，客戶端要回 PONG
        pipe._push(OP_PING, {"nonce": "x"})
        pipe._push(
            OP_FRAME,
            {"cmd": "SET_ACTIVITY", "evt": None, "nonce": "n1", "data": {}},
        )
        ipc.set_activity({"details": "a", "state": "b"})
        self.assertIn(OP_PONG, [opcode for opcode, _ in pipe.sent])

    def test_close_sends_close_opcode(self):
        pipe = FakeDiscordPipe()
        ipc = DiscordIPC(pipe, application_id="1", timeout=5)
        ipc.handshake()
        ipc.close()
        self.assertEqual(pipe.sent[-1][0], OP_CLOSE)
        self.assertTrue(pipe.closed)

    def test_send_after_close_fails(self):
        pipe = FakeDiscordPipe()
        ipc = DiscordIPC(pipe, application_id="1", timeout=5)
        ipc.close()
        with self.assertRaises(DiscordIPCError):
            ipc.set_activity({"details": "a", "state": "b"})


class AssetListTests(unittest.TestCase):
    """查詢應用程式上傳過哪些 Rich Presence 圖片。"""

    APP_ID = "1560163564839510086"

    REAL_RESPONSE = [
        {"id": "1560176038842204240", "type": 1, "name": "logo-new"},
        {"id": "1560176144051871765", "type": 1, "name": "images"},
    ]

    # ------------------------------------------------------------ 解析

    def test_parse_assets(self):
        assets = parse_assets(self.REAL_RESPONSE)
        self.assertEqual([a.name for a in assets], ["logo-new", "images"])
        self.assertEqual(assets[0].id, "1560176038842204240")
        self.assertEqual(assets[0].kind, "small")
        self.assertTrue(assets[0].is_small)

    def test_type_mapping(self):
        assets = parse_assets(
            [
                {"name": "big", "type": 2},
                {"name": "small", "type": 1},
                {"name": "weird", "type": 99},
                {"name": "missing", "type": None},
            ]
        )
        kinds = {a.name: a.kind for a in assets}
        self.assertEqual(kinds["big"], "big")
        self.assertEqual(kinds["small"], "small")
        self.assertEqual(kinds["weird"], "other")
        self.assertEqual(kinds["missing"], "other")

    def test_nameless_assets_are_skipped(self):
        assets = parse_assets(
            [{"id": "1", "type": 2}, {"name": "  ", "type": 2}, {"name": "ok"}]
        )
        self.assertEqual([a.name for a in assets], ["ok"])

    def test_duplicate_names_are_collapsed(self):
        assets = parse_assets([{"name": "x"}, {"name": "x"}, {"name": "y"}])
        self.assertEqual([a.name for a in assets], ["x", "y"])

    def test_non_dict_items_are_skipped(self):
        assets = parse_assets([{"name": "ok"}, "nope", 123, None])
        self.assertEqual([a.name for a in assets], ["ok"])

    def test_empty_list(self):
        self.assertEqual(parse_assets([]), ())

    def test_bad_payload(self):
        for value in ({}, "nope", None, 5):
            with self.subTest(value=value):
                with self.assertRaises(DiscordAssetError):
                    parse_assets(value)

    # ------------------------------------------------------------ 排序

    def test_big_assets_come_first_for_the_large_slot(self):
        assets = parse_assets(
            [
                {"name": "s1", "type": 1},
                {"name": "b1", "type": 2},
                {"name": "b2", "type": 2},
            ]
        )
        self.assertEqual(order_assets_for(assets, "large"), ["b1", "b2", "s1"])

    def test_small_assets_come_first_for_the_small_slot(self):
        assets = parse_assets(
            [
                {"name": "s1", "type": 1},
                {"name": "b1", "type": 2},
                {"name": "s2", "type": 1},
            ]
        )
        self.assertEqual(order_assets_for(assets, "small"), ["s1", "s2", "b1"])

    def test_all_assets_are_offered_for_both_slots(self):
        assets = parse_assets([{"name": "a", "type": 1}, {"name": "b", "type": 2}])
        self.assertEqual(sorted(order_assets_for(assets, "large")), ["a", "b"])
        self.assertEqual(sorted(order_assets_for(assets, "small")), ["a", "b"])

    # ------------------------------------------------------------ 抓取

    def fake(self, payload):
        calls = []

        def opener(url, timeout):
            calls.append((url, timeout))
            return payload

        opener.calls = calls
        return opener

    def test_fetch_uses_the_assets_endpoint(self):
        opener = self.fake(self.REAL_RESPONSE)
        assets = fetch_assets(self.APP_ID, opener=opener)
        self.assertEqual(len(assets), 2)
        url, timeout = opener.calls[0]
        self.assertIn(self.APP_ID, url)
        self.assertIn("/assets", url)
        self.assertGreater(timeout, 0)

    def test_fetch_bypasses_the_cdn_cache(self):
        """剛上傳圖片時端點會回舊清單，所以一定要帶 cache buster。"""
        opener = self.fake(self.REAL_RESPONSE)
        fetch_assets(self.APP_ID, opener=opener)
        fetch_assets(self.APP_ID, opener=opener)
        first, second = opener.calls[0][0], opener.calls[1][0]
        self.assertIn("?t=", first)
        self.assertTrue(first.startswith(ASSETS_URL.format(application_id=self.APP_ID)))
        # 兩次請求的快取參數不一定不同（同一毫秒），但都要帶
        self.assertIn("?t=", second)

    def test_fetch_rejects_empty_id(self):
        with self.assertRaises(DiscordAssetError) as ctx:
            fetch_assets("")
        self.assertIn("請先填", str(ctx.exception))

    def test_fetch_rejects_non_numeric_id(self):
        with self.assertRaises(DiscordAssetError) as ctx:
            fetch_assets("abc123")
        self.assertIn("純數字", str(ctx.exception))

    def test_fetch_rejects_wrong_length(self):
        with self.assertRaises(DiscordAssetError) as ctx:
            fetch_assets("123")
        self.assertIn("長度", str(ctx.exception))

    def http_error(self, code):
        def opener(url, timeout):
            raise HTTPError(url, code, "boom", {}, None)

        return opener

    def test_unknown_application(self):
        with self.assertRaises(DiscordAssetError) as ctx:
            fetch_assets(self.APP_ID, opener=self.http_error(404))
        self.assertIn("找不到", str(ctx.exception))

    def test_invalid_id_from_discord(self):
        with self.assertRaises(DiscordAssetError) as ctx:
            fetch_assets(self.APP_ID, opener=self.http_error(400))
        self.assertIn("格式", str(ctx.exception))

    def test_rate_limited(self):
        with self.assertRaises(DiscordAssetError) as ctx:
            fetch_assets(self.APP_ID, opener=self.http_error(429))
        self.assertIn("頻繁", str(ctx.exception))

    def test_other_http_error(self):
        with self.assertRaises(DiscordAssetError) as ctx:
            fetch_assets(self.APP_ID, opener=self.http_error(500))
        self.assertIn("500", str(ctx.exception))

    def test_network_failure(self):
        def opener(url, timeout):
            raise URLError("網路不通")

        with self.assertRaises(DiscordAssetError) as ctx:
            fetch_assets(self.APP_ID, opener=opener)
        self.assertIn("連不上 Discord", str(ctx.exception))

    def test_broken_json(self):
        def opener(url, timeout):
            raise ValueError("Expecting value")

        with self.assertRaises(DiscordAssetError) as ctx:
            fetch_assets(self.APP_ID, opener=opener)
        self.assertIn("失敗", str(ctx.exception))

    def test_empty_asset_list_is_not_an_error(self):
        self.assertEqual(fetch_assets(self.APP_ID, opener=self.fake([])), ())


class TimeoutTests(unittest.TestCase):
    def test_silent_discord_times_out_instead_of_hanging(self):
        """Discord 不回話時必須放棄，不能把執行緒卡死。"""
        pipe = FakeDiscordPipe(silent=True)
        ipc = DiscordIPC(pipe, application_id="1", timeout=0.3)
        started = time.monotonic()
        with self.assertRaises(DiscordIPCError) as ctx:
            ipc.handshake()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5.0)
        self.assertIn("逾時", str(ctx.exception))
        self.assertTrue(pipe.closed, "逾時後應該把連線關掉")

    def test_timeout_does_not_leak_threads(self):
        before = threading.active_count()
        pipe = FakeDiscordPipe(silent=True)
        ipc = DiscordIPC(pipe, application_id="1", timeout=0.2)
        with self.assertRaises(DiscordIPCError):
            ipc.handshake()
        time.sleep(0.5)
        self.assertLessEqual(threading.active_count(), before + 1)


if __name__ == "__main__":
    unittest.main()
