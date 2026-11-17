"""实盘面板 —— 一个窗口，不是网页。

双击桌面快捷方式（或 ``panel.bat``）打开。窗口里只有三件事：账户现在长什么样、
今天它做了什么、这一笔成交怎么记。持仓与成交都由你自己录——不再读券商窗口，
因为读屏那套在换机器、改缩放、客户端更新之后就不再可靠，而一次读错会把止损
锚到错的价格上。

录一笔成交会自动算佣金、印花税与过户费，同时更新持仓与可用资金；也可以直接改
持仓表。两条路写的是同一份快照，所以不会出现"表里 200 股、账上 0 股"。

为什么是 webview 而不是 tkinter：这张表要放十个数字列、金额右对齐、深浅色跟随
系统，用原生控件做出来是 2010 年的样子。界面仍然是本地的 HTML 文件，不联网、
不监听端口、不需要浏览器——``webview`` 只是把 Edge 的内核装进这个窗口。

Python 侧（``Bridge``）只做搬运和落库：判断在 ``service/trading/`` 里，那是
唯一一份，CLI、服务端和这个窗口共用。
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

# Every other entry point loads this; the window did not, so `.env` settings
# (FILL_MODE, REALTIME_QUOTES, the quote key) silently did not apply inside it.
load_dotenv(ROOT / ".env")

from service.kernel.clock import SHANGHAI

UI = Path(__file__).resolve().parent / "panel_ui"
LOG = ROOT / "data" / "panel.log"

# 窗口现在装的是交易台前端（与浏览器里 /desk 同一份代码），不是第二套界面。
# 为什么改：看盘、看内核、看个股和"录实盘"是同一个人在同一分钟里做的事，
# 两套界面必然漂。旧面板作为逃生舱保留： panel.pyw legacy
DESK_URL = (os.getenv("PANEL_URL") or "http://127.0.0.1:8811/desk/").rstrip("/") + "/"
HEALTH_URL = DESK_URL.replace("/desk/", "/health")


def log(message: str) -> None:
    """A windowed process has no console; without this a crash is invisible."""
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"[{datetime.now(SHANGHAI):%Y-%m-%d %H:%M:%S}] {message}\n")
    except OSError:
        pass


class Bridge:
    """What the page can call. Every method returns JSON-able data or raises."""

    def __init__(self) -> None:
        self._desk = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ desk
    def _desk_obj(self):
        with self._lock:
            if self._desk is None:
                from service.trading import build_desk

                log("building desk")
                self._desk = build_desk(ROOT)
                log("desk ready")
            return self._desk

    def warm(self) -> None:
        try:
            self._desk_obj()
        except Exception:  # noqa: BLE001
            log("warm-up failed:\n" + traceback.format_exc())

    def report_error(self, text: str = "") -> bool:
        """页面里的未捕获异常落到 panel.log。

        窗口没有控制台，`PANEL_DEBUG=1` 才开的红条又只在手工调试时存在。
        没有这条路时，"页面某处报错"只能靠人盯着屏幕发现；有了它，
        `data/panel.log` 里会多一行 `[web] ...`，出问题能倒查。
        """
        try:
            log("[web] " + str(text or "")[:2000])
            return True
        except Exception:  # noqa: BLE001 - 记日志失败不该反向影响页面
            return False

    def _prices(self, symbols: list[str]) -> dict[str, float]:
        try:
            quotes = self._desk_obj().quotes(symbols)
            return {symbol: float(quote.last) for symbol, quote in quotes.items()
                    if getattr(quote, "last", None)}
        except Exception:  # noqa: BLE001 - no quote is a normal state, not an error
            return {}

    # ------------------------------------------------------------------- api
    def bootstrap(self) -> dict:
        from service.trading import deskview

        today = datetime.now(SHANGHAI).strftime("%Y%m%d")
        day = today
        try:
            day = deskview.day_of(self._desk_obj())
        except Exception:  # noqa: BLE001 - the window still has to open
            log("bootstrap without desk:\n" + traceback.format_exc())
        # `day` is the newest published bar — yesterday, for most of the session.
        # A snapshot you type in at 13:30 describes *today*, so the form defaults
        # to `today`; using `day` there wrote this afternoon's holdings onto
        # yesterday's date and left the panel reading a different day than the
        # reconciler.
        return {"day": day, "today": today,
                "now": datetime.now(SHANGHAI).isoformat(timespec="seconds")}

    def account(self, with_quotes: bool = True) -> dict | None:
        """The account view. ``with_quotes=False`` answers in milliseconds.

        The window paints twice: once from the snapshot (which already carries
        the broker's market value) and again when the vendor answers. Waiting
        for the vendor first meant eight seconds of grey placeholders on the
        first call of the day.
        """
        from service.trading import deskview

        return deskview.account(self._desk_obj(), root=ROOT,
                                with_quotes=bool(with_quotes))

    def events(self) -> list[dict]:
        from service.trading import deskview

        desk = self._desk_obj()
        return deskview.events(desk, deskview.day_of(desk))

    def system(self) -> dict:
        from service.trading import deskview

        desk = self._desk_obj()
        return deskview.system(desk, deskview.day_of(desk), root=ROOT)

    # ---------------------------------------------------------------- manual
    def preview(self, payload: dict | None = None) -> dict:
        payload = payload or {}
        from service.trading import manual

        positions = payload.get("positions") or []
        prices = self._prices([str(row.get("symbol") or "").strip()
                               for row in positions if row.get("symbol")])
        return manual.prepare(as_of=payload.get("as_of") or None, positions=positions,
                              total=payload.get("total"), cash=payload.get("cash"),
                              market_value=payload.get("market_value"), prices=prices)

    def commit(self, payload: dict | None = None) -> dict:
        payload = payload or {}
        from service.trading import manual
        from service.trading.external import ExternalStore

        store = ExternalStore(ROOT / "data" / "external.sqlite")
        plan = self.preview(payload)
        if not plan.get("ok"):
            return {"ok": False, "problems": plan.get("problems") or [],
                    "message": "校验没通过，没有写入"}
        result = manual.persist(store, plan)
        log(f"manual commit: {result}")
        return {**result, "problems": [], "warnings": plan.get("warnings") or []}

    def add_trade(self, row: dict | None = None) -> dict:
        """Book a fill: fees computed, position updated, ledger appended."""
        from service.trading import manual
        from service.trading.external import ExternalStore

        store = ExternalStore(ROOT / "data" / "external.sqlite")
        result = manual.apply_trade(store, row or {})
        log(f"add_trade: {result.get('message')}")
        return result

    def quote_fee(self, side: str = "BUY", price: float = 0, qty: int = 0) -> dict:
        """What this fill will cost, before it is booked."""
        from service.trading import fees

        try:
            return fees.compute(side, float(price or 0), int(float(qty or 0)))
        except (TypeError, ValueError):
            return {}

    # --------------------------------------------------------------- reading
    def parse_text(self, text: str | None = None) -> dict:
        from scripts import external

        return external.parse_text(str(text or ""))

    def import_text(self, text: str | None = None) -> dict:
        from scripts import external
        from service.trading.external import ExternalStore

        parsed = external.parse_text(str(text or ""))
        if not parsed.get("ok"):
            return {"ok": False, "message": parsed.get("message") or "解析失败"}
        store = ExternalStore(ROOT / "data" / "external.sqlite")
        result = external.apply_parsed(store, parsed, source="manual:paste")
        log(f"import_text: {result}")
        return result


def _fatal(message: str) -> None:
    log("fatal: " + message)
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, message, "quant-desk 面板", 0x10)
    except Exception:  # noqa: BLE001, S110 - nothing left to report to
        pass


def _dpi_aware() -> None:
    """Without this the window is a 1.5x-stretched bitmap on a scaled display.

    An unaware process asks Windows for "1320 by 860", Windows renders it at
    150% and hands back a window whose content is cut off at the screen edge —
    which is exactly how the first build of this window looked.
    """
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor
    except Exception:  # noqa: BLE001 - older Windows
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001, S110 - old Windows, nothing to fix
            pass


def _workarea() -> tuple[int, int]:
    """任务栏之外的工作区，**逻辑像素**（pywebview 会再乘一次缩放）。

    这台机器实测（2026-11-14 自检，当时窗口处于最大化）：物理 2560×1600、
    缩放 150%、工作区 1707×1019 逻辑，页面量到 viewport 1707×996、dpr 1.5
    ——即 CSS 宽度就是 1707，不是 1138。

    这里原来写着"物理 1707×1067 → 逻辑 1138×679"，是把物理尺寸连除两次缩放：
    1707 本身就是逻辑值。按那段注释去改窗口尺寸会开小一半。真正要防的是相反
    的方向——按物理像素（2560）写尺寸，窗口会比屏幕宽，右边被 Windows 裁掉。
    """
    import ctypes

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    try:
        scale = ctypes.windll.shcore.GetScaleFactorForDevice(0) / 100.0
    except Exception:  # noqa: BLE001 - 老系统没有 shcore
        scale = 1.0
    if scale <= 0:
        scale = 1.0

    rect = RECT()
    ok = ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
    if ok:
        w = (rect.right - rect.left) / scale
        h = (rect.bottom - rect.top) / scale
    else:
        w = ctypes.windll.user32.GetSystemMetrics(0) / scale
        h = ctypes.windll.user32.GetSystemMetrics(1) / scale
    return int(w), int(h)


def _window_state_path() -> Path:
    return ROOT / "data" / "panel_window.json"


def _load_window_state() -> dict:
    """上次关闭时的窗口。没有就用最大化 —— 一屏信息量是这台子的第一需求。"""
    import json

    try:
        raw = json.loads(_window_state_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 第一次打开、文件坏了，都当没有
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key in ("width", "height", "x", "y"):
        val = raw.get(key)
        if isinstance(val, (int, float)) and val > 0:
            out[key] = int(val)
    if raw.get("maximized"):
        out["maximized"] = True
    return out


def _save_window_state(window) -> None:
    """记住尺寸，不是记住像素 —— 下次换屏工作时不该还原到旧屏的坐标。"""
    import json

    work_w, work_h = _workarea()
    try:
        state = {
            "width": int(window.width or 0),
            "height": int(window.height or 0),
            "x": int(window.x or 0),
            "y": int(window.y or 0),
        }
    except Exception:  # noqa: BLE001 - 窗口已经销毁
        return
    # 铺满工作区（差 40px 以内）就当最大化：最大化时 pywebview 报的是内容尺寸。
    if state["width"] >= work_w - 40 and state["height"] >= work_h - 40:
        state = {"maximized": True}
    try:
        _window_state_path().parent.mkdir(parents=True, exist_ok=True)
        _window_state_path().write_text(json.dumps(state), encoding="utf-8")
    except Exception:  # noqa: BLE001 - 记不住就算了，不该因此打不开窗口
        log("could not save window state: " + traceback.format_exc())


def _service_alive(timeout: float = 1.2) -> bool:
    """服务在不在。窗口第一屏依赖它，先问再开，比开出一片白屏好。"""
    import urllib.request

    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 - 连不上就是没起来
        return False


def _start_service(wait: float = 10.0) -> bool:
    """把本地服务拉起来（只在它没跑时）。计划任务平时负责保活，这里兜底。"""
    import subprocess
    import time

    python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        return False
    log("starting service")
    try:
        subprocess.Popen(
            [str(python), "-m", "uvicorn", "service.api.app:app",
             "--host", "127.0.0.1", "--port", os.getenv("QUANT_PORT", "8811")],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:  # noqa: BLE001
        log("service start failed:\n" + traceback.format_exc())
        return False
    deadline = time.time() + wait
    while time.time() < deadline:
        if _service_alive(0.8):
            return True
        time.sleep(0.5)
    return False


def _desk_url(wanted_tab: str) -> str:
    """口令放在 fragment 里：不进 HTML、不进服务器日志、不发 Referer。

    WebView2 对 file:// 上的查询串会报 ERR_FILE_NOT_FOUND，所以 tab 也走 fragment。
    """
    token = os.getenv("QUANT_TOKEN", "")
    fragment = "#/" + (wanted_tab or "today")  # ?tab=s/600519.SH 也走得通
    if token:
        from urllib.parse import quote

        fragment += "?t=" + quote(token, safe="")
    return DESK_URL + fragment


def _selftest(window, tag: str = "") -> None:
    """PANEL_SELFTEST=1 时把窗口里的真实读数写进 panel.log。

    这个进程没有控制台，也没有人能替我截图，所以"到底渲染出来了没有"
    只能问页面自己：导航几项、面板几个、K 线容器在不在、背景色是什么。
    """
    import json

    probes = {
        "href": "location.href.replace(/(t=)[^&]+/, '$1<redacted>')",
        "navItems": "document.querySelectorAll('.nav-item').length",
        "panels": "document.querySelectorAll('.panel').length",
        "hasKline": "!!document.querySelector('.kline-host')",
        "bodyBg": "getComputedStyle(document.body).backgroundColor",
        "viewport": "`${innerWidth}x${innerHeight}`",
        "dpr": "devicePixelRatio",
        # 屏幕与窗口分开记：只有一个 viewport 时，分不清"窗口开这么大"还是
        # "窗口被裁到只剩这么大"，而这两件事的修法完全不同。
        "screen": "`${screen.width}x${screen.height}`",
        "outer": "`${outerWidth}x${outerHeight}`",
        "split": "(getComputedStyle(document.querySelector('.stock-split')||document.body).gridTemplateColumns||'').slice(0,40)",
        "rail": "Math.round((document.querySelector('.stock-rail')||{getBoundingClientRect:()=>({width:0})}).getBoundingClientRect().width)",
        "title": "document.title",
        "gateVisible": "!!document.querySelector('.gate-card input')",
        "klineCanvas": "!!document.querySelector('.kline-canvas')",
        "extText": "(document.querySelector('#ext-positions')?.textContent || '').trim().slice(0, 36)",
        "errors": "window.__qdErrors ? window.__qdErrors.join(' | ').slice(0, 200) : ''",
    }
    out = {}
    for key, expr in probes.items():
        try:
            out[key] = window.evaluate_js(expr)
        except Exception as exc:  # noqa: BLE001
            out[key] = "ERR " + type(exc).__name__ + ": " + str(exc)
    log("selftest" + (f"[{tag}]" if tag else "") + ": " + json.dumps(out, ensure_ascii=False))


def main() -> None:
    """外壳：pythonw 没有控制台，未捕获的异常 = 窗口不开、日志没有、用户一脸懵。

    所以真正的流程放 _main，这里兜底：任何逃逸的异常都写进日志并弹一条能看懂的话。
    2026-11-07 的教训：状态文件 {"maximized": true} 触发 KeyError，三次双击无声死。
    """
    try:
        _main()
    except Exception:  # noqa: BLE001 - 到这里都没有兜底的错，必须留全尸
        detail = traceback.format_exc()
        log("crash:\n" + detail)
        _fatal("窗口启动失败，请把 data/panel.log 里最后一段 'crash:' 发给维护者。\n\n"
               + detail.splitlines()[-1])


def _main() -> None:
    log("panel starting")
    _dpi_aware()
    legacy = any(arg.lower() == "legacy" for arg in sys.argv[1:])

    extra = next((arg for arg in sys.argv[1:] if arg.startswith("?")), "")
    wanted_tab = ""
    if extra.startswith("?"):
        from urllib.parse import parse_qs

        wanted_tab = (parse_qs(extra.lstrip("?")).get("tab") or [""])[0]

    try:
        import webview
    except ImportError:
        _fatal("缺少 pywebview，窗口打不开。\n\n"
               "在项目目录执行：\n"
               "uv pip install --python .venv/Scripts/python.exe pywebview\n\n"
               "或者直接跑命令行版本：python scripts/sync_today.py")
        return

    if legacy:
        index = UI / "index.html"
        if not index.exists():
            _fatal(f"旧界面文件不见了：{index}")
            return
        url = index.as_uri()
    else:
        if not _service_alive():
            if not _start_service():
                _fatal("本地服务没有起来，窗口打不开。\n\n"
                       "手动启动：\n"
                       ".venv\\Scripts\\python.exe -m uvicorn service.api.app:app "
                       "--host 127.0.0.1 --port 8811\n\n"
                       "或打开旧面板：panel.pyw legacy")
                return
        url = _desk_url(wanted_tab)

    bridge = Bridge()
    threading.Thread(target=bridge.warm, daemon=True).start()

    debug = os.getenv("PANEL_DEBUG", "") not in ("", "0")
    # 窗口尺寸只说逻辑像素，且以工作区为上限：这台机器逻辑是 1707×1019，
    # 按物理像素（2560）开会比屏幕还宽，右边直接被裁掉。
    work_w, work_h = _workarea()
    state = _load_window_state()
    # setdefault 无条件补齐：状态文件里可能只有 {"maximized": true}（最大化时不记尺寸），
    # 而 create_window 的 kwargs 必须有这些键 —— 否则 KeyError，pythonw 静默死。
    # 最大化时宽高只是占位，pywebview 不会用它们摆窗口。
    state.setdefault("width", work_w - 24)
    state.setdefault("height", work_h - 24)
    state.setdefault("x", 0)
    state.setdefault("y", 0)

    window = webview.create_window(
        "quant-desk · 实盘" if legacy else "quant-desk · 量化台",
        url=url,
        js_api=bridge,
        width=state["width"],
        height=state["height"],
        x=state.get("x"),
        y=state.get("y"),
        maximized=bool(state.get("maximized")),
        resizable=True,
        min_size=(1024, 640),
        background_color="#f6f7f9" if legacy else "#0B0D10",
        text_select=True,
    )
    window.events.closing += lambda: _save_window_state(window)

    if legacy and wanted_tab in ("live", "sync", "acts", "system"):
        def _select_tab() -> None:
            try:
                webview.windows[0].evaluate_js(f"showTab('{wanted_tab}')")
            except Exception:  # noqa: BLE001 - a tab is not worth a crash
                log("could not select tab: " + traceback.format_exc())

        window.events.loaded += _select_tab

    if os.getenv("PANEL_SELFTEST", "") not in ("", "0"):
        # loaded 在 DOM 就绪时就触发，而界面还要等一次 fetch 才画出来。
        # 立刻探针只会读到 0 个面板 —— 那是探针太早，不是页面没渲染。
        def _later() -> None:
            # 两次采样：5 秒看"壳"（导航/面板），12 秒看"数据到了没有"。
            # 一次探针分不清"没渲染"和"还在取数"，那是两种完全不同的故障。
            for delay in (5.0, 12.0):
                timer = threading.Timer(delay, lambda d=delay: _selftest(window, tag=f"t{d:.0f}s"))
                timer.daemon = True
                timer.start()

        window.events.loaded += _later

    try:
        webview.start(debug=debug, private_mode=False,
                      storage_path=str(ROOT / "data" / "panel_webview"))
    except Exception as exc:  # noqa: BLE001 - a black window is worse than a message
        log("webview failed:\n" + traceback.format_exc())
        _fatal(f"窗口启动失败：{type(exc).__name__}: {exc}\n\n详情见 {LOG}")
    finally:
        log("panel closed")
        del window


if __name__ == "__main__":
    main()
