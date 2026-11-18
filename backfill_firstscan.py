"""一次性回填：给 data/orders.json 补 dropIsEventCode / dropIsFirstScan。

为什么要单独回填：这两个字段是 scraper.py 新加的（用于在明细里标「首扫」），
但重新抓数要等 Cookie 有效 + 十几分钟，而本地 `data/_ev_cache_lms_*.json`
里已经有每张单的完整事件流，直接按同一套判定（scraper.extract_drop）算出来即可。
判定逻辑不另写一份，避免和抓数时的口径打架。

用法：
    python backfill_firstscan.py            # 自动取最新的 _ev_cache_lms_*.json
    python backfill_firstscan.py --dry-run  # 只打印，不写文件
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import scraper

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ORDERS = DATA_DIR / "orders.json"


def latest_cache() -> Path:
    files = sorted(DATA_DIR.glob("_ev_cache_lms_*.json"))
    if not files:
        sys.exit("找不到 data/_ev_cache_lms_*.json，先跑一次 lms_same_vendor.py")
    return files[-1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cache", default="")
    args = ap.parse_args()

    cache_path = Path(args.cache) if args.cache else latest_cache()
    raw = json.loads(cache_path.read_text(encoding="utf-8"))
    evs_by_tn: dict[str, list] = {}
    for item in raw:
        rec = item.get("rec") or {}
        tn = rec.get("lastMileWaybill") or rec.get("ocOrderNo") or ""
        if tn:
            evs_by_tn[tn] = item.get("evs") or []

    data = json.loads(ORDERS.read_text(encoding="utf-8"))
    orders = data.get("orders", [])
    meta = data.get("meta", {})

    n_drop = n_first = n_fallback = n_missing = 0
    for o in orders:
        tn = o.get("lastMileWaybill") or o.get("ocOrderNo") or ""
        evs = evs_by_tn.get(tn)
        if evs is None:
            n_missing += 1
            continue
        t, ev, _vendor, _raw = scraper.extract_drop(evs)
        o["dropIsEventCode"] = str((ev or {}).get("isEventCode") or "")
        o["dropIsFirstScan"] = bool(ev) and o["dropIsEventCode"].strip().upper() == "FIRST_SCAN"
        if ev:
            n_drop += 1
            if o["dropIsFirstScan"]:
                n_first += 1
            else:
                n_fallback += 1

    print(f"[backfill] 缓存 {cache_path.name}：{len(evs_by_tn)} 张单的事件流")
    print(f"[backfill] 有投递事件 {n_drop} 单 · isEventCode=FIRST_SCAN {n_first} 单"
          f" · 靠 DO* 事件码兜底 {n_fallback} 单 · 缓存里没有 {n_missing} 单")

    if args.dry_run:
        for o in orders[:5]:
            print("  ", o.get("ocOrderNo"), o.get("dropTime"), o.get("dropIsEventCode"),
                  o.get("dropIsFirstScan"))
        return

    scraper.save(orders, meta)
    print(f"[backfill] 已写回 {ORDERS.name} / orders.csv")


if __name__ == "__main__":
    main()
