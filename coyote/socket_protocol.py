"""DG-LAB 郊狼 3.0 Socket（WebSocket）協定的編碼與解碼。

這個模組只做純資料處理，完全不碰網路，所以可以直接單元測試。

協定依據：

- DG-LAB 官方 Socket 開放接口（``https://ws.dungeon-lab.cn/``）
- 社群文件與實作交叉驗證：
  `coyote-claw WebSocket 架构 <https://qiekn.github.io/coyote-claw/project/>`_、
  `PyDGLab-WS <https://github.com/Ljz-PRO/PyDGLab-WS>`_、
  `dglab-kit <https://github.com/dungeonlab-open/dglab-kit>`_

訊息一律是 JSON 物件，四個欄位都是字串::

    {"type": "msg", "clientId": "...", "targetId": "...", "message": "..."}

連線與配對流程：

1. 控制器連上 WebSocket 服務端。
2. 服務端下發 ``{"type":"bind","clientId":"<控制器ID>","message":"targetId"}``，
   控制器記下 ``clientId`` 並用它產生 QR code。
3. 使用者用 DG-LAB App 掃描 QR code，App 連上同一個服務端。
4. 服務端下發 ``{"type":"bind","clientId":"<控制器ID>","targetId":"<AppID>","message":"200"}``，
   配對完成，之後所有指令都帶上這兩個 ID。

指令（控制器 -> App，放在 ``message`` 裡）：

======================  ================================================
指令                     說明
======================  ================================================
``strength-<通>+<模>+<值>``  通道強度。通道 1=A、2=B；模式 0=降低、1=增加、2=設定為
``pulse-<通>:[...]``     波形佇列。通道是字母 A/B，內容是十六進位波形單位陣列
``clear-<通>``           清空波形佇列。通道是數字 1/2
======================  ================================================

回報（App -> 控制器）：

- ``strength-<A強度>+<B強度>+<A上限>+<B上限>``
- ``feedback-<0~9>``（App 上的按鈕）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Iterable, Sequence

from .waveform import ChannelWave

#: 單一 WebSocket 訊息的長度上限，超過會被 App 丟棄
MAX_MESSAGE_LENGTH = 1950

#: 單一 ``pulse-`` 指令最多可以帶幾個波形單位（協定上限 100）
MAX_PULSE_FRAMES_PER_MESSAGE = 80

#: App 端波形佇列的最大長度（500 個單位 = 50 秒）
APP_QUEUE_MAX_FRAMES = 500


class MessageType:
    """WebSocket 訊息類型。"""

    BIND = "bind"
    MSG = "msg"
    BREAK = "break"
    HEARTBEAT = "heartbeat"
    ERROR = "error"


class RetCode(IntEnum):
    """服務端回傳碼。"""

    SUCCESS = 200
    CLIENT_DISCONNECTED = 209
    INVALID_CLIENT_ID = 210
    SERVER_DELAY = 211
    ID_ALREADY_BOUND = 400
    TARGET_CLIENT_NOT_FOUND = 401
    INCOMPATIBLE_RELATIONSHIP = 402
    NON_JSON_CONTENT = 403
    RECIPIENT_NOT_FOUND = 404
    MESSAGE_TOO_LONG = 405
    SERVER_INTERNAL_ERROR = 500


#: 服務端回傳碼的中文說明，用於記錄
RET_CODE_DESCRIPTION: dict[int, str] = {
    RetCode.SUCCESS: "成功",
    RetCode.CLIENT_DISCONNECTED: "對方已斷開連線",
    RetCode.INVALID_CLIENT_ID: "QR code 中沒有有效的 clientId",
    RetCode.SERVER_DELAY: "服務端延遲，尚未下發 App 的 ID",
    RetCode.ID_ALREADY_BOUND: "這個 ID 已被其他控制器綁定",
    RetCode.TARGET_CLIENT_NOT_FOUND: "要綁定的 App 不存在",
    RetCode.INCOMPATIBLE_RELATIONSHIP: "控制器與 App 不是綁定關係",
    RetCode.NON_JSON_CONTENT: "送出的內容不是合法 JSON",
    RetCode.RECIPIENT_NOT_FOUND: "收訊方不在線",
    RetCode.MESSAGE_TOO_LONG: f"訊息長度超過 {MAX_MESSAGE_LENGTH}",
    RetCode.SERVER_INTERNAL_ERROR: "服務端內部錯誤",
}


def describe_ret_code(code: int | None) -> str:
    if code is None:
        return "（無回傳碼）"
    return RET_CODE_DESCRIPTION.get(int(code), f"未知回傳碼 {code}")


class Channel(IntEnum):
    """郊狼通道。

    - ``Channel.A.value == 1``、``Channel.B.value == 2``（強度、清空指令使用）
    - ``Channel.A.name == "A"``、``Channel.B.name == "B"``（波形指令使用）
    """

    A = 1
    B = 2

    @property
    def letter(self) -> str:
        return self.name

    @classmethod
    def from_value(cls, value: object) -> "Channel":
        """接受 ``1``/``2``/``"A"``/``"B"`` 等各種寫法。"""
        if isinstance(value, Channel):
            return value
        if isinstance(value, str):
            key = value.strip().upper()
            if key in ("A", "1"):
                return cls.A
            if key in ("B", "2"):
                return cls.B
        if value in (1, "1"):
            return cls.A
        if value in (2, "2"):
            return cls.B
        raise ValueError(f"無法辨識的通道：{value!r}")

    @classmethod
    def parse_all(cls, value: object) -> frozenset["Channel"]:
        """解析 ``"A"`` / ``"B"`` / ``"AB"`` / ``["A","B"]`` 之類的通道設定。"""
        if isinstance(value, Channel):
            return frozenset({value})
        if isinstance(value, (set, frozenset, list, tuple)):
            result: set[Channel] = set()
            for item in value:
                result.add(cls.from_value(item))
            return frozenset(result)
        text = str(value).strip().upper().replace(" ", "").replace(",", "")
        if not text:
            return frozenset()
        if text in ("AB", "BA", "A+B", "BOTH"):
            return frozenset({cls.A, cls.B})
        result = set()
        for char in text:
            result.add(cls.from_value(char))
        return frozenset(result)


class StrengthOperation(IntEnum):
    """通道強度變化模式。"""

    DECREASE = 0
    INCREASE = 1
    SET_TO = 2


# ----------------------------------------------------------------- 編碼


def strength_command(
    channel: Channel | int | str, operation: StrengthOperation | int, value: int
) -> str:
    """組出強度指令，例如 ``strength-1+2+30``（A 通道設為 30）。"""
    ch = Channel.from_value(channel)
    op = StrengthOperation(int(operation))
    amount = int(value)
    if not 0 <= amount <= 200:
        raise ValueError(f"強度值 {amount} 超出範圍 (0 ~ 200)")
    return f"strength-{ch.value}+{op.value}+{amount}"


def clear_command(channel: Channel | int | str) -> str:
    """組出清空波形佇列的指令，例如 ``clear-1``。"""
    return f"clear-{Channel.from_value(channel).value}"


def pulse_command(
    channel: Channel | int | str, frames: Iterable[ChannelWave | str]
) -> str:
    """組出波形指令，例如 ``pulse-A:["0A0A0A0A64646464"]``。

    Args:
        channel: 波形通道（指令中使用字母 ``A``/``B``）。
        frames: 波形單位，可以是 :class:`~coyote.waveform.ChannelWave`
            或已經是十六進位字串。

    Raises:
        ValueError: 沒有任何波形單位，或超過單一訊息的長度上限。
    """
    ch = Channel.from_value(channel)
    hexes = [frame.to_hex() if isinstance(frame, ChannelWave) else str(frame) for frame in frames]
    if not hexes:
        raise ValueError("波形指令至少要有一個波形單位")
    for item in hexes:
        if len(item) != 16:
            raise ValueError(f"波形單位必須是 8 bytes（16 個十六進位字元），收到 {item!r}")
    message = f"pulse-{ch.letter}:{json.dumps(hexes, separators=(',', ':'))}"
    if len(message) > MAX_MESSAGE_LENGTH:
        raise ValueError(
            f"波形指令長度 {len(message)} 超過上限 {MAX_MESSAGE_LENGTH}，"
            f"請減少單次送出的波形單位數量（目前 {len(hexes)} 個）"
        )
    return message


def chunk_frames(
    frames: Sequence[ChannelWave | str],
    limit: int = MAX_PULSE_FRAMES_PER_MESSAGE,
) -> list[list[ChannelWave | str]]:
    """把波形單位切成好幾個小批次，避免單一訊息超過長度上限。"""
    if limit < 1:
        raise ValueError("limit 必須大於 0")
    return [list(frames[index : index + limit]) for index in range(0, len(frames), limit)]


def build_message(
    message_type: str,
    message: str,
    *,
    client_id: str | None = None,
    target_id: str | None = None,
) -> str:
    """組出要送給服務端的 JSON 訊息。"""
    return json.dumps(
        {
            "type": message_type,
            "clientId": client_id or "",
            "targetId": target_id or "",
            "message": message,
        },
        separators=(",", ":"),
    )


def qrcode_url(uri: str, client_id: str) -> str:
    """組出 DG-LAB App 要掃的 QR code 內容。

    Args:
        uri: WebSocket 服務端位址，例如 ``wss://ws.dungeon-lab.cn``
            （結尾的 ``/`` 會被去掉）。
        client_id: 服務端分配給控制器的 ID。
    """
    base = str(uri).strip().rstrip("/")
    return f"https://www.dungeon-lab.com/app-download.php#DGLAB-SOCKET#{base}/{client_id}"


# ----------------------------------------------------------------- 解碼


@dataclass(frozen=True)
class ServerFrame:
    """服務端下發的訊息。"""

    type: str
    client_id: str | None = None
    target_id: str | None = None
    message: str | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def ret_code(self) -> int | None:
        """``message`` 若是數字就當成回傳碼。"""
        if self.message is None:
            return None
        try:
            return int(self.message)
        except (TypeError, ValueError):
            return None

    def is_bind_success(self) -> bool:
        return self.type == MessageType.BIND and self.ret_code == RetCode.SUCCESS

    def is_initial_bind(self) -> bool:
        """服務端分配 ``clientId`` 的初始 bind（此時還沒有 ``targetId``）。"""
        return (
            self.type == MessageType.BIND
            and self.client_id is not None
            and not self.target_id
        )


@dataclass(frozen=True)
class StrengthReport:
    """App 回報的強度與強度上限。"""

    strength_a: int
    strength_b: int
    limit_a: int
    limit_b: int

    def strength(self, channel: Channel) -> int:
        return self.strength_a if channel is Channel.A else self.strength_b

    def limit(self, channel: Channel) -> int:
        return self.limit_a if channel is Channel.A else self.limit_b


def parse_server_frame(text: str) -> ServerFrame:
    """解析服務端送來的 JSON 訊息。

    Raises:
        ValueError: 不是合法 JSON、或不是物件。
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"服務端訊息不是合法 JSON：{text[:200]!r}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"服務端訊息不是 JSON 物件：{text[:200]!r}")

    message = payload.get("message")
    if message is not None and not isinstance(message, str):
        message = str(message)

    return ServerFrame(
        type=str(payload.get("type", "")),
        client_id=payload.get("clientId") or None,
        target_id=payload.get("targetId") or None,
        message=message,
        raw=payload,
    )


