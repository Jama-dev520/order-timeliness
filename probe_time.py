# -*- coding: utf-8 -*-
"""时区字段自洽性探针：判断 oprTime / oprTimeWithZone / *Timestamp 谁可信。

结论口径：同一绝对时刻若被两个字段独立交叉验证，则该字段可信。
"""
import json
import re
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

BJ = timezone(timedelta(hours=8))
HOST = "spk-hub-us-api.orangeconnex.com"


def load_ck():
    for line in open(".env", encoding="utf-8"):
        line = line.strip()
        if line.startswith("OC_COOKIE"):
            return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("no cookie")


CK = load_ck()


def get(url):
    req = urllib.request.Request(url, headers={"Cookie": CK, "Accept": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=60))


def offset_of(s):
    m = re.search(r"([+-]\d{2}):?(\d{2})$", s)
    if not m:
        return None
    sign = 1 if m.group(1)[0] == "+" else -1
    return sign * (int(m.group(1)[1:]) + int(m.group(2)) / 60)


def parse(s):
    return datetime.fromisoformat(s)


q = urllib.parse.urlencode({
    "page": 1, "limit": 100,
    "startDate": "2026/10/20 00:00:00", "endDate": "2026/11/03 23:59:59",
    "destinationCountry": "US",
})
d = get(f"https://{HOST}/aquarius-order/order/tracking-number/v1/tracking-number-list?{q}")
orders = d["result"]["list"]
print(f"订单数: {len(orders)}")


def fetch_events(o):
    tn = o["trackingNumber"]
    try:
        e = get(f"https://{HOST}/aquarius-order/order/tracking-number/v1/tracking-event-list/{tn}")
        evs = e.get("result") or []
        if isinstance(evs, dict):
            evs = evs.get("list") or []
        return o, evs
    except Exception as ex:
        return o, []


with ThreadPoolExecutor(max_workers=12) as ex:
    pairs = list(ex.map(fetch_events, orders))

hdr_offset = Counter()
hdr_mismatch = 0    # 订单头: parse(firstScanTime) 与 firstScanTimeStamp 不一致
hdr_ok = 0
ev_offset = Counter()
ev_diff = Counter()          # parse(oprTimeWithZone) - oprTime (分钟)
ev_same_str_diff_time = 0    # 同字符串却给出不同 oprTime
first_scan_cross_ok = 0      # DOUS002/首个取件事件 parse() == firstScanTimeStamp
first_scan_cross_bad = 0

samples = []

for o, evs in pairs:
    fz, fs = o.get("firstScanTime"), o.get("firstScanTimeStamp")
    if fz and fs:
        hdr_offset[offset_of(fz)] += 1
        if abs(parse(fz).timestamp() * 1000 - fs) < 1000:
            hdr_ok += 1
        else:
            hdr_mismatch += 1

    by_str = {}
    for ev in evs:
        ot, oz = ev.get("oprTime"), ev.get("oprTimeWithZone")
        if not ot or not oz:
            continue
        ev_offset[offset_of(oz)] += 1
        diff = round((parse(oz).timestamp() - ot / 1000) / 60)
        ev_diff[diff] += 1
        by_str.setdefault(oz, set()).add(ot)

    for s, ots in by_str.items():
        if len(ots) > 1:
            ev_same_str_diff_time += 1

    # 交叉验证：取件事件（DOUS002 / 最早事件）的 parsed 时间 vs 订单头 firstScanTimeStamp
    pick = [ev for ev in evs if ev.get("jvEventCode") in ("DOUS002", "PUUS002", "LMSUS002")]
    if pick and fs:
        p = min(pick, key=lambda x: x.get("oprTime") or 0)
        if p.get("oprTimeWithZone"):
            if abs(parse(p["oprTimeWithZone"]).timestamp() * 1000 - fs) < 1000:
                first_scan_cross_ok += 1
            else:
                first_scan_cross_bad += 1
    if len(samples) < 4 and evs:
        samples.append((o["trackingNumber"], fz, fs, [(e.get("jvEventCode"), e.get("oprTimeWithZone"), e.get("oprTime")) for e in evs[:3]]))

print()
print("=== 订单头 firstScanTime 的偏移分布 ===")
for k, v in hdr_offset.most_common():
    print(f"  UTC{k:+g}: {v}")
print(f"  头字段自洽(parse==firstScanTimeStamp): {hdr_ok} / 不一致: {hdr_mismatch}")

print()
print("=== 事件 oprTimeWithZone 的偏移分布 ===")
for k, v in ev_offset.most_common():
    print(f"  UTC{k:+g}: {v}")

print()
print("=== parse(oprTimeWithZone) - oprTime 差值分布(分钟) ===")
for k, v in ev_diff.most_common(12):
    print(f"  {k:+d} min ({k/60:+.1f}h): {v}")

print()
print(f"同字符串却对应不同 oprTime 的组数: {ev_same_str_diff_time}")
print(f"取件事件 parse(WithZone) == firstScanTimeStamp: 一致 {first_scan_cross_ok} / 不一致 {first_scan_cross_bad}")

print()
print("=== 样本 ===")
for tn, fz, fs, evs in samples:
    print(f"\n{tn}")
    print(f"  firstScanTime       {fz}")
    print(f"  firstScanTimeStamp  {fs} -> " + (datetime.fromtimestamp(fs/1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + " UTC" if fs else "—"))
    for code, oz, ot in evs:
        line = f"  {code:16} {oz}"
        if ot:
            line += f"  oprTime->{datetime.fromtimestamp(ot/1000, timezone.utc):%Y-%m-%d %H:%M:%S} UTC"
        print(line)
