"""DG-LAB Socket 客戶端的端到端測試（用假的服務端連線）。"""

from __future__ import annotations

import asyncio
import json
import unittest

from coyote.socket_client import DglabSocketClient, SocketError
from coyote.socket_protocol import Channel, qrcode_url
from coyote.waveform import ChannelWave, WaveFrame
from tests.support import FakeClock, FakeSocketTransport

URI = "wss://example.invalid/"
PAUSE_HEX = "0A0A0A0A00000000"
ACTIVE_HEX = "0A0A0A0A64646464"


class ClientTestCase(unittest.IsolatedAsyncioTestCase):
    """建立一個已註冊但還沒配對的客戶端。"""

    channels = (Channel.A,)

    async def asyncSetUp(self):
        self.transport = FakeSocketTransport()
        self.transport.feed_initial_bind()
        self.clock = FakeClock()
        self.unpaired_calls: list[bool] = []
        self.client = DglabSocketClient(
            self.transport,
            uri=URI,
            channels=self.channels,
            wave_frames=[WaveFrame(10, 100)],
            # 背景串流調到幾乎不會動，測試改成手動呼叫 stream_once()
            stream_tick=60.0,
            clock=self.clock,
        )
        self.client.on_unpaired = lambda: self.unpaired_calls.append(True)
        await self.client.start(register_timeout=1.0)

    async def asyncTearDown(self):
        await self.client.close()

    async def pair(self):
        self.transport.feed_bind_success()
        for _ in range(100):
            if self.client.is_paired:
                return
            await asyncio.sleep(0.01)
        self.fail("等了 1 秒還是沒有配對成功")

    async def settle(self):
        """讓接收 task 有機會處理佇列中的訊息。"""
        for _ in range(5):
            await asyncio.sleep(0)


class RegistrationTests(ClientTestCase):
    async def test_client_id_and_qr_url(self):
        self.assertTrue(self.client.is_registered)
        self.assertEqual(self.client.client_id, "client-1")
        self.assertFalse(self.client.is_paired)
        self.assertEqual(self.client.qr_url, qrcode_url(URI, "client-1"))

    async def test_qr_url_is_none_before_registration(self):
        transport = FakeSocketTransport()
        client = DglabSocketClient(transport, uri=URI, channels=(Channel.A,))
        self.assertIsNone(client.qr_url)

    async def test_registration_timeout(self):
        transport = FakeSocketTransport()
        client = DglabSocketClient(transport, uri=URI, channels=(Channel.A,))
        with self.assertRaises(SocketError) as ctx:
            await client.start(register_timeout=0.05)
        self.assertIn("clientId", str(ctx.exception))
        await client.close()

    async def test_pairing_success(self):
        await self.pair()
        self.assertEqual(self.client.target_id, "app-1")
        self.assertTrue(self.client.is_paired)


class PairingFailureTests(ClientTestCase):
    async def test_bind_failure_does_not_pair(self):
        self.transport.feed_raw(
            {
                "type": "bind",
                "clientId": "client-1",
                "targetId": "app-1",
                "message": "400",
            }
        )
        await self.settle()
        self.assertFalse(self.client.is_paired)

    async def test_break_resets_pairing(self):
        await self.pair()
        self.transport.feed_break()
        for _ in range(100):
            if not self.client.is_paired:
                break
            await asyncio.sleep(0.01)
        self.assertFalse(self.client.is_paired)
        self.assertIsNone(self.client.target_id)
        self.assertEqual(self.unpaired_calls, [True])


