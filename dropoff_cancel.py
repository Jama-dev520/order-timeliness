#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""投递（drop off）后被取消的异常订单清单。

**什么算异常**
    客户已经把包裹交出去了（投递事件），系统却把它取消了（SHIPCCUS001），
    货在点位/门店，人在系统里却查不到这一单 —— 要么取回、要么补录、要么重发。
    这类单靠「停单清单」看不出来（停单只统计取消后没动过的，不区分是否已交寄）。

**判定口径**
    投递事件：isEventCode = FIRST_SCAN（US 的 DOUS002 / UK 的 SHRSERPTESDE002 等），
              取最早一条，代表客户首次交寄。
    取消事件：jvEventCode = SHIPCCUS001（面单过期 / 订单取消），取最后一次。

    同一单同时有这两者，才进本清单，并按「时序」分三档：

      倒挂（inverted）  取消时间 **早于** 投递时间
                        → 面单已经被取消/过期，客户却还是把包裹投了 → 最需要人处理
      无后续（noResume）投递之后被取消，取消之后**再没有任何节点**
                        → 货在点位上，单子死了 → 要跟进
      已恢复（resumed）  投递之后被取消，但取消之后**还有更晚的节点**
                        → 已经重新流转，留档可追溯，不算要跟进

    测试单（收件人占位）单独标注，不进「要跟进」。

用法：
    python dropoff_cancel.py                                  # 默认 14 天窗口
    python dropoff_cancel.py --from 2026/10/29 --to 2026/11/03
    python dropoff_cancel.py --cache data/_ev_cache_xxx.json   # 复用已拉取的事件缓存
    python dropoff_cancel.py --render-only                     # 只重新渲染页面
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import scraper as sc

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
# 页面输出位置：K8S 里落到共享卷（容器根是只读镜像层），本地默认项目根目录
HTML_OUT = Path(os.getenv("DROPOFF_HTML_OUT") or (DATA / "dropoff_cancel.html"))

CANCEL_CODE = "SHIPCCUS001"
# Depop 单（UDP* 前缀）用 UD 后缀：SHIPCCUD001，语义相同
CANCEL_CODES = (CANCEL_CODE, "SHIPCCUD001")

# 取消原因文案 → 中文
DESC_MAP = {
    "shipment.expired": "面单过期",
    "shipment.cancelled": "订单取消",
    "label expired, never dropped off": "面单过期，从未交寄",
    "shipment cancelled": "订单取消",
    "context.event_description": "面单过期（后端描述缺失）",
}

KIND_LABEL = {
    "inverted": "时序倒挂",
    "noResume": "取消后无后续",
    "resumed": "取消后已恢复",
}


def cn_desc(desc: str) -> str:
    key = (desc or "").strip().lower()
    return DESC_MAP.get(key, desc or "面单过期/取消")


def _code(ev) -> str:
    return str(ev.get("jvEventCode") or "").strip().upper()


def _vendor(ev) -> str:
    return str(ev.get("vendorCode") or "").strip().upper()


