"""一次性回填：按新口径重算 data/orders.json 里的 SPX 提取字段。

口径变更（2026-11-06 用户确认）：
    SPX 提取 = jvEventCode **SCAUS002 或 SCN005，任意一个命中即算，取最早一条**。
之前只认 SCAUS002（司机提取），PUDO 链路根本不报它，导致「已出库却算不出交接」；
现在 SCN005（Processing at Origin Facility）也算，PUDO 出库后 ~2.7h 就有扫描。

回填而不重抓：本地 `data/_ev_cache_lms_*.json` 已有每张单的完整事件流，
判定直接调 scraper 的同一套函数，不另写口径。

用法：
    python backfill_spx.py            # 自动取最新的 _ev_cache_lms_*.json
    python backfill_spx.py --dry-run  # 只打印统计，不写文件
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


def cfg_for(order: dict) -> dict:
    """按订单所属区域取事件码配置（US 的 SCAUS002 / UK 的 SCAUK002 + SCN005）。"""
    key = str(order.get("region") or "us").strip().lower()
    if key not in scraper.REGIONS:
        key = "us"
    return scraper.region_cfg(key)


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

    # 统计用
    stat = {"changed": 0, "sca": 0, "scn": 0, "none": 0, "missing": 0}
    by_vendor: dict[str, dict] = {}

    for o in orders:
        tn = o.get("lastMileWaybill") or o.get("ocOrderNo") or ""
        evs = evs_by_tn.get(tn)
        if evs is None:
            stat["missing"] += 1
            continue

        cfg = cfg_for(o)
        t, ev = scraper.extract_spx_pickup(evs, scraper.spx_pickup_codes(cfg))
        strict_t, strict_ev = scraper.extract_event(evs, cfg["spx"])
        code = str((ev or {}).get("jvEventCode") or "")

        old = o.get("spxPickupTime") or ""
        new = scraper.fmt_time(t)
        if old != new:
            stat["changed"] += 1

        o["spxPickupTime"] = new
        o["spxPickupCode"] = code
        o["spxStrictTime"] = scraper.fmt_time(strict_t)
        o["spxStrictCode"] = str((strict_ev or {}).get("jvEventCode") or "")

        rdc_dt = scraper.parse_time(o.get("rdcOutTime"))
        a_scan_dt = scraper.parse_time(o.get("aScanTime"))
        h = scraper.hours_between(rdc_dt, t)
        o["hRdcToSpx"] = h if (h is not None and h >= 0) else None
        hs = scraper.hours_between(rdc_dt, strict_t)
        o["hRdcToSpxStrict"] = hs if (hs is not None and hs >= 0) else None
        ha = scraper.hours_between(a_scan_dt, t)
        o["hAScanToSpx"] = ha if (ha is not None and ha >= 0) else None
        # 旧字段已废弃，避免看板误读
        o.pop("spxHandoverTime", None)
        o.pop("spxHandoverCode", None)
        o.pop("hRdcToSpxHandover", None)

        if code == cfg["spx"]:
            stat["sca"] += 1
        elif code:
            stat["scn"] += 1
        else:
            stat["none"] += 1

        v = o.get("vendor") or "—"
        slot = by_vendor.setdefault(v, {"total": 0, "out": 0, "hit": 0, "gap": []})
        slot["total"] += 1
        if o.get("rdcOutTime"):
            slot["out"] += 1
            if o["hRdcToSpx"] is not None:
                slot["hit"] += 1
                slot["gap"].append(o["hRdcToSpx"])

    print(f"[backfill] 缓存 {cache_path.name}：{len(evs_by_tn)} 张单的事件流")
    print(f"[backfill] 命中 {cfg['spx']} {stat['sca']} 单 · 仅 SCN005 {stat['scn']} 单"
          f" · 无 SPX 提取 {stat['none']} 单 · 缓存缺失 {stat['missing']} 单"
          f" · spxPickupTime 有变化 {stat['changed']} 单")
    print("[backfill] 按出库方（只算真实单才有意义，这里含测试单）：")
    for v, s in sorted(by_vendor.items()):
        g = s["gap"]
        avg = f"{sum(g)/len(g):.1f}h" if g else "—"
        print(f"    {v:9s} 出库 {s['out']:3d} 单 → 已提取 {s['hit']:3d} 单，均值 {avg}")

    if args.dry_run:
        for o in orders[:5]:
            print("  ", o.get("ocOrderNo"), o.get("rdcOutTime"), o.get("spxPickupTime"),
                  o.get("spxPickupCode"), o.get("hRdcToSpx"))
        return

    scraper.save(orders, meta)
    print(f"[backfill] 已写回 {ORDERS.name} / orders.csv")


if __name__ == "__main__":
    main()
