#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
内网订单时效看板服务。

  python serve.py                 # 默认 0.0.0.0:8000，同事用 http://<你的内网IP>:8000 访问
  python serve.py --port 9000

能力
----
  GET  /                看板页面（含单号追踪抽屉、刷新、更新 Cookie）
  GET  /api/track       按运单号实时拉事件流，返回完整轨迹与各段时长
                        本机回环来源免口令（给本地静态看板的抽屉用），其它来源照旧要口令
  GET  /api/orders      当前缓存的订单明细 JSON
  GET  /api/status      会话与缓存状态
  POST /api/refresh     重新抓取（强制刷新缓存）
  POST /api/cookie      页面内更新 hk-sit Cookie
  POST /api/refresh-all   一键全量刷新：跑整条 refresh_all.py（抓数+三张清单+重写 dashboard.html）
                          仅本机回环可调（不校验口令），给本地静态看板的「刷新数据」按钮用
  POST /api/refresh-status 上面那个任务的进度

注意：本服务不内置登录，仅靠内网网络边界，请勿绑定到公网。
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import dashboard
import scraper as sc

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "orders.json"
ENV_FILE = BASE_DIR / ".env"
CACHE_TTL = int(os.getenv("OC_CACHE_TTL", "600"))
# 固定起始日（OC_START_DATE=2026/10/20）。设置后「刷新数据」从该日起抓至今，
# 不再用 OC_DAYS 回溯，保证与取消节点清单同一口径。留空则按 OC_DAYS 滚动窗口。
START_DATE = (os.getenv("OC_START_DATE") or "").strip()

# 访问口令。为空时保持原行为（仅靠内网边界）；设置后进入只读模式：
# 未认证不能看任何页面/接口，且刷新数据与更新 Cookie 的入口全部关闭。
# 支持多个口令（逗号或换行分隔），便于分发给不同的人、单独撤销：
#   OC_DASHBOARD_TOKEN=key-a,key-b,key-c
TOKENS: list[str] = []
# 外发口令：只给「脱敏视图」。与 TOKENS 同一套登录流程，但看的页面去掉了
# 收件人真名，接口也不再返回门牌地址（consigneeAddr）。
#   OC_DASHBOARD_TOKEN_EXT=ext-a,ext-b
EXT_TOKENS: list[str] = []
TOKEN = ""  # 兼容旧引用 = TOKENS[0]
READONLY = False
AUTH_COOKIE = "oc_auth"
# 明细接口对外时**必须**剔除的字段：完整门牌属于个人信息，页面不显示但接口不该带出去
PII_DROP_FIELDS = ("consigneeAddr",)
AUTH_LIFETIME = 12 * 3600
_FAIL_LOCK: dict[str, tuple[int, float]] = {}
_FAIL_LOCK_TTL = 300
_FAIL_MAX = 5


def parse_tokens(raw: str) -> list[str]:
    """逗号 / 换行 / 分号分隔，去空白、去重、保持顺序。"""
    out: list[str] = []
    for part in (raw or "").replace(";", ",").replace("\n", ",").split(","):
        t = part.strip()
        if t and t not in out:
            out.append(t)
    return out


