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
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Sequence

import cs2cfg
import discord_rpc
import hotkeys as hotkeys_module
import tutorial
import update_check
from config import (
    AppConfig,
    ConfigError,
    DeathStopConfig,
    DglabConfig,
    DiscordConfig,
    GsiConfig,
    HOTKEY_ACTIONS,
    HotkeyConfig,
    SocketConfig,
    StreakConfig,
    TaserConfig,
    UpdateConfig,
    WaveConfig,
    load_config,
    with_max_strength,
)
from config_writer import config_values, save_config_values
from coyote import __version__ as PROJECT_VERSION
from coyote import qr as qr_module
from coyote.socket_protocol import Channel
from coyote.waveform import (
    FRAME_SECONDS,
    PRESET_NAMES,
    WAVE_FREQ_MAX,
    WAVE_FREQ_MIN,
    WAVE_STRENGTH_MAX,
    decode_wave_freq,
    encode_wave_freq,
    pulse_frequency_hz,
    resolve_preset,
)
from engine import (
    ControlEngine,
    EngineState,
    app_directory,
    default_config_path,
    describe_wave,
    resolve_app_path,
)
from strength import MODE_CONTINUOUS, MODE_HIT_ONLY, OutputGate, StrengthPolicy, StrengthStack
from hotkeys import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    MOD_WIN,
    format_hotkey,
    parse_hotkey,
    vk_for_name,
)

#: tkinter 的 keysym 名稱和 Win32 的按鍵名稱不完全一樣，這裡補上差異。
#: （Tk 用 Prior／Next 代表 PageUp／PageDown）
TKSYM_ALIASES: dict[str, str] = {
    "prior": "pageup",
    "next": "pagedown",
    "return": "enter",
    "backspace": "backspace",
    "kp_0": "numpad0",
    "kp_1": "numpad1",
    "kp_2": "numpad2",
    "kp_3": "numpad3",
    "kp_4": "numpad4",
    "kp_5": "numpad5",
    "kp_6": "numpad6",
    "kp_7": "numpad7",
    "kp_8": "numpad8",
    "kp_9": "numpad9",
    "kp_add": "add",
    "kp_subtract": "subtract",
    "kp_multiply": "multiply",
    "kp_divide": "divide",
    "kp_decimal": "decimal",
}

logger = logging.getLogger("dglab-cs2.gui")

# ------------------------------------------------------------------ 配色
#
# 深色主題。重點是「一眼看得出狀態」：連線是青色、警告是琥珀、錯誤是紅。
# 輸入框比背景亮一階，這樣不用畫框線也看得出可以打字。

BG = "#0f0f17"          # 視窗底色
SURFACE = "#1a1a28"     # 分頁／卡片
SURFACE2 = "#232336"    # 輸入框
SURFACE3 = "#2e2e46"    # 滑鼠移過去
BORDER = "#33334d"
TEXT = "#e8e8f4"
TEXT_DIM = "#8a8aa8"
ACCENT = "#22d3ee"      # 青：電
ACCENT_DARK = "#0b3b47"
PINK = "#f472b6"        # 血量
ORANGE = "#fb923c"      # 燃燒

#: 狀態燈號顏色
COLOR_OK = "#34d399"
COLOR_OFF = "#4b4b66"
COLOR_WARN = "#fbbf24"
COLOR_BAD = "#f87171"

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

