"""把 CS2 的狀態變成「要送什麼給裝置」的決策。

CLI（:mod:`main`）與 GUI（:mod:`engine`）都共用這一層，避免兩邊各寫一套規則、
也不會出現「UI 版行為和命令列版不一樣」的情況。

這一層完全不碰網路與 tkinter，所以可以直接單元測試。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from config import AppConfig
from coyote.socket_protocol import Channel
from game_events import GameEventTracker, GameEvents, PlayerSnapshot
from stats import SessionStats
from strength import (
    MODE_HIT_ONLY,
    KillStreak,
    OutputGate,
    StrengthPolicy,
    StrengthStack,
)

if TYPE_CHECKING:  # pragma: no cover
    from coyote.socket_client import DglabSocketClient


@dataclass(frozen=True)
class ControlDecision:
    """這個週期應該對裝置做什麼。"""

    #: 目標通道強度（已套用強度上限，但還沒套用 App 端的上限）
    strength: int
    #: 是否要輸出波形（``hit_only`` 模式下平常是 False）
    output_active: bool
    #: 是否要「清空佇列 + 補一段爆發波形」（受擊同步電擊／電擊槍）
    fire_burst: bool
    #: 這個週期偵測到的事件
    events: GameEvents
    #: 目前的疊加值
    stack: float
    #: 受擊時間窗內的額外加成
    boost: float
    #: 是否為第一筆有效資料
    first_sample: bool = False
    #: 爆發波形要持續幾秒（``None`` = 用 ``hit_duration``）
    burst_seconds: float | None = None
    #: 是否正在電擊槍的加成時間窗內
    taser_active: bool = False
    #: 是否因為死亡太久而被強制歸零
    dead_stopped: bool = False
    #: 目前連殺數（連殺獎勵用）
    streak: int = 0
    #: 這一殺因為連殺獎勵而額外多扣的疊加值
    streak_bonus: float = 0.0

    @property
    def should_update_device(self) -> bool:
        return True


@dataclass
class PlayerController:
    """把連續的 GSI 快照轉成 :class:`ControlDecision`。

    內部持有事件追蹤器、強度疊加與輸出閘門，所以同一個實例要一直沿用。
    """

    config: AppConfig
    _tracker: GameEventTracker = field(default_factory=GameEventTracker, init=False)
    #: 這次死亡是什麼時候（``None`` = 目前活著）
    _died_at: float | None = field(default=None, init=False)
    #: 電擊槍加成到什麼時候為止
    _taser_until: float = field(default=0.0, init=False)
    #: 連殺追蹤（跨週期保留，所以不能每次 apply_config 都重建）
    streak: KillStreak = field(default_factory=KillStreak, init=False)
    #: 本場統計
    stats: SessionStats = field(default_factory=SessionStats, init=False)

    def __post_init__(self) -> None:
        self.apply_config(self.config)

    # ------------------------------------------------------------------ 設定

    def apply_config(self, config: AppConfig) -> None:
        """熱套用設定（不會重置疊加值與連殺數）。"""
        self.config = config
        self.policy: StrengthPolicy = config.dglab.policy
        self.stack: StrengthStack = config.dglab.stack
        self.gate: OutputGate = config.dglab.output
        self.taser = config.dglab.taser
        self.death_stop = config.dglab.death_stop
        self.max_strength: int = int(config.dglab.max_strength)

        # 連殺的參數可以直接換，但「目前連殺數」要留著
        streak_config = config.dglab.streak
        self.streak.enabled = bool(streak_config.enabled)
        self.streak.starts_at = int(streak_config.starts_at)
        self.streak.bonus_per_kill = float(streak_config.bonus_per_kill)
        self.streak.max_bonus = float(streak_config.max_bonus)

    # ------------------------------------------------------------------ 狀態

    def reset_tracking(self) -> None:
        """忘掉事件基準點（重新配對、資料中斷之後呼叫）。"""
        self._tracker.reset()
        self._died_at = None
        self._taser_until = 0.0

    def reset_stack(self) -> None:
        """把疊加值與連殺數歸零。"""
        self.stack.reset()
        self.streak.reset()

    def reset_stats(self) -> None:
        """把本場統計歸零（但不動遊戲進行中的疊加值）。"""
        self.stats.reset()

    @property
    def previous_snapshot(self) -> PlayerSnapshot | None:
        return self._tracker.previous

    # ------------------------------------------------------------------ 決策

    def update(
        self, snapshot: PlayerSnapshot, now: float
    ) -> ControlDecision | None:
        """算出這個週期要做什麼。

        Returns:
            ``None`` 代表「血量不可信，這個週期不要動裝置」
            （例如只收到 CS2 的 heartbeat）。
        """
        events = self._tracker.update(snapshot)
        if not snapshot.health_known:
            return None

        if events.died:
            self.stack.apply_death()
            self.gate.on_death(now)
            self.streak.register_death()
        elif events.damage > 0:
            self.gate.on_hit(now, damage=events.damage)
        streak_bonus = 0.0
        if events.kills:
            # 連殺獎勵：連續擊殺時，這一殺扣掉的疊加值會越來越多
            streak_bonus = self.streak.register_kills(events.kills)
            self.stack.apply_kills(events.kills, extra=streak_bonus)

        # --- 電擊槍：單次掉血達到門檻就啟動一段加成時間窗 ---
        taser_hit = (
            self.taser.enabled
            and self.taser.duration > 0
            and self.taser.min_damage > 0
            and events.damage >= self.taser.min_damage
        )
        if taser_hit:
            self._taser_until = float(now) + float(self.taser.duration)
        taser_active = float(now) < self._taser_until

        # --- 死亡後歸零：記下死亡時間，撐過設定的秒數就強制歸零 ---
        if snapshot.health <= 0:
            if self._died_at is None:
                self._died_at = float(now)
        else:
            self._died_at = None
        dead_stopped = (
            self.death_stop.enabled
            and self._died_at is not None
            and (float(now) - self._died_at) >= float(self.death_stop.delay)
        )

        boost = self.gate.boost(now)
        target = self.policy.target(
            snapshot.health, snapshot.burning, self.stack.offset() + boost
        )
        strength = min(target, self.max_strength)
        output_active = self.gate.is_active(now)

        burst_seconds: float | None = None
        if self.gate.mode == MODE_HIT_ONLY and (events.damage > 0 or events.died):
            burst_seconds = self.gate.hit_duration

        if taser_active:
            # 電擊槍只會讓感覺更強，不會把原本更高的強度壓下來
            strength = min(max(strength, self.taser.strength), self.max_strength)
            output_active = True
            if taser_hit:
                burst_seconds = self.taser.duration

        if dead_stopped:
            # 死亡歸零優先於一切（人也死了，不該再有電擊槍加成）
            strength = 0
            output_active = False
            burst_seconds = None

        self.stats.record(
            now,
            strength=strength,
            output_active=output_active,
            died=events.died,
            kills=events.kills,
            damage=events.damage,
            taser_hit=taser_hit,
            streak=self.streak.count,
        )

        return ControlDecision(
            strength=strength,
            output_active=output_active,
            fire_burst=burst_seconds is not None,
            events=events,
            stack=self.stack.offset(),
            boost=boost,
            first_sample=events.first_sample,
            burst_seconds=burst_seconds,
            taser_active=taser_active,
            dead_stopped=dead_stopped,
            streak=self.streak.count,
            streak_bonus=streak_bonus,
        )

    def describe(self, snapshot: PlayerSnapshot, decision: ControlDecision) -> str:
        """產生一行人看得懂的說明，給記錄用。"""
        parts = []
        if decision.events.died:
            parts.append("陣亡")
        elif decision.events.damage > 0:
            parts.append(f"受擊 -{decision.events.damage}")
        if decision.events.respawned:
            parts.append("復活")
        if decision.events.kills:
            parts.append(f"擊殺 +{decision.events.kills}")
            if decision.streak_bonus:
                parts.append(f"連殺 x{decision.streak} +{decision.streak_bonus:.0f}")
            elif decision.streak > 1:
                parts.append(f"連殺 x{decision.streak}")
        if decision.taser_active:
            parts.append(f"電擊槍 {self.taser.strength}")
        if decision.dead_stopped:
            parts.append(f"死亡 {self.death_stop.delay:g} 秒歸零")
        head = "、".join(parts)
        detail = (
            f"血量 {snapshot.health} 燃燒 {snapshot.burning} "
            f"疊加 {decision.stack:+.1f}"
        )
        if decision.boost:
            detail += f" 受擊加成 +{decision.boost:.0f}"
        detail += f" → 強度 {decision.strength}"
        return f"{head}：{detail}" if head else detail


@dataclass
class DeviceWriter:
    """把 :class:`ControlDecision` 實際送給 DG-LAB App。

    負責兩件容易寫錯的事：

    - 每個通道各自的強度上限（本程式的 ``max_strength`` 與 App 上的上限取小）。
    - 記住「上次送出的值」，只有變化時才送指令，不要每 0.25 秒都重送一次。
    """

    client: "DglabSocketClient"
    config: AppConfig
    _last_sent: dict[Channel, int] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.apply_config(self.config)

    def apply_config(self, config: AppConfig) -> None:
        self.config = config
        dglab = config.dglab
        self.channels: list[Channel] = sorted(dglab.channels, key=int)
        self.max_strength = int(dglab.max_strength)
        self.respect_app_limit = bool(dglab.respect_app_limit)
        self.hit_duration = float(dglab.output.hit_duration)

    @property
    def last_sent(self) -> dict[Channel, int]:
        return dict(self._last_sent)

    def forget(self) -> None:
        """忘掉「上次送出的值」，讓下一個週期重新送一次。

        暫停後恢復、或重新配對之後一定要呼叫，否則值沒變就不會補送，
        裝置會停在歸零的狀態。
        """
        self._last_sent.clear()

    def limit_for(self, channel: Channel) -> int:
        """該通道實際可用的強度上限。"""
        if not self.respect_app_limit:
            return self.max_strength
        return self.client.effective_limit(channel, self.max_strength)

    async def reset_output(self) -> None:
        """所有通道歸零並停止輸出。"""
        for channel in self.channels:
            self.client.set_output_active(channel, False)
            if self._last_sent.get(channel) != 0:
                await self.client.set_strength(channel, 0)
                self._last_sent[channel] = 0

    async def apply(self, decision: ControlDecision) -> list[tuple[Channel, int]]:
        """套用決策；回傳這個週期真正有更新的 ``(通道, 強度)``。"""
        changes: list[tuple[Channel, int]] = []
        for channel in self.channels:
            value = min(decision.strength, self.limit_for(channel))
            self.client.set_output_active(channel, decision.output_active)
            if self._last_sent.get(channel) != value:
                await self.client.set_strength(channel, value)
                self._last_sent[channel] = value
                changes.append((channel, value))

        if decision.fire_burst:
            seconds = decision.burst_seconds or self.hit_duration
            for channel in self.channels:
                await self.client.fire_burst(channel, seconds)
        return changes
