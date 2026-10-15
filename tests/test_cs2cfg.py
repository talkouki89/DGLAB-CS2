"""CS2 GSI 設定檔的產生、掃描與安裝測試。"""

from __future__ import annotations

import shutil
import unittest
from pathlib import Path

import cs2cfg


def scratch_dir(test: unittest.TestCase, name: str) -> Path:
    """在 tests/ 底下開一個臨時資料夾並登記清理。"""
    folder = Path(__file__).parent / f"_scratch_cfgdir_{name}"
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    test.addCleanup(shutil.rmtree, folder, ignore_errors=True)
    return folder


class BuildCfgTests(unittest.TestCase):
    def test_contains_endpoint_and_token(self):
        text = cs2cfg.build_cfg_text("127.0.0.1", 3000, "DGLAB")
        self.assertIn('"uri" "http://127.0.0.1:3000"', text)
        self.assertIn('"token" "DGLAB"', text)
        self.assertIn('"player_state"', text)
        self.assertIn('"round"', text)

    def test_custom_port_and_host(self):
        text = cs2cfg.build_cfg_text("192.168.1.5", 3010, "SECRET")
        self.assertIn('"uri" "http://192.168.1.5:3010"', text)
        self.assertIn('"token" "SECRET"', text)

    def test_empty_token_omits_auth_block(self):
        text = cs2cfg.build_cfg_text("127.0.0.1", 3000, "")
        self.assertNotIn('"auth"', text)
        self.assertNotIn('"token"', text)

    def test_heartbeat_is_configurable(self):
        self.assertIn('"heartbeat" "5"', cs2cfg.build_cfg_text(heartbeat=5.0))

    def test_throttle_and_buffer_are_low_for_fast_updates(self):
        """throttle／buffer 是整條鏈路裡唯一能控制的 CS2 端延遲。"""
        text = cs2cfg.build_cfg_text()
        self.assertIn('"throttle" "0.05"', text)
        self.assertIn('"buffer"  "0.05"', text)

    def test_no_allplayers_subscriptions(self):
        """我們不解析 allplayers，訂閱它們只會讓每次 POST 變大變慢。"""
        text = cs2cfg.build_cfg_text()
        self.assertNotIn("allplayers", text)

    def test_repo_cfg_matches_the_generator(self):
        """專案裡那份手動安裝用的 cfg 必須和產生器一模一樣。

        兩份分岔過一次：產生器改成 throttle 0.05 但靜態檔還留著 0.1，
        結果手動安裝的人拿到的是舊設定。
        """
        repo_cfg = Path(__file__).parent.parent / cs2cfg.CFG_FILENAME
        self.assertTrue(repo_cfg.is_file(), f"找不到 {repo_cfg}")
        self.assertEqual(
            repo_cfg.read_text(encoding="utf-8"),
            cs2cfg.build_cfg_text(),
            "請用 cs2cfg.build_cfg_text() 重新產生 gamestate_integration_dglab.cfg",
        )

    def test_round_trip_through_read_cfg(self):
        folder = scratch_dir(self, "roundtrip")
        path = folder / "x.cfg"
        path.write_text(
            cs2cfg.build_cfg_text("localhost", 3123, "TOK"), encoding="utf-8"
        )
        cfg = cs2cfg.read_cfg(path)
        self.assertEqual(cfg.uri, "http://localhost:3123")
        self.assertEqual(cfg.token, "TOK")
        self.assertEqual(cfg.endpoint, ("localhost", 3123))


class ParseUriTests(unittest.TestCase):
    def test_hosts_and_ports(self):
        self.assertEqual(cs2cfg.parse_uri("http://127.0.0.1:3000"), ("127.0.0.1", 3000))
        self.assertEqual(cs2cfg.parse_uri("http://localhost:5000"), ("localhost", 5000))
        self.assertEqual(
            cs2cfg.parse_uri("http://127.0.0.1:3197/csgo_game_event"),
            ("127.0.0.1", 3197),
        )

    def test_default_port(self):
        self.assertEqual(cs2cfg.parse_uri("http://example.com"), ("example.com", 80))

    def test_invalid(self):
        for value in (None, "", "not a url"):
            with self.subTest(value=value):
                self.assertIsNone(cs2cfg.parse_uri(value))
        self.assertIsNone(cs2cfg.parse_uri(None))

    def test_normalize_host(self):
        self.assertEqual(cs2cfg.normalize_host("localhost"), "127.0.0.1")
        self.assertEqual(cs2cfg.normalize_host(" LocalHost "), "127.0.0.1")
        self.assertEqual(cs2cfg.normalize_host("192.168.0.2"), "192.168.0.2")


