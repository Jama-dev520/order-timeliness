#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
读取 data/orders.json，生成自包含的 HTML 看板（无外部依赖，双击即可打开）。
用法:  python dashboard.py
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "orders.json"
OUT_FILE = BASE_DIR / "dashboard.html"
# render(secure=True) 时把待加密的数据包放这里，由 build_site.py 取走加密成 data.bin
SECURE_PAYLOAD = None


def _env(key: str, default: str = "") -> str:
    """读 .env（环境变量优先）。只用于两个日期口径，不引入 dotenv 依赖。"""
    v = (os.getenv(key) or "").strip()
    if v:
        return v
    path = BASE_DIR / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, val = line.partition("=")
            if k.strip() == key:
                return val.strip().strip('"').strip("'")
    return default
# 投递取消异常清单（dropoff_cancel.py 的产出）：已投递却被取消的订单
DROPOFF_FILE = BASE_DIR / "data" / "dropoff_cancel.json"

# 出库（warehouse out）节点登记，与 scraper.OUT_NODES_US 保持一致。
# 两家 vendor 共用 jvEventCode=DFDLUS002，靠 vendorEventCode 分流：
#   PUDO INC → PUP；LMS → shipment.injected/made_ready
# 写在这里是为了旧快照（meta 里没有 outNodes）也能正常渲染。
OUT_NODES_FALLBACK = {
    "US": [
        {"vendor": "PUDO INC", "vendorCode": "PUP", "vendorEventCode": "PUP",
         "desc": "Driver pickup from PUDOpoint",
         "note": "接口 vendorCode 常返回短码 PU（vendorName=PUDO），PU / PUP 均归入 PUDO INC"},
        {"vendor": "LMS", "vendorCode": "LMS",
         "vendorEventCode": "shipment.injected/made_ready",
         "desc": "Package ready for dispatch",
         "note": "接口 vendorCode 常返回短码 LM；vendorEventCode 见登记表"},
    ],
}

# 替代码兜底：11/01 起 LMS 实际用 shipment.sorted 上报同一个 DFDLUS002
OUT_NODE_ALIASES_FALLBACK = {
    "shipment.sorted": "LMS",
}

# 妥投码兜底：US=DELUS006 / UK=DEL005，匹配时两个都认
DELIVERED_CODES_FALLBACK = ["DELUS006", "DEL005"]
DELIVERED_PRIMARY_FALLBACK = {"UK": "DEL005", "US": "DELUS006"}

# 投递方（drop off）登记兜底：客户把包裹交给谁，与「谁出的库」分开
DROP_NODES_FALLBACK = {
    "US": [
        {"vendor": "PUDO INC", "vendorCode": "PU",
         "desc": "Customer drop-off at PUDOpoint", "note": "客户把包裹投在 PUDO 网点"},
        {"vendor": "LMS", "vendorCode": "LM",
         "desc": "At store, awaiting LMS pickup", "note": "客户把包裹交到 LMS 门店 / 站点"},
    ],
}


def _meta_with_defaults(meta: dict) -> dict:
    meta = dict(meta or {})
    if not meta.get("outNodes"):
        ev = meta.get("eventCodes") or {}
        meta["outNodes"] = {
            k: [dict(n, code=(ev.get(k) or {}).get("rdc") or meta.get("rdcEventCode") or "DFDLUS002")
                for n in v]
            for k, v in OUT_NODES_FALLBACK.items()
        }
    # 旧快照里的 outNodes 没有 note / vendorEventCode，按 vendor 从兜底表补齐
    by_vendor = {n["vendor"]: n for v in OUT_NODES_FALLBACK.values() for n in v}
    meta["outNodes"] = {
        k: [dict(n,
                 note=n.get("note") or (by_vendor.get(n.get("vendor")) or {}).get("note", ""),
                 vendorEventCode=n.get("vendorEventCode")
                                 or (by_vendor.get(n.get("vendor")) or {}).get("vendorEventCode", ""))
            for n in v]
        for k, v in (meta.get("outNodes") or {}).items()
    }
    if not meta.get("outNodeAliases"):
        meta["outNodeAliases"] = dict(OUT_NODE_ALIASES_FALLBACK)
    # 妥投码：US=DELUS006 / UK=DEL005，旧快照没有这层，按区域补齐
    if not meta.get("deliveredEventCodes"):
        meta["deliveredEventCodes"] = list(DELIVERED_CODES_FALLBACK)
    # 投递方登记：旧快照没有这层，按区域补齐（否则筛选器里 PUDO INC / LMS 会消失）
    if not meta.get("dropNodes"):
        meta["dropNodes"] = {k: [dict(n) for n in v] for k, v in DROP_NODES_FALLBACK.items()}
    if not meta.get("dropEventCodes"):
        meta["dropEventCodes"] = ["DOUS002", "DOUD002"]
    ev = meta.get("eventCodes") or {}
    if ev:
        meta["eventCodes"] = {
            k: dict(v, delivered=v.get("delivered") or DELIVERED_PRIMARY_FALLBACK.get(k) or DELIVERED_CODES_FALLBACK[0])
            for k, v in ev.items()
        }
    return meta

TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>订单时效看板 · UK / All / US</title>
<style>
  :root{
    --bg:#f4f5f7; --card:#ffffff; --text:#1f2329; --muted:#8a919b;
    --border:#e6e8ec; --accent:#2f6bff;
    --ok:#12a150; --warn:#d97706; --warn-soft:#fff4e5; --pend:#8a919b; --pend-soft:#f0f1f3;
    --red:#d62c2c;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
    font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}
  .wrap{max-width:1260px;margin:0 auto;padding:24px 20px 48px}
  header{display:flex;align-items:flex-end;justify-content:space-between;
    flex-wrap:wrap;gap:12px;margin-bottom:20px}
  h1{font-size:21px;margin:0;font-weight:600;letter-spacing:.2px}
  .sub{color:var(--muted);font-size:12.5px;margin-top:6px}
  .badge{display:inline-block;padding:2px 8px;border-radius:5px;font-size:11.5px;
    background:var(--warn-soft);color:var(--warn);border:1px solid #f5dcb8;
    margin-left:8px;vertical-align:middle;font-weight:500}
  /* 6 张卡排 3 列 2 行：第一行计数（订单 / 面单 / 妥投），第二行时效与比率 */
  .kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:18px}
  .kpi{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px 18px}
  .kpi .label{color:var(--muted);font-size:12.5px;margin-bottom:8px;display:flex;align-items:center;gap:6px}
  .kpi .kpi-sub{font-size:11px;padding:1px 6px;border-radius:4px;background:#eef2f7;color:#5a6472;font-weight:400}
  /* 事件码当小标签用：比中文副标签更宽，压字号 + 不换行，别把卡片撑破 */
  .kpi .kpi-sub.mono{font-size:10px;padding:1px 5px;letter-spacing:-.1px;white-space:nowrap;background:#f1eefe;color:#5b4bb8}
  .kpi .val{font-size:26px;font-weight:600;letter-spacing:.3px}
  .kpi .val small{font-size:13px;font-weight:500;color:var(--muted);margin-left:3px}
  .kpi .hint{font-size:11.5px;color:var(--muted);margin-top:6px}
  /* 异常预警条：点卡片 → 下面的明细表筛出这些单，再点一次取消 */
  .alerts{display:grid;grid-template-columns:repeat(auto-fit,minmax(178px,1fr));gap:10px;margin-bottom:14px}
  .alert{background:var(--card);border:1px solid var(--border);border-left:3px solid #d9dee5;
    border-radius:9px;padding:11px 14px;cursor:pointer;transition:border-color .15s,box-shadow .15s,background .15s}
  .alert:hover{border-color:var(--accent);box-shadow:0 2px 10px rgba(47,107,255,.13)}
  .alert.active{border-color:var(--accent);background:#f4f7ff;box-shadow:inset 0 0 0 2px rgba(47,107,255,.18)}
  .alert .n{font-size:22px;font-weight:600;line-height:1.15}
  .alert .n small{font-size:12px;font-weight:500;color:var(--muted);margin-left:3px}
  .alert .t{font-size:12.5px;margin-top:3px;font-weight:500;display:flex;align-items:center;gap:5px}
  .alert .h{font-size:11px;color:var(--muted);margin-top:3px;line-height:1.35}
  .alert.sev-high{border-left-color:#d64545}
  .alert.sev-mid{border-left-color:#e0a33a}
  .alert.sev-low{border-left-color:#8b96a5}
  .alert.is-zero{opacity:.45}
  .alert.is-zero .n{color:var(--muted)}
  .alertbar{display:none;align-items:center;gap:9px;margin:0 0 12px;padding:8px 12px;
    background:#f4f7ff;border:1px solid #d8e3ff;border-radius:8px;font-size:12.5px}
  .alertbar b{font-weight:600}
  .alertbar .x{margin-left:auto;color:var(--accent);cursor:pointer;font-size:12.5px}
  .alertbar .x:hover{text-decoration:underline}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%}
  .grid2{display:grid;grid-template-columns:1.35fr 1fr;gap:14px;margin-bottom:18px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:18px}
  .card h2{font-size:14px;margin:0 0 4px;font-weight:600}
  .card .cap{font-size:12px;color:var(--muted);margin-bottom:14px}
  /* 图表下方的口径备注：说清「图里少算了哪些单」 */
  .binnote{font-size:11.5px;color:var(--muted);line-height:1.7;margin-top:10px}
  .binnote b{color:var(--text);font-weight:600}
  /* 卡片内的折叠说明：默认收起，不占版面 */
  .exp{margin-top:12px;border-top:1px dashed var(--border);padding-top:10px}
  .exp summary{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--accent);
    cursor:pointer;outline:none;list-style:none;user-select:none}
  .exp summary::-webkit-details-marker{display:none}
  .exp summary::before{content:'?';flex:none;width:15px;height:15px;border-radius:50%;
    background:#e8efff;color:var(--accent);font-size:11px;font-weight:700;
    display:inline-flex;align-items:center;justify-content:center}
  .exp summary::after{content:'展开';margin-left:auto;font-size:11px;color:var(--muted)}
  .exp[open] summary::after{content:'收起'}
  .exp summary:hover{text-decoration:underline}
  .exp ul{margin:9px 0 0;padding-left:18px;font-size:12px;color:#4a5261;line-height:1.85}
  .exp li{margin-bottom:5px}
  .exp li:last-child{margin-bottom:0}
  .exp b{color:var(--text);font-weight:600}
  .exp code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px;
    background:#f2f4f7;border:1px solid #e3e7ee;border-radius:4px;padding:0 2px;
    margin:0 1px;white-space:nowrap}
  .legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin-top:10px}
  .legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px;vertical-align:middle}
  .toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
  .toolbar input,.toolbar select{padding:7px 10px;border:1px solid var(--border);
    border-radius:7px;font-size:13px;color:var(--text);background:#fff;outline:none}
  .toolbar input:focus,.toolbar select:focus{border-color:var(--accent)}
  .toolbar input{width:250px}
  .toolbar input[type=date]{width:auto;min-width:140px}
  .toolbar .dsep{color:var(--muted);font-size:12px;margin:0 -4px}
  .toolbar .clear{color:var(--accent);font-size:12.5px;cursor:pointer;white-space:nowrap}
  .toolbar .clear:hover{text-decoration:underline}
  .toolbar .chk{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;
    color:var(--text);cursor:pointer;user-select:none;white-space:nowrap}
  .toolbar .chk input{width:auto;margin:0;cursor:pointer}
  .toolbar .count{color:var(--muted);font-size:12.5px;margin-left:auto}
  table{width:100%;border-collapse:collapse;font-size:13px}
  thead th{position:sticky;top:0;background:#fafbfc;text-align:left;color:var(--muted);
    font-weight:500;font-size:12px;padding:10px 12px;border-bottom:1px solid var(--border);
    white-space:nowrap;cursor:pointer;user-select:none}
  thead th:hover{color:var(--text)}
  thead th .arrow{opacity:.4;font-size:10px;margin-left:3px}
  tbody td{padding:10px 12px;border-bottom:1px solid #f1f2f4;white-space:nowrap;font-variant-numeric:tabular-nums}
  tbody tr:hover{background:#fafbff}
  .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px}
  .muted{color:var(--muted)}
  .tag{display:inline-block;padding:1.5px 8px;border-radius:20px;font-size:11.5px;font-weight:500}
  .t-ok{background:var(--ok);color:#fff}
  .t-done{background:#0b7a3b;color:#fff}
  .t-warn{background:var(--warn-soft);color:var(--warn)}
  .t-pend{background:var(--pend-soft);color:var(--pend)}
  .vtag{display:inline-flex;align-items:center;gap:6px;font-size:12.5px}
  .vcode{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:10.5px;font-weight:600;
    color:#6b7280;background:#f1f3f6;border:1px solid #e3e6ea;border-radius:4px;
    padding:0 4px;line-height:1.5;letter-spacing:.02em}
  .vcode.pup{color:#0b7a5a;background:#eaf7f1;border-color:#c9ebdc}
  .vcode.lms{color:#3b5bdb;background:#eef2ff;border-color:#d9e1fb}
  .vcode.alias{color:#9a6700;background:#fff8e1;border-color:#f0e0a8}
  /* 串场单标记：PUDO 点投递后被 LMS 司机取走 */
  .xflag{margin-left:5px;font-size:10.5px;font-weight:600;color:#b4232a;
    background:#fdecec;border:1px solid #f5c7c7;border-radius:4px;padding:0 4px;
    line-height:1.5;white-space:nowrap;cursor:help}
  /* LMS 同供：建单与投递都是 LMS，用蓝色系与「异常」类标记区分开，不带告警含义 */
  .xflag.lms{color:#0b5f8a;background:#eaf4fb;border-color:#c3dff0;margin-left:7px}
  /* FIRST_SCAN（收件扫描）标记：中性灰蓝，只是「有这个事件」的确认，不代表异常 */
  .xflag.fs{color:#3d5a80;background:#f2f5f9;border-color:#d3dde8;margin-left:6px;font-weight:500}
  /* 订单来源：eBay / Depop，纯标签，不带告警含义 */
  .src{display:inline-block;font-size:11px;font-weight:600;border-radius:5px;padding:1px 6px;
       border:1px solid transparent;white-space:nowrap;cursor:help}
  .src.ebay{color:#8a4b00;background:#fff3e0;border-color:#f3d9b0}
  .src.depop{color:#6b21a8;background:#f5ecff;border-color:#e2cff8}
  /* Depop 官方轨迹外链：表格单号后的 ↗，以及抽屉头部的按钮 */
  .xlink{margin-left:6px;font-size:11px;font-weight:700;color:#6b21a8;text-decoration:none;
    background:#f5ecff;border:1px solid #e2cff8;border-radius:4px;padding:0 4px;
    line-height:1.5;white-space:nowrap;cursor:pointer}
  .xlink:hover{background:#ede0ff;border-color:#d3b8f5;color:#5b1a91}
  .trace-hd .tlink{margin-top:9px}
  .trace-hd .tlink:empty{display:none}
  a.depop-go{display:inline-block;padding:5px 10px;border-radius:6px;text-decoration:none;
    background:#f5ecff;border:1px solid #e2cff8;color:#6b21a8;font-size:12px;font-weight:600}
  a.depop-go:hover{background:#ede0ff;border-color:#d3b8f5}
  a.depop-go i{font-style:normal;font-weight:400;color:#7c5aa6;margin-left:4px}
  .src.other{color:#5a6472;background:#f2f4f7;border-color:#dfe3e9}
  /* 右上角区域 tab：对齐后台 UK | All | US 的橙色开关 */
  .tabs{display:inline-flex;border:1px solid #ff6a13;border-radius:6px;overflow:hidden;background:#fff}
  .tabs button{border:0;background:transparent;padding:4px 15px;font-size:12.5px;font-weight:500;
    color:#ff6a13;cursor:pointer;font-family:inherit;line-height:1.5;transition:background .15s}
  .tabs button+button{border-left:1px solid #ffc4a3}
  .tabs button:hover{background:#fff3ec}
  .tabs button.on{background:#ff6a13;color:#fff}
  .tabs button.on:hover{background:#ff6a13}
  /* 渠道页签（全部 / eBay / Depop）：页面级开关，切换后 KPI、图表、预警、明细一起重算 */
  .chans{display:flex;align-items:center;gap:10px;margin:0 0 14px;flex-wrap:wrap}
  .chans .lab{font-size:12.5px;color:var(--muted)}
  .chans .tabs{border-color:#c9d3e6}
  .chans .tabs button{color:#4a5568;padding:5px 16px}
  .chans .tabs button+button{border-left:1px solid #dde4ef}
  .chans .tabs button:hover{background:#f4f7ff}
  .chans .tabs button.on{background:var(--accent);color:#fff}
  .chans .tabs button.on:hover{background:var(--accent)}
  .chans .tabs button .nc{font-size:11px;opacity:.72;margin-left:5px;font-variant-numeric:tabular-nums}
  .chans .tabs button.on .nc{opacity:.9}
  .chans .scope{font-size:12px;color:var(--muted)}
  .chans .scope b{color:var(--text);font-weight:600}
  .notice{margin:-6px 0 16px;padding:9px 13px;border-radius:8px;background:#fff8ed;
    border:1px solid #f5dcb8;color:#96631a;font-size:12.5px;line-height:1.6}
  .dur-bad{color:var(--red);font-weight:600}
  .tbl-scroll{max-height:560px;overflow:auto;border:1px solid var(--border);border-radius:9px}
  .empty{padding:40px;text-align:center;color:var(--muted)}
  .note{margin-top:14px;padding:10px 12px;border-radius:8px;background:#f7f9ff;
    border:1px solid #dfe7fb;color:#40506e;font-size:12.5px;line-height:1.7}
  footer{margin-top:22px;color:var(--muted);font-size:11.5px;text-align:center}
  /* drop off 量级右侧汇总面板 */
  .dsum .r{display:flex;justify-content:space-between;align-items:baseline;gap:10px;
    padding:9px 2px;border-bottom:1px dashed #eef0f3;font-size:13px}
  .dsum .r:last-child{border-bottom:0}
  .dsum .r span{color:var(--muted);font-size:12.5px;display:inline-flex;align-items:center;gap:6px}
  .dsum .r span i{width:8px;height:8px;border-radius:2px;display:inline-block}
  .dsum .r b{font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap}
  .dsum .r b em{font-style:normal;font-weight:400;color:var(--muted);font-size:11.5px;margin-left:5px}
  .dsum .note2{margin-top:10px;padding-top:10px;border-top:1px solid var(--border);
    color:var(--muted);font-size:11.5px;line-height:1.65}
  @media(max-width:900px){.kpis{grid-template-columns:repeat(2,1fr)}.grid2{grid-template-columns:1fr}}
  /* ---- 每日 dropoff 点位（location）---- */
  .sitetool{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:0 0 12px}
  .sitetool label{font-size:12.5px;color:var(--muted)}
  .sitetool select{padding:6px 10px;border:1px solid var(--border);border-radius:7px;
    font-size:13px;color:var(--text);background:#fff;outline:none;font-family:inherit}
  .sitetool select:focus{border-color:var(--accent)}
  .sitetool .count{color:var(--muted);font-size:12.5px;margin-left:auto}
  .sitesum{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}
  .sitesum .box{border:1px solid var(--border);border-radius:8px;padding:9px 13px;min-width:118px;
    background:#fcfcfd}
  .sitesum .box .k{font-size:11.5px;color:var(--muted);display:flex;align-items:center;gap:5px}
  .sitesum .box .k i{width:8px;height:8px;border-radius:2px;display:inline-block}
  .sitesum .box .v{font-size:17px;font-weight:600;font-variant-numeric:tabular-nums;margin-top:3px}
  .sitesum .box .v small{font-size:11.5px;font-weight:400;color:var(--muted);margin-left:4px}
  .sitetable{max-height:430px;overflow:auto;border:1px solid var(--border);border-radius:8px}
  .sitetable table{font-size:12.5px}
  .sitetable thead th{cursor:default}
  .sitetable td.addr{white-space:normal;max-width:250px;line-height:1.5;color:#4a5261}
  .sitetable td.city{color:#1f2329;white-space:nowrap;max-width:150px;overflow:hidden;
    text-overflow:ellipsis}
  .sitetable td.qty{text-align:right;font-weight:600}
  .sitetable td.pct{text-align:right;color:var(--muted)}
  .sitetable .bar{height:5px;border-radius:3px;background:#eef1f5;overflow:hidden;margin-top:5px}
  .sitetable .bar i{display:block;height:100%;border-radius:3px}
  .sitetable tr.total td{background:#fafbfc;font-weight:600;border-top:1px solid var(--border)}
  .sitetable tr.total td.qty{color:var(--text)}
  .siteloc{display:inline-flex;align-items:baseline;gap:6px}
  .siteloc .nm{color:var(--muted);font-size:11.5px}
  __EXTRA_CSS__
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1><span id="regionTag" class="badge" style="background:#eef3ff;color:#2f6bff;border-color:#d8e3ff">US 美国</span> 订单时效看板<span id="demoBadge" class="badge" style="display:none">示例数据 · DEMO</span><span id="extBadge" class="badge" style="display:none;background:#fff8e6;color:#9a6a00;border-color:#f2e2b8" title="此页面通过外发口令访问：收件人姓名已打码，门牌地址不在数据中">外发视图 · 收件人已脱敏</span></h1>
      <div class="sub" id="subline"></div>
    </div>
    <div style="text-align:right">
      <div class="tabs" id="regionTabs"></div>
      <div class="sub" id="genline"></div>
    </div>
  </header>

  <div class="notice" id="notice" style="display:none"></div>

  <!-- 渠道页签：eBay / Depop 只是 Service ID 不同，流程一致；切换后整页只算该渠道 -->
  <div class="chans" id="chanBar">
    <span class="lab">渠道</span>
    <div class="tabs" id="chanTabs"></div>
    <span class="scope" id="chanScope"></span>
  </div>

  <!-- 时区口径：所有「按日」统计、日期下拉、日期筛选、时间显示都跟着这里走 -->
  <div class="chans" id="tzBar">
    <span class="lab">时区</span>
    <div class="tabs" id="tzTabs"></div>
    <span class="scope" id="tzScope"></span>
  </div>

  <section class="kpis" id="kpis"></section>
  <!-- 异常预警：卡片数字即「命中单数」，点一下就把下面的明细表筛成这批单 -->
  <section class="alerts" id="alerts"></section>

  <section class="grid2">
    <div class="card">
      <h2>每日单量</h2>
      <div class="cap">按订单创建日期统计（日期按上方<b>时区</b>划分），按<b>投递方（drop off）</b>堆叠：客户把包裹交给了谁</div>
      <div id="chartDaily"></div>
      <div class="legend" id="legendDaily"></div>
    </div>
    <div class="card">
      <h2>各段时效均值</h2>
      <div class="cap">订单创建 → A-SCAN → RDC 出库 → SPX 提取（小时）</div>
      <div id="chartDur"></div>
      <div class="legend">
        <span><i style="background:#c3cad6"></i>头程 / 到仓</span>
        <span><i style="background:#2f6bff"></i>出库 → SPX 交接</span>
      </div>
    </div>
  </section>

  <section class="grid2">
    <div class="card">
      <h2>每日 drop off 量级</h2>
      <div class="cap"><b>10/29 起</b> · 按<b>投递发生日</b>统计（不是下单日，日期按上方<b>时区</b>划分）· isEventCode = <b>FIRST_SCAN</b>
        （US 即 <b>DOUS002 / DOUD002</b>）· 按投递方堆叠 ·
        <b>点柱条</b>可在下方明细按该投递日筛选</div>
      <div id="chartDropDaily"></div>
      <div class="legend" id="legendDrop"></div>
    </div>
    <div class="card">
      <h2>drop off 汇总</h2>
      <div class="cap">同一口径下的量级与两家占比</div>
      <div class="dsum" id="dropSummary"></div>
    </div>
  </section>

  <!-- 每日 dropoff 点位：哪个点位收了多少件、归属 LMS 还是 PUDO INC -->
  <section class="card" id="siteCard">
    <h2>每日 dropoff 点位（Location ID）</h2>
    <div class="cap">按<b>投递发生日</b> × <b>投递点位</b>聚合，只统计已有 <b>FIRST_SCAN</b> 的单 ·
      <b>LMS</b> 的投递事件带官方 <code>locationId</code>；<b>PUDO INC</b> 的投递事件不带 ID，
      用<b>点位地址</b>做标识（表格里标 <span class="vcode alias">addr</span>）·
      城市取投递事件的 <code>oprCity</code></div>
    <div class="sitetool">
      <label for="siteDay">投递日</label>
      <select id="siteDay"></select>
      <label for="siteVendor">投递方</label>
      <select id="siteVendor">
        <option value="">全部</option>
        <option value="LMS">LMS</option>
        <option value="PUDO INC">PUDO INC</option>
      </select>
      <span class="count" id="siteCount"></span>
    </div>
    <div class="sitesum" id="siteSum"></div>
    <div class="sitetable" id="siteTable"></div>
    <div class="binnote" id="siteNote"></div>
  </section>

  <section class="grid2">
    <div class="card">
      <h2>PUDO vs LMS：RDC 出库 → SPX 提取</h2>
      <div class="cap">两个 RDC 的交接耗时对比（小时，条形长度=均值）· SPX 提取 = <b>SCA*/SCN*</b> 任一命中</div>
      <div id="chartVendor"></div>
    </div>
    <div class="card">
      <h2>出库 → SPX 时长分布</h2>
      <div class="cap">RDC 出库（warehouse out）→ SPX 首次提取的耗时，按单数分四档 ·
        <b>只统计两段都齐的单</b>，不是全部订单</div>
      <div id="chartBins"></div>
      <div class="binnote" id="binsNote"></div>
      <details class="exp">
        <summary>口径与怎么看</summary>
        <ul>
          <li><b>起止点</b>：起点 = RDC 出库 <code>DFDLUS002</code>（warehouse out，Depop 为
            <code>DFDLUD002</code>）；终点 = SPX 首次提取 <code>SCAUS002</code> 或 <code>SCN005</code>
            （<code>SCA*</code>/<code>SCN*</code> 任一命中，取最早那条）。两端都是北京时间。</li>
          <li><b>为什么只看这两个点</b>：这一段的唯一动作是「包裹从 RDC 交到 SPX 手上」，
            不掺头程、不掺末端派送。它长了，端到端一定长；它短，后面还有 SPX 自己的时效。</li>
          <li><b>为什么分档加起来不等于订单数</b>：还没出库、出了库但 SPX 还没提取的单，
            这一段时间根本算不出来，不进图。图上方的分档单数只覆盖「已完成交接」的那部分。</li>
          <li><b>怎么看</b>：≤24h = 当日或次日就交出去了，正常；24–48h 偏慢；
            48–72h 要问 RDC 当天为什么没发；<b>&gt;72h 是要人查的</b> ——
            点上面「出库 &gt;72h 无提取」预警卡，明细表会直接筛出这几单。</li>
          <li><b>两家投递方的差别</b>：LMS 是司机集货到仓、到仓即出库，交接常在 1 小时内；
            PUDO 单要先在网点落点，均值天然高些。所以横向比「PUDO vs LMS」那张卡更准，
            这张图看的是<b>整体分布形状</b>和尾部。</li>
          <li><b>颜色</b>：绿 → 红只是档位顺序（越红越长），<b>不是</b>业务阈值。</li>
        </ul>
      </details>
    </div>
  </section>

  <section class="card" id="detailCard">
    <h2>订单明细</h2>
    <div class="cap">
      RDC 出库 = JV Event Code <b id="evRdc"></b>（warehouse out）· SPX 提取 = <b id="evSpx"></b> · 时间为北京时间
    </div>
    <!-- 从上方异常卡片跳进来时，这里显示当前筛选条件，点「清除」恢复全量 -->
    <div class="alertbar" id="alertBar">
      正在显示<b id="alertName"></b>的单<span id="alertCount" class="muted"></span>
      <span class="x" id="alertClear">清除筛选</span>
    </div>
    <div class="toolbar">
      <input id="search" placeholder="搜索单号 / 参考号 / 收件人 / 州 / 产品…"/>
      <select id="vendorFilter"><option value="">全部 RDC</option></select>
      <select id="dropFilter"><option value="">全部投递方</option></select>
      <select id="statusFilter"><option value="">全部状态</option></select>
      <select id="dateField" title="日期筛选按哪一列算：建单日 / 投递日 / 妥投日">
        <option value="orderCreateTime">按建单日</option>
        <option value="dropTime">按投递日</option>
        <option value="deliveredTime">按妥投日</option>
      </select>
      <input type="date" id="dateVal" title="选一个日期，只看该日的单；留空则不限"/>
      <span class="clear" id="dateClear" title="清除日期筛选" style="display:none">清除日期</span>
      <label class="chk"><input type="checkbox" id="excludeTest" checked/> 排除测试单</label>
      <label class="chk" title="只看 OCUS001 建单与 DOUS002 投递的 vendorCode 都是 LM 的单">
        <input type="checkbox" id="onlyLms"/> 只看 LMS 同供<span id="lmsCnt" class="muted"></span></label>
      <span class="count" id="count"></span>
    </div>
    <div class="tbl-scroll">
      <table>
        <thead><tr id="thead"></tr></thead>
        <tbody id="tbody"></tbody>
      </table>
    </div>
    <div class="note">
      口径说明：<b id="noteLabel">OCRETURNLBDE002</b> = The sender has placed an order and the shipping label
      has been generated，即 <b>面单生成（label generated）</b>，每单恰好一条，取最早一条。
      <br/>
      <b>订单来源</b>按接口的 <b>serviceProductCode</b>（即 serviceID）判定：
      <b>USDOMS2D = eBay</b>、<b>UDDOMS2D = Depop</b>。US 接口没有 serviceId 字段，
      <code>channel</code> 两边都是 GPLT、<code>ebayOrderId</code> 在 Depop 单上是占位值
      <code>00-00000-00000</code>，都区分不了渠道，所以只认 serviceProductCode。
      <br/>
      投递方 = 收件扫描事件（<b>isEventCode = FIRST_SCAN</b>，US 即 <b id="noteDrop">DOUS002</b>）的 vendor，
      表示<b>客户把包裹交给了谁</b>：<b>PUDO INC</b>（投在 PUDO 点）或 <b>LMS</b>（交到 LMS 门店/站点）。
      投递方与出库方（RDC）是两件事，存在「投在 PUDO INC、却从 LMS 的 RDC 出库」的单，明细表会标「异」。
      有这条事件的单，投递方后面会带一枚 <b class="xflag fs" style="margin-left:0">首扫</b> 徽标
      （悬停可看 isEventCode、事件码与时间）；<b>没有徽标 = 还没被网点/门店扫到</b>，不是漏抓。
      <br/>
      RDC 出库 = JV Event Code <b id="noteRdc">DFDLUS002</b> = Package ready for dispatch，即 <b>RDC 出库（warehouse out）</b>
      （isEventCode = ML_FACILITY_OUT，码尾随 Service ID 变）。
      <b>PUDO INC 与 LMS 共用这一个事件码</b>，按<b>同一条事件的供应商事件码（vendorEventCode）</b>区分是谁出的库：
      <div id="outNodeList" style="margin:7px 0 2px"></div>
      <b id="noteSpx">SCAUS002</b> = Picked up by delivery carrier（vendor = SPX），即 <b>SPX 已提取</b>；
      <b>SCN005</b> = Processing at Origin Facility（始发中心处理）也算已提取。
      <b>两个码满足任意一个即为已交接，取最早一条</b> —— PUDO 链路出库后往往不报 SCAUS002，
      只认前者会把「确实已交给 SPX」的单误判成未交接。
      两者之差就是「货在 RDC 备好后，多久被 SPX 拿走」。同一单可能重复上报同码事件，取最早一条；
      若同一单被两家各报一次（少见，通常是交接），以<b>更早那条</b>定 RDC 归属，单元格会标「两家」。
      <br/>
      妥投 = JV Event Code <b id="noteDelivered">DELUS006 / DEL005</b>（isEventCode = DELIVERED，派送完成），
      <b>码尾随 Service ID 变</b>（eBay 走 US 后缀如 <code>DELUS006</code>，Depop 走 UD 后缀如 <code>DELUD006</code>），
      取数时按<b>语义字段 isEventCode</b> 匹配，不写死具体码；
      每单最多一条，没有该事件的单只是<b>还没派送到</b>，不是漏数。
      <br/>
      <b>「投递」与「妥投」是两件事，别对着一个数找另一个</b>：<b>投递</b>＝客户把包裹交到 PUDO 点 / LMS 门店
      （FIRST_SCAN，表里就是<b>投递（drop off）</b>那一列）；<b>妥投</b>＝派送完成（DELIVERED）。
      一张单通常先投递、过几天才妥投，所以<b>投递列有时间、妥投列仍然是空白是正常的</b>。
      要按天找单，用工具栏的 <b>按投递日 / 按妥投日</b> 日期筛选，或直接<b>点上方「每日 drop off 量级」的柱条</b>跳转。
      <div id="crossNote" style="margin-top:6px"></div>
      <div id="lmsNote" style="margin-top:6px"></div>
    </div>
  </section>

  <footer id="foot"></footer>
</div>
__EXTRA_HTML__

<script>
/* 用 var 而不是 const：加密部署版里这几个值要由「解锁脚本」在另一个 <script> 中赋值，
   const 绑定无法跨脚本重写。本地内嵌版行为完全不变。 */
var META = __META_JSON__;
var ALL = __DATA_JSON__;
/* 串场单：PUDO 点投递 → LMS 司机取走（cross_vendor.py 产出，按运单号索引） */
var CROSS = __CROSS_JSON__;
/* 投递取消异常：已投递（FIRST_SCAN）却被取消（SHIPCCUS001）的订单 */
var DROPOFF = __DROPOFF_JSON__;
/* LMS 同供单：建单 OCUS001 与投递 DOUS002/DOUD002 的 vendorCode 都是 LM（lms_same_vendor.py 产出） */
var LMSSAME = __LMSSAME_JSON__;
let ORDERS = [];

/* ---------- 工具 ---------- */
const fmtNum = v => (v === null || v === undefined) ? '—' : (Math.round(v*10)/10).toFixed(1);
const avg = arr => { const v = arr.filter(x => x !== null && x !== undefined); return v.length ? v.reduce((a,b)=>a+b,0)/v.length : null };
const shortTime = s => s ? s.slice(5) : '';
const uniq = arr => [...new Set(arr)];
const cnt = k => ORDERS.filter(o => o[k] !== null && o[k] !== undefined && o[k] !== '').length;

function statusClass(s){
  if (s === '已妥投') return 't-done';
  if (s === '已交SPX') return 't-ok';
  if (s === '已出库待SPX') return 't-warn';
  return 't-pend';
}

/* ---------- 供应商（RDC）配色 ---------- */
const VCOLORS = ['#2f6bff','#0e9f9f','#8b5cf6','#f59e0b','#94a3b8'];
let vendorCount = {}, VENDORS = [], VCOLOR = {};
/* PUDO INC / LMS 是固定的两家 drop off vendor，颜色写死，不随单量排序漂移 */
const VCOLOR_FIXED = { 'PUDO INC': '#0e9f9f', 'LMS': '#2f6bff' };
function computeVendors(regionKey){
  const rk = regionKey || 'ALL';
  vendorCount = {};
  ORDERS.forEach(o => { vendorCount[o.vendor] = (vendorCount[o.vendor]||0)+1 });
  /* 登记的 vendor 永远进列表，本窗口 0 单也不消失（否则筛选器和对比图会以为这家没了） */
  const nodes = (META.outNodes && (rk === 'ALL'
      ? Object.keys(META.outNodes).reduce((a,k)=>a.concat(META.outNodes[k]||[]), [])
      : (META.outNodes[rk]||[]))) || [];
  const regUniq = uniq(nodes.map(n => n.vendor).filter(Boolean));
  regUniq.forEach(v => { if (!(v in vendorCount)) vendorCount[v] = 0; });
  const others = Object.keys(vendorCount)
    .filter(v => v && v !== '—' && !regUniq.includes(v))
    .sort((a,b)=>vendorCount[b]-vendorCount[a]);
  VENDORS = [...regUniq, ...others].slice(0, 6);
  VCOLOR = {};
  VENDORS.forEach((v,i)=> VCOLOR[v] = VCOLOR_FIXED[v] || VCOLORS[i % VCOLORS.length]);
}
const vendorColor = v => VCOLOR[v] || '#cbd5e1';

/* ---------- 投递方（drop off）配色与统计 ----------
   客户把包裹交给谁（PUDO INC / LMS）和谁出的库是两件事：
   存在「投在 PUDO INC、却从 LMS 的 RDC 出库」的单，按投递方统计才不会归错。 */
const DCOLOR_NODROP = '#c3cad6';
/* 投递方可能比出库方多（UK 有 DHL / EP / FE / AY 等首程承运商），色板给足 */
const DCOLORS = ['#2f6bff','#0e9f9f','#8b5cf6','#f59e0b','#e0559b','#0ea5e9','#65a30d','#94a3b8'];
let dropCount = {}, DROPS = [], DCOLOR = {};
/* 两家固定的 drop off vendor，颜色写死，不随单量排序漂移 */
const DCOLOR_FIXED = { 'PUDO INC': '#0e9f9f', 'LMS': '#2f6bff' };
function computeDrops(regionKey){
  const rk = regionKey || 'ALL';
  dropCount = {};
  ORDERS.forEach(o => {
    const key = o.dropVendor || '未投递';
    dropCount[key] = (dropCount[key]||0)+1;
  });
  /* 登记的投递方永远进列表，本窗口 0 单也不消失（否则筛选器会以为这家没了） */
  const nodes = (META.dropNodes && (rk === 'ALL'
      ? Object.keys(META.dropNodes).reduce((a,k)=>a.concat(META.dropNodes[k]||[]), [])
      : (META.dropNodes[rk]||[]))) || [];
  const regUniq = uniq(nodes.map(n => n.vendor).filter(Boolean));
  regUniq.forEach(v => { if (!(v in dropCount)) dropCount[v] = 0; });
  const others = Object.keys(dropCount)
    .filter(v => v !== '未投递' && !regUniq.includes(v))
    .sort((a,b)=>dropCount[b]-dropCount[a]);
  DROPS = [...regUniq, ...others].slice(0, 10);
  DCOLOR = {};
  DROPS.forEach((v,i)=> DCOLOR[v] = DCOLOR_FIXED[v] || DCOLORS[i % DCOLORS.length]);
}
const dropColor = v => !v ? DCOLOR_NODROP : (DCOLOR[v] || '#cbd5e1');

/* ---------- 头部 ---------- */
const LABELS = { US:'US 美国', UK:'UK 英国', AU:'AU 澳洲' };
const labelOf = k => (META.regionLabels && META.regionLabels[k]) || LABELS[k] || k;
const codeOf = k => ((META.eventCodes||{})[k]) || { rdc: META.rdcEventCode, spx: META.spxEventCode };

/* 出库节点登记：PUDO INC = PUP，LMS = shipment.injected/made_ready，两者共用同一个 jvEventCode */
function outNodeRows(){
  const all = META.outNodes || {};
  const keys = region === 'ALL' ? Object.keys(all) : [region];
  const seen = new Set(), rows = [];
  keys.forEach(k => (all[k]||[]).forEach(n => {
    const id = n.vendorCode + '|' + n.code;
    if (seen.has(id)) return;
    seen.add(id); rows.push(n);
  }));
  return rows;
}

function drawOutNodes(){
  const box = document.getElementById('outNodeList');
  if (!box) return;
  const rows = outNodeRows();
  if (!rows.length) { box.innerHTML = ''; return; }
  const aliases = META.outNodeAliases || {};
  const aliasHtml = Object.keys(aliases).length
    ? `<div style="color:#8a919b;font-size:11px;padding-left:2px;margin-top:2px">
         ${Object.entries(aliases).map(([k,v])=>`<span class="vcode alias" title="替代码：非登记表主码，实测在用">${k}</span> → ${v}`).join(' &nbsp; ')}
       </div>` : '';
  box.innerHTML = rows.map(n => {
    const cls = n.vendorCode === 'PUP' ? 'pup' : (n.vendorCode === 'LMS' ? 'lms' : '');
    const note = n.note ? `<div style="color:#8a919b;font-size:11px;padding-left:2px">${n.note}</div>` : '';
    const vec = n.vendorEventCode
      ? `<div style="color:#5b6a86;font-size:11.5px;padding-left:2px">
           供应商事件码 <span class="vcode">${n.vendorEventCode}</span></div>` : '';
    return `<div style="padding:2px 0">
      <div style="display:flex;align-items:baseline;gap:8px">
        <span class="vcode ${cls}">${n.vendorCode}</span>
        <b style="font-weight:600">${n.vendor}</b>
        <span style="color:#8a919b">→</span>
        <span class="vcode">${n.code}</span>
        <span style="color:#5b6a86">${n.desc||''}</span>
      </div>${vec}${note}
    </div>`;
  }).join('') + aliasHtml;
}

/* 串场：包裹投在 PUDO 点，却被 LMS 的司机取走（cross_vendor.py 判定） */
function drawCross(){  const box = document.getElementById('crossNote');
  if (!box) return;
  /* 只显示当前区域（ORDERS）里存在的串场单，切到 UK 时不该还挂着 US 的单号 */
  const inRegion = new Set((ORDERS && ORDERS.length ? ORDERS : ALL).map(o => o.ocOrderNo));
  const keys = Object.keys(CROSS).filter(tn => inRegion.has(tn));
  if (!keys.length) { box.innerHTML = ''; return; }
  const items = keys.map(tn => {
    const x = CROSS[tn] || {};
    const d = (x.dropAt||'').slice(5,16), p = (x.pickAt||'').slice(5,16);
    return `<span style="white-space:nowrap"><b class="mono">${tn}</b>
      <span style="color:#5b6a86">投递 ${d} → ${x.pickLabel||x.pickCode} ${p}
      <span style="color:#b4232a">+${x.delayH}h</span></span></span>`;
  }).join('<span style="color:#c9ccd1"> · </span>');
  box.innerHTML = `<div style="background:#fdecec;border:1px solid #f5c7c7;border-radius:6px;
      padding:6px 9px;margin-top:4px">
    <b style="color:#b4232a">串场 ${keys.length} 单</b>
    <span style="color:#5b6a86">：<code>DOUS002</code> 由 <b>PUDO</b> 上报（客户投在 PUDO 点），
      但随后的取件事件 <code>vendorCode = LM</code>，即被 <b>LMS 司机取走</b>。不属于本单的归属 vendor。</span>
    <div style="margin-top:4px;font-size:11.5px;color:#3f4a5a">${items}</div>
  </div>`;
}

/* LMS 同供：建单(OCUS001)与投递(DOUS002/DOUD002)的 vendorCode 都是 LM（lms_same_vendor.py 判定） */
function drawLmsNote(){
  const box = document.getElementById('lmsNote');
  if (!box) return;
  const all = Object.keys(LMSSAME);
  if (!all.length) { box.innerHTML = ''; return; }
  const realAll = all.filter(k => !LMSSAME[k].isTest).length;
  const testAll = all.length - realAll;
  const inScope = (ORDERS_DETAIL || []).filter(o => lmsOf(o)).length;
  /* 真实单 0 命中时必须写明原因，否则会被当成「功能没生效」；
     分母固定用「明细窗口内的真实单」，不随「排除测试单」勾选变化，避免同一句话出现两个数 */
  const realInDetail = (ALL || []).filter(o => !o.isTest
      && (!DETAIL_START || dayOf(o) >= DETAIL_START)
      && (region === 'ALL' || o.region === region));
  const why = realAll === 0
    ? `<br/><b>本窗口真实单 0 单命中</b>：明细窗口内 ${realInDetail.length} 张真实单，
       <code>OCUS001</code> 的 vendorCode 全是 <b>PU</b>（语义是「待投递到 PUDO 点」），
       没有一张是 <b>LM</b> 建单。命中的都是 10/19–10/28 压测期测试单，
       且建单与投递两条事件只差 2 秒（建单即投递），属压测脚本产物。`
    : '';
  box.innerHTML = `<div style="background:#eaf4fb;border:1px solid #c3dff0;border-radius:6px;
      padding:6px 9px;margin-top:4px">
    <b style="color:#0b5f8a">LMS 同供 · 当前明细 ${inScope} 单</b>
    <span style="color:#5b6a86">｜全窗口命中 <b>${all.length}</b> 单（真实 ${realAll} / 测试 ${testAll}）：
      单内 <code>OCUS001</code>（Shipment created，建单）与 <code>DOUS002 / DOUD002</code>（客户投递）
      两条事件的 <code>vendorCode</code> <b>都是 LM</b>，即从建单到投递全程由 <b>LMS</b> 承接、不经 PUDO INC。
      判定看<b>事件自身的供应商</b>，与订单上的出库方（RDC）无关；明细表里标蓝「LMS 同供」，
      勾选上方复选框可只看这批单。</span>${why}
  </div>`;
}

/* ---------- 事件码文案：按「当前渠道实际出现的码」显示 ----------
   eBay 用 US 后缀（DELUS002/DELUS006），Depop 用 UD 后缀（DELUD002/DELUD006）。
   写死哪一套都会在另一个渠道下说谎，所以从数据里取，取不到再回退到 META。
   池子用「该渠道全部单」而不是当前筛选结果：切到 Depop 时窗口内可能恰好没有妥投单，
   但码本身仍然是 DELUD006，不能因此显示成 US 的码。 */
function chanPool(){
  const inRegion = region === 'ALL' ? ALL : ALL.filter(o => o.region === region);
  return channel === 'ALL' ? inRegion : inRegion.filter(o => srcOf(o) === channel);
}
function codesInScope(field, fallback){
  const got = uniq(chanPool().map(o => o[field]).filter(Boolean));
  return got.length ? got.join(' / ') : fallback;
}

function drawHeader(){
  document.getElementById('demoBadge').style.display = META.demo ? '' : 'none';
  const extBadge = document.getElementById('extBadge');
  if (extBadge) extBadge.style.display = META.external ? '' : 'none';
  document.getElementById('regionTag').textContent =
    region === 'ALL' ? 'UK + US 全部' : labelOf(region);
  const cd = codeOf(region === 'ALL' ? 'US' : region);
  /* 出库/提取码同样按渠道实际出现的码显示：Depop 是 DFDLUD002 / SCAUD002 */
  document.getElementById('evRdc').textContent = codesInScope('outNodeCode', cd.rdc || 'DFDLUS002');
  document.getElementById('evSpx').textContent = codesInScope('spxPickupCode', cd.spx || 'SCAUS002');
  const nRdc = cnt('rdcOutTime'), nSpx = cnt('spxPickupTime');
  document.getElementById('noteRdc').textContent = codesInScope('outNodeCode', cd.rdc || 'DFDLUS002');
  document.getElementById('noteSpx').textContent = codesInScope('spxPickupCode', cd.spx || 'SCAUS002');
  const noteLabel = document.getElementById('noteLabel');
  if (noteLabel) noteLabel.textContent = META.labelEventCode || 'OCRETURNLBDE002';
  const noteDel = document.getElementById('noteDelivered');
  if (noteDel) noteDel.textContent = codesInScope('deliveredCode',
    (META.deliveredEventCodes || ['DELUS006', 'DEL005']).join(' / '));
  const noteDrop = document.getElementById('noteDrop');
  if (noteDrop) {
    const codes = codesInScope('dropEventCode',
      (META.dropEventCodes && META.dropEventCodes.length) ? META.dropEventCodes.join(' / ') : 'DOUS002');
    noteDrop.textContent = codes;
  }
  drawOutNodes();
  drawCross();

  const byRegion = {};
  ALL.forEach(o => { byRegion[o.region] = (byRegion[o.region]||0)+1 });
  const parts = Object.keys(byRegion).sort().map(k => `${labelOf(k)} ${byRegion[k]}`);
  document.getElementById('subline').textContent =
    `抓数范围 ${META.dateFrom||''} ~ ${META.dateTo||''}（后台 ${totalCount} 单`
    + (region === 'ALL' ? `：${parts.join(' · ')}` : '')
    + `） · 汇总口径 自 ${mmdd(KPI_START)} 起 ${ORDERS.length} 单`
    + (kpiTestCount
        ? (excludeTest ? `（已排除 ${kpiTestCount} 单测试）` : `（含 ${kpiTestCount} 单测试）`)
        : '')
    + ` · 明细表 自 ${mmdd(DETAIL_START)} 起 ${ORDERS_DETAIL.length} 单`
    + (META.anomalies ? ` · ${META.anomalies} 单时间倒挂已剔除` : '');
  document.getElementById('genline').textContent = `生成于 ${META.generatedAt||''}`;

  /* 没有 RDC/SPX 节点时给明确说明，避免被误读成「数据缺失」。
     原因分两种，要看这批单卡在哪一步，不能一律说成跨境首程单：
       a) 绝大多数还没投递（待A-SCAN）→ 货还没进仓，自然没有出库/提取
       b) 大多已投递但无出库 → 才是跨境首程单（FedEx / 分拨码，不经 PUDO/LMS） */
  const notice = document.getElementById('notice');
  if (!nRdc && !nSpx) {
    const outCode = codesInScope('outNodeCode', cd.rdc || 'DFDLUS002');
    const spxCode = codesInScope('spxPickupCode', cd.spx || 'SCAUS002');
    const nNoDrop = ORDERS.filter(o => !o.aScanTime).length;
    const mostlyNoDrop = nNoDrop >= ORDERS.length * 0.6;
    notice.style.display = '';
    notice.innerHTML = mostlyNoDrop
      ? `当前${channel === 'ALL' ? '区域' : '渠道'} ${ORDERS.length} 单里 <b>${nNoDrop} 单还没投递</b>（待 A-SCAN），
         货还没进仓，因此还没有 <b>${outCode}</b> / <b>${spxCode}</b> 事件，
         「RDC 出库 → SPX」指标为空属于正常，不是漏数。`
      : `当前${channel === 'ALL' ? '区域' : '渠道'} ${ORDERS.length} 单里没有任何 <b>${outCode}</b> / <b>${spxCode}</b> 事件 ——
         这批是跨境首程单（事件以 FedEx / 分拨码为主），不含 PUDO / LMS 的 RDC 出库与 SPX 提取节点，
         因此「RDC 出库 → SPX」相关指标为空，只能看单量与 A-SCAN 时效。`;
  } else {
    notice.style.display = 'none';
  }
}

/* ---------- KPI ---------- */
function drawKpis(){
  const nAScan = cnt('aScanTime'), nRdc = cnt('rdcOutTime'), nSpx = cnt('spxPickupTime');
  /* 面单生成：jvEventCode = OCRETURNLBDE002，每单一次，有它 = 面单已出 */
  const nLabel = cnt('labelTime');
  const missList = ORDERS.filter(o => !o.labelTime);
  /* 缺失单若绝大多数是 E 开头的跨境首程产品（EDAE / ESEPAE / EXAE / ESAYAE / EXFE*），
     那是接口对这类单压根不上报该码，不能说成「面单漏生成」，文案要分开 */
  const missCross = missList.filter(o => /^E/.test(o.product || '')).length;
  const missNote = !missList.length ? '（无缺失）'
    : (missCross >= missList.length * 0.8
        ? `（${missList.length} 单为跨境首程单，接口不报此码）`
        : `（${missList.length} 单未上报）`);
  /* 妥投：isEventCode=DELIVERED，具体 jvEventCode 随 Service ID 变（US=DELUS006 / UD=DELUD006），
     这里按当前渠道实际出现的码显示，不再按区域写死 */
  const nDelivered = cnt('deliveredTime');
  const delCode = codesInScope('deliveredCode', region === 'ALL'
    ? ((META.deliveredEventCodes || ['DELUS006', 'DEL005']).join(' / '))
    : (codeOf(region).delivered || 'DELUS006'));
  const delAvg = avg(ORDERS.map(o => o.hCreateToDelivered));
  /* 订单来源拆分：serviceProductCode USDOMS2D=eBay / UDDOMS2D=Depop */
  const nEbay = ORDERS.filter(o => srcOf(o) === 'eBay').length;
  const nDepop = ORDERS.filter(o => srcOf(o) === 'Depop').length;
  const srcSplit = (nEbay || nDepop)
    ? `eBay ${nEbay}` + (nDepop ? ` · Depop ${nDepop}` : '')
    : '';
  const kpis = [
    { label:'<span class="dot" style="background:#2f6bff"></span>订单总数'
             + (excludeTest && kpiTestCount ? '<span class="kpi-sub">真实单</span>' : '')
             + `<span class="kpi-sub mono">自 ${mmdd(KPI_START)}</span>`,
      val: ORDERS.length, unit:'单',
      hint: (kpiTotalCount !== ORDERS.length
              ? `汇总窗口(自 ${mmdd(KPI_START)}) 后台 ${kpiTotalCount} 单 · 排除测试 ${kpiTestCount} 单 · ` : '')
            + `已 A-SCAN ${nAScan} · 已出库 ${nRdc}`
            + (srcSplit ? ` · ${srcSplit}` : '') },
    { label:'<span class="dot" style="background:#8b5cf6"></span>面单生成'
             + '<span class="kpi-sub mono">OCRETURNLBDE002</span>',
      title: '面单生成：jvEventCode=OCRETURNLBDE002（卖家已下单并生成物流面单），每单一次。'
           + 'US 单全量上报；UK 的跨境首程单（EDAE / ESPAE / EXAE）接口不报此码，'
           + '因此 UK 覆盖率天然低于 100%。',
      val: nLabel, unit:'单',
      hint: nLabel
        ? `覆盖 ${nLabel}/${ORDERS.length} 单${missNote}`
        : '本快照未采集该事件，需重新抓取' },
    { label:'<span class="dot" style="background:#12a150"></span>妥投'
             + `<span class="kpi-sub mono">${delCode}</span>`,
      title: '妥投：isEventCode=DELIVERED（派送完成）。jvEventCode 按区域不同，US=DELUS006、UK=DEL005，'
           + '取数时两个码都认，只用于展示的主码才按区域切换。每单最多一条，'
           + '没有该事件的单是「还没派送到」。',
      val: nDelivered, unit:'单',
      hint: nDelivered
        ? `占本窗口 ${nDelivered}/${ORDERS.length} 单`
          + (delAvg !== null ? ` · 均 下单→妥投 ${fmtNum(delAvg)} 小时` : '')
        : `本窗口没有 ${delCode} 事件（单还没派送到，非接口异常）` },
    { label:'<span class="dot" style="background:#0e9f9f"></span>RDC 出库 → SPX 提取',
      val: fmtNum(avg(ORDERS.map(o=>o.hRdcToSpx))), unit:'小时',
      title: 'SPX 提取 = isEventCode IN_TRANSIT 下的 SCA*/SCN* 事件（司机提取或始发中心处理），'
           + '任意一个命中即算，取最早一条',
      hint: `覆盖 ${cnt('hRdcToSpx')} 单（有出库，且 ${codesInScope('spxPickupCode','SCAUS002/SCN005')} 任一命中）` },
    { label:'A-SCAN → RDC 出库',
      val: fmtNum(avg(ORDERS.map(o=>o.hAScanToRdc))), unit:'小时',
      hint: `覆盖 ${cnt('hAScanToRdc')} 单` },
    { label:'SPX 交接率',
      val: ORDERS.length ? Math.round(nSpx / ORDERS.length * 100) : 0, unit:'%',
      hint: `已交 ${nSpx} 单 · 出库待交 ${ORDERS.filter(o=>o.status==='已出库待SPX').length} 单` },
  ];
  document.getElementById('kpis').innerHTML = kpis.map(k=>`
  <div class="kpi"${k.title ? ` title="${k.title}"` : ''}>
    <div class="label">${k.label}</div>
    <div class="val">${k.val}<small>${k.val === '—' ? '' : k.unit}</small></div>
    <div class="hint">${k.hint}</div>
  </div>`).join('');
}

/* ---------- 每日单量（按投递方堆叠） ---------- */
function drawDaily(){
  const map = {};
  ORDERS.forEach(o => {
    const d = isoDay(o.orderCreateTime);
    if (!d) return;
    map[d] = map[d] || {};
    const v = o.dropVendor || '未投递';
    map[d][v] = (map[d][v]||0)+1;
  });
  const days = Object.keys(map).sort();
  const box = document.getElementById('chartDaily');
  if (!days.length) { box.innerHTML='<div class="empty">无数据</div>'; return; }

  const totals = days.map(d=>Object.values(map[d]).reduce((a,b)=>a+b,0));
  const maxV = Math.max(...totals, 1);
  const W=620, rowH=24, padL=46, padR=44, padT=10;
  const H = padT + days.length*rowH + 6;
  const barMaxW = W - padL - padR;

  let svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}">`;
  days.forEach((d,i)=>{
    const y = padT + i*rowH;
    const total = totals[i];
    const w = total/maxV*barMaxW;
    let x = padL;
    /* 堆叠顺序跟图例一致：登记的两家在前，「未投递」永远垫底 */
    const order = new Map(DROPS.map((v,i)=>[v,i]));
    order.set('未投递', 999);
    const keys = Object.keys(map[d]).sort((a,b)=>
      (order.has(a) ? order.get(a) : 500) - (order.has(b) ? order.get(b) : 500));
    keys.forEach(v=>{
      const c = map[d][v]; if (!c) return;
      const segW = c/total*w;
      const col = v === '未投递' ? DCOLOR_NODROP : dropColor(v);
      svg += `<rect x="${x}" y="${y+5}" width="${Math.max(segW-1,1)}" height="13" fill="${col}" rx="2"/>`;
      x += segW;
    });
    svg += `<text x="${padL-8}" y="${y+15}" font-size="11.5" fill="#8a919b" text-anchor="end">${shortTime(d)}</text>`;
    svg += `<text x="${x+8}" y="${y+15}" font-size="11.5" fill="#1f2329" font-weight="600">${total}</text>`;
  });
  svg += `</svg>`;
  box.innerHTML = svg;

  /* 图例计数必须跟柱条同源（当前 ORDERS，即汇总窗口），不能直接用 computeDrops 的
     全局 dropCount——那是明细窗口的统计，两处窗口不同会让人以为图例在骗人 */
  const inView = v => ORDERS.filter(o => (o.dropVendor || '未投递') === v).length;
  document.getElementById('legendDaily').innerHTML =
    DROPS.map(v=>`<span><i style="background:${DCOLOR[v]}"></i>${v}（${inView(v)}）</span>`).join('')
    + (inView('未投递')
        ? `<span><i style="background:${DCOLOR_NODROP}"></i>未投递（${inView('未投递')}）</span>` : '');
}

/* ---------- 每日 drop off（FIRST_SCAN）量级 ----------
   和上面「每日单量」是两个口径：这里按**投递发生日**统计，不是下单日。
   投递一般比下单晚 1–3 天，若按下单日归集，10/29 之前下单、之后才投递的单会整段消失，
   日量级就会失真。所以这个卡片吃**全部已抓订单**（不受 KPI_START 限制），
   只跟随「排除测试单」开关。 */
const DROP_FIXED = ['PUDO INC', 'LMS'];

/* ---------- 时区口径：北京 / 纽约（页面右上角可切） ----------
   数据里所有 *Time 字段都是**北京时间墙钟字符串**（scraper 落盘时 astimezone(TZ_CN)）。
   换算到纽约必须交给 Intl，不能固定减 13 小时：10/29–10/31 纽约还是 EDT，只差 12 小时，
   11/01 之后才切换到 EST（-13h）。夏令时由时区数据库处理。
   切换只影响「按日分桶 / 日期筛选 / 时间显示」，时长类指标（两时刻之差）不受影响。 */
const TZ_LIST = [
  { key:'bj', label:'北京', tz:'Asia/Shanghai' },
  { key:'ny', label:'纽约', tz:'America/New_York' },
];
let tzMode = 'bj';
try { const _s = localStorage.getItem('oc_tz'); if (_s) tzMode = _s; } catch(e) {}
if (!TZ_LIST.some(t => t.key === tzMode)) tzMode = 'bj';
const tzOf = () => (TZ_LIST.find(t => t.key === tzMode) || TZ_LIST[0]).tz;
const _fmtCache = {};
const _dtFmt = tz => _fmtCache['dt|' + tz] || (_fmtCache['dt|' + tz] = new Intl.DateTimeFormat(
  'en-US', { timeZone: tz, year:'numeric', month:'2-digit', day:'2-digit',
            hour:'2-digit', minute:'2-digit', hourCycle:'h23' }));
const TS_RE = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/;
/* 北京墙钟 → 真正的 UTC 毫秒。不能交给 Date.parse：那是按浏览器本地时区解释的 */
function cnMs(s){
  const m = TS_RE.exec(String(s || ''));
  if (!m) return NaN;
  return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4] - 8, +m[5], +(m[6] || 0));
}
function _ymd(ms, tz){
  const o = {};
  _dtFmt(tz).formatToParts(new Date(ms)).forEach(p => { o[p.type] = p.value; });
  return o;
}
/* 时间戳 → 所选时区的日期键（YYYY-MM-DD） */
function tzDay(s){
  const ms = cnMs(s);
  if (isNaN(ms)) return '';
  const o = _ymd(ms, tzOf());
  return o.year + '-' + o.month + '-' + o.day;
}
/* 时间戳 → 所选时区的完整时刻字符串。北京模式原样返回，零开销 */
function tzStamp(s){
  if (!s) return '';
  if (tzMode === 'bj') return String(s);
  const ms = cnMs(s);
  if (isNaN(ms)) return String(s);
  const o = _ymd(ms, tzOf());
  return o.year + '-' + o.month + '-' + o.day + ' ' + o.hour + ':' + o.minute;
}

/* 按日切分的唯一入口：
   - 带 '/' 的是配置里的窗口边界（'2026/10/19 00:00:00'），不做时区换算；
   - 纯日期原样返回；
   - 其余都是订单时间戳，跟着 tzMode 走。 */
const isoDay = s => {
  if (!s) return '';
  const str = String(s);
  if (str.indexOf('/') >= 0) return str.slice(0, 10).replace(/\//g, '-');
  if (str.length <= 10) return str.slice(0, 10);
  return tzDay(str) || str.slice(0, 10);
};
function dayRange(from, to){
  const out = [];
  if (!from || !to || from > to) return out;
  const a = Date.parse(from + 'T00:00:00Z'), b = Date.parse(to + 'T00:00:00Z');
  if (isNaN(a) || isNaN(b)) return out;
  for (let t = a; t <= b; t += 86400000) out.push(new Date(t).toISOString().slice(0, 10));
  return out;
}

function drawDropDaily(){
  const box = document.getElementById('chartDropDaily');
  const sumBox = document.getElementById('dropSummary');
  if (!box) return;

  /* 时间窗跟「汇总口径」对齐：投递发生日 ≥ KPI_START（10/29），末日取抓数窗口末。
     注意这里卡的是**投递日**，不是下单日——否则 10/29 前下单、之后才投递的包裹会被误杀。 */
  const dFrom = (KPI_START || isoDay(META.dateFrom)), dTo = isoDay(META.dateTo);
  const fetchFrom = isoDay(META.dateFrom);
  const src = ORDERS_DROP || [];
  const inWin = [], beforeWin = [], dirty = [];
  src.forEach(o => {
    if (!o.dropTime) return;
    const d = isoDay(o.dropTime);
    if (dTo && d > dTo) dirty.push(o);                       // 投递日晚于窗口末：脏数据
    else if (fetchFrom && d < fetchFrom) dirty.push(o);      // 早于抓数窗口：脏数据
    else if (dFrom && d < dFrom) beforeWin.push(o);          // 10/19–10/28：汇总口径之前
    else inWin.push(o);
  });

  const map = {}, agg = {};
  inWin.forEach(o => {
    const d = isoDay(o.dropTime), v = o.dropVendor || '其他';
    map[d] = map[d] || {};
    map[d][v] = (map[d][v] || 0) + 1;
    agg[v] = (agg[v] || 0) + 1;
  });
  const got = Object.keys(map).sort();

  /* 图例：固定两家在前，其余按量排 */
  const vendors = [...DROP_FIXED.filter(v => v in agg),
                   ...Object.keys(agg).filter(v => !DROP_FIXED.includes(v)).sort((a,b)=>agg[b]-agg[a])];
  const colorOf = v => DCOLOR_FIXED[v] || (v === '其他' ? '#94a3b8' : '#c3cad6');

  if (!got.length){
    box.innerHTML = '<div class="empty">本窗口还没有 FIRST_SCAN（drop off）事件</div>';
    if (sumBox) sumBox.innerHTML = '<div class="empty">无数据</div>';
    document.getElementById('legendDrop').innerHTML = '';
    return;
  }

  /* 日期补齐：从最早投递日铺到窗口末日，中间没有投递的空白也画出来（那段是真的没量） */
  const to = dTo > got[got.length-1] ? dTo : got[got.length-1];
  let days = dayRange(dFrom || got[0], to);
  const CAP = 45;
  if (days.length > CAP) days = days.slice(-CAP);

  const totals = days.map(d => vendors.reduce((a,v)=>a+((map[d]||{})[v]||0), 0)
                                          + Object.keys(map[d]||{}).filter(v=>!vendors.includes(v))
                                               .reduce((a,v)=>a+map[d][v],0));
  const maxV = Math.max(...totals, 1);
  const W = 620, rowH = 24, padL = 46, padR = 46, padT = 10;
  const H = padT + days.length * rowH + 6;
  const barMaxW = W - padL - padR;

  let svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}">`;
  days.forEach((d, i) => {
    const y = padT + i * rowH;
    const total = totals[i];
    const row = map[d] || {};
    const w = total / maxV * barMaxW;
    let x = padL;
    const keys = [...vendors.filter(v => v in row),
                  ...Object.keys(row).filter(v => !vendors.includes(v))];
    keys.forEach(v => {
      const c = row[v]; if (!c) return;
      const segW = c / total * w;
      svg += `<rect class="dseg" data-d="${d}" style="cursor:pointer" x="${x}" y="${y+5}"
               width="${Math.max(segW-1,1)}" height="13" rx="2"
               fill="${colorOf(v)}"><title>${d} ${v}：${c} 单（点击按投递日筛选明细）</title></rect>`;
      x += segW;
    });
    svg += `<text x="${padL-8}" y="${y+15}" font-size="11.5" fill="#8a919b"
             text-anchor="end">${mmdd(d)}</text>`;
    svg += `<text x="${padL + Math.max(w,0) + 8}" y="${y+15}" font-size="11.5" font-weight="600"
             fill="${total ? '#1f2329' : '#c0c6cf'}">${total || '0'}</text>`;
  });
  svg += `</svg>`;
  box.innerHTML = svg;

  /* 点柱条 = 我要看这天投递的单：直接把明细切到「投递日 = 该天」，
     不用再靠建单日去猜——投递一般比建单晚 1–3 天，靠建单日永远对不上 */
  box.querySelectorAll('rect.dseg').forEach(r => {
    r.addEventListener('click', () => {
      dateField = 'dropTime'; dateVal = r.dataset.d || '';
      const df = document.getElementById('dateField'), dv = document.getElementById('dateVal');
      if (df) df.value = dateField;
      if (dv) dv.value = dateVal;
      dropFilter = ''; const dEl = document.getElementById('dropFilter'); if (dEl) dEl.value = '';
      statusFilter = ''; const sEl = document.getElementById('statusFilter'); if (sEl) sEl.value = '';
      renderTable();
      const tbl = document.querySelector('.tbl-scroll');
      if (tbl) tbl.scrollIntoView({behavior:'smooth', block:'start'});
    });
  });

  /* 图例 */
  document.getElementById('legendDrop').innerHTML =
    vendors.map(v => `<span><i style="background:${colorOf(v)}"></i>${v}（${agg[v]}）</span>`).join('')
    + (beforeWin.length
        ? `<span><i style="background:#e8ebef"></i>10/29 前投递 ${beforeWin.length} 单（不计入）</span>` : '')
    + (dirty.length
        ? `<span><i style="background:#f0dede"></i>投递时间异常 ${dirty.length} 单（已剔除）</span>` : '');

  /* 汇总面板 */
  if (sumBox){
    const total = inWin.length;
    const activeDays = got.length;
    const peakDay = got.reduce((a,d) => totals[days.indexOf(d)] > (totals[days.indexOf(a)]||0) ? d : a, got[0]);
    const peakVal = totals[days.indexOf(peakDay)] || 0;
    const avg = activeDays ? (total / activeDays) : 0;
    const avgAll = days.length ? (total / days.length) : 0;
    const pct = n => total ? Math.round(n / total * 100) : 0;
    const pn = agg['PUDO INC'] || 0, ln = agg['LMS'] || 0;
    const row = (label, val, extra, color) => `
      <div class="r"><span>${color ? `<i style="background:${color}"></i>` : ''}${label}</span>
      <b>${val}${extra ? `<em>${extra}</em>` : ''}</b></div>`;
    sumBox.innerHTML =
        row('合计（有投递的单）', total, '单')
      + row('日均 · 只看有投递的 ' + activeDays + ' 天', fmtNum(avg), '单/天')
      + row('日均 · 按窗口 ' + days.length + ' 天摊平', fmtNum(avgAll), '单/天')
      + row('峰值日', peakVal, `单 · ${mmdd(peakDay)}`)
      + DROP_FIXED.filter(v => v in agg).map(v => row(v, agg[v], `单 · ${pct(agg[v])}%`, colorOf(v))).join('')
      + (pn && ln ? row('LMS : PUDO 比例', (ln / pn).toFixed(1) + ' : 1', '') : '')
      + `<div class="note2">口径：包裹<b>首次被取件点/网点扫描</b>（FIRST_SCAN）即计入，每单取最早一条，
           所以是<b>包裹数</b>而非事件条数。横轴是<b>投递发生日</b>（≥ 10/29），与上方「每日单量」
           的下单日口径不同——同一张单可能在这张图里出现在下单日之后几天。
           ${beforeWin.length ? `<br/>另有 ${beforeWin.length} 单是 10/29 之前投递的，不计入本图。` : ''}
           ${dirty.length ? ` ${dirty.length} 单投递时间早于抓数窗口（脏数据），已剔除。` : ''}</div>`;
  }
}

/* ---------- 每日 dropoff 点位（location ID） ----------
   数据来源：scraper 从 FIRST_SCAN 事件的 specialOprDesc 里抠出来的
   locationId / locationAlias / locationName / address / zipCode。
   **必须知道的事实**：LMS 的投递事件带 locationId（如 AB4122）；
   PUDO INC 的 FIRST_SCAN 根本没有 specialOprDesc，只能退到「地址|邮编」当点位标识
   （dropSiteKind = 'addr'）。两种来源不能混着当同一个 ID 看，所以下表分开标。
   口径与上方「每日 drop off 量级」一致：吃 ORDERS_DROP（全窗口、跟随「排除测试单」）。 */
let siteDay = '', siteVendor = '';

function drawSites(){
  const daySel = document.getElementById('siteDay');
  const box    = document.getElementById('siteTable');
  const sumBox = document.getElementById('siteSum');
  const noteEl = document.getElementById('siteNote');
  if (!box || !daySel) return;

  const src = (ORDERS_DROP || []).filter(o => o.dropTime);
  const days = [...new Set(src.map(o => isoDay(o.dropTime)))].sort().reverse();

  if (!days.length){
    daySel.innerHTML = '';
    box.innerHTML = '<div class="empty">本窗口还没有投递（FIRST_SCAN）的单</div>';
    if (sumBox) sumBox.innerHTML = '';
    if (noteEl) noteEl.innerHTML = '';
    return;
  }
  if (!siteDay || !days.includes(siteDay)) siteDay = days[0];

  /* 日期下拉带当天单量。只在日期集合变化时重建，否则每次重绘都会重置选中项 */
  const sig = days.join(',');
  if (daySel.dataset.sig !== sig){
    daySel.innerHTML = days.map(d => {
      const n = src.filter(o => isoDay(o.dropTime) === d).length;
      return `<option value="${d}">${d}　${n} 单</option>`;
    }).join('');
    daySel.dataset.sig = sig;
  }
  daySel.value = siteDay;

  const dayAll = src.filter(o => isoDay(o.dropTime) === siteDay);
  const noSite = dayAll.filter(o => !o.dropSiteKey && (!siteVendor || o.dropVendor === siteVendor));
  const rows = dayAll.filter(o => o.dropSiteKey && (!siteVendor || o.dropVendor === siteVendor));

  /* 按「投递方 + 点位键」聚合：同一地址被两家共用时不会被合成一行 */
  const agg = new Map();
  rows.forEach(o => {
    const k = (o.dropVendor || '—') + '|' + o.dropSiteKey;
    let r = agg.get(k);
    if (!r){
      r = { vendor: o.dropVendor || '—', id: o.dropLocationId || '',
            alias: o.dropLocationAlias || '', name: o.dropLocationName || '',
            addr: o.dropLocationAddr || '', city: o.dropLocationCity || '',
            state: o.dropLocationState || '', zip: o.dropLocationZip || '', n: 0 };
      agg.set(k, r);
    }
    r.n++;
    /* 同一点位有的单带名字、有的没带，取第一个非空值补全 */
    r.id    = r.id    || o.dropLocationId   || '';
    r.alias = r.alias || o.dropLocationAlias|| '';
    r.name  = r.name  || o.dropLocationName || '';
    r.addr  = r.addr  || o.dropLocationAddr || '';
    r.city  = r.city  || o.dropLocationCity || '';
    r.state = r.state || o.dropLocationState|| '';
    r.zip   = r.zip   || o.dropLocationZip  || '';
  });
  const list = [...agg.values()].sort((a,b)=> b.n - a.n || a.vendor.localeCompare(b.vendor));
  const total = rows.length;
  const cntOf = v => list.filter(r => r.vendor === v);
  const sumOf = v => cntOf(v).reduce((a,r)=>a+r.n,0);

  if (sumBox){
    const box1 = (label, val, unit, color) => `
      <div class="box"><div class="k">${color?`<i style="background:${color}"></i>`:''}${label}</div>
      <div class="v">${val}${unit?`<small>${unit}</small>`:''}</div></div>`;
    sumBox.innerHTML =
        box1('当日投递单量', total, '单')
      + box1('当日点位数', list.length, '个')
      + box1('LMS', sumOf('LMS'), `单 · ${cntOf('LMS').length} 个点`, DCOLOR_FIXED['LMS'])
      + box1('PUDO INC', sumOf('PUDO INC'), `单 · ${cntOf('PUDO INC').length} 个点`, DCOLOR_FIXED['PUDO INC'])
      + (noSite.length ? box1('无点位信息', noSite.length, '单') : '');
  }
  const cntEl = document.getElementById('siteCount');
  if (cntEl) cntEl.textContent = `${list.length} 个点位 · ${total} 单`;

  if (!list.length){
    box.innerHTML = '<div class="empty">该日没有匹配的点位</div>';
  } else {
    const pct = n => total ? (n / total * 100) : 0;
    const cls = v => v === 'LMS' ? 'lms' : (v === 'PUDO INC' ? 'pup' : '');
    /* PUDO 的地址兜底值本身是「街道, 城市, 州 邮编」，城市已单列一栏，
       这里把地址里重复的城市名去掉，避免同一行出现两遍 */
    const stripCity = (addr, city) => {
      if (!addr || !city) return addr;
      const esc = city.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      return addr.replace(new RegExp(',?\\s*' + esc + '\\b', 'gi'), '')
                .replace(/,\s*,/g, ',').replace(/\s{2,}/g, ' ')
                .trim().replace(/^,|,$/g, '');
    };
    const body = list.map(r => {
      const color = DCOLOR_FIXED[r.vendor] || '#9aa3af';
      const idCell = r.id
        ? `<span class="vcode ${cls(r.vendor)}">${r.id}</span>`
          + (r.alias && r.alias !== r.id
              ? `<span class="vcode alias" style="margin-left:5px" title="点位别名">${r.alias}</span>` : '')
        : `<span class="vcode alias" title="PUDO INC 的投递事件不带 locationId，用地址聚合">addr</span>`;
      const addr = stripCity(r.addr, r.city);
      return `<tr>
        <td><span class="vtag"><i style="width:8px;height:8px;border-radius:2px;background:${color};display:inline-block"></i>${r.vendor}</span></td>
        <td class="mono">${idCell}</td>
        <td>${r.name || '<span class="muted">—</span>'}</td>
        <td class="city">${r.city || '<span class="muted">—</span>'}</td>
        <td class="addr">${addr || '<span class="muted">—</span>'}${r.zip && !(addr||'').includes(r.zip) ? ` <span class="muted">${r.zip}</span>`:''}</td>
        <td class="qty">${r.n}</td>
        <td class="pct">${pct(r.n).toFixed(1)}%</td>
      </tr>`;
    }).join('');
    box.innerHTML = `<table>
      <thead><tr><th>投递方</th><th>Location ID</th><th>点位名称</th><th>城市</th><th>地址</th>
        <th style="text-align:right">单量</th><th style="text-align:right">占比</th></tr></thead>
      <tbody>${body}
        <tr class="total"><td>合计</td><td>—</td><td>—</td><td>—</td>
          <td class="addr">${list.length} 个点位${siteVendor?`（仅 ${siteVendor}）`:''}</td>
          <td class="qty">${total}</td><td class="pct">100%</td></tr>
      </tbody></table>`;
  }

  if (noteEl){
    const withId = list.filter(r => r.id).reduce((a,r)=>a+r.n, 0);
    const parts = [
      (siteVendor
        ? `当前只看 <b>${siteVendor}</b>：本日共 ${dayAll.length} 单有投递扫描，匹配 <b>${total} 单</b>能定位到点位（见上表），`
        : `本日 <b>${dayAll.length} 单</b>有投递扫描：<b>${total} 单</b>能定位到点位（见上表），`)
      + `<b>${withId} 单</b>带官方 locationId，<b>${total-withId} 单</b>是地址兜底（<span class="vcode alias">addr</span>）。`
    ];
    if (noSite.length)
      parts.push(`另有 <b>${noSite.length} 单</b>投递事件里既没有 locationId 也没有地址，无法归到点位（上游没填 specialOprDesc / oprDistrict）。`);
    if (!total && siteVendor)
      parts.push(`该投递方本日没有投递点位 —— 把「投递方」切回<b>全部</b>就能看到另外的单。`);
    parts.push(`ID 取自投递事件（FIRST_SCAN）的 <code>specialOprDesc.locationId</code>：LMS 有值；PUDO INC 不带这个字段，所以 PUDO 的点位是按<b>地址</b>聚合的 —— 同一家店换地址写法会被算成两个点，跨 vendor 也不要直接比点位数。`);
    noteEl.innerHTML = parts.join('<br/>');
  }
}

/* ---------- 各段时效 ---------- */
function drawDur(){
  const rows = [
    { label:'创建 → A-SCAN',  k:'hCreateToAScan', color:'#c3cad6' },
    { label:'A-SCAN → 出库',  k:'hAScanToRdc',    color:'#c3cad6' },
    { label:'出库 → SPX',     k:'hRdcToSpx',      color:'#2f6bff' },
    { label:'A-SCAN → SPX',   k:'hAScanToSpx',    color:'#8fb0ff' },
    { label:'创建 → 最新节点', k:'hCreateToLatest', color:'#e2e6ec' },
  ].map(r => ({ ...r, val: avg(ORDERS.map(o=>o[r.k])), n: cnt(r.k) }));

  const valid = rows.filter(r=>r.val!==null);
  const box = document.getElementById('chartDur');
  if (!valid.length) { box.innerHTML='<div class="empty">暂无可计算的时效区间</div>'; return; }

  const W=430, rowH=42, lblW=112, padT=8;
  const H = padT*2 + valid.length*rowH;
  const maxV = Math.max(...valid.map(r=>r.val), 1);
  const barMax = W - lblW - 66;

  let svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}">`;
  valid.forEach((r,i)=>{
    const y = padT + i*rowH;
    const bw = Math.max(r.val/maxV*barMax, 2);
    svg += `<text x="0" y="${y+16}" font-size="11.5" fill="#1f2329">${r.label}</text>`;
    svg += `<rect x="${lblW}" y="${y+4}" width="${bw}" height="17" rx="4" fill="${r.color}"/>`;
    svg += `<text x="${lblW+bw+8}" y="${y+17}" font-size="11.5" font-weight="600" fill="#1f2329">${fmtNum(r.val)}h</text>`;
    svg += `<text x="${lblW}" y="${y+34}" font-size="10.5" fill="#8a919b">${r.n} 单</text>`;
  });
  svg += `</svg>`;
  box.innerHTML = svg;
}

/* ---------- PUDO vs LMS ---------- */
function drawVendor(){
  const box = document.getElementById('chartVendor');
  const has = x => x !== null && x !== undefined;
  /* 口径：SPX 提取 = SCAUS002（司机提取）或 SCN005（始发中心处理），任一命中即算，
     取最早一条；hRdcToSpx 已经是这个口径，这里不再分严格/宽口径两套。 */
  const codes = (codeOf(region==='ALL'?'US':region).spx || 'SCAUS002') + ' / SCN005';
  const rows = VENDORS.map(v=>{
    /* 分母只算「已出库」的单：这张卡讲的是出库之后的交接 */
    const out = ORDERS.filter(o=>o.vendor===v && o.rdcOutTime);
    return { v, color: vendorColor(v),
             val: avg(out.map(o=>o.hRdcToSpx)),
             n: out.filter(o=>has(o.hRdcToSpx)).length,
             total: out.length };
  /* 只要有单出库就画这一行，否则「已出库、尚未提取」的 RDC 会整行消失 */
  }).filter(r => r.total > 0);

  if (!rows.length) { box.innerHTML='<div class="empty">暂无可对比数据（还没有出库的单）</div>'; return; }

  const W=430, rowH=52, lblW=86, padT=10;
  const H = padT*2 + rows.length*rowH;
  const maxV = Math.max(...rows.map(r=>r.val||0), 1);
  const barMax = W - lblW - 76;

  let svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}">`;
  rows.forEach((r,i)=>{
    const y = padT + i*rowH;
    const empty = r.val === null;
    const bw = empty ? 3 : Math.max(r.val/maxV*barMax, 3);
    svg += `<text x="0" y="${y+18}" font-size="12" fill="#1f2329" font-weight="600">${r.v}</text>`;
    svg += `<rect x="${lblW}" y="${y+6}" width="${bw}" height="19" rx="4"
             fill="${empty ? '#e5e7eb' : r.color}"/>`;
    svg += `<text x="${lblW+bw+8}" y="${y+20}" font-size="12" font-weight="600"
             fill="${empty ? '#9aa0a8' : '#1f2329'}">${empty ? '—' : fmtNum(r.val)+'h'}</text>`;
    svg += `<text x="${lblW}" y="${y+40}" font-size="10.5" fill="#8a919b">${r.n} / ${r.total} 单已交 SPX</text>`;
  });
  svg += `</svg>`;

  /* 空条不是漏数，要说清楚为什么 */
  const emptyRows = rows.filter(r=>r.val === null);
  const tips = [`<b>SPX 提取</b> = <code>${codes}</code> 任一命中即算，取最早一条。`];
  if (emptyRows.length) {
    tips.push(emptyRows.map(r=>`<b>${r.v}</b> 已出库 ${r.total} 单，出库后还没有上述任一事件`)
            + '，均值为空 —— 不是漏数，是还没有可算的样本。');
  }
  svg += `<div style="margin-top:6px;font-size:10.5px;line-height:1.6;color:#8a919b">`
       + tips.join('<br>') + `</div>`;
  box.innerHTML = svg;
}

/* ---------- 时长分布 ---------- */
function drawBins(){
  const bins = [
    { label:'≤24h',      test:v=>v<=24 },
    { label:'24–48h',    test:v=>v>24 && v<=48 },
    { label:'48–72h',    test:v=>v>48 && v<=72 },
    { label:'>72h',      test:v=>v>72 },
  ].map(b=>({ ...b, n: ORDERS.filter(o=>o.hRdcToSpx!==null && o.hRdcToSpx!==undefined && b.test(o.hRdcToSpx)).length }));

  const total = bins.reduce((a,b)=>a+b.n,0);
  const box = document.getElementById('chartBins');
  const noteEl = document.getElementById('binsNote');
  /* 口径备注：图里只覆盖「出库 + 提取」两段都齐的单，剩下的还没提取，别把分档之和当总单量 */
  const outCnt = ORDERS.filter(o => o.rdcOutTime).length;
  const waitCnt = Math.max(0, outCnt - total);
  if (noteEl) {
    noteEl.innerHTML = total
      ? `本窗口<b>已出库 ${outCnt} 单</b>：其中 <b>${total} 单</b>已被 SPX 提取（上图统计），`
        + `<b>${waitCnt} 单</b>已出库但还没提取 —— 这 ${waitCnt} 单算不出时长，不在图里。`
      : `本窗口${outCnt ? `已出库 <b>${outCnt} 单</b>，但都还没被 SPX 提取` : '还没有单出库'}，`
        + '所以分布为空 —— 等提取事件回来才有数。';
  }
  if (!total) { box.innerHTML='<div class="empty">暂无已完成「出库 → SPX」的单</div>'; return; }

  const W=430, rowH=44, lblW=64, padT=10;
  const H = padT*2 + bins.length*rowH;
  const maxV = Math.max(...bins.map(b=>b.n), 1);
  const barMax = W - lblW - 66;
  const colors = ['#12a150','#0e9f9f','#d97706','#d62c2c'];

  let svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}">`;
  bins.forEach((b,i)=>{
    const y = padT + i*rowH;
    const bw = Math.max(b.n/maxV*barMax, b.n?3:0);
    svg += `<text x="0" y="${y+17}" font-size="11.5" fill="#1f2329">${b.label}</text>`;
    if (b.n) svg += `<rect x="${lblW}" y="${y+5}" width="${bw}" height="17" rx="4" fill="${colors[i]}"/>`;
    svg += `<text x="${lblW+bw+8}" y="${y+18}" font-size="11.5" font-weight="600" fill="#1f2329">${b.n} 单 · ${Math.round(b.n/total*100)}%</text>`;
  });
  svg += `</svg>`;
  box.innerHTML = svg;
}

/* ---------- 明细表 ---------- */
const COLS = [
  {key:'ocOrderNo',      label:'OC 单号',   cls:'mono'},
  {key:'source',         label:'来源',      type:'source'},
  {key:'vendor',         label:'RDC',       type:'vendor'},
  {key:'dropVendor',     label:'投递方',    type:'drop'},
  {key:'destState',      label:'目的州'},
  {key:'consignee',      label:'收件人'},
  {key:'orderCreateTime',label:'订单创建'},
  {key:'labelTime',      label:'面单生成'},
  {key:'aScanTime',      label:'A-SCAN'},
  {key:'rdcOutTime',     label:'RDC 出库'},
  {key:'spxPickupTime',  label:'SPX 提取'},
  {key:'dropTime',       label:'投递（drop off）'},
  {key:'deliveredTime',  label:'妥投'},
  {key:'hRdcToSpx',      label:'出库→SPX',  type:'hour'},
  {key:'latestCode',     label:'最新节点',  cls:'mono'},
  {key:'status',         label:'状态',      type:'status'},
];
let sortKey='orderCreateTime', sortAsc=false, query='', vendorFilter='', dropFilter='', statusFilter='', onlyLms=false;
let dateField='orderCreateTime', dateVal='';
/* LMS 同供单：建单 OCUS001 与投递 DOUS002/DOUD002 的 vendorCode 都是 LM */
const lmsOf = o => LMSSAME[o.lastMileWaybill] || LMSSAME[o.ocOrderNo] || null;
/* 订单来源：抓数时按 serviceProductCode 写进 source；
   老快照没有该字段时用 product（就是 serviceProductCode）兜底，避免显示成空 */
const SRC_CODE = { USDOMS2D:'eBay', UDDOMS2D:'Depop' };
const srcOf = o => o.source
  || SRC_CODE[String(o.product||'').toUpperCase()]
  || (o.product ? o.product : '—');
/* Depop 有自己的对外轨迹页，单号直接拼在 number= 后面。
   域名由 OC_DEPOP_TRACK 注入（仓库里不写死），没配就整条外链不渲染。 */
const DEPOP_TRACK_BASE = __DEPOP_TRACK_BASE__;
const DEPOP_TRACK = tn => DEPOP_TRACK_BASE ? DEPOP_TRACK_BASE + encodeURIComponent(tn || '') : '';
const DATE_LABEL = {orderCreateTime:'建单日', dropTime:'投递日', deliveredTime:'妥投日'};

document.getElementById('thead').innerHTML = COLS.map(c =>
  `<th data-key="${c.key}">${c.label}<span class="arrow"></span></th>`).join('');

document.querySelectorAll('#thead th').forEach(th=>{
  th.addEventListener('click', ()=>{
    const k = th.dataset.key;
    if (sortKey === k) sortAsc = !sortAsc; else { sortKey = k; sortAsc = true; }
    renderTable();
  });
});

function renderTable(){
  let rows = ORDERS.slice();
  if (query) {
    const q = query.toLowerCase();
    rows = rows.filter(o => (o.ocOrderNo||'').toLowerCase().includes(q)
      || (o.referenceNumber||'').toLowerCase().includes(q)
      || (o.lastMileWaybill||'').toLowerCase().includes(q)
      || (o.consignee||'').toLowerCase().includes(q)
      || (o.destState||'').toLowerCase().includes(q)
      || (o.product||'').toLowerCase().includes(q)
      || srcOf(o).toLowerCase().includes(q));
  }
  if (vendorFilter) rows = rows.filter(o => (o.vendor || '—') === vendorFilter);
  if (dropFilter) rows = rows.filter(o => (o.dropVendor || '未投递') === dropFilter);
  if (statusFilter) rows = rows.filter(o => o.status === statusFilter);
  if (onlyLms) rows = rows.filter(o => lmsOf(o));
  /* 从上方异常卡片点进来：只留命中该规则的单，并限定在汇总窗口内，
     这样表里行数跟卡片上的数字严格一致（明细表本身能看到更早的单） */
  if (alertKey) {
    const _a = ALERTS.find(x => x.key === alertKey);
    if (_a) rows = rows.filter(o => (!KPI_START || dayOf(o) >= KPI_START) && _a.pred(o));
  }
  /* 日期筛选：dropTime 是「投递日」，orderCreateTime 是「建单日」，deliveredTime 是「妥投日」
     ——三列混用是之前「投递/妥投的单在明细里找不到」的根因，这里显式选维度 */
  if (dateVal) rows = rows.filter(o => isoDay(o[dateField]) === dateVal);

  rows.sort((a,b)=>{
    const pick = o => sortKey === 'source' ? srcOf(o) : o[sortKey];
    const x = pick(a), y = pick(b);
    if (x===y) return 0;
    if (x===null||x===undefined||x==='') return 1;
    if (y===null||y===undefined||y==='') return -1;
    return (x>y?1:-1) * (sortAsc?1:-1);
  });

  document.querySelectorAll('#thead th .arrow').forEach(a=>a.textContent='');
  const cur = document.querySelector(`#thead th[data-key="${sortKey}"] .arrow`);
  if (cur) cur.textContent = sortAsc ? '▲' : '▼';

  document.getElementById('tbody').innerHTML = rows.map(o=>{
    const cells = COLS.map(c=>{
      const v = o[c.key];
      if (c.type === 'source') {
        /* serviceProductCode：USDOMS2D = eBay，UDDOMS2D = Depop；未登记的码原样显示 */
        const s = srcOf(o);
        const cls = s === 'eBay' ? 'ebay' : (s === 'Depop' ? 'depop' : 'other');
        const code = String(o.product || '').toUpperCase();
        return `<td><span class="src ${cls}" title="serviceProductCode = ${code || '—'} → ${s}">${s}</span></td>`;
      }
      if (c.type === 'vendor') {
        const code = o.vendorCode || '';
        const cls = code === 'PUP' ? 'pup' : (code === 'LMS' ? 'lms' : '');
        const rawTip = (o.vendorCodeRaw && o.vendorCodeRaw !== code)
          ? `，接口原值 ${o.vendorCodeRaw}` : '';
        const vecTip = o.vendorEventCode ? `，vendorEventCode = ${o.vendorEventCode}` : '';
        const badge = code
          ? `<span class="vcode ${cls}" title="出库事件的 vendorCode = ${code}${rawTip}${vecTip}">${code}</span>`
          : '';
        /* 替代码：不是登记表主码，但实测在用（如 11/01 起 LMS 改报 shipment.sorted） */
        const alias = (o.outCodeKind === 'alias' && o.vendorEventCode)
          ? `<span class="vcode alias" title="替代码 ${o.vendorEventCode}：非登记表主码，实测在用">替</span>`
          : '';
        const unreg = (o.outCodeKind === '' && o.vendorEventCode)
          ? `<span class="vcode" style="color:#d62c2c;background:#fdecec;border-color:#f5c2c2"
                   title="未登记码 ${o.vendorEventCode}：登记表里没有，需补充映射">?</span>`
          : '';
        const mix = o.outMixed
          ? `<span class="vcode" style="color:#9a6a00;background:#fff8e6;border-color:#f2e2b8"
               title="同一单被两家各报了一次出库，按更早那条定归属">两家</span>` : '';
        return `<td><span class="vtag"><i class="dot" style="background:${vendorColor(v)}"></i>${v||'—'}${badge}${alias}${unreg}${mix}</span></td>`;
      }
      if (c.type === 'drop') {
        /* 投递方 = 客户把包裹交给谁（FIRST_SCAN 事件的 vendor）。
           与出库方不一致时标一下：投在 PUDO 却从 LMS 出库，货权不在同一侧。 */
        const v = o.dropVendor || '';
        const body = v
          ? `<span class="vtag"><i class="dot" style="background:${dropColor(v)}"></i>${v}</span>`
          : '<span class="muted">未投递</span>';
        const diff = (v && o.vendor && o.vendor !== '—' && v !== o.vendor)
          ? `<span class="vcode" style="color:#9a6a00;background:#fff8e6;border-color:#f2e2b8"
                   title="投递方 ${v} ≠ 出库方 ${o.vendor}">异</span>` : '';
        const tip = v
          ? `title="投递事件 ${o.dropEventCode||''} · ${o.dropTime||''}"`
          : 'title="还没有投递（收件扫描）事件"';
        /* isEventCode = FIRST_SCAN 的显式标记：只有一个小圆点太容易被忽略，
           这里给一条「首扫」确认徽标。老快照没有 dropIsEventCode 字段时，
           退回「有投递时间就算」——抓数时的投递判定本来就以 FIRST_SCAN 为主判据 */
        const hasFsField = o.dropIsEventCode !== undefined && o.dropIsEventCode !== null
                           && o.dropIsEventCode !== '';
        const isFs = hasFsField ? !!o.dropIsFirstScan : !!o.dropTime;
        const fs = isFs
          ? `<span class="xflag fs" title="isEventCode = FIRST_SCAN（收件扫描）· 事件码 ${o.dropEventCode||'—'}`
            + ` · 投递方 ${v}${o.dropTime ? ' · ' + o.dropTime : ''}">首扫</span>`
          : '';
        return `<td><span ${tip}>${body}${fs}${diff}</span></td>`;
      }
      if (c.type === 'status') {
        return `<td><span class="tag ${statusClass(v)}">${v}</span></td>`;
      }
      if (c.type === 'hour') {
        if (v===null||v===undefined) return `<td class="muted">—</td>`;
        const bad = v > 48;
        return `<td class="${bad?'dur-bad':''}">${fmtNum(v)}h</td>`;
      }
      if (c.key === 'dropTime') {
        if (!v) return `<td class="muted" title="还没有 FIRST_SCAN（客户交件到网点）事件">未投递</td>`;
        return `<td title="FIRST_SCAN ${o.dropEventCode||''} · 投递方 ${o.dropVendor||'—'}">${tzStamp(v)}</td>`;
      }
      if (c.key === 'spxPickupTime') {
        if (!v) return `<td class="muted" title="出库后还没有 SCAUS002 / SCN005 事件">—</td>`;
        return `<td title="SPX 提取 ${o.spxPickupCode||''}（SCAUS002 或 SCN005 任一命中）">${tzStamp(v)}</td>`;
      }
      /* 时间列跟着时区页签走；其它列原样输出 */
      const shown = (typeof v === 'string' && TS_RE.test(v)) ? tzStamp(v) : v;
      const text = (v===null||v===undefined||v==='') ? '<span class="muted">—</span>' : shown;
      if (c.key === 'ocOrderNo') {
        const x = CROSS[v];
        const flag = x
          ? `<span class="xflag" title="PUDO 点投递 ${x.dropAt} → ${x.pickLabel}（LMS）${x.pickAt}，间隔 ${x.delayH}h">串场</span>`
          : '';
        const d = DROPOFF[v];
        const dc = (d && !d.isTest && d.kind !== 'resumed')
          ? `<span class="xflag" style="background:#fdecec;color:#b4232a;border-color:#f5c7c7"
               title="已投递却被取消：${d.kindLabel||''}（投递 ${d.dropAt} → 取消 ${d.cancelAt}）">取消异常</span>`
          : '';
        const ls = lmsOf(o);
        const lsFlag = ls
          ? `<span class="xflag lms" title="建单 OCUS001（LMS）${ls.createAt} → 投递 ${ls.dropCode}（LMS）${ls.dropAt}，间隔 ${ls.delayH}h">LMS 同供</span>`
          : '';
        /* Depop 单：单号后挂 ↗，直达 Depop 官方轨迹页（不触发抽屉） */
        const dpHref = (v && srcOf(o) === 'Depop') ? DEPOP_TRACK(v) : '';
        const dpLink = dpHref
          ? `<a class="xlink" href="${dpHref}" target="_blank" rel="noopener noreferrer"
                title="在 Depop 官方轨迹页查看 ${v}"
                onclick="event.stopPropagation()">↗</a>`
          : '';
        return `<td class="${c.cls||''}">${text}${dpLink}${lsFlag}${flag}${dc}</td>`;
      }
      return `<td class="${c.cls||''}">${text}</td>`;
    }).join('');
    const tn = (o.lastMileWaybill || o.ocOrderNo || '').replace(/"/g,'');
    return `<tr class="row" data-tn="${tn}" data-region="${o.region||''}">${cells}</tr>`;
  }).join('');

  if (!rows.length) {
    document.getElementById('tbody').innerHTML = `<tr><td colspan="${COLS.length}"><div class="empty">没有匹配的订单</div></td></tr>`;
  }
  document.getElementById('count').textContent =
    `显示 ${rows.length} / ${ORDERS.length} 单（明细自 ${mmdd(DETAIL_START)} 起`
    + (dateVal ? `，${DATE_LABEL[dateField] || ''} = ${mmdd(dateVal)}` : '')
    + '）';
  const dClear = document.getElementById('dateClear');
  if (dClear) dClear.style.display = dateVal ? '' : 'none';
  /* 勾选项旁边的命中数：跟随「排除测试单」与区域，不随搜索/筛选变化 */
  const lmsCnt = document.getElementById('lmsCnt');
  if (lmsCnt) lmsCnt.textContent = `（${ORDERS.filter(o => lmsOf(o)).length}）`;
}

/* 下拉选项 */
const vSel = document.getElementById('vendorFilter');
const dSel = document.getElementById('dropFilter');
const sSel = document.getElementById('statusFilter');
function rebuildFilters(){
  vSel.innerHTML = '<option value="">全部 RDC</option>';
  /* 登记的 vendor 一律列出来（PUDO INC / LMS 是固定的两家），本窗口 0 单也保留，
     否则会出现「PUDO INC 从下拉里消失」这种让人以为逻辑漏了的情况 */
  const registered = uniq(outNodeRows().map(n => n.vendor).filter(Boolean));
  const inData = uniq(ORDERS.map(o => o.vendor || '—'));
  const all = [...registered, ...inData.filter(v => !registered.includes(v))];
  all.forEach(v=>{
    const n = ORDERS.filter(o => (o.vendor || '—') === v).length;
    const dim = n === 0 ? ' style="color:#9aa0a8"' : '';
    const text = v === '—' ? '无 RDC 归属' : v;
    vSel.insertAdjacentHTML('beforeend',
      `<option value="${v}"${dim}>${text}（${n}）</option>`);
  });

  /* 投递方下拉：登记的两家固定在前面，未投递单独一项 */
  dSel.innerHTML = '<option value="">全部投递方</option>';
  const dropReg = uniq((META.dropNodes
      ? Object.values(META.dropNodes).reduce((a,k)=>a.concat(k||[]), [])
      : []).map(n => n.vendor).filter(Boolean));
  const dropIn = uniq(ORDERS.map(o => o.dropVendor || '未投递'));
  const dropAll = [...dropReg, ...dropIn.filter(v => !dropReg.includes(v))];
  dropAll.forEach(v=>{
    const n = ORDERS.filter(o => (o.dropVendor || '未投递') === v).length;
    const dim = n === 0 ? ' style="color:#9aa0a8"' : '';
    dSel.insertAdjacentHTML('beforeend',
      `<option value="${v}"${dim}>${v}（${n}）</option>`);
  });

  /* 来源不再做下拉筛选：页面顶部的「渠道」页签已按 eBay / Depop 整体切分 */
  sSel.innerHTML = '<option value="">全部状态</option>';
  uniq(ORDERS.map(o=>o.status)).forEach(s=>{ sSel.insertAdjacentHTML('beforeend', `<option value="${s}">${s}</option>`); });
  vendorFilter = ''; dropFilter = ''; statusFilter = '';
  dateField = 'orderCreateTime'; dateVal = '';
  const dEl = document.getElementById('dateField'), vEl = document.getElementById('dateVal');
  if (dEl) dEl.value = dateField;
  if (vEl) vEl.value = '';
}

document.getElementById('search').addEventListener('input', e=>{ query=e.target.value.trim(); renderTable(); });
vSel.addEventListener('change', e=>{ vendorFilter=e.target.value; renderTable(); });
dSel.addEventListener('change', e=>{ dropFilter=e.target.value; renderTable(); });
sSel.addEventListener('change', e=>{ statusFilter=e.target.value; renderTable(); });
/* 每日 dropoff 点位卡：日期 / 投递方切换只重绘这张卡，不动其它图表 */
const siteDayEl = document.getElementById('siteDay');
if (siteDayEl) siteDayEl.addEventListener('change', e=>{ siteDay = e.target.value; drawSites(); });
const siteVendorEl = document.getElementById('siteVendor');
if (siteVendorEl) siteVendorEl.addEventListener('change', e=>{ siteVendor = e.target.value; drawSites(); });
document.getElementById('dateField').addEventListener('change', e=>{
  dateField = e.target.value || 'orderCreateTime'; renderTable();
});
document.getElementById('dateVal').addEventListener('change', e=>{
  dateVal = e.target.value || ''; renderTable();
});
document.getElementById('dateClear').addEventListener('click', ()=>{
  dateVal = ''; document.getElementById('dateVal').value = ''; renderTable();
});
document.getElementById('excludeTest').addEventListener('change', e=>{
  excludeTest = e.target.checked;
  setRegion(region);
});
document.getElementById('onlyLms').addEventListener('change', e=>{
  onlyLms = e.target.checked;
  renderTable();
});

/* ---------- 外壳初始化：脚注 / 区域 tab ----------
   这几行原本是顶层直接执行的，但加密部署版里 ALL / META 在解锁前是空的，
   顶层执行会读到 null 而崩掉。所以统一收进 __initShell()，由 __start() 调用。 */
var present = null, TABS = [], region = 'ALL';

function __initShell(){
  document.getElementById('foot').textContent =
    (META.demo ? '当前为示例数据，仅供看板效果参考。' : `数据来源：${META.source||''}`) + ' · 时间为北京时间（UTC+8）';

  /* ---------- 区域 tab：对齐后台右上角 UK | All | US ---------- */
  present = new Set(ALL.map(o=>o.region).filter(Boolean));
  TABS = [
    { key:'UK' }, { key:'ALL' }, { key:'US' }, { key:'AU' },
  ].filter(t => t.key==='ALL' ? present.size > 1 : present.has(t.key));

  region = present.has('US') ? 'US' : (TABS.find(t=>t.key!=='ALL') || {key:'ALL'}).key;
  /* 支持用 ?region=UK / ?region=ALL 直接定位到某个 tab */
  const qRegion = (new URLSearchParams(location.search).get('region') || '').toUpperCase();
  if (qRegion && TABS.some(t=>t.key===qRegion)) region = qRegion;

  document.getElementById('regionTabs').innerHTML =
    TABS.map(t=>`<button data-key="${t.key}">${t.key==='ALL' ? 'All' : t.key}</button>`).join('');
  document.querySelectorAll('#regionTabs button').forEach(b=>{
    b.addEventListener('click', ()=>setRegion(b.dataset.key));
  });
  if (TABS.length <= 1) document.getElementById('regionTabs').style.display = 'none';
}

let excludeTest = true, testCount = 0, totalCount = 0;
/* 两条日期口径：
   汇总（KPI + 所有图表）自 KPI_START 起；明细表自 DETAIL_START 起（看得更早）。
   两个值由 .env 的 OC_KPI_START / OC_DETAIL_START 注入。 */
const KPI_START = '__KPI_START__';
const DETAIL_START = '__DETAIL_START__';
let ORDERS_KPI = [], ORDERS_DETAIL = [], ORDERS_DROP = [], kpiTestCount = 0, kpiTotalCount = 0;
let alertKey = '';   /* 当前选中的异常类型；空 = 未筛选 */
const dayOf = o => isoDay(o.orderCreateTime);
const mmdd = s => (s || '').slice(5).replace('-', '/');

/* 口径基准用「数据时刻」(META.generatedAt)，不用浏览器当前时间：
   看板是离线单文件，明天再打开时若按浏览器时间算，所有单都会虚增十几个小时，
   全变成红的。这里判定的是「截至这份数据时刻」的状态。
   加密部署版解锁前 META 是 null，所以不能在顶层算，收进 __initDataTs()。 */
var DATA_TS = 0;
function __initDataTs(){
  const t = Date.parse(String(META.generatedAt || '').replace(' ', 'T'));
  DATA_TS = isNaN(t) ? Date.now() : t;
}
const hoursSince = ts => {
  if (!ts) return null;
  const t = Date.parse(String(ts).replace(' ', 'T'));
  return isNaN(t) ? null : (DATA_TS - t) / 3600000;
};

/* 每条规则都用「本窗口已观测到的极值」当阈值，不是拍脑袋：
   - 出库→提取：p50 46.6h / p90 53.6h / max 54.5h → 取 72h（越过就直接是破纪录）
   - 到仓→出库：LMS p50 0.2h、max 22.3h；PUDO p50 24.5h、max 68.9h → 分方给阈值
   - 提取→妥投：p50 39.7h / p90 122.1h → 取 120h
   改了阈值记得同步页面上的 hint 文案。 */
const ALERTS = [
  { key:'stuckOut', sev:'high', label:'出库后未提取',
    hint:'出库超 72 小时仍无提取（历史最长 54.5h）',
    pred: o => !!o.rdcOutTime && !o.spxPickupTime && (hoursSince(o.rdcOutTime) || 0) > 72 },
  { key:'warehouse', sev:'high', label:'到仓未出库',
    hint:'压库：PUDO 超 48h / 其他超 24h 未出库',
    pred: o => {
      if (!o.rdcArriveTime || o.rdcOutTime) return false;
      const h = hoursSince(o.rdcArriveTime);
      if (h === null) return false;
      const lim = (o.dropVendor === 'PUDO INC') ? 48 : 24;
      return h > lim;
    } },
  { key:'transit', sev:'mid', label:'提取未妥投',
    hint:'SPX 提取超 120 小时仍未妥投',
    pred: o => !!o.spxPickupTime && !o.deliveredTime && (hoursSince(o.spxPickupTime) || 0) > 120 },
  { key:'nodrop', sev:'low', label:'久未投递',
    hint:'建单超 5 天客户仍未送到 PUDO 点（客户侧）',
    pred: o => !o.aScanTime && (hoursSince(o.orderCreateTime) || 0) > 120 },
  { key:'cross', sev:'mid', label:'串场',
    hint:'投递方与出库方不是同一家',
    pred: o => !!o.dropVendorDiffOut },
  { key:'anomaly', sev:'mid', label:'时序倒挂',
    hint:'建单 / A-SCAN / 出库 / 提取出现负间隔',
    pred: o => !!o.anomaly },
];

/* 卡片计数与明细筛选必须同源：都限定在「汇总窗口 + 当前区域 + 排除测试」 */
function alertSrc(){
  return (ORDERS_KPI || []).filter(o => !KPI_START || dayOf(o) >= KPI_START);
}

function renderAlerts(){
  const box = document.getElementById('alerts');
  if (!box) return;
  const src = alertSrc();
  const counts = {};
  ALERTS.forEach(a => { counts[a.key] = src.filter(a.pred).length; });

  box.innerHTML = ALERTS.map(a => {
    const n = counts[a.key];
    return `<div class="alert sev-${a.sev}${n === 0 ? ' is-zero' : ''}${alertKey === a.key ? ' active' : ''}"
                 data-alert="${a.key}" title="${a.hint} · 点击筛到下方明细">
      <div class="n">${n}<small>单</small></div>
      <div class="t">${a.label}</div>
      <div class="h">${a.hint}</div>
    </div>`;
  }).join('');

  box.querySelectorAll('.alert').forEach(el => {
    el.addEventListener('click', () => {
      const k = el.dataset.alert;
      alertKey = (alertKey === k) ? '' : k;   /* 再点一次取消 */
      if (alertKey) clearTableFilters();       /* 避免旧筛选叠上去，数字对不上 */
      renderAlerts();
      renderTable();
      if (alertKey) {
        document.getElementById('detailCard').scrollIntoView({ behavior:'smooth', block:'start' });
      }
    });
  });

  /* 筛选条 */
  const bar = document.getElementById('alertBar');
  const cur = ALERTS.find(x => x.key === alertKey);
  if (cur) {
    bar.style.display = 'flex';
    document.getElementById('alertName').textContent = '「' + cur.label + '」';
    document.getElementById('alertCount').textContent =
      ' · ' + counts[cur.key] + ' 单（汇总窗口）';
  } else {
    bar.style.display = 'none';
  }
}

/* 点预警卡片时把工具栏上的筛选全部归零，保证「卡片数字 = 表里行数」 */
function clearTableFilters(){
  query = ''; vendorFilter = ''; dropFilter = ''; statusFilter = '';
  onlyLms = false; dateVal = '';
  const $ = id => document.getElementById(id);
  if ($('search')) $('search').value = '';
  if ($('vendorFilter')) $('vendorFilter').value = '';
  if ($('dropFilter')) $('dropFilter').value = '';
  if ($('statusFilter')) $('statusFilter').value = '';
  if ($('dateVal')) $('dateVal').value = '';
  if ($('onlyLms')) $('onlyLms').checked = false;
  if ($('dateClear')) $('dateClear').style.display = 'none';
}

document.getElementById('alertClear').addEventListener('click', () => {
  alertKey = '';
  renderAlerts();
  renderTable();
});

/* 汇总区执行期间把 ORDERS 临时切到汇总口径，函数体不用改；明细表仍走 ORDERS_DETAIL */
function withKpi(fn){
  const saved = ORDERS;
  ORDERS = ORDERS_KPI;
  try { fn(); } finally { ORDERS = saved; }
}

/* ---------- 渠道页签（全部 / eBay / Depop）----------
   eBay 与 Depop 只是 serviceProductCode（Service ID）不同，链路与口径完全一致，
   所以不做两套统计，只在数据源上切一刀：切完 KPI、漏斗、图表、预警、明细全部按该渠道重算。 */
const CHAN_ORDER = ['eBay', 'Depop'];          /* 固定顺序，避免 tab 随单量漂移 */
/* 同 __initShell：渠道清单依赖 ALL，加密版解锁前不能碰，收进函数 */
var chanPresent = [], CHANS = [{ key:'ALL', label:'全部' }], channel = 'ALL';

function __initChans(){
  chanPresent = uniq(ALL.map(o => srcOf(o)).filter(v => v && v !== '—'));
  CHANS = [{ key:'ALL', label:'全部' }].concat(
    CHAN_ORDER.filter(s => chanPresent.includes(s)).map(s => ({ key:s, label:s })),
    chanPresent.filter(s => !CHAN_ORDER.includes(s)).sort().map(s => ({ key:s, label:s }))
  );
  channel = 'ALL';
  /* 支持 ?channel=Depop 直接定位 */
  const qChan = (new URLSearchParams(location.search).get('channel') || '');
  if (qChan && CHANS.some(c => c.key === qChan)) channel = qChan;
}

/* 页签上的数字：按「当前区域的全部数据」算该渠道单量（不跟测试单开关联动，
   否则勾选/取消勾选时数字会跳，用户会以为丢单）。 */
function chanCount(key){
  const inRegion = region === 'ALL' ? ALL : ALL.filter(o => o.region === region);
  return key === 'ALL' ? inRegion.length : inRegion.filter(o => srcOf(o) === key).length;
}

function renderChanTabs(){
  const box = document.getElementById('chanTabs');
  if (!box) return;
  box.innerHTML = CHANS.map(c => {
    const n = chanCount(c.key);
    const dis = n === 0 ? ' style="opacity:.45"' : '';
    return `<button data-ch="${c.key}" class="${channel === c.key ? 'on' : ''}"${dis}>`
         + `${c.label}<span class="nc">${n}</span></button>`;
  }).join('');
  box.querySelectorAll('button').forEach(b => {
    b.addEventListener('click', () => setChannel(b.dataset.ch));
  });

  const scope = document.getElementById('chanScope');
  if (scope) {
    const pool = channel === 'ALL' ? ALL : ALL.filter(o => srcOf(o) === channel);
    const nT = pool.filter(o => o.isTest).length;
    const nR = pool.length - nT;
    scope.innerHTML = channel === 'ALL'
      ? `当前 <b>全部渠道</b>（eBay + Depop 合并统计）· ${pool.length} 单`
      : `当前只看 <b>${channel}</b> · 共 ${pool.length} 单（真实 ${nR}${nT ? ` · 测试 ${nT}` : ''}）`
        + ` · KPI 与预警均按该渠道单独计算`;
  }
}

/* ---------- 时区页签 ---------- */
function renderTzTabs(){
  const box = document.getElementById('tzTabs');
  if (!box) return;
  box.innerHTML = TZ_LIST.map(t =>
    `<button data-tz="${t.key}" class="${tzMode === t.key ? 'on' : ''}">${t.label}</button>`).join('');
  box.querySelectorAll('button').forEach(b => {
    b.addEventListener('click', () => setTzMode(b.dataset.tz));
  });
  const sc = document.getElementById('tzScope');
  if (sc) {
    const cur = TZ_LIST.find(t => t.key === tzMode) || TZ_LIST[0];
    sc.innerHTML = `当前按 <b>${cur.label}时间</b>（${cur.tz}）划分日期`
      + ' · 影响：每日单量 / 投递日统计 / 点位卡日期 / 日期筛选 / 明细时间列'
      + '；时长类指标（各段小时数）不受影响';
  }
}

function setTzMode(key){
  if (key === tzMode || !TZ_LIST.some(t => t.key === key)) return;
  tzMode = key;
  try { localStorage.setItem('oc_tz', key); } catch(e) {}
  /* 日期下拉里的选项集合变了，必须让它重建；表格的日期筛选也清掉，避免指向一个已不存在的日期 */
  siteDay = '';
  const sd = document.getElementById('siteDay');
  if (sd) sd.dataset.sig = '';
  dateVal = '';
  const dv = document.getElementById('dateVal');
  if (dv) dv.value = '';
  setRegion(region);
}

function setChannel(key){
  if (key === channel) return;
  channel = key;
  /* 换渠道等于换数据集，旧的预警筛选一定要清掉：
     否则会出现「卡片写着 0 单、筛选条还挂着『正在显示到仓未出库』」这种自相矛盾的界面 */
  alertKey = '';
  setRegion(region);       /* 渠道变了要按新数据源把所有区块重算一遍 */
}

function setRegion(key){
  region = key;
  const base0 = key === 'ALL' ? ALL.slice() : ALL.filter(o=>o.region===key);
  /* 渠道切口：eBay / Depop 仅 Service ID 不同，数据上就是 source 字段 */
  const base = channel === 'ALL' ? base0 : base0.filter(o => srcOf(o) === channel);
  totalCount = base.length;
  testCount = base.filter(o=>o.isTest).length;

  const kpiBase = base.filter(o => !KPI_START || dayOf(o) >= KPI_START);
  const detailBase = base.filter(o => !DETAIL_START || dayOf(o) >= DETAIL_START);
  kpiTotalCount = kpiBase.length;
  kpiTestCount = kpiBase.filter(o=>o.isTest).length;
  ORDERS_KPI = excludeTest ? kpiBase.filter(o=>!o.isTest) : kpiBase;
  ORDERS_DETAIL = excludeTest ? detailBase.filter(o=>!o.isTest) : detailBase;
  /* drop off 卡片按投递发生日统计，必须吃全窗口，不能跟着 KPI_START 截断 */
  ORDERS_DROP = excludeTest ? base.filter(o=>!o.isTest) : base.slice();
  ORDERS = ORDERS_DETAIL;

  computeVendors(key);
  computeDrops(key);
  rebuildFilters();
  withKpi(()=>{ drawHeader(); drawKpis(); drawDaily(); drawDur(); drawVendor(); drawBins(); });
  renderAlerts();
  drawDropDaily();
  drawSites();
  drawLmsNote();
  renderTable();
  document.querySelectorAll('#regionTabs button').forEach(b=>b.classList.toggle('on', b.dataset.key===key));
  renderChanTabs();   /* 渠道页签的选中态与单量在这里统一刷新 */
  renderTzTabs();     /* 时区页签的选中态同理 */
}

/* 入口包成函数：本地内嵌版在页面底部立刻执行；加密部署版等口令解密后再调 window.__start() */
window.__start = function(){
  __initDataTs();
  __initShell();
  __initChans();
  setRegion(region);
};
if (window.__autoStart !== false) { window.__start(); }
</script>
__EXTRA_JS__
</body>
</html>
"""


TRACE_CSS = r"""
  /* ---- 动态模式：单号追踪抽屉 ---- */
  body.dynamic tbody tr.row{cursor:pointer}
  body.dynamic tbody tr.row:hover{background:#f2f6ff}
  .topbtns{display:flex;gap:8px;justify-content:flex-end;margin-top:8px;flex-wrap:wrap}
  .btn{border:1px solid var(--border);background:#fff;color:var(--text);border-radius:7px;
    padding:5px 12px;font-size:12.5px;cursor:pointer;font-family:inherit;line-height:1.5;
    text-decoration:none;display:inline-block}
  .btn:hover{border-color:#c9d3e6;background:#fafbff}
  .btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
  .btn.primary:hover{background:#255ee0;border-color:#255ee0}
  /* 需要人处理的异常入口：红底 + 数量徽标，轻轻呼吸提示它别被忽略 */
  .btn.alert{background:#fdecec;border-color:#f0b9b9;color:#b4232a;font-weight:600;
    box-shadow:0 0 0 3px rgba(214,44,44,.06);animation:alertpulse 2.4s ease-in-out infinite}
  .btn.alert:hover{background:#fbdada;border-color:#e39c9c}
  .btn.alert .cnt{display:inline-block;background:#d62c2c;color:#fff;border-radius:9px;
    padding:0 6px;margin-left:7px;font-size:11px;line-height:17px;font-weight:600;
    font-variant-numeric:tabular-nums}
  @keyframes alertpulse{
    0%,100%{box-shadow:0 0 0 3px rgba(214,44,44,.06)}
    50%{box-shadow:0 0 0 5px rgba(214,44,44,.13)}}
  @media(prefers-reduced-motion:reduce){.btn.alert{animation:none}}
  .btn:disabled{opacity:.55;cursor:default}
  .ro-tag{display:inline-flex;align-items:center;padding:5px 11px;border-radius:7px;
    font-size:12.5px;color:var(--muted);background:#f0f1f3;border:1px solid var(--border)}
  .dot-live{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;vertical-align:1px}
  .trace-mask{position:fixed;inset:0;background:rgba(20,24,32,.34);display:none;z-index:60}
  .trace-mask.on{display:block}
  .trace-panel{position:fixed;top:0;right:0;bottom:0;width:min(640px,94vw);background:#fff;
    box-shadow:-8px 0 30px rgba(0,0,0,.13);z-index:61;display:none;flex-direction:column}
  .trace-panel.on{display:flex}
  .trace-hd{padding:18px 22px 14px;border-bottom:1px solid var(--border);
    display:flex;align-items:flex-start;justify-content:space-between;gap:14px}
  .trace-hd h3{margin:0;font-size:15px;font-weight:600}
  .trace-hd .tsub{margin-top:5px;font-size:12px;color:var(--muted);line-height:1.6}
  .trace-close{border:0;background:#f2f3f5;border-radius:6px;width:28px;height:28px;
    cursor:pointer;font-size:17px;color:var(--muted);line-height:1;flex:none}
  .trace-close:hover{background:#e8eaed;color:var(--text)}
  .trace-body{padding:18px 22px 30px;overflow:auto;flex:1}
  .seg{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-bottom:18px}
  .seg-item{border:1px solid var(--border);border-radius:8px;padding:10px 12px;background:#fafbfc}
  .seg-item .k{font-size:11.5px;color:var(--muted)}
  .seg-item .v{font-size:17px;font-weight:600;margin-top:3px;font-variant-numeric:tabular-nums}
  .seg-item .v small{font-size:12px;font-weight:400;color:var(--muted);margin-left:2px}
  .tl{position:relative;padding-left:22px}
  .tl::before{content:'';position:absolute;left:5px;top:8px;bottom:8px;width:1px;background:#e3e6ea}
  .tl-item{position:relative;padding:0 0 18px}
  .tl-item:last-child{padding-bottom:0}
  .tl-item::before{content:'';position:absolute;left:-21px;top:4px;width:9px;height:9px;
    border-radius:50%;background:#c9ced6;border:2px solid #fff;box-shadow:0 0 0 1px #d5d9df}
  .tl-item.key::before{background:var(--accent);box-shadow:0 0 0 1px var(--accent)}
  .tl-item.spx::before{background:var(--ok);box-shadow:0 0 0 1px var(--ok)}
  .tl-time{font-size:12.5px;font-variant-numeric:tabular-nums;font-weight:500}
  .tl-delta{font-size:11.5px;color:var(--muted);margin-left:8px;font-weight:400}
  .tl-code{display:inline-block;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11.5px;
    background:#f2f4f7;border-radius:4px;padding:1px 6px;margin-right:7px;color:#3d4654}
  .tl-desc{font-size:12.5px;color:#4a5260;margin-top:4px;line-height:1.55}
  .tl-meta{font-size:11.5px;color:var(--muted);margin-top:3px}
  /* ---- 原始事件列表：接口字段原样展示，没有换算值 ---- */
  .raw{display:flex;flex-direction:column;gap:0}
  .raw-item{display:flex;gap:12px;padding:11px 0;border-bottom:1px solid #eef0f3}
  .raw-item:last-child{border-bottom:0}
  .raw-l{flex:none;width:168px;font-variant-numeric:tabular-nums}
  .raw-t{font-size:12px;font-weight:500;color:#2b3240;word-break:break-all;line-height:1.5}
  .raw-bj{font-size:11px;color:var(--muted);margin-top:2px}
  .raw-r{flex:1;min-width:0}
  .raw-line{font-size:12px;color:#4a5260;line-height:1.6;margin-bottom:2px;word-break:break-word}
  .raw-k{display:inline-block;font-size:10.5px;color:#8a93a0;background:#f4f5f7;
    border-radius:3px;padding:0 4px;margin-right:5px}
  .raw-desc{color:#2b3240}
  .raw-json{margin-top:4px}
  .raw-json summary{font-size:11px;color:#7b8494;cursor:pointer;outline:none}
  .raw-json pre{margin:6px 0 0;padding:9px 11px;background:#f7f8fa;border:1px solid #eceef1;
    border-radius:6px;font-size:11px;line-height:1.5;max-height:260px;overflow:auto;
    font-family:ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-all}
  .trace-note{background:#fff8ed;border:1px solid #f5dcb8;color:#96631a;border-radius:7px;
    padding:9px 12px;font-size:12px;line-height:1.65;margin-bottom:16px}
  .trace-err{background:#fdecec;border:1px solid #f6c9c9;color:#b02a2a;border-radius:7px;
    padding:9px 12px;font-size:12.5px;line-height:1.6;margin-bottom:16px}
  .loading{color:var(--muted);font-size:13px;padding:24px 0;text-align:center}
"""

TRACE_HTML = r"""
<div class="trace-mask" id="traceMask"></div>
<aside class="trace-panel" id="tracePanel" aria-hidden="true">
  <div class="trace-hd">
    <div>
      <h3 id="traceTitle">包裹轨迹</h3>
      <div class="tsub" id="traceSub"></div>
      <div class="tlink" id="traceLink"></div>
    </div>
    <button class="trace-close" id="traceClose" title="关闭">×</button>
  </div>
  <div class="trace-body" id="traceBody"></div>
</aside>
"""

TRACE_JS = r"""
<script>
(function(){
  const DYNAMIC = __DYNAMIC__;
  const READONLY = __READONLY__;
  if (!DYNAMIC) return;
  document.body.classList.add('dynamic');

  const genLine = document.getElementById('genline');
  if (genLine) {
    const bar = document.createElement('div');
    bar.className = 'topbtns';
    /* 要人处理的口子放最前面并高亮：已投递却被取消的订单（红底 + 数量） */
    const dc = META.dropoffCancel || {};
    const todo = dc.todo || 0;
    const alertBtn = todo
      ? `<a class="btn alert" href="/dropoff-cancel" target="_blank" rel="noopener"
            title="已投递（FIRST_SCAN）却被取消（SHIPCCUS001）的订单：时序倒挂 + 取消后无后续">drop off 取消异常<span class="cnt">${todo}</span></a>`
      : `<a class="btn" href="/dropoff-cancel" target="_blank" rel="noopener"
            title="已投递却被取消的订单清单（当前窗口没有要跟进的单）">drop off 取消异常</a>`;
    bar.innerHTML = alertBtn +
      '<a class="btn" href="/stuck" target="_blank" rel="noopener" ' +
      'title="全部 SHIPCCUS001 取消节点清单（含从未交寄的取消单）">SHIPCCUS001 取消清单</a>' +
      (READONLY
        ? '<span class="ro-tag">只读模式</span>'
        : '<button class="btn" id="btnCookie">更新 Cookie</button>' +
          '<button class="btn primary" id="btnRefresh">刷新数据</button>');
    genLine.parentNode.appendChild(bar);
  }

  const mask = document.getElementById('traceMask');
  const panel = document.getElementById('tracePanel');
  const body = document.getElementById('traceBody');
  const titleEl = document.getElementById('traceTitle');
  const subEl = document.getElementById('traceSub');
  const linkEl = document.getElementById('traceLink');

  const fmtH = v => (v === null || v === undefined) ? '—' : (Math.round(v*10)/10).toFixed(1);

  /* Depop 单在抽屉头部给一个官方轨迹页入口；非 Depop 清空，避免上一单残留 */
  function setExtLink(o, tn){
    if (!linkEl) return;
    if (o && typeof srcOf === 'function' && srcOf(o) === 'Depop') {
      const num = o.lastMileWaybill || o.ocOrderNo || tn;
      const href = DEPOP_TRACK(num);
      if (!href) { linkEl.innerHTML = ''; return; }
      linkEl.innerHTML = '<a class="depop-go" href="' + href + '"' +
        ' target="_blank" rel="noopener noreferrer" title="打开 ' + num + ' 的 Depop 官方轨迹页">' +
        'Depop 官方轨迹 ↗<i>' + num + '</i></a>';
    } else {
      linkEl.innerHTML = '';
    }
  }

  function close(){
    mask.classList.remove('on');
    panel.classList.remove('on');
    panel.setAttribute('aria-hidden','true');
  }
  document.getElementById('traceClose').addEventListener('click', close);
  mask.addEventListener('click', close);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });

  document.getElementById('tbody').addEventListener('click', e => {
    const tr = e.target.closest('tr.row');
    if (!tr || !tr.dataset.tn) return;
    openTrace(tr.dataset.tn, tr.dataset.region);
  });

  async function openTrace(tn, region){
    mask.classList.add('on');
    panel.classList.add('on');
    panel.setAttribute('aria-hidden','false');
    titleEl.textContent = tn;
    subEl.textContent = '正在拉取最新轨迹…';
    body.innerHTML = '<div class="loading">加载中…</div>';
    const o0 = (typeof ALL !== 'undefined' ? ALL : []).find(x => x.ocOrderNo === tn) || {};
    setExtLink(o0, tn);

    let d;
    try {
      const r = await fetch('/api/track?tn=' + encodeURIComponent(tn) + '&region=' + encodeURIComponent(region||''));
      d = await r.json();
    } catch (err) {
      body.innerHTML = '<div class="trace-err">请求失败：' + err.message + '</div>';
      return;
    }
    if (!d.ok) {
      subEl.textContent = '';
      body.innerHTML = '<div class="trace-err">' + (d.error || '查询失败') + '</div>';
      return;
    }

    const o = d.order || {};
    if (!o0.source && o.source) setExtLink(o, tn);   /* 本地快照没这条时，用接口返回的订单补 */
    const rdcTxt = o.vendor && o.vendor !== '—'
      ? 'RDC ' + o.vendor + (o.vendorCode ? '（' + o.vendorCode + '）' : '')
      : '';
    subEl.textContent = [ o.ocOrderNo, rdcTxt,
      o.destState, o.consignee, d.eventCount + ' 条事件' ].filter(Boolean).join(' · ');

    let html = '';

    if (d.cookieInvalid) {
      html += '<div class="trace-err">OC 会话已失效，拉不到最新事件。请在页面右上角粘贴新的 Cookie 再看。</div>';
    }
    if (d.notes && d.notes.length) {
      html += '<div class="trace-note">' + d.notes.map(n => '· ' + n).join('<br/>') + '</div>';
    }

    // 优先展示接口原始事件（字段原样，不换算）
    const rawEvs = d.raw || [];
    if (rawEvs.length) {
      html += rawBlock(rawEvs, '接口实时返回 · 共 ' + rawEvs.length + ' 条原始事件 · 字段原样，未做任何换算');
    } else if ((d.events || []).length) {
      const evs = d.events;
      html += '<div style="font-size:11.5px;color:var(--muted);margin:0 0 12px">' +
        '北京时间，按发生顺序排列 · 接口未返回原始事件，下面是已整理的行</div>';
      html += '<div class="tl">' + evs.map(ev => {
        const cls = ev.isSpx ? 'spx' : (ev.isRdc ? 'key' : '');
        return '<div class="tl-item ' + cls + '">' +
          '<div class="tl-time">' + ev.time + '</div>' +
          '<div class="tl-desc"><span class="tl-code">' + ev.code + '</span>' + (ev.desc || '') + '</div>' +
          (ev.meta ? '<div class="tl-meta">' + ev.meta + '</div>' : '') +
          '</div>';
      }).join('') + '</div>';
    } else {
      html += '<div class="loading">该运单暂无事件记录</div>';
    }

    body.innerHTML = html;
  }

  /* ---- 数据刷新 ---- */
  const btnRefresh = document.getElementById('btnRefresh');
  if (btnRefresh) {
    btnRefresh.addEventListener('click', async () => {
      btnRefresh.disabled = true;
      const old = btnRefresh.textContent;
      btnRefresh.textContent = '抓取中…';
      try {
        const r = await fetch('/api/refresh', { method:'POST' });
        const d = await r.json();
        if (d.ok) location.reload();
        else alert('刷新失败：' + (d.error || '未知错误'));
      } catch (err) {
        alert('刷新失败：' + err.message);
      } finally {
        btnRefresh.disabled = false;
        btnRefresh.textContent = old;
      }
    });
  }

  /* ---- Cookie 更新 ---- */
  const btnCookie = document.getElementById('btnCookie');
  if (btnCookie) {
    btnCookie.addEventListener('click', async () => {
      const v = prompt('粘贴新的 hk-sit Cookie（形如 hk-sit=xxxxxxxx-xxxx-…）');
      if (!v) return;
      const r = await fetch('/api/cookie', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ cookie: v.trim() })
      });
      const d = await r.json();
      alert(d.ok ? '会话已更新，可以重新查询了' : '更新失败：' + (d.error || ''));
      if (d.ok) location.reload();
    });
  }
})();
</script>
"""


def _load_cross() -> dict:
    """读 cross_vendor.py 的产出，按运单号索引；没有就跑空。"""
    f = BASE_DIR / "data" / "cross_vendor.json"
    if not f.exists():
        return {}
    try:
        rows = (json.loads(f.read_text(encoding="utf-8")) or {}).get("rows") or []
    except Exception:
        return {}
    keys = ("dropAt", "pickAt", "pickCode", "pickLabel", "delayH", "isTest")
    return {r["waybill"]: {k: r.get(k) for k in keys} for r in rows if r.get("waybill")}


def _load_dropoff() -> dict:
    """读 dropoff_cancel.py 的产出，按运单号索引；没有就跑空。"""
    if not DROPOFF_FILE.exists():
        return {}
    try:
        rows = (json.loads(DROPOFF_FILE.read_text(encoding="utf-8")) or {}).get("rows") or []
    except Exception:
        return {}
    keys = ("kind", "kindLabel", "dropVendor", "dropAt", "cancelAt", "isTest", "hDropToCancel")
    return {r["waybill"]: {k: r.get(k) for k in keys} for r in rows if r.get("waybill")}


LMS_SAME_FILE = BASE_DIR / "data" / "lms_same_vendor.json"


def _load_lms_same() -> dict:
    """读 lms_same_vendor.py 的产出：建单(OCUS001)与投递(DOUS002)的 vendor 都是 LMS 的单。"""
    if not LMS_SAME_FILE.exists():
        return {}
    try:
        rows = (json.loads(LMS_SAME_FILE.read_text(encoding="utf-8")) or {}).get("rows") or []
    except Exception:
        return {}
    keys = ("createAt", "createVendor", "dropAt", "dropCode", "dropVendor", "delayH", "isTest")
    return {r["waybill"]: {k: r.get(k) for k in keys} for r in rows if r.get("waybill")}


def _static_links() -> list[dict]:
    """静态版（双击打开）能直接点开的清单文件，相对路径，和 dashboard.html 同级。

    这两个清单本来是 serve.py 的路由（/dropoff-cancel、/stuck），离线打开时打不通，
    所以改成指向已经生成好的 data/*.html —— 双击就能看，不需要起服务。
    """
    out: list[dict] = []
    dd_json = BASE_DIR / "data" / "dropoff_cancel.html"
    if dd_json.exists():
        todo = 0
        try:
            meta = json.loads((BASE_DIR / "data" / "dropoff_cancel.json")
                              .read_text(encoding="utf-8")).get("meta") or {}
            todo = int(meta.get("todo") or 0)
        except Exception:  # noqa: BLE001
            todo = 0
        out.append({
            "href": "data/dropoff_cancel.html",
            "text": "drop off 取消异常" + (f" {todo}" if todo else ""),
            "alert": bool(todo),
        })
    if (BASE_DIR / "data" / "stuck_shipcc.html").exists():
        out.append({"href": "data/stuck_shipcc.html",
                    "text": "SHIPCCUS001 取消清单", "alert": False})
    return out


# 统一刷新内核：三个入口（线上口令版 / 本地 file:// / 书签实时版）点「刷新数据」
# 走的是**同一段代码、同一套优先级**，只有最后落到哪条路上不同。
#
#   ① 书签实时版（window.__OC_LIVE）—— 数据在你的浏览器内存里，唯一的「新数据」
#      来源是回内部系统页面重点一次书签；这里只解释，不假装在抓。
#   ② 本机服务（127.0.0.1:8000 有 serve.py）—— POST /api/refresh-all，
#      跑完 refresh_all.py 会重写 dashboard.html，reload 即可。
#   ③ 云端定时（GitHub Actions 每 2 小时）—— 只「检查有没有新数据包」，
#      有就重新加载（口令在 sessionStorage 里，自动解锁）。
#
# 三处的按钮文案、进度文字、失败提示因此完全一致，不用再记「哪个页面该按哪个键」。
REFRESH_CORE_JS = r"""
<script>
window.OCRefresh = (function(){
  var QS  = new URLSearchParams(location.search);
  var API = (QS.get('api') || 'http://127.0.0.1:8000').replace(/\/+$/, '');
  var POLL = 2000, PROBE_MS = 1500;

  var esc = function(s){
    return String(s == null ? '' : s).replace(/[&<>"]/g, function(c){
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];
    });
  };

  /* ---------------- 弹窗（统一外观，替代 alert） ---------------- */
  var mask = null;
  function close(){ if (mask && mask.parentNode) mask.parentNode.removeChild(mask); mask = null; }

  function modal(title, bodyHtml, actions){
    close();
    mask = document.createElement('div');
    mask.id = '__ocf_mask';
    mask.style.cssText = 'position:fixed;inset:0;z-index:100000;background:rgba(26,26,24,.34);' +
      'display:flex;align-items:center;justify-content:center';
    mask.onclick = function(e){ if (e.target === mask) close(); };

    var card = document.createElement('div');
    card.style.cssText = 'width:430px;max-width:calc(100vw - 40px);padding:22px 24px;background:#fff;' +
      'border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.18);' +
      'font:13px/1.75 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;' +
      'color:#2c2c2a';
    card.innerHTML = '<div class="__t" style="font-size:14px;font-weight:600;margin-bottom:8px"></div>' +
      '<div class="__b" style="color:#55554f;font-size:12.5px"></div>' +
      '<div class="__a" style="margin-top:18px;display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap"></div>';
    card.querySelector('.__t').textContent = title;
    card.querySelector('.__b').innerHTML = bodyHtml || '';

    var act = card.querySelector('.__a');
    (actions || []).forEach(function(a){
      var el = document.createElement(a.href ? 'a' : 'button');
      if (a.href){ el.href = a.href; el.target = '_blank'; el.rel = 'noopener'; }
      el.textContent = a.text;
      el.style.cssText = 'padding:6px 14px;border-radius:8px;font-size:12.5px;cursor:pointer;' +
        'text-decoration:none;line-height:1.6;' +
        (a.primary
          ? 'background:#378add;border:1px solid #378add;color:#fff'
          : 'background:#fff;border:1px solid #d5d9e0;color:#3d4450');
      el.onclick = function(){ if (a.onclick) a.onclick(); else close(); };
      act.appendChild(el);
    });

    mask.appendChild(card);
    document.body.appendChild(mask);
  }

  /* ---------------- 探测本机服务 ---------------- */
  function probeLocal(){
    if (!API) return Promise.resolve(null);
    return new Promise(function(done){
      var ctl = window.AbortController ? new AbortController() : null;
      var fin = false;
      var t = setTimeout(function(){
        if (fin) return; fin = true;
        if (ctl) { try { ctl.abort(); } catch (e) {} }
        done(null);
      }, PROBE_MS);
      var opt = { cache: 'no-store' };
      if (ctl) opt.signal = ctl.signal;
      fetch(API + '/healthz', opt).then(function(r){
        return r.json().catch(function(){ return {}; }).then(function(j){
          if (fin) return; fin = true; clearTimeout(t);
          done({ status: r.status, body: j || {} });
        });
      }).catch(function(){
        if (fin) return; fin = true; clearTimeout(t); done(null);
      });
    });
  }

  /* ---------------- ② 本机抓数 ---------------- */
  function localFlow(setBusy){
    setBusy(true, '启动抓取…');
    fetch(API + '/api/refresh-all', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
    }).then(function(r){
      return r.json().catch(function(){ return {}; }).then(function(j){
        return { status: r.status, body: j || {} };
      });
    }).then(function(res){
      if (res.status === 403){
        setBusy(false);
        modal('本机服务在跑，但不允许这个页面触发',
          '服务只接受<b>本机打开的页面</b>点刷新（防止别处网页偷偷调用）。<br>' +
          '请用本机的 <code>dashboard.html</code> 或 <code>http://127.0.0.1:8000</code> 打开再点。');
        return;
      }
      // 409 = 已经有一个刷新任务在跑（另一个标签页点的，或上一次还没结束），
      // 这种情况不该报错，直接跟着看同一个进度就行
      var already = res.status === 409 && (res.body.error || '').indexOf('已有刷新') >= 0;
      if (!already && !res.body.ok){
        setBusy(false);
        modal('启动刷新失败', esc(res.body.error || '服务返回异常') );
        return;
      }
      var t0 = Date.now(), timer = null;
      function tick(){
        setBusy(true, '抓取中 ' + Math.round((Date.now() - t0) / 1000) + 's…');
        fetch(API + '/api/refresh-status', {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
        }).then(function(r){ return r.json(); }).then(function(st){
          if (st.running){
            if (st.elapsed) setBusy(true, '抓取中 ' + Math.round(st.elapsed) + 's…');
            return;
          }
          if (timer) { clearInterval(timer); timer = null; }
          if (st.ok){
            setBusy(true, '完成，正在重载…');
            setTimeout(function(){ location.reload(); }, 500);
          } else {
            setBusy(false);
            modal('抓取失败', esc(st.error || '未知错误') +
              '<div style="margin-top:8px;color:#9a9a94">Cookie 过期的话，跑一次 <code>python renew_token.py</code>。</div>');
          }
        }).catch(function(){});
      }
      timer = setInterval(tick, POLL);
      // 页面切后台时定时器会被节流到 30~60 秒一次；切回来立刻补查
      document.addEventListener('visibilitychange', function(){
        if (!document.hidden && timer) tick();
      });
    }).catch(function(){
      setBusy(false);
      modal('连不上本机服务 ' + API,
        '探测时还在，发请求时断了。可能服务刚退出，或浏览器拦了本地地址。<br>' +
        '双击项目目录的「启动看板.bat」可以重新拉起服务。');
    });
  }

  /* ---------------- ③ 云端定时：查有没有新数据包 ---------------- */
  function binSig(){
    return fetch('data.bin', {method: 'HEAD', cache: 'no-store'}).then(function(r){
      if (!r.ok) return null;
      // 与 build_site.py 解锁脚本里的取值顺序必须一致，否则会天天误判「有新数据」
      return r.headers.get('etag') || r.headers.get('last-modified')
             || r.headers.get('content-length') || '';
    }).catch(function(){ return null; });
  }

  // cron 是 `23 */2 * * *`，**UTC** 每两小时的第 23 分。必须先在 UTC 上对齐，
  // 再交给 Date 显示成本地时间——否则换个时区的浏览器会算出错误的下次时间。
  function nextRun(){
    var now = new Date();
    var u = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(),
                              now.getUTCHours(), now.getUTCMinutes(), 0, 0));
    u.setUTCMinutes(23, 0, 0);
    if (u <= now) u.setUTCHours(u.getUTCHours() + 1);
    while (u.getUTCHours() % 2 !== 0) u.setUTCHours(u.getUTCHours() + 1);
    return u;
  }
  function hhmm(d){
    return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  }

  function cloudFlow(setBusy){
    var gen = (window.META && META.generatedAt) ? String(META.generatedAt) : '未知';
    var nr = nextRun();
    var acts = [];

    acts.push({ text: '检查有没有新数据', primary: true, onclick: function(){
      close();
      setBusy(true, '检查云端数据…');
      binSig().then(function(sig){
        setBusy(false);
        if (!sig){ location.reload(); return; }
        if (!window.__OC_BIN_SIG || sig !== window.__OC_BIN_SIG){
          modal('云端已经有新数据', '正在重新加载（口令会自动带上，不用再输）。',
                [{ text: '好', primary: true, onclick: function(){ location.reload(); } }]);
          setTimeout(function(){ location.reload(); }, 900);
          return;
        }
        modal('云端还没有新数据',
          '当前数据：<b>' + esc(gen) + '</b><br>' +
          '云端每 2 小时自动抓一次，下一次约 <b>' + hhmm(nr) + '</b>。<br>' +
          '<span style="color:#9a9a94">想立刻拿到最新数据，用下面这条：</span>',
          [{ text: '用我的登录态立即取数', href: 'install.html' },
           { text: '知道了', primary: true }]);
      });
    }});

    acts.push({ text: '用我的登录态取数', href: 'install.html' });
    acts.push({ text: '关闭' });

    modal('数据来源是云端定时任务',
      '当前数据：<b>' + esc(gen) + '</b><br>' +
      '云端每 2 小时自动抓一次，下一次约 <b>' + hhmm(nr) + '</b>。' +
      '<div style="margin-top:10px;padding:9px 11px;background:#f7f7f5;border-radius:8px;font-size:12px">' +
      '想<b>立刻</b>取到最新数据：在内部系统页面点一下书签，用你自己的登录态抓，' +
      '再打开自动弹窗的看板。<a href="install.html" style="color:#378add;text-decoration:none">装书签 →</a>' +
      '</div>' +
      '<div style="margin-top:10px;color:#9a9a94;font-size:11.5px">' +
      '这台机器上如果跑着 <code>serve.py</code>，刷新会直接走本地抓数；现在没探测到。</div>',
      acts);
  }

  /* ---------------- ③-b 本地文件 + 本机没服务 ---------------- */
  // file:// 打开的 dashboard.html 不会自己更新，唯一的取数途径就是本机 serve.py，
  // 所以这里不能讲「云端每 2 小时自动抓」，得直接说怎么把服务拉起来。
  function localMissingFlow(setBusy){
    setBusy(false);
    modal('本机服务没在跑',
      '刷新要靠本机 <code>serve.py</code> 抓数，现在连不上 <code>' + esc(API) + '</code>。' +
      '<div style="margin-top:12px;padding:10px 12px;background:#f7f7f5;border-radius:8px">' +
      '<b>最简单的做法</b><br>双击项目目录里的 <code>启动看板.bat</code>，' +
      '它会起一个最小化的服务窗口，再重新打开本页。</div>' +
      '<div style="margin-top:10px;color:#9a9a94;font-size:11.5px">' +
      '手动也可以：<code>python serve.py --host 127.0.0.1 --port 8000 --no-fetch</code><br>' +
      '服务换过端口：<code>dashboard.html?api=http://127.0.0.1:端口</code></div>');
  }

  /* ---------------- ① 书签实时版 ---------------- */
  function liveFlow(setBusy){
    setBusy(false);
    modal('这个窗口的数据来自书签',
      '数据是用<b>你自己</b>在内部系统的登录态抓的，只存在这个浏览器窗口里，' +
      '所以「刷新」必须回到内部系统页面重新点一次书签。<div style="margin-top:10px;color:#9a9a94">' +
      '抓完会自动弹出一个新的看板窗口，用那个就行。</div>',
      [{ text: '知道了', primary: true }]);
  }

  /* ---------------- 入口 ---------------- */
  function run(setBusy){
    setBusy(true, '检查数据源…');
    if (window.__OC_LIVE){ liveFlow(setBusy); return; }
    probeLocal().then(function(p){
      if (p) { localFlow(setBusy); return; }
      if (location.protocol === 'file:'){ localMissingFlow(setBusy); return; }
      setBusy(false);
      cloudFlow(setBusy);
    });
  }

  return { run: run, modal: modal, close: close };
})();
</script>
"""


# 静态版专用：标题右侧一排按钮
#   ①「刷新数据」—— 逻辑全在 REFRESH_CORE_JS 里（三处入口共用），这里只负责摆位置、
#      管按钮忙碌态。本机有 serve.py 就本地抓，托管站就查云端有没有新数据包。
#      **不用输口令**：服务端只认「回环 IP + 无反代头 + 本机 Origin」，本机点一下就开始抓；
#      隧道/内网进来的人没有这个组合，会被 403 挡掉
#   ② 两张异常清单 —— 直接指向已生成好的 data/*.html
STATIC_LINKS_JS = r"""
<script>
(function(){
  const genLine = document.getElementById('genline');
  if (!genLine) return;

  const wrap = document.createElement('span');
  wrap.style.cssText = 'display:block;text-align:right;margin:0 0 4px';

  function makeLink(href, text, alert){
    const a = document.createElement('a');
    a.href = href; a.target = '_blank'; a.rel = 'noopener';
    a.textContent = text;
    a.title = '本地清单文件，双击即可打开';
    a.style.cssText = 'display:inline-block;margin-left:8px;padding:3px 10px;border-radius:12px;' +
      'font-size:12px;line-height:18px;text-decoration:none;border:1px solid ' +
      (alert ? '#e5484d' : '#d5d9e0') + ';color:' + (alert ? '#fff' : '#3d4450') +
      ';background:' + (alert ? '#e5484d' : '#fff');
    return a;
  }

  /* ---------- 刷新按钮 ---------- */
  const btn = document.createElement('button');
  btn.id = 'btnRefresh';           // live.js 会按这个 id 改 title（文案保持统一）
  btn.textContent = '刷新数据';
  btn.title = '取最新数据：本机有 serve.py 就本地抓数，托管站就查云端有没有新数据包';
  btn.style.cssText = 'margin-left:0;padding:3px 14px;border-radius:12px;font-size:12px;' +
    'line-height:18px;font-family:inherit;cursor:pointer;color:#fff;background:#e8722c;' +
    'border:1px solid #e8722c';
  wrap.appendChild(btn);

  // 三处入口共用同一套逻辑，见 REFRESH_CORE_JS；这里只管按钮的忙碌态
  function setBusy(on, txt){
    btn.disabled = !!on;
    btn.style.cursor = on ? 'default' : 'pointer';
    btn.style.opacity = on ? '0.75' : '1';
    btn.textContent = txt || (on ? '取数中…' : '刷新数据');
  }
  btn.onclick = function(){
    if (!window.OCRefresh){ alert('页面是旧版本，请刷新一次本页'); return; }
    window.OCRefresh.run(setBusy);
  };

  /* ---------- 本地清单链接 ---------- */
  const links = (typeof META !== 'undefined' && META && META.localLinks) || [];
  links.forEach(function(l){ wrap.appendChild(makeLink(l.href, l.text, l.alert)); });

  genLine.parentNode.insertBefore(wrap, genLine);
})();
</script>
"""


# 静态版专用：点运单号 → 右侧抽屉看轨迹
#   数据有两级来源，自动降级，不弹错：
#     ① 本机 serve.py 在跑 → 调 /api/track 拿**完整事件流**（与动态版一模一样）
#     ② 没起服务 / 换了台机器 → 用页面内联的订单对象画**里程碑时间轴**
#        （下单 / 面单 / A-SCAN / RDC 出库 / SPX 提取 / 投递 / 妥投 / 最新）
#   离线可用，是静态版能给出的最好结果：明细事件必须实时调 OC，静态文件里没有。
STATIC_TRACE_JS = r"""
<script>
(function(){
  const mask   = document.getElementById('traceMask');
  const panel  = document.getElementById('tracePanel');
  const bodyEl = document.getElementById('traceBody');
  const titleEl= document.getElementById('traceTitle');
  const subEl  = document.getElementById('traceSub');
  const linkEl = document.getElementById('traceLink');
  const closeEl= document.getElementById('traceClose');
  const tbody  = document.getElementById('tbody');
  if (!mask || !panel || !tbody) return;

  const QS = new URLSearchParams(location.search);
  const API = (QS.get('api') || 'http://127.0.0.1:8000').replace(/\/+$/, '');
  document.body.classList.add('dynamic');   // 复用动态族的样式（行 cursor / hover）

  const fmtH = v => (v === null || v === undefined) ? '—' : (Math.round(v * 10) / 10).toFixed(1);
  const esc  = s => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

  /* Depop 单在抽屉头部给一个官方轨迹页入口；非 Depop 清空 */
  function setExtLink(o, tn){
    if (!linkEl) return;
    if (o && typeof srcOf === 'function' && srcOf(o) === 'Depop') {
      const num = o.lastMileWaybill || o.ocOrderNo || tn;
      const href = DEPOP_TRACK(num);
      if (!href) { linkEl.innerHTML = ''; return; }
      linkEl.innerHTML = '<a class="depop-go" href="' + href + '"' +
        ' target="_blank" rel="noopener noreferrer" title="打开 ' + num + ' 的 Depop 官方轨迹页">' +
        'Depop 官方轨迹 ↗<i>' + num + '</i></a>';
    } else {
      linkEl.innerHTML = '';
    }
  }

  function open(){ mask.classList.add('on'); panel.classList.add('on'); panel.setAttribute('aria-hidden','false'); }
  function close(){ mask.classList.remove('on'); panel.classList.remove('on'); panel.setAttribute('aria-hidden','true'); }
  closeEl.addEventListener('click', close);
  mask.addEventListener('click', close);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });

  /* ---------- 离线的里程碑视图 ---------- */
  const ML = [
    ['orderCreateTime', 'ORDER',   '订单创建',            'key',  ''],
    ['labelTime',       'LABEL',   '面单生成',            '',     ''],
    ['aScanTime',       'ASCAN',   'A-SCAN 揽收 / 投递',  'key',  ''],
    ['rdcOutTime',      'RDC',     'RDC 出库',            'key',  'vendor'],
    ['spxPickupTime',   'SPX',     'SPX 提取',            'spx',  ''],
    ['dropTime',        'DROP',    '末端投递',            '',     'dropVendor'],
    ['deliveredTime',   'DEL',     '妥投',                'spx',  ''],
  ];

  function localTrace(o){
    const base = o.orderCreateTime || '';
    const baseMs = base ? Date.parse(base.replace(/-/g, '/')) : 0;
    const rows = [];
    const seen = {};
    ML.forEach(function(m){
      const t = o[m[0]];
      if (!t) return;
      let code = '';
      if (m[1] === 'DROP')  code = o.dropEventCode || '';
      if (m[1] === 'DEL')   code = o.deliveredCode || '';
      let desc = m[2];
      if (m[4] === 'vendor' && o.vendor && o.vendor !== '—') {
        // vendor 与 vendorCode 同名时（LMS / LMS）只显示一次，别重复
        const vc = (o.vendorCode && o.vendorCode !== o.vendor) ? ' / ' + o.vendorCode : '';
        desc += '（' + o.vendor + vc + '）';
      }
      if (m[4] === 'dropVendor' && o.dropVendor)
        desc += '（' + o.dropVendor + '）';   // 事件码已单独显示在左侧，别重复
      const k = t + '|' + m[1];
      if (seen[k]) return;
      seen[k] = 1;
      const ms = Date.parse(t.replace(/-/g, '/'));
      rows.push({ t: t, code: code || m[1], desc: desc, cls: m[3],
                  deltaH: (baseMs && ms) ? (ms - baseMs) / 3600000 : null });
    });
    // 最新节点：不是上面任一项时补一条，否则抽屉会少掉「卡在哪」
    if (o.latestTime && !seen[o.latestTime + '|DEL'] && !seen[o.latestTime + '|DROP']) {
      const exists = rows.some(r => r.t === o.latestTime);
      if (!exists) {
        const ms = Date.parse(o.latestTime.replace(/-/g, '/'));
        rows.push({ t: o.latestTime, code: o.latestCode || 'LATEST', desc: '最新节点', cls: '',
                    deltaH: (baseMs && ms) ? (ms - baseMs) / 3600000 : null });
      }
    }
    rows.sort((a, b) => a.t < b.t ? -1 : (a.t > b.t ? 1 : 0));
    return rows;
  }

  function tlBlock(rows, baseLabel){
    if (!rows.length) return '<div class="loading">该运单暂无节点记录</div>';
    const tzName = (TZ_LIST.find(function(t){ return t.key === tzMode; }) || TZ_LIST[0]).label;
    return '<div style="font-size:11.5px;color:var(--muted);margin:0 0 12px">' +
      tzName + '时间，按发生顺序排列 · +Xh 为' + baseLabel + '的累计时长</div>' +
      '<div class="tl">' + rows.map(function(ev){
        const dv = ev.deltaH;
        let dTxt = '';
        if (dv !== null && dv !== undefined) {
          const neg = dv < 0;
          dTxt = '<span class="tl-delta"' + (neg ? ' style="color:var(--red)"' : '') + '>' +
                 (neg ? '' : '+') + fmtH(dv) + 'h' + (neg ? '（早于建单）' : '') + '</span>';
        }
        return '<div class="tl-item ' + (ev.cls || '') + '">' +
          '<div class="tl-time">' + esc(tzStamp(ev.time || ev.t)) + dTxt + '</div>' +
          '<div class="tl-desc"><span class="tl-code">' + esc(ev.code) + '</span>' + esc(ev.desc || '') + '</div>' +
          (ev.meta ? '<div class="tl-meta">' + esc(ev.meta) + '</div>' : '') +
          '</div>';
      }).join('') + '</div>';
  }

  function anomalies(o, tn){
    let h = '';
    const c = (typeof CROSS !== 'undefined' && CROSS) ? CROSS[tn] : null;
    if (c) {
      h += '<div class="trace-note">串场：' + esc(c.dropAt || '') + ' 在 PUDO 点投递，' +
           esc(c.pickAt || '') + ' 被 ' + esc(c.pickLabel || 'LMS') + ' 司机取走' +
           (c.delayH != null ? '（间隔 ' + fmtH(c.delayH) + 'h）' : '') + '</div>';
    }
    const d = (typeof DROPOFF !== 'undefined' && DROPOFF) ? DROPOFF[tn] : null;
    if (d) {
      h += '<div class="trace-err">投递后取消：' + esc(d.kindLabel || d.kind || '') +
           '　投递 ' + esc(d.dropAt || '') + ' → 取消 ' + esc(d.cancelAt || '') +
           (d.hDropToCancel != null ? '（' + fmtH(d.hDropToCancel) + 'h）' : '') + '</div>';
    }
    return h;
  }

  /* ---------- 主流程：先试接口，失败降级 ---------- */
  async function openTrace(tn, region){
    open();
    titleEl.textContent = tn;
    bodyEl.innerHTML = '<div class="loading">加载中…</div>';

    const o = (typeof ALL !== 'undefined' ? ALL : []).find(x => x.ocOrderNo === tn) || {};
    setExtLink(o, tn);
    let d = null;
    /* API 为空（加密部署版）时不发请求，直接用内嵌原始事件：静态托管上没有 serve.py */
    if (API) {
      try {
        const r = await fetch(API + '/api/track?tn=' + encodeURIComponent(tn) +
                              '&region=' + encodeURIComponent(region || ''), { cache: 'no-store' });
        if (r.ok) d = await r.json();
      } catch (e) { /* 服务没起、或跨源被拦：静默降级 */ }
    }

    subEl.textContent = [tn, o.vendor && o.vendor !== '—' ? 'RDC ' + o.vendor : '',
                         o.destState, o.consignee].filter(Boolean).join(' · ');

    // ① 服务在跑：用接口刚拉的原始事件（最新）
    if (d && d.ok && (d.raw || []).length) {
      bodyEl.innerHTML = rawBlock(d.raw,
        '接口实时返回 · 共 ' + d.raw.length + ' 条原始事件 · 字段原样，未做任何换算');
      return;
    }

    // ② 服务没起 / 接口不通：用构建时嵌进 HTML 的那份原始事件
    const rawKey = RAW[tn] ? tn
                 : (o.lastMileWaybill && RAW[o.lastMileWaybill] ? o.lastMileWaybill : '');
    if (rawKey && RAW[rawKey].length) {
      const evs = RAW[rawKey];
      let html = '<div class="trace-note">下面是<b>构建时嵌入的接口原始事件</b>' +
        (RAW_SNAP ? '（快照 ' + esc(RAW_SNAP) + '）' : '') +
        '，字段与接口返回完全一致。' +
        (API ? '要最新数据请点「刷新数据」。' : '') + '</div>';
      html += rawBlock(evs, '共 ' + evs.length + ' 条 · 原样展示，未做任何换算');
      bodyEl.innerHTML = html;
      return;
    }

    // ③ 连原始事件都没有：最后才退回里程碑
    const cutTxt = RAW_CUTOFF ? String(RAW_CUTOFF) : '';
    const created = (o.orderCreateTime || '').slice(0, 10);
    let why;
    if (RAW_DAYS > 0 && cutTxt && created && created < cutTxt) {
      why = '这张单建单于 ' + created + '，早于内嵌窗口（' + cutTxt + ' 起，最近 ' + RAW_DAYS +
            ' 天），没内嵌原始事件。想看它的原始事件：把 .env 的 OC_RAW_DAYS 调大（0 = 全量）后重新生成。';
    } else {
      why = '这张单不在本地事件缓存里，拿不到原始事件，只能给出由字段拼出的里程碑（仅供参考）。';
    }
    let html = '<div class="trace-note">' + esc(why) + '</div>';
    html += anomalies(o, tn);
    html += tlBlock(localTrace(o), '距订单创建');
    bodyEl.innerHTML = html;
  }

  tbody.addEventListener('click', function(e){
    const tr = e.target.closest('tr.row');
    if (!tr || !tr.dataset.tn) return;
    openTrace(tr.dataset.tn, tr.dataset.region);
  });
})();
</script>
"""


# 原始事件渲染器：接口字段原样列出，不换算、不合并、不筛选。
# 静态版把事件嵌进 HTML（__RAW_JSON__），动态版留空、改由 /api/track 现拉。
RAW_BLOCK_JS = r"""
<script>
var RAW = __RAW_JSON__;
var RAW_SNAP = __RAW_SNAP__;
var RAW_CUTOFF = __RAW_CUTOFF__;
var RAW_DAYS = __RAW_DAYS__;

function rawEsc(s){
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function rawTs(e){
  var n = Number((e && (e.oprTimestamp || e.oprTime)) || 0);
  return isFinite(n) ? n : 0;
}
/* 只做时区呈现：oprTimeWithZone 原串照抄，另外标一个北京时间方便对表 */
function rawBj(e){
  var n = rawTs(e);
  if (!n) return '';
  var d = new Date(n + 8 * 3600 * 1000);
  var p = function(x){ return (x < 10 ? '0' : '') + x; };
  return d.getUTCFullYear() + '-' + p(d.getUTCMonth() + 1) + '-' + p(d.getUTCDate()) + ' ' +
         p(d.getUTCHours()) + ':' + p(d.getUTCMinutes()) + ':' + p(d.getUTCSeconds());
}
function rawBlock(evs, note){
  if (!evs || !evs.length) return '<div class="loading">该运单暂无事件记录</div>';
  var arr = evs.slice().sort(function(a, b){ return rawTs(a) - rawTs(b); });
  var h = '<div style="font-size:11.5px;color:var(--muted);margin:0 0 10px">' + rawEsc(note) + '</div>';
  h += '<div class="raw">';
  arr.forEach(function(e){
    var jv = e.jvEventCode || '';
    var jvRemark = e.jvEventCnRemark || e.jvEventEnRemark || '';
    var isc = e.isEventCode || '';
    var isRemark = e.isEventCnRemark || '';
    var vend = [e.vendorCode, e.vendorName, e.vendorEventCode]
      .filter(function(x){ return x; }).join(' / ');
    var desc = [e.oprCnDes, e.oprEnDes].filter(function(x, i, a){
      return x && a.indexOf(x) === i; }).join(' · ');
    var opr = [e.oprUser, e.oprDistrict, e.oprCountry]
      .filter(function(x){ return x; }).join(' · ');
    h += '<div class="raw-item">' +
      '<div class="raw-l">' +
        '<div class="raw-t">' + rawEsc(e.oprTimeWithZone || e.oprTime || '') + '</div>' +
        (rawBj(e) ? '<div class="raw-bj">北京 ' + rawEsc(rawBj(e)) + '</div>' : '') +
      '</div>' +
      '<div class="raw-r">' +
        '<div class="raw-line"><span class="tl-code">' + rawEsc(jv || '—') + '</span>' + rawEsc(jvRemark) + '</div>' +
        (isc ? '<div class="raw-line"><span class="raw-k">isEventCode</span>' + rawEsc(isc) +
               (isRemark ? ' ' + rawEsc(isRemark) : '') + '</div>' : '') +
        (vend ? '<div class="raw-line"><span class="raw-k">vendor</span>' + rawEsc(vend) + '</div>' : '') +
        (desc ? '<div class="raw-line raw-desc">' + rawEsc(desc) + '</div>' : '') +
        (opr ? '<div class="raw-line"><span class="raw-k">opr</span>' + rawEsc(opr) + '</div>' : '') +
        '<details class="raw-json"><summary>原始 JSON（这条事件的全部字段）</summary><pre>' +
          rawEsc(JSON.stringify(e, null, 1)) + '</pre></details>' +
      '</div></div>';
  });
  return h + '</div>';
}
</script>
"""


def mask_name(name: str) -> str:
    """收件人脱敏：保留首末字符。EVA MYRBACK -> E***A M***K

    只用于「外发视图」。公司同事需要核对收件人时可以给内部口令，
    外发口令看到的永远是打码后的名字。
    """
    out = []
    for w in str(name or "").split():
        if len(w) <= 2:
            out.append(w[0] + "*")
        elif len(w) <= 4:
            out.append(w[0] + "*" * (len(w) - 2) + w[-1])
        else:
            out.append(w[0] + "***" + w[-1])
    return " ".join(out)


# 外发视图里整体剔除的字段：完整门牌属于个人信息，页面不显示也不该从接口带出去
PII_DROP_FIELDS = ("consigneeAddr",)


def mask_orders(orders: list) -> list:
    """外发视图数据：姓名打码 + 剔掉门牌。返回浅拷贝，不动调用方的原对象。"""
    out = []
    for o in orders:
        c = dict(o)
        for k in PII_DROP_FIELDS:
            c.pop(k, None)
        if c.get("consignee"):
            c["consignee"] = mask_name(c["consignee"])
        out.append(c)
    return out


def _raw_window(orders: list) -> tuple:
    """内嵌窗口：默认嵌**最近 7 天建单**的单，外加**还没妥投的真实单**（管它建单多早）。

    前一条控制体积（全量 2.5MB → 约 1.4MB），后一条保证「正在盯的单」一定有原始事件：
    待提取、待 SPX、异常单往往建单已经超过 7 天，被 7 天窗口裁掉最难受。
    OC_RAW_DAYS=0 表示不裁，全量内嵌。
    返回 (允许的运单号集合 or None, 截止日期文本, 天数)。
    """
    try:
        days = int((_env("OC_RAW_DAYS", "7") or "7").strip())
    except ValueError:
        days = 7
    if days <= 0:
        return None, "", 0
    cut = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    keep = set()
    for o in orders:
        tn = o.get("lastMileWaybill") or o.get("ocOrderNo") or ""
        if not tn:
            continue
        recent = (o.get("orderCreateTime") or "")[:10] >= cut
        in_flight = not o.get("deliveredTime") and not o.get("isTest")
        if recent or in_flight:
            keep.add(tn)
    return keep, cut, days


def _load_raw_events(keep: set | None = None) -> tuple:
    """读最新一份事件缓存，按运单号索引**接口原始事件**（字段原样，不改不合并）。

    只给静态看板用：把原始事件嵌进 HTML，抽屉才能离线看到真正的原始数据，
    而不是拿归一化字段拼出来的里程碑。keep 为 None 表示不过滤。
    返回 ({运单号: [事件...]}, 快照时间文本)。
    """
    files = sorted((BASE_DIR / "data").glob("_ev_cache_*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files:
        try:
            blob = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        out = {}
        for item in blob or []:
            rec = item.get("rec") or {}
            tn = str(rec.get("lastMileWaybill") or rec.get("ocOrderNo") or "")
            evs = item.get("evs") or []
            if not tn or not evs:
                continue
            if keep is not None and tn not in keep:
                continue
            out[tn] = evs
        if out:
            snap = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            return out, snap
    return {}, ""


def render(meta: dict, orders: list, dynamic: bool = False, readonly: bool = False,
           external: bool = False, secure: bool = False) -> str:
    """渲染看板 HTML。dynamic=True 挂载单号追踪抽屉与刷新/Cookie 入口。

    readonly=True 时隐藏「刷新数据」「更新 Cookie」，只留查询与追踪。
    external=True 是外发视图：收件人打码、门牌字段整体剔除（数据在注入前就处理掉，
    前端拿不到明文，不是靠 CSS 遮）。
    secure=True 是加密部署版：HTML 里不内嵌任何业务数据，数据被打包进 SECURE_PAYLOAD，
    由 build_site.py 加密成 data.bin，浏览器输口令后解密回填再调 window.__start()。
    """
    meta = _meta_with_defaults(meta)
    if external:
        meta["external"] = True
        orders = mask_orders(orders)
    cross = _load_cross()
    if cross:
        meta["crossMeta"] = {
            "count": len(cross),
            "items": list(cross.keys()),
            "rule": "DOUS002(vendor=PU) 之后出现 COLUS002/DFDLUS002/AFDLUS002/SHIPFCUS002 且 vendorCode=LM；不含 SHIPCCUS001 取消",
        }
    dropoff = _load_dropoff()
    if dropoff:
        todo = [v for v in dropoff.values()
                if not v.get("isTest") and v.get("kind") in ("inverted", "noResume")]
        meta["dropoffCancel"] = {
            "todo": len(todo),
            "hits": len(dropoff),
            "rule": "已投递（FIRST_SCAN）却被取消（SHIPCCUS001）：取消早于投递=时序倒挂，"
                    "投递后取消且无更晚节点=取消后无后续",
        }
    lms_same = _load_lms_same()
    if lms_same:
        real = [v for v in lms_same.values() if not v.get("isTest")]
        meta["lmsSame"] = {
            "hits": len(lms_same),
            "real": len(real),
            "rule": "OCUS001 与 DOUS002/DOUD002 两条事件的 vendorCode 都是 LM",
        }
    extra_js = ""
    # 静态版：把接口原始事件嵌进 HTML，抽屉离线也能看到原始数据（默认只嵌最近 7 天）
    raw_map, raw_snap = ({}, "")
    raw_cut, raw_days = ("", 0)
    if not dynamic:
        keep, raw_cut, raw_days = _raw_window(orders)
        raw_map, raw_snap = _load_raw_events(keep)
    if dynamic:
        extra_js = ((TRACE_JS + RAW_BLOCK_JS)
                    .replace("__DYNAMIC__", "true")
                    .replace("__READONLY__", "true" if readonly else "false"))
    else:
        if not secure:
            # 加密部署版没有"本机路径"这个概念，别把 C:\ 路径写进外发页面
            meta["localLinks"] = _static_links()
        # REFRESH_CORE_JS 必须在 STATIC_LINKS_JS 之前：后者一执行就会绑定按钮
        extra_js = REFRESH_CORE_JS + STATIC_LINKS_JS + STATIC_TRACE_JS + RAW_BLOCK_JS
        if secure:
            # 加密部署版跑在静态托管上，没有本机 serve.py：默认不发 /api/track 请求，
            # 直接吃内嵌的原始事件（仍可用 ?api=https://... 指向一个真实后端）
            extra_js = extra_js.replace(
                "const API = (QS.get('api') || 'http://127.0.0.1:8000')",
                "const API = (QS.get('api') || '')")
    # 两条日期口径：汇总区起点 / 明细表起点
    kpi_start = _env("OC_KPI_START", "2026/10/29").replace("/", "-")
    detail_start = _env("OC_DETAIL_START", "2026/10/22").replace("/", "-")
    if secure:
        # 数据整体抽出去加密，HTML 里只留空壳，由解锁脚本回填
        global SECURE_PAYLOAD
        SECURE_PAYLOAD = {
            # 加密外发版也要脱敏：口令一旦被猜到/泄露，前端拿到的就是明文姓名 + 完整门牌。
            # 姓名打码、剔除 consigneeAddr，保留城市/州/ZIP 足够做投递点位分析。
            "meta": meta, "orders": mask_orders(orders), "cross": cross, "dropoff": dropoff,
            "lmsSame": lms_same, "raw": raw_map, "rawSnap": raw_snap,
            "rawCut": raw_cut, "rawDays": raw_days,
        }
        meta_json, data_json = "null", "[]"
        cross_json = dropoff_json = lmssame_json = "{}"
        raw_json = "{}"
        raw_snap_json, raw_cut_json, raw_days_val = '""', '""', "0"
    else:
        meta_json = json.dumps(meta, ensure_ascii=False)
        data_json = json.dumps(orders, ensure_ascii=False)
        cross_json = json.dumps(cross, ensure_ascii=False)
        dropoff_json = json.dumps(dropoff, ensure_ascii=False)
        lmssame_json = json.dumps(lms_same, ensure_ascii=False)
        raw_json = json.dumps(raw_map, ensure_ascii=False).replace("</", "<\\/")
        raw_snap_json = json.dumps(raw_snap, ensure_ascii=False)
        raw_cut_json = json.dumps(raw_cut, ensure_ascii=False)
        raw_days_val = str(int(raw_days))
    # Depop 官方轨迹页前缀：OC_DEPOP_TRACK 只配域名，路径与参数在代码里拼
    _dt = _env("OC_DEPOP_TRACK").strip().rstrip("/")
    depop_track_base = f"{_dt}/en/tracking?number=" if _dt else ""
    html = (TEMPLATE
            .replace("__META_JSON__", meta_json)
            .replace("__DATA_JSON__", data_json)
            .replace("__CROSS_JSON__", cross_json)
            .replace("__DROPOFF_JSON__", dropoff_json)
            .replace("__LMSSAME_JSON__", lmssame_json)
            .replace("__KPI_START__", kpi_start)
            .replace("__DETAIL_START__", detail_start)
            # Depop 官方轨迹页：域名从 OC_DEPOP_TRACK 注入（仓库源码里不写死）
            .replace("__DEPOP_TRACK_BASE__", json.dumps(depop_track_base))
            # 抽屉样式与骨架两版共用：静态版也要「点单号看右侧轨迹」
            .replace("__EXTRA_CSS__", TRACE_CSS)
            .replace("__EXTRA_HTML__", TRACE_HTML)
            .replace("__EXTRA_JS__", extra_js)
            # 原始事件在这里注入（先替换 extra_js 再替换它，顺序不能调）
            .replace("__RAW_JSON__", raw_json)
            .replace("__RAW_SNAP__", raw_snap_json)
            .replace("__RAW_CUTOFF__", raw_cut_json)
            .replace("__RAW_DAYS__", raw_days_val))
    return html


def main():
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    meta = data.get("meta", {})
    orders = data.get("orders", [])

    html = render(meta, orders, dynamic=False)
    OUT_FILE.write_text(html, encoding="utf-8")
    print(f"[dashboard] 已生成 -> {OUT_FILE}")
    print(f"[dashboard] 订单数 {len(orders)}")


if __name__ == "__main__":
    main()
