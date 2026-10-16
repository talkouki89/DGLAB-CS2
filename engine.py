"""與 UI 無關的執行引擎。

把「開 GSI 伺服器 → 連 Socket 服務端 → 顯示 QR code → 等配對 → 控制迴圈」
這一整套包成一個物件，可以在背景執行緒裡跑，而且隨時能被 UI 查狀態、下指令。

設計重點：

- 引擎在自己的執行緒裡跑自己的 asyncio event loop，UI 永遠不會被網路卡住。
- UI 只透過 :meth:`ControlEngine.status` 讀狀態，透過
  :meth:`~ControlEngine.start` / :meth:`~ControlEngine.stop` /
  :meth:`~ControlEngine.pause` / :meth:`~ControlEngine.resume` /
  :meth:`~ControlEngine.apply_config` 下指令，這些方法都是執行緒安全的。
- 決策規則全部放在 :mod:`controller`，和命令列版共用同一份。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from config import AppConfig
from controller import DeviceWriter, PlayerController
from coyote import (
    DglabSocketClient,
    NullTransport,
    SocketTransport,
    WebsocketsTransport,
    resolve_preset,
)
from coyote import __version__ as PROJECT_VERSION
from coyote import qr as qr_module
from dashboard import (
    DashboardOptions,
    DashboardServer,
    build_status_payload,
)
from engine_status import EngineState, EngineStatus
from game_events import PlayerSnapshot
from gsi_server import GSIServer, serve_forever
from presence import DiscordPresence, PresenceView, build_view

logger = logging.getLogger(__name__)

#: dry-run 時假裝的服務端／App ID
DRY_RUN_CLIENT_ID = "00000000-0000-4000-8000-000000000001"
DRY_RUN_TARGET_ID = "00000000-0000-4000-8000-000000000002"

#: 記錄回呼：``(訊息, 等級)``，等級是 ``info`` / ``success`` / ``warning`` / ``error``
LogCallback = Callable[[str, str], None]

#: 預設設定檔名稱
DEFAULT_CONFIG_NAME = "config.toml"


def app_directory() -> Path:
    """程式所在的目錄。

    打包成 exe（PyInstaller）時是 exe 的位置，直接跑原始碼時是專案目錄。
    設定檔與 QR code 圖片都相對於這裡，雙擊 exe 才不會找不到 config.toml。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def default_config_path() -> str:
    """優先使用程式目錄下的 ``config.toml``，找不到才退回目前工作目錄。"""
    candidate = app_directory() / DEFAULT_CONFIG_NAME
    if candidate.exists():
        return str(candidate)
    return DEFAULT_CONFIG_NAME


def resolve_app_path(value: str | Path) -> Path:
    """把相對路徑解析成「相對於程式目錄」。"""
    path = Path(value)
    return path if path.is_absolute() else app_directory() / path


# --------------------------------------------------------------------- 建構


def describe_wave(config: AppConfig) -> str:
    """產生一行人看得懂的波形說明。"""
    frames = config.dglab.wave.build_frames()
    if not frames:
        return "無"
    if len(frames) == 1:
        return f"{config.dglab.wave.preset}（{frames[0].describe()}）"
    preview = "、".join(frame.describe() for frame in frames[:3])
    return (
        f"{config.dglab.wave.preset}"
        f"（{len(frames)} 個取樣點，25ms/點；例如 {preview} …）"
    )


def build_transport(
    config: AppConfig, *, dry_run: bool
) -> tuple[SocketTransport, object | None]:
    """建立傳輸層；回傳 ``(transport, dry_run_task)``。"""
    if dry_run:
        transport = NullTransport(config.socket.uri)
        # 走的是和正式執行完全一樣的註冊／配對流程，只是訊息是假的
        transport.feed(NullTransport.bind_frame(DRY_RUN_CLIENT_ID))
        return transport, fake_pairing(transport)
    return WebsocketsTransport(config.socket.uri), None


async def fake_pairing(transport: NullTransport) -> None:
    """dry-run 用：過一會假裝 App 已經掃碼完成。"""
    await asyncio.sleep(1.5)
    transport.feed(
        NullTransport.bind_frame(DRY_RUN_CLIENT_ID, DRY_RUN_TARGET_ID, "200")
    )
    logger.info("[dry-run] 假裝 App 已完成掃碼配對")


