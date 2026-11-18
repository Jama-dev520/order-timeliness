#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""挑出命中 SHIPCCUS001（面单过期 / 订单取消）且之后没有任何新节点的单。

**唯一判定口径（业务确认）**
    命中 SHIPCCUS001  **且**  存在一条时间严格晚于该取消节点的新节点
    → 视为「已有作业」，已恢复流转，**不进停单清单**。
    取消节点之后时间轴上再没有任何东西 → 停单。

    注意：判据是「时间先后」，不是「事件链里有没有出现过 A-SCAN」。
    取消发生在 A-SCAN 之后、之后又零动作的单，依然算停单。

清单内再按「取消之前是否已经交寄」拆两组（纯属归因，不改变停单判定）：
  A 组「从未交寄」    —— 取消前只有建单/信息收到，没有 A-SCAN、没有出库、没有 SPX
  B 组「交寄后被撤销」—— 取消前已有 A-SCAN / RDC 出库 / SPX 交接，之后彻底停住

同一单号可能有多次 SHIPCCUS001（重复上报），一律以**最后一次**取消为基准。

输出：
  data/stuck_shipcc.json      停单明细（含完整事件流）
  data/stuck_shipcc.csv       Excel 可直接打开
  data/stuck_recovered.json   被排除的「已有作业」单，留档可追溯
  data/stuck_recovered.csv
  stuck_shipcc.html           独立清单页（自包含，双击即开）
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import scraper as sc

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
# 页面输出位置：K8S 里让它落到共享卷（容器根是只读镜像层），本地默认项目根目录
HTML_OUT = Path(os.getenv("STUCK_HTML_OUT") or (ROOT / "stuck_shipcc.html"))
CODE = "SHIPCCUS001"
# Depop 单（UDP* 前缀）的取消事件用 UD 后缀：SHIPCCUD001，语义相同，一起认。
CODES = (CODE, "SHIPCCUD001")

# SHIPCCUS001 在后台有多种描述文案，统一归类
DESC_MAP = {
    "shipment.expired": "面单过期",
    "shipment.cancelled": "订单取消",
    "label expired, never dropped off": "面单过期，从未交寄",
    "shipment cancelled": "订单取消",
    "context.event_description": "面单过期（后端描述缺失）",
}


def cn_desc(desc: str, code: str) -> str:
    key = (desc or "").strip().lower()
    if key in DESC_MAP:
        return DESC_MAP[key]
    if code.upper() in CODES:
        return desc or "面单过期/取消"
    return desc or ""


