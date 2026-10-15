"""DG-LAB Socket 客戶端：配對、強度控制、波形串流。

和藍牙的差別在於：Socket 模式下波形是「把 100ms 的波形單位排進 App 的佇列」，
由 App 自己照著播。所以這裡有一個背景 task 負責把佇列維持在一個很小的領先量
（預設 0.5 ~ 3 秒）之內：

- 領先量太小 -> 補一批波形單位，避免 App 播完之後中斷。
- 領先量太大 -> 先不補，讓它自然消化；這樣即使長時間掛機也不會把 App 的
  500 單位（50 秒）佇列塞滿，導致後面的變化延遲 50 秒才出現。

需要「立刻」反應的時候（例如受擊要同步電擊）則是用 ``clear`` 清空佇列後
馬上補上爆發波形，這樣就沒有等待佇列消化的延遲。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable

from .socket_protocol import (
    Channel,
    MessageType,
    ServerFrame,
    StrengthOperation,
    StrengthReport,
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
    FEEDBACK_BUTTONS,
)
from .transport import SocketTransport
from .waveform import FRAME_SECONDS, ChannelWave, WaveCycle, WaveFrame, clamp

logger = logging.getLogger(__name__)

#: 串流迴圈的檢查間隔（秒）
STREAM_TICK_SECONDS = 0.25
#: 佇列至少要領先幾個波形單位（0.5 秒）
MIN_LEAD_FRAMES = 5
#: 佇列最多領先幾個波形單位（3 秒）
MAX_LEAD_FRAMES = 30
#: 註冊（取得 clientId）的預設逾時
DEFAULT_REGISTER_TIMEOUT = 20.0


class SocketError(RuntimeError):
    """Socket 連線或協定層級的錯誤。"""


@dataclass
class ChannelState:
    """單一通道的輸出狀態。"""

    strength: int = 0
    #: 是否輸出波形（False 時串流會送「暫停」波形，也就是不出力）
    active: bool = False
    #: 目前套用到通道上的波形循環
    cycle: WaveCycle = field(default_factory=lambda: WaveCycle([]))

    def set_wave(self, frames: Iterable[WaveFrame]) -> None:
        self.cycle = WaveCycle(frames)


class DglabSocketClient:
    """與 DG-LAB App 透過 Socket 中繼伺服器通訊的控制器。"""

    def __init__(
        self,
        transport: SocketTransport,
        *,
        uri: str,
        channels: Iterable[Channel] = (Channel.A,),
        wave_frames: Iterable[WaveFrame] = (),
        push_min_lead: int = MIN_LEAD_FRAMES,
        push_max_lead: int = MAX_LEAD_FRAMES,
        stream_tick: float = STREAM_TICK_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self._uri = uri
        self._channels = frozenset(Channel.from_value(channel) for channel in channels)
        if not self._channels:
            raise ValueError("至少要啟用一個通道")

        self._frames = tuple(wave_frames)
        self._push_min_lead = max(1, int(push_min_lead))
        self._push_max_lead = max(self._push_min_lead + 1, int(push_max_lead))
        self._stream_tick = float(stream_tick)
        self._clock = clock

        self._channel_state: dict[Channel, ChannelState] = {}
        for channel in self._channels:
            state = ChannelState()
            state.set_wave(self._frames)
            self._channel_state[channel] = state

        self.client_id: str | None = None
        self.target_id: str | None = None
        self.report: StrengthReport | None = None
        self.last_feedback: int | None = None

        #: 送出卻還沒被播掉的波形單位數量估計
        self._pushed_frames = 0
        self._stream_epoch = self._clock()

        self._registered = asyncio.Event()
        self._closed = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._fatal_error: Exception | None = None

        #: App 斷線時會被呼叫（通常是重新顯示 QR code）
        self.on_unpaired: Callable[[], Awaitable[None] | None] | None = None

        self.sent_count = 0
        self.send_error_count = 0

    # ---------------------------------------------------------------- 屬性

    @property
    def channels(self) -> frozenset[Channel]:
        return self._channels

    @property
    def uri(self) -> str:
        return self._uri

    @property
    def is_registered(self) -> bool:
        return self.client_id is not None

    @property
    def is_paired(self) -> bool:
        return self.client_id is not None and self.target_id is not None

    @property
    def running(self) -> bool:
        return not self._closed.is_set() and self._fatal_error is None

    @property
    def fatal_error(self) -> Exception | None:
        return self._fatal_error

    @property
    def qr_url(self) -> str | None:
        """給 DG-LAB App 掃描的 QR code 內容。"""
        if self.client_id is None:
            return None
        return qrcode_url(self._uri, self.client_id)

    def channel_state(self, channel: Channel) -> ChannelState:
        return self._channel_state[Channel.from_value(channel)]

    def target_strength(self, channel: Channel) -> int:
        """本程式最後一次設定給該通道的強度；未啟用的通道固定為 0。"""
        state = self._channel_state.get(Channel.from_value(channel))
        return state.strength if state is not None else 0

    # ------------------------------------------------------------ 生命週期

    async def start(self, *, register_timeout: float = DEFAULT_REGISTER_TIMEOUT) -> None:
        """連線並等待服務端分配 ``clientId``。"""
        await self._transport.connect()
        self._tasks.append(asyncio.create_task(self._receiver(), name="dglab-receiver"))
        self._tasks.append(asyncio.create_task(self._streamer(), name="dglab-streamer"))

        if not await self.wait_registered(register_timeout):
            # 別把背景 task 留在原地
            for task in self._tasks:
                task.cancel()
            self._tasks.clear()
            raise SocketError(
                f"連上 {self._uri} 之後 {register_timeout:.0f} 秒內沒有收到 clientId，"
                "請確認服務端位址是否正確（官方為 wss://ws.dungeon-lab.cn/）"
            )
        logger.info("已向服務端註冊，clientId=%s", self.client_id)

    async def wait_registered(self, timeout: float | None = None) -> bool:
        """等待服務端分配 ``clientId``。"""
        try:
            await asyncio.wait_for(self._registered.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return True

    async def close(self) -> None:
        """停止串流、把裝置靜音，然後關閉連線。"""
        if self._closed.is_set():
            return
        self._closed.set()

        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - 收尾時不該再拋錯
                logger.debug("停止背景工作時發生例外", exc_info=True)
        self._tasks.clear()

        if self.is_paired:
            try:
                await self.silence()
            except Exception:
                logger.warning("結束前無法把裝置靜音，請手動確認 App 上的強度", exc_info=True)

        await self._transport.close()

    async def silence(self) -> None:
        """把兩個通道歸零並清空波形佇列。"""
        for channel in self._channels:
            await self.clear_pulses(channel)
            await self.set_strength(channel, 0)
        self._pushed_frames = 0
        self._stream_epoch = self._clock()

    # ------------------------------------------------------------ 對外指令

    async def set_strength(self, channel: Channel, value: int) -> None:
        """把通道強度設定為指定值（0 ~ 200）。"""
        channel = Channel.from_value(channel)
        value = clamp(int(value), 0, 200)
        state = self._channel_state.get(channel)
        if state is None:
            return
        state.strength = value
        await self._send(strength_command(channel, StrengthOperation.SET_TO, value))

    async def add_strength(self, channel: Channel, delta: int) -> None:
        """相對調整通道強度（正數增加、負數減少）。"""
        channel = Channel.from_value(channel)
        delta = int(delta)
        if delta == 0:
            return
        operation = StrengthOperation.INCREASE if delta > 0 else StrengthOperation.DECREASE
        state = self._channel_state.get(channel)
        if state is not None:
            state.strength = clamp(state.strength + delta, 0, 200)
        await self._send(strength_command(channel, operation, abs(delta)))

    async def add_pulses(self, channel: Channel, frames: Iterable[ChannelWave]) -> int:
        """把波形單位加入 App 的佇列，並回傳加入的數量。"""
        channel = Channel.from_value(channel)
        count = await self._push(channel, frames)
        self._pushed_frames += count
        return count

    async def _push(self, channel: Channel, frames: Iterable[ChannelWave]) -> int:
        """實際送出波形，不更新領先量估計。"""
        items = list(frames)
        if not items:
            return 0
        for batch in chunk_frames(items):
            await self._send(pulse_command(channel, batch))
        return len(items)

    async def clear_pulses(self, channel: Channel) -> None:
        """清空該通道的波形佇列。"""
        channel = Channel.from_value(channel)
        await self._send(clear_command(channel))
        self._pushed_frames = 0
        self._stream_epoch = self._clock()

    def set_wave(self, frames: Iterable[WaveFrame]) -> None:
        """換掉所有通道的波形循環。"""
        self._frames = tuple(frames)
        for state in self._channel_state.values():
            state.set_wave(self._frames)

    def set_output_active(self, channel: Channel, active: bool) -> None:
        """設定該通道是否要出力（False = 之後送出的波形都是靜音）。"""
        self.channel_state(channel).active = bool(active)

    def is_output_active(self, channel: Channel) -> bool:
        return self.channel_state(channel).active

    async def fire_burst(self, channel: Channel, seconds: float) -> int:
        """立刻清空佇列並補上一段爆發波形，用於「受擊同步電擊」。

        之所以要先用 ``clear`` 清空，是因為佇列裡可能還排著幾秒的靜音波形，
        不清掉的話爆發不會立刻出現。

        Returns:
            實際送出的波形單位數量。
        """
        channel = Channel.from_value(channel)
        count = max(1, int(round(float(seconds) / FRAME_SECONDS)))
        await self.clear_pulses(channel)
        frames = self._build_frames(channel, count, force_active=True)
        await self.add_pulses(channel, frames)
        return len(frames)

    def effective_limit(self, channel: Channel, configured_max: int) -> int:
        """實際可用的強度上限。

        若 App 上設定的強度上限比本程式低，就以 App 的為準，避免我們一直
        送超出上限的值卻怎麼按都沒反應。
        """
        limit = int(configured_max)
        if self.report is not None and self.target_id is not None:
            limit = min(limit, self.report.limit(Channel.from_value(channel)))
        return clamp(limit, 0, 200)

    def app_strength(self, channel: Channel) -> int | None:
        """App 目前回報的通道強度。"""
        if self.report is None:
            return None
        return self.report.strength(Channel.from_value(channel))

    # ------------------------------------------------------------ 內部實作

    async def _send(self, message: str) -> None:
        if not self.is_paired:
            raise SocketError("尚未與 App 完成配對，無法送出指令")
        text = build_message(
            MessageType.MSG,
            message,
            client_id=self.client_id,
            target_id=self.target_id,
        )
        await self._transport.send(text)
        self.sent_count += 1
        logger.debug("送出：%s", message)

    async def _receiver(self) -> None:
        try:
            while not self._closed.is_set():
                try:
                    text = await self._transport.recv()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not self._closed.is_set():
                        self._fatal_error = exc
                        logger.error("Socket 連線中斷：%s", exc)
                    return
                self._handle_text(text)
        except asyncio.CancelledError:
            raise

    def _handle_text(self, text: str) -> None:
        try:
            frame = parse_server_frame(text)
        except ValueError:
            logger.debug("忽略無法解析的服務端訊息：%r", text[:200])
            return
        self._handle_frame(frame)

    def _handle_frame(self, frame: ServerFrame) -> None:
        if frame.type == MessageType.BIND:
            if frame.is_initial_bind():
                self.client_id = frame.client_id
                self._registered.set()
                logger.debug("服務端分配 clientId=%s", frame.client_id)
                return
            if frame.is_bind_success():
                self.target_id = frame.target_id
                logger.info("已與 App 完成配對，targetId=%s", self.target_id)
                return
            code = frame.ret_code
            logger.error(
                "配對失敗：%s（%s）", describe_ret_code(code), code if code is not None else "?"
            )
            return

        if frame.type == MessageType.BREAK:
            logger.warning(
                "App 已斷開連線（%s），請重新掃描 QR code",
                describe_ret_code(frame.ret_code),
            )
            self._handle_unpaired()
            return

        if frame.type == MessageType.HEARTBEAT:
            logger.debug("收到心跳：%s", frame.message)
            return

        if frame.type == MessageType.ERROR:
            logger.error("服務端回報錯誤：%s", frame.message)
            return

        if frame.type == MessageType.MSG:
            report = parse_strength_report(frame.message)
            if report is not None:
                self.report = report
                logger.debug(
                    "App 回報強度 A=%d B=%d（上限 %d / %d）",
                    report.strength_a,
                    report.strength_b,
                    report.limit_a,
                    report.limit_b,
                )
                return
            feedback = parse_feedback(frame.message)
            if feedback is not None:
                self.last_feedback = feedback
                logger.info(
                    "App 按下按鈕：%s",
                    FEEDBACK_BUTTONS.get(feedback, f"索引 {feedback}"),
                )
                return
            logger.debug("收到其他訊息：%r", frame.message)
            return

        logger.debug("收到未處理的訊息類型 %r：%r", frame.type, frame.message)

    def _handle_unpaired(self) -> None:
        was_paired = self.target_id is not None
        self.target_id = None
        self.report = None
        for state in self._channel_state.values():
            state.strength = 0
            state.active = False
        self._pushed_frames = 0
        self._stream_epoch = self._clock()
        if was_paired and self.on_unpaired is not None:
            result = self.on_unpaired()
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)

    # -------------------------------------------------------------- 串流

    def _build_frames(
        self, channel: Channel, count: int, *, force_active: bool = False
    ) -> list[ChannelWave]:
        state = self.channel_state(channel)
        active = state.active or force_active
        if not active:
            return [ChannelWave.pause() for _ in range(count)]
        return [state.cycle.next_wave() for _ in range(count)]

    def _lead_frames(self) -> float:
        """估計 App 佇列裡還剩幾個波形單位。"""
        elapsed = self._clock() - self._stream_epoch
        consumed = elapsed / FRAME_SECONDS if elapsed > 0 else 0.0
        return self._pushed_frames - consumed

    async def _streamer(self) -> None:
        """維持 App 的波形佇列，讓輸出不會中斷。"""
        try:
            while not self._closed.is_set():
                await asyncio.sleep(self._stream_tick)
                if not self.is_paired:
                    continue
                try:
                    await self.stream_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.send_error_count += 1
                    logger.exception("補波形時發生錯誤（會在下個週期重試）")
        except asyncio.CancelledError:
            raise

    async def stream_once(self) -> int:
        """檢查領先量，必要時補一批波形；回傳實際補了幾個波形單位。

        正常由背景的串流 task 呼叫；測試或需要手動同步時也可以直接呼叫。
        """
        lead = self._lead_frames()
        if lead >= self._push_min_lead:
            return 0
        needed = int(self._push_max_lead - lead)
        if needed <= 0:
            return 0

        pushed = 0
        for channel in sorted(self._channels, key=int):
            frames = self._build_frames(channel, needed)
            pushed = await self._push(channel, frames)
        # 所有通道的佇列是以同樣速度消耗的，領先量只需要算一次
        self._pushed_frames += pushed
        logger.debug(
            "補波形：領先量 %.1f -> %d 個單位（每個通道各 %d 個）",
            lead,
            self._push_max_lead,
            pushed,
        )
        return pushed
