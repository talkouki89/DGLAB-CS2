"""讀取並驗證 ``config.toml``。

設定錯誤一律在程式啟動時就擋下來，並且給出明確的中文訊息，
不要等到連上裝置之後才出現莫名其妙的行為（尤其是跟安全有關的強度設定）。
"""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from coyote.socket_protocol import Channel
from coyote.waveform import (
    PRESET_NAMES,
    WAVE_FREQ_INPUT_MAX,
    WAVE_FREQ_INPUT_MIN,
    WaveFrame,
    frame_from_input,
)
from discord_rpc import MAX_ASSET_KEY_LENGTH, MAX_TEXT_LENGTH
from strength import (
    STRENGTH_MAX,
    OutputGate,
    StrengthPolicy,
    StrengthStack,
)

DEFAULT_CONFIG_PATH = "config.toml"

#: 內建的 Discord 應用程式（本專案作者的）。
#:
#: 使用者不改任何設定也能直接顯示 Rich Presence；想換成自己的品牌時，
#: 把 ``discord.client_id`` 換成自己的 Application ID 再重新讀取圖片清單即可。
DEFAULT_DISCORD_CLIENT_ID = "1560163564839510086"
DEFAULT_DISCORD_LARGE_IMAGE = "dglad-logo"
DEFAULT_DISCORD_LARGE_TEXT = "DGLAB-CS2"
DEFAULT_DISCORD_SMALL_IMAGE = "cs2-logo"
DEFAULT_DISCORD_SMALL_TEXT = "Counter-Strike 2"

_WS_SCHEMES = ("ws://", "wss://")


class ConfigError(ValueError):
    """設定檔內容不合法。"""


# ------------------------------------------------------------------ 小工具


def _require_table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] 必須是一個區塊（table）")
    return value


def _get(table: dict[str, Any], key: str, default: Any, where: str) -> Any:
    if key not in table:
        return default
    value = table[key]
    if isinstance(default, bool) and not isinstance(value, bool):
        raise ConfigError(f"{where}.{key} 必須是 true 或 false，目前是 {value!r}")
    if isinstance(default, (int, float)) and not isinstance(default, bool):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where}.{key} 必須是數字，目前是 {value!r}")
    if isinstance(default, str) and not isinstance(value, str):
        raise ConfigError(f"{where}.{key} 必須是字串，目前是 {value!r}")
    return value


def _get_number(
    table: dict[str, Any],
    key: str,
    default: float,
    where: str,
    *,
    low: float,
    high: float,
) -> float:
    value = _get(table, key, default, where)
    number = float(value)
    if not low <= number <= high:
        raise ConfigError(
            f"{where}.{key} 必須介於 {low:g} ~ {high:g}，目前是 {value!r}"
        )
    return number


# -------------------------------------------------------------------- 設定


@dataclass(frozen=True)
class GsiConfig:
    """CS2 Game State Integration 的接收端設定。"""

    host: str = "127.0.0.1"
    port: int = 3000
    token: str = "DGLAB"


@dataclass(frozen=True)
class SocketConfig:
    """DG-LAB Socket 連線與 QR code 設定。"""

    uri: str = "wss://ws.dungeon-lab.cn/"
    register_timeout: float = 20.0
    qr_invert: bool = True
    qr_png: str = "qrcode.png"
    qr_open: bool = True

    @property
    def png_path(self) -> str | None:
        """要輸出的 PNG 路徑；空字串代表不輸出。"""
        return self.qr_png.strip() or None


@dataclass(frozen=True)
class WaveConfig:
    """波形設定。"""

    preset: str = "steady"
    custom_samples: tuple[tuple[int, int], ...] = ()

    def build_frames(self) -> tuple[WaveFrame, ...]:
        """產生實際要送出的波形取樣點。"""
        from coyote.waveform import resolve_preset

        if self.preset == "custom":
            frames = [
                frame_from_input(frequency, strength)
                for frequency, strength in self.custom_samples
            ]
        else:
            frames = resolve_preset(self.preset)
        return tuple(frames)


