"""產生並自動安裝 CS2 的 Game State Integration 設定檔。

CS2 只會讀 ``<遊戲目錄>/game/csgo/cfg/gamestate_integration_*.cfg``，
而且要**重新啟動遊戲**才會生效。手動放檔案對使用者來說很麻煩，所以這裡提供：

- 從登錄檔與 ``libraryfolders.vdf`` 自動找出 CS2 的 cfg 目錄。
- **用目前的 port / token 產生** cfg（不是複製固定內容），改了設定也不會對不上。
- 安裝前先檢查同一個目錄裡有沒有**其他也指向同一個埠**的 cfg
  ——那正是「token 不符」警告最常見的來源。
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: 我們自己安裝的檔名
CFG_FILENAME = "gamestate_integration_dglab.cfg"

#: CS2 相對於 Steam 媒體庫的路徑（前面是 CS2，後面是舊版 CS:GO 的位置）
CS2_CFG_SUBPATHS: tuple[str, ...] = (
    "steamapps/common/Counter-Strike Global Offensive/game/csgo/cfg",
    "steamapps/common/Counter-Strike Global Offensive/csgo/cfg",
)

#: 判斷「這看起來是不是 CS2 的 cfg 目錄」用的檔名樣式
GSI_FILE_GLOB = "gamestate_integration*.cfg"

#: 我們需要的 ``data`` 訂閱項目。
#:
#: ``provider``（自己的 SteamID）與 ``player_id``（player 區塊的 SteamID）是
#: 修正「死亡觀戰被誤判成復活／擊殺漏接」的必要條件：兩者一比對才知道
#: 那一包 player 資料是不是自己的。
#: ``player_match_stats`` 提供整場累計擊殺數，比每回合歸零的 round_kills 可靠。
REQUIRED_DATA_BLOCKS: tuple[str, ...] = (
    "provider",
    "player_id",
    "player_state",
    "player_match_stats",
)

_URI_RE = re.compile(r'"uri"\s+"([^"]+)"')
_TOKEN_RE = re.compile(r'"token"\s+"([^"]+)"')
_PATH_RE = re.compile(r'"path"\s+"([^"]+)"')

#: 這些主機名稱代表同一台機器
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def normalize_host(host: str) -> str:
    return "127.0.0.1" if host.strip().lower() in LOCAL_HOSTS else host.strip().lower()


def build_cfg_text(
    host: str = "127.0.0.1",
    port: int = 3000,
    token: str = "DGLAB",
    *,
    heartbeat: float = 10.0,
) -> str:
    """產生 cfg 內容。

    ``token`` 是空字串時就不寫 ``auth`` 區塊（本程式的 ``gsi.token`` 設成空字串
    時會接受任何來源，兩邊剛好對得上）。
    """
    lines = [
        '"CS2 x DGLAB-CS2"',
        "{",
        f'    "uri" "http://{host}:{port}"',
        '    "timeout" "5.0"',
        # buffer = 收集多久才送出；throttle = 兩次送出之間的最短間隔。
        # 兩個都調到 0.05（最多 20 次/秒）讓「受擊」這種瞬間事件能早一點送到，
        # 這也是整條鏈路裡我們唯一能控制的 CS2 端延遲。
        '    "buffer"  "0.05"',
        '    "throttle" "0.05"',
        f'    "heartbeat" "{heartbeat:g}"',
    ]
    if token:
        lines += [
            '    "auth"',
            "    {",
            f'        "token" "{token}"',
            "    }",
        ]
    lines += [
        '    "data"',
        "    {",
        '        "provider"              "1"',
        '        "map"                   "1"',
        '        "round"                 "1"',
        '        "player_id"             "1"',
        '        "player_state"          "1"',
        '        "player_weapons"        "1"',
        '        "player_match_stats"    "1"',
        "    }",
        "}",
        "",
    ]
    return "\n".join(lines)


def missing_data_blocks(path: str | Path) -> tuple[str, ...]:
    """檢查 cfg 的 ``data`` 區塊有沒有訂閱我們需要的項目。

    缺少 ``provider`` 或 ``player_id`` 時，程式就沒辦法分辨「這包 player 資料
    是不是自己的」，死亡後觀戰別人會被誤判成復活。
    """
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return REQUIRED_DATA_BLOCKS
    return tuple(
        key for key in REQUIRED_DATA_BLOCKS if f'"{key}"' not in text
    )


def parse_uri(uri: str | None) -> tuple[str, int] | None:
    """把 ``http://127.0.0.1:3000`` 解析成 ``(host, port)``。"""
    if not uri:
        return None
    match = re.match(r"^[a-zA-Z]+://([^/:]+)(?::(\d+))?", uri.strip())
    if not match:
        return None
    host = match.group(1)
    port = int(match.group(2)) if match.group(2) else 80
    return host, port


