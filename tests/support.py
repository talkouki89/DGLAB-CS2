"""測試用的共用工具。

提供假的 Socket 傳輸層與假的時鐘，讓整個客戶端邏輯可以完全離線測試。
"""

from __future__ import annotations

import asyncio
import json
import struct
import threading


class FakeClock:
    """可以手動推進的假時鐘。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class FakeSocketTransport:
    """假的 DG-LAB Socket 服務端連線。

    測試可以用 :meth:`feed` / :meth:`feed_initial_bind` 之類的方法扮演服務端，
    並用 :attr:`messages` 檢查客戶端送了哪些指令。
    """

    def __init__(self) -> None:
        self.outgoing: list[str] = []
        self.connected = False
        self.close_count = 0
        self._incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self._fail_recv: Exception | None = None

    # --------------------------------------------------------- 傳輸層介面

    @property
    def description(self) -> str:
        return "fake-socket"

    async def connect(self) -> None:
        self.connected = True

    async def send(self, text: str) -> None:
        self.outgoing.append(text)

    async def recv(self) -> str:
        if self._fail_recv is not None:
            raise self._fail_recv
        text = await self._incoming.get()
        if text is None:
            raise ConnectionError("假的連線已關閉")
        return text

    async def close(self) -> None:
        self.connected = False
        self.close_count += 1
        self._incoming.put_nowait(None)

    # ------------------------------------------------------------ 測試輔助

    def fail_recv_with(self, error: Exception) -> None:
        """讓之後的 ``recv`` 直接失敗，用來模擬連線中斷。"""
        self._fail_recv = error
        # 叫醒正在等訊息的接收迴圈
        self._incoming.put_nowait("")

    def feed(self, text: str) -> None:
        self._incoming.put_nowait(text)

    def feed_raw(self, payload: dict) -> None:
        self.feed(json.dumps(payload))

    def feed_initial_bind(self, client_id: str = "client-1") -> None:
        """服務端分配 clientId。"""
        self.feed_raw(
            {
                "type": "bind",
                "clientId": client_id,
                "targetId": "",
                "message": "targetId",
            }
        )

    def feed_bind_success(
        self, client_id: str = "client-1", target_id: str = "app-1"
    ) -> None:
        """App 掃碼完成配對。"""
        self.feed_raw(
            {
                "type": "bind",
                "clientId": client_id,
                "targetId": target_id,
                "message": "200",
            }
        )

    def feed_message(self, message: str, client_id: str = "app-1") -> None:
        """App 送來的指令／回報。"""
        self.feed_raw(
            {
                "type": "msg",
                "clientId": client_id,
                "targetId": "client-1",
                "message": message,
            }
        )

    def feed_break(self, code: str = "209") -> None:
        self.feed_raw(
            {
                "type": "break",
                "clientId": "app-1",
                "targetId": "client-1",
                "message": code,
            }
        )

    def feed_heartbeat(self, message: str = "200") -> None:
        self.feed_raw(
            {
                "type": "heartbeat",
                "clientId": "client-1",
                "targetId": "app-1",
                "message": message,
            }
        )

    # --------------------------------------------------------------- 檢查

    @property
    def frames(self) -> list[dict]:
        return [json.loads(text) for text in self.outgoing]

    @property
    def messages(self) -> list[str]:
        """所有送出的 ``message`` 欄位。"""
        return [frame.get("message", "") for frame in self.frames]

    def messages_of(self, prefix: str) -> list[str]:
        return [message for message in self.messages if message.startswith(prefix)]

    def pulse_frames(self, channel: str = "A") -> list[str]:
        """把該通道所有送出的波形單位攤平成一個清單。"""
        result: list[str] = []
        prefix = f"pulse-{channel}:"
        for message in self.messages_of(prefix):
            payload = message[len(prefix) :]
            result.extend(json.loads(payload))
        return result


# --------------------------------------------------------- Discord RPC 用


class FakeDiscordPipe:
    """假的 Discord IPC 端點，會**照真正的協定**回應。

    所以這一組可以驗證編解碼、握手、SET_ACTIVITY 的完整來回，
    而不需要真的開著 Discord。
    """

    def __init__(
        self,
        *,
        ready_event: str = "READY",
        reject_handshake: bool = False,
        reject_activity: bool = False,
        silent: bool = False,
    ) -> None:
        from discord_rpc import OP_CLOSE, OP_FRAME, OP_HANDSHAKE, encode_message

        self._encode = encode_message
        self._op_close = OP_CLOSE
        self._op_frame = OP_FRAME
        self._op_handshake = OP_HANDSHAKE

        self.ready_event = ready_event
        self.reject_handshake = reject_handshake
        self.reject_activity = reject_activity
        self.silent = silent

        self.sent: list[tuple[int, dict]] = []
        self.closed = False
        self._inbox = bytearray()
        self._wakeup = threading.Event()

    # ------------------------------------------------------- 連線層介面

    def write(self, data: bytes) -> None:
        opcode, payload = self._decode(data)
        self.sent.append((opcode, payload))
        if self.silent:
            return
        if opcode == self._op_handshake:
            if self.reject_handshake:
                self._push(
                    self._op_frame,
                    {
                        "cmd": "DISPATCH",
                        "evt": "ERROR",
                        "data": {"code": 4000, "message": "無效的用戶端 ID"},
                    },
                )
            else:
                self._push(
                    self._op_frame,
                    {
                        "cmd": "DISPATCH",
                        "evt": self.ready_event,
                        "data": {"v": 1, "user": {"username": "tester"}},
                    },
                )
        elif opcode == self._op_frame and payload.get("cmd") == "SET_ACTIVITY":
            if self.reject_activity:
                self._push(
                    self._op_frame,
                    {
                        "cmd": "SET_ACTIVITY",
                        "evt": "ERROR",
                        "nonce": payload.get("nonce"),
                        "data": {"code": 4001, "message": "活動內容不合法"},
                    },
                )
            else:
                self._push(
                    self._op_frame,
                    {
                        "cmd": "SET_ACTIVITY",
                        "evt": None,
                        "nonce": payload.get("nonce"),
                        "data": payload.get("args", {}).get("activity"),
                    },
                )

    def read(self, size: int) -> bytes:
        while not self._inbox:
            if self.closed:
                raise OSError("假的管線已關閉")
            if not self._wakeup.wait(0.05):
                continue
        chunk = bytes(self._inbox[:size])
        del self._inbox[:size]
        return chunk

    def close(self) -> None:
        self.closed = True
        self._wakeup.set()

    # ------------------------------------------------------------ 測試輔助

    def _push(self, opcode: int, payload: dict) -> None:
        self._inbox.extend(self._encode(opcode, payload))
        self._wakeup.set()

    @staticmethod
    def _decode(data: bytes) -> tuple[int, dict]:
        opcode, length = struct.unpack("<ii", data[:8])
        return opcode, json.loads(data[8 : 8 + length].decode("utf-8"))

    @property
    def activities(self) -> list[dict]:
        """所有送出的 activity 物件。"""
        return [
            payload["args"]["activity"]
            for opcode, payload in self.sent
            if payload.get("cmd") == "SET_ACTIVITY" and "activity" in payload.get("args", {})
        ]

    @property
    def commands(self) -> list[str]:
        return [str(payload.get("cmd")) for _, payload in self.sent]


class FakeDiscordIPC:
    """假的 :class:`discord_rpc.DiscordIPC`，只記錄被要求顯示什麼。"""

    def __init__(
        self,
        connection=None,
        *,
        application_id: str = "",
        fail_handshake: bool = False,
        fail_activity: bool = False,
        ready: bool = True,
    ) -> None:
        self.connection = connection
        self.application_id = application_id
        self.fail_handshake = fail_handshake
        self.fail_activity = fail_activity
        self.ready = False
        self.activities: list[dict] = []
        self.closed = False
        self._ready_after_handshake = ready

    def handshake(self) -> dict:
        from discord_rpc import DiscordIPCError

        if self.fail_handshake:
            raise DiscordIPCError("假的握手失敗")
        self.ready = self._ready_after_handshake
        return {}

    def set_activity(self, activity: dict) -> None:
        from discord_rpc import DiscordIPCError

        if self.fail_activity:
            raise DiscordIPCError("假的更新失敗")
        self.activities.append(activity)

    def clear_activity(self) -> None:
        self.activities.clear()

    def close(self) -> None:
        self.closed = True
        self.ready = False


class FakeIPCFactory:
    """產生假的 IPC 用戶端，並保留每個實例供檢查。"""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.instances: list[FakeDiscordIPC] = []
        self.connections: list[object] = []

    def __call__(self, connection, *, application_id):
        self.connections.append(connection)
        ipc = FakeDiscordIPC(connection, application_id=application_id, **self.kwargs)
        self.instances.append(ipc)
        return ipc

    @property
    def last(self) -> FakeDiscordIPC:
        return self.instances[-1]


class FakeDiscordConnector:
    """假的端點連線函式，可以設定要失敗幾次。"""

    def __init__(self, *, failures: int = 0, path: str = r"\\.\pipe\discord-ipc-0") -> None:
        self.failures = failures
        self.path = path
        self.calls = 0

    def __call__(self):
        from discord_rpc import DiscordIPCError

        self.calls += 1
        if self.calls <= self.failures:
            raise DiscordIPCError("連不上 Discord（假的）")
        return object(), self.path