@dataclass(frozen=True)
class TaserConfig:
    """被電擊槍（Zeus x27）電到時的特別處置。

    GSI 不會告訴我們傷害是誰造成的，所以只能用「單次掉了多少血」來判斷。
    電擊槍是一擊必殺（傷害 500），所以門檻預設 100
    ——也就是「一次就被打到 0 血」。

    .. note::
       AWP 爆頭、近戰背刺之類的一擊必殺也會符合這個條件，
       GSI 沒有提供傷害來源，沒辦法再區分。
    """

    enabled: bool = True
    #: 觸發時把強度拉到這個值（仍然受 max_strength 限制）
    strength: int = 50
    #: 持續幾秒
    duration: float = 1.0
    #: 單次掉血達到這個值才算（0 = 停用判斷）
    min_damage: int = 100

    def __post_init__(self) -> None:
        if not 0 <= self.strength <= STRENGTH_MAX:
            raise ValueError(f"taser.strength 必須介於 0 ~ {STRENGTH_MAX}")
        if self.duration < 0:
            raise ValueError("taser.duration 不可為負")
        if self.min_damage < 0:
            raise ValueError("taser.min_damage 不可為負")


@dataclass(frozen=True)
class DeathStopConfig:
    """死亡之後過幾秒把強度歸零。"""

    enabled: bool = True
    #: 死亡後幾秒歸零
    delay: float = 5.0

    def __post_init__(self) -> None:
        if self.delay < 0:
            raise ValueError("death_stop.delay 不可為負")


@dataclass(frozen=True)
class StreakConfig:
    """連殺獎勵：連續擊殺時，每一殺扣掉的疊加值越來越多。"""

    enabled: bool = True
    #: 從第幾殺開始算獎勵（2 = 第二殺開始）
    starts_at: int = 2
    #: 每多一殺，這一殺再多扣多少疊加值
    bonus_per_kill: float = 1.0
    #: 單次額外扣減的上限
    max_bonus: float = 6.0

    def __post_init__(self) -> None:
        if self.starts_at < 1:
            raise ValueError("streak.starts_at 至少要是 1")
        if self.bonus_per_kill < 0:
            raise ValueError("streak.bonus_per_kill 不可為負")
        if self.max_bonus < 0:
            raise ValueError("streak.max_bonus 不可為負")


@dataclass(frozen=True)
class DglabConfig:
    """郊狼裝置與玩法設定。"""

    channels: frozenset[Channel] = frozenset({Channel.A})
    max_strength: int = 80
    respect_app_limit: bool = True
    #: 控制迴圈的「最大」檢查間隔（秒）。
    #:
    #: 平常是事件驅動：收到 GSI 資料就立刻處理，所以這個值只是保底
    #: （看門狗、狀態刷新、沒有 GSI 資料時的心跳）。
    poll_interval: float = 0.1
    stop_when_no_data_seconds: float = 30.0
    policy: StrengthPolicy = field(default_factory=StrengthPolicy)
    stack: StrengthStack = field(default_factory=StrengthStack)
    streak: StreakConfig = field(default_factory=StreakConfig)
    output: OutputGate = field(default_factory=OutputGate)
    wave: WaveConfig = field(default_factory=WaveConfig)
    taser: TaserConfig = field(default_factory=TaserConfig)
    death_stop: DeathStopConfig = field(default_factory=DeathStopConfig)


#: 可以綁定全域熱鍵的動作：``(設定鍵, 中文名稱)``。
#:
#: 放在設定層是因為「有哪些動作可以綁」同時是設定與介面的知識，
#: 兩邊都從這裡讀才不會兜不起來。
HOTKEY_ACTIONS: tuple[tuple[str, str], ...] = (
    ("emergency_stop", "緊急停止（歸零並暫停）"),
    ("toggle_pause", "暫停／繼續"),
    ("reset_stack", "疊加歸零"),
)


