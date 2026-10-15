"""DGLAB-CS2 —— 讓 CS2 的血量決定郊狼 3.0 的電擊強度（Socket 連線版）。

用法::

    python main.py                 # 用 config.toml 的設定啟動
    python main.py --dry-run       # 不連服務端，用假的流程跑一遍

流程：

1. 開一個 HTTP 伺服器接收 CS2 的 Game State Integration 資料。
2. 連上 DG-LAB 的 Socket 服務端，取得 ``clientId`` 後顯示 QR code。
3. 用 DG-LAB App 掃描 QR code 完成配對。
4. 每 0.25 秒依血量／燃燒／擊殺疊加算出目標強度，交給
   :class:`coyote.DglabSocketClient` 下指令，並維持 App 的波形佇列。
5. 結束時（Ctrl+C）把強度歸零、清空波形再斷線。
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import signal
import sys
import threading
import time

from config import AppConfig, ConfigError, WaveConfig, load_config
from coyote import Channel, DglabSocketClient
from coyote import qr as qr_display
from controller import DeviceWriter, PlayerController
from engine import (
    build_client,
    build_transport,
    default_config_path,
    describe_wave,
    resolve_app_path,
)
from gsi_server import GSIServer, serve_forever
from strength import MODE_HIT_ONLY, OUTPUT_MODES

logger = logging.getLogger("dglab-cs2")

#: Ctrl+C 的優雅停止旗標（Windows 上 asyncio 沒有 add_signal_handler）
_STOP = threading.Event()

# --------------------------------------------------------------------- CLI


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="把 CS2 的血量連動到郊狼 3.0（DG-LAB Socket 連線）。",
    )
    parser.add_argument(
        "--config",
        default=default_config_path(),
        help="設定檔路徑（預設 config.toml，會先找程式所在目錄）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="不連服務端，用假的 Socket 流程跑一遍並印出所有指令",
    )
    parser.add_argument("--socket-uri", help="覆寫 Socket 服務端位址")
    parser.add_argument(
        "--channels", choices=["A", "B", "AB"], help="要輸出波形的通道（預設 A）"
    )
    parser.add_argument(
        "--output-mode",
        choices=list(OUTPUT_MODES),
        help="continuous = 一直輸出；hit_only = 只有受擊時輸出",
    )
    parser.add_argument("--base-strength", type=float, help="覆寫基礎強度")
    parser.add_argument("--max-strength", type=float, help="覆寫安全上限")
    parser.add_argument("--strength-per-hp", type=float, help="覆寫每滴血的強度")
    parser.add_argument("--wave", help="覆寫波形預設集名稱")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="開啟 debug 等級的記錄"
    )
    return parser


def apply_overrides(config: AppConfig, args: argparse.Namespace) -> AppConfig:
    """把命令列參數套用到設定上。"""
    socket = config.socket
    dglab = config.dglab

    try:
        if args.socket_uri:
            socket = dataclasses.replace(socket, uri=args.socket_uri.strip())
        if socket.uri and not socket.uri.startswith(("ws://", "wss://")):
            raise ValueError(
                f"socket-uri 必須以 ws:// 或 wss:// 開頭，目前是 {socket.uri!r}"
            )

        if args.channels:
            dglab = dataclasses.replace(dglab, channels=Channel.parse_all(args.channels))

        if args.output_mode:
            dglab = dataclasses.replace(
                dglab, output=dataclasses.replace(dglab.output, mode=args.output_mode)
            )

        if (
            args.base_strength is not None
            or args.max_strength is not None
            or args.strength_per_hp is not None
        ):
            policy = dataclasses.replace(
                dglab.policy,
                base_strength=(
                    args.base_strength
                    if args.base_strength is not None
                    else dglab.policy.base_strength
                ),
                max_strength=(
                    args.max_strength
                    if args.max_strength is not None
                    else dglab.policy.max_strength
                ),
                strength_per_hp=(
                    args.strength_per_hp
                    if args.strength_per_hp is not None
                    else dglab.policy.strength_per_hp
                ),
            )
            dglab = dataclasses.replace(dglab, policy=policy)

        if args.wave:
            wave = WaveConfig(
                preset=args.wave.strip().lower(),
                custom_samples=dglab.wave.custom_samples,
            )
            wave.build_frames()
            dglab = dataclasses.replace(dglab, wave=wave)
    except ValueError as exc:
        raise ConfigError(f"命令列參數不合法：{exc}") from None

    return dataclasses.replace(config, socket=socket, dglab=dglab)


# ------------------------------------------------------------------ 工具


def configure_stdio() -> None:
    """讓中文輸出在 cp950 主控台或被重導向到檔案時都不會炸掉。

    必須在 argparse 印出 ``--help`` 之前就呼叫。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover
            pass


