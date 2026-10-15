"""把 CS2 的狀態換算成郊狼的強度與輸出時機。

這裡刻意寫成不依賴任何其他模組的純函式，方便單元測試。三個部分：

- :class:`StrengthPolicy`：血量／燃燒 → 基礎強度
- :class:`StrengthStack`：死亡累加、擊殺遞減的「強度疊加」
- :class:`OutputGate`：什麼時候要出力（連續輸出 vs 只有受擊時輸出）

.. note::
   原版 ``main.py`` / ``main_v3.py`` 的血量邏輯有嚴重的 bug：

   .. code-block:: python

       if hp[0] == 100 and not keep_strength_while_not_injured:
           await dglab_instance.set_strength_sync(0, 0)
       elif hp[0] == 100 and keep_strength_while_not_injured:
           await dglab_instance.set_strength_sync(BASE_STRENGTH + strength_offset, 0)

   兩個分支都要求 ``hp[0] == 100``，所以只要玩家受傷就完全不會更新強度，
   核心功能「強度隨血量變化」根本不會生效。這裡已修正成：只有「滿血且設定為
   不電擊」時才歸零，其餘情況一律套用血量偏移。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: CS2 滿血血量
FULL_HEALTH = 100

#: CS2 的 burning 上限（0 ~ 255）
BURNING_MAX = 255

#: 郊狼通道強度上限
STRENGTH_MAX = 200


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


# --------------------------------------------------------------- 血量 → 強度


@dataclass(frozen=True)
class StrengthPolicy:
    """血量 → 強度 的換算規則。"""

    #: 基礎強度（滿血時，或受傷後再加成的起點）
    base_strength: float = 20.0
    #: 每掉一滴血增加多少強度
    strength_per_hp: float = 0.2
    #: 受燃燒彈傷害時的額外加成倍率（0.2 = 最多 +20%）
    burning_multiplier: float = 0.2
    #: 滿血時是否仍然維持基礎電擊
    keep_strength_while_not_injured: bool = True
    #: 硬性上限
    max_strength: float = 80.0
    #: 滿血血量
    full_health: int = FULL_HEALTH

    def __post_init__(self) -> None:
        if self.base_strength < 0:
            raise ValueError("base_strength 不可為負")
        if self.strength_per_hp < 0:
            raise ValueError("strength_per_hp 不可為負")
        if self.burning_multiplier < 0:
            raise ValueError("burning_multiplier 不可為負")
        if not 0 <= self.max_strength <= STRENGTH_MAX:
            raise ValueError(f"max_strength 必須介於 0 ~ {STRENGTH_MAX}")
        if self.base_strength > self.max_strength:
            raise ValueError(
                f"base_strength ({self.base_strength}) 不可大於 "
                f"max_strength ({self.max_strength})"
            )

    def raw_target(self, health: int, burning: int = 0) -> float:
        """未加上疊加、也還沒取整的強度。"""
        health = int(_clamp(float(health), 0.0, float(self.full_health)))
        burning = int(_clamp(float(burning), 0.0, float(BURNING_MAX)))

        if health >= self.full_health and not self.keep_strength_while_not_injured:
            return 0.0

        hp_offset = (self.full_health - health) * self.strength_per_hp
        burning_scale = 1.0 + self.burning_multiplier * (burning / BURNING_MAX)
        return self.base_strength + hp_offset * burning_scale

    def target(
        self, health: int, burning: int = 0, stack: float = 0.0
    ) -> int:
        """算出該套用的通道強度（整數，0 ~ ``max_strength``）。

        Args:
            health: 目前血量；CS2 的範圍是 0 ~ 100。
            burning: 燃燒狀態；CS2 的範圍是 0 ~ 255，越大代表燒得越久。
            stack: 死亡／擊殺累積出來的強度疊加值。
        """
        value = self.raw_target(health, burning) + float(stack)
        return int(round(_clamp(value, 0.0, float(self.max_strength))))

    def explain(self, health: int, burning: int = 0, stack: float = 0.0) -> str:
        """產生一句人類可讀的說明，方便記錄。"""
        health = int(_clamp(float(health), 0.0, float(self.full_health)))
        burning = int(_clamp(float(burning), 0.0, float(BURNING_MAX)))
        base = self.raw_target(health, burning)
        parts = [f"基礎 {base:.1f}"]
        if stack:
            parts.append(f"疊加 {stack:+.1f}")
        total = self.target(health, burning, stack)
        if health >= self.full_health and not self.keep_strength_while_not_injured:
            return "滿血且設定為滿血不電擊 → 0"
        return (
            f"血量 {health}（-{self.full_health - health}）燃燒 {burning} → "
            + "、".join(parts)
            + f" = {total}"
        )


# ----------------------------------------------------------------- 強度疊加


@dataclass
class StrengthStack:
    """死亡會累加强度、擊殺會扣回強度的「疊加值」。

    對應功能列表裡的「強度疊加，死亡時增加強度，擊殺降低強度」。
    值會被夾在 ``minimum`` ~ ``maximum`` 之間。
    """

    enabled: bool = True
    #: 每次死亡增加多少強度
    death_gain: float = 5.0
    #: 每次擊殺降低多少強度
    kill_reduction: float = 2.0
    minimum: float = 0.0
    maximum: float = 30.0
    value: float = 0.0

    def __post_init__(self) -> None:
        if self.death_gain < 0:
            raise ValueError("stack.death_gain 不可為負")
        if self.kill_reduction < 0:
            raise ValueError("stack.kill_reduction 不可為負")
        if self.minimum > self.maximum:
            raise ValueError("stack.minimum 不可大於 stack.maximum")
        self.value = self._clamped(self.value)

    def _clamped(self, value: float) -> float:
        return _clamp(float(value), float(self.minimum), float(self.maximum))

    def apply_death(self, count: int = 1) -> float:
        """死亡時呼叫；回傳更新後的疊加值。"""
        if not self.enabled:
            return 0.0
        self.value = self._clamped(self.value + self.death_gain * max(0, int(count)))
        return self.value

    def apply_kills(self, count: int = 1) -> float:
        """擊殺時呼叫；回傳更新後的疊加值。"""
        if not self.enabled:
            return 0.0
        self.value = self._clamped(
            self.value - self.kill_reduction * max(0, int(count))
        )
        return self.value

    def offset(self) -> float:
        """目前要套用到強度上的偏移量。"""
        return self.value if self.enabled else 0.0

    def reset(self) -> None:
        self.value = 0.0


# --------------------------------------------------------------- 輸出時機


MODE_CONTINUOUS = "continuous"
MODE_HIT_ONLY = "hit_only"
OUTPUT_MODES: tuple[str, ...] = (MODE_CONTINUOUS, MODE_HIT_ONLY)


@dataclass
class OutputGate:
    """決定什麼時候要輸出波形。

    - ``continuous``：一直輸出（原版行為），強度隨血量變化。
    - ``hit_only``：平常完全不出力，只有受擊後 ``hit_duration`` 秒內輸出，
      對應功能列表裡的「受擊時同步電擊，不受擊不電擊」。

    ``hit_boost`` 則是在兩種模式下都有效：受擊後的時間窗內額外加上這麼多強度，
    讓「被打到」的感覺更明顯。
    """

    mode: str = MODE_CONTINUOUS
    #: 受擊後持續輸出幾秒（只有 hit_only 模式會影響是否出力）
    hit_duration: float = 0.35
    #: 受擊時間窗內額外增加的強度
    hit_boost: float = 0.0

    _until: float = field(default=0.0, init=False, repr=False)
    hit_count: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        mode = str(self.mode).strip().lower()
        if mode not in OUTPUT_MODES:
            raise ValueError(
                f"output.mode 必須是 {', '.join(OUTPUT_MODES)} 其中之一，目前是 {self.mode!r}"
            )
        self.mode = mode
        if self.hit_duration < 0:
            raise ValueError("output.hit_duration 不可為負")
        if self.hit_boost < 0:
            raise ValueError("output.hit_boost 不可為負")

    def on_hit(self, now: float, *, damage: int = 0) -> None:
        """記錄一次受擊（順便讓連續兩次受擊的時間窗往後延）。"""
        self.hit_count += 1
        self._until = float(now) + float(self.hit_duration)

    def on_death(self, now: float) -> None:
        """死亡也當成一次受擊，確保 hit_only 模式下陣亡會有明確的一下。"""
        self.on_hit(now)

    def is_active(self, now: float) -> bool:
        """現在是否應該輸出波形。"""
        if self.mode == MODE_CONTINUOUS:
            return True
        return float(now) < self._until

    def boost(self, now: float) -> float:
        """受擊時間窗內要額外加上的強度。"""
        if self.hit_boost <= 0:
            return 0.0
        return self.hit_boost if float(now) < self._until else 0.0

    def remaining(self, now: float) -> float:
        return max(0.0, self._until - float(now))

    def reset(self) -> None:
        self._until = 0.0
        self.hit_count = 0