def analyze(rec: dict, events: list) -> dict | None:
    """这个单是不是「投递后被取消」的异常单？是则返回明细，否则 None。"""
    seq = sorted([(t, e) for t, e in
                   ((sc.event_time(ev), ev) for ev in (events or [])) if t],
                  key=lambda x: x[0])

    drops = [(t, e) for t, e in seq if sc.is_drop_event(e)]
    cancels = [(t, e) for t, e in seq if _code(e) in CANCEL_CODES]
    if not drops or not cancels:
        return None

    drop_t, drop_ev = drops[0]
    cancel_t, cancel_ev = cancels[-1]
    later = [(t, e) for t, e in seq if t > cancel_t]

    if cancel_t < drop_t:
        kind = "inverted"
    elif later:
        kind = "resumed"
    else:
        kind = "noResume"

    drop_raw = str(drop_ev.get("vendorCode") or "").strip().upper()
    drop_vendor = sc.map_vendor(drop_raw, drop_ev.get("vendorName"))

    created = sc.parse_time(rec.get("orderCreatedTimestamp") or rec.get("orderCreatedTime"))
    h_since = round((datetime.now(sc.TZ_CN) - cancel_t).total_seconds() / 3600, 2)

    return {
        "kind": kind,
        "kindLabel": KIND_LABEL[kind],
        "waybill": rec.get("trackingNumber") or "",
        "lastMileWaybill": rec.get("lastMileTrackingNumber") or "",
        "referenceNumber": rec.get("referenceNumber") or "",
        "region": (rec.get("region") or "").upper(),
        "dropVendor": drop_vendor,
        "dropVendorRaw": drop_raw,
        "dropCode": _code(drop_ev),
        "dropDesc": drop_ev.get("oprEnDes") or drop_ev.get("oprCnDes") or "",
        "dropAt": drop_t.strftime("%Y-%m-%d %H:%M:%S"),
        "cancelAt": cancel_t.strftime("%Y-%m-%d %H:%M:%S"),
        "cancelCode": CANCEL_CODE,
        "cancelDesc": cancel_ev.get("oprEnDes") or cancel_ev.get("oprCnDes") or "",
        "cancelDescCn": cn_desc(cancel_ev.get("oprEnDes") or cancel_ev.get("oprCnDes")),
        "cancelVendor": _vendor(cancel_ev),
        "cancelCount": len(cancels),
        "orderCreateTime": created.strftime("%Y-%m-%d %H:%M:%S") if created else "",
        "hCreateToDrop": sc.hours_between(created, drop_t),
        "hDropToCancel": round((cancel_t - drop_t).total_seconds() / 3600, 2),
        "hSinceCancel": h_since,
        "laterCount": len(later),
        "laterCodes": " → ".join(_code(e) for _t, e in later),
        "laterAt": later[0][0].strftime("%Y-%m-%d %H:%M:%S") if later else "",
        "destState": rec.get("consigneeState") or "",
        "consignee": (rec.get("consigneeFullName") or "").strip(),
        "consigneeAddr": (rec.get("consigneeAddr1") or rec.get("consigneeAddr") or "").strip(),
        "product": rec.get("serviceProductCode") or "",
        "events": [
            {"time": t.strftime("%Y-%m-%d %H:%M:%S"), "code": _code(e),
             "desc": e.get("oprEnDes") or e.get("oprCnDes") or "",
             "vendor": e.get("vendorName") or _vendor(e)}
            for t, e in seq
        ],
    }


def collect(start: str, end: str, region: str = "us",
            cache: Path | None = None) -> tuple[list[dict], dict]:
    cfg = sc.region_cfg(region)

    if cache and cache.exists():
        blob = json.loads(cache.read_text(encoding="utf-8"))
        pairs = [(x["rec"], x.get("evs") or []) for x in blob
                 if str((x.get("rec") or {}).get("_region") or region).lower() == region.lower()]
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

    # 测试单标记要在全量订单上算，否则同地址推定拿不到上下文
    flags = sc.test_flags([
        (cfg["code"].upper(),
         (r.get("consigneeFullName") or "").strip(),
         (r.get("consigneeAddr1") or r.get("consigneeAddr") or "").strip())
        for r, _ in pairs
    ])

    rows = []
    for (rec, evs), is_test in zip(pairs, flags):
        rec = dict(rec)
        rec["region"] = cfg["code"].upper()
        row = analyze(rec, evs)
        if not row:
            continue
        row["isTest"] = bool(is_test)
        rows.append(row)

    rows.sort(key=lambda r: r["dropAt"], reverse=True)

    drops = 0
    for _rec, evs in pairs:
        if any(sc.is_drop_event(e) for e in (evs or [])):
            drops += 1

    todo = [r for r in rows if not r["isTest"] and r["kind"] in ("inverted", "noResume")]
    meta = {
        "generatedAt": datetime.now(sc.TZ_CN).strftime("%Y-%m-%d %H:%M"),
        "dateFrom": start, "dateTo": end,
        "region": cfg["code"].upper(),
        "orders": len(pairs),
        "dropOrders": drops,
        "hits": len(rows),
        "todo": len(todo),
        "inverted": sum(1 for r in rows if r["kind"] == "inverted"),
        "noResume": sum(1 for r in rows if r["kind"] == "noResume"),
        "resumed": sum(1 for r in rows if r["kind"] == "resumed"),
        "testHits": sum(1 for r in rows if r["isTest"]),
        "dropVendors": dict(Counter(r["dropVendor"] for r in rows)),
        "cancelDesc": dict(Counter(r["cancelDescCn"] for r in rows)),
        "rule": ("投递事件（isEventCode=FIRST_SCAN / DOUS002）+ 取消事件（SHIPCCUS001）同时存在；"
                 "取消早于投递=时序倒挂，投递后取消且无更晚节点=取消后无后续，"
                 "投递后取消但仍有更晚节点=已恢复"),
    }
    return rows, meta