def auth_sig(token: str) -> str:
    return hmac.new(token.encode("utf-8"), b"oc-timeliness-v1", hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------
class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.orders: list = []
        self.meta: dict = {}
        self.ts: float = 0.0
        self.mtime: float = 0.0
        self.loading = False
        self.error = ""
        self.cookie_invalid = False

    def snapshot(self):
        with self.lock:
            return list(self.orders), dict(self.meta), self.ts

    def age(self) -> float:
        return time.time() - self.ts if self.ts else -1


STORE = Store()


def log(msg: str):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 抓取 / 缓存
# ---------------------------------------------------------------------------
def load_snapshot() -> bool:
    """启动时先用磁盘快照，保证页面能立刻打开。"""
    if not DATA_FILE.exists():
        return False
    try:
        d = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log(f"快照读取失败：{exc}")
        return False
    with STORE.lock:
        STORE.orders = d.get("orders", [])
        # 旧快照的 meta 可能没有 outNodes（PUDO=PUP / LMS 出库节点登记），补默认值
        STORE.meta = dashboard._meta_with_defaults(d.get("meta", {}))
        STORE.ts = DATA_FILE.stat().st_mtime
        STORE.mtime = STORE.ts
    log(f"已载入磁盘快照 {len(STORE.orders)} 单")
    return True


def ensure_fresh():
    """快照被别的进程改过（如 K8S 的 CronJob 抓完写盘）就重新载入。

    页面与接口都走内存缓存，进程不会自己发现磁盘变了；每次请求前比一次 mtime 最省事。
    """
    try:
        m = DATA_FILE.stat().st_mtime
    except OSError:
        return
    if m > STORE.mtime + 1:
        log(f"检测到快照更新（{datetime.fromtimestamp(m):%Y-%m-%d %H:%M:%S}），重新载入")
        load_snapshot()


def date_range(days: int = 14) -> tuple[str, str]:
    """本次抓取的日期窗口。配了 OC_START_DATE 就固定从那天起，否则按 days 回溯。"""
    if START_DATE:
        return sc._norm_date(START_DATE), datetime.now(sc.TZ_CN).strftime("%Y/%m/%d 23:59:59")
    return sc._range(days, None, None)


def refresh_orders(days: int = 14) -> tuple[bool, str]:
    """同步抓一轮，写内存 + 磁盘。返回 (ok, error)。"""
    with STORE.lock:
        if STORE.loading:
            return False, "已有刷新任务在执行"
        STORE.loading = True
    t0 = time.time()
    try:
        if not sc.COOKIE:
            raise RuntimeError("缺少登录态：请在页面右上角或 .env 里配置 hk-sit Cookie")
        start, end = date_range(days)
        log(f"开始抓取 {start} ~ {end} …")
        orders = sc.fetch_real(start, end, 0)
        meta = sc.build_meta(start, end, orders, demo=False)
        sc.save(orders, meta)
        with STORE.lock:
            STORE.orders = orders
            STORE.meta = meta
            STORE.ts = time.time()
            try:
                STORE.mtime = DATA_FILE.stat().st_mtime
            except OSError:
                STORE.mtime = STORE.ts
            STORE.error = ""
            STORE.cookie_invalid = False
        log(f"抓取完成 {len(orders)} 单，用时 {time.time() - t0:.1f}s")
        return True, ""
    except Exception as exc:  # noqa: BLE001
        with STORE.lock:
            STORE.error = str(exc)
        log(f"抓取失败：{exc}")
        return False, str(exc)
    finally:
        with STORE.lock:
            STORE.loading = False


def refresh_async(days: int = 14):
    threading.Thread(target=refresh_orders, args=(days,), daemon=True).start()


# ---------------------------------------------------------------------------
# 一键全量刷新（给本地静态 dashboard.html 的按钮用）
# ---------------------------------------------------------------------------
# 和 refresh_orders 的区别：这个跑整条 refresh_all.py ——
# 抓数 → 取消清单 → 串场 → 投递取消异常 → 重新生成 dashboard.html。
# 静态文件点「刷新」要的就是这最后一件事，否则数据更新了页面还是旧的。
REFALL = {"running": False, "ok": None, "error": "", "started": 0.0,
          "finished": 0.0, "tail": ""}
REFALL_LOCK = threading.Lock()


def child_env() -> dict:
    """给刷新子进程准备环境：以**当前** .env 为准。

    serve.py 启动时 scraper 已把 .env 读进 os.environ，之后改 .env 不会回灌到本进程，
    而子进程是继承 os.environ 的 —— 结果「刷新数据」会沿用进程启动那一刻的区域/日期配置
    （实测踩过：.env 已改成 us + 10/19，服务是两小时前启的，刷新却抓回 UK+US、10/29 起）。
    所以这里按 .env 逐行覆盖，不依赖本进程内存里的旧值。
    """
    env = os.environ.copy()
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k:
                env[k] = v
    except FileNotFoundError:
        pass          # 容器里没有 .env，沿用 ConfigMap 注入的环境变量
    return env


def refresh_all_async() -> tuple[bool, str]:
    with REFALL_LOCK:
        if REFALL["running"]:
            return False, "已有刷新任务在执行"
        REFALL.update(running=True, ok=None, error="", started=time.time(),
                      finished=0.0, tail="")
    log("一键刷新：开始跑 refresh_all.py（约 3 分钟）")

    def run():
        try:
            p = subprocess.run([sys.executable, str(BASE_DIR / "refresh_all.py")],
                               cwd=str(BASE_DIR), capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=1800, env=child_env())
            out = (p.stdout or "") + (p.stderr or "")
            ok = p.returncode == 0
            with REFALL_LOCK:
                REFALL.update(ok=ok,
                              error="" if ok else (out.strip()[-400:] or f"退出码 {p.returncode}"),
                              tail=out.strip()[-1500:], finished=time.time())
            if ok:
                load_snapshot()          # 让内存里的快照也跟上，动态版立即看到新数
                log("一键刷新：完成，dashboard.html 已重新生成")
            else:
                log(f"一键刷新：失败（退出码 {p.returncode}）")
        except Exception as exc:  # noqa: BLE001
            with REFALL_LOCK:
                REFALL.update(ok=False, error=str(exc), finished=time.time())
            log(f"一键刷新：异常 {exc}")
        finally:
            with REFALL_LOCK:
                REFALL["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return True, ""


# ---------------------------------------------------------------------------
# Cookie
# ---------------------------------------------------------------------------
def probe_cookie(cookie: str) -> tuple[bool, str]:
    """用一次真实请求判断 Cookie 是否还有效。"""
    try:
        s = sc._session()
        s.headers["Cookie"] = cookie
        cfg = sc.region_cfg("us")
        params = {"page": 1, "limit": 1, "startDate": sc._range(1, None, None)[0],
                  "endDate": sc._range(1, None, None)[1]}
        if cfg["dest"]:
            params["destinationCountry"] = cfg["dest"]
        r = s.get(sc._url(sc.API["list"], cfg), params=params, timeout=25)
        d = r.json()
        if d.get("success") is False:
            return False, f"接口返回 {d.get('errorCode') or '未登录'}"
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def update_cookie(raw: str) -> tuple[bool, str]:
    cookie = (raw or "").strip().strip('"')
    if not cookie:
        return False, "Cookie 为空"
    if "hk-sit" not in cookie:
        cookie = "hk-sit=" + cookie

    ok, err = probe_cookie(cookie)
    if not ok:
        return False, f"Cookie 校验未通过：{err}"

    sc.COOKIE = cookie
    os.environ["OC_COOKIE"] = cookie

    # 容器里 /app 是只读镜像层，写 .env 会失败；此时只让内存生效（重启失效），
    # 正式环境请改 Secret —— 这里不因落盘失败而否定这次更新
    try:
        lines, hit = [], False
        if ENV_FILE.exists():
            for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("OC_COOKIE"):
                    lines.append(f"OC_COOKIE={cookie}")
                    hit = True
                else:
                    lines.append(line)
        if not hit:
            lines.append(f"OC_COOKIE={cookie}")
        ENV_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        log("Cookie 已更新并通过校验（已写入 .env）")
    except OSError as exc:
        log(f"Cookie 已更新并通过校验（.env 不可写，仅内存生效：{exc}）")

    with STORE.lock:
        STORE.cookie_invalid = False
    return True, ""


# ---------------------------------------------------------------------------
# 单号追踪
# ---------------------------------------------------------------------------
def _event_rows(events: list, cfg: dict, base_epoch: float | None = None) -> list[dict]:
    """把原始事件转成前端时间轴行，时间统一按 oprTimeWithZone 解析。

    deltaH 以订单创建时间为基准（拿不到时退回最早事件），表示「距建单过去了多久」，
    这样即使首个事件是 cancelled 之类的杂讯，累计时长仍然可读。
    """
    rdc_code = (cfg.get("rdc") or "").upper()
    spx_code = (cfg.get("spx") or "").upper()

    rows = []
    for ev in events or []:
        t = sc.event_time(ev)
        if not t:
            continue
        code = str(ev.get("jvEventCode") or "").strip()
        desc = ev.get("oprEnDes") or ev.get("oprCnDes") or ""
        vendor = ev.get("vendorName") or ev.get("vendorCode") or ""
        meta_bits = []
        if vendor:
            meta_bits.append(f"vendor {vendor}")
        raw = ev.get("oprTimeWithZone")
        if raw:
            meta_bits.append(f"原值 {raw}")
        rows.append({
            "code": code,
            "time": t.strftime("%Y-%m-%d %H:%M:%S"),
            "epoch": t.timestamp(),
            "desc": desc,
            "meta": " · ".join(meta_bits),
            "isRdc": code.upper() == rdc_code,
            "isSpx": code.upper() == spx_code,
        })
    rows.sort(key=lambda r: r["epoch"])
    if rows:
        base = base_epoch if base_epoch else rows[0]["epoch"]
        for r in rows:
            r["deltaH"] = round((r["epoch"] - base) / 3600.0, 2)
    return rows


def track(tn: str, region: str, external: bool = False) -> dict:
    tn = (tn or "").strip()
    if not tn:
        return {"ok": False, "error": "缺少运单号"}

    # 外发视图：收件人一律打码，说明文字里嵌的姓名同样处理
    blur = dashboard.mask_name if external else (lambda s: s)

    region = (region or "").strip().lower()
    cands = [region] if region in ("us", "uk", "au") else ["us", "uk", "au"]

    events, used_cfg = [], None
    for key in cands:
        cfg = sc.region_cfg(key)
        s = sc._session()
        evs = sc.fetch_events(s, tn, cfg)
        if evs:
            events, used_cfg = evs, cfg
            break

    if used_cfg is None:
        used_cfg = sc.region_cfg(cands[0])

    events_invalid = False
    if not events:
        ok, err = probe_cookie(sc.COOKIE)
        if not ok:
            with STORE.lock:
                STORE.cookie_invalid = True
            events_invalid = True

    cached = None
    with STORE.lock:
        for o in STORE.orders:
            if (o.get("lastMileWaybill") or "") == tn or (o.get("ocOrderNo") or "") == tn:
                cached = dict(o)
                break

    base_epoch = None
    if cached and cached.get("orderCreateTime"):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                base_epoch = datetime.strptime(cached["orderCreateTime"], fmt).replace(
                    tzinfo=sc.TZ_CN).timestamp()
                break
            except ValueError:
                continue

    rows = _event_rows(events, used_cfg, base_epoch)

    segments = {"createToAScan": None, "aScanToRdc": None, "rdcToSpx": None, "aScanToSpx": None}
    if cached:
        segments = {
            "createToAScan": cached.get("hCreateToAScan"),
            "aScanToRdc": cached.get("hAScanToRdc"),
            "rdcToSpx": cached.get("hRdcToSpx"),
            "aScanToSpx": cached.get("hAScanToSpx"),
        }
    elif rows:
        by = {}
        for r in rows:
            by.setdefault(r["code"].upper(), r)
        a = by.get("DOUS002") or (rows[0] if rows else None)
        rdc = by.get((used_cfg.get("rdc") or "").upper())
        spx = by.get((used_cfg.get("spx") or "").upper())
        h = lambda x, y: round((y["epoch"] - x["epoch"]) / 3600.0, 2) if x and y else None
        segments = {
            "createToAScan": None,
            "aScanToRdc": h(a, rdc),
            "rdcToSpx": h(rdc, spx),
            "aScanToSpx": h(a, spx),
        }

    notes = []
    if cached and cached.get("isTest"):
        if external:
            # 外发视图不落地址；没有姓名时退回中性说法，不打码「占位信息」这种词
            c0 = ((cached or {}).get("consignee") or "").strip()
            who = dashboard.mask_name(c0) if c0 else "占位信息"
        else:
            who = cached.get("consignee") or cached.get("consigneeAddr") or "占位信息"
        notes.append(f"该单判定为测试单（{who}）。追踪可用，但不要纳入时效统计。")
    if cached and segments.get("rdcToSpx") == 0:
        notes.append("出库与 SPX 提取在同一分钟内上报，0.0h 是真实上报结果，不能当作实际作业时长。")
    if cached and cached.get("outMixed"):
        notes.append(f"同一单被两家各报了一次出库（{cached.get('outVendors')}），"
                     f"RDC 归属按更早那条（{cached.get('vendor')}）计。")
    if not cached:
        notes.append("该运单不在当前缓存范围内，各段时长由事件流现算，可能与看板汇总口径略有差异。")
    if events_invalid:
        notes.append("OC 会话可能已失效，未能取到事件。")
    if cached and cached.get("anomaly"):
        notes.append("该单存在时间倒挂（事件时间早于建单），可能为批量导入回填，相关区间已置空。")

    return {
        "ok": True,
        "tn": tn,
        "region": used_cfg["code"],
        "order": {
            "ocOrderNo": (cached or {}).get("ocOrderNo") or tn,
            "vendor": (cached or {}).get("vendor") or "—",
            "vendorCode": (cached or {}).get("vendorCode") or "",
            "outNodeCode": (cached or {}).get("outNodeCode") or used_cfg.get("rdc") or "",
            "destState": (cached or {}).get("destState") or "",
            "consignee": blur((cached or {}).get("consignee") or ""),
            "isTest": bool((cached or {}).get("isTest")),
            "status": (cached or {}).get("status") or "",
        },
        "events": rows,
        # 接口原始事件：字段原样返回，给抽屉做「原始数据」视图（rows 是整理后的版本，保留兼容）
        "raw": sorted(events or [],
                      key=lambda e: int(e.get("oprTimestamp") or e.get("oprTime") or 0)),
        "eventCount": len(rows),
        "deltaBase": "create" if base_epoch else "firstEvent",
        "segments": segments,
        "notes": notes,
        "cookieInvalid": events_invalid,
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "OCTimeliness/1.0"

    def log_message(self, fmt, *args):  # 安静一点
        if "/api/" in (self.path or ""):
            log(f"{self.address_string()} {self.command} {self.path}")

    # -- helpers --
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 本地静态 dashboard.html 用 file:// 打开时 Origin 是 "null"，
        # 想让它的「刷新数据」按钮能调本机接口，必须放行跨域。
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _html(self, text: str):
        self._send(200, text.encode("utf-8"), "text/html; charset=utf-8")

    def _body_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _is_local(self) -> bool:
        """是不是真的坐在本机上点的（而不是从隧道/内网绕进来的）。

        三重判据，全部满足才算本机：
        1. 来源 IP 是回环
        2. 没有 CF / 反代头（cloudflared 也从 127.0.0.1 转发，但必定带这些头）
        3. Origin 为空 / "null"（file:// 双击打开）或是本机地址
           —— 挡掉「本机浏览器里开着的外部网页偷偷 POST 过来」这种 CSRF
        """
        ip = (self.client_address or ("",))[0]
        if ip not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            return False
        for h in ("CF-Connecting-IP", "CF-Ray", "X-Forwarded-For", "X-Real-IP"):
            if self.headers.get(h):
                return False
        origin = (self.headers.get("Origin") or "").strip().lower()
        if origin and origin != "null":
            if not origin.startswith(("http://127.0.0.1", "http://localhost",
                                      "http://[::1]", "https://127.0.0.1",
                                      "https://localhost", "https://[::1]")):
                return False
        return True

    def do_OPTIONS(self):
        """CORS 预检。本地静态页 fetch(POST application/json) 会先发这个。"""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- auth --
    def _cookie_val(self, name: str) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return ""

    def _client(self) -> str:
        return self.client_address[0] if self.client_address else "?"

    def _role(self) -> str:
        """当前请求的视图身份：'full' 内部全量 / 'ext' 外发脱敏 / '' 未认证。

        未配置任何口令时返回 'full'，保持原行为（靠内网边界）。
        """
        if not TOKENS and not EXT_TOKENS:
            return "full"
        v = self._cookie_val(AUTH_COOKIE)
        if not v:
            return ""
        # 两组都算完再判定，避免按口令位置泄露时序
        hit_full = any([hmac.compare_digest(v, auth_sig(t)) for t in TOKENS])
        hit_ext = any([hmac.compare_digest(v, auth_sig(t)) for t in EXT_TOKENS])
        if hit_full:
            return "full"
        return "ext" if hit_ext else ""

    def _authed(self) -> bool:
        return bool(self._role())

    def _locked_for(self) -> int:
        """连续输错后的剩余锁定秒数。"""
        rec = _FAIL_LOCK.get(self._client())
        if not rec:
            return 0
        n, ts = rec
        left = _FAIL_LOCK_TTL - (time.time() - ts)
        if left <= 0:
            _FAIL_LOCK.pop(self._client(), None)
            return 0
        return int(left) if n >= _FAIL_MAX else 0

    def _note_fail(self):
        n, ts = _FAIL_LOCK.get(self._client(), (0, time.time()))
        if time.time() - ts > _FAIL_LOCK_TTL:
            n, ts = 0, time.time()
        _FAIL_LOCK[self._client()] = (n + 1, time.time())

    def _guard(self, path: str) -> bool:
        """返回 True 表示已处理（拦截），调用方直接 return。"""
        if self._authed():
            return False
        if path.startswith("/api/"):
            self._json({"ok": False, "error": "需要访问口令"}, 401)
        else:
            self.send_response(302)
            self.send_header("Location", "/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
        return True

    def _login_page(self, err: str = "") -> str:
        hours = AUTH_LIFETIME // 3600
        return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>订单时效看板 · 登录</title>
<style>
  *{{box-sizing:border-box}}
  body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
    background:#f4f5f7;color:#1f2329;padding:20px;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}
  .box{{background:#fff;border:1px solid #e6e8ec;border-radius:12px;padding:27px 30px 20px;
    width:362px;max-width:100%;box-shadow:0 6px 24px rgba(31,35,41,.06)}}
  .hd{{display:flex;align-items:center;gap:9px}}
  .dot{{width:8px;height:8px;border-radius:50%;background:#2f6bff;flex:none}}
  h1{{font-size:16px;margin:0;font-weight:600;letter-spacing:.2px}}
  .sub{{color:#8a919b;font-size:12.5px;margin:7px 0 19px;line-height:1.55}}
  .lab{{font-size:12px;color:#5b6472;margin-bottom:6px}}
  .pw{{position:relative}}
  input{{width:100%;padding:9px 58px 9px 11px;border:1px solid #e6e8ec;border-radius:8px;
    font-size:14px;outline:none;font-family:inherit;transition:.15s;background:#fdfdfe}}
  input:focus{{border-color:#2f6bff;background:#fff;box-shadow:0 0 0 3px rgba(47,107,255,.09)}}
  .eye{{position:absolute;right:5px;top:50%;transform:translateY(-50%);border:0;
    background:transparent;color:#8a919b;font-size:12px;cursor:pointer;padding:5px 9px;
    border-radius:6px;font-family:inherit}}
  .eye:hover{{background:#f0f1f3;color:#1f2329}}
  .go{{width:100%;margin-top:14px;padding:9px;border:0;border-radius:8px;background:#2f6bff;
    color:#fff;font-size:13.5px;font-weight:500;cursor:pointer;font-family:inherit;transition:.15s}}
  .go:hover{{background:#2560ea}}
  .go:disabled{{opacity:.55;cursor:default}}
  .err{{color:#d62c2c;font-size:12.5px;margin:10px 0 0;min-height:17px;line-height:1.45}}
  .ft{{border-top:1px solid #f0f1f3;margin-top:13px;padding-top:11px;
    font-size:11.5px;color:#9aa2ac;line-height:1.65}}
</style></head><body>
<div class="box">
  <div class="hd"><span class="dot"></span><h1>订单时效看板</h1></div>
  <p class="sub">内部数据，凭访问口令进入。口令由看板管理员分发，请勿外传。</p>
  <div class="lab">访问口令</div>
  <div class="pw">
    <input id="t" type="password" placeholder="请输入口令" autocomplete="current-password"
           spellcheck="false"/>
    <button class="eye" id="eye" type="button">显示</button>
  </div>
  <button class="go" id="go" type="button">进入看板</button>
  <p class="err" id="err">{html.escape(err)}</p>
  <div class="ft">登录后 {hours} 小时内免输口令。本页为只读视图，不含刷新数据与配置入口。</div>
</div>
<script>
const t = document.getElementById('t'), go = document.getElementById('go'),
      err = document.getElementById('err'), eye = document.getElementById('eye');
eye.addEventListener('click', () => {{
  const show = t.type === 'password';
  t.type = show ? 'text' : 'password';
  eye.textContent = show ? '隐藏' : '显示';
  t.focus();
}});
let busy = false;
async function submit(){{
  if (busy) return;
  const v = t.value.trim();
  if (!v) {{ err.textContent = '请输入口令'; t.focus(); return; }}
  busy = true; go.disabled = true; go.textContent = '验证中…'; err.textContent = '';
  try {{
    const r = await fetch('/login', {{method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{token: v}})}});
    const d = await r.json().catch(() => ({{}}));
    if (r.ok && d.ok) {{ go.textContent = '已通过'; location.replace('/'); return; }}
    err.textContent = d.error || '口令不正确，请核对后重试';
    t.select();
  }} catch (e) {{
    err.textContent = '网络异常，请稍后重试';
  }}
  busy = false; go.disabled = false; go.textContent = '进入看板';
}}
go.addEventListener('click', submit);
t.addEventListener('keydown', e => {{ if (e.key === 'Enter') submit(); }});
t.focus();
</script></body></html>"""

    # -- routes --
    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        path = u.path.rstrip("/") or "/"

        # 容器探针专用：**必须放在鉴权之前**，否则被 _guard 拦成 302 登录页，
        # kubelet 会认为探针失败。只看内存里有没有快照，不暴露业务数据。
        if path in ("/healthz", "/readyz", "/health"):
            # 也要热重载：否则 CronJob 写盘后探针仍报旧单量，看监控会误判"数据没更新"
            ensure_fresh()
            orders, _meta, ts = STORE.snapshot()
            ok = bool(orders)
            self._json({
                "ok": ok,
                "orders": len(orders),
                "dataTs": ts,
                "dataTime": (datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
                             if ts else ""),
                "loading": STORE.loading,
                "error": STORE.error,
            }, 200 if ok else 503)
            return

        if path == "/login":
            if not (TOKENS or EXT_TOKENS) or self._authed():
                self.send_response(302)
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._html(self._login_page())
            return

        if path == "/logout":
            left = self._locked_for()
            self.send_response(302)
            self.send_header("Set-Cookie",
                             f"{AUTH_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
            self.send_header("Location", "/login" if (TOKENS or EXT_TOKENS) and not left else "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        # 本地静态 dashboard.html（file://）点运单号要看轨迹：本机来源免口令，
        # 判据与 /api/refresh-all 完全一致（回环 IP + 无反代头 + 本机/null Origin）。
        # 隧道与内网进来的请求带 CF-Connecting-IP，照旧走口令。
        if path == "/api/track" and self._is_local():
            pass
        elif self._guard(path):
            return

        if path in ("/", "/index.html"):
            ensure_fresh()
            orders, meta, ts = STORE.snapshot()
            if not orders:
                with STORE.lock:
                    loading, err = STORE.loading, STORE.error
                self._html(self._waiting_page(loading, err))
                if not loading:
                    refresh_async()
                return
            m = dict(meta)
            m["generatedAt"] = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            self._html(dashboard.render(m, orders, dynamic=True, readonly=READONLY,
                                        external=(self._role() == "ext")))
            return

        if path == "/api/orders":
            ensure_fresh()
            orders, meta, ts = STORE.snapshot()
            region = (qs.get("region", [""])[0] or "").upper()
            if region and region != "ALL":
                orders = [o for o in orders if o.get("region") == region]
            if self._role() == "ext":
                # 外发视图：门牌不返回、姓名打码，接口层就拦住，不依赖前端
                orders = dashboard.mask_orders(orders)
                meta = dict(meta, external=True)
            self._json({"ok": True, "meta": meta, "orders": orders,
                        "cachedAt": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""})
            return

        if path == "/api/track":
            tn = qs.get("tn", [""])[0]
            region = qs.get("region", [""])[0]
            try:
                self._json(track(tn, region, external=(self._role() == "ext")))
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "error": f"查询异常：{exc}"}, 500)
            return

        if path in ("/stuck", "/stuck_shipcc.html"):
            # 优先读数据目录：K8S 里由 CronJob 生成到共享卷，容器根目录是只读镜像层
            cands = [BASE_DIR / "data" / "stuck_shipcc.html",
                     BASE_DIR / "stuck_shipcc.html"]
            f = next((p for p in cands if p.exists()), None)
            if f:
                self._html(f.read_text(encoding="utf-8"))
            else:
                self._html("<h3>清单尚未生成</h3><p>请先运行 "
                           "<code>python stuck_shipcc.py --region us --days 14</code></p>")
            return

        if path in ("/dropoff-cancel", "/dropoff_cancel.html"):
            # 投递取消异常清单（已投递却被取消的订单）
            cands = [BASE_DIR / "data" / "dropoff_cancel.html",
                     BASE_DIR / "dropoff_cancel.html"]
            f = next((p for p in cands if p.exists()), None)
            if f:
                self._html(f.read_text(encoding="utf-8"))
            else:
                self._html("<h3>清单尚未生成</h3><p>请先运行 "
                           "<code>python dropoff_cancel.py --region us --days 14</code></p>")
            return

        if path == "/api/dropoff-cancel":
            f = BASE_DIR / "data" / "dropoff_cancel.json"
            if not f.exists():
                self._json({"ok": False, "error": "清单尚未生成"}, 404)
                return
            self._send(200, f.read_bytes(), "application/json; charset=utf-8")
            return

        if path == "/api/stuck":
            f = BASE_DIR / "data" / "stuck_shipcc.json"
            if not f.exists():
                self._json({"ok": False, "error": "清单尚未生成"}, 404)
                return
            self._send(200, f.read_bytes(), "application/json; charset=utf-8")
            return

        if path == "/api/status":
            ensure_fresh()
            orders, meta, ts = STORE.snapshot()
            with STORE.lock:
                loading, err, bad = STORE.loading, STORE.error, STORE.cookie_invalid
            ok_cookie, cookie_msg = (False, "未校验") if bad else probe_cookie(sc.COOKIE)
            self._json({
                "ok": True,
                "orders": len(orders),
                "cachedAt": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "",
                "ageSeconds": round(STORE.age()),
                "ttl": CACHE_TTL,
                "loading": loading,
                "error": err,
                "cookieValid": ok_cookie,
                "cookieMessage": cookie_msg,
            })
            return

        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return

        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")

        # 本地静态看板的「刷新」按钮走这两个口子。
        # 必须在鉴权之前拦，而且**不校验口令**：口令是给「看」用的，刷新是给
        # 「坐在本机的人」用的。门禁靠 _is_local()（回环 IP + 无反代头 + Origin 本机），
        # 外人从隧道进来会带 CF-Connecting-IP，直接 403，不需要再输一遍口令。
        if path in ("/api/refresh-all", "/api/refresh-status"):
            if not self._is_local():
                self._json({"ok": False, "error": "刷新只能在运行服务的这台机器上触发"}, 403)
                return
            if path == "/api/refresh-status":
                with REFALL_LOCK:
                    st = dict(REFALL)
                if st["started"]:
                    end = st["finished"] or time.time()
                    st["elapsed"] = round(end - st["started"], 1)
                else:
                    st["elapsed"] = 0
                self._json({"ok": True, **st})
                return
            ok, err = refresh_all_async()
            self._json({"ok": ok, "error": err}, 200 if ok else 409)
            return

        if path == "/login":
            left = self._locked_for()
            if left:
                self._json({"ok": False, "error": f"尝试次数过多，请 {left} 秒后再试"}, 429)
                return
            data = self._body_json()
            given = str(data.get("token") or "").strip()
            hits = [t for t in TOKENS if hmac.compare_digest(given, t)] if given else []
            ext_hits = [t for t in EXT_TOKENS if hmac.compare_digest(given, t)] if given else []
            if hits or ext_hits:
                _FAIL_LOCK.pop(self._client(), None)
                if hits:
                    log(f"[登录] {self._client()} 使用内部口令 #{TOKENS.index(hits[0]) + 1} 登录成功（全量视图）")
                else:
                    log(f"[登录] {self._client()} 使用外发口令 #{EXT_TOKENS.index(ext_hits[0]) + 1} 登录成功（脱敏视图）")
                tok = hits[0] if hits else ext_hits[0]
                body = json.dumps({"ok": True}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Set-Cookie",
                                 f"{AUTH_COOKIE}={auth_sig(tok)}; Path=/; HttpOnly; "
                                 f"SameSite=Lax; Max-Age={AUTH_LIFETIME}")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._note_fail()
            log(f"[登录失败] {self._client()} 口令不正确")
            self._json({"ok": False, "error": "口令不正确"}, 401)
            return

        if self._guard(path):
            return

        if READONLY and path in ("/api/refresh", "/api/cookie"):
            self._json({"ok": False, "error": "只读模式已关闭该操作"}, 403)
            return

        if path == "/api/refresh":
            ok, err = refresh_orders()
            self._json({"ok": ok, "error": err})
            return

        if path == "/api/cookie":
            data = self._body_json()
            ok, err = update_cookie(data.get("cookie", ""))
            self._json({"ok": ok, "error": err})
            return

        self._json({"ok": False, "error": "not found"}, 404)

    def _waiting_page(self, loading: bool, err: str) -> str:
        msg = "正在从 OC 后台抓取数据，首次约需 3–5 分钟…" if loading else "尚未载入数据"
        if err:
            msg = f"抓取失败：{err}"
        return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"/>
<meta http-equiv="refresh" content="5"/>
<title>订单时效看板 · 准备中</title>
<style>body{{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
background:#f4f5f7;color:#1f2329;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}}
.box{{background:#fff;border:1px solid #e6e8ec;border-radius:12px;padding:32px 40px;text-align:center;max-width:520px}}
h1{{font-size:17px;margin:0 0 10px;font-weight:600}}p{{color:#8a919b;font-size:13px;margin:0;line-height:1.7}}</style>
</head><body><div class="box"><h1>订单时效看板</h1><p>{msg}</p></div></body></html>"""


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:  # noqa: BLE001
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:  # noqa: BLE001
            return "127.0.0.1"


def main():
    global TOKEN, TOKENS, EXT_TOKENS, READONLY
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--days", type=int, default=int(os.getenv("OC_DAYS", "14")))
    ap.add_argument("--no-fetch", action="store_true", help="只读磁盘快照，不自动抓取")
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    ap.add_argument("--token", default=os.getenv("OC_DASHBOARD_TOKEN", ""),
                    help="内部口令，可用逗号分隔多个。看全量：含收件人真名。"
                         "设置后进入只读模式：未认证看不到任何页面/接口")
    ap.add_argument("--token-ext", default=os.getenv("OC_DASHBOARD_TOKEN_EXT", ""),
                    help="外发口令，逗号分隔多个。同样能登录，但视图是脱敏的："
                         "收件人打码、门牌地址不返回。发给公司同事用这一组")
    args = ap.parse_args()

    TOKENS = parse_tokens(args.token)
    EXT_TOKENS = [t for t in parse_tokens(args.token_ext) if t not in TOKENS]
    TOKEN = TOKENS[0] if TOKENS else ""
    READONLY = bool(TOKENS or EXT_TOKENS)

    have = load_snapshot()
    if not have and not args.no_fetch:
        refresh_async(args.days)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    ip = lan_ip()
    log(f"服务已启动 → http://{ip}:{args.port}  （本机 http://127.0.0.1:{args.port}）")
    d_start, d_end = date_range(args.days)
    log(f"缓存 TTL {CACHE_TTL}s，数据范围 {d_start} ~ {d_end}")
    if TOKENS or EXT_TOKENS:
        log(f"口令已启用（内部 {len(TOKENS)} 个 / 外发 {len(EXT_TOKENS)} 个，只读，"
            f"{AUTH_LIFETIME // 3600}h 有效）：未登录跳转 /login；"
            f"刷新数据与更新 Cookie 已关闭")
        if EXT_TOKENS:
            log("外发口令访问的是脱敏视图：收件人打码、门牌地址不返回")
    else:
        log("提示：未设置访问口令，任何人拿到地址即可访问，仅在办公内网使用")

    if args.open:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}")).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("已停止")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
