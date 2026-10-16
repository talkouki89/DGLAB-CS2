"""給別人看的即時狀態網頁（直播用）。

**這不是 GSI 伺服器。** GSI 那個埠收的是「可以改變電擊行為」的指令，
開放出去等於讓任何人遙控你的裝置，所以絕對不能動它。

這裡另外開一個**唯讀**的 HTTP 伺服器：

- ``GET /``          給人看的儀表板（深色、自動更新）
- ``GET /overlay``   給 OBS 瀏覽器來源用的透明疊層（大數字）
- ``GET /api/status`` 機器可讀的 JSON
- ``GET /api/events`` 最近的紀錄

三個刻意的安全限制：

1. **沒有任何會改變狀態的端點** —— 只能讀。
2. **絕對不輸出配對用的 QR 連結**，否則別人拿去配對就能接管你的裝置。
3. 預設只綁 ``127.0.0.1``。要給別人看必須自己改成 ``0.0.0.0``，
   而且可以再加上 ``token`` 當通關密語。

只用標準函式庫（``http.server``），所以不需要額外安裝任何東西。
"""

from __future__ import annotations

import html
import json
import logging
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

#: 監看頁面每次重抓資料的間隔（毫秒）
DEFAULT_REFRESH_MS = 500

#: 最多保留幾筆事件
MAX_EVENTS = 60

#: 狀態資料的欄位（順序就是 JSON 的順序，方便人眼看）
STATUS_FIELDS: tuple[str, ...] = (
    "ok",
    "version",
    "state",
    "paused",
    "paired",
    "cs2_connected",
    "in_game",
    "spectating",
    "health",
    "burning",
    "strength",
    "max_strength",
    "stack",
    "streak",
    "output_active",
    "wave",
    "last_event",
    "stats",
    "updated_at",
)


def build_status_payload(
    status: Any,
    *,
    version: str = "",
    wave: str = "",
    max_strength: int = 0,
) -> dict[str, Any]:
    """把 :class:`engine_status.EngineStatus` 轉成監看頁要的 dict。

    刻意只挑「可以公開」的欄位：配對連結、client id、GSI token 這些
    一律不輸出。
    """
    stats = getattr(status, "stats", None)
    stats_payload: dict[str, Any] = {}
    if stats is not None:
        stats_payload = {
            "kills": int(stats.kills),
            "deaths": int(stats.deaths),
            "kd": round(stats.kill_death_ratio, 2),
            "best_streak": int(stats.best_streak),
            "hits": int(stats.hits),
            "damage_taken": int(stats.damage_taken),
            "taser_hits": int(stats.taser_hits),
            "max_strength": int(stats.max_strength),
            "average_strength": round(stats.average_strength, 1),
            "shock_seconds": round(stats.shock_seconds, 1),
            "duration": stats.duration_label,
        }

    health_known = bool(getattr(status, "health_known", False))
    return {
        "ok": True,
        "version": version,
        "state": str(getattr(status, "state_label", "") or ""),
        "paused": bool(getattr(status, "paused", False)),
        "paired": bool(getattr(status, "paired", False)),
        "cs2_connected": bool(getattr(status, "cs2_connected", False)),
        "in_game": bool(health_known),
        "spectating": bool(getattr(status, "spectating", False)),
        "health": int(getattr(status, "health", 0)),
        "burning": int(getattr(status, "burning", 0)),
        "strength": int(getattr(status, "strength", 0)),
        "max_strength": int(max_strength or getattr(status, "max_strength", 0) or 0),
        "stack": round(float(getattr(status, "stack", 0.0)), 1),
        "streak": int(getattr(status, "streak", 0)),
        "output_active": bool(getattr(status, "output_active", False)),
        "wave": wave,
        "last_event": str(getattr(status, "last_event", "") or ""),
        "stats": stats_payload,
        "updated_at": time.time(),
    }


# ------------------------------------------------------------------ 前端頁面

_BASE_CSS = """
:root{
  --bg:#0f0f17; --surface:#1a1a28; --surface2:#232336; --border:#33334d;
  --text:#e8e8f4; --dim:#8a8aa8; --accent:#22d3ee; --pink:#f472b6;
  --orange:#fb923c; --ok:#34d399; --warn:#fbbf24; --bad:#f87171;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font-family:"Microsoft JhengHei UI","Noto Sans TC",system-ui,sans-serif}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px}
.dim{color:var(--dim)}
.accent{color:var(--accent)}
.bar{height:12px;border-radius:6px;background:var(--surface2);overflow:hidden}
.bar>i{display:block;height:100%;width:0;border-radius:6px;transition:width .25s ease}
"""