@dataclass(frozen=True)
class HotkeyConfig:
    """全域熱鍵。

    「全域」的意思是即使 CS2 是前景視窗也有效 —— 緊急的時候不必先切回本程式。

    空字串代表不綁定。
    """

    enabled: bool = True
    emergency_stop: str = "ctrl+alt+s"
    toggle_pause: str = "ctrl+alt+p"
    reset_stack: str = ""

    def bindings(self) -> dict[str, str]:
        """回傳 ``{動作名稱: 熱鍵字串}``，略過沒有綁定的。"""
        out: dict[str, str] = {}
        for key, _label in HOTKEY_ACTIONS:
            value = str(getattr(self, key, "") or "").strip()
            if value:
                out[key] = value
        return out


@dataclass(frozen=True)
class DashboardConfig:
    """給別人看的即時狀態網頁（直播用）。

    這是一個**唯讀**的伺服器，跟收 GSI 資料的那個埠完全分開 ——
    GSI 埠收的是能改變電擊行為的指令，開放出去等於讓人遙控你的裝置。
    """

    enabled: bool = False
    #: 綁定的位址。預設只有自己看得到；要給別人看就改成 "0.0.0.0"
    host: str = "127.0.0.1"
    port: int = 8770
    #: 通關密語（放在網址的 ?token=...）。空字串 = 不檢查
    token: str = ""
    #: 網頁重新抓資料的間隔（毫秒）
    refresh_ms: int = 500
    #: 網頁標題
    title: str = "DGLAB-CS2"

    def __post_init__(self) -> None:
        if not 0 <= self.port <= 65535:
            raise ValueError("dashboard.port 必須介於 0 ~ 65535")
        if self.refresh_ms < 100:
            raise ValueError("dashboard.refresh_ms 不可小於 100")


@dataclass(frozen=True)
class UpdateConfig:
    """自動檢查更新。"""

    enabled: bool = True
    #: 啟動時自動檢查一次
    check_on_start: bool = True
    #: 之後每隔幾秒再檢查一次（0 = 只檢查啟動那次）
    interval: float = 0.0
    timeout: float = 10.0

    def __post_init__(self) -> None:
        if self.interval < 0:
            raise ValueError("update.interval 不可為負")
        if self.timeout <= 0:
            raise ValueError("update.timeout 必須大於 0")


@dataclass(frozen=True)
class DiscordConfig:
    """Discord Rich Presence 設定。

    顯示的應用名稱與圖片都是跟著 ``client_id`` 走的。這裡預設放的是本專案
    作者自己的應用程式，所以**不設定也能直接用**；想換成自己的品牌時，
    把 ``client_id`` 換成自己的 Application ID 再重新讀取圖片清單就好。
    """

    enabled: bool = False
    #: 預設用作者的應用程式；換成自己的 Application ID 就會顯示你自己的名稱
    client_id: str = DEFAULT_DISCORD_CLIENT_ID
    update_interval: float = 5.0
    show_health: bool = True
    show_strength: bool = True
    show_stack: bool = True
    #: 大圖示的 asset key（在 Discord 開發者頁面的 Art Assets 上傳後的名稱）
    large_image: str = DEFAULT_DISCORD_LARGE_IMAGE
    #: 滑鼠移到大圖示時顯示的文字
    large_text: str = DEFAULT_DISCORD_LARGE_TEXT
    #: 疊在大圖示右下角的小圖示
    small_image: str = DEFAULT_DISCORD_SMALL_IMAGE
    #: 滑鼠移到小圖示時顯示的文字
    small_text: str = DEFAULT_DISCORD_SMALL_TEXT

    def __post_init__(self) -> None:
        for name in ("large_image", "small_image"):
            value = getattr(self, name)
            if len(value) > MAX_ASSET_KEY_LENGTH:
                raise ValueError(
                    f"discord.{name} 太長（上限 {MAX_ASSET_KEY_LENGTH} 字）"
                )
        for name in ("large_text", "small_text"):
            if len(getattr(self, name)) > MAX_TEXT_LENGTH:
                raise ValueError(
                    f"discord.{name} 太長（上限 {MAX_TEXT_LENGTH} 字）"
                )

    @property
    def usable(self) -> bool:
        """有開、而且有填 Application ID，才真的能連上 Discord。"""
        return self.enabled and bool(self.client_id)

    @property
    def missing_client_id(self) -> bool:
        return self.enabled and not self.client_id