@dataclass(frozen=True)
class InstalledCfg:
    """一個已經放在 cfg 目錄裡的 GSI 設定檔。"""

    path: Path
    uri: str | None = None
    token: str | None = None

    @property
    def filename(self) -> str:
        return self.path.name

    @property
    def endpoint(self) -> tuple[str, int] | None:
        return parse_uri(self.uri)

    @property
    def is_ours(self) -> bool:
        return self.path.name == CFG_FILENAME

    def targets(self, host: str, port: int) -> bool:
        """這個檔案是不是也指向同一台主機的同一個埠。"""
        endpoint = self.endpoint
        if endpoint is None:
            return False
        other_host, other_port = endpoint
        return normalize_host(other_host) == normalize_host(host) and other_port == int(port)


def read_cfg(path: str | Path) -> InstalledCfg:
    """讀出 cfg 裡的 uri 與 token。"""
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return InstalledCfg(path=target)
    uri_match = _URI_RE.search(text)
    token_match = _TOKEN_RE.search(text)
    return InstalledCfg(
        path=target,
        uri=uri_match.group(1) if uri_match else None,
        token=token_match.group(1) if token_match else None,
    )


def scan_cfg_dir(directory: str | Path) -> list[InstalledCfg]:
    """列出目錄裡所有 GSI 設定檔（依檔名排序）。"""
    folder = Path(directory)
    if not folder.is_dir():
        return []
    return [read_cfg(path) for path in sorted(folder.glob(GSI_FILE_GLOB))]


def find_conflicts(
    directory: str | Path, host: str, port: int, *, ignore: str = CFG_FILENAME
) -> list[InstalledCfg]:
    """找出「也指向同一個埠、但不是我們這個檔」的其他 cfg。

    這些就是「收到 token 不符的 GSI 資料」警告的元凶：CS2 會把狀態同時送給
    每一個訂閱，別份 cfg 用不同的 token 送過來就會被我們的伺服器拒絕。
    """
    return [
        cfg
        for cfg in scan_cfg_dir(directory)
        if cfg.filename != ignore and cfg.targets(host, port)
    ]


# ------------------------------------------------------------------ 找目錄


def steam_install_path() -> Path | None:
    """從 Windows 登錄檔找出 Steam 安裝路徑。"""
    try:
        import winreg
    except ImportError:  # pragma: no cover - 非 Windows
        return None

    candidates = (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    )
    for root, key_path, value_name in candidates:
        try:
            with winreg.OpenKey(root, key_path) as key:
                value, _ = winreg.QueryValueEx(key, value_name)
        except OSError:
            continue
        if value:
            path = Path(str(value))
            if path.is_dir():
                return path
    return None


def read_library_folders(steam_path: str | Path) -> list[Path]:
    """讀 ``libraryfolders.vdf``，列出所有媒體庫路徑。"""
    steam = Path(steam_path)
    folders: list[Path] = []
    if steam.is_dir():
        folders.append(steam)
    vdf = steam / "steamapps" / "libraryfolders.vdf"
    if not vdf.is_file():
        return folders
    try:
        text = vdf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return folders
    for raw in _PATH_RE.findall(text):
        # VDF 裡的跳脫字元：\\ -> \
        candidate = Path(raw.replace("\\\\", "\\"))
        if candidate.is_dir():
            folders.append(candidate)

    unique: list[Path] = []
    seen: set[str] = set()
    for folder in folders:
        key = str(folder).lower()
        if key not in seen:
            seen.add(key)
            unique.append(folder)
    return unique


