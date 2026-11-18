# -*- coding: utf-8 -*-
"""跨 vendor 取件检测：包裹投在 PUDO 点，却被 LMS 的司机取走。

判定规则（两条同时成立才算）：

1. 单内存在 `DOUS002`（Customer drop-off at PUDOpoint）且该事件 `vendorCode = PU`
   —— 说明客户是把包裹投在 **PUDO INC** 的点位上；
2. 该投递时间之后，出现「取件 / 离场」类事件，且 `vendorCode = LM`
   —— 取件码默认 COLUS002 / DFDLUS002 / AFDLUS002 / SHIPFCUS002。

刻意排除的两类：

- `SHIPCCUS001`（订单取消）虽然也带 `vendorCode=LM`，但它不是取件，不算串场；
  窗口内「投递后被取消」的单很多，混进来会把结果放大十几倍。
- 投递时间之前的事件不看。只看严格晚于投递的取件，否则会把「先被 LMS 取、
  后才补登 PUDO 投递」这种顺序倒挂的单算进来。

用法：
    python cross_vendor.py                       # 默认 14 天窗口
    python cross_vendor.py --from 2026/10/20 --to 2026/11/03
    python cross_vendor.py --cache data/_ev_cache.json   # 复用已拉取的事件缓存
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import scraper as sc

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

# Depop 的单只是 Service ID 不同（UDDOMS2D vs USDOMS2D），事件码后缀 US→UD，
# 语义完全一样。统一在 _code() 里归一到 US，下游逻辑不用改。
UD_ALIAS = {
    "DOUD002": "DOUS002",
    "COLUD002": "COLUS002",
    "DFDLUD002": "DFDLUS002",
    "AFDLUD002": "AFDLUS002",
    "SHIPFCUD002": "SHIPFCUS002",
    "MISUD001": "MISUS001",
    "SHIPCCUD001": "SHIPCCUS001",
    "SCAUD002": "SCAUS002",
    "DELUD006": "DELUS006",
}

DROP_CODE = "DOUS002"           # 客户投递到 PUDO 点
DROP_VENDOR = "PU"              # 投递点由 PUDO INC 运营

PICKUP_CODES = ("COLUS002", "DFDLUS002", "AFDLUS002", "SHIPFCUS002")
PICKUP_LABEL = {
    "COLUS002": "司机取件",
    "DFDLUS002": "场地分拣进笼车",
    "AFDLUS002": "到仓",
    "SHIPFCUS002": "可提货",
}

# 佐证码：PUDO 去取件时发现包裹已不在
EVIDENCE_CODES = {"MISUS001": "PUDO 取件时包裹未找到"}


def _t(ev):
    return sc.event_time(ev)


def _code(ev):
    c = str(ev.get("jvEventCode") or "").strip().upper()
    return UD_ALIAS.get(c, c)


def _vendor(ev):
    return str(ev.get("vendorCode") or "").strip().upper()


def analyze(rec: dict, events: list, cfg: dict) -> list[dict]:
    """返回该单命中的串场记录（一单可能有多段投递，逐段判断）。"""
    evs = sorted([e for e in (events or []) if _t(e)], key=_t)
    out = []
    for i, ev in enumerate(evs):
        if _code(ev) != DROP_CODE or _vendor(ev) != DROP_VENDOR:
            continue
        drop_at = _t(ev)
        after = [a for a in evs[i + 1:] if _t(a) > drop_at]

        pick = next(((a, _code(a)) for a in after
                     if _code(a) in PICKUP_CODES and _vendor(a) == "LM"), None)
        if not pick:
            continue
        pick_ev, pick_code = pick
        pick_at = _t(pick_ev)

        evidence = [(str(_t(a)), _code(a), EVIDENCE_CODES[_code(a)])
                    for a in after
                    if _code(a) in EVIDENCE_CODES and _t(a) >= pick_at]

        out.append({
            "waybill": rec.get("trackingNumber") or "",
            "region": cfg["code"],
            "consignee": (rec.get("consigneeFullName") or "").strip(),
            "address": (rec.get("consigneeAddr1") or rec.get("consigneeAddr") or "").strip(),
            "createdAt": (rec.get("orderCreatedTimeWithZone") or "")[:19],
            "dropAt": drop_at.strftime("%Y-%m-%d %H:%M:%S"),
            "dropVendor": DROP_VENDOR,
            "pickAt": pick_at.strftime("%Y-%m-%d %H:%M:%S"),
            "pickCode": pick_code,
            "pickLabel": PICKUP_LABEL.get(pick_code, pick_code),
            "pickVendor": "LM",
            "delayH": round((pick_at - drop_at).total_seconds() / 3600, 2),
            "evidence": evidence,
        })
    return out


def collect(start: str, end: str, cache: Path | None = None) -> tuple[list[dict], dict]:
    cfg = sc.region_cfg("us")

    if cache and cache.exists():
        blob = json.loads(cache.read_text(encoding="utf-8"))
        pairs = [(x["rec"], x["evs"]) for x in blob]
        print(f"[cache] 复用 {cache.name}：{len(pairs)} 单", flush=True)
    else:
        s = sc._session()
        recs, total, page = [], 0, 1
        while True:
            batch, total = sc.fetch_list(s, start, end, page=page, limit=100, cfg=cfg)
            recs.extend(batch)
            if len(batch) < 100 or len(recs) >= total or page > 30:
                break
            page += 1
        print(f"[list] {len(recs)}/{total} 单，开始拉事件…", flush=True)

        def work(r):
            return r, sc.fetch_events(s, r.get("trackingNumber") or "", cfg)

        with ThreadPoolExecutor(max_workers=12) as pool:
            pairs = list(pool.map(work, recs))
        print(f"[ev] 事件拉取完成 {len(pairs)} 单", flush=True)

    # 测试单单独标注，但不剔除（测试单也可能串场，只是不该进运营口径）
    flags = sc.test_flags([(cfg["code"],
                            (r.get("consigneeFullName") or "").strip(),
                            (r.get("consigneeAddr1") or r.get("consigneeAddr") or "").strip())
                           for r, _ in pairs])

    rows = []
    for (rec, evs), is_test in zip(pairs, flags):
        for row in analyze(rec, evs, cfg):
            row["isTest"] = bool(is_test)
            rows.append(row)

    rows.sort(key=lambda r: r["dropAt"], reverse=True)
    drops = sum(1 for _, evs in pairs
                for e in (evs or [])
                if _code(e) == DROP_CODE and _vendor(e) == DROP_VENDOR)
    meta = {
        "generatedAt": datetime.now(sc.TZ_CN).strftime("%Y-%m-%d %H:%M"),
        "dateFrom": start, "dateTo": end,
        "orders": len(pairs),
        "pudoDrops": drops,                       # PUDO 点投递事件总数（分母）
        "hitEvents": len(rows),
        "hitOrders": len({r["waybill"] for r in rows}),
        "hitReal": len({r["waybill"] for r in rows if not r["isTest"]}),
        "hitTest": len({r["waybill"] for r in rows if r["isTest"]}),
        "pickCodes": dict(Counter(r["pickCode"] for r in rows)),
        "rule": (f"{DROP_CODE}(vendor={DROP_VENDOR}) 之后出现 "
                 f"{'/'.join(PICKUP_CODES)} 且 vendorCode=LM；不含 SHIPCCUS001 取消"),
    }
    return rows, meta


def write_csv(rows: list[dict]) -> Path:
    out = DATA / "cross_vendor.csv"
    cols = ["waybill", "isTest", "consignee", "address", "createdAt",
            "dropAt", "pickAt", "pickCode", "pickLabel", "delayH"]
    for target in (out, DATA / "cross_vendor_new.csv"):
        try:
            with target.open("w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["运单号", "测试单", "收件人", "地址", "建单时间",
                            "PUDO 投递时间", "LMS 取件时间", "取件事件码",
                            "取件环节", "投递到取件(小时)"])
                for r in rows:
                    w.writerow([r["waybill"], "是" if r["isTest"] else "否",
                                r["consignee"], r["address"], r["createdAt"],
                                r["dropAt"], r["pickAt"], r["pickCode"],
                                r["pickLabel"], r["delayH"]])
            print(f"[csv] -> {target}")
            return target
        except PermissionError:
            print(f"[csv] {target.name} 被占用，尝试下一路径")
    return out


def main():
    ap = argparse.ArgumentParser(description="检测「PUDO 投递 → LMS 取走」的串场单")
    ap.add_argument("--from", dest="start", default=None, help="起始日期 2026/10/20")
    ap.add_argument("--to", dest="end", default=None, help="结束日期 2026/11/03")
    ap.add_argument("--days", type=int, default=14, help="未指定日期时回看天数")
    ap.add_argument("--cache", default=None, help="复用事件缓存 JSON 路径")
    a = ap.parse_args()

    end = a.end or datetime.now(sc.TZ_CN).strftime("%Y/%m/%d")
    start = a.start or (datetime.strptime(end, "%Y/%m/%d")
                        - timedelta(days=a.days - 1)).strftime("%Y/%m/%d")
    start_s, end_s = f"{start} 00:00:00", f"{end} 23:59:59"

    cache = Path(a.cache) if a.cache else None
    rows, meta = collect(start_s, end_s, cache)

    DATA.mkdir(exist_ok=True)
    (DATA / "cross_vendor.json").write_text(
        json.dumps({"meta": meta, "rows": rows}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    print(f"[json] -> {DATA / 'cross_vendor.json'}")
    write_csv(rows)

    print(f"\n窗口 {start_s} ~ {end_s} | 订单 {meta['orders']} 单")
    print(f"PUDO 点投递事件 {meta['pudoDrops']} 条 → 命中串场 "
          f"{meta['hitOrders']} 单（真实 {meta['hitReal']} / 测试 {meta['hitTest']}）")
    if rows:
        print(f"取件码分布: {meta['pickCodes']}")
        for r in rows:
            tag = "测试" if r["isTest"] else "真实"
            print(f"  [{tag}] {r['waybill']}  {r['consignee'][:22]:<22} "
                  f"投递 {r['dropAt'][5:16]} → {r['pickLabel']} {r['pickAt'][5:16]} "
                  f"(+{r['delayH']}h)")
            for t, c, d in r["evidence"]:
                print(f"         佐证 {t[5:16]} {c} {d}")
    else:
        print("窗口内没有命中。")


if __name__ == "__main__":
    main()
