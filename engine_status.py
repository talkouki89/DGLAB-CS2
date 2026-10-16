"""引擎對外的狀態型別。

單獨放一個模組是為了避開循環匯入：:mod:`engine` 會產生這些狀態，
:mod:`presence` 會讀它們，兩邊都只依賴這裡。

:mod:`engine` 仍然把它們 re-export 出去（``from engine import EngineState``），
所以原本的寫法不用改。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from stats import SessionStats


class EngineState(str, Enum):
    """引擎狀態。"""

    IDLE = "idle"
    STARTING = "starting"
    WAITING_PAIR = "waiting_pair"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"


STATE_LABELS: dict[EngineState, str] = {
    EngineState.IDLE: "未啟動",
    EngineState.STARTING: "啟動中",
    EngineState.WAITING_PAIR: "等待配對",
    EngineState.RUNNING: "執行中",
    EngineState.PAUSED: "已暫停",
    EngineState.STOPPED: "已停止",
    EngineState.ERROR: "錯誤",
}


@dataclass(frozen=True)
class EngineStatus:
    """給 UI 看的即時狀態（不可變，整份原子替換）。"""

    state: EngineState = EngineState.IDLE
    message: str = ""
    paused: bool = False
    dry_run: bool = False
    socket_uri: str = ""
    gsi_address: str = ""
    client_id: str | None = None
    target_id: str | None = None
    paired: bool = False
    qr_url: str | None = None
    qr_matrix: tuple[tuple[bool, ...], ...] | None = None

    app_strength: tuple[int, int] | None = None
    app_limit: tuple[int, int] | None = None

    gsi_payloads: int = 0
    gsi_rejected: int = 0
    gsi_seconds_since: float | None = None

    health: int = 100
    burning: int = 0
    round_kills: int = 0
    health_known: bool = False
    #: 自己是不是已經死了、正在看別人的視角
    spectating: bool = False

    strength: int = 0
    stack: float = 0.0
    output_active: bool = False
    last_event: str = ""

    #: 目前連殺數
    streak: int = 0
    #: 本場統計的快照（唯讀；每個週期換一份新的）
    stats: SessionStats | None = None

    error: str | None = None

    @property
    def state_label(self) -> str:
        return STATE_LABELS.get(self.state, str(self.state))

    @property
    def cs2_connected(self) -> bool:
        """有沒有收到 CS2 的資料（最近 5 秒內）。"""
        if self.gsi_seconds_since is None:
            return False
        return self.gsi_seconds_since < 5.0
