"""tkinter 桌面介面。

分成三塊：

- 上半：狀態列 + QR code + 即時數值（血量／燃燒／強度／疊加）
- 中間：開始／暫停歸零／結束，以及可以直接改的參數（分頁）
- 下半：事件紀錄

真正的網路與控制都在 :class:`engine.ControlEngine`（跑在背景執行緒），
這裡只負責畫畫面、把使用者的操作轉成引擎指令。
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

import cs2cfg
import discord_rpc
import tutorial
from config import (
    AppConfig,
    ConfigError,
    DeathStopConfig,
    DglabConfig,
    DiscordConfig,
    GsiConfig,
    SocketConfig,
    TaserConfig,
    WaveConfig,
    load_config,
    with_max_strength,
)
from config_writer import config_values, save_config_values
from coyote import qr as qr_module
from coyote.socket_protocol import Channel
from coyote.waveform import PRESET_NAMES
from engine import (
    ControlEngine,
    EngineState,
    app_directory,
    default_config_path,
    describe_wave,
    resolve_app_path,
)
from strength import MODE_CONTINUOUS, MODE_HIT_ONLY, OutputGate, StrengthPolicy, StrengthStack

logger = logging.getLogger("dglab-cs2.gui")

#: 狀態燈號顏色
COLOR_OK = "#2e7d32"
COLOR_OFF = "#9e9e9e"
COLOR_WARN = "#ef6c00"
COLOR_BAD = "#c62828"

#: 連線層級的欄位：改了要重新啟動才會生效
CONNECTION_FIELDS = (
    "socket_uri",
    "gsi_host",
    "gsi_port",
    "gsi_token",
    "channels",
    "qr_invert",
    "qr_png",
    "qr_open",
)

#: Discord 開發者頁面
DISCORD_DEVELOPERS_URL = "https://discord.com/developers/applications"

#: 本專案在 GitHub 上的位置
PROJECT_URL = "https://github.com/talkouki89/DGLAB-CS2"

#: QR code 圖片的預設檔名（設定裡的 qr_png 是空的時候用）
DEFAULT_QR_IMAGE = "qrcode.png"

#: 參數分頁的可視高度。欄位再多也是這個高度，不夠就自己滾 ——
#: 這樣視窗大小不會被欄位數量撐開。
SETTINGS_HEIGHT = 230

#: 說明文字的初始換行寬度（之後會依實際寬度自動調整）
HINT_WRAPLENGTH = 250

#: 每隔幾個 tick 重新檢查一次 GSI 設定（tick 預設 150ms，所以大約 10 秒）
GSI_CHECK_EVERY_TICKS = 66


# --------------------------------------------------------------- 表單轉換


@dataclass(frozen=True)
class Field:
    """一個設定欄位。"""

    tab: str
    key: str
    label: str
    kind: str  # float / int / bool / choice / text
    hint: str = ""
    choices: tuple[str, ...] = ()


FIELDS: tuple[Field, ...] = (
    # --- 基本 ---
    Field("基本", "base_strength", "基礎強度", "float", "滿血時的強度（0 ~ 200）"),
    Field("基本", "strength_per_hp", "每滴血強度", "float", "每掉一滴血增加多少，建議 0.2 ~ 1"),
    Field("基本", "burning_multiplier", "燃燒加成", "float", "0.2 = 燒到最滿時再多 20%"),
    Field("基本", "keep_strength_while_not_injured", "滿血時仍電擊", "bool", "取消 = 滿血完全不輸出"),
    Field("基本", "max_strength", "安全上限", "float", "強度絕對不會超過這個值（0 ~ 200）"),
    Field("基本", "respect_app_limit", "尊重 App 上限", "bool", "App 的上限更低時以 App 的為準"),
    Field("基本", "poll_interval", "檢查間隔（秒）", "float", "越小越即時，0.05 ~ 5"),
    Field("基本", "stop_when_no_data_seconds", "沒資料就歸零（秒）", "float", "0 = 停用"),
    # --- 疊加 ---
    Field("疊加", "stack_enabled", "啟用強度疊加", "bool", "死亡增加、擊殺降低"),
    Field("疊加", "stack_death_gain", "陣亡增加", "float", "每次陣亡累加多少強度"),
    Field("疊加", "stack_kill_reduction", "擊殺降低", "float", "每次擊殺扣回多少強度"),
    Field("疊加", "stack_min", "疊加下限", "float", "可以設負值"),
    Field("疊加", "stack_max", "疊加上限", "float", ""),
    # --- 輸出 ---
    Field(
        "輸出",
        "output_mode",
        "輸出模式",
        "choice",
        "continuous = 一直輸出；hit_only = 只有受擊時輸出",
        choices=(MODE_CONTINUOUS, MODE_HIT_ONLY),
    ),
    Field("輸出", "hit_duration", "受擊輸出時間（秒）", "float", "受擊後持續出力多久"),
    Field("輸出", "hit_boost", "受擊額外加成", "float", "受擊時間窗內額外增加的強度（0 = 不加成）"),
    Field(
        "輸出",
        "taser_enabled",
        "電擊槍加成",
        "bool",
        "被電擊槍電到時，下面設定的秒數內把強度拉高",
    ),
    Field(
        "輸出",
        "taser_strength",
        "電擊槍強度",
        "float",
        "觸發時至少拉到這個強度（不會超過安全上限）",
    ),
    Field("輸出", "taser_duration", "電擊槍時間（秒）", "float", "持續幾秒，預設 1"),
    Field(
        "輸出",
        "taser_min_damage",
        "電擊槍判定傷害",
        "float",
        "單次掉血達到這個值才算（GSI 不會說傷害來源，預設 100 = 一擊必殺）",
    ),
    Field(
        "輸出",
        "death_stop_enabled",
        "死亡後歸零",
        "bool",
        "陣亡後先維持原強度，超過下面的秒數就歸零到復活為止",
    ),
    Field("輸出", "death_stop_delay", "死亡歸零延遲（秒）", "float", "死亡後幾秒歸零"),
    # --- 波形 ---
    Field("波形", "wave_preset", "波形預設集", "choice", "", choices=PRESET_NAMES),
    Field(
        "波形",
        "wave_custom",
        "自訂取樣點",
        "text",
        "每行一組「波形頻率ms, 波形強度」，頻率 10~1000、強度 0~100（preset 選 custom 時才用）",
    ),
    # --- 連線 ---
    Field("連線", "socket_uri", "Socket 服務端", "text", "官方為 wss://ws.dungeon-lab.cn/"),
    Field("連線", "gsi_host", "GSI 監聽位址", "text", "通常不用改"),
    Field("連線", "gsi_port", "GSI 連接埠", "int", "必須和 cfg 檔的 uri 一致"),
    Field("連線", "gsi_token", "GSI token", "text", "必須和 cfg 檔的 auth token 一致；空字串 = 接受任何來源"),
    Field("連線", "channels", "輸出通道", "choice", "AB = 兩條通道一起輸出", choices=("A", "B", "AB")),
    Field("連線", "qr_invert", "終端機 QR 反相", "bool", "深色背景請保持勾選"),
    Field("連線", "qr_png", "QR 圖片路徑", "text", "留空 = 不輸出圖片"),
    Field("連線", "qr_open", "自動開啟 QR 圖片", "bool", ""),
    # --- Discord ---
    Field(
        "Discord",
        "discord_enabled",
        "啟用 Discord 顯示",
        "bool",
        "在 Discord 個人檔案顯示目前狀態",
    ),
    Field(
        "Discord",
        "discord_client_id",
        "Application ID",
        "text",
        "純數字。取得方式見下方說明",
    ),
    Field(
        "Discord",
        "discord_update_interval",
        "更新間隔（秒）",
        "float",
        "Discord 限制 20 秒 5 次，建議 5",
    ),
    Field("Discord", "discord_show_strength", "顯示強度", "bool", ""),
    Field("Discord", "discord_show_health", "顯示血量", "bool", ""),
    Field("Discord", "discord_show_stack", "顯示疊加", "bool", "疊加為 0 時不會顯示"),
    Field(
        "Discord",
        "discord_large_image",
        "大圖示 Key",
        "combo",
        "按「讀取圖片清單」會自動列出你上傳過的圖片，也可以自己輸入",
    ),
    Field(
        "Discord",
        "discord_large_text",
        "大圖示提示",
        "text",
        "滑鼠移到大圖示時顯示的字（沒有大圖示就沒用）",
    ),
    Field(
        "Discord",
        "discord_small_image",
        "小圖示 Key",
        "combo",
        "疊在大圖示右下角的小圖（同樣是上傳過的 asset key）",
    ),
    Field(
        "Discord",
        "discord_small_text",
        "小圖示提示",
        "text",
        "滑鼠移到小圖示時顯示的字",
    ),
)

FIELD_BY_KEY: dict[str, Field] = {field.key: field for field in FIELDS}

TAB_ORDER: tuple[str, ...] = ("基本", "疊加", "輸出", "波形", "連線", "Discord")


def form_values(config: AppConfig) -> dict[str, Any]:
    """把設定攤平成表單用的 ``{欄位鍵: 值}``。"""
    dglab = config.dglab
    policy = dglab.policy
    stack = dglab.stack
    gate = dglab.output
    return {
        "base_strength": policy.base_strength,
        "strength_per_hp": policy.strength_per_hp,
        "burning_multiplier": policy.burning_multiplier,
        "keep_strength_while_not_injured": policy.keep_strength_while_not_injured,
        "max_strength": policy.max_strength,
        "respect_app_limit": dglab.respect_app_limit,
        "poll_interval": dglab.poll_interval,
        "stop_when_no_data_seconds": dglab.stop_when_no_data_seconds,
        "stack_enabled": stack.enabled,
        "stack_death_gain": stack.death_gain,
        "stack_kill_reduction": stack.kill_reduction,
        "stack_min": stack.minimum,
        "stack_max": stack.maximum,
        "output_mode": gate.mode,
        "hit_duration": gate.hit_duration,
        "hit_boost": gate.hit_boost,
        "taser_enabled": dglab.taser.enabled,
        "taser_strength": dglab.taser.strength,
        "taser_duration": dglab.taser.duration,
        "taser_min_damage": dglab.taser.min_damage,
        "death_stop_enabled": dglab.death_stop.enabled,
        "death_stop_delay": dglab.death_stop.delay,
        "wave_preset": dglab.wave.preset,
        "wave_custom": "\n".join(
            f"{frequency}, {strength}"
            for frequency, strength in dglab.wave.custom_samples
        ),
        "socket_uri": config.socket.uri,
        "gsi_host": config.gsi.host,
        "gsi_port": config.gsi.port,
        "gsi_token": config.gsi.token,
        "channels": "".join(sorted(channel.letter for channel in dglab.channels)),
        "qr_invert": config.socket.qr_invert,
        "qr_png": config.socket.qr_png,
        "qr_open": config.socket.qr_open,
        "discord_enabled": config.discord.enabled,
        "discord_client_id": config.discord.client_id,
        "discord_update_interval": config.discord.update_interval,
        "discord_show_strength": config.discord.show_strength,
        "discord_show_health": config.discord.show_health,
        "discord_show_stack": config.discord.show_stack,
        "discord_large_image": config.discord.large_image,
        "discord_large_text": config.discord.large_text,
        "discord_small_image": config.discord.small_image,
        "discord_small_text": config.discord.small_text,
    }


def parse_custom_samples(text: str) -> tuple[tuple[int, int], ...]:
    """把「每行一組 頻率, 強度」的文字解析成取樣點。

    Raises:
        ValueError: 格式不對、或數值超出範圍。
    """
    samples: list[tuple[int, int]] = []
    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.replace("，", ",").split(",")]
        if len(parts) != 2:
            raise ValueError(f"自訂取樣點第 {line_number} 行必須是「頻率, 強度」")
        try:
            frequency, strength = int(float(parts[0])), int(float(parts[1]))
        except ValueError:
            raise ValueError(f"自訂取樣點第 {line_number} 行不是數字：{line!r}") from None
        if not 10 <= frequency <= 1000:
            raise ValueError(f"自訂取樣點第 {line_number} 行的頻率必須介於 10 ~ 1000")
        if not 0 <= strength <= 100:
            raise ValueError(f"自訂取樣點第 {line_number} 行的強度必須介於 0 ~ 100")
        samples.append((frequency, strength))
    return tuple(samples)


def config_from_form(base: AppConfig, values: dict[str, Any]) -> AppConfig:
    """把表單值套回設定，並做完整驗證。

    Raises:
        ValueError: 任何欄位不合法（訊息可以直接顯示給使用者）。
    """

    def number(key: str, cast=float):
        raw = values.get(key, "")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return cast(raw)
        text = str(raw).strip()
        if not text:
            raise ValueError(f"「{FIELD_BY_KEY[key].label}」不能是空的")
        try:
            return cast(text)
        except ValueError:
            raise ValueError(
                f"「{FIELD_BY_KEY[key].label}」必須是數字（目前是 {text!r}）"
            ) from None

    def text(key: str) -> str:
        return str(values.get(key, "")).strip()

    def boolean(key: str) -> bool:
        return bool(values.get(key, False))

    def choice(key: str) -> str:
        value = text(key)
        allowed = FIELD_BY_KEY[key].choices
        if allowed and value not in allowed:
            raise ValueError(
                f"「{FIELD_BY_KEY[key].label}」只能是 {', '.join(allowed)}"
            )
        return value

    try:
        policy = StrengthPolicy(
            base_strength=number("base_strength"),
            strength_per_hp=number("strength_per_hp"),
            burning_multiplier=number("burning_multiplier"),
            keep_strength_while_not_injured=boolean("keep_strength_while_not_injured"),
            max_strength=number("max_strength"),
        )
        stack = StrengthStack(
            enabled=boolean("stack_enabled"),
            death_gain=number("stack_death_gain"),
            kill_reduction=number("stack_kill_reduction"),
            minimum=number("stack_min"),
            maximum=number("stack_max"),
        )
        output = OutputGate(
            mode=choice("output_mode"),
            hit_duration=number("hit_duration"),
            hit_boost=number("hit_boost"),
        )
    except ValueError as exc:
        raise ValueError(str(exc)) from None

    try:
        taser = TaserConfig(
            enabled=boolean("taser_enabled"),
            strength=int(number("taser_strength")),
            duration=number("taser_duration"),
            min_damage=int(number("taser_min_damage")),
        )
        death_stop = DeathStopConfig(
            enabled=boolean("death_stop_enabled"),
            delay=number("death_stop_delay"),
        )
    except ValueError as exc:
        raise ValueError(f"電擊槍／死亡歸零設定不合法：{exc}") from None

    channels = Channel.parse_all(choice("channels"))
    if not channels:
        raise ValueError("至少要選一個輸出通道")

    preset = choice("wave_preset")
    custom_samples = parse_custom_samples(str(values.get("wave_custom", "")))
    if preset == "custom" and not custom_samples:
        raise ValueError("波形預設集選 custom 時，至少要填一組自訂取樣點")

    gsi_host = text("gsi_host")
    if not gsi_host:
        raise ValueError("「GSI 監聽位址」不能是空的")
    gsi_port = int(number("gsi_port", int))
    if not 0 <= gsi_port <= 65535:
        raise ValueError("「GSI 連接埠」必須介於 0 ~ 65535")

    socket_uri = text("socket_uri")
    if not socket_uri.startswith(("ws://", "wss://")):
        raise ValueError("「Socket 服務端」必須以 ws:// 或 wss:// 開頭")

    discord_client_id = text("discord_client_id")
    if discord_client_id and not discord_client_id.isdigit():
        raise ValueError(
            "「Application ID」必須是純數字"
            "（Discord 開發者頁面 General Information 裡的那串數字）"
        )
    if boolean("discord_enabled") and not discord_client_id:
        raise ValueError(
            "啟用 Discord 顯示就必須填 Application ID。\n\n"
            "取得方式：\n"
            "1. 打開 https://discord.com/developers/applications\n"
            "2. New Application → 隨便取個名字\n"
            "3. General Information → 複製 Application ID"
        )

    try:
        discord = DiscordConfig(
            enabled=boolean("discord_enabled"),
            client_id=discord_client_id,
            update_interval=number("discord_update_interval"),
            show_strength=boolean("discord_show_strength"),
            show_health=boolean("discord_show_health"),
            show_stack=boolean("discord_show_stack"),
            large_image=text("discord_large_image"),
            large_text=str(values.get("discord_large_text", "")).strip(),
            small_image=text("discord_small_image"),
            small_text=str(values.get("discord_small_text", "")).strip(),
        )
    except ValueError as exc:
        raise ValueError(f"Discord 設定不合法：{exc}") from None

    config = dataclasses.replace(
        base,
        gsi=GsiConfig(host=gsi_host, port=gsi_port, token=str(values.get("gsi_token", ""))),
        socket=SocketConfig(
            uri=socket_uri,
            register_timeout=base.socket.register_timeout,
            qr_invert=boolean("qr_invert"),
            qr_png=text("qr_png"),
            qr_open=boolean("qr_open"),
        ),
        discord=discord,
        dglab=DglabConfig(
            channels=channels,
            max_strength=int(number("max_strength")),
            respect_app_limit=boolean("respect_app_limit"),
            poll_interval=number("poll_interval"),
            stop_when_no_data_seconds=number("stop_when_no_data_seconds"),
            policy=policy,
            stack=stack,
            output=output,
            wave=WaveConfig(preset=preset, custom_samples=custom_samples),
            taser=taser,
            death_stop=death_stop,
        ),
    )
    # 讓 policy 與 dglab 的 max_strength 保持一致
    config = with_max_strength(config, number("max_strength"))
    try:
        config.dglab.wave.build_frames()
    except ValueError as exc:
        raise ValueError(f"波形設定不合法：{exc}") from None
    return config


def connection_fields_changed(old: AppConfig, new: AppConfig) -> list[str]:
    """回傳「需要重新啟動才會生效」的欄位標籤。"""
    before = form_values(old)
    after = form_values(new)
    return [
        FIELD_BY_KEY[key].label
        for key in CONNECTION_FIELDS
        if before.get(key) != after.get(key)
    ]


@dataclass
class StatusCell:
    """狀態列上的一格：一個燈號 + 標題 + 內容。"""

    frame: ttk.Frame
    dot: ttk.Label
    value: ttk.Label

    def set(self, color: str, text: str) -> None:
        self.dot.configure(foreground=color)
        self.value.configure(text=text)


class ScrollableFrame(ttk.Frame):
    """可以上下滾動的容器。

    參數分頁最多有 10 個欄位，全部攤開會把整個視窗撐得很高。這裡固定一個
    高度，內容超過就自己滾 —— 視窗大小就不會被欄位數量牽著走。

    滑鼠滾輪要綁在**每一個子元件**上：Tk 的 Enter／Leave 是看指標底下的元件，
    只綁在 canvas 上的話，指標移到輸入框上面時滾輪就會失效。
    """

    def __init__(self, parent: tk.Widget, *, height: int = 280, **kwargs) -> None:
        super().__init__(parent, **kwargs)
        self.canvas = tk.Canvas(self, height=height, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(
            self, orient="vertical", command=self.canvas.yview
        )
        self.body = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")

        self.body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

    # ------------------------------------------------------------ 內部

    def _on_body_configure(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._sync_scrollbar()

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)
        self._sync_scrollbar()

    def _sync_scrollbar(self) -> None:
        """內容比視窗矮時就把捲軸藏起來。"""
        needed = self.body.winfo_reqheight()
        visible = self.canvas.winfo_height()
        if needed > visible:
            self.scrollbar.grid()
        else:
            self.scrollbar.grid_remove()
            self.canvas.yview_moveto(0)

    def _on_wheel(self, event) -> None:
        if self.body.winfo_reqheight() <= self.canvas.winfo_height():
            return
        self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    # ------------------------------------------------------------ 對外

    def bind_wheel_to_children(self) -> None:
        """把所有子元件的滾輪事件接到這個捲動區（建立完內容後呼叫）。"""

        def walk(widget: tk.Widget) -> None:
            widget.bind("<MouseWheel>", self._on_wheel, add="+")
            for child in widget.winfo_children():
                walk(child)

        walk(self.body)

    def scroll_to_top(self) -> None:
        self.canvas.yview_moveto(0)


# ------------------------------------------------------------------- 介面


class DglabApp:
    """主視窗。"""

    POLL_MS = 150
    QR_SIZE = 250
    MAX_LOG_LINES = 500

    def __init__(
        self,
        root: tk.Tk,
        config: AppConfig,
        *,
        config_path: str,
        dry_run: bool = False,
    ) -> None:
        self.root = root
        self.config = config
        self.config_path = config_path
        self.dry_run = dry_run
        self.log_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.engine = ControlEngine(
            config, dry_run=dry_run, on_log=self._enqueue_log
        )
        self.vars: dict[str, Any] = {}
        self._last_qr_url: str | None = None
        self._stopping = False
        self._gsi_check: cs2cfg.GsiCheck | None = None
        self._ticks_since_gsi_check = 0
        #: 查到的 Discord 圖片素材（依 Application ID 快取）
        self._discord_assets: dict[str, tuple] = {}
        self._assets_fetching = False
        self._last_fetched_app_id = ""
        #: 背景執行緒抓回來的圖片清單（tkinter 只能在主執行緒碰，所以走 queue）
        self.asset_results: queue.Queue[tuple[str, str, Any, bool]] = queue.Queue()
        #: 使用者手動填的 Discord 狀態說明（沒有就顯示連線狀態）
        self._discord_note = ""

        self._build_style()
        self._build_layout()
        self._load_form(config)
        self._append_log(
            f"設定檔：{config_path}"
            + ("（模擬模式：不會真的控制裝置）" if dry_run else ""),
            "info",
        )
        self._append_log(f"記錄檔：{log_file_path()}", "info")
        for folder in cs2cfg.find_cs2_cfg_dirs():
            problems = cs2cfg.find_conflicts(
                folder, config.gsi.host, config.gsi.port
            )
            if problems:
                self._append_log(
                    "注意：遊戲 cfg 目錄裡有別的 GSI 設定檔也指向 "
                    f"{config.gsi.host}:{config.gsi.port} → "
                    + "、".join(cfg.filename for cfg in problems),
                    "warning",
                )

        root.title("DGLAB-CS2 — 郊狼 3.0 × CS2")
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(self.POLL_MS, self._tick)
        # 開程式就直接連線並顯示 QR code，使用者少按一個按鈕；
        # 還沒配對之前不會有任何輸出，所以是安全的。
        root.after(400, self._autostart)

    def _autostart(self) -> None:
        if not self.engine.running and not self._stopping:
            self._on_start()

    # ------------------------------------------------------------ 畫面建立

    def _build_style(self) -> None:
        style = ttk.Style()
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        self.font_normal = ("Microsoft JhengHei UI", 10)
        self.font_small = ("Microsoft JhengHei UI", 9)
        self.font_bold = ("Microsoft JhengHei UI", 10, "bold")
        self.font_title = ("Microsoft JhengHei UI", 13, "bold")
        style.configure(".", font=self.font_normal)
        style.configure("Title.TLabel", font=self.font_title)
        style.configure("Hint.TLabel", font=self.font_small, foreground="#666666")
        style.configure("Big.TLabel", font=("Microsoft JhengHei UI", 15, "bold"))
        style.configure("Primary.TButton", font=self.font_bold)

    def _build_layout(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        # 只有中間那一塊（QR + 參數分頁）會跟著視窗長高
        root.rowconfigure(2, weight=1)

        self._build_status_bar()
        self._build_buttons()
        self._build_main_area()
        self._build_log()

    def _build_status_bar(self) -> None:
        frame = ttk.Frame(self.root, padding=(12, 10, 12, 0))
        frame.grid(row=0, column=0, sticky="ew")
        for column in range(4):
            frame.columnconfigure(column, weight=1)
        self.socket_status = self._status_cell(frame, 0, "Socket 服務端")
        self.pair_status = self._status_cell(frame, 1, "DG-LAB App")
        self.cs2_status = self._status_cell(frame, 2, "CS2 資料")
        self.gsi_status = self._status_cell(frame, 3, "GSI 設定")

    def _status_cell(self, parent: ttk.Frame, column: int, caption: str) -> StatusCell:
        cell = ttk.Frame(parent)
        cell.grid(row=0, column=column, sticky="w")
        dot = ttk.Label(cell, text="\u25cf", foreground=COLOR_OFF, font=("Segoe UI", 14))
        dot.grid(row=0, column=0, rowspan=2, padx=(0, 6))
        ttk.Label(cell, text=caption, font=self.font_small, foreground="#666666").grid(
            row=0, column=1, sticky="w"
        )
        value = ttk.Label(cell, text="—", font=self.font_bold)
        value.grid(row=1, column=1, sticky="w")
        return StatusCell(frame=cell, dot=dot, value=value)

    def _build_main_area(self) -> None:
        main = ttk.Frame(self.root, padding=(12, 8, 12, 0))
        main.grid(row=2, column=0, sticky="nsew")
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        self._build_qr_box(main)

        right = ttk.Frame(main)
        right.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        self._build_info(right)
        self._build_settings(right)

    def _build_qr_box(self, parent: ttk.Frame) -> None:
        qr_box = ttk.LabelFrame(parent, text=" 配對 QR code ", padding=8)
        qr_box.grid(row=0, column=0, sticky="nsew")
        self.qr_canvas = tk.Canvas(
            qr_box,
            width=self.QR_SIZE,
            height=self.QR_SIZE,
            background="white",
            highlightthickness=1,
            highlightbackground="#cccccc",
        )
        self.qr_canvas.grid(row=0, column=0, columnspan=2)
        self.qr_hint = ttk.Label(
            qr_box,
            text="啟動後會顯示 QR code",
            font=self.font_small,
            foreground="#666666",
            wraplength=self.QR_SIZE,
            justify="center",
        )
        self.qr_hint.grid(row=1, column=0, columnspan=2, pady=(6, 4))
        ttk.Button(qr_box, text="複製連結", command=self._copy_qr_link, width=12).grid(
            row=2, column=0, padx=(0, 4)
        )
        ttk.Button(qr_box, text="開啟 QR 圖片", command=self._open_qr_image, width=14).grid(
            row=2, column=1
        )
        # 讓 QR 區塊在垂直方向可以跟著視窗長高
        qr_box.rowconfigure(4, weight=1)

    def _build_info(self, parent: ttk.Frame) -> None:
        info = ttk.LabelFrame(parent, text=" 即時狀態 ", padding=10)
        info.grid(row=0, column=0, sticky="ew")
        info.columnconfigure(1, weight=1)

        self.health_bar, self.health_text = self._value_row(info, 0, "血量")
        self.burning_bar, self.burning_text = self._value_row(info, 1, "燃燒")
        self.strength_bar, self.strength_text = self._value_row(info, 2, "強度")

        ttk.Separator(info).grid(row=3, column=0, columnspan=3, sticky="ew", pady=6)

        self.stack_text = self._info_row(info, 4, "強度疊加", "0.0")
        self.output_text = self._info_row(info, 5, "輸出狀態", "未出力")
        self.app_text = self._info_row(info, 6, "App 回報", "—")
        self.wave_text = self._info_row(info, 7, "波形", "—")
        self.event_text = self._info_row(info, 8, "最近事件", "—")

    def _value_row(self, parent: ttk.Frame, row: int, caption: str):
        ttk.Label(parent, text=caption, width=8).grid(row=row, column=0, sticky="w")
        bar = ttk.Progressbar(parent, maximum=100, length=200)
        bar.grid(row=row, column=1, sticky="ew", padx=6, pady=2)
        value = ttk.Label(parent, text="—", width=12, font=self.font_bold)
        value.grid(row=row, column=2, sticky="e")
        return bar, value

    def _info_row(self, parent: ttk.Frame, row: int, caption: str, initial: str):
        ttk.Label(parent, text=caption, width=8, foreground="#666666").grid(
            row=row, column=0, sticky="w"
        )
        label = ttk.Label(parent, text=initial, wraplength=430, justify="left")
        label.grid(row=row, column=1, columnspan=2, sticky="w", padx=6)
        return label

    def _build_buttons(self) -> None:
        bar = ttk.Frame(self.root, padding=(12, 10, 12, 0))
        bar.grid(row=1, column=0, sticky="ew")

        self.start_button = ttk.Button(bar, text="\u25b6 開始", command=self._on_start, width=10)
        self.start_button.grid(row=0, column=0, padx=(0, 6))
        self.pause_button = ttk.Button(
            bar, text="\u23f8 暫停並歸零", command=self._on_pause, width=14, state="disabled"
        )
        self.pause_button.grid(row=0, column=1, padx=6)
        self.stop_button = ttk.Button(
            bar, text="\u23f9 結束", command=self._on_stop, width=9, state="disabled"
        )
        self.stop_button.grid(row=0, column=2, padx=6)
        ttk.Button(bar, text="疊加歸零", command=self._on_reset_stack, width=9).grid(
            row=0, column=3, padx=6
        )
        self.dry_run_var = tk.BooleanVar(value=self.dry_run)
        self.dry_run_check = ttk.Checkbutton(
            bar, text="模擬模式", variable=self.dry_run_var, command=self._on_toggle_dry_run
        )
        self.dry_run_check.grid(row=0, column=4, padx=(14, 6))

        ttk.Separator(bar, orient="vertical").grid(row=0, column=5, sticky="ns", padx=6)
        ttk.Button(
            bar, text="\u2699 安裝 GSI 設定", command=self._on_install_cfg, width=17
        ).grid(row=0, column=6, padx=(0, 6))
        ttk.Button(
            bar, text="? 使用教學", command=self._show_tutorial, width=11
        ).grid(row=0, column=7, padx=(0, 6))
        ttk.Button(
            bar,
            text="GitHub",
            command=lambda: webbrowser.open(PROJECT_URL),
            width=9,
        ).grid(row=0, column=8)

    def _build_settings(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text=" 參數設定 ", padding=8)
        frame.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self.notebook = ttk.Notebook(frame)
        self.notebook.grid(row=0, column=0, sticky="nsew")

        for tab_name in TAB_ORDER:
            tab = ttk.Frame(self.notebook)
            tab.columnconfigure(0, weight=1)
            tab.rowconfigure(0, weight=1)
            self.notebook.add(tab, text=tab_name)
            # 每個分頁都可以上下滾動，欄位多也不會把視窗撐高
            scrollable = ScrollableFrame(tab, height=SETTINGS_HEIGHT)
            scrollable.grid(row=0, column=0, sticky="nsew")
            self._fill_tab(scrollable.body, tab_name, padding=10)
            scrollable.bind_wheel_to_children()

        footer = ttk.Frame(frame)
        footer.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(footer, text="套用設定", command=self._on_apply, width=14).grid(
            row=0, column=0, padx=(0, 6)
        )
        ttk.Button(
            footer, text="存回 config.toml", command=self._on_save, width=18
        ).grid(row=0, column=1, padx=6)
        self.apply_hint = ttk.Label(footer, text="", font=self.font_small, foreground=COLOR_WARN)
        self.apply_hint.grid(row=0, column=2, sticky="w", padx=10)

    def _fill_tab(self, tab: ttk.Frame, tab_name: str, *, padding: int = 0) -> None:
        """一個欄位一列：標籤 | 輸入框 | 說明。

        說明放在同一列右側而不是下面，這樣最長的分頁也只要 8 列就放得完。
        """
        if padding:
            tab.configure(padding=padding)
        tab.columnconfigure(2, weight=1)
        row = 0
        for field in FIELDS:
            if field.tab != tab_name:
                continue
            ttk.Label(tab, text=field.label, width=13, anchor="w").grid(
                row=row, column=0, sticky="w", pady=4
            )
            if field.kind == "bool":
                var: tk.Variable = tk.BooleanVar()
                ttk.Checkbutton(tab, variable=var).grid(row=row, column=1, sticky="w")
            elif field.kind == "choice":
                var = tk.StringVar()
                combo = ttk.Combobox(
                    tab, textvariable=var, values=list(field.choices), state="readonly", width=18
                )
                combo.grid(row=row, column=1, sticky="w")
                if field.key == "wave_preset":
                    combo.bind("<<ComboboxSelected>>", lambda _e: self._sync_wave_hint())
            elif field.kind == "combo":
                var = tk.StringVar()
                # 可編輯：清單是從 Discord 抓來的建議，也可以自己打
                combo = ttk.Combobox(
                    tab, textvariable=var, values=[], width=24
                )
                combo.grid(row=row, column=1, sticky="w")
                self.vars["__widget__" + field.key] = combo
            elif field.kind == "text" and field.key == "wave_custom":
                var = tk.StringVar()
                box = tk.Text(tab, height=4, width=30, font=("Consolas", 10))
                box.grid(row=row, column=1, sticky="w", pady=4)
                box.bind(
                    "<KeyRelease>",
                    lambda _event, widget=box, key=field.key: self._on_text_edit(widget, key),
                )
                self.vars["__widget__" + field.key] = box
            else:
                var = tk.StringVar()
                entry = ttk.Entry(tab, textvariable=var, width=24)
                entry.grid(row=row, column=1, sticky="w", pady=4)
                self.vars["__widget__" + field.key] = entry
            self.vars[field.key] = var
            if field.hint:
                hint = ttk.Label(
                    tab,
                    text=field.hint,
                    style="Hint.TLabel",
                    justify="left",
                    wraplength=HINT_WRAPLENGTH,
                )
                hint.grid(row=row, column=2, sticky="ew", padx=(14, 0))
                hint.bind("<Configure>", self._autowrap_hint)
            row += 1

        if tab_name == "Discord":
            self._fill_discord_tab(tab, row)

    def _autowrap_hint(self, event) -> None:
        """讓說明文字依實際可用寬度換行，免得被裁掉。

        Tk 的 Label 不會自動依容器寬度換行，所以要自己把 wraplength 調成
        目前寬度。加了「變化超過 8px 才改」的判斷，避免 Configure 迴圈。
        """
        label = event.widget
        width = max(120, int(event.width))
        try:
            current = int(float(label.cget("wraplength")))
        except (TypeError, ValueError):  # pragma: no cover
            current = 0
        if abs(current - width) > 8:
            label.configure(wraplength=width)

    def _fill_discord_tab(self, tab: ttk.Frame, row: int) -> None:
        """Discord 分頁多出來的：目前狀態、測試按鈕、取得 ID 的說明。"""
        ttk.Separator(tab).grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=(10, 8)
        )
        row += 1
        ttk.Label(tab, text="目前狀態", width=16, anchor="w").grid(
            row=row, column=0, sticky="w"
        )
        self.discord_status_label = ttk.Label(tab, text="未啟用", font=self.font_bold)
        self.discord_status_label.grid(row=row, column=1, columnspan=2, sticky="w")
        row += 1

        buttons = ttk.Frame(tab)
        buttons.grid(row=row, column=0, columnspan=3, sticky="w", pady=(6, 4))
        ttk.Button(
            buttons, text="測試連線", command=self._on_test_discord, width=12
        ).grid(row=0, column=0, padx=(0, 6))
        self.discord_fetch_button = ttk.Button(
            buttons,
            text="讀取圖片清單",
            command=lambda: self._maybe_fetch_discord_assets(explicit=True),
            width=14,
        )
        self.discord_fetch_button.grid(row=0, column=1, padx=(0, 6))
        ttk.Button(
            buttons,
            text="開啟 Discord 開發者頁面",
            command=lambda: webbrowser.open(DISCORD_DEVELOPERS_URL),
            width=24,
        ).grid(row=0, column=2)
        row += 1

        # 填完 Application ID 就把焦點移開時自動去抓圖片清單
        app_id_widget = self.vars.get("__widget__discord_client_id")
        if isinstance(app_id_widget, tk.Widget):
            app_id_widget.bind(
                "<FocusOut>", lambda _event: self._maybe_fetch_discord_assets()
            )

        ttk.Label(
            tab,
            text=(
                "怎麼拿到 Application ID：\n"
                "  1. 按上面的按鈕打開 Discord 開發者頁面（需要登入）\n"
                "  2. New Application → 隨便取個名字，例如 DGLAB-CS2\n"
                "  3. General Information → 複製 Application ID\n"
                "  4. 貼到上面的欄位、勾選「啟用 Discord 顯示」，再按「套用設定」\n"
                "\n"
                "圖片（大圖示／小圖示）：\n"
                "  Discord 只接受上傳到應用程式的素材，不能給本機檔案路徑。\n"
                "  在同一個應用程式左邊選 Rich Presence → Art Assets →\n"
                "  Add Image(s) 上傳，然後幫圖片取個名字。\n"
                "  填好 Application ID 之後按「讀取圖片清單」，\n"
                "  上面兩個欄位就會變成下拉選單讓你直接挑。\n"
                "  剛上傳的圖片可能要等幾分鐘才會生效。留空就不顯示圖片。\n"
                "\n"
                "顯示的樣子：第一行是遊玩狀態，第二行是強度／血量／疊加。"
                "（Discord 桌面版要開著才有效）"
            ),
            style="Hint.TLabel",
            justify="left",
            wraplength=620,
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(6, 0))

    # ------------------------------------------------ Discord 圖片清單

    def _maybe_fetch_discord_assets(self, *, explicit: bool = False) -> None:
        """去 Discord 查這個應用程式上傳過哪些圖片，填進下拉選單。"""
        if self._assets_fetching:
            return
        try:
            app_id = str(self.vars["discord_client_id"].get()).strip()
        except (KeyError, tk.TclError):  # pragma: no cover
            return

        if not app_id.isdigit():
            if explicit:
                messagebox.showinfo(
                    "還沒填 Application ID",
                    "請先在「Application ID」欄位填入應用程式的 ID"
                    "（純數字，可在 Discord 開發者頁面的 General Information 找到）。",
                    parent=self.root,
                )
            return

        cached = self._discord_assets.get(app_id)
        if cached is not None:
            self._apply_discord_assets(app_id, cached, from_cache=True)
            return

        self._assets_fetching = True
        self._last_fetched_app_id = app_id
        self.discord_fetch_button.configure(state="disabled", text="讀取中…")
        self.discord_status_label.configure(text="正在讀取圖片清單…")
        self._append_log(f"讀取 Discord 應用程式 {app_id} 的圖片清單…")

        def worker() -> None:
            # 注意：背景執行緒不能碰 tkinter（連 root.after 都不可靠），
            # 所以結果一律丟進 queue，由主執行緒的 _tick 取出來處理。
            try:
                assets = discord_rpc.fetch_assets(app_id)
            except discord_rpc.DiscordAssetError as exc:
                self.asset_results.put(("error", app_id, str(exc), explicit))
            except Exception as exc:  # noqa: BLE001 - 絕不讓它弄倒 UI
                logger.debug("讀取 Discord 素材時發生例外", exc_info=True)
                self.asset_results.put(
                    ("error", app_id, f"讀取圖片清單失敗：{exc}", explicit)
                )
            else:
                self.asset_results.put(("ok", app_id, assets, explicit))

        threading.Thread(target=worker, name="discord-assets", daemon=True).start()

    def _drain_asset_results(self) -> None:
        """把背景執行緒抓回來的圖片清單套用到畫面上（只在主執行緒呼叫）。"""
        while True:
            try:
                kind, app_id, payload, explicit = self.asset_results.get_nowait()
            except queue.Empty:
                return
            if kind == "ok":
                self._assets_loaded(app_id, payload)
            else:
                self._assets_failed(app_id, payload, explicit)

    def _assets_loaded(self, app_id: str, assets) -> None:
        self._assets_fetching = False
        self.discord_fetch_button.configure(state="normal", text="讀取圖片清單")
        self._discord_assets[app_id] = assets
        self._apply_discord_assets(app_id, assets)

    def _assets_failed(self, app_id: str, message: str, explicit: bool) -> None:
        self._assets_fetching = False
        self.discord_fetch_button.configure(state="normal", text="讀取圖片清單")
        # 錯誤要留在狀態列上，不要被連線狀態蓋掉
        self._update_discord_status(message)
        self._append_log(f"讀取圖片清單失敗：{message}", "warning")
        if explicit:
            messagebox.showwarning("讀不到圖片清單", message, parent=self.root)

    def _apply_discord_assets(self, app_id: str, assets, *, from_cache: bool = False) -> None:
        """把抓到的素材填進兩個下拉選單。"""
        for key, slot in (
            ("discord_large_image", "large"),
            ("discord_small_image", "small"),
        ):
            widget = self.vars.get("__widget__" + key)
            if isinstance(widget, ttk.Combobox):
                widget.configure(values=discord_rpc.order_assets_for(assets, slot))

        if not assets:
            note = "這個應用程式還沒有上傳任何圖片"
            self._append_log(
                f"{note}（Discord 開發者頁面 → Rich Presence → Art Assets 可以上傳）",
                "warning",
            )
        else:
            names = "、".join(asset.name for asset in assets)
            note = f"共 {len(assets)} 張：{names}"
            self._append_log(f"讀到 {note}" + ("（快取）" if from_cache else ""), "success")
        self._update_discord_status(note)

    def _build_log(self) -> None:
        frame = ttk.LabelFrame(self.root, text=" 事件紀錄 ", padding=8)
        frame.grid(row=3, column=0, sticky="nsew", padx=12, pady=10)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(frame, height=6, wrap="word", state="disabled", font=("Consolas", 9))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.tag_configure("success", foreground=COLOR_OK)
        self.log_text.tag_configure("warning", foreground=COLOR_WARN)
        self.log_text.tag_configure("error", foreground=COLOR_BAD)
        self.log_text.tag_configure("info", foreground="#333333")

        ttk.Button(frame, text="清除紀錄", command=self._clear_log, width=10).grid(
            row=1, column=0, sticky="e", pady=(6, 0)
        )

    # ------------------------------------------------------------ 表單存取

    def _load_form(self, config: AppConfig) -> None:
        values = form_values(config)
        for key, value in values.items():
            var = self.vars.get(key)
            if isinstance(var, tk.BooleanVar):
                var.set(bool(value))
            elif isinstance(var, tk.StringVar):
                var.set(str(value))
        widget = self.vars.get("__widget__wave_custom")
        if isinstance(widget, tk.Text):
            widget.delete("1.0", "end")
            widget.insert("1.0", str(values.get("wave_custom", "")))
        self._sync_wave_hint()

    def _on_text_edit(self, widget: tk.Text, key: str) -> None:
        var = self.vars.get(key)
        if isinstance(var, tk.StringVar):
            var.set(widget.get("1.0", "end-1c"))

    def _read_form(self) -> AppConfig:
        values: dict[str, Any] = {}
        for key, var in self.vars.items():
            if key.startswith("__widget__"):
                continue
            values[key] = var.get()
        return config_from_form(self.config, values)

    # ------------------------------------------------------------ 使用者操作

    def _on_start(self, *, check_gsi: bool = True) -> None:
        if self.engine.running:
            return
        try:
            config = self._read_form()
        except ValueError as exc:
            messagebox.showerror("設定有問題", str(exc), parent=self.root)
            return

        if check_gsi and not self._passes_gsi_check(config):
            return

        self.config = config
        self.engine.apply_config(config)
        self._stopping = False
        self.engine.start()
        # 按鈕狀態交給 _tick 依引擎實際狀態更新
        self._append_log("引擎啟動中…（要停止請按「⏹ 結束」）", "info")

    def _passes_gsi_check(self, config: AppConfig) -> bool:
        """啟動前的把關：GSI 沒裝好就不顯示 QR code。

        回傳 True 才真的啟動引擎。
        """
        check = self._refresh_gsi_check(config)
        self._update_gsi_status(check)
        if check.ok:
            self._append_log(f"GSI 檢查通過：{check.summary}", "success")
            return True

        self._append_log(f"GSI 檢查未通過：{check.summary}", "warning")
        decision = self._ask_about_missing_gsi(check)
        if decision == "cancel":
            self._append_log("已取消啟動（GSI 設定還沒安裝）", "info")
            return False
        if decision == "start":
            self._append_log("使用者選擇略過 GSI 檢查，仍然啟動", "warning")
            return True

        # decision == "install"
        if not self._install_cfg_for(config):
            return False
        check = self._refresh_gsi_check(config)
        self._update_gsi_status(check)
        if check.ok:
            self._append_log(f"安裝後檢查通過：{check.summary}", "success")
            return True
        return messagebox.askokcancel(
            "還是沒通過檢查",
            f"{check.summary}\n\n仍要啟動嗎？",
            parent=self.root,
        )

    def _on_pause(self) -> None:
        if self.engine.paused:
            self.engine.resume()
        else:
            self.engine.pause("使用者按下暫停")

    def _on_stop(self) -> None:
        if not self.engine.running:
            return
        self._stopping = True
        self.stop_button.configure(state="disabled")
        self._append_log("正在停止…", "info")
        # timeout=0 -> 只送出停止要求，不阻塞 UI；真正停好會在 _tick 看到
        self.engine.stop(timeout=0)

    def _on_reset_stack(self) -> None:
        self.engine.reset_stack()

    def _on_toggle_dry_run(self) -> None:
        wanted = self.dry_run_var.get()
        if wanted == self.dry_run:
            return
        if self.engine.running:
            self.dry_run_var.set(self.dry_run)
            messagebox.showinfo(
                "需要重新啟動",
                "模擬模式必須在停止狀態下切換，請先按「結束」。",
                parent=self.root,
            )
            return
        self.dry_run = wanted
        # 舊引擎要先收乾淨，不然它的 Discord 連線會留下來跟新引擎打架
        self.engine.shutdown()
        self.engine = ControlEngine(
            self.config, dry_run=wanted, on_log=self._enqueue_log
        )
        self._append_log(
            "已切換為模擬模式（不會真的控制裝置）" if wanted else "已切換為正式模式",
            "warning" if wanted else "info",
        )

    def _on_apply(self) -> None:
        try:
            config = self._read_form()
        except ValueError as exc:
            messagebox.showerror("設定有問題", str(exc), parent=self.root)
            return
        changed = connection_fields_changed(self.config, config)
        self.config = config
        self.engine.apply_config(config)
        self._refresh_gsi_check(config)
        self._update_gsi_status(self._gsi_check)
        if changed:
            self.apply_hint.configure(
                text="需要重新啟動：" + "、".join(changed)
            )
            self._append_log(
                "以下設定要重新啟動才會生效：" + "、".join(changed), "warning"
            )
        else:
            self.apply_hint.configure(text="")
        self._sync_wave_hint()
        self._append_log("已套用設定", "success")

    def _on_save(self) -> None:
        try:
            config = self._read_form()
        except ValueError as exc:
            messagebox.showerror("設定有問題", str(exc), parent=self.root)
            return
        try:
            save_config_values(self.config_path, config_values(config))
        except (ConfigError, OSError) as exc:
            messagebox.showerror("儲存失敗", str(exc), parent=self.root)
            self._append_log(f"儲存失敗：{exc}", "error")
            return
        self.config = config
        self.engine.apply_config(config)
        self._append_log(
            f"已存回 {self.config_path}（原檔備份為 {self.config_path}.bak）", "success"
        )
        messagebox.showinfo(
            "已儲存", f"設定已寫回：\n{self.config_path}", parent=self.root
        )

    # -------------------------------------------------- GSI 設定與使用教學

    def _check_gsi(self, config: AppConfig | None = None) -> cs2cfg.GsiCheck:
        """檢查遊戲那邊的 GSI 設定裝好了沒。"""
        current = config or self.config
        return cs2cfg.check_gsi_installation(
            host=current.gsi.host,
            port=current.gsi.port,
            token=current.gsi.token,
        )

    def _refresh_gsi_check(self, config: AppConfig | None = None) -> cs2cfg.GsiCheck:
        self._gsi_check = self._check_gsi(config)
        self._ticks_since_gsi_check = 0
        return self._gsi_check

    def _update_gsi_status(self, check: cs2cfg.GsiCheck | None) -> None:
        if check is None:
            self.gsi_status.set(COLOR_OFF, "尚未檢查")
        elif not check.ok:
            self.gsi_status.set(COLOR_BAD, "未安裝（按「安裝 GSI 設定」）")
        elif check.conflicts:
            self.gsi_status.set(COLOR_WARN, f"已安裝，但有 {len(check.conflicts)} 個衝突")
        else:
            self.gsi_status.set(COLOR_OK, "已安裝")

    def _ask_choice(
        self,
        title: str,
        message: str,
        options: list[tuple[str, str, bool]],
        *,
        details: str = "",
        width: int = 760,
        height: int = 14,
    ) -> str:
        """跳出一個有多個選項的對話框，回傳被選到的 key。"""
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.columnconfigure(0, weight=1)

        ttk.Label(
            dialog, text=message, padding=(12, 12, 12, 6), justify="left", wraplength=width
        ).grid(row=0, column=0, sticky="w")
        if details:
            box = tk.Text(
                dialog, width=88, height=height, wrap="none", font=("Consolas", 9)
            )
            box.grid(row=1, column=0, sticky="nsew", padx=12)
            box.insert("1.0", details)
            box.configure(state="disabled")
        dialog.rowconfigure(1, weight=1)

        chosen: dict[str, str] = {"key": options[-1][0] if options else "cancel"}

        def pick(key: str) -> None:
            chosen["key"] = key
            dialog.destroy()

        bar = ttk.Frame(dialog, padding=12)
        bar.grid(row=2, column=0, sticky="ew")
        for key, label, primary in reversed(options):
            button = ttk.Button(
                bar,
                text=label,
                command=lambda k=key: pick(k),
                style="Primary.TButton" if primary else "TButton",
            )
            button.pack(side="right", padx=(6, 0))
            if primary:
                dialog.bind("<Return>", lambda _event, k=key: pick(k))

        dialog.update_idletasks()
        x = self.root.winfo_rootx() + max(
            0, (self.root.winfo_width() - dialog.winfo_width()) // 2
        )
        y = self.root.winfo_rooty() + 80
        dialog.geometry(f"+{x}+{y}")
        dialog.lift()
        self.root.wait_window(dialog)
        return chosen["key"]

    def _ask_about_missing_gsi(self, check: cs2cfg.GsiCheck) -> str:
        """GSI 沒裝好時問使用者要怎麼辦。"""
        return self._ask_choice(
            "還沒安裝 GSI 設定",
            "遊戲的 GSI 設定還沒安裝好，現在啟動的話 CS2 的資料不會送過來，\n"
            "QR code 掃了也只是一直停在「等待 CS2 資料」。\n\n"
            f"狀況：{check.summary}",
            [
                ("install", "立即安裝", True),
                ("start", "仍要啟動", False),
                ("cancel", "取消", False),
            ],
            details="\n".join(check.details),
        )

    def _install_cfg_for(
        self, config: AppConfig, directory: Path | None = None, *, confirm: bool = True
    ) -> bool:
        """把 cfg 裝進指定（或自動找到的）目錄；回傳是否安裝成功。"""
        if directory is None:
            directory = self._resolve_cfg_directory()
        if directory is None:
            self._append_log("已取消安裝 GSI 設定", "warning")
            return False

        token_text = config.gsi.token or "（不驗證，接受任何來源）"
        if confirm and not messagebox.askokcancel(
            "安裝 GSI 設定",
            f"要把 GSI 設定檔寫到這個資料夾嗎？\n\n{directory}\n\n"
            f"檔名：{cs2cfg.CFG_FILENAME}\n"
            f"連線：http://{config.gsi.host}:{config.gsi.port}\n"
            f"token：{token_text}\n\n"
            "裝好之後要「重新啟動 CS2」才會生效。",
            parent=self.root,
        ):
            return False

        try:
            result = cs2cfg.install_cfg(
                directory,
                host=config.gsi.host,
                port=config.gsi.port,
                token=config.gsi.token,
            )
        except OSError as exc:
            messagebox.showerror(
                "安裝失敗",
                f"寫入失敗：\n{exc}\n\n"
                "如果遊戲裝在 Program Files 之類的位置，"
                "可能要改用系統管理員身分執行本程式。",
                parent=self.root,
            )
            self._append_log(f"安裝 GSI 設定失敗：{exc}", "error")
            return False

        action = "已建立" if result.created else "已更新"
        self._append_log(f"{action} GSI 設定：{result.path}", "success")
        if result.backup is not None:
            self._append_log(f"原本的檔案已備份為 {result.backup.name}")

        lines = [f"{action}：", str(result.path), "", "接下來請重新啟動 CS2。"]
        if result.conflicts:
            self._append_log(
                "發現其他也指向同一個埠的 cfg："
                + "、".join(cfg.filename for cfg in result.conflicts),
                "warning",
            )
            lines += [
                "",
                "⚠ 這個目錄裡還有其他也指向同一個埠的 GSI 設定檔，",
                "它們會讓程式一直出現「token 不符」的警告：",
                "",
            ]
            for cfg in result.conflicts:
                lines.append(f"  • {cfg.filename}")
                lines.append(f"      uri={cfg.uri}  token={cfg.token}")
            lines += ["", "建議把它們移走或改名（例如加上 .disabled）。"]
        messagebox.showinfo("安裝完成", "\n".join(lines), parent=self.root)
        self._refresh_gsi_check(config)
        self._update_gsi_status(self._gsi_check)
        return True

    def _on_install_cfg(self) -> None:
        """把 GSI 設定檔寫進 CS2 的 cfg 目錄。"""
        try:
            config = self._read_form()
        except ValueError as exc:
            messagebox.showerror("設定有問題", str(exc), parent=self.root)
            return
        self._install_cfg_for(config)

    def _on_test_discord(self) -> None:
        """送出一次測試用的狀態，確認 Discord 那邊收得到。"""
        try:
            config = self._read_form()
        except ValueError as exc:
            messagebox.showerror("設定有問題", str(exc), parent=self.root)
            return
        if not config.discord.client_id:
            messagebox.showinfo(
                "還沒填 Application ID",
                "請先到 Discord 開發者頁面建立應用程式，並把 Application ID 填進來。\n\n"
                "按「開啟 Discord 開發者頁面」可以直接打開。",
                parent=self.root,
            )
            return

        self.config = config
        self.engine.apply_config(config)
        presence = self.engine.presence
        if not config.discord.enabled:
            self._append_log(
                "Discord 顯示沒有啟用，已直接測試連線（勾選「啟用 Discord 顯示」才會顯示）",
                "warning",
            )
        # 立刻跑一次，不用等背景執行緒
        try:
            presence.tick()
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"Discord 測試失敗：{exc}", "error")
        self._update_discord_status()

        if presence.connected:
            messagebox.showinfo(
                "Discord 連線成功",
                "已經送出一筆狀態到 Discord。\n\n"
                "打開你自己的 Discord 個人檔案就看得到了"
                "（如果剛剛才啟用，可能要等幾秒）。",
                parent=self.root,
            )
        else:
            detail = presence.last_error or "還沒連上"
            messagebox.showwarning(
                "Discord 連線失敗",
                f"{detail}\n\n"
                "常見原因：\n"
                "  • Discord 桌面版沒有開著（網頁版不算）\n"
                "  • Application ID 填錯\n"
                "  • Discord 剛啟動，等幾秒再試一次",
                parent=self.root,
            )

    def _update_discord_status(self, note: str = "") -> None:
        label = getattr(self, "discord_status_label", None)
        if label is None:
            return
        try:
            text = self.engine.presence.status_text()
        except Exception:  # noqa: BLE001
            text = "未知"
        if note:
            self._discord_note = note
        if self._discord_note:
            text = f"{text}　｜　{self._discord_note}"
        label.configure(text=text)

    def _resolve_cfg_directory(self) -> Path | None:
        """找出要安裝 GSI 設定的資料夾（必要時讓使用者挑）。"""
        found = cs2cfg.find_cs2_cfg_dirs()
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            chosen = self._choose_directory(
                "找到多個 CS2 安裝，請選擇要裝到哪一個：", found
            )
            if chosen is not None:
                return chosen
        return self._browse_cfg_directory()

    def _choose_directory(self, title: str, options: list[Path]) -> Path | None:
        """讓使用者從清單挑一個資料夾。"""
        dialog = tk.Toplevel(self.root)
        dialog.title("選擇資料夾")
        dialog.transient(self.root)
        dialog.grab_set()
        ttk.Label(dialog, text=title, padding=10).pack(anchor="w")
        listbox = tk.Listbox(dialog, width=78, height=min(8, len(options)))
        for option in options:
            listbox.insert("end", str(option))
        listbox.selection_set(0)
        listbox.pack(padx=10, fill="both", expand=True)

        selection: dict[str, Path | None] = {"value": None}

        def confirm() -> None:
            chosen = listbox.curselection()
            if chosen:
                selection["value"] = options[chosen[0]]
            dialog.destroy()

        buttons = ttk.Frame(dialog, padding=10)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="安裝到這個資料夾", command=confirm).pack(side="right")
        ttk.Button(buttons, text="改用手動選擇", command=dialog.destroy).pack(
            side="right", padx=6
        )
        listbox.bind("<Double-Button-1>", lambda _event: confirm())
        self.root.wait_window(dialog)
        return selection["value"]

    def _browse_cfg_directory(self) -> Path | None:
        """手動選擇 CS2 的 cfg 資料夾。"""
        messagebox.showinfo(
            "手動選擇 cfg 資料夾",
            "沒有自動找到 CS2 的 cfg 資料夾。\n\n"
            "請在下一步選擇這個位置（各人安裝路徑可能不同）：\n\n"
            "...\\steamapps\\common\\Counter-Strike Global Offensive\\game\\csgo\\cfg",
            parent=self.root,
        )
        chosen = filedialog.askdirectory(
            parent=self.root, title="選擇 CS2 的 cfg 資料夾"
        )
        if not chosen:
            return None
        folder = Path(chosen)
        if not cs2cfg.looks_like_cfg_dir(folder):
            if not messagebox.askyesno(
                "確認資料夾",
                f"這個資料夾看起來不像 CS2 的 cfg 資料夾：\n{folder}\n\n還是要繼續嗎？",
                parent=self.root,
            ):
                return None
        return folder

    def _open_cfg_folder(self) -> None:
        """用檔案總管打開 CS2 的 cfg 資料夾。"""
        found = cs2cfg.find_cs2_cfg_dirs()
        folder = found[0] if found else self._browse_cfg_directory()
        if folder is None:
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(folder))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception:
            self._append_log(f"無法開啟資料夾，位置是：{folder}", "warning")
            return
        self._append_log(f"已開啟資料夾：{folder}")

    def _show_tutorial(self) -> None:
        """顯示使用教學。"""
        window = tk.Toplevel(self.root)
        window.title(f"{tutorial.TUTORIAL_TITLE} — DGLAB-CS2")
        window.transient(self.root)
        frame = ttk.Frame(window, padding=10)
        frame.pack(fill="both", expand=True)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        text = tk.Text(frame, wrap="word", width=82, height=34, font=self.font_normal)
        text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, command=text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=scroll.set)
        text.insert("1.0", tutorial.TUTORIAL_TEXT)
        text.configure(state="disabled")

        bar = ttk.Frame(frame)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(
            bar,
            text="\u2699 安裝 GSI 設定到遊戲",
            command=lambda: (window.destroy(), self._on_install_cfg()),
        ).pack(side="left")
        ttk.Button(bar, text="開啟 cfg 資料夾", command=self._open_cfg_folder).pack(
            side="left", padx=6
        )
        ttk.Button(bar, text="關閉", command=window.destroy).pack(side="right")

        window.update_idletasks()
        width = window.winfo_width()
        height = window.winfo_height()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - width) // 2)
        y = self.root.winfo_rooty() + 40
        window.geometry(f"+{x}+{y}")
        window.lift()

    def _copy_qr_link(self) -> None:
        url = self.engine.status.qr_url
        if not url:
            self._append_log("還沒有配對連結可以複製", "warning")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        self._append_log("已複製配對連結到剪貼簿", "success")

    def _qr_image_path(self) -> Path:
        """QR 圖片要存哪裡。

        用設定裡的 ``qr_png``（相對於程式目錄），留空就存成程式目錄下的
        ``qrcode.png``。這裡刻意不跳「另存新檔」對話框 —— 按鈕的目的只是
        「把圖打開來掃」，多問一個問題反而麻煩。
        """
        configured = self.config.socket.png_path
        if configured:
            return resolve_app_path(configured)
        return app_directory() / DEFAULT_QR_IMAGE

    def _open_qr_image(self) -> None:
        url = self.engine.status.qr_url
        if not url:
            self._append_log("還沒有配對連結，無法產生圖片", "warning")
            return
        if not qr_module.qr_available():
            messagebox.showwarning(
                "缺少套件", "沒有安裝 qrcode，無法產生圖片。", parent=self.root
            )
            return

        path = self._qr_image_path()
        if not qr_module.save_png(url, str(path)):
            self._append_log(f"輸出 QR code 圖片失敗（{path}）", "error")
            messagebox.showerror(
                "無法輸出圖片",
                f"寫入失敗：\n{path}\n\n"
                "如果程式放在需要管理員權限的位置，請改用別的路徑"
                "（設定裡的「QR 圖片路徑」）。",
                parent=self.root,
            )
            return
        self._append_log(f"QR code 圖片已存到 {path}，正在開啟…", "success")
        if not qr_module.open_image(str(path)):
            self._append_log(
                "無法自動開啟圖片，請自己到上面的路徑打開", "warning"
            )

    def _on_close(self) -> None:
        if self.engine.running:
            if not messagebox.askokcancel(
                "結束程式", "引擎還在執行，確定要結束嗎？", parent=self.root
            ):
                return
        self.engine.shutdown()
        self.root.destroy()

    # ------------------------------------------------------------ 週期更新

    def _enqueue_log(self, message: str, level: str) -> None:
        # 引擎自己已經寫過記錄檔了，這裡只負責顯示，避免檔案出現兩份
        self.log_queue.put((level, message, False))

    def _append_log(
        self, message: str, level: str = "info", *, to_file: bool = True
    ) -> None:
        if to_file:
            logger.log(
                {"success": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}
                .get(level, logging.INFO),
                "%s",
                message,
            )
        stamp = time.strftime("%H:%M:%S")
        text = f"[{stamp}] {message}\n"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text, level)
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > self.MAX_LOG_LINES:
            self.log_text.delete("1.0", f"{lines - self.MAX_LOG_LINES}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _tick(self) -> None:
        try:
            while True:
                level, message, to_file = self.log_queue.get_nowait()
                self._append_log(message, level, to_file=to_file)
        except queue.Empty:
            pass
        self._drain_asset_results()

        status = self.engine.status
        self._update_status_bar(status)
        self._update_values(status)
        self._update_buttons(status)
        self._update_qr(status)

        # GSI 設定可能被外部改動（自己動手裝、CS2 更新…），定期重掃一次
        self._ticks_since_gsi_check += 1
        if self._ticks_since_gsi_check >= GSI_CHECK_EVERY_TICKS:
            self._refresh_gsi_check()
        self._update_gsi_status(self._gsi_check)
        self._update_discord_status()

        self.root.after(self.POLL_MS, self._tick)

    def _update_status_bar(self, status) -> None:
        if status.error:
            self.socket_status.set(COLOR_BAD, f"錯誤  {status.socket_uri}")
        elif not self.engine.running:
            self.socket_status.set(COLOR_OFF, f"未啟動  {status.socket_uri}")
        elif status.state is EngineState.STARTING:
            self.socket_status.set(COLOR_WARN, f"啟動中  {status.socket_uri}")
        else:
            self.socket_status.set(COLOR_OK, f"已連線  {status.socket_uri}")

        if status.paired:
            self.pair_status.set(COLOR_OK, "已配對")
        elif self.engine.running:
            self.pair_status.set(COLOR_WARN, "等待掃碼")
        else:
            self.pair_status.set(COLOR_OFF, "—")

        if status.cs2_connected and status.health_known:
            seconds = status.gsi_seconds_since or 0.0
            if status.spectating:
                self.cs2_status.set(COLOR_WARN, f"接收中（觀戰別人，{seconds:.1f}s 前）")
            else:
                self.cs2_status.set(COLOR_OK, f"接收中（{seconds:.1f}s 前）")
        elif status.gsi_payloads:
            self.cs2_status.set(COLOR_WARN, "只有心跳，沒有玩家狀態")
        else:
            self.cs2_status.set(COLOR_OFF, "尚未收到")

    def _update_values(self, status) -> None:
        self.health_bar.configure(value=status.health)
        self.health_text.configure(text=f"{status.health} / 100")
        self.burning_bar.configure(value=min(255, status.burning) / 2.55)
        self.burning_text.configure(text=f"{status.burning} / 255")

        maximum = max(1, int(self.config.dglab.policy.max_strength))
        self.strength_bar.configure(maximum=maximum, value=min(status.strength, maximum))
        self.strength_text.configure(text=f"{status.strength} / {maximum}")

        self.stack_text.configure(text=f"{status.stack:+.1f}")
        if status.output_active:
            self.output_text.configure(text="\u25cf 出力中", foreground=COLOR_OK)
        else:
            self.output_text.configure(text="\u25cb 未出力", foreground="#666666")

        if status.app_strength is not None:
            a, b = status.app_strength
            limit = status.app_limit or ("?", "?")
            self.app_text.configure(text=f"強度 A={a} B={b}　上限 A={limit[0]} B={limit[1]}")
        else:
            self.app_text.configure(text="—")

        self.wave_text.configure(text=describe_wave(self.config))
        self.event_text.configure(text=status.last_event or "—")

    def _update_buttons(self, status) -> None:
        running = self.engine.running
        if not running and self._stopping:
            self._stopping = False
            self._append_log("已停止", "info")

        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        self.dry_run_check.configure(state="disabled" if running else "normal")

        if self.engine.paused:
            self.pause_button.configure(state="normal", text="\u25b6 繼續")
        else:
            self.pause_button.configure(
                state="normal" if status.paired else "disabled",
                text="\u23f8 暫停並歸零",
            )

    def _update_qr(self, status) -> None:
        if status.qr_url == self._last_qr_url:
            return
        self._last_qr_url = status.qr_url
        self.qr_canvas.delete("all")
        if not status.qr_url:
            self.qr_hint.configure(text="啟動後會顯示 QR code")
            return
        if not status.qr_matrix:
            self.qr_hint.configure(text="沒有安裝 qrcode，請用「複製連結」手動貼到手機")
            return
        self._draw_qr(status.qr_matrix)
        self.qr_hint.configure(text="用 DG-LAB App 掃碼連接")

    def _draw_qr(self, matrix) -> None:
        size = self.QR_SIZE
        count = len(matrix)
        if count == 0:
            return
        cell = max(1, size // count)
        offset = (size - cell * count) // 2
        for y, row in enumerate(matrix):
            x = 0
            while x < count:
                if row[x]:
                    run = 1
                    while x + run < count and row[x + run]:
                        run += 1
                    self.qr_canvas.create_rectangle(
                        offset + x * cell,
                        offset + y * cell,
                        offset + (x + run) * cell,
                        offset + (y + 1) * cell,
                        fill="black",
                        outline="",
                    )
                    x += run
                else:
                    x += 1

    def _sync_wave_hint(self) -> None:
        """只有選 custom 時才讓自訂取樣點的輸入框可以編輯。"""
        widget = self.vars.get("__widget__wave_custom")
        if not isinstance(widget, tk.Text):
            return
        var = self.vars.get("wave_preset")
        enabled = str(var.get()) == "custom" if isinstance(var, tk.StringVar) else False
        widget.configure(
            state="normal" if enabled else "disabled",
            background="white" if enabled else "#f0f0f0",
        )


# ------------------------------------------------------------------- 進入點


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="DGLAB-CS2",
        description="郊狼 3.0 × CS2 連動（圖形介面）",
    )
    parser.add_argument(
        "--config",
        default=default_config_path(),
        help="設定檔路徑（預設 config.toml，會先找程式所在目錄）",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="模擬模式：不連服務端，只跑流程"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="開啟 debug 記錄")
    return parser


def log_file_path() -> Path:
    """記錄檔的位置（放在程式旁邊）。"""
    return app_directory() / "dglab-cs2.log"


def setup_logging(debug: bool) -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover
                pass

    # 打包成 --windowed 的 exe 時沒有主控台（sys.stderr 是 None），
    # 所以一律另外寫一份檔案，出事才有東西可以查。
    handlers: list[logging.Handler] = []
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    try:
        handlers.append(
            logging.FileHandler(log_file_path(), mode="w", encoding="utf-8")
        )
    except OSError:  # pragma: no cover - 沒有寫入權限時就只留主控台
        pass

    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )
    if not debug:
        for name in ("websockets", "websockets.client", "websockets.protocol"):
            logging.getLogger(name).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("設定錯誤", str(exc), parent=root)
        root.destroy()
        return 2

    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.25)
    except tk.TclError:  # pragma: no cover - 非 Windows
        pass
    app = DglabApp(root, config, config_path=args.config, dry_run=args.dry_run)
    # 視窗依內容決定大小。參數分頁有各自的固定高度（會自己滾），
    # 所以視窗大小不會被欄位數量撐開；這裡只要確保不超過螢幕。
    root.update_idletasks()
    root.geometry("")
    width = root.winfo_reqwidth()
    height = root.winfo_reqheight()
    limit = max(560, root.winfo_screenheight() - 110)
    if height > limit:
        height = limit
    root.geometry(f"{width}x{height}")
    root.minsize(min(940, width), 560)
    try:
        root.mainloop()
    finally:
        app.engine.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
