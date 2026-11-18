# -*- coding: utf-8 -*-
"""标记「建单与投递都是 LMS」的单。

判定依据是**事件本身携带的 vendorCode**，不是订单上的出库方：

1. 单内存在 `OCUS001`（Shipment created，建单）且该事件 `vendorCode = LM`；
2. 同一单存在 `DOUS002` / `DOUD002`（客户投递 / 已投递）且该事件 `vendorCode = LM`。

两条同时成立 → 该单从建单到投递全程由 LMS 承接，明细表里单独打标。

刻意做了几件事：
- 同一单可能有多次投递事件（补投、重复上报），**任一条为 LM 即算命中**，
  但会记下最早那条的时间用于展示。
- 建单事件也可能重复，同样「任一为 LM 即命中」。
- 测试单照常标注 `isTest`，不剔除——标不标是运营口径，剔不剔由看板开关决定。

用法：
    python lms_same_vendor.py                        # 读 data/orders.json，全量跑
    python lms_same_vendor.py --no-cache             # 忽略事件缓存，重新拉
    python lms_same_vendor.py --workers 16
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import scraper as sc

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT = DATA / "lms_same_vendor.json"

CREATE_CODE = "OCUS001"                    # Shipment created
DROP_CODES = ("DOUS002", "DOUD002")        # 客户投递 / 已投递到点
TARGET = "LM"                              # LMS 的 vendorCode 短码


def _code(ev) -> str:
    return str(ev.get("jvEventCode") or "").strip().upper()


def _vendor(ev) -> str:
    return str(ev.get("vendorCode") or "").strip().upper()


def _t(ev):
    return sc.event_time(ev)


def analyze(rec: dict, events: list, cfg: dict) -> dict | None:
    """返回命中记录；不命中返回 None。"""
    evs = [e for e in (events or []) if _t(e)]
    creates = sorted([e for e in evs if _code(e) == CREATE_CODE], key=_t)
    drops = sorted([e for e in evs if _code(e) in DROP_CODES], key=_t)
    if not creates or not drops:
        return None

    create = next((e for e in creates if _vendor(e) == TARGET), None)
    drop = next((e for e in drops if _vendor(e) == TARGET), None)
    if not create or not drop:
        return None

    c_at, d_at = _t(create), _t(drop)
    return {
        "waybill": (rec.get("lastMileWaybill") or rec.get("ocOrderNo") or ""),
        "ocOrderNo": rec.get("ocOrderNo") or "",
        "region": cfg["code"],
        "consignee": (rec.get("consignee") or "").strip(),
        "createdAt": rec.get("orderCreateTime") or "",
        "createAt": c_at.strftime("%Y-%m-%d %H:%M:%S"),
        "createVendor": _vendor(create),
        "dropAt": d_at.strftime("%Y-%m-%d %H:%M:%S"),
        "dropCode": _code(drop),
        "dropVendor": _vendor(drop),
        "delayH": round((d_at - c_at).total_seconds() / 3600, 2),
        "isTest": bool(rec.get("isTest")),
        # 便于排查：同单里这两个码出现的全部 vendor
        "createVendors": sorted({_vendor(e) for e in creates}),
        "dropVendors": sorted({_vendor(e) for e in drops}),
    }


def load_pairs(orders: list[dict], cfg: dict, cache: Path, workers: int) -> list[tuple]:
    """取每单的事件链：优先用缓存，缺失的单再打接口补。"""
    cached: dict[str, list] = {}
    if cache.exists():
        try:
            blob = json.loads(cache.read_text(encoding="utf-8"))
            for x in blob:
                tn = (x.get("rec") or {}).get("lastMileWaybill") or ""
                if tn:
                    cached[tn] = x.get("evs") or []
            print(f"[cache] {cache.name}：{len(cached)} 单", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[cache] 读取失败，忽略：{exc}", flush=True)

    todo = [o for o in orders if o.get("lastMileWaybill") not in cached]
    if todo:
        print(f"[ev] 需要补拉 {len(todo)} 单事件…", flush=True)
        s = sc._session()

        def work(rec):
            tn = rec.get("lastMileWaybill") or ""
            try:
                return tn, sc.fetch_events(s, tn, cfg) or []
            except Exception:  # noqa: BLE001
                return tn, []

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i, (tn, evs) in enumerate(pool.map(work, todo), 1):
                cached[tn] = evs
                if i % 50 == 0:
                    print(f"[ev] {i}/{len(todo)}", flush=True)

        cache.write_text(json.dumps(
            [{"rec": {"lastMileWaybill": k}, "evs": v} for k, v in cached.items()],
            ensure_ascii=False), encoding="utf-8")
        print(f"[cache] 已写回 {cache.name}（{len(cached)} 单）", flush=True)

    return [(o, cached.get(o.get("lastMileWaybill") or "", [])) for o in orders]


def main():
    ap = argparse.ArgumentParser(description="标记建单+投递都是 LMS 的单")
    ap.add_argument("--no-cache", action="store_true", help="忽略事件缓存，全部重拉")
    ap.add_argument("--workers", type=int, default=12)
    a = ap.parse_args()

    snap = json.loads((DATA / "orders.json").read_text(encoding="utf-8"))
    orders = [o for o in snap["orders"] if o.get("region") == "US"]
    cfg = sc.region_cfg("us")
    print(f"[in] {len(orders)} 单（US，窗口 {snap['meta'].get('dateFrom')} ~ {snap['meta'].get('dateTo')}）",
          flush=True)

    cache = DATA / f"_ev_cache_lms_{datetime.now().strftime('%Y%m%d')}.json"
    if a.no_cache and cache.exists():
        cache.unlink()
    pairs = load_pairs(orders, cfg, cache, a.workers)

    rows, both_lm, no_create, no_drop = [], 0, 0, 0
    combo = Counter()
    for rec, evs in pairs:
        codes = {_code(e) for e in (evs or [])}
        if CREATE_CODE not in codes:
            no_create += 1
        if not (codes & set(DROP_CODES)):
            no_drop += 1
        hit = analyze(rec, evs, cfg)
        if hit:
            rows.append(hit)
        c_v = sorted({_vendor(e) for e in (evs or []) if _code(e) == CREATE_CODE})
        d_v = sorted({_vendor(e) for e in (evs or []) if _code(e) in DROP_CODES})
        if c_v and d_v:
            combo[("+".join(c_v) or "?", "+".join(d_v) or "?")] += 1

    rows.sort(key=lambda r: r["dropAt"], reverse=True)
    meta = {
        "generatedAt": datetime.now(sc.TZ_CN).strftime("%Y-%m-%d %H:%M"),
        "dateFrom": snap["meta"].get("dateFrom"),
        "dateTo": snap["meta"].get("dateTo"),
        "orders": len(pairs),
        "hits": len(rows),
        "hitReal": sum(1 for r in rows if not r["isTest"]),
        "hitTest": sum(1 for r in rows if r["isTest"]),
        "hitWithDropInWindow": sum(1 for r in rows if r["dropAt"] >= "2026-10-29"),
        "noCreateEvent": no_create,
        "noDropEvent": no_drop,
        "combos": {" + ".join(k): v for k, v in combo.most_common()},
        "rule": f"{CREATE_CODE}(vendor={TARGET}) 且 {('/'.join(DROP_CODES))}(vendor={TARGET})",
    }

    DATA.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"meta": meta, "rows": rows}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"[out] -> {OUT}")
    print(f"\n订单 {meta['orders']} 单 | 命中「建单+投递都是 LMS」{meta['hits']} 单"
          f"（真实 {meta['hitReal']} / 测试 {meta['hitTest']}）")
    print(f"其中投递发生在 10/29 之后的：{meta['hitWithDropInWindow']} 单")
    print(f"缺建单事件 {no_create} 单 · 缺投递事件 {no_drop} 单")
    print("建单方 + 投递方 组合分布：")
    for k, v in meta["combos"].items():
        print(f"  {k:12s} {v:4d} 单")


if __name__ == "__main__":
    main()
