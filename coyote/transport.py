"""DG-LAB Socket 的傳輸層。

把「怎麼連線」跟「要送什麼」分開，這樣 :mod:`coyote.socket_client` 可以在
沒有網路的情況下用假傳輸層做完整的端到端測試。

``websockets`` 只在真的要連線時才 import，所以單元測試不需要安裝它。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

#: DG-LAB 官方 Socket 服務端
DEFAULT_URI = "wss://ws.dungeon-lab.cn/"


@runtime_checkable
class SocketTransport(Protocol):
    """WebSocket 傳輸層介面。"""

    @property
    def connected(self) -> bool:
        """是否仍連線中。"""

    @property
    def description(self) -> str:
        """人類可讀的連線描述。"""

    async def connect(self) -> None:
        """建立連線。"""

    async def send(self, text: str) -> None:
        """送出一則文字訊息。"""

    async def recv(self) -> str:
        """收取一則文字訊息；連線關閉時應拋出例外。"""

    async def close(self) -> None:
        """關閉連線。"""


def _websockets_connect():
    """取得 ``websockets`` 的 ``connect``，相容於新舊版本。"""
    try:  # websockets >= 13
        from websockets.asyncio.client import connect  # type: ignore
    except ImportError:  # pragma: no cover - 舊版
        from websockets.client import connect  # type: ignore
    return connect


class WebsocketsTransport:
    """透過 ``websockets`` 連上 DG-LAB Socket 服務端。"""

    def __init__(
        self,
        uri: str = DEFAULT_URI,
        *,
        open_timeout: float = 15.0,
        close_timeout: float = 5.0,
        ping_interval: float | None = 20.0,
        ping_timeout: float | None = 20.0,
    ) -> None:
        self._uri = uri
        self._open_timeout = open_timeout
        self._close_timeout = close_timeout
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._ws = None

    @property
    def uri(self) -> str:
        return self._uri

    @property
    def connected(self) -> bool:
        if self._ws is None:
            return False
        closed = getattr(self._ws, "close_code", None)
        return closed is None

    @property
    def description(self) -> str:
        return f"Socket {self._uri}"

    async def connect(self) -> None:
        connect = _websockets_connect()
        logger.info("連線到 DG-LAB Socket 服務端 %s …", self._uri)
        # ping_interval 會在連線閒置時自動送出協定層 ping，
        # 避免中繼伺服器把閒置連線切掉。
        self._ws = await connect(
            self._uri,
            open_timeout=self._open_timeout,
            close_timeout=self._close_timeout,
            ping_interval=self._ping_interval,
            ping_timeout=self._ping_timeout,
            max_size=2**20,
        )
        logger.info("已連上 DG-LAB Socket 服務端")

    async def send(self, text: str) -> None:
        if self._ws is None:
            raise ConnectionError("尚未連上 DG-LAB Socket 服務端")
        await self._ws.send(text)

    async def recv(self) -> str:
        if self._ws is None:
            raise ConnectionError("尚未連上 DG-LAB Socket 服務端")
        return await self._ws.recv()

    async def close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is None:
            return
        try:
            await ws.close()
        except Exception:  # pragma: no cover - 收尾時不該再拋錯
            logger.debug("關閉 Socket 時發生例外", exc_info=True)
        logger.info("已關閉 DG-LAB Socket 連線")


class NullTransport:
    """不真的連線的假傳輸層，用於 ``--dry-run``。

    可以透過 :meth:`feed` 把假的服務端訊息排進佇列，所以 dry-run 走的其實是
    和正式執行完全相同的註冊／配對流程，只是沒有真的碰到網路。
    """

    def __init__(
        self,
        uri: str = DEFAULT_URI,
        *,
        log_interval: float = 0.0,
        max_log_chars: int = 180,
    ) -> None:
        self._uri = uri
        self._connected = False
        self._log_interval = float(log_interval)
        self._max_log_chars = int(max_log_chars)
        self._last_log_at = 0.0
        self._incoming: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[str] = []

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def description(self) -> str:
        return f"dry-run（未連線，假裝連到 {self._uri}）"

    def feed(self, text: str) -> None:
        """排入一則假的服務端訊息。"""
        self._incoming.put_nowait(text)

    @staticmethod
    def bind_frame(client_id: str, target_id: str = "", message: str = "targetId") -> str:
        import json

        return json.dumps(
            {
                "type": "bind",
                "clientId": client_id,
                "targetId": target_id,
                "message": message,
            }
        )

    async def connect(self) -> None:
        self._connected = True
        logger.info("[dry-run] 假裝連上 %s，不會真的控制裝置", self._uri)

    async def send(self, text: str) -> None:
        self.sent.append(text)
        logger.debug("[dry-run] 送出（完整）%s", text)
        now = asyncio.get_running_loop().time()
        if self._log_interval and now - self._last_log_at < self._log_interval:
            return
        self._last_log_at = now
        # 波形訊息常常很長（一次 25 個波形單位），截短才看得清楚
        if len(text) > self._max_log_chars:
            text = f"{text[:self._max_log_chars]}…（共 {len(text)} 字元）"
        logger.info("[dry-run] 送出 %s", text)

    async def recv(self) -> str:
        if not self._connected:
            raise ConnectionError("dry-run 傳輸層已關閉")
        return await self._incoming.get()

    async def close(self) -> None:
        self._connected = False
        logger.info("[dry-run] 總共送出 %d 則訊息", len(self.sent))