@dataclass(frozen=True)
class AppConfig:
    """整份設定。"""

    debug: bool = False
    gsi: GsiConfig = field(default_factory=GsiConfig)
    socket: SocketConfig = field(default_factory=SocketConfig)
    dglab: DglabConfig = field(default_factory=DglabConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    hotkeys: HotkeyConfig = field(default_factory=HotkeyConfig)
    update: UpdateConfig = field(default_factory=UpdateConfig)


# ------------------------------------------------------------------ 解析


def parse_config(raw: dict[str, Any]) -> AppConfig:
    """把 ``tomllib`` 讀出來的 dict 轉成 :class:`AppConfig`。"""
    debug = raw.get("debug", False)
    if not isinstance(debug, bool):
        raise ConfigError(f"debug 必須是 true 或 false，目前是 {debug!r}")

    gsi = _parse_gsi(_require_table(raw, "gsi"))
    socket = _parse_socket(_require_table(raw, "socket"))
    dglab = _parse_dglab(_require_table(raw, "dglab"))
    discord = _parse_discord(_require_table(raw, "discord"))
    dashboard = _parse_dashboard(raw.get("dashboard", {}))
    hotkeys = _parse_hotkeys(raw.get("hotkeys", {}))
    update = _parse_update(raw.get("update", {}))
    return AppConfig(
        debug=debug,
        gsi=gsi,
        socket=socket,
        dglab=dglab,
        discord=discord,
        dashboard=dashboard,
        hotkeys=hotkeys,
        update=update,
    )


def _optional_table(value: Any, name: str) -> dict[str, Any]:
    """選用區塊：不存在或 None 時回傳空 dict，存在但不是 table 才報錯。"""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] 必須是一個區塊（table）")
    return value


def _parse_dashboard(raw: Any) -> DashboardConfig:
    table = _optional_table(raw, "dashboard")
    try:
        return DashboardConfig(
            enabled=bool(_get(table, "enabled", False, "dashboard")),
            host=str(_get(table, "host", "127.0.0.1", "dashboard")).strip(),
            port=int(_get_number(table, "port", 8770, "dashboard", low=0, high=65535)),
            token=str(_get(table, "token", "", "dashboard")),
            refresh_ms=int(
                _get_number(table, "refresh_ms", 500, "dashboard", low=100, high=60000)
            ),
            title=str(_get(table, "title", "DGLAB-CS2", "dashboard")),
        )
    except ValueError as exc:
        raise ConfigError(f"[dashboard] 不合法：{exc}") from None


def _parse_hotkeys(raw: Any) -> HotkeyConfig:
    table = _optional_table(raw, "hotkeys")
    try:
        config = HotkeyConfig(
            enabled=bool(_get(table, "enabled", True, "hotkeys")),
            emergency_stop=str(_get(table, "emergency_stop", "ctrl+alt+s", "hotkeys")),
            toggle_pause=str(_get(table, "toggle_pause", "ctrl+alt+p", "hotkeys")),
            reset_stack=str(_get(table, "reset_stack", "", "hotkeys")),
        )
    except ValueError as exc:
        raise ConfigError(f"[hotkeys] 不合法：{exc}") from None

    # 熱鍵字串在這裡先驗一次，免得使用者打完要到啟動時才發現打錯
    try:
        from hotkeys import parse_hotkey
    except ImportError:  # pragma: no cover - 理論上不會發生
        return config

    for key, label in HOTKEY_ACTIONS:
        value = str(getattr(config, key, "") or "").strip()
        if value and parse_hotkey(value) is None:
            raise ConfigError(
                f"hotkeys.{key}（{label}）的熱鍵 {value!r} 看不懂。"
                "格式像是 ctrl+alt+s，至少要有一個修飾鍵（ctrl／alt／shift／win）。"
            )
    return config


