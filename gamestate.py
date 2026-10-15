import map
import player
import logging


class GameStateManager:
    def __init__(self):
        self.gamestate = GameState()


class GameState:
    def __init__(self):
        self.map = map.Map()
        self.player = player.Player()
        self.round_phase = ""
        #: ``provider.steamid`` —— 執行遊戲的那個人（也就是「我」）的 SteamID。
        #:
        #: 死亡觀戰時 GSI 的 ``player`` 區塊會變成「被觀戰那個人」的資料，
        #: 靠這個值和 ``player.steamid`` 比對才能分辨那包是不是自己的。
        self.provider_steamid = ""

    def update_round_phase(self, phase):
        self.round_phase = phase
        logging.info("Round phase: " + phase)

    def update_round_kills(self, kills):
        # 注意：這裡一定要把 0 寫回去。早期版本寫成「kills != 0 才更新」，
        # 導致每回合重置時值留在上一回合的數字，下一回合第一次擊殺的差值
        # 就變成負的、被當成沒有擊殺（擊殺漏接的元凶之一）。
        if self.player.state.round_kills != kills:
            self.player.state.round_kills = kills
