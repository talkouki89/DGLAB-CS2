"""CS2 遊戲事件偵測的測試。"""

from __future__ import annotations

import unittest

from game_events import MAX_KILLS_PER_TICK, GameEventTracker, PlayerSnapshot

#: 測試用的 SteamID
ME = "76561197990087679"
OTHER = "76561198000000001"


def snap(
    health: int,
    kills: int = 0,
    burning: int = 0,
    known: bool = True,
    *,
    steamid: str = ME,
    provider: str = ME,
    spectarget: str = "",
    match_kills: int | None = None,
) -> PlayerSnapshot:
    """建立快照。

    預設是「自己的資料、沒有 match_stats」，所以既有的測試行為不變。
    """
    return PlayerSnapshot(
        health=health,
        burning=burning,
        round_kills=kills,
        match_kills=match_kills if match_kills is not None else 0,
        match_kills_known=match_kills is not None,
        health_known=known,
        steamid=steamid,
        provider_steamid=provider,
        spectarget=spectarget,
    )


class FirstSampleTests(unittest.TestCase):
    def test_first_valid_sample_is_not_an_event(self):
        tracker = GameEventTracker()
        events = tracker.update(snap(100))
        self.assertTrue(events.first_sample)
        self.assertFalse(events.has_event)

    def test_unknown_health_is_ignored(self):
        tracker = GameEventTracker()
        events = tracker.update(snap(0, known=False))
        self.assertFalse(events.first_sample)
        self.assertFalse(events.has_event)
        self.assertIsNone(tracker.previous)

    def test_heartbeat_resets_the_baseline(self):
        tracker = GameEventTracker()
        tracker.update(snap(100))
        tracker.update(snap(0, known=False))
        # 基準點被清掉，所以下一筆有效資料會被當成第一筆，而不是「掉了 100 血」
        events = tracker.update(snap(100))
        self.assertTrue(events.first_sample)
        self.assertEqual(events.damage, 0)


class DamageTests(unittest.TestCase):
    def test_damage_is_detected(self):
        tracker = GameEventTracker()
        tracker.update(snap(100))
        events = tracker.update(snap(87))
        self.assertEqual(events.damage, 13)
        self.assertFalse(events.died)
        self.assertEqual(events.kills, 0)

    def test_healing_is_not_damage(self):
        tracker = GameEventTracker()
        tracker.update(snap(50))
        events = tracker.update(snap(80))
        self.assertEqual(events.damage, 0)
        self.assertFalse(events.respawned)

    def test_no_change_is_no_event(self):
        tracker = GameEventTracker()
        tracker.update(snap(70))
        self.assertFalse(tracker.update(snap(70)).has_event)

    def test_damage_accumulates_over_multiple_snapshots(self):
        tracker = GameEventTracker()
        tracker.update(snap(100))
        self.assertEqual(tracker.update(snap(90)).damage, 10)
        self.assertEqual(tracker.update(snap(85)).damage, 5)

    def test_burning_is_carried_through(self):
        tracker = GameEventTracker()
        tracker.update(snap(100))
        events = tracker.update(snap(80, burning=200))
        self.assertEqual(events.damage, 20)


class DeathTests(unittest.TestCase):
    def test_death_is_detected(self):
        tracker = GameEventTracker()
        tracker.update(snap(30))
        events = tracker.update(snap(0))
        self.assertTrue(events.died)
        self.assertEqual(events.damage, 30)
        self.assertFalse(events.respawned)

    def test_staying_dead_is_not_repeated(self):
        tracker = GameEventTracker()
        tracker.update(snap(0))
        events = tracker.update(snap(0))
        self.assertFalse(events.died)
        self.assertFalse(events.has_event)

    def test_respawn_is_detected(self):
        tracker = GameEventTracker()
        tracker.update(snap(0))
        events = tracker.update(snap(100))
        self.assertTrue(events.respawned)
        self.assertFalse(events.died)
        self.assertEqual(events.damage, 0)

    def test_death_then_respawn_sequence(self):
        tracker = GameEventTracker()
        tracker.update(snap(100, kills=0))
        self.assertTrue(tracker.update(snap(0)).died)
        self.assertTrue(tracker.update(snap(100)).respawned)


