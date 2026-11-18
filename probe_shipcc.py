#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探针：SHIPCCUS001 之后还会不会产生新节点。

逐单拉事件流，按时间排序后看 SHIPCCUS001 的位置：
  - 是最后一条        → 之后再无节点
  - 不是最后一条      → 列出后续节点序列与间隔，并区分两种性质：
        (a) 取消/过期时间早于后续事件 → 真的被恢复、继续走了
        (b) 取消/过期时间晚于后续事件 → 只是打标滞后，货其实先走了
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import scraper as sc

CODE = "SHIPCCUS001"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="us")
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args()

    start, end = sc._range(args.days, None, None)
    cfg = sc.region_cfg(args.region)
    s = sc._session()

    recs, total, page = [], 0, 1
    while True:
        batch, total = sc.fetch_list(s, start, end, page=page, limit=100, cfg=cfg)
        recs.extend(batch)
        if len(batch) < 100 or len(recs) >= total:
            break
        page += 1
    print(f"[{args.region}] 订单 {len(recs)} / {total}  {start} ~ {end}\n")

    def work(rec):
        return rec, sc.fetch_events(s, rec.get("trackingNumber") or "", cfg)

    with ThreadPoolExecutor(max_workers=12) as pool:
        pairs = list(pool.map(work, recs))

    has_code, no_after, with_after = 0, 0, []
    after_codes = Counter()
    for rec, evs in pairs:
        seq = sorted([(sc.event_time(e), e) for e in evs or [] if sc.event_time(e)],
                     key=lambda x: x[0])
        codes = [str(e.get("jvEventCode") or "").strip().upper() for _, e in seq]
        if CODE not in codes:
            continue
        has_code += 1
        idx = codes.index(CODE)
        after = seq[idx + 1:]
        if not after:
            no_after += 1
            continue
        t0 = seq[idx][0]
        for _, e in after:
            after_codes[str(e.get("jvEventCode") or "").upper()] += 1
        gap = (after[0][0] - t0).total_seconds() / 3600
        with_after.append({
            "waybill": rec.get("trackingNumber"),
            "expired": t0,
            "gapH": gap,
            "after": [(t, str(e.get("jvEventCode") or ""),
                       e.get("oprEnDes") or e.get("oprCnDes") or "") for t, e in after],
            "consignee": (rec.get("consigneeFullName") or "").strip(),
            "isTest": sc.looks_like_test(rec.get("consigneeFullName"),
                                         rec.get("consigneeAddr1") or rec.get("consigneeAddr")),
        })

    print(f"含 {CODE}: {has_code} 单")
    print(f"  之后无任何事件: {no_after} 单 ({no_after/has_code*100:.0f}%)")
    print(f"  之后还有事件  : {len(with_after)} 单 ({len(with_after)/has_code*100:.0f}%)")
    print()
    if not with_after:
        return

    print("后续事件码分布:", dict(after_codes.most_common()))
    print()
    pos = sum(1 for r in with_after if r["gapH"] > 0)
    neg = sum(1 for r in with_after if r["gapH"] <= 0)
    print(f"  取消→首个后续节点 间隔>0（真恢复）: {pos} 单")
    print(f"  间隔<=0（打标滞后）             : {neg} 单")
    print()

    for r in sorted(with_after, key=lambda x: x["expired"]):
        kind = "恢复" if r["gapH"] > 0 else "滞后"
        print(f"{r['waybill']}  [{kind}] 取消于 {r['expired']:%m-%d %H:%M} "
              f"| 首后续 +{r['gapH']:+.1f}h | {r['consignee'][:14]}"
              f"{' [测试]' if r['isTest'] else ''}")
        for t, c, d in r["after"]:
            print(f"      {t:%m-%d %H:%M}  {c:16} {d[:52]}")
        print()


if __name__ == "__main__":
    main()
