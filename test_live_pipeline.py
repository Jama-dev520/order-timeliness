#!/usr/bin/env python
"""回归测试：live/pipeline.py 算出来的订单，必须与本地 refresh_all.py 的逐字段一致。

做法：
  1. 用 requests 按线上同一套参数抓一个窗口（list + events），存成 payload
  2. 交给 live/pipeline.py 在独立目录里跑（不碰真实 data/）
  3. 与 data/orders.json 里同一批运单号逐字段比对

用法：python test_live_pipeline.py [--from 2026/11/17] [--to 2026/11/17]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

API_PATH = "/aquarius-order/order/tracking-number/v1/tracking-number-list"
EV_PATH = "/aquarius-order/order/tracking-number/v1/tracking-event-list/{tn}"


def env(key: str, default: str = "") -> str:
    for line in (BASE / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return default


def fetch(host: str, cookie: str, start: str, end: str, workers: int = 12):
    import requests

    s = requests.Session()
    s.trust_env = False
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        # 接口的国家名会随这个头变化：不带 → "UnitedStates"，带了（任何语言）→ "美国"。
        # 真实抓数脚本和浏览器都会带，所以测试也必须带，否则 destCountry 会不一致。
        "Accept-Language": "en-US,en;q=0.9",
    })
    if cookie:
        s.headers["Cookie"] = cookie

    def page(p):
        r = s.get(host + API_PATH, params={
            "page": p, "limit": 10, "startDate": start, "endDate": end,
            "destinationCountry": "US"}, timeout=40)
        r.raise_for_status()
        res = r.json().get("result") or {}
        return res.get("list") or [], int(res.get("total") or 0)

    t0 = time.time()
    first, total = page(1)
    if total > 5000:
        sys.exit(f"返回 {total} 单，日期条件被忽略，请缩小窗口")
    pages = (total + 9) // 10
    records = list(first)
    with ThreadPoolExecutor(max_workers=8) as ex:
        for lst, _ in ex.map(page, range(2, pages + 1)):
            records += lst
    print(f"[fetch] list {len(records)}/{total} 单，{pages} 页，{time.time()-t0:.1f}s")

    t1 = time.time()

    def ev(rec):
        tn = rec.get("trackingNumber") or ""
        try:
            r = s.get(host + EV_PATH.format(tn=tn), timeout=40)
            res = r.json().get("result")
            return tn, (res if isinstance(res, list) else [])
        except Exception:  # noqa: BLE001
            return tn, []

    with ThreadPoolExecutor(max_workers=workers) as ex:
        events = dict(ex.map(ev, records))
    got = sum(1 for v in events.values() if v)
    print(f"[fetch] events {len(events)} 单（{got} 单有事件），{time.time()-t1:.1f}s")
    return records, events


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="d1", default="2026/11/17")
    ap.add_argument("--to", dest="d2", default="2026/11/17")
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args()
    start, end = f"{args.d1} 00:00:00", f"{args.d2} 23:59:59"

    host = env("OC_HOST_US").rstrip("/")
    cookie = env("OC_COOKIE")
    if not host:
        sys.exit("OC_HOST_US 为空")

    records, events = fetch(host, cookie, start, end, args.workers)

    # ---- 跑 pipeline（在临时目录里，不碰真实 data/）----
    sys.path.insert(0, str(BASE / "live"))
    import pipeline  # noqa: E402

    sources = {n: (BASE / n).read_text(encoding="utf-8")
               for n in pipeline.DATA_SOURCES}
    r = pipeline.bootstrap(json.dumps(sources))
    if r != "ok":
        sys.exit(f"bootstrap 失败：{r}")
    print(f"[pipe] bootstrap ok -> {pipeline.BASE}")

    payload = json.dumps({"records": records, "events": events,
                          "start": start, "end": end,
                          "region": "us", "api": host}, ensure_ascii=False)
    t0 = time.time()
    out = json.loads(pipeline.run(payload))
    if not out.get("ok"):
        sys.exit(f"pipeline 失败：{out.get('error')}")
    print(f"[pipe] 计算完成 {time.time()-t0:.1f}s | {json.dumps(out['stats'], ensure_ascii=False)}")

    live = {o["ocOrderNo"]: o for o in out["payload"]["orders"]}

    # ---- 与本地快照比对 ----
    snap = json.loads((BASE / "data" / "orders.json").read_text(encoding="utf-8"))
    local = {o["ocOrderNo"]: o for o in snap["orders"]}

    common = sorted(set(live) & set(local))
    if not common:
        sys.exit("没有交集，窗口对不上")

    skip = {"isTest"}  # 同地址推定依赖全量上下文，子集窗口会有差异，属预期
    diffs = []
    for tn in common:
        a, b = live[tn], local[tn]
        for k in sorted(set(a) | set(b)):
            if k in skip:
                continue
            if a.get(k) != b.get(k):
                diffs.append((tn, k, a.get(k), b.get(k)))

    print(f"\n[比对] 交集 {len(common)} 单 | 仅实时 {len(set(live)-set(local))} | 仅本地 {len(set(local)-set(live))}")
    if diffs:
        print(f"✗ 字段不一致 {len(diffs)} 处，前 12 条：")
        for tn, k, x, y in diffs[:12]:
            print(f"   {tn} {k}: 实时={x!r} 本地={y!r}")
        sys.exit(1)
    print(f"✓ {len(common)} 单全部字段一致（除 isTest 外）")

    # 指标口径抽查
    m = out["payload"]["meta"]
    print(f"[meta] 窗口 {m.get('dateFrom')} ~ {m.get('dateTo')} | 单量 {m.get('total')} "
          f"| 妥投 {m.get('delivered')} | 待A-SCAN {m.get('pendingAScan')}")
    print(f"[异常] 串场 {out['stats']['cross']} | 投递取消 {out['stats']['dropoff']} "
          f"| LMS同供 {out['stats']['lmsSame']} | 有事件 {out['stats']['eventsOrders']} 单")


if __name__ == "__main__":
    main()