def _dashboard_html(*, refresh_ms: int, title: str) -> str:
    return f"""<!doctype html>
<html lang="zh-Hant"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_BASE_CSS}
.wrap{{max-width:900px;margin:0 auto;padding:20px;display:grid;gap:14px}}
header{{display:flex;align-items:center;justify-content:space-between;gap:12px}}
h1{{font-size:18px;margin:0;letter-spacing:.5px}}
.dot{{display:inline-block;width:9px;height:9px;border-radius:50%;
  background:var(--dim);margin-right:6px;vertical-align:middle}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px}}
.kv{{display:flex;flex-direction:column;gap:4px}}
.kv b{{font-size:22px;font-weight:700}}
.kv span{{font-size:12px;color:var(--dim)}}
.big{{font-size:52px;font-weight:800;line-height:1;color:var(--accent)}}
.row{{display:flex;align-items:center;gap:10px;margin:8px 0}}
.row label{{width:56px;font-size:13px;color:var(--dim)}}
.row .num{{width:86px;text-align:right;font-variant-numeric:tabular-nums}}
#event{{font-family:Consolas,monospace;font-size:12px;color:var(--dim);min-height:18px}}
</style></head>
<body><div class="wrap">

<header>
  <h1><span class="dot" id="dot"></span>DGLAB-CS2 即時狀態</h1>
  <span class="dim" id="meta">連線中…</span>
</header>

<div class="card">
  <div class="row"><label>強度</label>
    <div style="flex:1" class="bar"><i id="strength-bar" style="background:var(--accent)"></i></div>
    <span class="num" id="strength-num">0 / 0</span></div>
  <div class="row"><label>血量</label>
    <div style="flex:1" class="bar"><i id="health-bar" style="background:var(--pink)"></i></div>
    <span class="num" id="health-num">0 / 100</span></div>
  <div class="row"><label>燃燒</label>
    <div style="flex:1" class="bar"><i id="burning-bar" style="background:var(--orange)"></i></div>
    <span class="num" id="burning-num">0 / 255</span></div>
</div>

<div class="card">
  <div class="grid">
    <div class="kv"><span>狀態</span><b id="state">—</b></div>
    <div class="kv"><span>疊加</span><b id="stack">0.0</b></div>
    <div class="kv"><span>連殺</span><b id="streak">0</b></div>
    <div class="kv"><span>輸出</span><b id="output">未出力</b></div>
    <div class="kv"><span>波形</span><b id="wave" style="font-size:15px">—</b></div>
  </div>
  <div id="event" style="margin-top:12px"></div>
</div>

<div class="card">
  <div class="grid">
    <div class="kv"><span>擊殺 / 死亡</span><b id="kd">0 / 0</b></div>
    <div class="kv"><span>K/D</span><b id="ratio">0.00</b></div>
    <div class="kv"><span>最佳連殺</span><b id="best">0</b></div>
    <div class="kv"><span>受擊</span><b id="hits">0</b></div>
    <div class="kv"><span>承受傷害</span><b id="damage">0</b></div>
    <div class="kv"><span>最高強度</span><b id="maxstrength">0</b></div>
    <div class="kv"><span>平均強度</span><b id="avg">0.0</b></div>
    <div class="kv"><span>通電時間</span><b id="shock">0:00</b></div>
  </div>
</div>

<p class="dim" style="font-size:12px;margin:0">
  唯讀頁面：這裡只能看，不能控制。<a class="accent" href="/overlay">OBS 疊層</a>
  ・ <a class="accent" href="/api/status">JSON</a>
</p>
</div>

<script>
const REFRESH = {refresh_ms};
function set(id, text){{ document.getElementById(id).textContent = text; }}
function bar(id, ratio, colour){{
  const el = document.getElementById(id);
  el.style.width = Math.max(0, Math.min(1, ratio)) * 100 + '%';
  if (colour) el.style.background = colour;
}}
async function tick(){{
  try {{
    const r = await fetch('/api/status', {{cache:'no-store'}});
    const s = await r.json();
    const max = s.max_strength || 1;
    const ratio = s.strength / max;
    const colour = ratio >= .8 ? 'var(--bad)' : ratio >= .55 ? 'var(--warn)' : 'var(--accent)';
    bar('strength-bar', ratio, colour);
    bar('health-bar', s.health / 100);
    bar('burning-bar', s.burning / 255);
    set('strength-num', s.strength + ' / ' + max);
    set('health-num', s.health + ' / 100');
    set('burning-num', s.burning + ' / 255');
    set('state', s.state + (s.spectating ? '（觀戰）' : ''));
    set('stack', (s.stack > 0 ? '+' : '') + s.stack.toFixed(1));
    set('streak', String(s.streak));
    set('output', s.output_active ? '● 出力中' : '○ 未出力');
    set('wave', s.wave || '—');
    set('event', s.last_event || '');
    const st = s.stats || {{}};
    set('kd', (st.kills||0) + ' / ' + (st.deaths||0));
    set('ratio', (st.kd||0).toFixed(2));
    set('best', String(st.best_streak||0));
    set('hits', String(st.hits||0));
    set('damage', String(st.damage_taken||0));
    set('maxstrength', String(st.max_strength||0));
    set('avg', (st.average_strength||0).toFixed(1));
    set('shock', st.duration || '0:00');
    const dot = document.getElementById('dot');
    dot.style.background = !s.paired ? 'var(--warn)'
      : (s.in_game ? 'var(--ok)' : 'var(--dim)');
    document.getElementById('meta').textContent =
      (s.paired ? 'App 已配對' : '等待配對') + '・' + (s.in_game ? '遊戲中' : '待機') +
      '・v' + (s.version || '?');
  }} catch (e) {{
    document.getElementById('meta').textContent = '連不上程式…';
  }}
}}
tick();
setInterval(tick, REFRESH);
</script>
</body></html>
"""