def build_client(transport: SocketTransport, config: AppConfig) -> DglabSocketClient:
    """依設定建立 Socket 客戶端。"""
    dglab = config.dglab
    return DglabSocketClient(
        transport,
        uri=config.socket.uri,
        channels=dglab.channels,
        wave_frames=resolve_preset(dglab.wave.preset, dglab.wave.custom_samples),
    )


# --------------------------------------------------------------------- 引擎


class ControlEngine:
    """在背景執行緒裡跑完整流程的引擎。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        dry_run: bool = False,
        on_log: LogCallback | None = None,
    ) -> None:
        self._config = config
        self._dry_run = dry_run
        self._on_log = on_log
        self._controller = PlayerController(config)

        self._status = EngineStatus(
            state=EngineState.IDLE,
            dry_run=dry_run,
            socket_uri=config.socket.uri,
            gsi_address=f"{config.gsi.host}:{config.gsi.port}",
        )
        self._status_lock = threading.Lock()

        self._stop_requested = threading.Event()
        self._paused = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: DglabSocketClient | None = None
        self._server: GSIServer | None = None
        self._writer: DeviceWriter | None = None
        #: 收到 GSI 資料時會被 set，讓控制迴圈立刻醒來（事件驅動）
        self._payload_event: asyncio.Event | None = None

        #: 唯讀的監看網頁伺服器（直播用）；沒開就是 None
        self._dashboard: DashboardServer | None = None

        # Discord 顯示：自己拉狀態，所以不用在控制迴圈裡塞任何東西
        self._presence = DiscordPresence(
            config.discord,
            view_source=self._presence_view,
            on_log=self.log,
        )
        self._presence.configure(config.discord)
        self.restart_dashboard()

    def _presence_view(self) -> PresenceView:
        return build_view(self.status, self._config)

    @property
    def presence(self) -> DiscordPresence:
        return self._presence

    # ------------------------------------------------------------ 對外狀態

    @property
    def config(self) -> AppConfig:
        return self._config

    @property
    def status(self) -> EngineStatus:
        with self._status_lock:
            return self._status

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def log(self, message: str, level: str = "info") -> None:
        logger.log(
            {
                "success": logging.INFO,
                "warning": logging.WARNING,
                "error": logging.ERROR,
            }.get(level, logging.INFO),
            "%s",
            message,
        )
        if self._on_log is not None:
            try:
                self._on_log(message, level)
            except Exception:  # pragma: no cover - UI 的錯不該弄倒引擎
                logger.debug("記錄回呼發生例外", exc_info=True)

    def _set(self, **changes) -> None:
        with self._status_lock:
            self._status = dataclasses.replace(self._status, **changes)

    # ------------------------------------------------------------ 生命週期

    def start(self) -> None:
        """啟動引擎（立刻返回，實際工作在背景執行緒）。"""
        if self.running:
            return
        self._stop_requested.clear()
        self._paused.clear()
        self._controller.reset_tracking()
        self._set(
            state=EngineState.STARTING,
            message="啟動中…",
            error=None,
            paused=False,
            strength=0,
            output_active=False,
        )
        self._thread = threading.Thread(
            target=self._run, name="dglab-engine", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> bool:
        """要求引擎停止並等待收尾；回傳是否真的停下來。"""
        self._stop_requested.set()
        self._paused.clear()
        self._wake_control_loop()
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    @property
    def dashboard_url(self) -> str:
        """監看頁面的網址（沒開就回傳空字串）。"""
        if self._dashboard is None or not self._dashboard.running:
            return ""
        return self._dashboard.url

    def restart_dashboard(self) -> None:
        """依照目前設定重新啟動監看伺服器（設定改完會呼叫）。"""
        if self._dashboard is not None:
            self._dashboard.stop()
        self._dashboard = None
        self._start_dashboard()

    def _start_dashboard(self) -> None:
        """啟動唯讀的監看網頁伺服器（沒開或失敗都不影響控制）。"""
        config = self._config.dashboard
        if not config.enabled:
            return
        options = DashboardOptions(
            enabled=config.enabled,
            host=config.host,
            port=config.port,
            token=config.token,
            refresh_ms=config.refresh_ms,
            title=config.title,
        )
        self._dashboard = DashboardServer(self._dashboard_payload, options)
        if self._dashboard.start():
            self.log(f"監看頁面：{self._dashboard.url}（唯讀，不會洩漏配對連結）")
        else:
            self.log(
                f"無法啟動監看頁面（{config.host}:{config.port}），"
                "可能是埠被佔用；其餘功能不受影響",
                "warning",
            )

    def _dashboard_payload(self) -> dict:
        """提供給監看頁面的資料（刻意不含配對連結與 token）。"""
        return build_status_payload(
            self._status,
            version=PROJECT_VERSION,
            wave=describe_wave(self._config),
            max_strength=int(self._config.dglab.max_strength),
        )

    def shutdown(self) -> None:
        """停止引擎並釋放所有資源（含 Discord 連線）。

        換掉整個引擎物件（例如切換模擬模式）之前一定要呼叫，
        否則舊引擎的 Discord 執行緒會繼續活著、和新引擎搶著更新狀態。
        """
        self.stop(timeout=5)
        self._presence.close()
        if self._dashboard is not None:
            self._dashboard.stop()
            self._dashboard = None

    # ------------------------------------------------------------ 執行中指令

    def pause(self, reason: str = "暫停") -> None:
        """暫停控制並立刻把裝置歸零（緊急停止用）。

        連線不會斷，按「繼續」就會恢復。
        """
        if self._paused.is_set():
            return
        self._paused.set()
        self.log(f"已暫停，強度歸零（{reason}）", "warning")
        self._set(state=EngineState.PAUSED, message="已暫停，強度歸零", paused=True)
        self._schedule(self._silence_now())
        self._wake_control_loop()

    def resume(self) -> None:
        """恢復控制。"""
        if not self._paused.is_set():
            return
        self._paused.clear()
        if self._writer is not None:
            # 歸零之後值沒變就不會補送，這裡要強迫重送一次
            self._writer.forget()
        self.log("已恢復控制")
        self._set(state=EngineState.RUNNING, message="執行中", paused=False)
        self._wake_control_loop()

    def reset_stack(self) -> None:
        """把死亡／擊殺累積的疊加值與連殺數歸零。"""
        self._controller.reset_stack()
        self._set(stack=0.0, streak=0)
        self.log("強度疊加與連殺數已歸零")
        self._wake_control_loop()

    def reset_stats(self) -> None:
        """把本場統計歸零（不影響正在跑的電擊邏輯）。"""
        self._controller.reset_stats()
        self._set(stats=dataclasses.replace(self._controller.stats))

    def emergency_stop(self) -> None:
        """緊急停止：立刻歸零並暫停輸出。

        給全域熱鍵用。會直接寫裝置（不等控制迴圈），所以人在 CS2 裡按下去
        也能馬上停。
        """
        self.pause()
        self.log("緊急停止：強度已歸零並暫停輸出", "warning")

    def apply_config(self, config: AppConfig) -> None:
        """熱套用設定（連線層級的項目要重新啟動才會生效）。"""
        self._config = config
        self._controller.apply_config(config)
        self._presence.configure(config.discord)
        if self._writer is not None:
            self._writer.apply_config(config)
        frames = config.dglab.wave.build_frames()
        client = self._client
        if client is not None:
            loop = self._loop
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(client.set_wave, frames)
            else:  # pragma: no cover - 只在收尾競態時走到
                client.set_wave(frames)
        self._set(
            socket_uri=config.socket.uri,
            gsi_address=f"{config.gsi.host}:{config.gsi.port}",
        )
        self.log(f"已套用新設定（波形：{describe_wave(config)}）")
        # 改完設定讓控制迴圈立刻重算一次，不用等下一個輪詢
        self._wake_control_loop()

    # ------------------------------------------------------------ 執行緒內部

    def _schedule(self, coro) -> None:
        """把協程排進引擎的 event loop。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            coro.close()
            return
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:  # pragma: no cover - loop 正在關閉
            coro.close()

    async def _silence_now(self) -> None:
        client = self._client
        if client is None or not client.is_paired:
            return
        try:
            await client.silence()
        except Exception:
            logger.debug("暫停時歸零失敗", exc_info=True)
        if self._writer is not None:
            self._writer.forget()
        self._set(strength=0, output_active=False)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except Exception as exc:  # pragma: no cover - 保底
            logger.exception("引擎發生未預期錯誤")
            self.log(f"引擎發生未預期錯誤：{exc}", "error")
            self._set(state=EngineState.ERROR, error=str(exc), message=str(exc))
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:  # pragma: no cover
                pass
            asyncio.set_event_loop(None)
            loop.close()
            self._loop = None
            self._client = None
            self._server = None
            self._writer = None
            if self._dashboard is not None:
                self._dashboard.stop()
            self._payload_event = None
            if self.status.state is not EngineState.ERROR:
                self._set(
                    state=EngineState.STOPPED,
                    message="已停止",
                    strength=0,
                    output_active=False,
                    paired=False,
                )

    async def _main(self) -> None:
        config = self._config

        try:
            server = GSIServer(config.gsi)
        except OSError as exc:
            message = (
                f"無法在 {config.gsi.host}:{config.gsi.port} 啟動 GSI 伺服器（{exc}）"
            )
            self.log(
                f"{message}。常見原因是這個埠被其他程式佔用"
                "（例如已經開了另一份本程式），或權限被阻擋。",
                "error",
            )
            self._set(state=EngineState.ERROR, error=message, message=message)
            return
        self._server = server
        serve_forever(server)
        bound_host, bound_port = server.server_address[:2]
        self._set(gsi_address=f"{bound_host}:{bound_port}")
        self.log(f"GSI 伺服器監聽 http://{bound_host}:{bound_port}")

        # 事件驅動：收到 GSI 資料就立刻叫醒控制迴圈，不要等下一次輪詢。
        # 這一段是在 HTTP 執行緒被呼叫，所以要轉回 asyncio 迴圈上才安全。
        loop = asyncio.get_running_loop()
        payload_event = asyncio.Event()
        self._payload_event = payload_event
        server.on_payload = lambda: loop.call_soon_threadsafe(payload_event.set)

        transport, pairing_task = build_transport(config, dry_run=self._dry_run)
        client = build_client(transport, config)
        self._client = client
        self._writer = DeviceWriter(client, config)
        unpaired = asyncio.Event()
        client.on_unpaired = unpaired.set

        self._set(state=EngineState.STARTING, message=f"連線到 {config.socket.uri} …")
        try:
            await client.start(register_timeout=config.socket.register_timeout)
        except Exception as exc:
            message = f"無法連上 DG-LAB Socket 服務端：{exc}"
            self.log(message, "error")
            self._set(state=EngineState.ERROR, error=message, message=message)
            await client.close()
            server.shutdown()
            server.server_close()
            return

        self.log(f"已向服務端註冊（clientId={client.client_id}）", "success")
        if pairing_task is not None:
            asyncio.create_task(pairing_task)
        self._publish_qr(client)

        try:
            while not self._stop_requested.is_set() and client.running:
                self._set(
                    state=EngineState.WAITING_PAIR,
                    message="等待 DG-LAB App 掃描 QR code…",
                )
                self.log("等待 DG-LAB App 掃描 QR code …")
                if not await self._wait_pairing(client):
                    break

                self.log(f"已與 App 完成配對（targetId={client.target_id}）", "success")
                self._publish_qr(client)
                unpaired.clear()
                self._controller.reset_tracking()
                if self._writer is not None:
                    self._writer.forget()
                self._paused.clear()
                # 先刷新一次狀態，UI 才不會在配對後還看到「未配對」
                self._refresh_status(client)
                self._set(state=EngineState.RUNNING, message="執行中")

                await self._control_loop(server, client, unpaired)

                if unpaired.is_set() and not self._stop_requested.is_set():
                    self.log("App 已離線，重新顯示 QR code 等待重新配對", "warning")
                    self._publish_qr(client)
        finally:
            await client.close()
            server.shutdown()
            server.server_close()

    def _wake_control_loop(self) -> None:
        """叫醒正在等待的控制迴圈，不用等到下一個輪詢間隔。

        停止、暫停、改設定時都會用到；這樣「按了結束／暫停」是馬上生效的。
        這個方法可能從別的執行緒被呼叫，所以要轉回 asyncio 迴圈。
        """
        loop = self._loop
        event = self._payload_event
        if loop is None or event is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:  # pragma: no cover - 迴圈剛好被關掉
            pass

    async def _wait_for_payload(self, interval: float) -> None:
        """等到有新資料、或超過 ``interval`` 秒為止。

        事件驅動比固定輪詢快得多：輪詢的話最差要等一整個間隔（預設 250ms）
        才會發現「有人打我」，現在收到 GSI 資料就會立刻醒來。

        處理資料的期間如果有新資料進來，事件會是 set 的狀態，
        所以下一個迴圈會馬上再跑一次、不會漏掉。
        """
        event = self._payload_event
        if event is None:
            await asyncio.sleep(interval)
            return
        try:
            await asyncio.wait_for(event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        finally:
            event.clear()

    async def _wait_pairing(self, client: DglabSocketClient) -> bool:
        while not self._stop_requested.is_set():
            if client.is_paired:
                return True
            if not client.running:
                self.log("與服務端的連線已中斷", "error")
                self._set(state=EngineState.ERROR, error="連線中斷", message="連線中斷")
                return False
            self._refresh_status(client)
            await asyncio.sleep(0.2)
        return False

    async def _control_loop(
        self,
        server: GSIServer,
        client: DglabSocketClient,
        unpaired: asyncio.Event,
    ) -> None:
        writer = self._writer
        assert writer is not None

        paused_logged = False
        waiting_logged = False
        watchdog_logged = False

        while (
            not self._stop_requested.is_set()
            and not unpaired.is_set()
            and client.running
        ):
            interval = self._config.dglab.poll_interval
            await self._wait_for_payload(interval)
            if self._stop_requested.is_set() or unpaired.is_set() or not client.running:
                break

            self._refresh_status(client)

            if self._paused.is_set():
                if not paused_logged:
                    self.log("暫停中：不會套用任何強度變化", "warning")
                    paused_logged = True
                continue
            paused_logged = False

            watchdog = self._config.dglab.stop_when_no_data_seconds
            elapsed = server.seconds_since_last_payload()
            if elapsed is None:
                if not waiting_logged:
                    self.log("等待 CS2 的 GSI 資料中…（請先進入遊戲）")
                    waiting_logged = True
                continue

            if watchdog and elapsed > watchdog:
                if not watchdog_logged:
                    self.log(
                        f"已經 {elapsed:.0f} 秒沒收到 CS2 資料，判定遊戲已結束，強度歸零",
                        "warning",
                    )
                    watchdog_logged = True
                self._controller.reset_tracking()
                await writer.reset_output()
                self._set(strength=0, output_active=False, last_event="資料中斷 → 歸零")
                continue
            watchdog_logged = False

            snapshot = server.snapshot()
            decision = self._controller.update(snapshot, time.monotonic())
            if decision is None:
                # 只收到 heartbeat（沒有 player_state），血量不可信
                continue

            if decision.first_sample:
                self.log(
                    f"開始接收遊戲狀態（血量 {snapshot.health}、"
                    f"本回合擊殺 {snapshot.round_kills}）",
                    "success",
                )
            elif decision.events.has_event:
                self.log(self._controller.describe(snapshot, decision))

            await writer.apply(decision)
            self._set(
                strength=min(
                    decision.strength, writer.limit_for(writer.channels[0])
                ),
                stack=decision.stack,
                output_active=decision.output_active,
                streak=decision.streak,
                stats=dataclasses.replace(self._controller.stats),
                last_event=(
                    self._controller.describe(snapshot, decision)
                    if decision.events.has_event
                    else self._status.last_event
                ),
            )

    def _refresh_status(self, client: DglabSocketClient) -> None:
        server = self._server
        if server is None:
            return
        snapshot: PlayerSnapshot = server.snapshot()
        report = client.report
        self._set(
            paired=client.is_paired,
            paused=self._paused.is_set(),
            client_id=client.client_id,
            target_id=client.target_id,
            gsi_payloads=server.payload_count,
            gsi_rejected=server.rejected_count,
            gsi_seconds_since=server.seconds_since_last_payload(),
            health=snapshot.health,
            burning=snapshot.burning,
            round_kills=snapshot.round_kills,
            health_known=snapshot.health_known,
            spectating=snapshot.spectating,
            app_strength=(
                (report.strength_a, report.strength_b) if report is not None else None
            ),
            app_limit=(
                (report.limit_a, report.limit_b) if report is not None else None
            ),
        )

    def _publish_qr(self, client: DglabSocketClient) -> None:
        url = client.qr_url
        if url is None:
            return
        if url == self._status.qr_url and self._status.qr_matrix is not None:
            self._set(qr_url=url)
            return
        matrix = qr_module.build_matrix(url)
        self._set(
            qr_url=url,
            qr_matrix=(
                tuple(tuple(bool(module) for module in row) for row in matrix)
                if matrix
                else None
            ),
        )