def collect(region: str, days: int, date_from: str | None = None,
            date_to: str | None = None) -> tuple[list[dict], list[dict], dict, str, str]:
    start, end = sc._range(days, date_from, date_to)
    cfg = sc.region_cfg(region)
    s = sc._session()

    recs, total, page = [], 0, 1
    while True:
        batch, total = sc.fetch_list(s, start, end, page=page, limit=100, cfg=cfg)
        recs.extend(batch)
        if len(batch) < 100 or len(recs) >= total:
            break
        page += 1
    print(f"[{region}] 订单 {len(recs)} / {total}  {start} ~ {end}")

    # 测试单标记要在全量订单上算，否则同地址推定拿不到上下文
    flags = sc.test_flags([
        (region.upper(),
         (r.get("consigneeFullName") or "").strip(),
         (r.get("consigneeAddr1") or r.get("consigneeAddr") or "").strip())
        for r in recs
    ])
    test_by_wb = {r.get("trackingNumber") or "": f for r, f in zip(recs, flags)}

    def work(rec):
        return rec, sc.fetch_events(s, rec.get("trackingNumber") or "", cfg)

    with ThreadPoolExecutor(max_workers=12) as pool:
        pairs = list(pool.map(work, recs))

    hits, recovered = [], []
    with_code = 0

    for rec, evs in pairs:
        seq = []
        for ev in evs or []:
            t = sc.event_time(ev)
            if t:
                seq.append((t, ev))
        seq.sort(key=lambda x: x[0])
        cancels = [(t, ev) for t, ev in seq
                   if str(ev.get("jvEventCode") or "").strip().upper() in CODES]
        if not cancels:
            continue
        with_code += 1

        # 同一单号可能重复上报取消，以最后一次为准
        t_exp, ev_exp = cancels[-1]

        # 核心判据：取消时间之后是否还有更晚的新节点
        later = [(t, ev) for t, ev in seq if t > t_exp]
        if later:
            first_t, first_ev = later[0]
            recovered.append({
                "waybill": rec.get("trackingNumber") or "",
                "region": region.upper(),
                "cancelAt": t_exp.strftime("%Y-%m-%d %H:%M:%S"),
                "cancelCount": len(cancels),
                "resumeAt": first_t.strftime("%Y-%m-%d %H:%M:%S"),
                "resumeCode": str(first_ev.get("jvEventCode") or ""),
                "resumeDesc": first_ev.get("oprEnDes") or first_ev.get("oprCnDes") or "",
                "resumeVendor": first_ev.get("vendorName") or "",
                "hCancelToResume": round((first_t - t_exp).total_seconds() / 3600, 2),
                "laterCount": len(later),
                "laterCodes": " → ".join(str(e.get("jvEventCode") or "") for _t, e in later),
                "consignee": (rec.get("consigneeFullName") or "").strip(),
                "destState": rec.get("consigneeState") or "",
                "isTest": test_by_wb.get(rec.get("trackingNumber") or ""),
            })
            continue

        norm = sc.normalize_order(rec, evs, cfg)

        # 取消之前是否已经交寄（只用于归因分组，不影响停单判定）
        work_before = []
        for key in ("aScanTime", "rdcOutTime", "spxPickupTime"):
            dt = sc.parse_time(norm.get(key))
            if dt and dt < t_exp:
                work_before.append(dt)
        has_work_before = bool(work_before)

        created = sc.parse_time(rec.get("orderCreatedTimestamp") or rec.get("orderCreatedTime"))
        h_create = sc.hours_between(created, t_exp)
        h_since = (datetime.now(sc.TZ_CN) - t_exp).total_seconds() / 3600

        exp_vs_work = None
        if work_before:
            exp_vs_work = round((t_exp - max(work_before)).total_seconds() / 3600, 2)

        hits.append({
            "group": "B" if has_work_before else "A",
            "waybill": rec.get("trackingNumber") or "",
            "lastMileWaybill": rec.get("lastMileTrackingNumber") or "",
            "referenceNumber": rec.get("referenceNumber") or "",
            "region": region.upper(),
            "vendor": ev_exp.get("vendorName") or ev_exp.get("vendorCode") or "",
            "eventCode": str(ev_exp.get("jvEventCode") or CODE),
            "eventDesc": ev_exp.get("oprEnDes") or ev_exp.get("oprCnDes") or "",
            "eventDescCn": cn_desc(ev_exp.get("oprEnDes") or ev_exp.get("oprCnDes"),
                                   str(ev_exp.get("jvEventCode") or CODE)),
            "expiredTime": t_exp.strftime("%Y-%m-%d %H:%M:%S"),
            "cancelCount": len(cancels),
            "orderCreateTime": created.strftime("%Y-%m-%d %H:%M:%S") if created else "",
            "aScanTime": norm.get("aScanTime") or "",
            "rdcOutTime": norm.get("rdcOutTime") or "",
            "spxPickupTime": norm.get("spxPickupTime") or "",
            "eventCount": len(seq),
            "hCreateToExpire": round(h_create, 2) if h_create is not None else None,
            "hSinceExpire": round(h_since, 2),
            "hExpireVsWork": exp_vs_work,
            "status": norm.get("status") or "",
            "destState": rec.get("consigneeState") or "",
            "consignee": (rec.get("consigneeFullName") or "").strip(),
            "consigneeAddr": (rec.get("consigneeAddr1") or rec.get("consigneeAddr") or "").strip(),
            "isTest": test_by_wb.get(rec.get("trackingNumber") or "", norm.get("isTest")),
            "product": rec.get("serviceProductCode") or "",
            "events": [
                {"time": t.strftime("%Y-%m-%d %H:%M:%S"), "code": str(ev.get("jvEventCode") or ""),
                 "desc": ev.get("oprEnDes") or ev.get("oprCnDes") or "",
                 "vendor": ev.get("vendorName") or ""}
                for t, ev in seq
            ],
        })

    stats = {"orders": len(recs), "withCode": with_code,
             "recovered": len(recovered), "stuck": len(hits)}
    print(f"[{region}] 含 {CODE} 共 {with_code} 单 | 取消已恢复（取消后有新节点）"
          f" {len(recovered)} 单 | 取消未恢复 {len(hits)} 单")
    return hits, recovered, stats, start, end


def _safe_open(path: Path):
    """目标文件可能被 Excel / 编辑器占用，退回带时间戳的副本，不让整个任务失败。"""
    try:
        return path.open("w", newline="", encoding="utf-8-sig"), path
    except PermissionError:
        alt = path.with_name(f"{path.stem}_new{path.suffix}")
        print(f"[warn] {path.name} 被占用（可能在 Excel 里开着），改写 {alt.name}")
        return alt.open("w", newline="", encoding="utf-8-sig"), alt


