# -*- coding: utf-8 -*-
"""对比两种时间口径（oprTime 毫秒戳 vs oprTimeWithZone）对时效结果的影响。"""
import importlib.util
import sys
from datetime import datetime
from pathlib import Path

spec = importlib.util.spec_from_file_location("scraper", Path(__file__).with_name("scraper.py"))
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)

orig_event_time = sc.event_time


def old_event_time(ev):
    """旧口径：只用毫秒戳。"""
    for key in ("oprTime", "oprTimestamp"):
        v = ev.get(key)
        if not v:
            continue
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n > 1e11:
            n /= 1000
        return datetime.fromtimestamp(n, tz=sc.TZ_CN)
    return None


cfg = sc.region_cfg("us")
s = sc._session()
records, total = sc.fetch_list(s, "2026/10/20 00:00:00", "2026/11/03 23:59:59", limit=100, cfg=cfg)
records = records[:120]
print("订单数:", len(records), "总数:", total)
rows = []
for rec in records:
    evs = sc.fetch_events(s, rec.get("trackingNumber"), cfg)
    sc.event_time = old_event_time
    a = sc.normalize_order(rec, evs, cfg)
    sc.event_time = orig_event_time
    b = sc.normalize_order(rec, evs, cfg)
    rows.append((a, b, evs))

def stat(items, key):
    vals = [o[key] for o in items if o.get(key) is not None]
    neg = [v for v in vals if v < 0]
    return len(vals), (sum(vals) / len(vals) if vals else None), len(neg)

for key in ("hCreateToAScan", "hAScanToRdc", "hRdcToSpx"):
    print()
    print(key)
    n1, m1, g1 = stat([r[0] for r in rows], key)
    n2, m2, g2 = stat([r[1] for r in rows], key)
    f = lambda v: f"{v:.2f}h" if v is not None else "—"
    print(f"  旧 oprTime         n={n1:4d} 均值={f(m1):>9} 负值={g1}")
    print(f"  新 oprTimeWithZone n={n2:4d} 均值={f(m2):>9} 负值={g2}")

print()
print("=== 差异明细（新口径下有 RDC→SPX 的单）===")
shown = 0
for a, b, evs in rows:
    if b.get("hRdcToSpx") is None:
        continue
    d = (a.get("hRdcToSpx") - b["hRdcToSpx"]) if a.get("hRdcToSpx") is not None else None
    print(f"{b['lastMileWaybill'][:22]:24} vendor={b['vendor']:9} "
          f"旧={('%7.2f' % a['hRdcToSpx']) if a.get('hRdcToSpx') is not None else '    —  '} "
          f"新={b['hRdcToSpx']:7.2f}  差={('%+7.2f' % d) if d is not None else '      —'}")
    shown += 1
    if shown >= 15:
        break
print(f"(共 {sum(1 for _, b, _ in rows if b.get('hRdcToSpx') is not None)} 单有 RDC→SPX)")
