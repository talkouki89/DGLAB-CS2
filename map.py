class Map:
    def __init__(self):
        self.mode = ""
        self.name = ""
        self.phase = ""
        self.round = 0
        self.team_ct = Team()
        self.team_t = Team()
        self.num_matches_to_win_series = 0
        self.current_spectators = 0
        self.souvenirs_total = 0


class Team:
    def __init__(self):
        self.score = 0
        self.timeouts_remaining = 0
        self.matches_won_this_series = 0
