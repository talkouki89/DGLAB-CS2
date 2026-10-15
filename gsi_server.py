"""CS2 Game State Integration 的接收伺服器。

CS2 會把遊戲狀態以 HTTP POST 丟到 ``gamestate_integration_*.cfg`` 裡設定的
``uri``。這個模組負責接收、驗證 token、解析，並記錄「最後一次收到資料的時間」
供安全看門狗使用。

與原版相比的修正：

- 回應標頭順序修正（原本先 ``send_header`` 才 ``send_response``，送出的其實是
  格式錯誤的 HTTP 回應）。
- 改用 :class:`~http.server.ThreadingHTTPServer`，避免某個請求卡住就整個停擺。
- 補上 ``Content-Length``，避免 HTTP/1.1 連線一直掛著。
- 記錄最後收到資料的時間，讓主程式能在遊戲關掉之後自動把強度歸零。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import gamestate
import payloadparser
from config import GsiConfig
from game_events import PlayerSnapshot, payload_is_mine

logger = logging.getLogger(__name__)

#: 同一個 token 問題最多幾秒提醒一次，避免每筆 GSI 資料都洗一行
AUTH_WARNING_INTERVAL = 60.0


def extract_token(payload: Any) -> str | None:
    """取出 GSI 資料裡的 auth token；沒有 auth 區塊時回傳 ``None``。"""
    if not isinstance(payload, dict):
        return None
    auth = payload.get("auth")
    if not isinstance(auth, dict):
        return None
    token = auth.get("token")
    return token if isinstance(token, str) else None


def describe_payload_source(payload: Any) -> str:
    """描述這筆資料是誰送的，方便判斷是哪個 cfg 檔在講話。"""
    if not isinstance(payload, dict):
        return "不是 JSON 物件"
    provider = payload.get("provider")
    if not isinstance(provider, dict):
        return "沒有 provider 資訊（可能不是遊戲送來的）"
    name = provider.get("name") or "未知的程式"
    appid = provider.get("appid")
    return f"{name}（appid {appid}）" if appid is not None else str(name)


class GSIServer(ThreadingHTTPServer):
    """接收 CS2 GSI 資料的 HTTP 伺服器。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: GsiConfig) -> None:
        super().__init__((config.host, config.port), GSIRequestHandler)
        self.auth_token = config.token
        self.gamestate_manager = gamestate.GameStateManager()
        self.payload_parser = payloadparser.PayloadParser()

        self._lock = threading.Lock()
        self._last_payload_at: float | None = None
        self._last_auth_warning_at: float | None = None
        self.payload_count = 0
        self.rejected_count = 0

        #: 「自己」最後一次已知的血量／燃燒。觀戰別人時會沿用這些值，
        #: 而不是被觀戰者的（見 :meth:`snapshot`）。
        self._own_health = 100
        self._own_burning = 0
        self._own_health_known = False

        #: 收到有效資料時要通知誰（由引擎設定；會在 HTTP 執行緒被呼叫）
        self.on_payload: Callable[[], None] | None = None

    def notify_payload(self) -> None:
        """通知等待中的控制迴圈有新資料。

        這個方法是在 HTTP 處理執行緒裡被呼叫的，所以實作必須是執行緒安全的
        （引擎會用 ``loop.call_soon_threadsafe`` 轉到 asyncio 迴圈上）。
        """
        callback = self.on_payload
        if callback is None:
            return
        try:
            callback()
        except Exception:  # pragma: no cover - 通知失敗不該影響接收
            logger.debug("通知新資料時發生例外", exc_info=True)

    # ------------------------------------------------------------ 狀態存取

    def record_payload(self) -> None:
        with self._lock:
            self._last_payload_at = time.monotonic()
            self.payload_count += 1
            # 收到合法資料就重置，之後如果再出問題會立刻再提醒一次
            self._last_auth_warning_at = None

    def note_auth_failure(self) -> tuple[bool, int]:
        """記錄一次 token 不符。

        Returns:
            ``(是否該印出警告, 累計被拒次數)``。同一個問題每
            :data:`AUTH_WARNING_INTERVAL` 秒最多提醒一次，避免洗版。
        """
        with self._lock:
            self.rejected_count += 1
            now = time.monotonic()
            if (
                self._last_auth_warning_at is not None
                and now - self._last_auth_warning_at < AUTH_WARNING_INTERVAL
            ):
                return False, self.rejected_count
            self._last_auth_warning_at = now
            return True, self.rejected_count

    def seconds_since_last_payload(self) -> float | None:
        """距離上次收到合法 GSI 資料過了幾秒；從未收到時回傳 ``None``。"""
        with self._lock:
            last = self._last_payload_at
        if last is None:
            return None
        return time.monotonic() - last

    def snapshot(self) -> PlayerSnapshot:
        """回傳目前與遊戲事件有關的狀態。

        這裡會處理一個很重要的 CS2 行為：**死亡之後觀戰別人時，GSI 的
        ``player`` 區塊會變成「被觀戰那個人」的資料**。如果不處理，血量會從
        0 跳成別人的血量（被誤判成復活），強度也會跟著別人的血量跑。

        判斷方式是比對 ``player.steamid`` 與 ``provider.steamid``（自己）。
        只要不是自己的資料，就繼續沿用自己最後一次已知的血量，
        並把 ``spectating`` 標成 True。

        CS2 的 heartbeat 封包不含 ``player_state``，因此不能只憑
        「收到過資料」就相信血量的值，``health_known`` 就是判斷依據。
        """
        game = self.gamestate_manager.gamestate
        state = game.player.state
        stats = game.player.match_stats
        steamid = str(game.player.steamid or "")
        provider_steamid = str(game.provider_steamid or "")
        spectarget = str(game.player.spectarget or "")
        mine = payload_is_mine(steamid, provider_steamid, spectarget)

        if mine:
            self._own_health = int(state.health)
            self._own_burning = int(state.burning)
            self._own_health_known = bool(state.health_known)

        return PlayerSnapshot(
            health=self._own_health,
            burning=self._own_burning,
            round_kills=int(state.round_kills),
            match_kills=int(stats.kills),
            match_kills_known=bool(stats.kills_known),
            health_known=self._own_health_known,
            round_phase=str(game.round_phase or ""),
            steamid=steamid,
            provider_steamid=provider_steamid,
            spectarget=spectarget,
            spectating=not mine,
        )