def cs2_cfg_candidates(libraries: list[Path]) -> list[Path]:
    """由媒體庫清單推出所有可能的 CS2 cfg 目錄。"""
    found: list[Path] = []
    for library in libraries:
        for subpath in CS2_CFG_SUBPATHS:
            candidate = Path(library) / Path(subpath)
            if candidate.is_dir():
                found.append(candidate)
    return found


def find_cs2_cfg_dirs() -> list[Path]:
    """自動找出這台電腦上所有 CS2 的 cfg 目錄。"""
    steam = steam_install_path()
    libraries = read_library_folders(steam) if steam else []
    return cs2_cfg_candidates(libraries)


def looks_like_cfg_dir(directory: str | Path) -> bool:
    """判斷使用者手動選的資料夾像不像 CS2 的 cfg 目錄。"""
    folder = Path(directory)
    if not folder.is_dir():
        return False
    if any(folder.glob(GSI_FILE_GLOB)):
        return True
    lowered = str(folder).replace("/", "\\").lower()
    return lowered.endswith("csgo\\cfg")


# ------------------------------------------------------------------ 安裝


@dataclass(frozen=True)
class InstallResult:
    """安裝結果。"""

    path: Path
    #: 是不是新建立的檔案（False = 覆蓋舊的）
    created: bool
    #: 覆蓋前備份到哪裡（沒有舊檔就是 ``None``）
    backup: Path | None
    #: 安裝前後發現的「搶同一個埠」的其他 cfg
    conflicts: tuple[InstalledCfg, ...]


def _safe_backup(target: Path, backup: Path) -> Path | None:
    """把舊的設定檔備份起來；失敗時回傳 ``None`` 而不是讓安裝整個失敗。

    備份只是「出事可以還原」的方便功能，不該擋住真正重要的事（把新的
    cfg 寫進去）。實際上就踩過這個坑：舊的 ``.bak`` 被其他程式鎖住或帶有
    唯讀屬性時，``shutil.copy2`` 會丟出 ``PermissionError``，使用者看到的
    是「安裝失敗」——但其實只要跳過備份就能裝好。

    會先嘗試清掉目的檔的唯讀屬性再複製。
    """
    try:
        if backup.exists():
            # 舊的備份可能是唯讀的（copy2 會連權限一起複製過來）
            backup.chmod(0o666)
    except OSError:
        pass

    try:
        shutil.copy2(target, backup)
    except OSError as exc:
        logger.warning("無法備份舊的 GSI 設定（%s）：%s；仍會繼續安裝", backup, exc)
        return None
    return backup


def install_cfg(
    directory: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 3000,
    token: str = "DGLAB",
    heartbeat: float = 10.0,
    filename: str = CFG_FILENAME,
) -> InstallResult:
    """把產生的 cfg 寫進指定目錄。

    Raises:
        FileNotFoundError: 目錄不存在。
        OSError: 沒有寫入權限（例如遊戲裝在需要管理員權限的位置）。
    """
    folder = Path(directory)
    if not folder.is_dir():
        raise FileNotFoundError(f"找不到資料夾：{folder}")

    conflicts = tuple(find_conflicts(folder, host, port, ignore=filename))

    target = folder / filename
    backup: Path | None = None
    created = not target.exists()
    if not created:
        backup = target.with_suffix(target.suffix + ".bak")
        backup = _safe_backup(target, backup)

    target.write_text(
        build_cfg_text(host, port, token, heartbeat=heartbeat), encoding="utf-8"
    )
    return InstallResult(
        path=target, created=created, backup=backup, conflicts=conflicts
    )


# ------------------------------------------------------------------ 健檢