def _overlay_html(*, refresh_ms: int, title: str) -> str:
    """給 OBS 瀏覽器來源用的透明疊層。"""
    return f"""<!doctype html>
<html lang="zh-Hant"><head>
<meta charset="utf-8">
<title>{html.escape(title)} Overlay</title>
<style>{_BASE_CSS}
html,body{{background:transparent}}
body{{display:flex;align-items:flex-end;padding:12px}}
#box{{background:rgba(15,15,23,.72);border:1px solid var(--border);
  border-radius:12px;padding:12px 16px;min-width:250px;
  backdrop-filter:blur(6px)}}
#top{{display:flex;align-items:baseline;gap:10px}}
#num{{font-size:46px;font-weight:800;color:var(--accent);
  line-height:1;font-variant-numeric:tabular-nums}}
#of{{font-size:14px;color:var(--dim)}}
#bars{{margin-top:10px;display:grid;gap:6px}}
#row{{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--dim)}}
#row .bar{{flex:1;height:8px;min-width:120px}}
#meta{{margin-top:8px;font-size:12px;color:var(--dim)}}
</style></head>
<body><div id="box">
  <div id="top"><span id="num">0</span><span id="of">/ 0</span>
    <span id="meta" style="margin:0 0 0 auto"></span></div>
  <div id="bars">
    <div id="row"><span style="width:30px">血量</span>
      <div class="bar"><i id="health-bar" style="background:var(--pink)"></i></div></div>
    <div id="row"><span style="width:30px">燃燒</span>
      <div class="bar"><i id="burning-bar" style="background:var(--orange)"></i></div></div>
  </div>
</div>
<script>
const REFRESH = {refresh_ms};
async function tick(){{
  try {{
    const r = await fetch('/api/status', {{cache:'no-store'}});
    const s = await r.json();
    const max = s.max_strength || 1;
    const ratio = s.strength / max;
    const num = document.getElementById('num');
    num.textContent = s.strength;
    num.style.color = ratio >= .8 ? 'var(--bad)' : ratio >= .55 ? 'var(--warn)' : 'var(--accent)';
    document.getElementById('of').textContent = '/ ' + max;
    document.getElementById('health-bar').style.width = s.health + '%';
    document.getElementById('burning-bar').style.width = (s.burning / 255 * 100) + '%';
    document.getElementById('meta').textContent =
      s.paired ? (s.in_game ? '遊戲中' : '待機') : '等待配對';
  }} catch (e) {{}}
}}
tick();
setInterval(tick, REFRESH);
</script>
</body></html>
"""


# ------------------------------------------------------------------ 伺服器


@dataclass
class DashboardOptions:
    """執行時用的監看伺服器設定（由 :class:`config.DashboardConfig` 轉過來）。"""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8770
    token: str = ""
    refresh_ms: int = DEFAULT_REFRESH_MS
    title: str = "DGLAB-CS2"


class _Server(ThreadingHTTPServer):
    """``allow_reuse_address = False`` 是刻意的。

    ``http.server`` 預設會開 ``SO_REUSEADDR``。在 Linux 上那只是讓重啟快一點，
    但在 **Windows 上它的語意是「可以搶佔同一個埠」** —— 開兩份程式時兩個都會
    綁成功，連線卻隨機送到其中一個，使用者只會覺得「監看頁面壞了」。
    關掉之後第二個實例會直接拿到「位址已被使用」，比較好懂。
    """

    allow_reuse_address = False
    daemon_threads = True


