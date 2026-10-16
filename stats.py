"""本場統計。

累積「這一場玩下來發生了什麼」：死亡、擊殺、受擊、最高／平均強度、
實際通電時間、最佳連殺……讓玩家打完可以回頭看自己有多慘。

純資料處理，不碰網路與 UI，所以可以直接單元測試。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 兩次取樣之間最多算幾秒。GSI 中斷之後第一筆資料的間隔可能很大，
#: 不設上限的話「通電時間」會一次跳掉好幾分鐘。
MAX_STEP_SECONDS = 2.0


def format_duration(seconds: float) -> str:
    """把秒數變成 ``12:34`` 或 ``1:02:03``。"""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


@dataclass
class SessionStats:
    """一場遊戲的累計數據。

    由 :class:`controller.PlayerController` 在每個週期呼叫 :meth:`record`；
    這裡只做累加與換算，不決定任何電擊行為。
    """

    enabled: bool = True
    #: 第一次收到有效資料的時間（``None`` = 還沒開始）
    started_at: float | None = None

    deaths: int = 0
    kills: int = 0
    #: 受擊次數（含致死的那一下）
    hits: int = 0
    #: 累計承受的傷害
    damage_taken: int = 0
    #: 觸發電擊槍加成的次數
    taser_hits: int = 0

    #: 目前連殺數
    current_streak: int = 0
    #: 本場最佳連殺
    best_streak: int = 0

    #: 出現過的最高強度
    max_strength: int = 0

    #: 累計通電時間（秒）
    shock_seconds: float = 0.0
    #: 這組統計涵蓋的時間（秒）
    elapsed_seconds: float = 0.0

    _strength_sum: float = field(default=0.0, repr=False)
    _strength_samples: int = field(default=0, repr=False)
    _last_update: float | None = field(default=None, repr=False)

    # ------------------------------------------------------------------ 累計

    def record(
        self,
        now: float,
        *,
        strength: int = 0,
        output_active: bool = False,
        died: bool = False,
        kills: int = 0,
        damage: int = 0,
        taser_hit: bool = False,
        streak: int = 0,
    ) -> None:
        """累加一個週期的數據。"""
        if not self.enabled:
            return
        now = float(now)
        if self.started_at is None:
            self.started_at = now
            self._last_update = now
        if self._last_update is not None:
            step = now - self._last_update
            # 時間倒流（換了時鐘來源）就當成沒經過時間，不要汙染統計
            if step > 0:
                step = min(step, MAX_STEP_SECONDS)
                self.elapsed_seconds += step
                if output_active:
                    self.shock_seconds += step
        self._last_update = now

        self.max_strength = max(self.max_strength, int(strength))
        self._strength_sum += float(strength)
        self._strength_samples += 1

        if damage > 0:
            self.hits += 1
            self.damage_taken += int(damage)
        if died:
            self.deaths += 1
        if kills:
            self.kills += int(kills)
        if taser_hit:
            self.taser_hits += 1
        self.current_streak = max(0, int(streak))
        self.best_streak = max(self.best_streak, self.current_streak)

    # ------------------------------------------------------------------ 查詢

    @property
    def average_strength(self) -> float:
        """平均強度（含沒有出力的時候）。"""
        if not self._strength_samples:
            return 0.0
        return self._strength_sum / self._strength_samples

    @property
    def shock_ratio(self) -> float:
        """通電時間佔比（0 ~ 1）。"""
        if self.elapsed_seconds <= 0:
            return 0.0
        return min(1.0, self.shock_seconds / self.elapsed_seconds)

    @property
    def kill_death_ratio(self) -> float:
        """擊殺／死亡比。沒有死亡時等於擊殺數。"""
        if self.deaths <= 0:
            return float(self.kills)
        return self.kills / self.deaths

    @property
    def duration_label(self) -> str:
        return format_duration(self.elapsed_seconds)

    @property
    def has_data(self) -> bool:
        return self._strength_samples > 0

    def summary(self) -> list[tuple[str, str]]:
        """回傳 ``[(標籤, 值), ...]``，給介面直接排成兩欄。"""
        return [
            ("時間", self.duration_label),
            ("擊殺 / 死亡", f"{self.kills} / {self.deaths}"),
            ("K/D", f"{self.kill_death_ratio:.2f}"),
            ("最佳連殺", str(self.best_streak)),
            ("受擊次數", str(self.hits)),
            ("承受傷害", str(self.damage_taken)),
            ("電擊槍觸發", str(self.taser_hits)),
            ("最高強度", str(self.max_strength)),
            ("平均強度", f"{self.average_strength:.1f}"),
            ("通電時間", f"{format_duration(self.shock_seconds)}（{self.shock_ratio:.0%}）"),
        ]

    def reset(self) -> None:
        self.started_at = None
        self.deaths = 0
        self.kills = 0
        self.hits = 0
        self.damage_taken = 0
        self.taser_hits = 0
        self.current_streak = 0
        self.best_streak = 0
        self.max_strength = 0
        self.shock_seconds = 0.0
        self.elapsed_seconds = 0.0
        self._strength_sum = 0.0
        self._strength_samples = 0
        self._last_update = None