class ScanTests(unittest.TestCase):
    def make_dir(self, name: str) -> Path:
        return scratch_dir(self, name)

    def test_scan_reads_every_gsi_file(self):
        folder = self.make_dir("scan")
        (folder / "gamestate_integration_dglab.cfg").write_text(
            cs2cfg.build_cfg_text("127.0.0.1", 3000, "DGLAB"), encoding="utf-8"
        )
        (folder / "gamestate_integration_other.cfg").write_text(
            cs2cfg.build_cfg_text("localhost", 3000, "MYTOKENHERE"), encoding="utf-8"
        )
        (folder / "not_gsi.txt").write_text("ignore me", encoding="utf-8")
        found = cs2cfg.scan_cfg_dir(folder)
        self.assertEqual(
            [cfg.filename for cfg in found],
            ["gamestate_integration_dglab.cfg", "gamestate_integration_other.cfg"],
        )

    def test_find_conflicts_detects_the_real_world_case(self):
        """就是實際上遇到的情況：另一份 cfg 指向同一個埠、token 不同。"""
        folder = self.make_dir("conflict")
        (folder / "gamestate_integration_dglab.cfg").write_text(
            cs2cfg.build_cfg_text("127.0.0.1", 3000, "DGLAB"), encoding="utf-8"
        )
        (folder / "gamestate_integration_nodecs2.cfg").write_text(
            cs2cfg.build_cfg_text("127.0.0.1", 3000, "MYTOKENHERE"), encoding="utf-8"
        )
        conflicts = cs2cfg.find_conflicts(folder, "127.0.0.1", 3000)
        self.assertEqual(
            [cfg.filename for cfg in conflicts],
            ["gamestate_integration_nodecs2.cfg"],
        )
        self.assertEqual(conflicts[0].token, "MYTOKENHERE")

    def test_different_port_is_not_a_conflict(self):
        folder = self.make_dir("noconflict")
        (folder / "gamestate_integration_logitech.cfg").write_text(
            cs2cfg.build_cfg_text("localhost", 5000, "X"), encoding="utf-8"
        )
        self.assertEqual(cs2cfg.find_conflicts(folder, "127.0.0.1", 3000), [])

    def test_localhost_counts_as_same_host(self):
        folder = self.make_dir("localhost")
        (folder / "gamestate_integration_other.cfg").write_text(
            cs2cfg.build_cfg_text("localhost", 3000, "X"), encoding="utf-8"
        )
        self.assertEqual(len(cs2cfg.find_conflicts(folder, "127.0.0.1", 3000)), 1)

    def test_scan_missing_dir(self):
        self.assertEqual(cs2cfg.scan_cfg_dir(Path("no-such-dir-xyz")), [])


class InstallTests(unittest.TestCase):
    def test_creates_file(self):
        folder = scratch_dir(self, "install_new")
        result = cs2cfg.install_cfg(folder, port=3010, token="NEW")
        self.assertTrue(result.created)
        self.assertIsNone(result.backup)
        self.assertTrue(result.path.exists())
        cfg = cs2cfg.read_cfg(result.path)
        self.assertEqual(cfg.uri, "http://127.0.0.1:3010")
        self.assertEqual(cfg.token, "NEW")
        self.assertEqual(result.path.name, cs2cfg.CFG_FILENAME)

    def test_overwrites_and_backs_up(self):
        folder = scratch_dir(self, "install_over")
        first = cs2cfg.install_cfg(folder, port=3000, token="OLD")
        second = cs2cfg.install_cfg(folder, port=3001, token="NEW")
        self.assertFalse(second.created)
        self.assertIsNotNone(second.backup)
        self.assertEqual(cs2cfg.read_cfg(second.backup).token, "OLD")
        self.assertEqual(cs2cfg.read_cfg(first.path).uri, "http://127.0.0.1:3001")

    def test_reports_conflicts(self):
        folder = scratch_dir(self, "install_conflict")
        (folder / "gamestate_integration_other.cfg").write_text(
            cs2cfg.build_cfg_text("127.0.0.1", 3000, "MYTOKENHERE"), encoding="utf-8"
        )
        result = cs2cfg.install_cfg(folder, port=3000, token="DGLAB")
        self.assertEqual(len(result.conflicts), 1)
        self.assertEqual(result.conflicts[0].token, "MYTOKENHERE")

    def test_missing_directory(self):
        with self.assertRaises(FileNotFoundError):
            cs2cfg.install_cfg(Path("no-such-dir-abc"))