class KillTests(unittest.TestCase):
    def test_kill_is_detected(self):
        tracker = GameEventTracker()
        tracker.update(snap(100, kills=0))
        events = tracker.update(snap(100, kills=1))
        self.assertEqual(events.kills, 1)
        self.assertFalse(events.damage)

    def test_multiple_kills_at_once(self):
        tracker = GameEventTracker()
        tracker.update(snap(100, kills=0))
        events = tracker.update(snap(100, kills=2))
        self.assertEqual(events.kills, 2)

    def test_round_reset_is_not_a_kill(self):
        tracker = GameEventTracker()
        tracker.update(snap(0, kills=3))
        events = tracker.update(snap(100, kills=0))
        self.assertEqual(events.kills, 0)
        self.assertTrue(events.respawned)

    def test_kill_and_damage_together(self):
        tracker = GameEventTracker()
        tracker.update(snap(100, kills=0))
        events = tracker.update(snap(60, kills=1))
        self.assertEqual(events.damage, 40)
        self.assertEqual(events.kills, 1)


class ResetTests(unittest.TestCase):
    def test_reset_forgets_the_baseline(self):
        tracker = GameEventTracker()
        tracker.update(snap(100))
        tracker.reset()
        self.assertIsNone(tracker.previous)
        self.assertTrue(tracker.update(snap(20)).first_sample)

    def test_reset_clears_the_death_latch(self):
        tracker = GameEventTracker()
        tracker.update(snap(100))
        tracker.update(snap(0))
        self.assertTrue(tracker.dead)
        tracker.reset()
        self.assertFalse(tracker.dead)


class SpectatingTests(unittest.TestCase):
    """死亡後觀戰別人時，GSI 的 ``player`` 區塊會變成被觀戰者的資料。

    這是「死亡後觀戰被誤判成復活」的原因，也是「擊殺漏接」的原因之一。
    """

    def test_spectating_is_not_a_respawn(self):
        tracker = GameEventTracker()
        tracker.update(snap(100))
        self.assertTrue(tracker.update(snap(0)).died)
        # 開始觀戰：血量變成被觀戰者的 73，但那不是我的血
        events = tracker.update(snap(73, steamid=OTHER))
        self.assertFalse(events.respawned)
        self.assertFalse(events.has_event)
        self.assertEqual(events.damage, 0)

    def test_spectating_does_not_heal_or_damage(self):
        tracker = GameEventTracker()
        tracker.update(snap(100))
        tracker.update(snap(0))
        # 被觀戰者被打，不該算成我被電；被觀戰者滿血也不算我復活
        self.assertFalse(tracker.update(snap(40, steamid=OTHER)).has_event)
        self.assertFalse(tracker.update(snap(100, steamid=OTHER)).has_event)

    def test_real_respawn_after_spectating_is_detected(self):
        tracker = GameEventTracker()
        tracker.update(snap(100))
        tracker.update(snap(0))
        tracker.update(snap(73, steamid=OTHER))
        tracker.update(snap(100, steamid=OTHER))
        events = tracker.update(snap(100))
        self.assertTrue(events.respawned)

    def test_death_is_inferred_when_the_death_packet_is_missed(self):
        """沒收到自己 0 血那一包，直接跳到觀戰別人也該算死亡。"""
        tracker = GameEventTracker()
        tracker.update(snap(100))
        events = tracker.update(snap(80, steamid=OTHER))
        self.assertTrue(events.died)
        self.assertFalse(events.respawned)

    def test_death_is_not_reported_twice_while_spectating(self):
        tracker = GameEventTracker()
        tracker.update(snap(100))
        tracker.update(snap(0))
        self.assertFalse(tracker.update(snap(80, steamid=OTHER)).died)
        self.assertFalse(tracker.update(snap(90, steamid=OTHER)).died)

    def test_spectating_then_own_death_packet_still_works(self):
        tracker = GameEventTracker()
        tracker.update(snap(100))
        tracker.update(snap(80, steamid=OTHER))  # 推論死亡
        self.assertFalse(tracker.update(snap(0)).died, "已經死亡過，不該再報一次")
        self.assertTrue(tracker.update(snap(100)).respawned)

    def test_spectarget_fallback_when_provider_is_unknown(self):
        """拿不到 provider.steamid 時（例如自己手寫的 cfg 沒有 provider）。"""
        tracker = GameEventTracker()
        tracker.update(snap(100, provider=""))
        tracker.update(snap(0, provider=""))
        events = tracker.update(snap(80, provider="", spectarget=OTHER))
        self.assertFalse(events.respawned)

    def test_provider_matching_means_it_is_mine(self):
        tracker = GameEventTracker()
        tracker.update(snap(100))
        self.assertEqual(tracker.update(snap(80)).damage, 20)

    def test_own_steamid_missing_falls_back_to_health(self):
        """cfg 沒有 player_id 時，至少不要壞掉。"""
        tracker = GameEventTracker()
        tracker.update(snap(100, steamid=""))
        self.assertEqual(tracker.update(snap(80, steamid="")).damage, 20)