def _parse_update(raw: Any) -> UpdateConfig:
    table = _optional_table(raw, "update")
    try:
        return UpdateConfig(
            enabled=bool(_get(table, "enabled", True, "update")),
            check_on_start=bool(_get(table, "check_on_start", True, "update")),
            interval=_get_number(table, "interval", 0.0, "update", low=0, high=86400),
            timeout=_get_number(table, "timeout", 10.0, "update", low=0.5, high=120),
        )
    except ValueError as exc:
        raise ConfigError(f"[update] 不合法：{exc}") from None


def _parse_gsi(gsi_raw: dict[str, Any]) -> GsiConfig:
    # token 允許空字串，代表「接受任何來源」；單純是為了方便排除設定問題
    return GsiConfig(
        host=_get(gsi_raw, "host", "127.0.0.1", "gsi"),
        port=int(_get_number(gsi_raw, "port", 3000, "gsi", low=0, high=65535)),
        token=_get(gsi_raw, "token", "DGLAB", "gsi"),
    )


def _parse_socket(socket_raw: dict[str, Any]) -> SocketConfig:
    uri = str(_get(socket_raw, "uri", "wss://ws.dungeon-lab.cn/", "socket")).strip()
    if not uri.startswith(_WS_SCHEMES):
        raise ConfigError(
            f"socket.uri 必須以 {' 或 '.join(_WS_SCHEMES)} 開頭，目前是 {uri!r}"
        )
    return SocketConfig(
        uri=uri,
        register_timeout=_get_number(
            socket_raw, "register_timeout", 20.0, "socket", low=1, high=300
        ),
        qr_invert=bool(_get(socket_raw, "qr_invert", True, "socket")),
        qr_png=str(_get(socket_raw, "qr_png", "qrcode.png", "socket")).strip(),
        qr_open=bool(_get(socket_raw, "qr_open", True, "socket")),
    )