class DiscoveryTests(unittest.TestCase):
    def test_read_library_folders(self):
        steam = scratch_dir(self, "steam")
        (steam / "steamapps").mkdir(parents=True, exist_ok=True)
        other = scratch_dir(self, "library")
        escaped = str(other).replace("\\", "\\\\")
        (steam / "steamapps" / "libraryfolders.vdf").write_text(
            '"libraryfolders"\n{\n'
            '    "0"\n    {\n      "path" "' + escaped + '"\n    }\n'
            '    "1"\n    {\n      "path" "Z:\\\\no-such-library"\n    }\n'
            "}\n",
            encoding="utf-8",
        )
        folders = cs2cfg.read_library_folders(steam)
        self.assertIn(steam, folders)
        self.assertIn(other, folders)
        # 不存在的媒體庫會被濾掉
        self.assertFalse(any("no-such-library" in str(folder) for folder in folders))

    def test_read_library_folders_without_vdf(self):
        steam = scratch_dir(self, "steam_novdf")
        self.assertEqual(cs2cfg.read_library_folders(steam), [steam])

    def test_cs2_cfg_candidates(self):
        library = scratch_dir(self, "lib")
        cfg_dir = (
            library
            / "steamapps"
            / "common"
            / "Counter-Strike Global Offensive"
            / "game"
            / "csgo"
            / "cfg"
        )
        cfg_dir.mkdir(parents=True)
        self.assertEqual(cs2cfg.cs2_cfg_candidates([library]), [cfg_dir])
        self.assertEqual(cs2cfg.cs2_cfg_candidates([library.parent]), [])

    def test_looks_like_cfg_dir(self):
        folder = scratch_dir(self, "looks")
        self.assertFalse(cs2cfg.looks_like_cfg_dir(folder))
        (folder / "gamestate_integration_x.cfg").write_text("x", encoding="utf-8")
        self.assertTrue(cs2cfg.looks_like_cfg_dir(folder))
        self.assertFalse(cs2cfg.looks_like_cfg_dir(Path("no-such-dir")))

    def test_looks_like_cfg_dir_by_name(self):
        base = scratch_dir(self, "byname")
        cfg_dir = base / "csgo" / "cfg"
        cfg_dir.mkdir(parents=True)
        self.assertTrue(cs2cfg.looks_like_cfg_dir(cfg_dir))

    def test_find_cs2_cfg_dirs_returns_paths(self):
        # 只確認不會爆掉、而且回傳的是 Path（實際內容取決於這台電腦）
        for folder in cs2cfg.find_cs2_cfg_dirs():
            self.assertIsInstance(folder, Path)
            self.assertTrue(folder.is_dir())


class CheckInstallationTests(unittest.TestCase):
    """「顯示 QR code 之前」的把關邏輯。"""

    def make_dir(self, name: str) -> Path:
        return scratch_dir(self, name)

    def check(self, folder, **kwargs):
        kwargs.setdefault("host", "127.0.0.1")
        kwargs.setdefault("port", 3000)
        kwargs.setdefault("token", "DGLAB")
        return cs2cfg.check_gsi_installation(directories=[folder], **kwargs)

    def test_no_directory_found(self):
        result = cs2cfg.check_gsi_installation(directories=[])
        self.assertFalse(result.ok)
        self.assertTrue(result.needs_install)
        self.assertIn("找不到", result.summary)
        self.assertEqual(result.directories, ())

    def test_directory_without_our_cfg(self):
        folder = self.make_dir("check_missing")
        result = self.check(folder)
        self.assertFalse(result.ok)
        self.assertIn("還沒有", result.summary)
        self.assertIsNone(result.installed)

    def test_directory_with_correct_cfg(self):
        folder = self.make_dir("check_ok")
        cs2cfg.install_cfg(folder, port=3000, token="DGLAB")
        result = self.check(folder)
        self.assertTrue(result.ok)
        self.assertFalse(result.needs_install)
        self.assertIn("已安裝", result.summary)
        self.assertIsNotNone(result.installed)

    def test_localhost_matches(self):
        folder = self.make_dir("check_localhost")
        (folder / cs2cfg.CFG_FILENAME).write_text(
            cs2cfg.build_cfg_text("localhost", 3000, "DGLAB"), encoding="utf-8"
        )
        self.assertTrue(self.check(folder).ok)

    def test_wrong_port_is_mismatch(self):
        folder = self.make_dir("check_port")
        cs2cfg.install_cfg(folder, port=3000, token="DGLAB")
        result = self.check(folder, port=3010)
        self.assertFalse(result.ok)
        self.assertIn("不一致", result.summary)
        self.assertEqual(len(result.mismatched), 1)
        self.assertIn("3010", " ".join(result.details))

    def test_wrong_token_is_mismatch(self):
        folder = self.make_dir("check_token")
        cs2cfg.install_cfg(folder, port=3000, token="OLD")
        result = self.check(folder, token="DGLAB")
        self.assertFalse(result.ok)
        self.assertEqual(len(result.mismatched), 1)
        self.assertIn("OLD", " ".join(result.details))

    def test_empty_token_matches_cfg_without_auth(self):
        folder = self.make_dir("check_notoken")
        cs2cfg.install_cfg(folder, port=3000, token="")
        self.assertTrue(self.check(folder, token="").ok)
        # 但要求 token 時就不算數
        self.assertFalse(self.check(folder, token="DGLAB").ok)

    def test_conflicts_are_reported_but_do_not_block(self):
        folder = self.make_dir("check_conflict")
        cs2cfg.install_cfg(folder, port=3000, token="DGLAB")
        (folder / "gamestate_integration_nodecs2.cfg").write_text(
            cs2cfg.build_cfg_text("127.0.0.1", 3000, "MYTOKENHERE"), encoding="utf-8"
        )
        result = self.check(folder)
        self.assertTrue(result.ok, "有衝突的檔案不代表沒安裝好")
        self.assertEqual(len(result.conflicts), 1)
        self.assertIn("1 個", result.summary)
        details = " ".join(result.details)
        self.assertIn("nodecs2", details)
        self.assertIn("token 不符", details)

    def test_details_list_every_directory(self):
        first = self.make_dir("check_multi1")
        second = self.make_dir("check_multi2")
        cs2cfg.install_cfg(second, port=3000, token="DGLAB")
        result = cs2cfg.check_gsi_installation(directories=[first, second])
        self.assertTrue(result.ok)
        self.assertEqual(len(result.directories), 2)
        self.assertTrue(any(str(first) in line for line in result.details))
        self.assertTrue(any(str(second) in line for line in result.details))

    def test_installed_anywhere_is_enough(self):
        empty = self.make_dir("check_empty")
        good = self.make_dir("check_good")
        cs2cfg.install_cfg(good, port=3000, token="DGLAB")
        self.assertTrue(cs2cfg.check_gsi_installation(directories=[empty, good]).ok)

    def test_unreadable_cfg_counts_as_mismatch(self):
        folder = self.make_dir("check_broken")
        (folder / cs2cfg.CFG_FILENAME).write_text("這不是設定檔", encoding="utf-8")
        result = self.check(folder)
        self.assertFalse(result.ok)
        self.assertEqual(len(result.mismatched), 1)
        self.assertIsNone(result.mismatched[0].uri)


