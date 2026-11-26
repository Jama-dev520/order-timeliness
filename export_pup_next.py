#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""导出「PUDO 的 PUP 节点 → 下一个节点是 SPX 的 57102 / 52002」的物流单。

口径（2026-11-26 与业务确认）：
    节点 = 事件的 vendorEventCode
    PUDO = vendorCode 'PU'，PUP 节点 = 'PUP'（Package ready for dispatch）
    SPX  = vendorCode 'SX'，目标节点 = '57102'（Picked up by delivery carrier）
                                  或 '52002'（Destination Post Station Scan）
    「下一个节点」= 按操作时间排序后紧邻的下一条事件

用法：
    python export_pup_next.py                 # 用最新一轮快照 data/_ev_run.json
    python export_pup_next.py --out x.xlsx    # 指定输出
    python export_pup_next.py --cache 其他快照.json
    python export_pup_next.py --csv           # 只出 CSV（不装 openpyxl 也能用）
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import scraper as sc

DATA_DIR = Path(__file__).resolve().parent / "data"
PUDO_VENDOR = "PU"
PUDO_NODE = "PUP"
SPX_VENDOR = "SX"
SPX_NODES = ("57102", "52002")

COLS = ["物流单号", "下个节点供应商", "供应商节点", "供应商事件描述", "供应商节点操作时间"]


def _ts(e: dict) -> int:
    """事件的排序/展示时间：优先 oprTime（操作时间），退化到 createTime。"""
    for k in ("oprTime", "createTime"):
        v = e.get(k)
        if isinstance(v, (int, float)) and v:
            return int(v)
    return 0


def _desc(e: dict) -> str:
    return (e.get("jvEventEnRemark") or e.get("oprEnDes")
            or e.get("jvEventCnRemark") or e.get("oprCnDes") or "").strip()


def collect(pairs: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for item in pairs:
        rec = item.get("rec") or {}
        tn = str(rec.get("trackingNumber") or rec.get("lastMileWaybill") or "").strip()
        evs = item.get("evs") or []
        if not tn or not evs:
            continue
        # 同一秒内的事件用 oprTimestamp 兜底，避免顺序漂移
        ordered = sorted(evs, key=lambda e: (_ts(e), int(str(e.get("oprTimestamp") or 0) or 0)))
        for i, cur in enumerate(ordered):
            if (str(cur.get("vendorCode") or "").upper() != PUDO_VENDOR
                    or str(cur.get("vendorEventCode") or "").upper() != PUDO_NODE):
                continue
            if i + 1 >= len(ordered):
                continue
            nxt = ordered[i + 1]
            node = str(nxt.get("vendorEventCode") or "").strip()
            if str(nxt.get("vendorCode") or "").upper() != SPX_VENDOR or node not in SPX_NODES:
                continue
            dt = sc.parse_time(_ts(nxt))
            rows.append({
                "物流单号": tn,
                "下个节点供应商": (nxt.get("vendorName") or "").strip(),
                "供应商节点": node,
                "供应商事件描述": _desc(nxt),
                "供应商节点操作时间": dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "",
            })
    # 同一单可能多次出现同样的配对，去重但保留顺序
    seen, out = set(), []
    for r in rows:
        k = tuple(r.values())
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    out.sort(key=lambda r: (r["供应商节点操作时间"], r["物流单号"]), reverse=True)
    return out


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig：Excel 双击打开不乱码
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)


def write_xlsx(rows: list[dict], path: Path) -> bool:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError:
        return False
    wb = Workbook()
    ws = wb.active
    ws.title = "PUP→SPX"
    ws.append(COLS)
    head = PatternFill("solid", fgColor="FFF3E0")
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = head
        c.alignment = Alignment(horizontal="center", vertical="center")
    for r in rows:
        ws.append([r[c] for c in COLS])
    widths = [24, 16, 14, 30, 22]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = f"A1:E{len(rows) + 1}"
    wb.save(path)
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(sc.EV_RUN), help="事件快照（默认 data/_ev_run.json）")
    ap.add_argument("--out", default="", help="输出路径（.xlsx 或 .csv）")
    ap.add_argument("--csv", action="store_true", help="只输出 CSV")
    a = ap.parse_args()

    p = Path(a.cache)
    if not p.exists():
        print(f"找不到事件快照 {p}\n先跑一轮 python refresh_all.py，或指定 --cache")
        return 2
    pairs = json.loads(p.read_text(encoding="utf-8"))
    rows = collect(pairs)
    print(f"快照 {p.name}：{len(pairs)} 单")

    n_orders = len({r["物流单号"] for r in rows})
    by_node: dict[str, int] = {}
    for r in rows:
        by_node[r["供应商节点"]] = by_node.get(r["供应商节点"], 0) + 1
    print(f"命中 {len(rows)} 条配对，涉及 {n_orders} 单")
    for k, v in sorted(by_node.items()):
        print(f"  节点 {k}: {v} 条")

    stem = DATA_DIR / "PUP到SPX导出"
    if a.out:
        out = Path(a.out)
        xlsx_mode = out.suffix.lower() == ".xlsx" and not a.csv
    else:
        out = stem.with_suffix(".csv" if a.csv else ".xlsx")
        xlsx_mode = not a.csv

    if xlsx_mode:
        if write_xlsx(rows, out):
            print(f"已写出 {out}")
        else:
            out = out.with_suffix(".csv")
            write_csv(rows, out)
            print(f"（没装 openpyxl，改出 CSV）{out}")
    else:
        write_csv(rows, out)
        print(f"已写出 {out}")

    for r in rows[:5]:
        print("  ", " | ".join(r[c] for c in COLS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
