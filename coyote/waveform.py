"""郊狼 3.0 的波形資料。

不論是藍牙還是 Socket，郊狼 3.0 的波形都是用「4 組波形頻率 + 4 組波形強度」
共 8 個位元組來描述 100ms 的輸出；Socket 協定直接把這 8 個位元組以十六進位
字串放在 ``pulse-<通道>:[...]`` 裡。

這個模組只做純資料處理，不碰網路，所以可以直接單元測試。

協定依據（DG-LAB 官方開源文件）：
- 藍牙協定 V3：https://github.com/dungeonlab-open/dglab-bluetooth-protocol/blob/main/coyote/v3/README.md
- 脈衝波形解釋：https://github.com/dungeonlab-open/dglab-bluetooth-protocol/blob/main/coyote/README.md
- App 內建波形轉 V3 格式：https://github.com/dungeonlab-open/dglab-bluetooth-protocol/blob/main/coyote/v3/example.md
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

# --------------------------------------------------------------------- 常數

#: 每個波形單位代表的時間（秒）。App 的波形佇列也是以這個單位計算。
FRAME_SECONDS = 0.1

#: 每個波形單位攜帶的取樣點數量
SAMPLES_PER_FRAME = 4

#: 程式端可用的「波形頻率」輸入範圍（單位：輸出單元時長 ms）
WAVE_FREQ_INPUT_MIN = 10
WAVE_FREQ_INPUT_MAX = 1000

#: 裝置端實際接受的波形頻率值
WAVE_FREQ_MIN = 10
WAVE_FREQ_MAX = 240

#: 波形強度
WAVE_STRENGTH_MIN = 0
WAVE_STRENGTH_MAX = 100

#: 通道強度範圍
STRENGTH_MIN = 0
STRENGTH_MAX = 200


def clamp(value: int, low: int, high: int) -> int:
    """把 ``value`` 限制在 ``[low, high]`` 之內。"""
    return low if value < low else high if value > high else value


def encode_wave_freq(input_value: int) -> int:
    """把波形頻率輸入值 (10~1000) 壓縮成裝置要的值 (10~240)。

    官方給的公式是分段函式，且使用整數截斷：

    ====================  ================================
    輸入值                波形頻率（裝置值）
    ====================  ================================
    10 ~ 100             輸入值
    101 ~ 600            (輸入值 - 100) / 5 + 100
    601 ~ 1000           (輸入值 - 600) / 10 + 200
    ====================  ================================

    這裡的「輸入值」在數值上等於 V2 協定的 ``X + Y``（輸出單元時長，單位 ms）。
    """
    value = int(input_value)
    if value <= WAVE_FREQ_INPUT_MIN:
        return WAVE_FREQ_INPUT_MIN
    if value <= 100:
        return value
    if value <= 600:
        return (value - 100) // 5 + 100
    if value <= WAVE_FREQ_INPUT_MAX:
        return (value - 600) // 10 + 200
    return WAVE_FREQ_MAX


def decode_wave_freq(output_value: int) -> int:
    """:func:`encode_wave_freq` 的反函式：裝置值 (10~240) -> 輸出單元時長 (ms)。"""
    value = clamp(int(output_value), WAVE_FREQ_MIN, WAVE_FREQ_MAX)
    if value <= 100:
        return value
    if value <= 200:
        return (value - 100) * 5 + 100
    return (value - 200) * 10 + 600


def pulse_frequency_hz(output_value: int) -> float:
    """由裝置波形頻率值推算實際脈衝頻率 (Hz)，只用於記錄／顯示。"""
    period_ms = decode_wave_freq(output_value)
    return 1000.0 / period_ms if period_ms else 0.0


# --------------------------------------------------------------- 波形單位材


@dataclass(frozen=True)
class ChannelWave:
    """單一通道在一個 100ms 波形單位內的資料（4 組頻率 + 4 組強度）。"""

    frequencies: tuple[int, int, int, int]
    strengths: tuple[int, int, int, int]

    def __post_init__(self) -> None:
        if len(self.frequencies) != SAMPLES_PER_FRAME:
            raise ValueError(f"波形頻率必須剛好 {SAMPLES_PER_FRAME} 組")
        if len(self.strengths) != SAMPLES_PER_FRAME:
            raise ValueError(f"波形強度必須剛好 {SAMPLES_PER_FRAME} 組")
        for freq in self.frequencies:
            if not WAVE_FREQ_MIN <= freq <= WAVE_FREQ_MAX:
                raise ValueError(
                    f"波形頻率 {freq} 超出範圍 "
                    f"({WAVE_FREQ_MIN} ~ {WAVE_FREQ_MAX})"
                )
        for strength in self.strengths:
            if not WAVE_STRENGTH_MIN <= strength <= WAVE_STRENGTH_MAX:
                raise ValueError(
                    f"波形強度 {strength} 超出範圍 "
                    f"({WAVE_STRENGTH_MIN} ~ {WAVE_STRENGTH_MAX})"
                )

    @classmethod
    def steady(cls, frequency: int, strength: int) -> "ChannelWave":
        """4 組都相同的穩定波形。"""
        return cls((frequency,) * SAMPLES_PER_FRAME, (strength,) * SAMPLES_PER_FRAME)

    @classmethod
    def pause(cls) -> "ChannelWave":
        """暫停輸出。

        官方 App 內建的「呼吸」波形就是用 ``0A0A0A0A00000000``
        （波形頻率 10、波形強度 0）代表不輸出，這裡沿用同樣的做法。
        """
        return cls.steady(WAVE_FREQ_MIN, WAVE_STRENGTH_MIN)

    @classmethod
    def from_samples(cls, samples: Iterable[tuple[int, int]]) -> "ChannelWave":
        """由 ``(頻率, 強度)`` 序列建立；不足 4 組時重複最後一組，超過則取前 4 組。"""
        pairs = [(int(f), int(s)) for f, s in samples]
        if not pairs:
            return cls.pause()
        if len(pairs) > SAMPLES_PER_FRAME:
            pairs = pairs[:SAMPLES_PER_FRAME]
        while len(pairs) < SAMPLES_PER_FRAME:
            pairs.append(pairs[-1])
        return cls(
            tuple(f for f, _ in pairs),  # type: ignore[arg-type]
            tuple(s for _, s in pairs),  # type: ignore[arg-type]
        )

    @property
    def is_pause(self) -> bool:
        return self == ChannelWave.pause()

    def to_bytes(self) -> bytes:
        """轉成 8 個位元組：4 組頻率 + 4 組強度。"""
        return bytes((*self.frequencies, *self.strengths))

    def to_hex(self) -> str:
        """轉成 Socket 協定要的十六進位字串，例如 ``0A0A0A0A64646464``。"""
        return self.to_bytes().hex().upper()

    def describe(self) -> str:
        head = self.frequencies[0]
        return (
            f"頻率 {head}（{pulse_frequency_hz(head):.1f}Hz）"
            f" 波形強度 {self.strengths[0]}"
        )


# ------------------------------------------------------------------- 波形點


@dataclass(frozen=True)
class WaveFrame:
    """單一波形取樣點（25ms），是組出 :class:`ChannelWave` 的原料。"""

    frequency: int  # 裝置值 10 ~ 240
    strength: int  # 0 ~ 100

    def __post_init__(self) -> None:
        if not WAVE_FREQ_MIN <= int(self.frequency) <= WAVE_FREQ_MAX:
            raise ValueError(
                f"波形頻率 {self.frequency} 超出範圍 "
                f"({WAVE_FREQ_MIN} ~ {WAVE_FREQ_MAX})"
            )
        if not WAVE_STRENGTH_MIN <= int(self.strength) <= WAVE_STRENGTH_MAX:
            raise ValueError(
                f"波形強度 {self.strength} 超出範圍 "
                f"({WAVE_STRENGTH_MIN} ~ {WAVE_STRENGTH_MAX})"
            )

    @property
    def period_ms(self) -> int:
        """對應的輸出單元時長（ms）。"""
        return decode_wave_freq(self.frequency)

    @property
    def pulse_hz(self) -> float:
        """對應的脈衝頻率（Hz），僅供顯示。"""
        return pulse_frequency_hz(self.frequency)

    def describe(self) -> str:
        return (
            f"{self.pulse_hz:.1f}Hz (波形頻率 {self.period_ms}ms / 值 {self.frequency})"
            f" 強度 {self.strength}"
        )


def from_v2(x: int, y: int, z: int) -> WaveFrame:
    """把郊狼 2.0 的 ``(X, Y, Z)`` 波形參數換算成 V3 的取樣點。

    官方對應關係：

    - ``V3 波形頻率 = encode_wave_freq(X + Y)``
    - ``V3 波形強度 = Z * 5``（V2 的 20 約等於 V3 的 100）
    """
    return WaveFrame(
        frequency=encode_wave_freq(int(x) + int(y)),
        strength=clamp(int(z) * 5, WAVE_STRENGTH_MIN, WAVE_STRENGTH_MAX),
    )


def frame_from_input(input_value: int, strength: int) -> WaveFrame:
    """由「波形頻率輸入值 10~1000」與波形強度建立取樣點。"""
    return WaveFrame(
        frequency=encode_wave_freq(input_value),
        strength=clamp(int(strength), WAVE_STRENGTH_MIN, WAVE_STRENGTH_MAX),
    )


# --------------------------------------------------------------------- 預設集


def _breathe(frames: int = 20, low: int = 40, high: int = 100) -> list[WaveFrame]:
    """強度在 40 ~ 100 之間來回（一個循環 500ms）。"""
    out: list[WaveFrame] = []
    span = max(frames - 1, 1)
    for i in range(frames):
        t = i / span
        strength = low + (high - low) * (1 - abs(2 * t - 1))
        out.append(WaveFrame(WAVE_FREQ_MIN, clamp(int(round(strength)), 0, 100)))
    return out


def _pulse(on_samples: int = 4, off_samples: int = 4) -> list[WaveFrame]:
    """週期性開關輸出（預設 100ms 開、100ms 關）。"""
    return [WaveFrame(WAVE_FREQ_MIN, 100)] * on_samples + [
        WaveFrame(WAVE_FREQ_MIN, 0)
    ] * off_samples


def _ramp(frames: int = 20, low: int = 10, high: int = 100) -> list[WaveFrame]:
    """波形頻率由低到高緩緩上升後循環。"""
    out: list[WaveFrame] = []
    span = max(frames - 1, 1)
    for i in range(frames):
        value = low + (high - low) * i / span
        out.append(
            WaveFrame(clamp(int(round(value)), WAVE_FREQ_MIN, WAVE_FREQ_MAX), 100)
        )
    return out


#: 內建波形預設集。``custom`` 由 config.toml 的 ``[dglab.wave].custom_samples`` 提供。
PRESETS: dict[str, list[WaveFrame]] = {
    # 與原本 2.0 版本的 set_wave_sync(1, 9, 20, ...) 等效
    "steady": [from_v2(1, 9, 20)],
    # 與 2.0 的 (5, 95, 20) 等效：輸出單元 100ms，體感上是一下一下的
    "slow": [from_v2(5, 95, 20)],
    "breathe": _breathe(),
    "pulse": _pulse(),
    "ramp": _ramp(),
    # 官方 App 內建「呼吸」波形的完整循環（取自官方 example.md）
    "app_breathe": [
        WaveFrame(10, 0),
        WaveFrame(10, 20),
        WaveFrame(10, 40),
        WaveFrame(10, 60),
        WaveFrame(10, 80),
        WaveFrame(10, 100),
        WaveFrame(10, 100),
        WaveFrame(10, 100),
        WaveFrame(10, 0),
        WaveFrame(10, 0),
        WaveFrame(10, 0),
        WaveFrame(10, 0),
    ],
    # 官方 App 內建「潮汐」波形的完整循環（取自官方 example.md）
    "app_tide": [
        WaveFrame(freq, strength)
        for freq, strength in (
            (10, 0), (11, 16), (13, 33), (14, 50), (16, 66), (18, 83),
            (19, 100), (21, 92), (22, 84), (24, 76), (26, 68), (26, 0),
            (27, 16), (29, 33), (30, 50), (32, 66), (34, 83), (35, 100),
            (37, 92), (38, 84), (40, 76), (42, 68), (10, 0),
        )
    ],
}

#: 所有可用的預設集名稱（含 ``custom``）。
PRESET_NAMES: tuple[str, ...] = tuple(PRESETS) + ("custom",)


def resolve_preset(
    name: str, custom_samples: Iterable[Sequence[int]] | None = None
) -> list[WaveFrame]:
    """取得指定預設集的波形取樣點。

    Args:
        name: :data:`PRESET_NAMES` 之一。
        custom_samples: 當 ``name == "custom"`` 時使用的
            ``[[波形頻率ms, 波形強度], ...]``；波形頻率是 10 ~ 1000 的
            「輸出單元時長」，會自動套用官方壓縮轉換。

    Raises:
        ValueError: 名稱不存在，或 ``custom`` 沒有提供任何取樣點。
    """
    key = str(name).strip().lower()
    if key == "custom":
        frames = [
            frame_from_input(int(pair[0]), int(pair[1]))
            for pair in (custom_samples or [])
        ]
        if not frames:
            raise ValueError(
                "wave.preset = \"custom\" 時必須在 config.toml 提供 "
                "[dglab.wave].custom_samples = [[波形頻率ms, 波形強度], ...]"
            )
        return frames
    try:
        return list(PRESETS[key])
    except KeyError:
        raise ValueError(
            f"未知的波形預設集 {name!r}，可用選項：{', '.join(PRESET_NAMES)}"
        ) from None


class WaveCycle:
    """把一串波形取樣點變成「一直循環的 100ms 波形單位」。

    每個取樣點佔 25ms，所以一個波形單位（100ms）會消耗 4 個取樣點。
    """

    def __init__(self, frames: Iterable[WaveFrame]) -> None:
        self._frames: list[WaveFrame] = list(frames)
        self._cursor = 0

    def __bool__(self) -> bool:
        return bool(self._frames)

    @property
    def frames(self) -> list[WaveFrame]:
        return list(self._frames)

    def reset(self) -> None:
        self._cursor = 0

    @property
    def cursor(self) -> int:
        return self._cursor

    def next_wave(self) -> ChannelWave:
        """取出下一個 100ms 波形單位。沒有取樣點時回傳暫停波形。"""
        if not self._frames:
            return ChannelWave.pause()

        cursor = self._cursor
        samples = [
            self._frames[(cursor + offset) % len(self._frames)]
            for offset in range(SAMPLES_PER_FRAME)
        ]
        self._cursor = (cursor + SAMPLES_PER_FRAME) % len(self._frames)
        return ChannelWave.from_samples(
            (frame.frequency, frame.strength) for frame in samples
        )
