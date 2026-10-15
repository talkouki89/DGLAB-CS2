"""DG-LAB Socket 協定編解碼的測試。

訊息格式對照官方／社群文件：

- 強度：``strength-<1|2>+<0|1|2>+<值>``
- 波形：``pulse-<A|B>:["<16 個十六進位字元>", ...]``
- 清空：``clear-<1|2>``
- App 回報：``strength-<A>+<B>+<A上限>+<B上限>``、``feedback-<0~9>``
"""

from __future__ import annotations

import json
import unittest

from coyote.socket_protocol import (
    MAX_MESSAGE_LENGTH,
    Channel,
    MessageType,
    RetCode,
    ServerFrame,
    StrengthOperation,
    build_message,
    chunk_frames,
    clear_command,
    describe_ret_code,
    parse_feedback,
    parse_server_frame,
    parse_strength_report,
    pulse_command,
    qrcode_url,
    strength_command,
)
from coyote.waveform import ChannelWave


class ChannelTests(unittest.TestCase):
    def test_values(self):
        self.assertEqual(Channel.A.value, 1)
        self.assertEqual(Channel.B.value, 2)
        self.assertEqual(Channel.A.letter, "A")
        self.assertEqual(Channel.B.letter, "B")

    def test_from_value_accepts_many_spellings(self):
        for value in (Channel.A, 1, "1", "a", " A "):
            self.assertEqual(Channel.from_value(value), Channel.A)
        for value in (Channel.B, 2, "2", "b"):
            self.assertEqual(Channel.from_value(value), Channel.B)
        with self.assertRaises(ValueError):
            Channel.from_value("C")

    def test_parse_all(self):
        self.assertEqual(Channel.parse_all("A"), frozenset({Channel.A}))
        self.assertEqual(Channel.parse_all("b"), frozenset({Channel.B}))
        self.assertEqual(
            Channel.parse_all("AB"), frozenset({Channel.A, Channel.B})
        )
        self.assertEqual(
            Channel.parse_all("a,b"), frozenset({Channel.A, Channel.B})
        )
        self.assertEqual(Channel.parse_all(["A", "B"]), frozenset({Channel.A, Channel.B}))
        self.assertEqual(Channel.parse_all(""), frozenset())
        with self.assertRaises(ValueError):
            Channel.parse_all("C")


class StrengthCommandTests(unittest.TestCase):
    def test_official_examples(self):
        # 文件上的四個例子
        self.assertEqual(
            strength_command(Channel.A, StrengthOperation.INCREASE, 5),
            "strength-1+1+5",
        )
        self.assertEqual(
            strength_command(Channel.B, StrengthOperation.SET_TO, 0),
            "strength-2+2+0",
        )
        self.assertEqual(
            strength_command(Channel.B, StrengthOperation.DECREASE, 20),
            "strength-2+0+20",
        )
        self.assertEqual(
            strength_command(Channel.A, StrengthOperation.SET_TO, 35),
            "strength-1+2+35",
        )

    def test_accepts_plain_values(self):
        self.assertEqual(strength_command("B", 2, 10), "strength-2+2+10")
        self.assertEqual(strength_command(1, 1, 3), "strength-1+1+3")

    def test_rejects_out_of_range(self):
        with self.assertRaises(ValueError):
            strength_command(Channel.A, StrengthOperation.SET_TO, 201)
        with self.assertRaises(ValueError):
            strength_command(Channel.A, StrengthOperation.SET_TO, -1)


class ClearCommandTests(unittest.TestCase):
    def test_uses_numeric_channel(self):
        self.assertEqual(clear_command(Channel.A), "clear-1")
        self.assertEqual(clear_command(Channel.B), "clear-2")
        self.assertEqual(clear_command("A"), "clear-1")


class PulseCommandTests(unittest.TestCase):
    def test_uses_letter_channel_and_hex_frames(self):
        message = pulse_command(
            Channel.A, [ChannelWave.steady(10, 100), ChannelWave.pause()]
        )
        self.assertEqual(
            message, 'pulse-A:["0A0A0A0A64646464","0A0A0A0A00000000"]'
        )
        # 沒有多餘空白，長度才不會白白吃掉 1950 的上限
        self.assertNotIn(" ", message)

    def test_channel_b(self):
        self.assertTrue(pulse_command(Channel.B, [ChannelWave.pause()]).startswith("pulse-B:["))

    def test_accepts_hex_strings(self):
        self.assertEqual(
            pulse_command("A", ["0A0A0A0A64646464"]),
            'pulse-A:["0A0A0A0A64646464"]',
        )

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            pulse_command(Channel.A, [])

    def test_rejects_wrong_length_frame(self):
        with self.assertRaises(ValueError):
            pulse_command(Channel.A, ["0A0A"])

    def test_rejects_too_long_message(self):
        many = [ChannelWave.pause() for _ in range(200)]
        with self.assertRaises(ValueError) as ctx:
            pulse_command(Channel.A, many)
        self.assertIn(str(MAX_MESSAGE_LENGTH), str(ctx.exception))

    def test_chunk_frames(self):
        batches = chunk_frames([ChannelWave.pause() for _ in range(9)], limit=4)
        self.assertEqual([len(batch) for batch in batches], [4, 4, 1])
        self.assertEqual(chunk_frames([], limit=4), [])
        with self.assertRaises(ValueError):
            chunk_frames([], limit=0)