class GSIRequestHandler(BaseHTTPRequestHandler):
    """處理 CS2 送來的 POST。"""

    protocol_version = "HTTP/1.1"
    server_version = "DGLAB-CS2-GSI"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的命名
        server: GSIServer = self.server  # type: ignore[assignment]

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        if not body:
            self._respond(400, "empty body")
            return

        try:
            payload: Any = json.loads(body)
        except json.JSONDecodeError:
            logger.debug("收到無法解析的 JSON（%d bytes）", len(body))
            self._respond(400, "invalid json")
            return

        if not self._authenticated(server, payload):
            should_warn, count = server.note_auth_failure()
            if should_warn:
                received = extract_token(payload)
                logger.warning(
                    "收到 token 不符的 GSI 資料，已忽略（累計 %d 筆）\n"
                    "    來源        ：%s\n"
                    "    收到的 token：%s\n"
                    "    設定的 token：%s\n"
                    "  最常見的原因：cfg 目錄裡還有「其他」也指向 %s:%d 的 "
                    "gamestate_integration_*.cfg（例如之前試過的其他工具），"
                    "把它們移走或改名即可。\n"
                    "  如果不想管 token，可以把 config.toml 的 gsi.token 設成空字串 \"\"，"
                    "就會接受任何來源。",
                    count,
                    describe_payload_source(payload),
                    "沒有 auth 區塊" if received is None else repr(received),
                    repr(server.auth_token),
                    server.server_address[0],
                    server.server_address[1],
                )
            self._respond(401, "unauthorized")
            return

        try:
            server.payload_parser.parse_payload(payload, server.gamestate_manager)
        except Exception:
            logger.exception("解析 GSI 資料時發生錯誤")
            self._respond(500, "parse error")
            return

        server.record_payload()
        self._respond(200, "ok")
        # 通知控制迴圈「有新資料了」，讓它立刻處理而不是等下一次輪詢。
        # 這可以把「受擊到真的電下去」的延遲從最多一個輪詢間隔降到幾乎沒有。
        server.notify_payload()

    def do_GET(self) -> None:  # noqa: N802
        """簡單的健康檢查，用瀏覽器打開 http://127.0.0.1:3000/ 就能看到狀態。"""
        server: GSIServer = self.server  # type: ignore[assignment]
        elapsed = server.seconds_since_last_payload()
        player = server.snapshot()
        state_text = (
            f"目前血量 / 燃燒 / 本回合擊殺：{player.health} / {player.burning} / "
            f"{player.round_kills}"
            if player.health_known
            else "尚未收到玩家狀態（只有 heartbeat）"
        )
        lines = [
            "DGLAB-CS2 GSI server",
            f"已收到資料筆數：{server.payload_count}",
            f"被拒絕筆數：{server.rejected_count}",
            (
                "距離上次收到資料：從未收到"
                if elapsed is None
                else f"距離上次收到資料：{elapsed:.1f} 秒"
            ),
            state_text,
        ]
        self._respond(
            200, "\n".join(lines) + "\n", content_type="text/plain; charset=utf-8"
        )

    @staticmethod
    def _authenticated(server: GSIServer, payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        # 空字串代表「接受任何來源」，方便排除 token 設定的問題
        if server.auth_token == "":
            return True
        auth = payload.get("auth")
        return isinstance(auth, dict) and auth.get("token") == server.auth_token

    def _respond(
        self,
        code: int,
        message: str,
        *,
        content_type: str = "text/plain; charset=utf-8",
    ) -> None:
        body = message.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """不要讓每個請求都洗到主控台（改成 debug 等級）。"""
        logger.debug("%s - %s", self.address_string(), format % args)


def serve_forever(server: GSIServer) -> threading.Thread:
    """在背景執行緒啟動伺服器，回傳該執行緒。"""

    def _run() -> None:
        logger.info("GSI 伺服器監聽 http://%s:%d", *server.server_address[:2])
        try:
            server.serve_forever()
        except Exception:  # pragma: no cover - 收尾時不該再拋錯
            logger.debug("GSI 伺服器結束", exc_info=True)

    thread = threading.Thread(target=_run, name="gsi-server", daemon=True)
    thread.start()
    return thread