def setup_logging(debug: bool) -> None:
    configure_stdio()
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    if not debug:
        # websockets 的 INFO 訊息沒什麼用
        for name in ("websockets", "websockets.client", "websockets.protocol"):
            logging.getLogger(name).setLevel(logging.WARNING)


def install_signal_handlers() -> None:
    def _handler(signum, _frame):  # pragma: no cover - 只在真實執行時觸發
        if not _STOP.is_set():
            logger.info("收到中斷訊號，正在把強度歸零並斷線…")
        _STOP.set()

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):  # pragma: no cover - 非主執行緒
            pass


def _websockets_available() -> bool:
    """確認 Socket 套件裝好了，並且給出可以照著做的錯誤訊息。"""
    try:
        import websockets  # noqa: F401
    except ImportError as exc:
        logger.error("找不到 WebSocket 套件 websockets（%s）", exc)
        logger.error("請先執行：python -m pip install -r requirements.txt")
        return False
    return True


# ------------------------------------------------------------------ 工具


def describe_setup(config: AppConfig) -> None:
    dglab = config.dglab
    if config.gsi.token:
        logger.info(
            "GSI 監聽：http://%s:%d（token %r，必須和 cfg 檔的 auth token 相同）",
            config.gsi.host,
            config.gsi.port,
            config.gsi.token,
        )
    else:
        logger.warning(
            "GSI 監聽：http://%s:%d，gsi.token 是空字串 → 會接受任何來源的 GSI 資料"
            "（建議只在本機單獨使用時這樣設）",
            config.gsi.host,
            config.gsi.port,
        )
    logger.info("Socket 服務端：%s", config.socket.uri)
    logger.info("波形：%s", describe_wave(config))
    logger.info(
        "強度規則：基礎 %g、每滴血 %g、燃燒加成 %g、上限 %d、滿血%s電擊",
        dglab.policy.base_strength,
        dglab.policy.strength_per_hp,
        dglab.policy.burning_multiplier,
        dglab.max_strength,
        "" if dglab.policy.keep_strength_while_not_injured else "不",
    )
    stack = dglab.stack
    logger.info(
        "強度疊加：%s（死亡 %+g、擊殺 %+g、範圍 %g ~ %g）",
        "啟用" if stack.enabled else "停用",
        stack.death_gain,
        -stack.kill_reduction,
        stack.minimum,
        stack.maximum,
    )
    gate = dglab.output
    logger.info(
        "輸出模式：%s%s",
        gate.mode,
        (
            f"（受擊後輸出 {gate.hit_duration:g} 秒）"
            if gate.mode == MODE_HIT_ONLY
            else ""
        )
        + (f"，受擊額外 +{gate.hit_boost:g}" if gate.hit_boost else ""),
    )


def show_qrcode(client: DglabSocketClient, config: AppConfig) -> None:
    url = client.qr_url
    if url is None:
        logger.error("還沒取得 clientId，無法產生 QR code")
        return
    png_path = config.socket.png_path
    print(
        qr_display.present(
            url,
            invert=config.socket.qr_invert,
            png_path=resolve_app_path(png_path) if png_path else None,
            open_png=config.socket.qr_open,
        ),
        flush=True,
    )


async def wait_for_pairing(client: DglabSocketClient) -> bool:
    """等待使用者掃碼完成配對。"""
    logged = False
    while not _STOP.is_set():
        if client.is_paired:
            return True
        if not client.running:
            logger.error("與服務端的連線已中斷")
            return False
        if not logged:
            logger.info("等待 DG-LAB App 掃描 QR code …")
            logged = True
        await asyncio.sleep(0.2)
    return False


# -------------------------------------------------------------- 主控制迴圈


