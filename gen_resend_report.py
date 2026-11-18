#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""再发批次核查：把用户给的 21 个单号按「取消后有无后续节点」分组，产出 HTML + CSV。

输入：data/resend_probe.json（probe_resend.py 产物）、data/stuck_shipcc.csv（15 单停单清单）
输出：resend_check.html、data/resend_check.csv
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"

# 6 个清单外单号的订单头（从 tracking-number-list 实测取回）
EXTRA_HEADER = {
    "USPDPPS1000057787N": ("TX", "EVA MYRBACK", "2026-11-03 07:31:16"),
    "USPDPPS2000057434N": ("NY", "EVA MYRBACK", "2026-10-23 03:36:20"),
    "USPDPPS2000057571N": ("FL", "SHARON DAVIS", "2026-10-30 09:02:29"),
    "USPDPPS5000057516N": ("NY", "Test", "2026-10-28 17:42:01"),
    "USPDPPS6000057447N": ("NY", "Test", "2026-10-26 11:19:24"),
    "USPDPPS9000057574N": ("WA", "GIOVANNI FIORILLI", "2026-10-30 10:21:59"),
}

CODE_CN = {
    "INF005": "信息收到",
    "OCRETURNLBDE002": "建面单",
    "OCUS001": "已建单",
    "DOUS002": "包裹已投递",
    "COLUS002": "离开投递点",
    "AFDLUS002": "到达枢纽扫描",
    "SHIPFCUS002": "已交尾程预报",
    "DFDLUS002": "RDC 出库",
    "SCAUS002": "SPX 已取件",
    "INT005": "尾程转运中",
    "ARR005": "到达站点",
    "OFDUS006": "派送中",
    "DELUS006": "已签收",
    "SHIPCCUS001": "订单取消",
    "MISUS001": "包裹异常",
}


def load_stuck():
    p = DATA / "stuck_shipcc.csv"
    out = {}
    with p.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            out[r["waybill"]] = r
    return out


