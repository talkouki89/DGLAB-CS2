"""把引擎狀態送到 Discord Rich Presence。

分成兩塊：

- :func:`build_view`：純函式，把 :class:`engine.EngineStatus` 翻成要顯示的文字
  （所以「顯示什麼」是可以完整測試的，不需要 Discord）。
- :class:`DiscordPresence`：負責連線、節流、斷線重連；跑在自己的執行緒，
  而且**任何失敗都不會影響主程式**。

狀態是用「拉」的：:class:`DiscordPresence` 拿著一個 ``view_source`` 回呼，
每次 tick 自己去問目前狀態，所以不需要在控制迴圈裡塞任何東西。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from config import AppConfig, DiscordConfig
from discord_rpc import DiscordIPC, DiscordIPCError, build_activity, connect
from engine_status import EngineState, EngineStatus

logger = logging.getLogger("dglab-cs2.presence")

#: 兩次連線嘗試之間的最短間隔（秒）
RECONNECT_DELAY = 15.0

#: 執行緒的巡邏間隔（秒）
POLL_SECONDS = 1.0

ViewSource = Callable[[], "PresenceView"]
LogCallback = Callable[[str, str], None]


@dataclass(frozen=True)
class PresenceView:
    """要顯示在 Discord 上的內容。"""

    #: 第一行：目前在做什麼
    details: str
    #: 第二行：數值
    state: str
    #: 是否正在對戰（決定要不要顯示遊玩計時）
    active: bool = False


def describe_phase(status: EngineStatus) -> tuple[str, bool]:
    """把引擎狀態翻成「遊玩狀態」文字，並回報是否正在對戰。"""
    if status.error:
        return "發生錯誤", False
    if status.state is EngineState.IDLE:
        return "待機中", False
    if status.state is EngineState.STOPPED:
        return "已停止", False
    if status.state is EngineState.ERROR:
        return "發生錯誤", False
    if status.state is EngineState.STARTING:
        return "連線中…", False
    if status.state is EngineState.PAUSED or status.paused:
        return "已暫停", False
    if status.state is EngineState.WAITING_PAIR:
        return "等待 App 配對", False
    if not status.paired:
        return "等待配對", False
    if status.cs2_connected and status.health_known:
        return "CS2 對戰中", True
    return "等待 CS2 資料", False


def build_view(status: EngineStatus, config: AppConfig) -> PresenceView:
    """組出要顯示的內容。

    顯示哪些欄位由 ``config.discord`` 的三個開關決定。
    """
    discord = config.discord
    details, active = describe_phase(status)

    maximum = int(config.dglab.policy.max_strength)
    parts: list[str] = []
    if discord.show_strength:
        parts.append(f"強度 {status.strength}／{maximum}")
    if discord.show_health:
        parts.append(f"血量 {status.health}" if status.health_known else "血量 ？")
    if discord.show_stack and status.stack:
        parts.append(f"疊加 {status.stack:+.0f}")

    return PresenceView(
        details=details,
        state="　".join(parts) if parts else details,
        active=active,
    )


class DiscordPresence:
    """管理與 Discord 的連線，並在狀態改變時更新 Rich Presence。"""

    def __init__(
        self,
        config: DiscordConfig,
        *,
        view_source: ViewSource,
        connector: Callable[..., tuple[Any, str]] = connect,
        ipc_factory: Callable[..., DiscordIPC] = DiscordIPC,
        on_log: LogCallback | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        reconnect_delay: float = RECONNECT_DELAY,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self._config = config
        self._view_source = view_source
        self._connector = connector
        self._ipc_factory = ipc_factory
        self._on_log = on_log
        self._clock = clock
        self._wall_clock = wall_clock
        self._reconnect_delay = reconnect_delay
        self._poll_seconds = poll_seconds

        self._ipc: DiscordIPC | None = None
        self._endpoint = ""
        self._last_payload: dict[str, Any] | None = None
        self._last_sent_at: float | None = None
        self._next_connect_at = 0.0
        self._started_at: float | None = None
        self._last_error: str | None = None
        self._warned_missing_id = False
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------ 對外介面

    @property
    def connected(self) -> bool:
        ipc = self._ipc
        return ipc is not None and ipc.ready

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def endpoint(self) -> str:
        return self._endpoint

    def status_text(self) -> str:
        """一行給 UI 看的說明。"""
        config = self._config
        if not config.enabled:
            return "未啟用"
        if not config.client_id:
            return "未啟用（還沒填 Application ID）"
        if self.connected:
            return "已連線"
        if self._last_error:
            return f"未連線（{self._last_error}）"
        return "連線中…"

    def configure(self, config: DiscordConfig) -> None:
        """套用新的設定（可在執行中熱套用）。"""
        with self._lock:
            changed = config != self._config
            identity_changed = config.client_id != self._config.client_id
            self._config = config
            if changed:
                # 換了 Application ID 一定要重連，否則送出的還是舊的身分
                self._last_payload = None
                self._last_sent_at = None
        if identity_changed and self._ipc is not None:
            self._disconnect("換了 Application ID")
            self._next_connect_at = 0.0
        if not config.usable:
            self._warned_missing_id = False
            self._disconnect("已停用")
        if config.enabled and not config.client_id and not self._warned_missing_id:
            self._warned_missing_id = True
            self._log(
                "Discord 顯示已啟用，但還沒填 Application ID，"
                "請到 discord.com/developers/applications 建一個應用程式後填入。",
                "warning",
            )
        if config.usable:
            self.start()

    def tick(self) -> None:
        """跑一輪：必要時連線、必要時更新狀態。

        測試直接呼叫這個方法（搭配假時鐘與假連線），不需要真的開執行緒。
        """
        config = self._config
        if not config.usable:
            if self._ipc is not None:
                self._disconnect("已停用")
            return

        now = self._clock()
        view = self._view_source()
        self._track_play_time(view)

        if self._ipc is None:
            if now < self._next_connect_at:
                return
            if not self._connect():
                return

        payload = self._activity(view)
        if payload == self._last_payload:
            return
        if (
            self._last_sent_at is not None
            and now - self._last_sent_at < config.update_interval
        ):
            return

        ipc = self._ipc
        if ipc is None:  # pragma: no cover - 上面才剛確認過
            return
        try:
            ipc.set_activity(payload)
        except DiscordIPCError as exc:
            self._last_error = str(exc)
            self._log(f"更新 Discord 狀態失敗：{exc}", "warning")
            self._disconnect("更新失敗")
            return
        self._last_payload = payload
        self._last_sent_at = now

    def start(self) -> None:
        """啟動背景執行緒（重複呼叫不會有事）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="discord-presence", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """停止背景執行緒並清掉 Discord 上的狀態。"""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3)
        self._thread = None

    def close(self) -> None:
        self.stop()
        self._disconnect("關閉")

    # ------------------------------------------------------------ 內部

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - 絕不讓它弄倒主程式
                logger.debug("Discord 狀態更新發生例外", exc_info=True)
            self._stop.wait(self._poll_seconds)

    def _log(self, message: str, level: str = "info") -> None:
        logger.log(
            {"warning": logging.WARNING, "error": logging.ERROR}.get(level, logging.INFO),
            "%s",
            message,
        )
        if self._on_log is not None:
            try:
                self._on_log(message, level)
            except Exception:  # pragma: no cover
                logger.debug("記錄回呼發生例外", exc_info=True)

    def _connect(self) -> bool:
        now = self._clock()
        try:
            connection, path = self._connector()
            ipc = self._ipc_factory(
                connection, application_id=self._config.client_id
            )
            ipc.handshake()
        except (DiscordIPCError, OSError) as exc:
            self._last_error = str(exc).splitlines()[0]
            # 只記一次同樣的錯誤，不要每 15 秒洗一次版
            if self._last_error != getattr(self, "_logged_error", None):
                self._logged_error = self._last_error
                self._log(f"連不上 Discord：{self._last_error}", "warning")
            self._next_connect_at = now + self._reconnect_delay
            return False

        self._ipc = ipc
        self._endpoint = path
        self._last_error = None
        self._logged_error = None
        self._last_payload = None
        self._next_connect_at = 0.0
        self._log(f"已連上 Discord（{path}）", "success")
        return True

    def _disconnect(self, reason: str) -> None:
        ipc = self._ipc
        self._ipc = None
        self._endpoint = ""
        self._last_payload = None
        if ipc is None:
            return
        try:
            ipc.close()
        except Exception:  # noqa: BLE001 - 關閉失敗不需要處理
            logger.debug("關閉 Discord 連線時發生例外", exc_info=True)

    def _track_play_time(self, view: PresenceView) -> None:
        if not view.active:
            self._started_at = None
            return
        if self._started_at is None:
            self._started_at = self._wall_clock()

    def _activity(self, view: PresenceView) -> dict[str, Any]:
        discord = self._config
        return build_activity(
            details=view.details,
            state=view.state,
            start_timestamp=self._started_at if view.active else None,
            large_image=discord.large_image,
            large_text=discord.large_text,
            small_image=discord.small_image,
            small_text=discord.small_text,
        )