class DashboardServer:
    """唯讀的狀態網頁伺服器。

    由 :class:`engine.ControlEngine` 在啟動時一起拉起來、停止時一起關掉。
    所有網路 I/O 都在背景執行緒，不會擋到控制迴圈。
    """

    def __init__(
        self,
        provider: Callable[[], dict[str, Any]],
        config: DashboardOptions,
        *,
        event_provider: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> None:
        self._provider = provider
        self._event_provider = event_provider
        self._config = config
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ 生命週期

    @property
    def config(self) -> DashboardOptions:
        return self._config

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def url(self) -> str:
        """給使用者複製的網址（綁 0.0.0.0 時換成比較好懂的說法）。"""
        host = self._config.host
        if host in ("0.0.0.0", "::", ""):
            host = _guess_lan_address()
        shown = f"{host}:{self._config.port}"
        query = f"?token={self._config.token}" if self._config.token else ""
        return f"http://{shown}/{query}"

    def start(self) -> bool:
        """啟動伺服器；已經在跑或失敗時回傳 False（不拋例外）。"""
        if self._server is not None:
            return False
        handler = _make_handler(self)
        try:
            server = _Server((self._config.host, self._config.port), handler)
        except OSError as exc:
            logger.error("無法啟動監看伺服器（%s:%s）：%s", self._config.host, self._config.port, exc)
            return False
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, name="dglab-dashboard", daemon=True
        )
        self._thread.start()
        logger.info("監看伺服器已啟動：%s", self.url)
        return True

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        try:
            server.shutdown()
            server.server_close()
        except OSError:  # pragma: no cover - 關閉時的競態
            logger.debug("關閉監看伺服器時發生錯誤", exc_info=True)
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        logger.info("監看伺服器已停止")

    def __enter__(self) -> "DashboardServer":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    # ------------------------------------------------------------ 給 handler

    def status(self) -> dict[str, Any]:
        try:
            payload = self._provider()
        except Exception:  # noqa: BLE001 - 監看頁壞掉不該影響主程式
            logger.exception("取得監看狀態時發生錯誤")
            payload = {"ok": False}
        payload["ok"] = True
        return payload

    def events(self) -> list[dict[str, Any]]:
        if self._event_provider is None:
            return []
        try:
            return list(self._event_provider())
        except Exception:  # noqa: BLE001
            return []

    def authorised(self, query: dict[str, list[str]]) -> bool:
        """檢查通關密語；沒設 token 就一律放行。"""
        expected = self._config.token
        if not expected:
            return True
        values = query.get("token") or []
        return bool(values) and values[0] == expected


def _guess_lan_address() -> str:
    """猜本機在區域網路的 IP（只是為了顯示好看的網址）。"""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"


def _make_handler(dashboard: DashboardServer):
    """建立 HTTP handler 類別（把 dashboard 綁進去）。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "DGLAB-CS2-dashboard"
        protocol_version = "HTTP/1.1"

        # 預設的 log_message 會把每個請求都印到 stderr，太吵
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.debug("監看頁 %s - %s", self.address_string(), fmt % args)

        # -------------------------------------------------------- 工具

        def _send(self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # 瀏覽器很常中途斷線（切分頁、按重新整理），不是錯誤
                logger.debug("監看頁的連線被對方關閉")

        def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            self._send(
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
                status,
            )

        def _send_html(self, body: str) -> None:
            self._send(body.encode("utf-8"), "text/html; charset=utf-8")

        def _deny(self) -> None:
            self._send(
                "<!doctype html><meta charset='utf-8'>"
                "<body style='background:#0f0f17;color:#e8e8f4;font-family:sans-serif;"
                "padding:40px'><h1>401 需要通關密語</h1>"
                "<p>請在網址後面加上 <code>?token=你的密語</code>。</p>",
                "text/html; charset=utf-8",
                HTTPStatus.UNAUTHORIZED,
            )

        # -------------------------------------------------------- 路由

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)

            if path == "/healthz":
                self._send_json({"ok": True})
                return

            if not dashboard.authorised(query):
                self._deny()
                return

            if path == "/":
                self._send_html(
                    _dashboard_html(
                        refresh_ms=dashboard.config.refresh_ms,
                        title=dashboard.config.title,
                    )
                )
            elif path == "/overlay":
                self._send_html(
                    _overlay_html(
                        refresh_ms=dashboard.config.refresh_ms,
                        title=dashboard.config.title,
                    )
                )
            elif path == "/api/status":
                self._send_json(dashboard.status())
            elif path == "/api/events":
                self._send_json({"ok": True, "events": dashboard.events()})
            else:
                self._send_json(
                    {"ok": False, "error": "找不到這個路徑"}, HTTPStatus.NOT_FOUND
                )

        do_HEAD = do_GET

    return Handler


__all__ = [
    "DEFAULT_REFRESH_MS",
    "DashboardOptions",
    "DashboardServer",
    "MAX_EVENTS",
    "STATUS_FIELDS",
    "build_status_payload",
]