def main():
    probe = json.loads((DATA / "resend_probe.json").read_text(encoding="utf-8"))
    stuck = load_stuck()

    rows = []
    for o in probe:
        tn = o["tn"]
        st = stuck.get(tn)
        if st:
            state, name, created = st["destState"], st["consignee"], st["orderCreateTime"]
            group = st["group"]
            src = "15单停单清单"
        else:
            state, name, created = EXTRA_HEADER.get(tn, ("", "", ""))
            group, src = "", "清单外"

        after = o["events"][o["events"].index(next(e for e in o["events"] if e["code"] == "SHIPCCUS001")) + 1:] \
            if o["hasCancel"] else []
        # 取最后一次取消之后的节点
        idx = max(i for i, e in enumerate(o["events"]) if e["code"] == "SHIPCCUS001")
        after = o["events"][idx + 1:]

        if not o["hasCancel"]:
            verdict, cls = "无取消节点", ""
        elif not after:
            verdict, cls = "取消后零动作 · 真停", "bad"
        else:
            # 取消后又出现「建面单 / 已建单 / 信息收到」= 同号复用重发
            rebuilt = any(e["code"] in ("INF005", "OCRETURNLBDE002", "OCUS001") for e in after)
            if rebuilt:
                verdict, cls = "取消后同号重建 · 等同换单重发", "warn"
            else:
                verdict, cls = "取消后仍在动 · 不算停", "ok"

        rows.append({
            "waybill": tn,
            "state": state,
            "consignee": name,
            "orderCreateTime": created,
            "inStuckList": "是" if st else "否",
            "group": group,
            "eventCount": o["n"],
            "cancelAt": o["cancelAt"],
            "afterCount": len(after),
            "afterChain": " → ".join(f"{CODE_CN.get(e['code'], e['code'])} {e['time'][5:16]}" for e in after),
            "verdict": verdict,
            "cls": cls,
        })

    order = {"bad": 0, "warn": 1, "ok": 2, "": 3}
    rows.sort(key=lambda r: (order.get(r["cls"], 9), r["cancelAt"]))

    with (DATA / "resend_check.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    bad = [r for r in rows if r["cls"] == "bad"]
    warn = [r for r in rows if r["cls"] == "warn"]
    ok = [r for r in rows if r["cls"] == "ok"]
    instuck = [r for r in rows if r["inStuckList"] == "是"]
    instuck_bad = [r for r in instuck if r["cls"] == "bad"]

    json.dump(rows, (DATA / "resend_check.json").open("w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    html = build_html(rows, bad, warn, ok, instuck, instuck_bad)
    (BASE / "resend_check.html").write_text(html, encoding="utf-8")
    print(f"[save] {len(rows)} 单：真停 {len(bad)} / 重建 {len(warn)} / 仍在动 {len(ok)}")
    print(f"[save] 15单清单内 {len(instuck)} 单，其中真停 {len(instuck_bad)} 单")
    print("[save] resend_check.html + data/resend_check.csv")


def build_html(rows, bad, warn, ok, instuck, instuck_bad):
    trs = []
    for r in rows:
        trs.append(f"""<tr class="{r['cls']}">
  <td class="tn">{r['waybill']}</td>
  <td>{r['state']}<span class="nm">{r['consignee']}</span></td>
  <td class="t">{r['orderCreateTime']}</td>
  <td class="c">{r['inStuckList']}{('·'+r['group']) if r['group'] else ''}</td>
  <td class="t">{r['cancelAt']}</td>
  <td class="c">{r['afterCount']}</td>
  <td class="chain">{r['afterChain'] or '—'}</td>
  <td><span class="tag {r['cls']}">{r['verdict']}</span></td>
</tr>""")

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>再发批次核查 · 21 单</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--line:#e4e7ec;--tx:#1f2430;--mut:#6b7280;
--bad:#c0392b;--warn:#b7791f;--ok:#1e7e50;}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--tx);
font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
.wrap{{max-width:1180px;margin:0 auto;padding:28px 20px 60px}}
h1{{font-size:19px;margin:0 0 4px;font-weight:600}}
.sub{{color:var(--mut);font-size:12.5px;margin-bottom:20px}}
.kpi{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:22px}}
.k{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px}}
.k b{{display:block;font-size:24px;font-weight:600;letter-spacing:-.5px}}
.k span{{color:var(--mut);font-size:12px}}
.k.bad b{{color:var(--bad)}} .k.warn b{{color:var(--warn)}} .k.ok b{{color:var(--ok)}}
table{{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:8px;overflow:hidden;font-size:13px}}
th{{background:#fafbfc;text-align:left;font-weight:600;color:#4b5563;font-size:12px;
padding:10px 12px;border-bottom:1px solid var(--line);white-space:nowrap}}
td{{padding:9px 12px;border-bottom:1px solid #f0f2f5;vertical-align:top}}
tr:last-child td{{border-bottom:none}}
tr.bad{{background:#fdf5f4}} tr.ok{{background:#f4faf7}} tr.warn{{background:#fdfaf3}}
.tn{{font-family:ui-monospace,Consolas,monospace;font-size:12.5px;white-space:nowrap}}
.nm{{display:block;color:var(--mut);font-size:11.5px}}
.t{{white-space:nowrap;color:#374151;font-size:12.5px}}
.c{{text-align:center}}
.chain{{font-size:12px;color:#4b5563;max-width:250px}}
.tag{{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11.5px;white-space:nowrap}}
.tag.bad{{background:#fae7e5;color:var(--bad)}}
.tag.warn{{background:#fbf0dc;color:var(--warn)}}
.tag.ok{{background:#e2f3ea;color:var(--ok)}}
.note{{margin-top:18px;background:var(--card);border:1px solid var(--line);
border-radius:8px;padding:16px 18px;font-size:13px}}
.note h2{{font-size:13px;margin:0 0 8px;font-weight:600}}
.note li{{margin:5px 0;color:#374151}}
.foot{{margin-top:14px;color:var(--mut);font-size:11.5px}}
</style></head><body><div class="wrap">
<h1>再发批次核查 · {len(rows)} 单</h1>
<div class="sub">口径：OC 后台 US 区 tracking-event-list 全量事件；时间北京时间 UTC+8；数据截至 2026-11-03 11:25</div>

<div class="kpi">
  <div class="k bad"><b>{len(bad)}</b><span>取消后零动作（真停）</span></div>
  <div class="k warn"><b>{len(warn)}</b><span>取消后重建面单</span></div>
  <div class="k ok"><b>{len(ok)}</b><span>取消后仍在动</span></div>
  <div class="k"><b>{len(instuck_bad)}/{len(instuck)}</b><span>15 单清单内仍为真停</span></div>
</div>

<table>
<thead><tr>
<th>物流单号</th><th>州 / 收件人</th><th>下单时间</th><th>15单清单</th>
<th>取消时间</th><th>取消后节点</th><th>取消后链路</th><th>判定</th>
</tr></thead>
<tbody>
{''.join(trs)}
</tbody></table>

<div class="note">
<h2>结论</h2>
<ul>
<li><b>21 单全部都有 SHIPCCUS001 取消节点</b>，没有一单是"无取消节点"；差别只在取消之后还有没有东西。</li>
<li>其中 <b>{len(bad)} 单取消即终点、之后再无任何节点</b>。这 18 单 = <b>10/29 之后下单的 15 单</b>（即 15 单停单清单，全部命中）+ <b>3 单 10/29 之前的老单</b>（<span class="tn">USPDPPS2000057434N</span> 10/23、<span class="tn">USPDPPS6000057447N</span> 10/26、<span class="tn">USPDPPS5000057516N</span> 10/28）。你说的 15 单，就是砍掉这 3 单老单后的结果，口径对得上。</li>
<li>你给的清单里 <b>{len(instuck)} 单与 15 单停单清单完全一致，且这 {len(instuck_bad)} 单到今天仍未产生任何后续节点</b>：再发动作在系统里还没落成扫描。</li>
<li>{len(warn)} 单取消后同号重建面单（<span class="tn">USPDPPS1000057787N</span> 11-03 07:31 以同号生成新订单并 09:31 投递），说明"再发"如果走同号复用，取消节点会留在链首，不能只看"有没有取消"。</li>
<li>{len(ok)} 单取消后包裹仍从投递点离开（11-03 05:10 COLUS002），物理上在跑，<b>不应算停单</b>。</li>
</ul>
</div>
<div class="foot">生成脚本 gen_resend_report.py；明细 CSV：data/resend_check.csv</div>
</div></body></html>"""


if __name__ == "__main__":
    main()