#: 本場統計面板的欄位順序（要跟 :meth:`stats.SessionStats.summary` 的標籤一致）。
#: 每 5 個換一欄。
DEFAULT_STATS_SUMMARY: tuple[tuple[str, str], ...] = (
    ("時間", "0:00"),
    ("擊殺 / 死亡", "0 / 0"),
    ("K/D", "0.00"),
    ("最佳連殺", "0"),
    ("受擊次數", "0"),
    ("承受傷害", "0"),
    ("電擊槍觸發", "0"),
    ("最高強度", "0"),
    ("平均強度", "0.0"),
    ("通電時間", "0:00"),
)


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
    Field(
        "疊加",
        "streak_enabled",
        "連殺獎勵",
        "bool",
        "連續擊殺（中間沒死）時，每一殺扣掉的疊加值越來越多",
    ),
    Field("疊加", "streak_starts_at", "連殺從第幾殺起算", "int", "2 = 第二殺開始有獎勵"),
    Field(
        "疊加",
        "streak_bonus_per_kill",
        "每多一殺多扣",
        "float",
        "第二殺多扣 1 倍、第三殺多扣 2 倍…（單位是強度）",
    ),
    Field("疊加", "streak_max_bonus", "連殺獎勵上限", "float", "單次最多額外扣多少"),
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
    # --- 熱鍵 ---
    Field(
        "熱鍵",
        "hotkeys_enabled",
        "啟用全域熱鍵",
        "bool",
        "即使 CS2 是前景視窗也有效 —— 緊急時不必先切回本程式",
    ),
    Field(
        "熱鍵",
        "hotkey_emergency_stop",
        "緊急停止",
        "hotkey",
        "立刻把強度歸零並暫停輸出",
    ),
    Field("熱鍵", "hotkey_toggle_pause", "暫停／繼續", "hotkey", "切換暫停狀態"),
    Field("熱鍵", "hotkey_reset_stack", "疊加歸零", "hotkey", "留空 = 不綁定"),
    # --- 更新 ---
    Field("更新", "update_enabled", "檢查更新", "bool", "啟動時問 GitHub 有沒有新版本"),
    Field("更新", "update_check_on_start", "啟動時檢查", "bool", "只在這一次啟動時檢查"),
    Field(
        "更新",
        "update_interval",
        "重新檢查間隔（秒）",
        "float",
        "0 = 只檢查啟動那一次",
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

TAB_ORDER: tuple[str, ...] = (
    "基本",
    "疊加",
    "輸出",
    "波形",
    "連線",
    "Discord",
    "熱鍵",
    "更新",
)


def form_values(config: AppConfig) -> dict[str, Any]:
    """把設定攤平成表單用的 ``{欄位鍵: 值}``。"""
    dglab = config.dglab
    policy = dglab.policy
    stack = dglab.stack
    gate = dglab.output
    streak = dglab.streak
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
        "streak_enabled": streak.enabled,
        "streak_starts_at": streak.starts_at,
        "streak_bonus_per_kill": streak.bonus_per_kill,
        "streak_max_bonus": streak.max_bonus,
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
        "hotkeys_enabled": config.hotkeys.enabled,
        "hotkey_emergency_stop": config.hotkeys.emergency_stop,
        "hotkey_toggle_pause": config.hotkeys.toggle_pause,
        "hotkey_reset_stack": config.hotkeys.reset_stack,
        "update_enabled": config.update.enabled,
        "update_check_on_start": config.update.check_on_start,
        "update_interval": config.update.interval,
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
        streak = StreakConfig(
            enabled=boolean("streak_enabled"),
            starts_at=int(number("streak_starts_at", int)),
            bonus_per_kill=number("streak_bonus_per_kill"),
            max_bonus=number("streak_max_bonus"),
        )
    except ValueError as exc:
        raise ValueError(f"電擊槍／死亡歸零／連殺設定不合法：{exc}") from None

    # 熱鍵字串先驗一次，不要等到註冊失敗才發現打錯
    hotkey_values: dict[str, str] = {}
    for key, label in HOTKEY_ACTIONS:
        value = text("hotkey_" + key)
        if value and parse_hotkey(value) is None:
            raise ValueError(
                f"「{label}」的熱鍵 {value!r} 看不懂。\n\n"
                "格式像是 ctrl+alt+s，至少要有一個修飾鍵"
                "（ctrl／alt／shift／win）。"
            )
        hotkey_values[key] = value

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
        hotkeys=HotkeyConfig(
            enabled=boolean("hotkeys_enabled"),
            emergency_stop=hotkey_values.get("emergency_stop", ""),
            toggle_pause=hotkey_values.get("toggle_pause", ""),
            reset_stack=hotkey_values.get("reset_stack", ""),
        ),
        update=UpdateConfig(
            enabled=boolean("update_enabled"),
            check_on_start=boolean("update_check_on_start"),
            interval=number("update_interval"),
            timeout=base.update.timeout,
        ),
        dglab=DglabConfig(
            channels=channels,
            max_strength=int(number("max_strength")),
            respect_app_limit=boolean("respect_app_limit"),
            poll_interval=number("poll_interval"),
            stop_when_no_data_seconds=number("stop_when_no_data_seconds"),
            policy=policy,
            stack=stack,
            streak=streak,
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


# ------------------------------------------------------------------- 折線圖


class StrengthGraph(tk.Canvas):
    """強度與血量的即時折線圖。

    X 軸是「最近 N 個取樣點」，最新的畫在最右邊、舊的往左推 ——
    像示波器一樣，看的重點是「剛剛發生什麼」。

    兩條線各有自己的刻度，但**都正規化成「滿」在最上面**：強度以安全上限
    為滿、血量以 100 為滿。這樣一眼就能看出「血快沒了」和「強度快頂到上限」
    是不是同時發生。
    """

    #: 最多保留幾個取樣點（每個 tick 一點，預設 150ms 所以約 60 秒）
    MAX_POINTS = 400

    PADDING_LEFT = 34
    PADDING_RIGHT = 6
    PADDING_TOP = 8
    PADDING_BOTTOM = 14

    def __init__(self, parent: tk.Widget, *, width: int = 250, height: int = 140) -> None:
        super().__init__(
            parent,
            width=width,
            height=height,
            background=SURFACE,
            highlightthickness=0,
            borderwidth=0,
        )
        self._strength: deque[int] = deque(maxlen=self.MAX_POINTS)
        self._health: deque[int] = deque(maxlen=self.MAX_POINTS)
        self._capacity = 100
        self._shown_capacity = 100
        self.bind("<Configure>", lambda _event: self.redraw())

    # ---------------------------------------------------------------- 資料

    def push(self, strength: int, health: int, capacity: int) -> None:
        """加入一個取樣點（不重畫，重畫的時機由呼叫端決定）。"""
        self._capacity = max(1, int(capacity))
        self._strength.append(max(0, int(strength)))
        self._health.append(max(0, min(100, int(health))))

    def clear(self) -> None:
        self._strength.clear()
        self._health.clear()
        self.redraw()

    @property
    def sample_count(self) -> int:
        return len(self._strength)

    # ---------------------------------------------------------------- 繪圖

    def _geometry(self) -> tuple[int, int, int, int]:
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1:
            width = int(self["width"])
        if height <= 1:
            height = int(self["height"])
        return (
            self.PADDING_LEFT,
            width - self.PADDING_RIGHT,
            self.PADDING_TOP,
            height - self.PADDING_BOTTOM,
        )

    def redraw(self) -> None:
        self.delete("all")
        left, right, top, bottom = self._geometry()
        if right - left < 20 or bottom - top < 20:
            return

        # 上限取成「好看的整數」，免得刻度一直跳
        self._shown_capacity = self._nice_max(self._capacity)

        for ratio in (0.0, 0.25, 0.5, 0.75, 1.0):
            y = bottom - (bottom - top) * ratio
            colour = BORDER if ratio in (0.0, 1.0) else "#26263a"
            self.create_line(left, y, right, y, fill=colour)
            self.create_text(
                left - 5,
                y,
                anchor="e",
                text=str(int(round(self._shown_capacity * ratio))),
                fill=TEXT_DIM,
                font=("Consolas", 7),
            )

        if not self._strength:
            self.create_text(
                (left + right) // 2,
                (top + bottom) // 2,
                text="開始遊戲後會在這裡畫出強度變化",
                fill=TEXT_DIM,
                font=("Microsoft JhengHei UI", 8),
            )
            return

        self._draw_series(self._health, PINK, 100.0, left, right, top, bottom)
        self._draw_series(
            self._strength, ACCENT, float(self._shown_capacity), left, right, top, bottom
        )

    def _draw_series(
        self,
        values: deque[int],
        colour: str,
        full_scale: float,
        left: int,
        right: int,
        top: int,
        bottom: int,
    ) -> None:
        count = len(values)
        if count < 2 or full_scale <= 0:
            return
        # 右邊是「現在」、左邊是過去。用固定間距，所以資料會從右邊長出來。
        step = (right - left) / max(1, self.MAX_POINTS - 1)
        span = bottom - top
        points: list[float] = []
        for index, value in enumerate(values):
            x = right - (count - 1 - index) * step
            ratio = min(1.0, max(0.0, float(value) / full_scale))
            points.extend((x, bottom - span * ratio))
        if len(points) < 4:
            return
        self.create_line(
            *points, fill=colour, width=2, capstyle="round", joinstyle="round"
        )

    @staticmethod
    def _nice_max(value: int) -> int:
        """把上限取成 10 的倍數，讓刻度是整數。"""
        value = max(10, int(value))
        for candidate in (10, 20, 25, 40, 50, 60, 80, 100, 120, 150, 200):
            if value <= candidate:
                return candidate
        return value


# --------------------------------------------------------------- 波形編輯器


class WaveformEditor(tk.Toplevel):
    """圖形化的自訂波形編輯器。

    可以直接用滑鼠拉波形，不必手打 ``[[10, 100], ...]``。

    座標刻意用「裝置值」而不是設定檔裡的輸入值：設定檔的波形頻率是
    10 ~ 1000 的輸出單元時長，但真正有用的範圍集中在 10 ~ 240，用輸入值
    畫圖會全部擠在最下面 9%。裝置值與輸入值可以互相換算
    （:func:`decode_wave_freq`），所以編輯時用裝置值、存檔時換回輸入值。
    """

    FREQ_MIN = WAVE_FREQ_MIN
    FREQ_MAX = WAVE_FREQ_MAX

    PADDING_LEFT = 36
    PADDING_RIGHT = 10
    PADDING_TOP = 10
    PADDING_BOTTOM = 18
    HANDLE_RADIUS = 4

    def __init__(
        self,
        master: tk.Misc,
        samples: Sequence[tuple[int, int]],
        on_apply: Callable[[list[tuple[int, int]]], None],
        *,
        font_normal: tuple | None = None,
        font_small: tuple | None = None,
    ) -> None:
        super().__init__(master, background=BG)
        self.title("波形編輯器 — DGLAB-CS2")
        self.transient(master)
        self.grab_set()
        self._on_apply = on_apply
        self._font_normal = font_normal or ("Microsoft JhengHei UI", 10)
        self._font_small = font_small or ("Microsoft JhengHei UI", 9)

        self._mode = "strength"
        self._drag: int | None = None
        #: ``[[裝置波形頻率, 波形強度], ...]``
        self._points: list[list[int]] = [
            [encode_wave_freq(int(freq)), int(strength)] for freq, strength in samples
        ] or [[WAVE_FREQ_MIN, 100]] * 8

        self._build()
        self._redraw()
        self.update_idletasks()
        self._centre_on_parent()

    # ------------------------------------------------------------ 建立畫面

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)

        toolbar = ttk.Frame(outer)
        toolbar.grid(row=0, column=0, sticky="ew")

        ttk.Label(toolbar, text="編輯：").pack(side="left")
        self._mode_var = tk.StringVar(value="strength")
        for value, label in (("strength", "波形強度"), ("frequency", "波形頻率")):
            tk.Radiobutton(
                toolbar,
                text=label,
                value=value,
                variable=self._mode_var,
                command=self._on_mode_change,
                background=BG,
                foreground=TEXT,
                activebackground=BG,
                activeforeground=ACCENT,
                selectcolor=SURFACE2,
                highlightthickness=0,
                borderwidth=0,
                cursor="hand2",
            ).pack(side="left", padx=(6, 0))

        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Label(toolbar, text="套用預設集：").pack(side="left")
        self._preset_var = tk.StringVar(value="steady")
        ttk.Combobox(
            toolbar,
            textvariable=self._preset_var,
            values=[name for name in PRESET_NAMES if name != "custom"],
            state="readonly",
            width=12,
        ).pack(side="left", padx=6)
        ttk.Button(toolbar, text="載入", command=self._load_preset, width=6).pack(side="left")

        self.canvas = tk.Canvas(
            outer,
            height=210,
            background=SURFACE,
            highlightthickness=0,
            borderwidth=0,
            cursor="hand2",
        )
        self.canvas.grid(row=1, column=0, sticky="ew", pady=(10, 6))
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)
        self.canvas.bind("<Configure>", lambda _event: self._redraw())

        tools = ttk.Frame(outer)
        tools.grid(row=2, column=0, sticky="ew")
        for label, command in (
            ("＋ 取樣點", self._add_point),
            ("－ 取樣點", self._remove_point),
            ("拉平", self._flatten),
            ("反轉", self._reverse),
            ("重設", self._reset),
        ):
            ttk.Button(tools, text=label, command=command, width=10).pack(
                side="left", padx=(0, 5)
            )

        self.info = ttk.Label(outer, text="", style="Caption.TLabel")
        self.info.grid(row=3, column=0, sticky="w", pady=(8, 0))

        footer = ttk.Frame(outer)
        footer.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(
            footer,
            text="拖曳圓點調整數值・雙擊圓點刪除、雙擊空白處新增",
            style="Caption.TLabel",
        ).pack(side="left")
        ttk.Button(footer, text="取消", command=self.destroy, width=10).pack(side="right")
        ttk.Button(
            footer,
            text="套用為自訂波形",
            command=self._apply,
            style="Primary.TButton",
            width=16,
        ).pack(side="right", padx=6)

    def _centre_on_parent(self) -> None:
        parent = self.master
        try:
            x = parent.winfo_rootx() + max(
                0, (parent.winfo_width() - self.winfo_width()) // 2
            )
            y = parent.winfo_rooty() + 60
            self.geometry(f"+{x}+{y}")
        except tk.TclError:  # pragma: no cover - 視窗還沒對應
            pass
        self.lift()

    # ------------------------------------------------------------ 座標換算

    @staticmethod
    def _to_input(device_freq: int) -> int:
        """裝置值 → 設定檔的輸入值。"""
        return decode_wave_freq(int(device_freq))

    def _plot(self) -> tuple[int, int, int, int]:
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        if width <= 1:
            width = int(self.canvas["width"])
        if height <= 1:
            height = int(self.canvas["height"])
        return (
            self.PADDING_LEFT,
            width - self.PADDING_RIGHT,
            self.PADDING_TOP,
            height - self.PADDING_BOTTOM,
        )

    def _x_for(self, index: int) -> float:
        left, right, _top, _bottom = self._plot()
        return left + (right - left) * index / max(1, len(self._points) - 1)

    def _ratio_for(self, index: int, mode: str) -> float:
        if mode == "strength":
            return self._points[index][1] / WAVE_STRENGTH_MAX
        span = max(1, self.FREQ_MAX - self.FREQ_MIN)
        return (self._points[index][0] - self.FREQ_MIN) / span

    def _y_for(self, index: int, mode: str | None = None) -> float:
        _left, _right, top, bottom = self._plot()
        ratio = min(1.0, max(0.0, self._ratio_for(index, mode or self._mode)))
        return bottom - (bottom - top) * ratio

    def _value_from_y(self, y: float) -> int:
        _left, _right, top, bottom = self._plot()
        ratio = min(1.0, max(0.0, (bottom - float(y)) / max(1, bottom - top)))
        if self._mode == "strength":
            return int(round(ratio * WAVE_STRENGTH_MAX))
        return int(round(self.FREQ_MIN + ratio * (self.FREQ_MAX - self.FREQ_MIN)))

    def _hit_test(self, x: float, y: float) -> int | None:
        best: int | None = None
        best_distance = float((self.HANDLE_RADIUS + 7) ** 2)
        for index in range(len(self._points)):
            dx = self._x_for(index) - x
            dy = self._y_for(index) - y
            distance = dx * dx + dy * dy
            if distance <= best_distance:
                best_distance = distance
                best = index
        return best

    # ------------------------------------------------------------ 繪圖

    def _redraw(self) -> None:
        self.canvas.delete("all")
        left, right, top, bottom = self._plot()
        if right - left < 20 or bottom - top < 20:
            return

        accent = ACCENT if self._mode == "strength" else "#a78bfa"

        for ratio in (0.0, 0.25, 0.5, 0.75, 1.0):
            y = bottom - (bottom - top) * ratio
            self.canvas.create_line(
                left, y, right, y, fill=BORDER if ratio in (0.0, 1.0) else "#26263a"
            )
            if self._mode == "strength":
                label = str(int(round(WAVE_STRENGTH_MAX * ratio)))
            else:
                label = str(int(round(self.FREQ_MIN + (self.FREQ_MAX - self.FREQ_MIN) * ratio)))
            self.canvas.create_text(
                left - 5, y, anchor="e", text=label, fill=TEXT_DIM, font=("Consolas", 7)
            )

        # 兩條曲線都畫，非編輯中的那條用暗色當背景參考
        self._draw_curve("frequency", "#5b4b8a", left, right, top, bottom)
        self._draw_curve(
            "strength",
            "#1f6f7d" if self._mode != "strength" else accent,
            left,
            right,
            top,
            bottom,
        )
        self._draw_curve(self._mode, accent, left, right, top, bottom, handles=True)
        self._update_info()

    def _draw_curve(
        self,
        mode: str,
        colour: str,
        left: int,
        right: int,
        top: int,
        bottom: int,
        *,
        handles: bool = False,
    ) -> None:
        if not self._points:
            return
        points: list[float] = []
        for index in range(len(self._points)):
            ratio = min(1.0, max(0.0, self._ratio_for(index, mode)))
            points.extend((self._x_for(index), bottom - (bottom - top) * ratio))

        if len(points) >= 4:
            self.canvas.create_line(
                *points, fill=colour, width=2, capstyle="round", joinstyle="round"
            )
        if not handles:
            return
        radius = self.HANDLE_RADIUS
        for index in range(0, len(points), 2):
            x, y = points[index], points[index + 1]
            self.canvas.create_oval(
                x - radius,
                y - radius,
                x + radius,
                y + radius,
                fill=BG,
                outline=colour,
                width=2,
            )

    def _update_info(self) -> None:
        count = len(self._points)
        seconds = count * FRAME_SECONDS / 4  # 每個取樣點佔 25ms
        strengths = [point[1] for point in self._points]
        freqs = [point[0] for point in self._points]
        self.info.configure(
            text=(
                f"{count} 個取樣點（每個 25ms，循環 {seconds * 1000:.0f}ms）　"
                f"強度 {min(strengths)} ~ {max(strengths)}　"
                f"波形頻率 {min(freqs)} ~ {max(freqs)}"
                f"（{pulse_frequency_hz(min(freqs)):.0f} ~ "
                f"{pulse_frequency_hz(max(freqs)):.0f} Hz）"
            )
        )

    # ------------------------------------------------------------ 互動

    def _on_mode_change(self) -> None:
        self._mode = self._mode_var.get()
        self._redraw()

    def _on_press(self, event: tk.Event) -> None:
        index = self._hit_test(event.x, event.y)
        if index is not None:
            self._drag = index
            self._apply_drag(event.y)

    def _on_drag(self, event: tk.Event) -> None:
        if self._drag is not None:
            self._apply_drag(event.y)

    def _on_release(self, _event: tk.Event) -> None:
        self._drag = None

    def _apply_drag(self, y: float) -> None:
        if self._drag is None:
            return
        value = self._value_from_y(float(y))
        if self._mode == "strength":
            self._points[self._drag][1] = value
        else:
            self._points[self._drag][0] = value
        self._redraw()

    def _on_double_click(self, event: tk.Event) -> None:
        """圓點上雙擊 = 刪掉它；空白處雙擊 = 在最近的間隔插入一個點。"""
        index = self._hit_test(event.x, event.y)
        if index is not None and len(self._points) > 1:
            del self._points[index]
            self._drag = None
        else:
            self._points.insert(self._nearest_gap(event.x), [WAVE_FREQ_MIN, 100])
        self._redraw()

    def _nearest_gap(self, x: float) -> int:
        best, best_distance = len(self._points), None
        for index in range(len(self._points) - 1):
            middle = (self._x_for(index) + self._x_for(index + 1)) / 2
            distance = abs(middle - x)
            if best_distance is None or distance < best_distance:
                best, best_distance = index + 1, distance
        return best

    def _add_point(self) -> None:
        if len(self._points) >= 64:
            return
        template = self._points[-1] if self._points else [WAVE_FREQ_MIN, 100]
        self._points.append(list(template))
        self._redraw()

    def _remove_point(self) -> None:
        if len(self._points) > 1:
            self._points.pop()
            self._redraw()

    def _flatten(self) -> None:
        """把所有點拉成平均值 —— 想要穩定輸出時很好用。"""
        if not self._points:
            return
        index = 1 if self._mode == "strength" else 0
        average = int(round(sum(p[index] for p in self._points) / len(self._points)))
        for point in self._points:
            point[index] = average
        self._redraw()

    def _reverse(self) -> None:
        self._points.reverse()
        self._redraw()

    def _reset(self) -> None:
        self._points = [[WAVE_FREQ_MIN, 100]] * 8
        self._redraw()

    def _load_preset(self) -> None:
        name = self._preset_var.get()
        if name == "custom":
            return
        try:
            frames = resolve_preset(name)
        except ValueError:
            return
        self._points = [[frame.frequency, frame.strength] for frame in frames]
        self._redraw()

    def _apply(self) -> None:
        self._on_apply(self.samples)
        self.destroy()

    @property
    def samples(self) -> list[tuple[int, int]]:
        """目前編輯中的取樣點（換算成設定檔要的輸入值）。"""
        return [(self._to_input(point[0]), point[1]) for point in self._points]


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
        #: 狀態燈號呼吸效果用的計數器
        self._pulse_tick = 0
        #: 查到的 Discord 圖片素材（依 Application ID 快取）
        self._discord_assets: dict[str, tuple] = {}
        self._assets_fetching = False
        self._last_fetched_app_id = ""
        #: 背景執行緒抓回來的圖片清單（tkinter 只能在主執行緒碰，所以走 queue）
        self.asset_results: queue.Queue[tuple[str, str, Any, bool]] = queue.Queue()
        #: 使用者手動填的 Discord 狀態說明（沒有就顯示連線狀態）
        self._discord_note = ""

        #: 折線圖每兩個 tick 重畫一次
        self._graph_tick = 0
        #: 熱鍵觸發（背景執行緒）→ 主執行緒處理
        self._hotkey_queue: queue.Queue[str] = queue.Queue()
        #: 更新檢查的結果（背景執行緒）→ 主執行緒處理
        self._update_queue: queue.Queue[Any] = queue.Queue()
        self._update_thread: threading.Thread | None = None
        self._update_last_check: float | None = None
        self._update_enabled_now = False

        self._build_style()
        self._apply_window_icon()
        self._build_layout()
        self._load_form(config)
        #: 更新提示要是按了「不再提醒」就不會再跳出來
        self._setup_hotkeys()
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
        # 更新檢查晚一點再跑，不要跟啟動搶資源
        self._update_enabled_now = bool(
            config.update.enabled and config.update.check_on_start
        )
        if self._update_enabled_now:
            root.after(2500, self._maybe_check_update)

    def _autostart(self) -> None:
        if not self.engine.running and not self._stopping:
            self._on_start()

    # ------------------------------------------------------------ 畫面建立

    def _build_style(self) -> None:
        """深色主題。

        用 ``clam`` 當基底 —— 內建的 vista／winnative 是走系統繪製的，
        大部分顏色選項會被忽略，只有 clam 能完整換色。
        """
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")

        self.font_normal = ("Microsoft JhengHei UI", 10)
        self.font_small = ("Microsoft JhengHei UI", 9)
        self.font_bold = ("Microsoft JhengHei UI", 10, "bold")
        self.font_title = ("Microsoft JhengHei UI", 13, "bold")
        self.font_huge = ("Microsoft JhengHei UI", 19, "bold")
        self.font_mono = ("Consolas", 10)

        style.configure(
            ".",
            font=self.font_normal,
            background=BG,
            foreground=TEXT,
            fieldbackground=SURFACE2,
            bordercolor=BORDER,
            focuscolor=BG,
            lightcolor=SURFACE,
            darkcolor=SURFACE,
            troughcolor=SURFACE2,
            arrowcolor=TEXT_DIM,
        )

        # --- 基本元件 ---
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("TSeparator", background=BORDER)

        style.configure(
            "TLabelframe",
            background=BG,
            bordercolor=BORDER,
            relief="solid",
            borderwidth=1,
        )
        style.configure(
            "TLabelframe.Label",
            background=BG,
            foreground=ACCENT,
            font=self.font_bold,
        )

        # --- 文字樣式 ---
        style.configure("Title.TLabel", font=self.font_title, background=BG, foreground=TEXT)
        style.configure("Hint.TLabel", font=self.font_small, background=BG, foreground=TEXT_DIM)
        style.configure("Big.TLabel", font=self.font_huge, background=BG, foreground=ACCENT)
        style.configure("Caption.TLabel", font=self.font_small, background=BG, foreground=TEXT_DIM)
        style.configure("Value.TLabel", font=self.font_bold, background=BG, foreground=TEXT)
        style.configure("Info.TLabel", background=BG, foreground=TEXT)
        style.configure("Warn.TLabel", background=BG, foreground=COLOR_WARN)

        # --- 按鈕 ---
        style.configure(
            "TButton",
            background=SURFACE2,
            foreground=TEXT,
            bordercolor=BORDER,
            padding=(8, 4),
            relief="flat",
        )
        style.map(
            "TButton",
            background=[("pressed", ACCENT_DARK), ("active", SURFACE3), ("disabled", SURFACE)],
            foreground=[("disabled", "#5a5a78"), ("active", ACCENT)],
        )
        # 主要動作（例如對話框裡的「立即安裝」）
        style.configure(
            "Primary.TButton",
            font=self.font_bold,
            background=ACCENT,
            foreground="#06232b",
            bordercolor=ACCENT,
        )
        style.map(
            "Primary.TButton",
            background=[("pressed", "#0e7490"), ("active", "#67e8f9")],
            foreground=[("active", "#06232b")],
        )
        # 危險動作（結束、緊急停止）
        style.configure("Danger.TButton", foreground=COLOR_BAD)
        style.map("Danger.TButton", foreground=[("active", "#fca5a5")])

        # --- 輸入類 ---
        for name in ("TEntry", "TCombobox"):
            style.configure(
                name,
                fieldbackground=SURFACE2,
                background=SURFACE2,
                foreground=TEXT,
                insertcolor=ACCENT,
                bordercolor=BORDER,
                lightcolor=BORDER,
                darkcolor=BORDER,
                arrowcolor=TEXT_DIM,
                padding=3,
            )
        style.map(
            "TEntry",
            bordercolor=[("focus", ACCENT)],
            lightcolor=[("focus", ACCENT)],
            darkcolor=[("focus", ACCENT)],
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", SURFACE2), ("disabled", SURFACE)],
            foreground=[("disabled", COLOR_OFF)],
            bordercolor=[("focus", ACCENT)],
            arrowcolor=[("active", ACCENT)],
        )
        # 下拉清單是傳統 Tk 元件，只能用 option database 改
        self.root.option_add("*TCombobox*Listbox.background", SURFACE2)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#06232b")
        self.root.option_add("*TCombobox*Listbox.borderWidth", 0)

        style.configure(
            "TCheckbutton",
            background=BG,
            foreground=TEXT,
            indicatorcolor=SURFACE2,
            focuscolor=BG,
            padding=2,
        )
        style.map(
            "TCheckbutton",
            background=[("active", BG)],
            foreground=[("active", ACCENT), ("disabled", COLOR_OFF)],
            indicatorcolor=[("selected", ACCENT), ("active", SURFACE3)],
        )
        # 註：實際上 bool 欄位用的是 tk.Checkbutton（見 _fill_tab）。
        # clam 的 ttk 勾選框在深色底上會畫成一個打叉的方框，很難看懂。

        # --- 分頁 ---
        style.configure("TNotebook", background=BG, bordercolor=BORDER, tabmargins=(2, 4, 2, 0))
        style.configure(
            "TNotebook.Tab",
            background=SURFACE,
            foreground=TEXT_DIM,
            padding=(14, 5),
            bordercolor=BORDER,
            font=self.font_bold,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", BG)],
            foreground=[("selected", ACCENT)],
            expand=[("selected", (0, 0, 0, 0))],
        )

        # --- 進度條（顏色代表意義，見 _update_values）---
        for name, color in (
            ("Health", PINK),
            ("Burning", ORANGE),
            ("Strength", ACCENT),
            ("StrengthWarn", COLOR_WARN),
            ("StrengthBad", COLOR_BAD),
        ):
            style.configure(
                f"{name}.Horizontal.TProgressbar",
                background=color,
                troughcolor=SURFACE2,
                bordercolor=SURFACE2,
                lightcolor=color,
                darkcolor=color,
            )

        # --- 捲軸 ---
        style.configure("TScrollbar", background=SURFACE2, troughcolor=BG, bordercolor=BG)
        style.map("TScrollbar", background=[("active", ACCENT), ("pressed", ACCENT_DARK)])

        self.root.configure(background=BG)

    def _apply_window_icon(self) -> None:
        """設定視窗左上角的圖示（找不到就沿用預設的）。"""
        candidates = [
            app_directory() / "assets" / "icon.ico",
            Path(__file__).resolve().parent / "assets" / "icon.ico",
        ]
        if getattr(sys, "_MEIPASS", None):
            candidates.insert(0, Path(sys._MEIPASS) / "assets" / "icon.ico")
        for path in candidates:
            try:
                if path.is_file():
                    self.root.iconbitmap(default=str(path))
                    return
            except (tk.TclError, OSError):  # pragma: no cover - 非 Windows
                continue

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
        frame = ttk.Frame(self.root, padding=(12, 0, 12, 0))
        frame.grid(row=0, column=0, sticky="ew")
        for column in range(4):
            frame.columnconfigure(column, weight=1)
        # 最上面一條青色細線，像電源指示燈
        accent = tk.Frame(frame, height=3, background=ACCENT)
        accent.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 10))
        self.socket_status = self._status_cell(frame, 0, "Socket 服務端", row=1)
        self.pair_status = self._status_cell(frame, 1, "DG-LAB App", row=1)
        self.cs2_status = self._status_cell(frame, 2, "CS2 資料", row=1)
        self.gsi_status = self._status_cell(frame, 3, "GSI 設定", row=1)

    def _status_cell(
        self, parent: ttk.Frame, column: int, caption: str, *, row: int = 0
    ) -> StatusCell:
        cell = ttk.Frame(parent)
        cell.grid(row=row, column=column, sticky="w")
        dot = ttk.Label(cell, text="\u25cf", foreground=COLOR_OFF, font=("Segoe UI", 15))
        dot.grid(row=0, column=0, rowspan=2, padx=(0, 6))
        ttk.Label(cell, text=caption, style="Caption.TLabel").grid(
            row=0, column=1, sticky="w"
        )
        value = ttk.Label(cell, text="—", style="Value.TLabel")
        value.grid(row=1, column=1, sticky="w")
        return StatusCell(frame=cell, dot=dot, value=value)

    def _build_main_area(self) -> None:
        main = ttk.Frame(self.root, padding=(12, 8, 12, 0))
        main.grid(row=2, column=0, sticky="nsew")
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        # 左欄自己是一個容器：QR 在上、折線圖在下。
        # 兩者如果各自佔 main 的一列，右欄只跨第一列，就會多出一列的高度。
        left = ttk.Frame(main)
        left.grid(row=0, column=0, sticky="nsew")
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=3)
        left.rowconfigure(1, weight=2)

        self._build_qr_box(left)
        self._build_graph(left)

        right = ttk.Frame(main)
        right.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        self._build_info(right)
        self._build_settings(right)

    def _build_qr_box(self, parent: ttk.Frame) -> None:
        qr_box = ttk.LabelFrame(parent, text=" 配對 QR code ", padding=8)
        qr_box.grid(row=0, column=0, sticky="nsew")
        # QR code 一定要白底黑方塊才掃得到，所以只有這一塊不用深色
        self.qr_canvas = tk.Canvas(
            qr_box,
            width=self.QR_SIZE,
            height=self.QR_SIZE,
            background="white",
            highlightthickness=2,
            highlightbackground=ACCENT,
        )
        self.qr_canvas.grid(row=0, column=0, columnspan=2)
        self.qr_hint = ttk.Label(
            qr_box,
            text="啟動後會顯示 QR code",
            style="Caption.TLabel",
            wraplength=self.QR_SIZE,
            justify="center",
        )
        self.qr_hint.grid(row=1, column=0, columnspan=2, pady=(8, 4))
        ttk.Button(qr_box, text="複製連結", command=self._copy_qr_link, width=12).grid(
            row=2, column=0, padx=(0, 4)
        )
        ttk.Button(qr_box, text="開啟 QR 圖片", command=self._open_qr_image, width=14).grid(
            row=2, column=1
        )
        # 讓 QR 區塊在垂直方向可以跟著視窗長高
        qr_box.rowconfigure(4, weight=1)

    def _build_graph(self, parent: ttk.Frame) -> None:
        """QR code 下面放強度／血量的即時折線圖。"""
        frame = ttk.LabelFrame(parent, text=" 強度歷程 ", padding=8)
        frame.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self.graph = StrengthGraph(frame, width=self.QR_SIZE, height=150)
        self.graph.grid(row=0, column=0, sticky="nsew")

        legend = ttk.Frame(frame)
        legend.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        for colour, text in ((ACCENT, "強度"), (PINK, "血量")):
            ttk.Label(legend, text="\u25cf", foreground=colour, font=("Segoe UI", 10)).pack(
                side="left"
            )
            ttk.Label(legend, text=text, style="Caption.TLabel").pack(
                side="left", padx=(3, 12)
            )
        self.graph_hint = ttk.Label(legend, text="最近 60 秒", style="Caption.TLabel")
        self.graph_hint.pack(side="right")

    def _build_info(self, parent: ttk.Frame) -> None:
        info = ttk.LabelFrame(parent, text=" 即時狀態 ", padding=10)
        info.grid(row=0, column=0, sticky="ew")
        info.columnconfigure(1, weight=1)

        self.health_bar, self.health_text = self._value_row(
            info, 0, "血量", style="Health.Horizontal.TProgressbar"
        )
        self.burning_bar, self.burning_text = self._value_row(
            info, 1, "燃燒", style="Burning.Horizontal.TProgressbar"
        )
        self.strength_bar, self.strength_text = self._value_row(
            info, 2, "強度", style="Strength.Horizontal.TProgressbar", big=True
        )

        ttk.Separator(info).grid(row=3, column=0, columnspan=3, sticky="ew", pady=8)

        self.stack_text = self._info_row(info, 4, "強度疊加", "0.0")
        self.output_text = self._info_row(info, 5, "輸出狀態", "未出力")
        self.app_text = self._info_row(info, 6, "App 回報", "—")
        self.wave_text = self._info_row(info, 7, "波形", "—")
        self.event_text = self._info_row(info, 8, "最近事件", "—")

    def _value_row(
        self, parent: ttk.Frame, row: int, caption: str, *, style: str, big: bool = False
    ):
        ttk.Label(parent, text=caption, width=8).grid(row=row, column=0, sticky="w")
        bar = ttk.Progressbar(parent, maximum=100, length=200, style=style)
        bar.grid(row=row, column=1, sticky="ew", padx=8, pady=3)
        value = ttk.Label(
            parent,
            text="—",
            width=11 if big else 12,
            font=self.font_huge if big else self.font_bold,
            foreground=ACCENT if big else TEXT,
        )
        value.grid(row=row, column=2, sticky="e")
        return bar, value

    def _info_row(self, parent: ttk.Frame, row: int, caption: str, initial: str):
        ttk.Label(parent, text=caption, width=8, style="Caption.TLabel").grid(
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
        self.dry_run_check = tk.Checkbutton(
            bar,
            text="模擬模式",
            variable=self.dry_run_var,
            command=self._on_toggle_dry_run,
            background=BG,
            foreground=TEXT,
            activebackground=BG,
            activeforeground=ACCENT,
            selectcolor=SURFACE2,
            highlightthickness=0,
            borderwidth=0,
            cursor="hand2",
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

        # 有新版本時才顯示（沒有就 grid_remove，完全不佔高度）
        self.update_banner = ttk.Frame(bar)
        self.update_banner.grid(row=1, column=0, columnspan=9, sticky="ew", pady=(8, 0))
        self.update_banner.grid_remove()

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
            # 不給固定寬度：Tk 的寬度單位是「平均字元寬」，中文比它寬，
            # 給固定值反而會把標籤裁掉。讓欄寬自己長到最寬的標籤就好。
            ttk.Label(tab, text=field.label, anchor="w").grid(
                row=row, column=0, sticky="w", pady=4, padx=(0, 12)
            )
            if field.kind == "bool":
                var: tk.Variable = tk.BooleanVar()
                check = tk.Checkbutton(
                    tab,
                    variable=var,
                    background=BG,
                    foreground=TEXT,
                    activebackground=BG,
                    activeforeground=ACCENT,
                    selectcolor=SURFACE2,
                    highlightthickness=0,
                    borderwidth=0,
                    cursor="hand2",
                )
                check.grid(row=row, column=1, sticky="w")
                self.vars["__widget__" + field.key] = check
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
            elif field.kind == "hotkey":
                var = tk.StringVar()
                holder = ttk.Frame(tab)
                holder.grid(row=row, column=1, sticky="w", pady=4)
                entry = ttk.Entry(holder, textvariable=var, width=16, state="readonly")
                entry.pack(side="left")
                ttk.Button(
                    holder,
                    text="錄製",
                    width=6,
                    command=lambda key=field.key: self._record_hotkey(key),
                ).pack(side="left", padx=(6, 0))
                ttk.Button(
                    holder,
                    text="清除",
                    width=6,
                    command=lambda variable=var: variable.set(""),
                ).pack(side="left", padx=(4, 0))
                self.vars["__widget__" + field.key] = entry
            elif field.kind == "text" and field.key == "wave_custom":
                var = tk.StringVar()
                box = tk.Text(
                    tab,
                    height=4,
                    width=30,
                    font=("Consolas", 10),
                    background=SURFACE2,
                    foreground=TEXT,
                    insertbackground=ACCENT,
                    relief="flat",
                    borderwidth=0,
                    padx=6,
                    pady=4,
                )
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
        elif tab_name == "波形":
            self._fill_wave_tab(tab, row)

    def _fill_wave_tab(self, tab: ttk.Frame, row: int) -> None:
        """波形分頁多放一顆「開啟波形編輯器」的按鈕。"""
        ttk.Label(tab, text="圖形化編輯", anchor="w").grid(
            row=row, column=0, sticky="w", pady=4, padx=(0, 12)
        )
        ttk.Button(
            tab,
            text="🎨 開啟波形編輯器",
            command=self._open_wave_editor,
            width=20,
        ).grid(row=row, column=1, sticky="w", pady=4)
        hint = ttk.Label(
            tab,
            text="用滑鼠直接拉波形，不用手打數字；套用後會自動把預設集切成 custom",
            style="Hint.TLabel",
            justify="left",
            wraplength=HINT_WRAPLENGTH,
        )
        hint.grid(row=row, column=2, sticky="ew", padx=(14, 0))
        hint.bind("<Configure>", self._autowrap_hint)

    # ------------------------------------------------------- 波形編輯器

    def _current_wave_samples(self) -> list[tuple[int, int]]:
        """目前表單裡的自訂取樣點；沒填的話就用目前預設集當起點。"""
        try:
            parsed = parse_custom_samples(str(self.vars["wave_custom"].get()))
        except (KeyError, ValueError):
            parsed = ()
        if parsed:
            return list(parsed)
        preset = str(self.vars.get("wave_preset", tk.StringVar()).get() or "steady")
        if preset == "custom":
            preset = "steady"
        try:
            frames = resolve_preset(preset)
        except ValueError:
            return []
        return [(decode_wave_freq(frame.frequency), frame.strength) for frame in frames]

    def _open_wave_editor(self) -> None:
        WaveformEditor(
            self.root,
            self._current_wave_samples(),
            self._apply_wave_samples,
            font_normal=self.font_normal,
            font_small=self.font_small,
        )

    def _apply_wave_samples(self, samples: list[tuple[int, int]]) -> None:
        """把編輯器的結果寫回表單（並把預設集切成 custom）。"""
        text = "\n".join(f"{frequency}, {strength}" for frequency, strength in samples)
        var = self.vars.get("wave_custom")
        if isinstance(var, tk.StringVar):
            var.set(text)
        widget = self.vars.get("__widget__wave_custom")
        if isinstance(widget, tk.Text):
            widget.delete("1.0", "end")
            widget.insert("1.0", text)
        preset = self.vars.get("wave_preset")
        if isinstance(preset, tk.StringVar):
            preset.set("custom")
        self._sync_wave_hint()
        self._append_log(
            f"波形編輯器：已套用 {len(samples)} 個取樣點，記得按「套用設定」生效",
            "success",
        )

    # ------------------------------------------------------- 熱鍵錄製

    _MODIFIER_KEYSYMS = frozenset(
        {
            "control_l", "control_r", "shift_l", "shift_r",
            "alt_l", "alt_r", "win_l", "win_r", "super_l", "super_r",
            "meta_l", "meta_r", "caps_lock", "num_lock",
        }
    )

    def _hotkey_from_event(self, event: tk.Event) -> tuple[int, int] | None:
        """把 tkinter 的按鍵事件轉成 ``(修飾鍵, 虛擬鍵碼)``。

        回傳 ``None`` 代表這個鍵不能當熱鍵（例如只按了 Ctrl 本身）。
        """
        name = str(event.keysym or "").strip().lower()
        if not name or name in self._MODIFIER_KEYSYMS:
            return None
        name = TKSYM_ALIASES.get(name, name)
        vk = vk_for_name(name)
        if vk is None:
            return None

        state = int(event.state)
        modifiers = 0
        if state & 0x0004:
            modifiers |= MOD_CONTROL
        if state & 0x0001:
            modifiers |= MOD_SHIFT
        # Tk 在 Windows 上把 Alt 放在 Mod1，不同版本位置不太一樣，兩個都認
        if state & 0x0008 or state & 0x20000:
            modifiers |= MOD_ALT
        if state & 0x0040:
            modifiers |= MOD_WIN
        return modifiers, vk

    def _record_hotkey(self, key: str) -> None:
        """開一個小視窗，讓使用者直接按組合鍵。"""
        field = FIELD_BY_KEY.get(key)
        title = field.label if field else key

        dialog = tk.Toplevel(self.root, background=BG)
        dialog.title("錄製熱鍵 — DGLAB-CS2")
        dialog.transient(self.root)
        dialog.grab_set()

        ttk.Label(
            dialog, text=f"要綁定的動作：{title}", style="Title.TLabel", padding=(18, 16, 18, 2)
        ).pack(anchor="w")
        ttk.Label(
            dialog,
            text="請直接按下想用的組合鍵（至少要有一個修飾鍵）",
            style="Caption.TLabel",
            padding=(18, 0, 18, 12),
        ).pack(anchor="w")
        preview = ttk.Label(dialog, text="等待按鍵…", style="Big.TLabel", padding=(18, 0, 18, 8))
        preview.pack(anchor="w")
        status = ttk.Label(dialog, text="", style="Caption.TLabel", padding=(18, 0, 18, 4))
        status.pack(anchor="w")

        def finish(value: str) -> None:
            variable = self.vars.get(key)
            if isinstance(variable, tk.StringVar):
                variable.set(value)
            dialog.destroy()

        def on_key(event: tk.Event) -> str:
            if event.keysym == "Escape":
                dialog.destroy()
                return "break"
            parsed = self._hotkey_from_event(event)
            if parsed is None:
                status.configure(
                    text=f"「{event.keysym}」不能當熱鍵，請換一個", foreground=COLOR_WARN
                )
                return "break"
            modifiers, vk = parsed
            if not modifiers:
                status.configure(
                    text="至少要有一個修飾鍵（ctrl／alt／shift／win）", foreground=COLOR_WARN
                )
                preview.configure(text=format_hotkey(modifiers, vk))
                return "break"
            finish(format_hotkey(modifiers, vk))
            return "break"

        bar = ttk.Frame(dialog, padding=(18, 14, 18, 16))
        bar.pack(fill="x")
        ttk.Button(bar, text="不綁定", command=lambda: finish(""), width=10).pack(side="right")
        ttk.Button(bar, text="取消", command=dialog.destroy, width=10).pack(
            side="right", padx=6
        )

        dialog.bind("<KeyPress>", on_key)
        dialog.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - dialog.winfo_width()) // 2)
        dialog.geometry(f"+{x}+{self.root.winfo_rooty() + 120}")
        dialog.focus_force()

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
        """下半部：事件紀錄與本場統計共用一個分頁框，不佔額外高度。"""
        frame = ttk.Frame(self.root)
        frame.grid(row=3, column=0, sticky="nsew", padx=12, pady=10)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self.bottom_tabs = ttk.Notebook(frame)
        self.bottom_tabs.grid(row=0, column=0, sticky="nsew")

        log_tab = ttk.Frame(self.bottom_tabs)
        log_tab.columnconfigure(0, weight=1)
        log_tab.rowconfigure(0, weight=1)
        self.bottom_tabs.add(log_tab, text="事件紀錄")

        self.log_text = tk.Text(
            log_tab,
            height=6,
            wrap="word",
            state="disabled",
            font=("Consolas", 9),
            background=SURFACE,
            foreground=TEXT,
            insertbackground=ACCENT,
            selectbackground=ACCENT_DARK,
            selectforeground=TEXT,
            relief="flat",
            borderwidth=0,
            padx=8,
            pady=6,
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_tab, command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.tag_configure("success", foreground=COLOR_OK)
        self.log_text.tag_configure("warning", foreground=COLOR_WARN)
        self.log_text.tag_configure("error", foreground=COLOR_BAD)
        self.log_text.tag_configure("info", foreground=TEXT_DIM)

        ttk.Button(log_tab, text="清除紀錄", command=self._clear_log, width=10).grid(
            row=1, column=0, sticky="e", pady=(6, 0)
        )

        self._build_stats_tab()

    def _build_stats_tab(self) -> None:
        """本場統計：一整排「標籤 → 數值」。"""
        tab = ttk.Frame(self.bottom_tabs, padding=(12, 10))
        tab.columnconfigure(0, weight=1)
        self.bottom_tabs.add(tab, text="本場統計")

        grid = ttk.Frame(tab)
        grid.grid(row=0, column=0, sticky="nw")
        #: ``{標籤: 顯示數值的 Label}``，由 :meth:`_refresh_stats` 更新
        self.stat_labels: dict[str, ttk.Label] = {}
        for index, (label, _placeholder) in enumerate(DEFAULT_STATS_SUMMARY):
            # 橫的排：每 5 個換一列，這樣只佔兩列高度
            line, column = divmod(index, 5)
            cell = ttk.Frame(grid)
            cell.grid(row=line, column=column, sticky="w", padx=(0, 24), pady=2)
            ttk.Label(cell, text=label, style="Caption.TLabel").pack(anchor="w")
            value = ttk.Label(cell, text="—", style="Value.TLabel")
            value.pack(anchor="w")
            self.stat_labels[label] = value

        footer = ttk.Frame(tab)
        footer.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.stats_hint = ttk.Label(footer, text="", style="Caption.TLabel")
        self.stats_hint.pack(side="left")
        ttk.Button(footer, text="統計歸零", command=self._reset_stats, width=10).pack(
            side="right"
        )

    def _reset_stats(self) -> None:
        self.engine.reset_stats()
        self._refresh_stats(None)
        self._append_log("本場統計已歸零", "info")

    def _refresh_stats(self, stats) -> None:
        if not hasattr(self, "stat_labels"):
            return
        if stats is None:
            for label in self.stat_labels:
                self.stat_labels[label].configure(text="—")
            if hasattr(self, "stats_hint"):
                self.stats_hint.configure(text="")
            return
        for label, value in stats.summary():
            if label in self.stat_labels:
                self.stat_labels[label].configure(text=value)
        self.stats_hint.configure(
            text=f"連殺 {stats.current_streak}（最佳 {stats.best_streak}）"
            if stats.has_data
            else "還沒有資料：進遊戲打一場就會開始累計"
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
        self._apply_hotkeys(config)
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
        self._apply_hotkeys(config)
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
        dialog = tk.Toplevel(self.root, background=BG)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.columnconfigure(0, weight=1)

        ttk.Label(
            dialog, text=message, padding=(12, 12, 12, 6), justify="left", wraplength=width
        ).grid(row=0, column=0, sticky="w")
        if details:
            box = tk.Text(
                dialog,
                width=88,
                height=height,
                wrap="none",
                font=("Consolas", 9),
                background=SURFACE,
                foreground=TEXT_DIM,
                insertbackground=ACCENT,
                relief="flat",
                borderwidth=0,
                padx=10,
                pady=8,
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
        dialog = tk.Toplevel(self.root, background=BG)
        dialog.title("選擇資料夾")
        dialog.transient(self.root)
        dialog.grab_set()
        ttk.Label(dialog, text=title, padding=10).pack(anchor="w")
        listbox = tk.Listbox(
            dialog,
            width=78,
            height=min(8, len(options)),
            background=SURFACE,
            foreground=TEXT,
            selectbackground=ACCENT,
            selectforeground="#06232b",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            activestyle="none",
            font=self.font_normal,
        )
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
        window = tk.Toplevel(self.root, background=BG)
        window.title(f"{tutorial.TUTORIAL_TITLE} — DGLAB-CS2")
        window.transient(self.root)
        frame = ttk.Frame(window, padding=10)
        frame.pack(fill="both", expand=True)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        text = tk.Text(
            frame,
            wrap="word",
            width=82,
            height=34,
            font=self.font_normal,
            background=SURFACE,
            foreground=TEXT,
            insertbackground=ACCENT,
            selectbackground=ACCENT_DARK,
            selectforeground=TEXT,
            relief="flat",
            borderwidth=0,
            padx=12,
            pady=10,
        )
        text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, command=text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=scroll.set)
        text.insert("1.0", tutorial.TUTORIAL_TEXT)
        # 標題與分隔線加一點顏色，長文件比較好讀
        text.tag_configure("heading", foreground=ACCENT, font=self.font_bold)
        text.tag_configure("rule", foreground=BORDER)
        lines = tutorial.TUTORIAL_TEXT.splitlines()
        for index, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped and not set(stripped) - set("─"):
                text.tag_add("rule", f"{index}.0", f"{index}.end")
                continue
            next_line = lines[index].strip() if index < len(lines) else ""
            prev_line = lines[index - 2].strip() if index >= 2 else ""
            if set(next_line) == {"="} or (prev_line and not set(prev_line) - set("─")):
                text.tag_add("heading", f"{index}.0", f"{index}.end")
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
        # 一定要把熱鍵取消掉，否則組合鍵會被這個已經結束的程式佔著
        if hasattr(self, "_hotkeys"):
            self._hotkeys.stop()
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
        self._drain_update_result()
        self._drain_hotkey_messages()

        status = self.engine.status
        self._update_status_bar(status)
        self._update_values(status)
        self._update_buttons(status)
        self._update_qr(status)
        self._update_graph(status)
        self._refresh_stats(status.stats)

        # GSI 設定可能被外部改動（自己動手裝、CS2 更新…），定期重掃一次
        self._ticks_since_gsi_check += 1
        if self._ticks_since_gsi_check >= GSI_CHECK_EVERY_TICKS:
            self._refresh_gsi_check()
        self._update_gsi_status(self._gsi_check)
        self._update_discord_status()
        self._maybe_check_update()

        self.root.after(self.POLL_MS, self._tick)

    # ------------------------------------------------------------ 折線圖

    def _update_graph(self, status) -> None:
        if not hasattr(self, "graph"):
            return
        if status.health_known or self.engine.running:
            self.graph.push(
                status.strength,
                status.health,
                max(1, int(self.config.dglab.max_strength)),
            )
        # 每兩個 tick 重畫一次就好（150ms 畫一次對眼睛沒差，對 CPU 有差）
        self._graph_tick = (self._graph_tick + 1) % 2
        if self._graph_tick == 0:
            self.graph.redraw()

    # ------------------------------------------------------------ 全域熱鍵

    def _setup_hotkeys(self) -> None:
        self._hotkeys = hotkeys_module.HotkeyManager(self._on_hotkey)
        self._apply_hotkeys(self.config)

    def _apply_hotkeys(self, config: AppConfig) -> None:
        """把設定裡的熱鍵註冊上去，失敗的會寫進事件紀錄。"""
        if not hasattr(self, "_hotkeys"):
            return
        if not config.hotkeys.enabled:
            self._hotkeys.clear()
            return
        failures = self._hotkeys.apply(config.hotkeys.bindings())
        # 這是在 __init__ 期間也會被呼叫的路徑，所以進不去 UI 時只記錄
        for key, message in failures.items():
            label = dict(HOTKEY_ACTIONS).get(key, key)
            self.log_queue.put(
                ("error", f"熱鍵「{label}」註冊失敗：{message}", False)
            )

    def _on_hotkey(self, action: str) -> None:
        """熱鍵觸發（在熱鍵的背景執行緒）。

        碰 UI 或引擎一定要丟回主執行緒，所以這裡只放進佇列。
        """
        self._hotkey_queue.put(action)

    def _drain_hotkey_messages(self) -> None:
        try:
            while True:
                action = self._hotkey_queue.get_nowait()
                self._handle_hotkey(action)
        except queue.Empty:
            pass

    def _handle_hotkey(self, action: str) -> None:
        if action == "emergency_stop":
            self.engine.emergency_stop()
            self._append_log("全域熱鍵：緊急停止", "warning")
        elif action == "toggle_pause":
            # _on_pause 自己會判斷要暫停還是繼續
            self._on_pause()
        elif action == "reset_stack":
            self.engine.reset_stack()

    # ------------------------------------------------------------ 檢查更新

    def _maybe_check_update(self) -> None:
        """每隔一段時間（或啟動時）問一次 GitHub。"""
        config = self.config.update
        if not config.enabled or not hasattr(self, "_update_thread"):
            return
        if self._update_thread is not None and self._update_thread.is_alive():
            return
        if not self._update_enabled_now:
            return

        now = time.monotonic()
        if self._update_last_check is not None:
            # 已經檢查過：只有在設定了間隔、而且真的過了一段時間才再查
            if config.interval <= 0:
                self._update_enabled_now = False
                return
            if now - self._update_last_check < config.interval:
                return
        self._update_last_check = now
        self._update_thread = update_check.check_in_background(
            PROJECT_VERSION,
            self._update_queue.put,
            timeout=config.timeout,
        )

    def _drain_update_result(self) -> None:
        try:
            result = self._update_queue.get_nowait()
        except queue.Empty:
            return
        if result.status == update_check.Status.ERROR:
            self._append_log(f"檢查更新失敗：{result.error}", "info")
            return
        if not result.available:
            self._append_log(f"已是最新版本（{result.current}）", "info")
            return
        latest = result.latest
        version = latest.version if latest else "?"
        url = latest.url if latest else update_check.RELEASES_PAGE
        self._append_log(f"有新版本 {version}（目前 {result.current}）→ {url}", "warning")
        self._show_update_banner(version, url)

    def _show_update_banner(self, version: str, url: str) -> None:
        """在按鈕列下面顯示一條「有新版本」的提示（可點擊）。"""
        if not hasattr(self, "update_banner"):
            return
        for child in self.update_banner.winfo_children():
            child.destroy()
        self.update_banner.columnconfigure(0, weight=1)
        link = tk.Label(
            self.update_banner,
            text=f"\u2b06 有新版本 {version} —— 點這裡開啟下載頁面",
            background=ACCENT_DARK,
            foreground=ACCENT,
            cursor="hand2",
            padx=10,
            pady=5,
            font=self.font_bold,
        )
        link.grid(row=0, column=0, sticky="w")
        link.bind("<Button-1>", lambda _event: webbrowser.open(url))
        dismiss = tk.Label(
            self.update_banner,
            text="\u2715",
            background=ACCENT_DARK,
            foreground=TEXT_DIM,
            cursor="hand2",
            padx=8,
        )
        dismiss.grid(row=0, column=1, sticky="e")
        dismiss.bind("<Button-1>", lambda _event: self.update_banner.grid_remove())
        self.update_banner.grid()

    def _update_status_bar(self, status) -> None:
        # 「已連線」的燈號做緩慢的呼吸效果，一眼就看得出程式還活著
        self._pulse_tick = (self._pulse_tick + 1) % 8
        alive = COLOR_OK if self._pulse_tick < 4 else "#1c7a5c"

        if status.error:
            self.socket_status.set(COLOR_BAD, f"錯誤  {status.socket_uri}")
        elif not self.engine.running:
            self.socket_status.set(COLOR_OFF, f"未啟動  {status.socket_uri}")
        elif status.state is EngineState.STARTING:
            self.socket_status.set(COLOR_WARN, f"啟動中  {status.socket_uri}")
        else:
            self.socket_status.set(alive, f"已連線  {status.socket_uri}")

        if status.paired:
            self.pair_status.set(alive, "已配對")
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

        # 強度：數字放大，而且越接近上限顏色越警示
        maximum = max(1, int(self.config.dglab.policy.max_strength))
        value = min(status.strength, maximum)
        ratio = value / maximum
        if ratio >= 0.8:
            bar_style, color = "StrengthBad.Horizontal.TProgressbar", COLOR_BAD
        elif ratio >= 0.55:
            bar_style, color = "StrengthWarn.Horizontal.TProgressbar", COLOR_WARN
        else:
            bar_style, color = "Strength.Horizontal.TProgressbar", ACCENT
        self.strength_bar.configure(
            maximum=maximum, value=value, style=bar_style
        )
        self.strength_text.configure(text=f"{value} / {maximum}", foreground=color)

        self.stack_text.configure(text=f"{status.stack:+.1f}")
        if status.output_active:
            self.output_text.configure(text="\u25cf 出力中", foreground=COLOR_OK)
        else:
            self.output_text.configure(text="\u25cb 未出力", foreground=TEXT_DIM)

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