async def control_loop(
    server: GSIServer,
    client: DglabSocketClient,
    config: AppConfig,
    unpaired: asyncio.Event,
) -> None:
    """依遊戲狀態持續更新強度與輸出，直到中斷或 App 離線。

    規則本身都在 :class:`controller.PlayerController` 裡（GUI 版也共用同一份）。
    """
    dglab = config.dglab
    controller = PlayerController(config)
    writer = DeviceWriter(client, config)

    interval = dglab.poll_interval
    watchdog = dglab.stop_when_no_data_seconds

    waiting_logged = False
    watchdog_logged = False

    while not _STOP.is_set() and not unpaired.is_set() and client.running:
        await asyncio.sleep(interval)
        if _STOP.is_set() or unpaired.is_set() or not client.running:
            break

        elapsed = server.seconds_since_last_payload()
        if elapsed is None:
            if not waiting_logged:
                logger.info("等待 CS2 的 GSI 資料中…（請先進入遊戲）")
                waiting_logged = True
            continue

        if watchdog and elapsed > watchdog:
            # 遊戲關掉之後不能一直維持上一個強度
            if not watchdog_logged:
                logger.warning(
                    "已經 %.0f 秒沒收到 CS2 資料，判定遊戲已結束，強度歸零",
                    elapsed,
                )
                watchdog_logged = True
            controller.reset_tracking()
            await writer.reset_output()
            continue
        watchdog_logged = False

        snapshot = server.snapshot()
        decision = controller.update(snapshot, time.monotonic())
        if decision is None:
            # 只收到 heartbeat（沒有 player_state），血量不可信
            continue

        if decision.first_sample:
            logger.info(
                "開始接收遊戲狀態（血量 %d、本回合擊殺 %d）",
                snapshot.health,
                snapshot.round_kills,
            )
        elif decision.events.has_event:
            logger.info(controller.describe(snapshot, decision))

        changes = await writer.apply(decision)
        if changes and not decision.events.has_event:
            logger.debug(
                "強度更新：%s（血量 %d 燃燒 %d 疊加 %+.1f）",
                "、".join(f"{channel.letter}={value}" for channel, value in changes),
                snapshot.health,
                snapshot.burning,
                decision.stack,
            )


# ------------------------------------------------------------------ 執行
# 傳輸層與客戶端的建構、波形說明都放在 engine（GUI 版也共用同一份）。


async def run(config: AppConfig, *, dry_run: bool) -> int:
    if not dry_run and not _websockets_available():
        return 1

    try:
        server = GSIServer(config.gsi)
    except OSError as exc:
        logger.error(
            "無法在 %s:%d 啟動 GSI 伺服器（%s）",
            config.gsi.host,
            config.gsi.port,
            exc,
        )
        logger.error(
            "常見原因：這個埠被其他程式佔用（例如已經開了另一份本程式），"
            "或防火牆／權限阻擋。請確認 config.toml 的 gsi.port 與 "
            "gamestate_integration_dglab.cfg 的 uri 一致且沒有衝突。"
        )
        return 1

    serve_forever(server)
    describe_setup(config)

    transport, pairing_task = build_transport(config, dry_run=dry_run)
    client = build_client(transport, config)
    unpaired = asyncio.Event()
    client.on_unpaired = unpaired.set

    try:
        try:
            await client.start(register_timeout=config.socket.register_timeout)
        except Exception as exc:
            logger.error("無法連上 DG-LAB Socket 服務端：%s", exc)
            logger.error(
                "請確認網路可以連到 %s；若在公司或校園網路，"
                "wss 連線可能被防火牆阻擋。",
                config.socket.uri,
            )
            return 1

        if pairing_task is not None:
            asyncio.create_task(pairing_task)
        else:
            # 正式模式下若 QR code 一直沒被掃描，提示一下可能的原因
            asyncio.create_task(_pairing_hint(client))

        while not _STOP.is_set() and client.running:
            show_qrcode(client, config)
            if not await wait_for_pairing(client):
                break

            unpaired.clear()
            logger.info("配對完成，開始連動 CS2 狀態")
            await control_loop(server, client, config, unpaired)

            if unpaired.is_set():
                logger.warning("App 已離線，重新顯示 QR code 等待重新配對")
        return 0
    finally:
        await client.close()
        server.shutdown()
        server.server_close()
        logger.info("已停止")


async def _pairing_hint(client: DglabSocketClient) -> None:
    """太久沒配對時給一段提示。"""
    for _ in range(int(60 / 5)):
        await asyncio.sleep(5)
        if client.is_paired or _STOP.is_set():
            return
    logger.warning(
        "已經 60 秒沒有配對成功。請確認 DG-LAB App 有掃到 QR code，"
        "且手機與服務端（%s）連線正常。",
        client.uri,
    )


# ------------------------------------------------------------------- 進入點


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    args = build_argument_parser().parse_args(argv)
    _STOP.clear()

    setup_logging(args.verbose)
    try:
        config = load_config(args.config)
        config = apply_overrides(config, args)
    except ConfigError as exc:
        logger.error("設定錯誤：%s", exc)
        return 2

    if config.debug and not args.verbose:
        setup_logging(True)
    install_signal_handlers()

    logger.info("DGLAB-CS2（郊狼 3.0 / Socket）啟動中…")
    if not args.dry_run:
        logger.warning(
            "本程式會控制電擊強度，請先確認 config.toml 的 base_strength / "
            "max_strength 是你可承受的範圍"
        )

    try:
        return asyncio.run(run(config, dry_run=args.dry_run))
    except KeyboardInterrupt:  # pragma: no cover - 訊號處理已涵蓋
        logger.info("已中斷")
        return 0
    except ConfigError as exc:
        logger.error("設定錯誤：%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