def write_csv(rows: list[dict]) -> Path:
    out = DATA / "dropoff_cancel.csv"
    cols = ["kindLabel", "kind", "waybill", "lastMileWaybill", "region",
            "dropVendor", "dropCode", "dropAt", "cancelAt", "cancelDescCn",
            "hDropToCancel", "hSinceCancel", "laterCount", "laterCodes",
            "orderCreateTime", "destState", "consignee", "consigneeAddr",
            "isTest", "product"]
    try:
        fh = out.open("w", newline="", encoding="utf-8-sig")
    except PermissionError:
        out = DATA / "dropoff_cancel_new.csv"
        fh = out.open("w", newline="", encoding="utf-8-sig")
    with fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"[csv] -> {out}")
    return out


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>drop off 取消异常清单</title>
<style>
  :root{
    --bg:#f4f5f7; --card:#fff; --text:#1f2329; --muted:#8a919b; --border:#e6e8ec;
    --accent:#2f6bff; --ok:#12a150; --warn:#d97706; --warn-soft:#fff4e5;
    --red:#d62c2c; --red-soft:#fdecec;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
    font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}
  .wrap{max-width:1240px;margin:0 auto;padding:24px 20px 56px}
  h1{font-size:21px;margin:0;font-weight:600;letter-spacing:.2px}
  .sub{color:var(--muted);font-size:12.5px;margin-top:6px}
  .note{background:var(--red-soft);border:1px solid #f5c7c7;color:#8a2020;
    border-radius:9px;padding:12px 14px;font-size:12.5px;margin:16px 0 18px;line-height:1.7}
  .note b{color:#6d1414}
  .note code{background:#fff;border:1px solid #f0d0d0;border-radius:4px;padding:1px 5px;font-size:11.5px}
  .kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
  .kpi{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
  .kpi .lab{color:var(--muted);font-size:12px;margin-bottom:6px}
  .kpi .num{font-size:25px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1.1}
  .kpi .num.bad{color:var(--red)} .kpi .num.warn{color:var(--warn)} .kpi .num.ok{color:var(--ok)}
  .kpi .tip{font-size:11.5px;color:var(--muted);margin-top:5px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:4px 0 0}
  .toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:14px 16px 12px}
  .tabs{display:inline-flex;background:#f0f1f3;border-radius:8px;padding:3px;gap:2px}
  .tabs button{border:0;background:transparent;color:var(--muted);font-size:12.5px;
    padding:6px 13px;border-radius:6px;cursor:pointer;font-weight:500;transition:.15s}
  .tabs button.on{background:#fff;color:var(--text);box-shadow:0 1px 3px rgba(0,0,0,.08)}
  .toolbar input[type=text]{flex:1;min-width:170px;padding:7px 10px;border:1px solid var(--border);
    border-radius:7px;font-size:13px;outline:none}
  .toolbar input[type=text]:focus{border-color:var(--accent)}
  .chk{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;cursor:pointer;user-select:none}
  .count{color:var(--muted);font-size:12.5px;margin-left:auto}
  table{width:100%;border-collapse:collapse;font-size:13px}
  thead th{text-align:left;font-weight:500;color:var(--muted);font-size:12px;
    padding:9px 10px;border-top:1px solid var(--border);border-bottom:1px solid var(--border);
    white-space:nowrap;cursor:pointer;user-select:none;background:#fafbfc}
  thead th:hover{color:var(--text)}
  tbody td{padding:9px 10px;border-bottom:1px solid #f1f2f4;vertical-align:middle}
  tbody tr.row{cursor:pointer}
  tbody tr.row:hover{background:#f8f9fb}
  tbody tr.row.open{background:#f4f7ff}
  .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
  .tag{display:inline-block;padding:1px 7px;border-radius:5px;font-size:11px;font-weight:500;
    border:1px solid transparent;white-space:nowrap}
  .tag.red{background:var(--red-soft);color:var(--red);border-color:#f6cfcf}
  .tag.warn{background:var(--warn-soft);color:#9a6a00;border-color:#f2e2b8}
  .tag.ok{background:#eaf7ef;color:var(--ok);border-color:#cfedd9}
  .tag.t{background:#f0f1f3;color:#6b7280;border-color:#e3e5e9}
  .tag.v{background:#eef2ff;color:#3559c9;border-color:#d9e1fb}
  .chain{font-size:11.5px;color:#6b7280;line-height:1.45;max-width:250px}
  .det td{background:#fafbfd;padding:0 10px 14px}
  .tl{border-left:2px solid var(--border);margin:8px 0 0 6px;padding-left:14px}
  .tl-item{position:relative;padding:5px 0}
  .tl-item:before{content:'';position:absolute;left:-19px;top:12px;width:7px;height:7px;
    border-radius:50%;background:#c9cdd4}
  .tl-item.key:before{background:var(--red);box-shadow:0 0 0 3px var(--red-soft)}
  .tl-item.drop:before{background:var(--accent);box-shadow:0 0 0 3px #e8efff}
  .tl-t{font-size:12px;color:var(--muted);display:flex;gap:10px;align-items:baseline}
  .tl-code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11.5px;color:var(--text);
    background:#eef0f3;border-radius:4px;padding:1px 6px}
  .tl-d{font-size:12.5px;margin-top:2px}
  .tl-v{font-size:11.5px;color:var(--muted);margin-top:1px}
  .empty{padding:36px;text-align:center;color:var(--muted);font-size:13px}
  footer{margin-top:20px;color:var(--muted);font-size:11.5px;text-align:center}
  @media(max-width:980px){.kpis{grid-template-columns:repeat(2,1fr)}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>drop off 取消异常清单</h1>
    <div class="sub" id="sub"></div>
  </header>

  <div class="note">
    <b>异常定义</b>：客户已经把包裹交出去了（投递事件 <code>isEventCode=FIRST_SCAN</code>，
    US 即 <code>DOUS002</code>），系统却把它取消（<code>SHIPCCUS001</code>）——
    货在点位/门店，系统里这一单却死了，人工要决定取回、补录还是重发。<br/>
    <b>怎么分档</b>：<br/>
    · <b>时序倒挂</b>：取消时间 <b>早于</b> 投递时间 —— 面单已取消/过期，客户却还是把包裹投了，<b>最需要人处理</b>；<br/>
    · <b>取消后无后续</b>：投递之后被取消，取消之后再没有任何节点 —— 货留在点位上，<b>要跟进</b>；<br/>
    · <b>取消后已恢复</b>：投递之后被取消，但取消之后还有更晚的节点 —— 已重新流转，留档可追溯。<br/>
    「要跟进」= 生产单 + （时序倒挂 或 取消后无后续）。测试单（收件人占位）单独归类，不计入。
  </div>

  <div class="kpis" id="kpis"></div>

  <div class="card">
    <div class="toolbar">
      <div class="tabs" id="tabs">
        <button data-g="TODO" class="on">要跟进</button>
        <button data-g="ALL">全部</button>
        <button data-g="INVERTED">时序倒挂</button>
        <button data-g="NORESUME">取消后无后续</button>
        <button data-g="RESUMED">已恢复</button>
        <button data-g="TEST">测试单</button>
      </div>
      <input type="text" id="q" placeholder="搜索运单号 / 收件人 / 州 / 地址"/>
      <span class="count" id="count"></span>
    </div>
    <table>
      <thead id="th"></thead>
      <tbody id="tb"></tbody>
    </table>
    <div class="empty" id="empty" style="display:none">没有符合条件的单</div>
  </div>

  <footer id="foot"></footer>
</div>

<script>
const PAYLOAD = __PAYLOAD__;
const META = PAYLOAD.meta || {};
const ROWS = (PAYLOAD.rows || []).slice();

const isTest = r => !!r.isTest;
const isTodo = r => !isTest(r) && (r.kind === 'inverted' || r.kind === 'noResume');
const F = {
  ALL:      () => true,
  TODO:     isTodo,
  INVERTED: r => r.kind === 'inverted',
  NORESUME: r => r.kind === 'noResume',
  RESUMED:  r => r.kind === 'resumed',
  TEST:     isTest,
};
const state = { group:'TODO', q:'', sort:'', desc:true, open:new Set() };

const nf = v => (v === null || v === undefined || v === '') ? '—'
  : (Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(1));
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

const COLS = [
  {k:'waybill',        t:'运单号'},
  {k:'kindLabel',      t:'类型'},
  {k:'dropVendor',     t:'投递方'},
  {k:'dropAt',         t:'投递时间'},
  {k:'cancelAt',       t:'取消时间'},
  {k:'cancelDescCn',   t:'取消原因'},
  {k:'hDropToCancel',  t:'投递→取消'},
  {k:'hSinceCancel',   t:'距今'},
  {k:'destState',      t:'州'},
  {k:'consignee',      t:'收件人'},
];

function renderKpis(){
  const n = g => ROWS.filter(F[g]).length;
  const todo = n('TODO'), inv = n('INVERTED'), nr = n('NORESUME'), rs = n('RESUMED'), tt = n('TEST');
  const dv = (META.dropVendors || {});
  document.getElementById('kpis').innerHTML = `
    <div class="kpi"><div class="lab">要跟进（生产 · 未恢复）</div>
      <div class="num bad">${todo}</div>
      <div class="tip">时序倒挂 ${inv} 单 · 取消后无后续 ${nr} 单</div></div>
    <div class="kpi"><div class="lab">时序倒挂</div>
      <div class="num bad">${inv}</div>
      <div class="tip">取消早于投递：面单已失效客户仍投了</div></div>
    <div class="kpi"><div class="lab">投递方分布</div>
      <div class="num" style="font-size:19px">${Object.keys(dv).length
        ? Object.entries(dv).map(([k,v])=>`${k} ${v}`).join(' · ') : '—'}</div>
      <div class="tip">异常单里客户交给了谁</div></div>
    <div class="kpi"><div class="lab">已恢复 / 测试单</div>
      <div class="num ok">${rs}<span style="font-size:13px;color:var(--muted)"> / ${tt}</span></div>
      <div class="tip">已恢复 ${rs} 单留档 · 测试单 ${tt} 单不算</div></div>`;
}

function current(){
  let rows = ROWS.filter(F[state.group] || F.ALL);
  if (state.q){
    const q = state.q.toLowerCase();
    rows = rows.filter(r => [r.waybill, r.lastMileWaybill, r.consignee, r.destState,
                             r.consigneeAddr, r.laterCodes]
      .some(v => (v||'').toLowerCase().includes(q)));
  }
  const k = state.sort;
  if (k){
    const d = state.desc ? -1 : 1;
    rows = rows.sort((x,y)=>{
      const a = x[k], b = y[k];
      if (a === null || a === undefined || a === '') return 1;
      if (b === null || b === undefined || b === '') return -1;
      if (typeof a === 'number' && typeof b === 'number') return (a-b)*d;
      return String(a).localeCompare(String(b))*d;
    });
  } else {
    rows = rows.sort((x,y)=>{
      const order = {inverted:0, noResume:1, resumed:2};
      if (order[x.kind] !== order[y.kind]) return order[x.kind] - order[y.kind];
      return String(y.dropAt||'').localeCompare(String(x.dropAt||''));
    });
  }
  return rows;
}

function cellText(k, r){
  switch(k){
    case 'waybill': return `<span class="mono">${esc(r.waybill)}</span>`;
    case 'kindLabel':
      if (r.kind === 'inverted') return '<span class="tag red">时序倒挂</span>';
      if (r.kind === 'noResume') return '<span class="tag warn">取消后无后续</span>';
      return '<span class="tag ok">取消后已恢复</span>';
    case 'dropVendor':
      return `<span class="tag v">${esc(r.dropVendor || '—')}</span>`
        + (r.dropCode ? ` <span class="mono" style="color:var(--muted)">${esc(r.dropCode)}</span>` : '');
    case 'dropAt': return esc(r.dropAt || '') || '—';
    case 'cancelAt': return esc(r.cancelAt || '') || '—';
    case 'cancelDescCn': return esc(r.cancelDescCn || '') || '—';
    case 'hDropToCancel':
      return `<span style="color:${r.hDropToCancel < 0 ? 'var(--red)' : 'inherit'}">`
        + `${nf(r.hDropToCancel)} h</span>`;
    case 'hSinceCancel': return nf(r.hSinceCancel) + ' h';
    case 'consignee':
      return (esc(r.consignee) || '—')
        + (isTest(r) ? ' <span class="tag t">测试</span>' : '')
        + (r.laterCount ? ` <span class="tag t">后续${r.laterCount}节点</span>` : '');
    default: return esc(r[k]) || '—';
  }
}

function timeline(r){
  if (!r.events || !r.events.length) return '';
  return '<div class="tl">' + r.events.map(e => {
    const c = (e.code||'').toUpperCase();
    const cls = c === 'SHIPCCUS001' ? ' key' : (c === 'DOUS002' || c === 'DOUD002' ? ' drop' : '');
    return `<div class="tl-item${cls}">
      <div class="tl-t"><span class="tl-code">${esc(e.code)}</span>${esc(e.time)}</div>
      <div class="tl-d">${esc(e.desc||'')}</div>
      ${e.vendor?`<div class="tl-v">vendor ${esc(e.vendor)}</div>`:''}
    </div>`;
  }).join('') + '</div>';
}

function detail(r){
  const n = COLS.length;
  return `<tr class="det"><td colspan="${n}">
    <div style="font-size:12px;color:var(--muted);margin:8px 0 6px">
      运单 ${esc(r.waybill)} · 投递 ${esc(r.dropAt)}（${esc(r.dropVendor)} · ${esc(r.dropCode)}）
      → 取消 ${esc(r.cancelAt)}（${esc(r.cancelDescCn)}），
      间隔 ${nf(r.hDropToCancel)} h${r.kind==='inverted' ? '（<b style="color:var(--red)">取消早于投递</b>）' : ''}
      ${r.laterCount ? ` · 取消后 ${r.laterCount} 条节点：${esc(r.laterCodes)}` : ' · 取消后无任何节点'}
      ${isTest(r) ? '（测试单，不计入要跟进）' : ''}
    </div>${timeline(r)}</td></tr>`;
}

function renderTable(){
  const rows = current();
  document.getElementById('count').textContent = `${rows.length} 单`;
  document.getElementById('empty').style.display = rows.length ? 'none' : 'block';
  document.getElementById('th').innerHTML = '<tr>' + COLS.map(c =>
    `<th data-s="${c.k}">${c.t}${state.sort===c.k ? (state.desc ? ' ↓' : ' ↑') : ''}</th>`).join('') + '</tr>';
  document.getElementById('tb').innerHTML = rows.map(r => {
    const open = state.open.has(r.waybill);
    return `<tr class="row${open?' open':''}" data-wb="${esc(r.waybill)}">`
      + COLS.map(c => `<td>${cellText(c.k, r)}</td>`).join('') + '</tr>'
      + (open ? detail(r) : '');
  }).join('');
}

function setGroup(g){
  state.group = g;
  [...document.querySelectorAll('#tabs button')].forEach(x => x.classList.toggle('on', x.dataset.g===g));
  state.sort=''; state.desc=true; renderTable();
}

document.getElementById('tabs').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  setGroup(b.dataset.g);
});
document.getElementById('q').addEventListener('input', e => { state.q=e.target.value.trim(); renderTable(); });
document.getElementById('th').addEventListener('click', e => {
  const th = e.target.closest('th'); if (!th) return;
  const k = th.dataset.s;
  if (state.sort === k) state.desc = !state.desc; else { state.sort = k; state.desc = true; }
  renderTable();
});
document.getElementById('tb').addEventListener('click', e => {
  const tr = e.target.closest('tr.row'); if (!tr) return;
  const wb = tr.dataset.wb;
  if (state.open.has(wb)) state.open.delete(wb); else state.open.add(wb);
  renderTable();
});

document.getElementById('sub').textContent =
  `${META.region || 'US'} · ${(META.dateFrom||'').slice(0,10)} ~ ${(META.dateTo||'').slice(0,10)}`
  + ` · 生成于 ${META.generatedAt} · 窗口内 ${META.orders} 单，其中 ${META.dropOrders} 单有投递事件`;

document.getElementById('foot').textContent =
  `命中 ${META.hits} 单（要跟进 ${META.todo} 单）· 判定：${META.rule || ''} · 时间口径北京时间 UTC+8`;

renderKpis(); renderTable();
</script>
</body>
</html>
"""


def render_html(payload: dict) -> Path:
    js = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    out = HTML_OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(PAGE.replace("__PAYLOAD__", js), encoding="utf-8")
    print(f"[html] -> {out}")
    return out


def main():
    ap = argparse.ArgumentParser(description="挑出「已投递却被取消」的异常订单")
    ap.add_argument("--region", default="us")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--from", dest="date_from", default=None)
    ap.add_argument("--to", dest="date_to", default=None)
    ap.add_argument("--cache", default=None, help="复用事件缓存 JSON")
    ap.add_argument("--render-only", action="store_true", help="只用已有 JSON 重新渲染页面")
    args = ap.parse_args()

    if args.render_only:
        payload = json.loads((DATA / "dropoff_cancel.json").read_text(encoding="utf-8"))
        write_csv(payload.get("rows") or [])
        render_html(payload)
        return

    d_to = args.date_to
    if args.date_from and not d_to:
        d_to = datetime.now(sc.TZ_CN).strftime("%Y/%m/%d")
    if args.date_from and d_to:
        start, end = sc._norm_date(args.date_from), sc._norm_date(d_to, end=True)
    else:
        start, end = sc._range(args.days, None, None)

    cache = Path(args.cache) if args.cache else None
    rows, meta = collect(start, end, args.region, cache)

    DATA.mkdir(exist_ok=True)
    payload = {"meta": meta, "rows": rows}
    (DATA / "dropoff_cancel.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[json] -> {DATA / 'dropoff_cancel.json'}")
    write_csv(rows)
    render_html(payload)

    print(f"\n窗口 {start} ~ {end} | 订单 {meta['orders']} 单 | 有投递事件 {meta['dropOrders']} 单")
    print(f"投递后被取消 {meta['hits']} 单：时序倒挂 {meta['inverted']} · "
          f"取消后无后续 {meta['noResume']} · 已恢复 {meta['resumed']}（测试 {meta['testHits']}）")
    print(f"要跟进（生产 · 未恢复）{meta['todo']} 单")
    for r in rows:
        tag = "测试" if r["isTest"] else "生产"
        print(f"  [{tag}] {r['waybill']} {r['dropVendor']:<9} 投递 {r['dropAt'][5:16]}"
              f" → 取消 {r['cancelAt'][5:16]} {r['kindLabel']}"
              f"（{r['hDropToCancel']}h）{r['consignee'][:16]}")


if __name__ == "__main__":
    main()