@dataclass(frozen=True)
class GsiCheck:
    """GSI 設定安裝狀況的檢查結果。"""

    #: 是否已經有一個「連線資訊完全正確」的設定檔
    ok: bool
    #: 一句話總結，可以直接顯示給使用者
    summary: str
    #: 找到的 CS2 cfg 目錄
    directories: tuple[Path, ...] = ()
    #: 找到且內容正確的那份
    installed: InstalledCfg | None = None
    #: 檔名對但連線資訊不符的那些（連同它們目前的設定）
    mismatched: tuple[InstalledCfg, ...] = ()
    #: 其他也指向同一個埠、可能造成「token 不符」警告的設定檔
    conflicts: tuple[InstalledCfg, ...] = ()
    #: 逐條說明，給訊息框用
    details: tuple[str, ...] = ()
    #: 已經安裝的 cfg 缺少的 data 訂閱項目
    missing_data: tuple[str, ...] = ()

    @property
    def needs_install(self) -> bool:
        return not self.ok


def _endpoint_matches(cfg: InstalledCfg, host: str, port: int, token: str) -> bool:
    endpoint = cfg.endpoint
    if endpoint is None:
        return False
    cfg_host, cfg_port = endpoint
    if normalize_host(cfg_host) != normalize_host(host) or cfg_port != int(port):
        return False
    return (cfg.token or "") == (token or "")


def check_gsi_installation(
    host: str = "127.0.0.1",
    port: int = 3000,
    token: str = "DGLAB",
    *,
    directories: list[Path] | tuple[Path, ...] | None = None,
    filename: str = CFG_FILENAME,
) -> GsiCheck:
    """檢查遊戲那邊的 GSI 設定是不是已經裝好、而且和目前設定一致。

    這是「顯示 QR code 之前」的把關：沒裝好就沒必要連上服務端，
    因為遊戲資料根本不會送過來。
    """
    folders = tuple(directories) if directories is not None else tuple(find_cs2_cfg_dirs())
    if not folders:
        return GsiCheck(
            ok=False,
            summary="找不到 CS2 的 GSI 設定資料夾",
            details=(
                "程式掃描了登錄檔與所有 Steam 媒體庫，都找不到",
                "Counter-Strike Global Offensive\\game\\csgo\\cfg。",
                "",
                "請確認 CS2 已經安裝，或按「安裝 GSI 設定」手動選擇資料夾。",
            ),
        )

    installed: InstalledCfg | None = None
    mismatched: list[InstalledCfg] = []
    conflicts: list[InstalledCfg] = []
    missing_data: tuple[str, ...] = ()
    details: list[str] = []

    for folder in folders:
        details.append(f"檢查資料夾：{folder}")
        target = folder / filename
        if target.is_file():
            cfg = read_cfg(target)
            if _endpoint_matches(cfg, host, port, token):
                installed = installed or cfg
                details.append("  ✓ 設定檔存在，連線資訊正確")
                missing = missing_data_blocks(target)
                if missing:
                    missing_data = missing
                    details.append(
                        "  ⚠ data 區塊少了 " + "、".join(missing)
                        + "；死亡後觀戰別人可能會被誤判成復活（按「安裝 GSI 設定」可修好）"
                    )
            else:
                mismatched.append(cfg)
                details.append("  ✗ 設定檔存在，但連線資訊不符：")
                details.append(f"      目前：uri={cfg.uri or '（沒有）'} token={cfg.token or '（沒有）'}")
                details.append(f"      應該：uri=http://{host}:{port} token={token or '（不驗證）'}")
        else:
            details.append("  ✗ 找不到 " + filename)

        for other in find_conflicts(folder, host, port, ignore=filename):
            conflicts.append(other)
            details.append(
                f"  ⚠ {other.filename} 也指向同一個埠（token={other.token or '（沒有）'}）"
                "，會造成「token 不符」警告"
            )

    if installed is not None:
        summary = "GSI 設定已安裝且連線資訊正確"
        if missing_data:
            summary += f"（但 data 少了 {'、'.join(missing_data)}）"
        if conflicts:
            summary += f"（另有 {len(conflicts)} 個搶同一個埠的設定檔）"
    elif mismatched:
        summary = "GSI 設定存在，但連線資訊和目前設定不一致"
    else:
        summary = "遊戲目錄裡還沒有 GSI 設定檔"

    return GsiCheck(
        ok=installed is not None,
        summary=summary,
        directories=folders,
        installed=installed,
        mismatched=tuple(mismatched),
        conflicts=tuple(conflicts),
        details=tuple(details),
        missing_data=missing_data,
    )