def _parse_dglab(dglab_raw: dict[str, Any]) -> DglabConfig:
    channels = _parse_channels(_get(dglab_raw, "channels", "A", "dglab"))
    max_strength = _get_number(
        dglab_raw, "max_strength", 80, "dglab", low=0, high=200
    )

    try:
        policy = StrengthPolicy(
            base_strength=_get_number(
                dglab_raw, "base_strength", 20, "dglab", low=0, high=200
            ),
            strength_per_hp=_get_number(
                dglab_raw, "strength_per_hp", 0.2, "dglab", low=0, high=10
            ),
            burning_multiplier=_get_number(
                dglab_raw, "burning_multiplier", 0.2, "dglab", low=0, high=10
            ),
            keep_strength_while_not_injured=bool(
                _get(dglab_raw, "keep_strength_while_not_injured", True, "dglab")
            ),
            max_strength=max_strength,
        )
    except ValueError as exc:
        raise ConfigError(f"dglab 的強度設定不合法：{exc}") from None

    try:
        stack = StrengthStack(
            enabled=bool(_get(dglab_raw, "stack_enabled", True, "dglab")),
            death_gain=_get_number(
                dglab_raw, "stack_death_gain", 5.0, "dglab", low=0, high=200
            ),
            kill_reduction=_get_number(
                dglab_raw, "stack_kill_reduction", 2.0, "dglab", low=0, high=200
            ),
            minimum=_get_number(
                dglab_raw, "stack_min", 0.0, "dglab", low=-200, high=200
            ),
            maximum=_get_number(
                dglab_raw, "stack_max", 30.0, "dglab", low=-200, high=200
            ),
        )
        output = OutputGate(
            mode=str(_get(dglab_raw, "output_mode", "continuous", "dglab")),
            hit_duration=_get_number(
                dglab_raw, "hit_duration", 0.35, "dglab", low=0, high=10
            ),
            hit_boost=_get_number(
                dglab_raw, "hit_boost", 0.0, "dglab", low=0, high=200
            ),
        )
    except ValueError as exc:
        raise ConfigError(f"dglab 的疊加／輸出設定不合法：{exc}") from None

    try:
        taser = TaserConfig(
            enabled=bool(_get(dglab_raw, "taser_enabled", True, "dglab")),
            strength=int(
                _get_number(dglab_raw, "taser_strength", 50, "dglab", low=0, high=STRENGTH_MAX)
            ),
            duration=_get_number(
                dglab_raw, "taser_duration", 1.0, "dglab", low=0, high=60
            ),
            min_damage=int(
                _get_number(dglab_raw, "taser_min_damage", 100, "dglab", low=0, high=1000)
            ),
        )
        death_stop = DeathStopConfig(
            enabled=bool(_get(dglab_raw, "death_stop_enabled", True, "dglab")),
            delay=_get_number(
                dglab_raw, "death_stop_delay", 5.0, "dglab", low=0, high=600
            ),
        )
        streak = StreakConfig(
            enabled=bool(_get(dglab_raw, "streak_enabled", True, "dglab")),
            starts_at=int(
                _get_number(dglab_raw, "streak_starts_at", 2, "dglab", low=1, high=100)
            ),
            bonus_per_kill=_get_number(
                dglab_raw, "streak_bonus_per_kill", 1.0, "dglab", low=0, high=200
            ),
            max_bonus=_get_number(
                dglab_raw, "streak_max_bonus", 6.0, "dglab", low=0, high=200
            ),
        )
    except ValueError as exc:
        raise ConfigError(f"dglab 的電擊槍／死亡歸零／連殺設定不合法：{exc}") from None

    wave_raw = dglab_raw.get("wave", {})
    if not isinstance(wave_raw, dict):
        raise ConfigError("[dglab.wave] 必須是一個區塊（table）")
    preset = str(_get(wave_raw, "preset", "steady", "dglab.wave")).strip().lower()
    if preset not in PRESET_NAMES:
        raise ConfigError(
            f"dglab.wave.preset 必須是 {', '.join(PRESET_NAMES)} 其中之一，"
            f"目前是 {preset!r}"
        )
    custom_samples = _parse_custom_samples(wave_raw.get("custom_samples", []))
    if preset == "custom" and not custom_samples:
        raise ConfigError(
            "dglab.wave.preset = \"custom\" 時必須提供 "
            "custom_samples = [[波形頻率ms, 波形強度], ...]"
        )
    wave = WaveConfig(preset=preset, custom_samples=custom_samples)
    try:
        wave.build_frames()
    except ValueError as exc:
        raise ConfigError(f"dglab.wave 不合法：{exc}") from None

    return DglabConfig(
        channels=channels,
        max_strength=int(max_strength),
        respect_app_limit=bool(_get(dglab_raw, "respect_app_limit", True, "dglab")),
        poll_interval=_get_number(
            dglab_raw, "poll_interval", 0.1, "dglab", low=0.02, high=5
        ),
        stop_when_no_data_seconds=_get_number(
            dglab_raw, "stop_when_no_data_seconds", 30.0, "dglab", low=0, high=3600
        ),
        policy=policy,
        stack=stack,
        streak=streak,
        output=output,
        wave=wave,
        taser=taser,
        death_stop=death_stop,
    )


def _parse_discord(discord_raw: dict[str, Any]) -> DiscordConfig:
    client_id = str(
        _get(discord_raw, "client_id", DEFAULT_DISCORD_CLIENT_ID, "discord")
    ).strip()
    if client_id and not client_id.isdigit():
        raise ConfigError(
            "discord.client_id 必須是純數字（Discord 應用程式的 Application ID），"
            f"目前是 {client_id!r}"
        )
    try:
        return DiscordConfig(
            enabled=bool(_get(discord_raw, "enabled", False, "discord")),
            client_id=client_id,
            update_interval=_get_number(
                discord_raw, "update_interval", 5.0, "discord", low=1, high=300
            ),
            show_health=bool(_get(discord_raw, "show_health", True, "discord")),
            show_strength=bool(_get(discord_raw, "show_strength", True, "discord")),
            show_stack=bool(_get(discord_raw, "show_stack", True, "discord")),
            large_image=str(
                _get(
                    discord_raw,
                    "large_image",
                    DEFAULT_DISCORD_LARGE_IMAGE,
                    "discord",
                )
            ).strip(),
            large_text=str(
                _get(discord_raw, "large_text", DEFAULT_DISCORD_LARGE_TEXT, "discord")
            ).strip(),
            small_image=str(
                _get(
                    discord_raw,
                    "small_image",
                    DEFAULT_DISCORD_SMALL_IMAGE,
                    "discord",
                )
            ).strip(),
            small_text=str(
                _get(discord_raw, "small_text", DEFAULT_DISCORD_SMALL_TEXT, "discord")
            ).strip(),
        )
    except ValueError as exc:
        raise ConfigError(f"discord 設定不合法：{exc}") from None


