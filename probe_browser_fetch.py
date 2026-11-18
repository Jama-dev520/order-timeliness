#!/usr/bin/env python
"""在真实浏览器里验证「书签小工具（bookmarklet）本地取数」是否可行。

验证点：
  1. 从 oc-uk 页面上下文里 fetch 内部接口，CORS 是否放行（Python 测试不等于浏览器测试）
  2. 浏览器是否自动带上 hk-sit（脚本不碰 cookie）
  3. 并发翻页的真实耗时（决定「点一下书签」要不要等很久）
  4. Sec-Fetch-Site / Referer 等浏览器自动头会不会导致网关拒绝

用法：python probe_browser_fetch.py [--headed]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from login_browser import PROFILE_DIR, ORDER_QUERY  # noqa: E402

JS_FETCH = """
async ([base, start, end, limit, pages, concurrency]) => {
  const out = {steps: [], t0: performance.now()};
  const one = async (page) => {
    const u = new URL(base + '/aquarius-order/order/tracking-number/v1/tracking-number-list');
    u.searchParams.set('page', page);
    u.searchParams.set('limit', limit);
    u.searchParams.set('startDate', start);
    u.searchParams.set('endDate', end);
    u.searchParams.set('destinationCountry', 'US');
    const r = await fetch(u.toString(), {credentials: 'include'});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return await r.json();
  };

  // 1) 首页：同时验证鉴权与总数
  let d1;
  try { d1 = await one(1); } catch (e) { return {fatal: String(e)}; }
  out.steps.push({name: 'page1', ms: Math.round(performance.now() - out.t0)});
  out.success = d1.success;
  out.errorCode = d1.errorCode;
  out.total = (d1.result || {}).total;

  if (!d1.success) return out;

  // 2) 并发翻页
  const totalPages = Math.ceil(out.total / (limit === undefined ? 10 : limit));
  const toFetch = [];
  for (let p = 2; p <= Math.min(pages, totalPages); p++) toFetch.push(p);
  const t1 = performance.now();
  let got = (d1.result || {}).list ? d1.result.list.length : 0;
  let failed = 0;
  let idx = 0;
  const worker = async () => {
    while (idx < toFetch.length) {
      const p = toFetch[idx++];
      try { const d = await one(p); got += ((d.result || {}).list || []).length; }
      catch (e) { failed++; }
    }
  };
  await Promise.all(Array.from({length: concurrency}, worker));
  out.steps.push({name: 'pages2..' + Math.min(pages, totalPages),
                  ms: Math.round(performance.now() - t1), rows: got, failed: failed});
  out.totalMs = Math.round(performance.now() - out.t0);
  out.sampleKeys = Object.keys(((d1.result || {}).list || [])[0] || {});
  return out;
}
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    ap.add_argument("--pages", type=int, default=20, help="并发抓多少页")
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()

    if not ORDER_QUERY:
        sys.exit("缺少 OC_PORTAL / OC_SSO_URL")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",
            headless=not args.headed,
            args=["--no-proxy-server"],
            viewport={"width": 1366, "height": 860},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        print(f"打开 {ORDER_QUERY} ...")
        page.goto(ORDER_QUERY, wait_until="commit", timeout=90000)
        # SPA 渲染需要时间，但我们只要 origin 和 cookie，先等一下让网络空闲
        page.wait_for_timeout(3000)
        print(f"  当前 origin: {page.evaluate('location.origin')}")
        print(f"  cookie hk-sit 可见性: "
              f"{page.evaluate('document.cookie.includes(\"hk-sit\")')}（HttpOnly 时为 False，属正常）")

        base = page.evaluate("location.origin").rsplit("//", 1)[-1]
        base = "https://" + base.split("/")[0] if base else ""
        # 接口主机跟页面主机不一定相同，从 .env 取
        from scraper import FIRST_CFG, _host_of  # noqa: E402
        api_base = _host_of(FIRST_CFG).rstrip("/")
        print(f"  接口地址: {api_base}")

        args_js = [api_base, "2026/11/12 00:00:00", "2026/11/18 23:59:59",
                   10, args.pages, args.concurrency]
        t0 = time.time()
        try:
            res = page.evaluate(JS_FETCH, args_js)
        except Exception as e:  # noqa: BLE001
            print(f"✗ 页面内 fetch 抛错：{e}")
            ctx.close()
            return
        print(f"\n结果（墙钟 {time.time()-t0:.1f}s）:")
        brief = {k: v for k, v in res.items() if k != "sampleKeys"}
        print(json.dumps(brief, ensure_ascii=False, indent=2))
        if res.get("sampleKeys"):
            print(f"  list 单条字段数 = {len(res['sampleKeys'])}")
        ctx.close()


if __name__ == "__main__":
    main()
