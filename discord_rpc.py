"""Discord Rich Presence 的 IPC 協定（自己實作，不依賴第三方套件）。

Discord 桌面版會在本機開一個 IPC 端點，協定非常單純：

- Windows：具名管線 ``\\\\.\\pipe\\discord-ipc-0`` ～ ``9``
- Linux / macOS：``$XDG_RUNTIME_DIR/discord-ipc-0`` 之類的 Unix socket

每個封包長這樣::

    +----------------+----------------+---------------------------+
    | opcode (int32) | 長度 (int32)   |  JSON 內容（UTF-8）       |
    |  little-endian |  little-endian |                           |
    +----------------+----------------+---------------------------+

opcode：``0`` 握手、``1`` 一般指令、``2`` 關閉、``3``  ping、``4`` pong。

連線物件是用注入的（``connection`` 參數），所以測試可以用假的端點把整個
握手與送指令的流程跑完，不需要真的開著 Discord。
"""

from __future__ import annotations

import json
import logging
import os
import socket
import struct
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger("dglab-cs2.discord")

OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2
OP_PING = 3
OP_PONG = 4

OP_NAMES = {
    OP_HANDSHAKE: "HANDSHAKE",
    OP_FRAME: "FRAME",
    OP_CLOSE: "CLOSE",
    OP_PING: "PING",
    OP_PONG: "PONG",
}

HEADER_SIZE = 8
HEADER_FORMAT = "<ii"

#: 單一封包的大小上限（Discord 官方 SDK 用的是 1 MB）
MAX_PAYLOAD_SIZE = 1 << 20

#: 具名管線／Unix socket 的編號範圍
PIPE_COUNT = 10

#: Discord 的活動欄位長度限制
MIN_TEXT_LENGTH = 2
MAX_TEXT_LENGTH = 128

#: 圖片 asset key 的長度上限（Discord 沒有公開明文，取一個安全的值）
MAX_ASSET_KEY_LENGTH = 256


class DiscordIPCError(RuntimeError):
    """與 Discord 的 IPC 溝通失敗。"""


# ------------------------------------------------------------------ 編解碼


def encode_message(opcode: int, payload: dict[str, Any]) -> bytes:
    """把一個封包編成 bytes。"""
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_PAYLOAD_SIZE:
        raise DiscordIPCError(f"封包太大（{len(body)} bytes）")
    return struct.pack(HEADER_FORMAT, opcode, len(body)) + body


def decode_header(raw: bytes) -> tuple[int, int]:
    """解析 8 bytes 的標頭，回傳 ``(opcode, 長度)``。

    Raises:
        DiscordIPCError: 長度不對、或長度值不合理。
    """
    if len(raw) != HEADER_SIZE:
        raise DiscordIPCError(f"標頭長度必須是 {HEADER_SIZE} bytes，收到 {len(raw)}")
    opcode, length = struct.unpack(HEADER_FORMAT, raw)
    if not 0 <= length <= MAX_PAYLOAD_SIZE:
        raise DiscordIPCError(f"封包長度不合理：{length}")
    return opcode, length