def write_csv(rows: list[dict], path: Path):
    cols = ["group", "waybill", "lastMileWaybill", "referenceNumber", "region", "vendor",
            "expiredTime", "cancelCount", "orderCreateTime", "hCreateToExpire", "hSinceExpire",
            "hExpireVsWork", "eventCount", "status", "destState", "consignee",
            "consigneeAddr", "isTest", "product", "eventDescCn", "eventDesc"]
    fh, real = _safe_open(path)
    with fh as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"[csv] -> {real}")


def write_recovered(rows: list[dict], path: Path):
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    fh, real = _safe_open(path)
    with fh as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"[csv] -> {real}")


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>SHIPCCUS001 未完成清单</title>
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
  header{margin-bottom:18px}
  h1{font-size:21px;margin:0;font-weight:600;letter-spacing:.2px}
  .sub{color:var(--muted);font-size:12.5px;margin-top:6px}
  .note{background:var(--warn-soft);border:1px solid #f5dcb8;color:#8a5a10;
    border-radius:9px;padding:12px 14px;font-size:12.5px;margin:16px 0 18px;line-height:1.65}
  .note b{color:#6d4508}
  .note code{background:#fff;border:1px solid #f0dcbe;border-radius:4px;padding:1px 5px;font-size:11.5px}
  .mx{background:var(--card);border:1px solid var(--border);border-radius:10px;
    padding:15px 18px 12px;margin-bottom:16px}
  .mx-h{display:flex;align-items:baseline;gap:9px;margin-bottom:4px}
  .mx-h b{font-size:13.5px;font-weight:600}
  .mx-h span{font-size:11.5px;color:var(--muted)}
  .mxt{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}
  .mxt th{text-align:center;font-weight:500;color:var(--muted);font-size:11.5px;
    padding:6px 10px 9px;line-height:1.35}
  .mxt th.rw{text-align:left;width:104px;font-size:12.5px;color:var(--text)}
  .mxt td{padding:10px 10px;text-align:center;border-top:1px solid #f2f3f5}
  .mxt td.rw{text-align:left;font-weight:500}
  .mxt .num{font-size:21px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1.1}
  .mxt .num.zero{color:#c9ced4}
  .mxt td.cell{cursor:pointer;transition:.15s;border-radius:7px}
  .mxt td.cell:hover{background:#f6f7f9}
  .mxt td.on{background:#f0f2f5;box-shadow:inset 0 0 0 1.5px #9aa2ac}
  .mxt td.hot .num{color:var(--red)}
  .mxt td.ok .num{color:var(--ok)}
  .mxt tr.sum td{border-top:1px solid var(--border);background:#fbfbfc;font-weight:600}
  .mxt tr.sum .num{font-size:18px}
  .mxt .flag{display:inline-block;font-size:10.5px;color:var(--red);background:#fdeeee;
    border:1px solid #f6cfcf;border-radius:4px;padding:1px 6px;margin-left:6px;vertical-align:2px}
  .mxt .sub{font-size:10.5px;color:#9aa2ac;display:block;margin-top:2px;font-weight:400}
  .card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:4px 0 0}
  .toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:14px 16px 12px}
  .tabs{display:inline-flex;background:#f0f1f3;border-radius:8px;padding:3px;gap:2px}
  .tabs button{border:0;background:transparent;color:var(--muted);font-size:12.5px;
    padding:6px 13px;border-radius:6px;cursor:pointer;font-weight:500;transition:.15s}
  .tabs button.on{background:#fff;color:var(--text);box-shadow:0 1px 3px rgba(0,0,0,.08)}
  .tabs button.ind{font-size:11.5px;padding:6px 9px;opacity:.8}
  .tabs button[data-g="A"]:before{content:"├ ";opacity:.45}
  .tabs button[data-g="B"]:before{content:"└ ";opacity:.45}
  .k-no{color:var(--red)}
  .k-yes{color:var(--ok)}
  .toolbar input[type=text]{flex:1;min-width:180px;padding:7px 10px;border:1px solid var(--border);
    border-radius:7px;font-size:13px;outline:none}
  .toolbar input[type=text]:focus{border-color:var(--accent)}
  .chk{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;cursor:pointer;
    user-select:none;white-space:nowrap}
  .count{color:var(--muted);font-size:12.5px;margin-left:auto}
  table{width:100%;border-collapse:collapse;font-size:13px}
  thead th{text-align:left;font-weight:500;color:var(--muted);font-size:12px;
    padding:9px 10px;border-top:1px solid var(--border);border-bottom:1px solid var(--border);
    white-space:nowrap;cursor:pointer;user-select:none;background:#fafbfc}
  thead th:hover{color:var(--text)}
  tbody td{padding:10px;border-bottom:1px solid #f1f2f4;vertical-align:middle}
  tbody tr.row{cursor:pointer}
  tbody tr.row:hover{background:#f8f9fb}
  tbody tr.row.open{background:#f4f7ff}
  .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
  .tag{display:inline-block;padding:1px 7px;border-radius:5px;font-size:11px;font-weight:500;
    border:1px solid transparent;white-space:nowrap}
  .tag.a{background:var(--red-soft);color:var(--red);border-color:#f6cfcf}
  .tag.b{background:#eef2ff;color:#3559c9;border-color:#d9e1fb}
  .tag.t{background:#f0f1f3;color:#6b7280;border-color:#e3e5e9}
  .tag.g{background:#eaf7ef;color:var(--ok);border-color:#cfedd9}
  .tag.r{background:#fff8e6;color:#9a6a00;border-color:#f2e2b8}
  .chain{font-size:11.5px;color:#6b7280;line-height:1.45;max-width:260px}
  .funnel{background:var(--card);border:1px solid var(--border);border-radius:10px;
    padding:15px 18px 13px;margin-bottom:16px}
  .fn-h{display:flex;align-items:baseline;gap:10px;margin-bottom:12px}
  .fn-h b{font-size:13.5px;font-weight:600}
  .fn-h span{font-size:11.5px;color:var(--muted)}
  .fn-row{display:flex;align-items:center;gap:11px;margin:7px 0}
  .fn-label{width:172px;flex:none;text-align:right;font-size:12.5px;color:var(--muted)}
  .fn-bar{height:29px;border-radius:6px;display:flex;align-items:center;
    justify-content:flex-end;padding:0 11px;font-size:12.5px;font-weight:600;color:#fff;
    white-space:nowrap;transition:width .35s ease}
  .fn-bar small{font-weight:400;opacity:.8;margin-left:5px;font-size:11px}
  .fn-1{background:#8a919b}
  .fn-2{background:#4a6fd4}
  .fn-3{background:var(--red)}
  .fn-note{font-size:11.5px;color:var(--muted);white-space:nowrap}
  .fn-split{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0 2px 183px}
  .fn-chip{display:inline-flex;align-items:center;gap:6px;font-size:12px;
    border:1px solid var(--border);border-radius:6px;padding:5px 10px;background:#fafbfc}
  .fn-chip i{width:7px;height:7px;border-radius:50%;display:inline-block}
  .fn-chip b{font-weight:600}
  @media(max-width:880px){.fn-label{width:110px}.fn-split{margin-left:121px}}
  .day{color:var(--muted);white-space:nowrap}
  .det td{background:#fafbfd;padding:0 10px 14px}
  .tl{border-left:2px solid var(--border);margin:8px 0 0 6px;padding-left:14px}
  .tl-item{position:relative;padding:6px 0}
  .tl-item:before{content:'';position:absolute;left:-19px;top:13px;width:7px;height:7px;
    border-radius:50%;background:#c9cdd4}
  .tl-item.key:before{background:var(--red);box-shadow:0 0 0 3px var(--red-soft)}
  .tl-t{font-size:12px;color:var(--muted);display:flex;gap:10px;align-items:baseline}
  .tl-code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11.5px;color:var(--text);
    background:#eef0f3;border-radius:4px;padding:1px 6px}
  .tl-d{font-size:12.5px;margin-top:2px}
  .tl-v{font-size:11.5px;color:var(--muted);margin-top:1px}
  .empty{padding:36px;text-align:center;color:var(--muted);font-size:13px}
  footer{margin-top:20px;color:var(--muted);font-size:11.5px;text-align:center}
  @media(max-width:880px){
    .mxt th:nth-child(2),.mxt td:nth-child(2){display:none}
    thead th:nth-child(4),tbody td:nth-child(4),
    thead th:nth-child(8),tbody td:nth-child(8){display:none}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>SHIPCCUS001 取消节点清单</h1>
    <div class="sub" id="sub"></div>
  </header>

  <div class="note">
    <b>SHIPCCUS001</b> 是 LMS 上报的「面单过期 / 订单取消」节点，后台文案有三种：
    <code>shipment.expired</code>、<code>shipment.cancelled</code>、
    <code>Label expired, never dropped off</code>。<br/>
    <b>怎么判断</b>：这个节点被打上之后，如果<b>再没有任何时间更晚的新节点</b>，说明包裹取消后就没再动过
    → 标为 <b class="k-no">取消未恢复</b>，需要人跟进；<br/>
    如果取消之后还有<b>一条时间更晚的节点</b>，说明包裹还在走、或已重新建单
    → 标为 <b class="k-yes">取消已恢复</b>，不算问题单，另开一个标签页留档可查。<br/>
    只看<b>时间先后</b>，不看有没有见过 A-SCAN：取消发生在 A-SCAN 之后、之后又零动作的单，仍然是「取消未恢复」。<br/>
    「取消已恢复」的单<b>并未丢弃</b>，在对应标签页里能看到取消后首个新节点、间隔多久、后续链路，
    也对应 <code>data/stuck_recovered.csv</code>。<br/>
    <b>数怎么读</b>：带取消节点 <b id="n-all">0</b> 单 = 测试 <b id="n-test">0</b>
    + 生产 <b id="n-prod">0</b>；每一行再按「取消之后<b>有没有新节点</b>」拆成
    <b>已更新</b>（有新节点）/ <b>没更新</b>（再也没动过）。
    合计已更新 <b id="n-rec" class="k-yes">0</b> 单、没更新 <b id="n-stuck">0</b> 单。
    <b class="k-no">右下角那格「生产 · 没更新」</b>才是真正要跟进的单。
    <span style="color:#9aa2ac">「分组」列的 A/B 只作归因（取消前是否已交寄），不影响上面的分法。</span>
  </div>

  <div class="funnel" id="funnel"></div>

  <div class="mx" id="mx"></div>

  <div class="card">
    <div class="toolbar">
      <div class="tabs" id="tabs">
        <button data-g="ALL" class="on">全部</button>
        <button data-g="TEST">测试单</button>
        <button data-g="REAL">生产单</button>
        <button data-g="TODO">要跟进</button>
      </div>
      <input type="text" id="q" placeholder="搜索运单号 / 末公里单号 / 收件人 / 州"/>
      <label class="chk"><input type="checkbox" id="exTest"/> 排除测试单</label>
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
const META = PAYLOAD.meta;
const STUCK = (PAYLOAD.rows || []).map(r => Object.assign({kind:'stuck'}, r));
const REC   = (PAYLOAD.recovered || []).map(r => Object.assign({kind:'recovered'}, r));
const ROWS  = STUCK.concat(REC);

const state = { group:'ALL', q:'', exTest:false, sort:'', desc:true, open:new Set() };

// 唯一筛选口径：测试/生产 × 已更新/没更新
const isTest = r => !!r.isTest;
const isUpd  = r => r.kind === 'recovered';   // 取消后有更晚节点 = 已更新
const F = {
  ALL:      () => true,
  TEST:     r => isTest(r),
  REAL:     r => !isTest(r),
  TODO:     r => !isTest(r) && !isUpd(r),     // 生产 · 没更新 ← 唯一要跟进的
  T_UPD:    r => isTest(r)  && isUpd(r),
  T_NO:     r => isTest(r)  && !isUpd(r),
  P_UPD:    r => !isTest(r) && isUpd(r),
  P_NO:     r => !isTest(r) && !isUpd(r),
};

const nf = n => (n === null || n === undefined || n === '') ? '—' : (Math.abs(n) >= 100 ? n.toFixed(0) : n.toFixed(1));
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

const COLS = {
  ALL: [
    {k:'waybill',   t:'运单号'},
    {k:'kindLabel', t:'类型'},
    {k:'status',    t:'当前状态'},
    {k:'cancelAt',  t:'取消时间'},
    {k:'lastNode',  t:'取消后首个新节点'},
    {k:'deltaH',    t:'间隔 / 距今'},
    {k:'destState', t:'州'},
    {k:'consignee', t:'收件人'},
  ],
  AB: [
    {k:'waybill',         t:'运单号'},
    {k:'status',          t:'当前状态'},
    {k:'eventDescCn',     t:'取消原因'},
    {k:'cancelAt',        t:'取消时间'},
    {k:'orderCreateTime', t:'建单时间'},
    {k:'hCreateToExpire', t:'建单→取消'},
    {k:'hSinceExpire',    t:'距今'},
    {k:'destState',       t:'州'},
    {k:'consignee',       t:'收件人'},
    {k:'groupLabel',      t:'分组'},
  ],
  REC: [
    {k:'waybill',         t:'运单号'},
    {k:'cancelAt',        t:'取消时间'},
    {k:'resumeAt',        t:'恢复节点时间'},
    {k:'resumeCode',      t:'恢复首节点'},
    {k:'hCancelToResume', t:'取消→恢复'},
    {k:'laterCount',      t:'后续节点数'},
    {k:'laterCodes',      t:'后续链路'},
    {k:'destState',       t:'州'},
    {k:'consignee',       t:'收件人'},
  ],
};

function viewCols(){
  const g = state.group;
  if (g === 'T_UPD' || g === 'P_UPD') return COLS.REC;
  if (g === 'TEST'  || g === 'T_NO')   return COLS.AB.filter(c => c.k !== 'groupLabel');
  if (g === 'REAL'  || g === 'TODO' || g === 'P_NO') return COLS.AB;
  return COLS.ALL;
}

function renderMatrix(){
  const n = g => ROWS.filter(F[g]).length;
  const T = { upd: n('T_UPD'), no: n('T_NO') };
  const P = { upd: n('P_UPD'), no: n('P_NO') };
  const tot = T.upd + T.no + P.upd + P.no;
  const cls = g => 'cell' + (state.group === g ? ' on' : '');
  const num = v => `<span class="num${v === 0 ? ' zero' : ''}">${v}</span>`;
  document.getElementById('mx').innerHTML =
    `<div class="mx-h"><b>取消节点结构</b>
       <span>US · ${(META.dateFrom||'').slice(0,10)} ~ ${(META.dateTo||'').slice(0,10)}
       · 点任意数字可筛选</span></div>
     <table class="mxt">
       <thead><tr>
         <th class="rw">总计 <b style="color:var(--text);font-size:16px">${tot}</b></th>
         <th>已更新<span class="sub">取消后又有新节点</span></th>
         <th>没更新<span class="sub">取消后再没动过</span></th>
         <th>小计</th>
       </tr></thead>
       <tbody>
         <tr>
           <td class="rw">测试单</td>
           <td class="${cls('T_UPD')}" data-g="T_UPD">${num(T.upd)}</td>
           <td class="${cls('T_NO')}" data-g="T_NO">${num(T.no)}<span class="sub">测试数据，不用管</span></td>
           <td class="${cls('TEST')}" data-g="TEST">${num(T.upd + T.no)}</td>
         </tr>
         <tr>
           <td class="rw">生产单</td>
           <td class="${cls('P_UPD')}" data-g="P_UPD">${num(P.upd)}</td>
           <td class="${cls('TODO')} hot" data-g="TODO">${num(P.no)}<span class="flag">要跟进</span></td>
           <td class="${cls('REAL')}" data-g="REAL">${num(P.upd + P.no)}</td>
         </tr>
         <tr class="sum">
           <td class="rw">合计</td>
           <td>${num(T.upd + P.upd)}</td>
           <td class="hot">${num(T.no + P.no)}</td>
           <td>${num(tot)}</td>
         </tr>
       </tbody>
     </table>`;
  const setN = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
  setN('n-all', tot); setN('n-test', T.upd + T.no); setN('n-prod', P.upd + P.no);
  setN('n-todo', P.no); setN('n-rec', T.upd + P.upd); setN('n-stuck', T.no + P.no);
  document.getElementById('sub').textContent =
    `US · ${(META.dateFrom||'').slice(0,10)} ~ ${(META.dateTo||'').slice(0,10)}`
    + ` · 生成于 ${META.generatedAt} · 数据源 ${META.region} 区订单接口`;
}

function renderFunnel(){
  const st = META.stats || {};
  const base = st.orders || STUCK.length + REC.length;
  const code = st.withCode || (STUCK.length + REC.length);
  const test = STUCK.filter(r=>r.isTest).length;
  const live = STUCK.filter(r=>!r.isTest).length;
  const back = REC.length;
  const w = n => Math.max(3, (n / base * 100)).toFixed(1) + '%';
  const pct = n => (n / base * 100).toFixed(1) + '%';
  document.getElementById('funnel').innerHTML =
    `<div class="fn-h"><b>整体漏斗</b><span>US · ${(META.dateFrom||'').slice(0,10)} ~ ${(META.dateTo||'').slice(0,10)} · 分母为后台订单数</span></div>
     <div class="fn-row"><span class="fn-label">后台订单</span>
       <div class="fn-bar fn-1" style="width:${w(base)}">${base} 单</div>
       <span class="fn-note">含测试单 ${test} 单</span></div>
     <div class="fn-row"><span class="fn-label">有取消节点 SHIPCCUS001</span>
       <div class="fn-bar fn-2" style="width:${w(code)}">${code} 单<small>${pct(code)}</small></div>
       <span class="fn-note">LMS 推过来</span></div>
     <div class="fn-row"><span class="fn-label">生产单 · 没更新</span>
       <div class="fn-bar fn-3" style="width:${w(live)}">${live} 单<small>${pct(live)}</small></div>
       <span class="fn-note">${code} 单里 −${test} 单测试 −${back} 单已更新 = ${live} 单要跟进</span></div>`;
}

function renderHead(){
  const cols = viewCols();
  document.getElementById('th').innerHTML = '<tr>' + cols.map(c =>
    `<th data-s="${c.k}">${c.t}${state.sort === c.k ? (state.desc ? ' ↓' : ' ↑') : ''}</th>`
  ).join('') + '</tr>';
}

function current(){
  let rows = ROWS.filter(F[state.group] || F.ALL);
  if (state.exTest) rows = rows.filter(r => !r.isTest);
  if (state.q){
    const q = state.q.toLowerCase();
    rows = rows.filter(r => [r.waybill, r.lastMileWaybill, r.consignee, r.destState, r.referenceNumber, r.laterCodes]
      .some(v => (v||'').toLowerCase().includes(q)));
  }
  const k = state.sort;
  if (k){
    const d = state.desc ? -1 : 1;
    rows.sort((x,y)=>{
      const a = x[k], b = y[k];
      if (a === null || a === undefined || a === '') return 1;
      if (b === null || b === undefined || b === '') return -1;
      if (typeof a === 'number' && typeof b === 'number') return (a-b)*d;
      return String(a).localeCompare(String(b))*d;
    });
  } else {
    // 默认：取消未恢复按取消时间倒序，取消已恢复排在后面
    rows.sort((x,y)=>{
      if (x.kind !== y.kind) return x.kind === 'recovered' ? 1 : -1;
      return String(y.cancelAt||y.expiredTime||'').localeCompare(String(x.cancelAt||x.expiredTime||''));
    });
  }
  return rows;
}

function timeline(r){
  if (!r.events || !r.events.length) return '';
  return '<div class="tl">' + r.events.map(e => {
    const key = (e.code||'').toUpperCase() === 'SHIPCCUS001';
    return `<div class="tl-item${key?' key':''}">
      <div class="tl-t"><span class="tl-code">${esc(e.code)}</span>${esc(e.time)}</div>
      <div class="tl-d">${esc(e.desc||'')}</div>
      ${e.vendor?`<div class="tl-v">vendor ${esc(e.vendor)}</div>`:''}
    </div>`;
  }).join('') + '</div>';
}

function tags(r){
  const test = r.isTest ? ' <span class="tag t">测试</span>' : '';
  const dup = (r.cancelCount > 1) ? ` <span class="tag t">取消×${r.cancelCount}</span>` : '';
  return test + dup;
}

function cellText(k, r){
  switch(k){
    case 'kindLabel':
      return r.kind === 'recovered'
        ? '<span class="tag g">取消已恢复</span>'
        : '<span class="tag a">取消未恢复</span>';
    case 'groupLabel':
      return r.group === 'A'
        ? '<span class="tag a">A 从未交寄</span>'
        : '<span class="tag b">B 交寄后撤销</span>';
    case 'status':
      return r.kind === 'recovered' ? '已恢复流转' : (esc(r.status) || '—');
    case 'cancelAt':
      return esc(r.cancelAt || r.expiredTime || '') || '—';
    case 'lastNode':
      return r.kind === 'recovered'
        ? `<span class="tl-code">${esc(r.resumeCode)}</span> ${esc(r.resumeAt)}`
        : '—';
    case 'deltaH': {
      const v = r.kind === 'recovered' ? r.hCancelToResume : r.hSinceExpire;
      return nf(v) + ' h';
    }
    case 'hCreateToExpire': return nf(r.hCreateToExpire) + ' h';
    case 'hSinceExpire':    return nf(r.hSinceExpire) + ' h';
    case 'hCancelToResume': return nf(r.hCancelToResume) + ' h';
    case 'laterCodes':      return r.laterCodes ? `<span class="chain">${esc(r.laterCodes)}</span>` : '—';
    case 'resumeCode':      return r.resumeCode ? `<span class="tl-code">${esc(r.resumeCode)}</span>` : '—';
    case 'consignee':       return (esc(r.consignee) || '—') + tags(r);
    case 'waybill':         return `<span class="mono">${esc(r.waybill)}</span>`;
    default:                return esc(r[k]) || '—';
  }
}

function detail(r){
  const n = viewCols().length;
  if (r.kind === 'recovered'){
    return `<tr class="det"><td colspan="${n}">
      <div style="font-size:12px;color:#8a919b;margin:8px 0 6px">
        运单 ${esc(r.waybill)} · 取消于 ${esc(r.cancelAt)} → 首个新节点
        ${esc(r.resumeCode)} @ ${esc(r.resumeAt)}，间隔 ${nf(r.hCancelToResume)} h，
        之后共 ${r.laterCount} 条节点。判定：取消之后仍有更晚节点 → <b>取消已恢复，不算问题单</b>。
        ${r.isTest ? '（测试单）' : ''}
      </div>
      <div class="chain" style="max-width:none">${esc(r.laterCodes || '')}</div>
    </td></tr>`;
  }
  return `<tr class="det"><td colspan="${n}">
    <div style="font-size:12px;color:#8a919b;margin:8px 0 0">
      运单 ${esc(r.waybill)} · ${esc(r.status)} · 共 ${r.eventCount} 条事件
      ${r.hExpireVsWork !== null && r.hExpireVsWork !== undefined
        ? ` · 取消时间相对最后作业 ${r.hExpireVsWork > 0 ? '晚' : '早'} ${Math.abs(r.hExpireVsWork).toFixed(1)} h` : ''}
      ${r.cancelCount > 1 ? ` · 该单重复上报取消 ${r.cancelCount} 次` : ''}
    </div>${timeline(r)}</td></tr>`;
}

function renderTable(){
  const rows = current(), cols = viewCols();
  document.getElementById('count').textContent = `${rows.length} 单`;
  document.getElementById('empty').style.display = rows.length ? 'none' : 'block';
  document.getElementById('tb').innerHTML = rows.map(r => {
    const open = state.open.has(r.waybill);
    return `<tr class="row${open?' open':''}" data-wb="${esc(r.waybill)}">`
      + cols.map(c => `<td>${cellText(c.k, r)}</td>`).join('')
      + '</tr>' + (open ? detail(r) : '');
  }).join('');
  renderHead();
}

function setGroup(g){
  state.group = g;
  [...document.querySelectorAll('#tabs button')].forEach(x => x.classList.toggle('on', x.dataset.g === g));
  [...document.querySelectorAll('#mx td.cell')].forEach(x => x.classList.toggle('on', x.dataset.g === g));
  state.sort = ''; state.desc = true;
  renderTable();
}

document.getElementById('tabs').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  setGroup(b.dataset.g);
});
document.getElementById('mx').addEventListener('click', e => {
  const c = e.target.closest('td.cell'); if (!c) return;
  setGroup(state.group === c.dataset.g ? 'ALL' : c.dataset.g);
});
document.getElementById('q').addEventListener('input', e => { state.q = e.target.value.trim(); renderTable(); });
document.getElementById('exTest').addEventListener('change', e => { state.exTest = e.target.checked; renderTable(); });
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
const cnt = g => ROWS.filter(F[g]).length;
document.getElementById('foot').textContent =
  `带取消节点 ${cnt('ALL')} 单 = 测试 ${cnt('TEST')}（已更新 ${cnt('T_UPD')} / 没更新 ${cnt('T_NO')}）`
  + ` + 生产 ${cnt('REAL')}（已更新 ${cnt('P_UPD')} / 没更新 ${cnt('P_NO')}）；`
  + `要跟进 ${cnt('P_NO')} 单。订单基数 ${META.stats ? META.stats.orders : '-'} 单。时间口径北京时间 UTC+8。`;

renderMatrix(); renderFunnel(); renderTable();
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="us")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--from", dest="date_from", default=None,
                    help="起始日期，如 2026/10/29；给了就不看 --days")
    ap.add_argument("--to", dest="date_to", default=None,
                    help="结束日期，默认今天 23:59:59")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--render-only", action="store_true",
                    help="只用已有 data/stuck_shipcc.json 重新渲染页面，不重新抓取")
    args = ap.parse_args()

    if args.render_only:
        payload = json.loads((DATA / "stuck_shipcc.json").read_text(encoding="utf-8"))
        write_csv(payload["rows"], DATA / "stuck_shipcc.csv")
        render_html(payload)
        return

    # _range 需要 from/to 成对出现，只给 from 时补今天
    d_to_arg = args.date_to
    if args.date_from and not d_to_arg:
        d_to_arg = datetime.now(sc.TZ_CN).strftime("%Y/%m/%d")

    rows, recovered, stats, d_from, d_to = collect(
        args.region, args.days, args.date_from, d_to_arg)
    grp = {"A": 0, "B": 0}
    for r in rows:
        grp[r["group"]] += 1
    rows.sort(key=lambda r: r["expiredTime"], reverse=True)
    recovered.sort(key=lambda r: r["cancelAt"], reverse=True)

    if args.no_write:
        print(json.dumps({"stats": stats, "stuck": rows[:2], "recovered": recovered[:2]},
                         ensure_ascii=False, indent=1)[:2000])
        return

    DATA.mkdir(exist_ok=True)
    payload = {
        "meta": {
            "code": CODE, "region": args.region.upper(), "days": args.days,
            "dateFrom": d_from, "dateTo": d_to,
            "count": len(rows), "groupA": grp["A"], "groupB": grp["B"],
            "testCount": sum(1 for r in rows if r["isTest"]),
            "stats": stats,
            "generatedAt": datetime.now(sc.TZ_CN).strftime("%Y-%m-%d %H:%M"),
        },
        "rows": rows,
        "recovered": recovered,
    }
    (DATA / "stuck_shipcc.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[json] -> {DATA / 'stuck_shipcc.json'}")
    render_html(payload)          # 先出页面，避免 CSV 被占用时整条任务中断
    write_csv(rows, DATA / "stuck_shipcc.csv")
    (DATA / "stuck_recovered.json").write_text(
        json.dumps({"meta": payload["meta"], "rows": recovered}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    print(f"[json] -> {DATA / 'stuck_recovered.json'}")
    write_recovered(recovered, DATA / "stuck_recovered.csv")


if __name__ == "__main__":
    main()