class KillDetectionTests(unittest.TestCase):
    """擊殺要用整場累計的 ``match_stats.kills``，才不會被回合重置影響。"""

    def test_kill_via_match_stats(self):
        tracker = GameEventTracker()
        tracker.update(snap(100, kills=0, match_kills=0))
        events = tracker.update(snap(100, kills=1, match_kills=1))
        self.assertEqual(events.kills, 1)

    def test_first_kill_of_the_next_round_is_detected(self):
        """上一回合 3 殺 → 回合重置回 0 → 新回合第一殺。

        早期版本因為 round_kills 不寫回 0，這一殺會被算成負的而漏掉。
        """
        tracker = GameEventTracker()
        tracker.update(snap(100, kills=3, match_kills=3))
        tracker.update(snap(100, kills=0, match_kills=3))  # 換回合
        events = tracker.update(snap(100, kills=1, match_kills=4))
        self.assertEqual(events.kills, 1)

    def test_round_kills_fallback_handles_the_reset(self):
        """沒有 match_stats 時退回 round_kills。"""
        tracker = GameEventTracker()
        tracker.update(snap(100, kills=3))
        self.assertEqual(tracker.update(snap(100, kills=0)).kills, 0)
        self.assertEqual(tracker.update(snap(100, kills=1)).kills, 1)

    def test_spectated_kills_are_not_counted(self):
        tracker = GameEventTracker()
        tracker.update(snap(100, kills=0, match_kills=0))
        tracker.update(snap(0, kills=0, match_kills=0))
        # 被觀戰的人拿了 3 殺，不是我的
        events = tracker.update(
            snap(90, kills=3, match_kills=3, steamid=OTHER)
        )
        self.assertEqual(events.kills, 0)
        # 復活之後自己的第一殺一定要算到
        tracker.update(snap(100, kills=0, match_kills=0))
        self.assertEqual(tracker.update(snap(100, kills=1, match_kills=1)).kills, 1)

    def test_implausible_jump_resyncs_instead_of_firing_events(self):
        tracker = GameEventTracker()
        tracker.update(snap(100, kills=0, match_kills=0))
        events = tracker.update(snap(100, kills=0, match_kills=30))
        self.assertEqual(events.kills, 0)
        # 重新同步之後，下一個正常的擊殺要算得到
        self.assertEqual(tracker.update(snap(100, kills=0, match_kills=31)).kills, 1)

    def test_match_kills_going_backwards_is_not_a_kill(self):
        tracker = GameEventTracker()
        tracker.update(snap(100, kills=0, match_kills=10))
        self.assertEqual(tracker.update(snap(100, kills=0, match_kills=0)).kills, 0)

    def test_multi_kill_is_capped_at_the_sanity_limit(self):
        tracker = GameEventTracker()
        tracker.update(snap(100, kills=0, match_kills=0))
        events = tracker.update(
            snap(100, kills=0, match_kills=MAX_KILLS_PER_TICK + 1)
        )
        self.assertEqual(events.kills, 0)


class SnapshotIdentityTests(unittest.TestCase):
    def test_default_snapshot_is_mine(self):
        self.assertTrue(PlayerSnapshot().is_mine)

    def test_different_player_steamid_is_not_mine(self):
        snapshot = snap(100, steamid=OTHER)
        self.assertFalse(snapshot.is_mine)

    def test_matching_steamid_is_mine(self):
        self.assertTrue(snap(100).is_mine)

    def test_no_provider_falls_back_to_spectarget(self):
        self.assertFalse(snap(100, provider="", spectarget=OTHER).is_mine)
        self.assertTrue(snap(100, provider="").is_mine)

    def test_alive_property(self):
        self.assertTrue(snap(1).alive)
        self.assertFalse(snap(0).alive)


if __name__ == "__main__":
    unittest.main()
