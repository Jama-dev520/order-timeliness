#!/usr/bin/env python
"""在浏览器（Pyodide）里跑完整计算链，复刻 refresh_all.py 的口径。

设计原则：**业务逻辑零重写**。
    scraper.fetch_real / build_meta / save
    cross_vendor.collect / dropoff_cancel.collect / lms_same_vendor.main
    dashboard._load_* / _raw_window / _load_raw_events
全部原样调用，只把网络层（_session / fetch_list / fetch_events）换成内存里的数据，
把多线程换成串行（Pyodide 没有真线程）。这样口径不可能漂移。

入口（JS 侧调用）：
    ocPipeline.bootstrap(sources_json)   # 先把 .py 源写进虚拟文件系统并 import
    ocPipeline.run(payload_json)         # 传原始抓取结果，返回看板启动载荷 JSON
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# 浏览器（emscripten）落到根目录；本机跑回归测试时落到临时目录，别污染项目
BASE = os.environ.get("OC_LIVE_BASE") or (
    "/oc_live" if sys.platform == "emscripten" else str(Path(tempfile.gettempdir()) / "oc_live"))
DATA_SOURCES = (
    "scraper.py",
    "cross_vendor.py",
    "dropoff_cancel.py",
    "lms_same_vendor.py",
    "dashboard.py",
)

_state = {"ready": False, "mods": {}}


class _SerialPool:
    """Pyodide 没有真线程，把 ThreadPoolExecutor 换成串行执行（逻辑不变）。"""

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def submit(self, fn, *a, **k):
        class _F:
            def __init__(self, v):
                self._v = v

            def result(self):
                return self._v

        return _F(fn(*a, **k))

    def map(self, fn, it):
        return [fn(x) for x in it]

    def shutdown(self, *a, **k):
        pass


def bootstrap(sources_json: str) -> str:
    """把 .py 源写入虚拟文件系统并 import。只做一次。"""
    if _state["ready"]:
        return "already"

    sources = json.loads(sources_json)
    missing = [n for n in DATA_SOURCES if n not in sources]
    if missing:
        return "missing: " + ",".join(missing)

    root = os.path.join(BASE)
    os.makedirs(root, exist_ok=True)
    os.makedirs(os.path.join(root, "data"), exist_ok=True)
    for name, src in sources.items():
        with open(os.path.join(root, name), "w", encoding="utf-8") as f:
            f.write(src)
    if root not in sys.path:
        sys.path.insert(0, root)

    # 关键：如果宿主环境里已经有同名模块（比如本机跑回归测试时 import 过仓库里的 scraper），
    # 必须踢掉，否则会用到仓库那份，DATA_DIR 指向真实 data/ 并覆盖线上快照。
    for name in DATA_SOURCES:
        sys.modules.pop(name[:-3], None)

    import scraper  # noqa: E402
    import cross_vendor  # noqa: E402
    import dropoff_cancel  # noqa: E402
    import lms_same_vendor  # noqa: E402
    import dashboard  # noqa: E402

    # 串行池（四个模块都各自 import 了 ThreadPoolExecutor）
    for m in (scraper, cross_vendor, dropoff_cancel, lms_same_vendor):
        m.ThreadPoolExecutor = _SerialPool

    _state["mods"] = {
        "sc": scraper, "cv": cross_vendor, "do": dropoff_cancel,
        "lm": lms_same_vendor, "dash": dashboard,
    }
    _state["ready"] = True
    return "ok"


def _patch_net(sc, records, events):
    """把 scraper 的网络层换成内存数据。三个异常脚本都走 sc.*，所以只需换这一处。"""
    total = len(records)

    def fake_session():
        return None

    def fake_list(s, start, end, page=1, limit=100, cfg=None):
        # 复刻服务端分页：limit 超过 100 会被忽略回退到 10，这里按 100 切片即可
        lim = limit if limit and limit <= 100 else 100
        lo = (page - 1) * lim
        return records[lo:lo + lim], total

    def fake_events(s, tn, cfg=None):
        return events.get(tn) or []

    sc._session = fake_session
    sc.fetch_list = fake_list
    sc.fetch_events = fake_events


def run(payload_json: str) -> str:
    """payload: {records, events, start, end, region, api}

    返回 {ok, payload: {meta, orders, cross, dropoff, lmsSame, raw, ...}, stats, error}
    """
    t0 = time.time()
    payload = json.loads(payload_json)
    records = payload.get("records") or []
    events = payload.get("events") or {}
    start = payload["start"]
    end = payload["end"]
    region = (payload.get("region") or "us").lower()
    api = (payload.get("api") or "").strip().rstrip("/")

    if _state["ready"] is False:
        return json.dumps({"ok": False, "error": "pipeline 未初始化"})
    if not records:
        return json.dumps({"ok": False, "error": "没有订单数据"})

    m = _state["mods"]
    sc, cv, do, lm, dash = m["sc"], m["cv"], m["do"], m["lm"], m["dash"]

    # scraper 在 import 时就把主机读进常量了；这里补上（浏览器侧传入，不落盘）
    if api:
        os.environ["OC_HOST_US"] = api
    sc.REGION = region
    for mod in (sc, cv, do, lm):
        if hasattr(mod, "REGION"):
            mod.REGION = region

    steps = {}
    t = time.time()
    _patch_net(sc, records, events)
    # fetch_real 开头有「缺 Cookie 就退出」的守卫。这里网络层已被替换成内存数据，
    # Cookie 永远不会被用到，但仍要给它一个非空值才能进到归一化那一步。
    sc.COOKIE = "browser-session"

    cfg_us = sc.region_cfg("us")
    # 主机常量在 import 时定型，这里按浏览器给的主机覆盖一次，避免 SystemExit
    cfg_us["host"] = api or cfg_us.get("host") or ""
    sc.FIRST_CFG = cfg_us
    sc.BASE_URL = cfg_us["host"]
    sc.DEST_COUNTRY = cfg_us.get("dest") or "US"
    steps["cfg"] = round(time.time() - t, 2)

    # 1) 归一化 —— 与 refresh_all.py 同一个入口
    t = time.time()
    orders = sc.fetch_real(start, end, 0)
    if not orders:
        return json.dumps({"ok": False, "error": "归一化后 0 单"})
    meta = sc.build_meta(start, end, orders, demo=False)
    sc.save(orders, meta)
    steps["normalize"] = round(time.time() - t, 2)

    # 2) 三个异常清单 —— 原样调用各自 collect/main
    t = time.time()
    try:
        rows, cmeta = cv.collect(start, end)
        (sc.DATA_DIR / "cross_vendor.json").write_text(
            json.dumps({"meta": cmeta, "rows": rows}, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print("[live] 串场检测失败：%s" % exc)
    try:
        rows, dmeta = do.collect(start, end)
        (sc.DATA_DIR / "dropoff_cancel.json").write_text(
            json.dumps({"meta": dmeta, "rows": rows}, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print("[live] 投递取消检测失败：%s" % exc)
    try:
        old_argv = sys.argv
        sys.argv = ["lms_same_vendor.py"]
        try:
            lm.main()
        finally:
            sys.argv = old_argv
    except Exception as exc:  # noqa: BLE001
        print("[live] LMS 同供检测失败：%s" % exc)
    steps["anomalies"] = round(time.time() - t, 2)

    # 3) 读回三份清单 + 原始事件（复用 dashboard 的 loader，键名与线上一致）
    t = time.time()
    cross = dash._load_cross()
    dropoff = dash._load_dropoff()
    lms_same = dash._load_lms_same()
    keep, raw_cut, raw_days = dash._raw_window(orders)
    raw_map, raw_snap = dash._load_raw_events(keep)
    if not raw_snap:
        raw_snap = time.strftime("%Y-%m-%d %H:%M")
    steps["load"] = round(time.time() - t, 2)

    # 4) meta 里的三块汇总（与 dashboard.render 同一套文案与字段）
    if cross:
        meta["crossMeta"] = {
            "count": len(cross),
            "items": list(cross.keys()),
            "rule": "DOUS002(vendor=PU) 之后出现 COLUS002/DFDLUS002/AFDLUS002/SHIPFCUS002 "
                    "且 vendorCode=LM；不含 SHIPCCUS001 取消",
        }
    if dropoff:
        todo = [v for v in dropoff.values()
                if not v.get("isTest") and v.get("kind") in ("inverted", "noResume")]
        meta["dropoffCancel"] = {
            "todo": len(todo),
            "hits": len(dropoff),
            "rule": "已投递（FIRST_SCAN）却被取消（SHIPCCUS001）：取消早于投递=时序倒挂，"
                    "投递后取消且无更晚节点=取消后无后续",
        }
    if lms_same:
        real = [v for v in lms_same.values() if not v.get("isTest")]
        meta["lmsSame"] = {
            "hits": len(lms_same),
            "real": len(real),
            "rule": "OCUS001 与 DOUS002/DOUD002 两条事件的 vendorCode 都是 LM",
        }

    out = {
        "meta": meta,
        "orders": orders,
        "cross": cross,
        "dropoff": dropoff,
        "lmsSame": lms_same,
        "raw": raw_map,
        "rawSnap": raw_snap,
        "rawCut": raw_cut,
        "rawDays": raw_days,
        "live": {"start": start, "end": end, "region": region,
                 "api": api, "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S")},
    }
    return json.dumps({
        "ok": True,
        "payload": out,
        "stats": {
            "orders": len(orders),
            "eventsOrders": len(raw_map),
            "cross": len(cross),
            "dropoff": len(dropoff),
            "lmsSame": len(lms_same),
            "steps": steps,
            "totalSec": round(time.time() - t0, 1),
        },
    }, ensure_ascii=False)
