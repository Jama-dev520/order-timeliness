#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐个探测给定物流单的完整事件链，判定「取消节点后有无后续节点」。

用法：
  python probe_resend.py            # 用内置清单
  python probe_resend.py --from-file tns.txt
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import scraper as S

TNS = """USPDPPS0000057627N
USPDPPS1000057657N
USPDPPS1000057688N
USPDPPS1000057725N
USPDPPS1000057787N
USPDPPS2000057434N
USPDPPS2000057571N
USPDPPS2000057656N
USPDPPS2000057694N
USPDPPS3000057716N
USPDPPS4000057661N
USPDPPS5000057516N
USPDPPS6000057447N
USPDPPS7000057712N
USPDPPS7000057729N
USPDPPS7000057743N
USPDPPS8000057599N
USPDPPS8000057742N
USPDPPS9000057574N
USPDPPS9000057710N
USPDPPS9000057741N"""

CANCEL = "SHIPCCUS001"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-file")
    ap.add_argument("--region", default="us")
    ap.add_argument("--json-out", default="data/resend_probe.json")
    args = ap.parse_args()

    tns = []
    if args.from_file:
        tns = [l.strip() for l in Path(args.from_file).read_text(encoding="utf-8").splitlines() if l.strip()]
    else:
        tns = [l.strip() for l in TNS.splitlines() if l.strip()]
    # 去重保序
    seen, uniq = set(), []
    for t in tns:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    tns = uniq
    print(f"共 {len(tns)} 个单号（已去重）")

    cfg = S.region_cfg(args.region)
    s = S._session()

    out = []
    for i, tn in enumerate(tns, 1):
        evs = S.fetch_events(s, tn, cfg)
        rows = []
        for e in evs:
            t = S.event_time(e)
            rows.append({
                "code": str(e.get("jvEventCode", "")).strip(),
                "time": S.fmt_time(t),
                "ts": t.timestamp() if t else None,
                "vendor": e.get("vendorName") or e.get("vendorCode") or "",
                "cn": e.get("jvEventCnRemark") or "",
                "en": e.get("jvEventEnRemark") or "",
            })
        rows.sort(key=lambda r: (r["ts"] or 0))
        cancel_idx = [k for k, r in enumerate(rows) if r["code"].upper() == CANCEL]
        after = rows[cancel_idx[-1] + 1:] if cancel_idx else []
        out.append({
            "tn": tn,
            "n": len(rows),
            "hasCancel": bool(cancel_idx),
            "cancelAt": rows[cancel_idx[-1]]["time"] if cancel_idx else "",
            "after": len(after),
            "events": rows,
        })
        print(f"[{i:>2}/{len(tns)}] {tn}  事件 {len(rows):>2}  取消 {'有@'+rows[cancel_idx[-1]]['time'] if cancel_idx else '无'}  取消后 {len(after)}")

    Path(args.json_out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细已写入 {args.json_out}")


if __name__ == "__main__":
    main()