def parse_strength_report(message: str | None) -> StrengthReport | None:
    """解析 App 回報的 ``strength-<A>+<B>+<A上限>+<B上限>``。"""
    if not message or not message.startswith("strength-"):
        return None
    try:
        values = message.split("-", 1)[1].split("+")
        if len(values) != 4:
            return None
        a, b, a_limit, b_limit = (int(item) for item in values)
    except (IndexError, ValueError):
        return None
    return StrengthReport(
        strength_a=a, strength_b=b, limit_a=a_limit, limit_b=b_limit
    )


def parse_feedback(message: str | None) -> int | None:
    """解析 App 按鈕回報的 ``feedback-<0~9>``。"""
    if not message or not message.startswith("feedback-"):
        return None
    try:
        index = int(message.split("-", 1)[1])
    except (IndexError, ValueError):
        return None
    return index if 0 <= index <= 9 else None


#: App 按鈕索引 -> 通道／位置說明
FEEDBACK_BUTTONS: dict[int, str] = {
    0: "A 通道按鈕 1", 1: "A 通道按鈕 2", 2: "A 通道按鈕 3",
    3: "A 通道按鈕 4", 4: "A 通道按鈕 5",
    5: "B 通道按鈕 1", 6: "B 通道按鈕 2", 7: "B 通道按鈕 3",
    8: "B 通道按鈕 4", 9: "B 通道按鈕 5",
}