class BuildMessageTests(unittest.TestCase):
    def test_shape(self):
        text = build_message(
            MessageType.MSG,
            "strength-1+2+30",
            client_id="client-1",
            target_id="app-1",
        )
        payload = json.loads(text)
        self.assertEqual(
            payload,
            {
                "type": "msg",
                "clientId": "client-1",
                "targetId": "app-1",
                "message": "strength-1+2+30",
            },
        )

    def test_missing_ids_become_empty_strings(self):
        payload = json.loads(build_message(MessageType.MSG, "clear-1"))
        self.assertEqual(payload["clientId"], "")
        self.assertEqual(payload["targetId"], "")


class QrCodeUrlTests(unittest.TestCase):
    def test_official_format(self):
        self.assertEqual(
            qrcode_url("wss://ws.dungeon-lab.cn/", "abc-123"),
            "https://www.dungeon-lab.com/app-download.php#DGLAB-SOCKET#"
            "wss://ws.dungeon-lab.cn/abc-123",
        )

    def test_trailing_slash_is_removed(self):
        self.assertEqual(
            qrcode_url("wss://ws.dungeon-lab.cn", "abc"),
            "https://www.dungeon-lab.com/app-download.php#DGLAB-SOCKET#"
            "wss://ws.dungeon-lab.cn/abc",
        )


class ParseServerFrameTests(unittest.TestCase):
    def test_initial_bind(self):
        frame = parse_server_frame(
            '{"type":"bind","clientId":"c1","targetId":"","message":"targetId"}'
        )
        self.assertTrue(frame.is_initial_bind())
        self.assertFalse(frame.is_bind_success())
        self.assertEqual(frame.client_id, "c1")
        self.assertIsNone(frame.target_id)

    def test_bind_success(self):
        frame = parse_server_frame(
            '{"type":"bind","clientId":"c1","targetId":"a1","message":"200"}'
        )
        self.assertFalse(frame.is_initial_bind())
        self.assertTrue(frame.is_bind_success())
        self.assertEqual(frame.ret_code, RetCode.SUCCESS)

    def test_bind_failure(self):
        frame = parse_server_frame(
            '{"type":"bind","clientId":"c1","targetId":"a1","message":"400"}'
        )
        self.assertFalse(frame.is_bind_success())
        self.assertEqual(frame.ret_code, RetCode.ID_ALREADY_BOUND)

    def test_break_and_heartbeat(self):
        frame = parse_server_frame('{"type":"break","message":"209"}')
        self.assertEqual(frame.type, MessageType.BREAK)
        self.assertEqual(frame.ret_code, RetCode.CLIENT_DISCONNECTED)
        heartbeat = parse_server_frame('{"type":"heartbeat","message":"200"}')
        self.assertEqual(heartbeat.type, MessageType.HEARTBEAT)

    def test_non_string_message_is_coerced(self):
        frame = parse_server_frame('{"type":"msg","message":123}')
        self.assertEqual(frame.message, "123")
        self.assertEqual(frame.ret_code, 123)

    def test_invalid_json(self):
        for text in ("not json", "[1,2,3]", ""):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_server_frame(text)


class ParseReportTests(unittest.TestCase):
    def test_strength_report(self):
        report = parse_strength_report("strength-30+0+200+150")
        self.assertIsNotNone(report)
        self.assertEqual(report.strength_a, 30)
        self.assertEqual(report.strength_b, 0)
        self.assertEqual(report.limit_a, 200)
        self.assertEqual(report.limit_b, 150)
        self.assertEqual(report.strength(Channel.A), 30)
        self.assertEqual(report.limit(Channel.B), 150)

    def test_strength_report_rejects_other_shapes(self):
        self.assertIsNone(parse_strength_report("strength-1+2+3"))
        self.assertIsNone(parse_strength_report("pulse-A:[]"))
        self.assertIsNone(parse_strength_report(None))

    def test_feedback(self):
        self.assertEqual(parse_feedback("feedback-3"), 3)
        self.assertEqual(parse_feedback("feedback-9"), 9)
        self.assertIsNone(parse_feedback("feedback-10"))
        self.assertIsNone(parse_feedback("feedback-x"))
        self.assertIsNone(parse_feedback(None))


class RetCodeTests(unittest.TestCase):
    def test_descriptions(self):
        self.assertIn("成功", describe_ret_code(200))
        self.assertIn("對方", describe_ret_code(209))
        self.assertIn("未知", describe_ret_code(999))
        self.assertIn("無回傳碼", describe_ret_code(None))

    def test_server_frame_defaults(self):
        frame = ServerFrame(type="msg")
        self.assertIsNone(frame.ret_code)


if __name__ == "__main__":
    unittest.main()