class CommandTests(ClientTestCase):
    async def test_send_before_pairing_raises(self):
        with self.assertRaises(SocketError):
            await self.client.set_strength(Channel.A, 10)

    async def test_set_strength_uses_set_to_and_carries_ids(self):
        await self.pair()
        await self.client.set_strength(Channel.A, 25)
        message = self.transport.messages[-1]
        self.assertEqual(message, "strength-1+2+25")
        frame = self.transport.frames[-1]
        self.assertEqual(frame["type"], "msg")
        self.assertEqual(frame["clientId"], "client-1")
        self.assertEqual(frame["targetId"], "app-1")

    async def test_set_strength_is_clamped(self):
        await self.pair()
        await self.client.set_strength(Channel.A, 999)
        self.assertEqual(self.transport.messages[-1], "strength-1+2+200")
        self.assertEqual(self.client.target_strength(Channel.A), 200)

    async def test_add_strength(self):
        await self.pair()
        await self.client.add_strength(Channel.A, 5)
        self.assertEqual(self.transport.messages[-1], "strength-1+1+5")
        await self.client.add_strength(Channel.A, -3)
        self.assertEqual(self.transport.messages[-1], "strength-1+0+3")

    async def test_add_strength_zero_is_noop(self):
        await self.pair()
        before = len(self.transport.outgoing)
        await self.client.add_strength(Channel.A, 0)
        self.assertEqual(len(self.transport.outgoing), before)

    async def test_add_pulses_and_clear(self):
        await self.pair()
        await self.client.add_pulses(Channel.A, [ChannelWave.steady(10, 100)])
        self.assertEqual(self.transport.messages[-1], f'pulse-A:["{ACTIVE_HEX}"]')
        await self.client.clear_pulses(Channel.A)
        self.assertEqual(self.transport.messages[-1], "clear-1")

    async def test_disabled_channel_is_ignored(self):
        client = DglabSocketClient(
            self.transport, uri=URI, channels=(Channel.A,), stream_tick=60.0
        )
        await client.set_strength(Channel.B, 50)
        self.assertEqual(client.target_strength(Channel.B), 0)


class DisabledChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_channel_a_is_commanded(self):
        transport = FakeSocketTransport()
        transport.feed_initial_bind()
        client = DglabSocketClient(
            transport, uri=URI, channels=(Channel.A,), stream_tick=60.0
        )
        await client.start(register_timeout=1.0)
        self.addAsyncCleanup(client.close)
        transport.feed_bind_success()
        for _ in range(100):
            if client.is_paired:
                break
            await asyncio.sleep(0.01)

        await client.set_strength(Channel.A, 10)
        await client.set_strength(Channel.B, 99)
        # B 通道沒有啟用，設定值會被忽略
        self.assertEqual(client.target_strength(Channel.B), 0)
        self.assertIn("strength-1+2+10", transport.messages)
        self.assertNotIn("strength-2+2+99", transport.messages)


class StreamerTests(ClientTestCase):
    async def test_inactive_pushes_pause_frames(self):
        await self.pair()
        await self.client.stream_once()
        frames = self.transport.pulse_frames("A")
        self.assertTrue(frames)
        self.assertTrue(all(frame == PAUSE_HEX for frame in frames))

    async def test_active_pushes_wave_frames(self):
        await self.pair()
        self.client.set_output_active(Channel.A, True)
        await self.client.stream_once()
        frames = self.transport.pulse_frames("A")
        self.assertEqual(len(frames), 30)  # MAX_LEAD_FRAMES
        self.assertTrue(all(frame == ACTIVE_HEX for frame in frames))

    async def test_stream_once_respects_lead(self):
        await self.pair()
        self.client.set_output_active(Channel.A, True)
        first = await self.client.stream_once()
        self.assertEqual(first, 30)
        # 領先量還很夠，不應該再補
        self.assertEqual(await self.client.stream_once(), 0)
        # 過了一秒（消耗 10 個單位）之後還是夠
        self.clock.advance(1.0)
        self.assertEqual(await self.client.stream_once(), 0)
        # 再過兩秒，領先量見底，應該補滿
        self.clock.advance(2.0)
        self.assertEqual(await self.client.stream_once(), 30)

    async def test_pause_frames_do_not_advance_wave(self):
        await self.pair()
        self.client.set_output_active(Channel.A, False)
        await self.client.stream_once()
        self.assertEqual(self.client.channel_state(Channel.A).cycle.cursor, 0)

    async def test_fire_burst_clears_then_pushes(self):
        await self.pair()
        await self.client.stream_once()  # 先塞一些暫停波形進去
        sent_before = len(self.transport.outgoing)
        count = await self.client.fire_burst(Channel.A, 0.5)
        self.assertEqual(count, 5)
        new_messages = self.transport.messages[sent_before:]
        self.assertEqual(new_messages[0], "clear-1")
        self.assertTrue(new_messages[1].startswith("pulse-A:["))
        self.assertEqual(len(json.loads(new_messages[1][len("pulse-A:"):])), 5)
        # 爆發一定是「有出力」的波形，即使通道目前是關閉的
        self.assertTrue(
            all(frame == ACTIVE_HEX for frame in self.transport.pulse_frames("A")[-5:])
        )

    async def test_set_wave_replaces_cycle(self):
        await self.pair()
        self.client.set_wave([WaveFrame(40, 70)])
        self.client.set_output_active(Channel.A, True)
        await self.client.stream_once()
        self.assertEqual(self.transport.pulse_frames("A")[-1], ChannelWave.steady(40, 70).to_hex())