def _parse_channels(value: Any) -> frozenset[Channel]:
    try:
        channels = Channel.parse_all(value)
    except ValueError as exc:
        raise ConfigError(f"dglab.channels 不合法：{exc}") from None
    if not channels:
        raise ConfigError("dglab.channels 至少要有一個通道（A、B 或 AB）")
    return channels


def _parse_custom_samples(value: Any) -> tuple[tuple[int, int], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ConfigError("dglab.wave.custom_samples 必須是陣列")
    samples: list[tuple[int, int]] = []
    for index, item in enumerate(value):
        if (
            not isinstance(item, Sequence)
            or isinstance(item, (str, bytes))
            or len(item) != 2
        ):
            raise ConfigError(
                f"dglab.wave.custom_samples[{index}] 必須是 "
                "[波形頻率ms, 波形強度] 兩個數字"
            )
        frequency, strength = item
        if not isinstance(frequency, (int, float)) or isinstance(frequency, bool):
            raise ConfigError(f"dglab.wave.custom_samples[{index}] 的波形頻率必須是數字")
        if not isinstance(strength, (int, float)) or isinstance(strength, bool):
            raise ConfigError(f"dglab.wave.custom_samples[{index}] 的波形強度必須是數字")
        if not WAVE_FREQ_INPUT_MIN <= float(frequency) <= WAVE_FREQ_INPUT_MAX:
            raise ConfigError(
                f"dglab.wave.custom_samples[{index}] 的波形頻率必須介於 "
                f"{WAVE_FREQ_INPUT_MIN} ~ {WAVE_FREQ_INPUT_MAX}（單位：ms）"
            )
        if not 0 <= float(strength) <= 100:
            raise ConfigError(
                f"dglab.wave.custom_samples[{index}] 的波形強度必須介於 0 ~ 100"
            )
        samples.append((int(frequency), int(strength)))
    return tuple(samples)


def with_max_strength(config: AppConfig, value: float) -> AppConfig:
    """更新安全上限。

    ``DglabConfig.max_strength`` 與 ``policy.max_strength`` 是同一件事的兩份值
    （:func:`parse_config` 會把它們設成一樣），改的時候一定要一起改，
    否則會出現「UI 顯示 50、實際卻還是夾在 80」這種鬼問題。
    """
    dglab = config.dglab
    return dataclasses.replace(
        config,
        dglab=dataclasses.replace(
            dglab,
            max_strength=int(value),
            policy=dataclasses.replace(dglab.policy, max_strength=float(value)),
        ),
    )


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    """從 TOML 檔讀取設定。

    Raises:
        ConfigError: 檔案不存在或內容不合法。
    """
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"找不到設定檔：{config_path}")

    # Windows 上的編輯器（記事本、PowerShell 的 Set-Content 等）常常會加上
    # UTF-8 BOM，tomllib 會因此判定成語法錯誤，這裡先自行剝掉。
    data = config_path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"{config_path} 不是 UTF-8 編碼（{exc}）；請用 UTF-8 重新存檔"
        ) from None
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_path} 不是合法的 TOML：{exc}") from exc
    try:
        return parse_config(raw)
    except ConfigError as exc:
        raise ConfigError(f"{config_path}：{exc}") from None
