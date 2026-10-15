class Player:
    def __init__(self):
        self.steamid = ""
        self.name = ""
        self.observer_slot = -1
        self.activity = ""
        #: 正在觀戰誰的 SteamID（旁觀者模式才會出現；空字串 = 沒有）
        self.spectarget = ""
        self.state = State()
        self.weapons = {}
        self.match_stats = MatchStats()


class State:
    def __init__(self):
        # 預設滿血，避免在還沒收到任何 GSI 資料前就被當成「死亡」而拉到最高強度
        self.health = 100
        #: 是否真的從 GSI 收到過血量；heartbeat 之類的封包不含 player_state
        self.health_known = False
        self.armor = 0
        self.helmet = False
        self.flashed = 0
        self.smoked = 0
        self.burning = 0
        self.money = 0
        self.round_kills = 0
        self.round_killhs = 0
        self.round_totaldmg = 0
        self.equip_value = 0


class MatchStats:
    def __init__(self):
        self.kills = 0
        #: 是否真的收到過比賽累計擊殺數（拿來判斷能不能用 match_stats 算擊殺）
        self.kills_known = False
        self.assists = 0
        self.deaths = 0
        self.mvps = 0
        self.score = 0