class AppReportTests(ClientTestCase):
    async def test_strength_report_is_parsed(self):
        await self.pair()
        self.transport.feed_message("strength-30+5+200+150")
        for _ in range(100):
            if self.client.report is not None:
                break
            await asyncio.sleep(0.01)
        self.assertIsNotNone(self.client.report)
        self.assertEqual(self.client.app_strength(Channel.A), 30)
        self.assertEqual(self.client.app_strength(Channel.B), 5)

    async def test_effective_limit_respects_app_limit(self):
        await self.pair()
        self.assertEqual(self.client.effective_limit(Channel.A, 80), 80)
        self.transport.feed_message("strength-0+0+200+40")
        for _ in range(100):
            if self.client.report is not None:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.client.effective_limit(Channel.A, 80), 80)
        self.assertEqual(self.client.effective_limit(Channel.B, 80), 40)
        self.assertEqual(self.client.effective_limit(Channel.A, 30), 30)

    async def test_feedback_is_parsed(self):
        await self.pair()
        self.transport.feed_message("feedback-7")
        for _ in range(100):
            if self.client.last_feedback is not None:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.client.last_feedback, 7)

    async def test_heartbeat_and_unknown_messages_are_ignored(self):
        await self.pair()
        self.transport.feed_heartbeat()
        self.transport.feed_message("something-else")
        self.transport.feed("this is not json")
        await self.settle()
        self.assertTrue(self.client.running)

    async def test_error_frame_is_handled(self):
        await self.pair()
        self.transport.feed_raw({"type": "error", "message": "500"})
        await self.settle()
        self.assertTrue(self.client.running)


class CloseTests(ClientTestCase):
    async def test_close_silences_and_disconnects(self):
        await self.pair()
        await self.client.set_strength(Channel.A, 40)
        sent_before = len(self.transport.outgoing)
        await self.client.close()
        new_messages = self.transport.messages[sent_before:]
        self.assertIn("clear-1", new_messages)
        self.assertIn("strength-1+2+0", new_messages)
        self.assertEqual(self.transport.close_count, 1)
        self.assertFalse(self.client.running)

    async def test_close_is_idempotent(self):
        await self.pair()
        await self.client.close()
        count = self.transport.close_count
        await self.client.close()
        self.assertEqual(self.transport.close_count, count)

    async def test_close_without_pairing(self):
        # 還沒配對就結束，不應該送出任何指令，也不該拋錯
        before = len(self.transport.outgoing)
        await self.client.close()
        self.assertEqual(len(self.transport.outgoing), before)


class TransportFailureTests(ClientTestCase):
    async def test_fatal_error_stops_the_client(self):
        await self.pair()
        self.transport.fail_recv_with(ConnectionError("藍牙…不對，是網路斷了"))
        for _ in range(100):
            if not self.client.running:
                break
            await asyncio.sleep(0.01)
        self.assertFalse(self.client.running)
        self.assertIsInstance(self.client.fatal_error, ConnectionError)


if __name__ == "__main__":
    unittest.main()
