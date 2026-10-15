"""從 CS2 的 GSI 狀態變化推斷遊戲事件（受擊／死亡／擊殺／復活）。

因為 GSI 只會一直送「目前狀態」，不會送事件，所以要在這裡比較前後兩次快照，
算出中間發生了什麼事。這支模組是純函式，方便單元測試。

.. important::

   **死亡之後觀戰別人時，GSI 的 ``player`` 區塊會變成「被觀戰那個人」的資料**
   ——生命值、每回合擊殺、武器、金錢全部都是他的。

   不處理的話會出現兩個症狀：

   1. 血量從 0 跳成被觀戰者的血量 → 被誤判成「我復活了」。
   2. 被觀戰者的擊殺數變成我們的基準點 → 復活後自己拿到擊殺時算出來的差值
      不合理 → 擊殺被漏掉。

   分辨方法是比對 ``player.steamid`` 與 ``provider.steamid``（執行遊戲的人，
   也就是自己）：兩者不同就代表那一包不是自己的資料。這時只更新「我死了」，
   其餘一律不採用，並把基準點作廢。

   擊殺偵測另外改用 ``match_stats.kills``（整場累計、單調遞增），
   而不是每回合會歸零的 ``round_kills``；後者只要在回合交界出錯就會漏掉擊殺。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 單次取樣最多可能發生的擊殺數。
#:
#: 超過這個數字代表不是「這段時間真的連殺」，而是基準點跑掉了
#: （例如中途加入、換地圖、資料中斷後重來），直接重新同步、不發事件。
MAX_KILLS_PER_TICK = 5


def payload_is_mine(
    steamid: str, provider_steamid: str, spectarget: str = ""
) -> bool:
    """判斷一包 GSI 資料的 ``player`` 區塊是不是「我自己」的。

    死亡之後開始觀戰時，CS2 會把 ``player`` 換成被觀戰那個人的資料
    （生命值、每回合擊殺、武器、金錢全部都是他的）。

    判斷方式是比對 ``player.steamid`` 與 ``provider.steamid``
    ——``provider.steamid`` 是執行遊戲的人，永遠是自己。
    """
    if provider_steamid and steamid:
        return steamid == provider_steamid
    # 拿不到 provider.steamid 時的備援（例如自己手寫的 cfg 沒有 provider）：
    # spectarget 有值就代表正在看別人。
    return not spectarget


@dataclass(frozen=True)
class PlayerSnapshot:
    """一次 GSI 快照中和遊戲事件有關的欄位。"""

    #: 自己的血量。觀戰別人時仍然是「自己最後一次已知的血量」，不會變成別人的。
    health: int = 100
    burning: int = 0
    round_kills: int = 0
    #: 整場累計擊殺數（比 round_kills 可靠）
    match_kills: int = 0
    match_kills_known: bool = False
    health_known: bool = False
    round_phase: str = ""
    #: 這一包裡 ``player.steamid``（觀戰時會是被觀戰者）
    steamid: str = ""
    #: 這一包裡 ``provider.steamid``（永遠是自己）
    provider_steamid: str = ""
    #: 這一包裡 ``player.spectarget``（旁觀者模式才有）
    spectarget: str = ""
    #: 目前是不是在看別人的資料（自己已經死了）
    spectating: bool = False

    @property
    def is_mine(self) -> bool:
        """這一包的 ``player`` 區塊是不是「我自己」的資料。"""
        return payload_is_mine(self.steamid, self.provider_steamid, self.spectarget)

    @property
    def alive(self) -> bool:
        return self.health > 0


@dataclass(frozen=True)
class GameEvents:
    """兩次快照之間發生的事件。"""

    #: 這次總共掉了幾滴血（死亡也算在內）
    damage: int = 0
    #: 是否在這次快照中死亡
    died: bool = False
    #: 這次新增了幾個擊殺
    kills: int = 0
    #: 是否從死亡狀態復活
    respawned: bool = False
    #: 是否為第一筆有效資料（不應該被當成事件）
    first_sample: bool = False

    @property
    def has_event(self) -> bool:
        return bool(self.damage or self.died or self.kills or self.respawned)


class GameEventTracker:
    """比較連續的 :class:`PlayerSnapshot`，產生 :class:`GameEvents`。

    同一個實例要一直沿用；它內部保有基準點與「目前是不是死的」的閂鎖。
    """

    def __init__(self) -> None:
        self._previous: PlayerSnapshot | None = None
        #: 死亡閂鎖。一旦死了就維持 True，直到確定復活。
        #: 這樣才不會被觀戰時的血量騙到。
        self._dead = False
        self._kills_baseline: int | None = None

    @property
    def previous(self) -> PlayerSnapshot | None:
        return self._previous

    @property
    def dead(self) -> bool:
        return self._dead

    def reset(self) -> None:
        """忘掉基準點（例如遊戲資料中斷之後）。"""
        self._previous = None
        self._dead = False
        self._kills_baseline = None

    def update(self, snapshot: PlayerSnapshot) -> GameEvents:
        """餵入最新的快照並取得事件。"""
        if not snapshot.health_known:
            # 只有 heartbeat（沒有 player_state）時血量不可信，
            # 直接重置基準點，免得拿舊資料去比較而誤判成受擊。
            self._previous = None
            return GameEvents()

        if not snapshot.is_mine:
            # 這包是「被觀戰的人」的資料。我們唯一能確定的是：自己死了。
            previous = self._previous
            died = not self._dead and previous is not None and previous.health > 0
            self._dead = True
            self._previous = None
            self._kills_baseline = None
            return GameEvents(died=died)

        previous = self._previous
        self._previous = snapshot

        if self._dead:
            # 死亡閂鎖中：只有在「確定是自己的資料、而且血量回來了」才解除。
            # 注意這裡不看觀戰時的血量，所以不會再被誤判成復活。
            if snapshot.health > 0:
                self._dead = False
                self._kills_baseline = (
                    snapshot.match_kills if snapshot.match_kills_known else None
                )
                return GameEvents(respawned=True)
            return GameEvents()

        if previous is None:
            # 第一筆資料：如果一開始就已經死了（例如程式在死亡後才啟動），
            # 也要把閂鎖設起來，之後血量回來才認得出是復活。
            self._dead = snapshot.health <= 0
            self._kills_baseline = (
                snapshot.match_kills if snapshot.match_kills_known else None
            )
            return GameEvents(first_sample=True)

        damage = max(0, previous.health - snapshot.health)
        died = snapshot.health == 0 and previous.health > 0
        if died:
            self._dead = True

        return GameEvents(
            damage=damage,
            died=died,
            kills=self._kill_delta(previous, snapshot),
        )

    def _kill_delta(self, previous: PlayerSnapshot, snapshot: PlayerSnapshot) -> int:
        """算出這次新增了幾個擊殺。"""
        if snapshot.match_kills_known:
            if self._kills_baseline is None:
                self._kills_baseline = snapshot.match_kills
                return 0
            delta = snapshot.match_kills - self._kills_baseline
            self._kills_baseline = snapshot.match_kills
            # 負的差值代表換場／重新開始；太大的差值代表基準點跑掉，
            # 兩種都只重新同步、不當成擊殺。
            if 0 < delta <= MAX_KILLS_PER_TICK:
                return delta
            return 0

        # 沒有 match_stats 時退回每回合擊殺數（round_kills 會歸零，所以夾在 0 以上）
        return max(0, min(snapshot.round_kills - previous.round_kills, MAX_KILLS_PER_TICK))