class RequiredDataBlockTests(unittest.TestCase):
    """缺少 provider／player_id 就修不了「觀戰被誤判成復活」。"""

    def test_generated_cfg_has_everything(self):
        text = cs2cfg.build_cfg_text("127.0.0.1", 3000, "DGLAB")
        folder = scratch_dir(self, "blocks_ok")
        path = folder / cs2cfg.CFG_FILENAME
        path.write_text(text, encoding="utf-8")
        self.assertEqual(cs2cfg.missing_data_blocks(path), ())
        self.assertIn('"provider"', text)
        self.assertIn('"player_id"', text)
        self.assertIn('"player_match_stats"', text)

    def test_missing_provider_is_detected(self):
        folder = scratch_dir(self, "blocks_missing")
        path = folder / cs2cfg.CFG_FILENAME
        path.write_text(
            '"x"\n{\n  "uri" "http://127.0.0.1:3000"\n'
            '  "data"\n  {\n    "player_state" "1"\n  }\n}\n',
            encoding="utf-8",
        )
        missing = cs2cfg.missing_data_blocks(path)
        self.assertIn("provider", missing)
        self.assertIn("player_id", missing)
        self.assertNotIn("player_state", missing)

    def test_unreadable_cfg_reports_everything_missing(self):
        self.assertEqual(
            cs2cfg.missing_data_blocks(Path("no-such-file-xyz")),
            cs2cfg.REQUIRED_DATA_BLOCKS,
        )

    def test_check_reports_missing_blocks_as_a_warning(self):
        folder = scratch_dir(self, "blocks_warn")
        path = folder / cs2cfg.CFG_FILENAME
        path.write_text(
            '"x"\n{\n  "uri" "http://127.0.0.1:3000"\n'
            '  "auth"\n  {\n    "token" "DGLAB"\n  }\n'
            '  "data"\n  {\n    "player_state" "1"\n  }\n}\n',
            encoding="utf-8",
        )
        result = cs2cfg.check_gsi_installation(directories=[folder])
        self.assertTrue(result.ok, "少了訂閱項目還是可以運作，不該擋住啟動")
        self.assertIn("provider", result.missing_data)
        self.assertIn("data 少了", result.summary)
        self.assertIn("觀戰", " ".join(result.details))


class TutorialTests(unittest.TestCase):
    def test_tutorial_covers_the_essentials(self):
        import tutorial

        text = tutorial.TUTORIAL_TEXT
        for keyword in (
            "安裝 GSI",
            "重新啟動 CS2",
            "掃碼連接",
            "安全上限",
            "暫停並歸零",
            "token 不符",
        ):
            with self.subTest(keyword=keyword):
                self.assertIn(keyword, text)


if __name__ == "__main__":
    unittest.main()