def decode_payload(raw: bytes) -> dict[str, Any]:
    """解析 JSON 內容。"""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiscordIPCError(f"Discord 回傳的內容不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise DiscordIPCError("Discord 回傳的內容不是物件")
    return payload


def named_pipe_paths(count: int = PIPE_COUNT) -> list[str]:
    """Windows 的具名管線候選路徑。"""
    return [rf"\\.\pipe\discord-ipc-{index}" for index in range(count)]


def unix_socket_paths(count: int = PIPE_COUNT) -> list[str]:
    """Linux / macOS 的 Unix socket 候選路徑。"""
    base = (
        os.environ.get("XDG_RUNTIME_DIR")
        or os.environ.get("TMPDIR")
        or os.environ.get("TMP")
        or "/tmp"
    )
    return [os.path.join(base, f"discord-ipc-{index}") for index in range(count)]


def default_endpoint_paths() -> list[str]:
    """依作業系統挑出要依序嘗試的端點。"""
    if sys.platform.startswith("win"):
        return named_pipe_paths()
    return unix_socket_paths()


# ------------------------------------------------------------------ 連線


class DiscordConnection(Protocol):
    """IPC 端點的最小介面。"""

    def read(self, size: int) -> bytes: ...

    def write(self, data: bytes) -> None: ...

    def close(self) -> None: ...


class NamedPipeConnection:
    """Windows 具名管線。

    Python 可以把管線當普通檔案開；讀寫都是阻塞式的，
    所以上層會用看門狗執行緒避免卡死。
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._file = open(path, "r+b", buffering=0)

    def read(self, size: int) -> bytes:
        return self._file.read(size)

    def write(self, data: bytes) -> None:
        self._file.write(data)
        self._file.flush()

    def close(self) -> None:
        try:
            self._file.close()
        except OSError:  # pragma: no cover - 已經關掉就算了
            pass


class UnixSocketConnection:
    """Linux / macOS 的 Unix socket（有真正的逾時可用）。"""

    def __init__(self, path: str, *, timeout: float = 10.0) -> None:
        self.path = path
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.settimeout(timeout)
        try:
            self._socket.connect(path)
        except OSError:
            self._socket.close()
            raise

    def read(self, size: int) -> bytes:
        return self._socket.recv(size)

    def write(self, data: bytes) -> None:
        self._socket.sendall(data)

    def close(self) -> None:
        try:
            self._socket.close()
        except OSError:  # pragma: no cover
            pass


def open_connection(path: str, *, timeout: float = 10.0) -> DiscordConnection:
    """開啟一個 IPC 端點。"""
    if sys.platform.startswith("win"):
        return NamedPipeConnection(path)
    return UnixSocketConnection(path, timeout=timeout)


def connect(paths: list[str] | None = None, *, timeout: float = 10.0) -> tuple[DiscordConnection, str]:
    """依序嘗試各候選端點，回傳 ``(連線, 路徑)``。

    Raises:
        DiscordIPCError: 全部嘗試都失敗（最常見的原因是 Discord 沒開）。
    """
    tried = paths if paths is not None else default_endpoint_paths()
    errors: list[str] = []
    for path in tried:
        try:
            return open_connection(path, timeout=timeout), path
        except OSError as exc:
            errors.append(f"{path}（{exc}）")
    detail = "\n".join(f"  • {item}" for item in errors) if errors else "  （沒有候選端點）"
    raise DiscordIPCError("連不上 Discord，請確認 Discord 桌面版正在執行：\n" + detail)


# ------------------------------------------------------------------ 用戶端


@dataclass(frozen=True)
class DiscordResponse:
    """Discord 回傳的一個封包。"""

    opcode: int
    payload: dict[str, Any]

    @property
    def command(self) -> str:
        return str(self.payload.get("cmd") or "")

    @property
    def event(self) -> str | None:
        event = self.payload.get("evt")
        return str(event) if event else None

    @property
    def is_error(self) -> bool:
        return self.event == "ERROR"

    def describe_error(self) -> str:
        data = self.payload.get("data")
        if isinstance(data, dict):
            code = data.get("code")
            message = data.get("message")
            if code is not None or message is not None:
                return f"{message or '未知錯誤'}（代碼 {code}）"
        return str(self.payload)


class DiscordIPC:
    """Discord IPC 的用戶端：握手、送活動、關閉。"""

    def __init__(
        self,
        connection: DiscordConnection,
        *,
        application_id: str,
        pid: int | None = None,
        timeout: float = 10.0,
        nonce_factory=None,
    ) -> None:
        self._connection = connection
        self._application_id = str(application_id)
        self._pid = os.getpid() if pid is None else pid
        self._timeout = timeout
        self._nonce_factory = nonce_factory or (lambda: str(uuid.uuid4()))
        self._closed = False
        self._ready = False

    # -------------------------------------------------------- 低階收送

    def _send(self, opcode: int, payload: dict[str, Any]) -> None:
        if self._closed:
            raise DiscordIPCError("連線已關閉")
        data = encode_message(opcode, payload)
        try:
            self._connection.write(data)
        except OSError as exc:
            raise DiscordIPCError(f"寫入 Discord 失敗：{exc}") from exc

    def _read_exact(self, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            chunk = self._read_with_timeout(remaining)
            if not chunk:
                raise DiscordIPCError("Discord 關閉了連線")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_with_timeout(self, size: int) -> bytes:
        """讀一次，並且保證不會無限期卡住。

        具名管線沒有逾時機制，所以在這裡開一個看門狗執行緒；
        真的等太久就把連線關掉，讓被卡住的讀取收到錯誤後結束。
        """
        outcome: dict[str, Any] = {}

        def worker() -> None:
            try:
                outcome["data"] = self._connection.read(size)
            except BaseException as exc:  # noqa: BLE001 - 一律回報給呼叫者
                outcome["error"] = exc

        thread = threading.Thread(target=worker, name="discord-ipc-read", daemon=True)
        thread.start()
        thread.join(self._timeout)
        if thread.is_alive():
            self._abort()
            thread.join(2.0)
            raise DiscordIPCError(f"等待 Discord 回應逾時（超過 {self._timeout:g} 秒）")
        if "error" in outcome:
            error = outcome["error"]
            if isinstance(error, DiscordIPCError):
                raise error
            raise DiscordIPCError(f"讀取 Discord 失敗：{error}")
        return outcome.get("data") or b""

    def _abort(self) -> None:
        self._closed = True
        try:
            self._connection.close()
        except OSError:  # pragma: no cover
            pass

    def receive(self) -> DiscordResponse:
        """收一個封包。"""
        opcode, length = decode_header(self._read_exact(HEADER_SIZE))
        body = self._read_exact(length) if length else b"{}"
        return DiscordResponse(opcode=opcode, payload=decode_payload(body))

    def _receive_until(self, nonce: str, *, limit: int = 20) -> DiscordResponse:
        """一直收到指定 nonce 的回應為止（其餘事件略過）。"""
        last: DiscordResponse | None = None
        for _ in range(limit):
            response = self.receive()
            if response.opcode == OP_PING:
                # 對方主動 ping 要回 pong
                self._send(OP_PONG, response.payload)
                continue
            if response.opcode == OP_CLOSE:
                raise DiscordIPCError("Discord 主動關閉了連線")
            last = response
            if str(response.payload.get("nonce") or "") == nonce or response.is_error:
                return response
        raise DiscordIPCError(
            "Discord 一直沒回覆對應的指令"
            + (f"（最後收到：{last.command or last.event}）" if last else "")
        )

    # -------------------------------------------------------- 高階操作

    def handshake(self) -> dict[str, Any]:
        """握手；成功後回傳 READY 的內容。"""
        self._send(OP_HANDSHAKE, {"v": 1, "client_id": self._application_id})
        # 握手的回應沒有 nonce，直接收第一個 FRAME
        for _ in range(10):
            response = self.receive()
            if response.opcode == OP_PING:
                self._send(OP_PONG, response.payload)
                continue
            if response.is_error:
                raise DiscordIPCError(
                    f"Discord 拒絕了握手：{response.describe_error()}"
                )
            if response.opcode != OP_FRAME:
                continue
            if response.event == "READY":
                self._ready = True
                data = response.payload.get("data")
                return data if isinstance(data, dict) else {}
            # 其他事件（例如先來的 DISPATCH）就當作已就緒
            self._ready = True
            return {}
        raise DiscordIPCError("Discord 沒有回應握手")

    def set_activity(self, activity: dict[str, Any]) -> None:
        """更新 Rich Presence。"""
        nonce = self._nonce_factory()
        self._send(
            OP_FRAME,
            {
                "cmd": "SET_ACTIVITY",
                "args": {"pid": self._pid, "activity": activity},
                "nonce": nonce,
            },
        )
        response = self._receive_until(nonce)
        if response.is_error:
            raise DiscordIPCError(f"Discord 拒絕了狀態更新：{response.describe_error()}")

    def clear_activity(self) -> None:
        """清掉 Rich Presence。"""
        nonce = self._nonce_factory()
        self._send(
            OP_FRAME,
            {"cmd": "SET_ACTIVITY", "args": {"pid": self._pid}, "nonce": nonce},
        )
        response = self._receive_until(nonce)
        if response.is_error:
            raise DiscordIPCError(f"Discord 拒絕了清除指令：{response.describe_error()}")

    def close(self) -> None:
        """送 CLOSE 並關閉連線。"""
        if self._closed:
            return
        try:
            self._send(OP_CLOSE, {})
        except DiscordIPCError:
            pass
        self._abort()

    @property
    def ready(self) -> bool:
        return self._ready and not self._closed


def clamp_text(text: str, *, fallback: str = "　") -> str:
    """把文字修到 Discord 接受的長度（2 ~ 128 字）。"""
    value = (text or "").strip() or fallback
    if len(value) < MIN_TEXT_LENGTH:
        value = value + "\u200b" * (MIN_TEXT_LENGTH - len(value))
    return value[:MAX_TEXT_LENGTH]


def clamp_asset_key(key: str) -> str:
    """把圖片 asset key 修到合理長度。"""
    return (key or "").strip()[:MAX_ASSET_KEY_LENGTH]


def build_activity(
    *,
    details: str,
    state: str,
    start_timestamp: float | None = None,
    large_image: str = "",
    large_text: str = "",
    small_image: str = "",
    small_text: str = "",
) -> dict[str, Any]:
    """組出 SET_ACTIVITY 的 activity 物件。

    圖片是用「asset key」指定的：必須先在 Discord 開發者頁面的
    ``Rich Presence → Art Assets`` 上傳圖片並幫它取名，這裡填那個名稱。

    ``large_text`` / ``small_text`` 是滑鼠移上去顯示的提示文字；
    沒有對應圖片時 Discord 會忽略它們，所以這裡也只在圖片存在時一起送。
    """
    activity: dict[str, Any] = {
        "details": clamp_text(details),
        "state": clamp_text(state),
    }
    if start_timestamp is not None:
        activity["timestamps"] = {"start": int(start_timestamp * 1000)}

    assets: dict[str, str] = {}
    key = clamp_asset_key(large_image)
    if key:
        assets["large_image"] = key
        if large_text:
            assets["large_text"] = clamp_text(large_text)
    key = clamp_asset_key(small_image)
    if key:
        assets["small_image"] = key
        if small_text:
            assets["small_text"] = clamp_text(small_text)
    if assets:
        activity["assets"] = assets
    return activity


def describe_connection(path: str) -> str:
    """給記錄用的端點描述。"""
    return path


# ------------------------------------------------------- 應用程式的圖片素材


#: 查詢應用程式 Rich Presence 素材清單的端點。
#:
#: 這個端點**不需要任何 token**（帶錯的 ID 會回 404 Unknown Application），
#: 所以使用者只要填 Application ID 就能看到自己上傳過哪些圖片。
ASSETS_URL = "https://discord.com/api/v10/oauth2/applications/{application_id}/assets"

#: 素材型別：1 = 上傳時選 Small，2 = 選 Big
ASSET_TYPE_SMALL = 1
ASSET_TYPE_BIG = 2

#: 查詢素材的清單大小上限（避免逾時太久）
ASSETS_TIMEOUT = 10.0


class DiscordAssetError(RuntimeError):
    """查詢應用程式素材時失敗。"""


@dataclass(frozen=True)
class DiscordAsset:
    """應用程式的一張 Rich Presence 素材。"""

    #: 素材的 snowflake id
    id: str = ""
    #: 素材名稱 —— 這就是 ``large_image`` / ``small_image`` 要填的值
    name: str = ""
    #: ``big`` / ``small`` / ``other``
    kind: str = "other"

    @property
    def is_big(self) -> bool:
        return self.kind == "big"

    @property
    def is_small(self) -> bool:
        return self.kind == "small"


def _asset_kind(type_value: Any) -> str:
    if type_value == ASSET_TYPE_BIG:
        return "big"
    if type_value == ASSET_TYPE_SMALL:
        return "small"
    return "other"


def parse_assets(raw: Any) -> tuple[DiscordAsset, ...]:
    """把 API 回傳的 JSON 轉成 :class:`DiscordAsset` 清單。

    沒有名稱的素材會被略過（填進 Rich Presence 也沒有意義）。
    """
    if not isinstance(raw, list):
        raise DiscordAssetError("Discord 回傳的素材清單格式不對")
    assets: list[DiscordAsset] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        assets.append(
            DiscordAsset(
                id=str(item.get("id") or ""),
                name=name,
                kind=_asset_kind(item.get("type")),
            )
        )
    return tuple(assets)


def order_assets_for(assets: tuple[DiscordAsset, ...], slot: str) -> list[str]:
    """排出下拉選單要顯示的順序。

    上傳時標成 Big 的素材排在大圖示前面，Small 的排在小圖示前面；
    兩種其實都可以互換使用，所以只是排序、不是過濾。
    """
    prefer = "small" if slot == "small" else "big"
    preferred = [asset.name for asset in assets if asset.kind == prefer]
    others = [asset.name for asset in assets if asset.kind != prefer]
    return preferred + others


def _http_get_json(url: str, timeout: float) -> Any:
    """抓一個 JSON 端點（``urllib``，不需要額外套件）。"""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "DGLAB-CS2",
            "Accept": "application/json",
            # 這個端點會被 CDN 快取，不講清楚會拿到舊的素材清單
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _cache_buster() -> str:
    """加上時間戳，繞過 CDN 快取。

    實際踩到的問題：剛上傳的圖片，這個端點會先回舊的清單（Cloudflare 快取），
    使用者按了「讀取圖片清單」卻看到已經刪掉的名字。
    """
    return str(int(time.time() * 1000))


def fetch_assets(
    application_id: str,
    *,
    timeout: float = ASSETS_TIMEOUT,
    opener=None,
) -> tuple[DiscordAsset, ...]:
    """查詢某個應用程式上傳過的所有 Rich Presence 素材。

    Args:
        application_id: Discord 應用程式的 Application ID（純數字）。
        timeout: 連線逾時秒數。
        opener: 測試用；預設是 ``urllib`` 的實作，可替換成假的。

    Raises:
        DiscordAssetError: ID 格式不對、找不到應用程式、或連線失敗。
    """
    import urllib.error

    app_id = (application_id or "").strip()
    if not app_id:
        raise DiscordAssetError("請先填 Application ID")
    if not app_id.isdigit():
        raise DiscordAssetError("Application ID 必須是純數字")
    if not 15 <= len(app_id) <= 20:
        raise DiscordAssetError(
            f"Application ID 長度看起來不對（{len(app_id)} 位數字），請重新複製一次"
        )

    url = ASSETS_URL.format(application_id=app_id) + f"?t={_cache_buster()}"
    getter = opener or _http_get_json
    try:
        raw = getter(url, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise DiscordAssetError(
                "找不到這個 Application ID 的應用程式，請確認沒有複製錯"
            ) from None
        if exc.code == 400:
            raise DiscordAssetError("Discord 說這個 Application ID 格式不對") from None
        if exc.code == 429:
            raise DiscordAssetError("查詢太頻繁，請等一下再試") from None
        raise DiscordAssetError(f"Discord 回覆錯誤（HTTP {exc.code}）") from None
    except urllib.error.URLError as exc:
        raise DiscordAssetError(f"連不上 Discord（{exc.reason}）") from None
    except (OSError, ValueError) as exc:
        raise DiscordAssetError(f"讀取圖片清單失敗：{exc}") from None

    return parse_assets(raw)


def wait_briefly(seconds: float) -> None:
    """小睡一下（測試會換掉）。"""
    time.sleep(seconds)
