#!/usr/bin/env python
"""探测：书签脚本能否自己推出接口主机（避免把内网域名写进公开页面）。

思路：页面自己会发接口请求，performance.getEntriesByType('resource') 里就有完整 URL。
用法：python probe_discover.py [--wait 30]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

JS = """
() => {
  const out = {hosts: {}, samples: [], globals: []};
  const es = performance.getEntriesByType('resource') || [];
  for (const e of es) {
    try {
      const u = new URL(e.name);
      const k = u.origin + u.pathname.split('/').slice(0, 3).join('/');
      out.hosts[k] = (out.hosts[k] || 0) + 1;
    } catch (err) {}
  }
  out.total = es.length;
  out.samples = es.slice(0, 8).map(e => e.name.slice(0, 110));
  // 顺便看看全局有没有现成的配置对象
  for (const k of Object.keys(window)) {
    if (/config|__|api|base/i.test(k) && typeof window[k] === 'object' && window[k]) {
      out.globals.push(k);
    }
  }
  return out;
}
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=int, default=30, help="等 SPA 渲染并发出请求的秒数")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    from login_browser import PROFILE_DIR, ORDER_QUERY  # noqa: E402
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), channel="chrome",
            headless=not args.headed, args=["--no-proxy-server"],
            viewport={"width": 1366, "height": 860},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        api_calls = []

        def on_resp(r):
            if "/aquarius-order/" in r.url:
                api_calls.append(r.url)

        page.on("response", on_resp)
        print(f"打开 {ORDER_QUERY} ，等 {args.wait}s ...")
        page.goto(ORDER_QUERY, wait_until="commit", timeout=90000)
        page.wait_for_timeout(args.wait * 1000)

        info = page.evaluate(JS)
        print(f"\n资源条目 {info['total']} 条。按 host+前三段聚合：")
        for k, v in sorted(info["hosts"].items(), key=lambda x: -x[1])[:12]:
            mark = "  <== 接口" if "aquarius" in k else ""
            print(f"  {v:3d}  {k}{mark}")
        print("\n页面自己发出的接口请求（response 监听）：")
        uniq = Counter(re.sub(r"\?.*$", "", u) for u in api_calls)
        if not uniq:
            print("  （无，说明这页不会自动查数）")
        for u, c in list(uniq.items())[:6]:
            print(f"  {c:3d}  {u[:110]}")

        # 从资源条目里提取接口 origin
        hits = [k for k in info["hosts"] if "aquarius" in k]
        if hits:
            origin = hits[0].split("/")[0] + "//" + hits[0].split("/")[2]
            print(f"\n✓ 可自动推导接口主机：{origin}")
        else:
            print("\n✗ 资源条目里没有接口地址，需要别的发现方式")

        if info["globals"]:
            print(f"\n可探测的全局对象：{', '.join(info['globals'][:8])}")
        ctx.close()


if __name__ == "__main__":
    main()
