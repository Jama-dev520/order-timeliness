#!/usr/bin/env python
"""验证「浏览器内跑 Python」在当前网络下是否可行（方案 B 的第 0 步）。

检查：
  1. 页面是否允许动态注入外部脚本（无 CSP 才行）
  2. 各 CDN 能否拉到 Pyodide 运行时，耗时与体积
  3. 加载完成后能否真的执行 Python 代码

用法：python probe_pyodide.py [--headed] [--url <页面地址>]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

# 候选 CDN（按优先级）
CDNS = [
    ("jsdelivr", "https://cdn.jsdelivr.net/pyodide/v0.26.4/full/"),
    ("unpkg", "https://unpkg.com/pyodide@0.26.4/"),
    ("cdnjs", "https://cdnjs.cloudflare.com/ajax/libs/pyodide/0.26.4/"),
]

JS_CSP_PROBE = """
() => {
  // 无 CSP 时，内联脚本与外部脚本都放行
  const meta = document.querySelector('meta[http-equiv="Content-Security-Policy"]');
  return {
    headerCsp: null,  // 响应头在 Python 侧看
    metaCsp: meta ? meta.getAttribute('content') : null,
    origin: location.origin,
    ua: navigator.userAgent.slice(0, 60),
  };
}
"""

JS_SPEED = """
async ([url, bytesWanted]) => {
  const t0 = performance.now();
  try {
    const ctl = new AbortController();
    const r = await fetch(url, {signal: ctl.signal, credentials: 'omit'});
    if (!r.ok) return {ok: false, status: r.status, ms: Math.round(performance.now()-t0)};
    // 只读到指定字节数，避免浪费流量
    const reader = r.body.getReader();
    let n = 0;
    while (n < bytesWanted) {
      const {done, value} = await reader.read();
      if (done) break;
      n += value.length;
    }
    ctl.abort();
    const ms = performance.now() - t0;
    return {ok: true, status: r.status, bytes: n, ms: Math.round(ms),
            mbps: +(n / 1e6 / (ms / 1000)).toFixed(2)};
  } catch (e) {
    return {ok: false, err: String(e).slice(0, 120), ms: Math.round(performance.now()-t0)};
  }
}
"""

JS_LOAD_PYODIDE = """
async ([indexUrl]) => {
  const t0 = performance.now();
  const logs = [];
  try {
    // 动态注入 loader
    await new Promise((res, rej) => {
      const s = document.createElement('script');
      s.src = indexUrl + 'pyodide.js';
      s.onload = res;
      s.onerror = () => rej(new Error('pyodide.js 加载失败（被拦截或网络不通）'));
      document.head.appendChild(s);
      setTimeout(() => rej(new Error('pyodide.js 10 秒超时')), 10000);
    });
    logs.push({step: 'loader', ms: Math.round(performance.now() - t0)});

    const t1 = performance.now();
    const py = await globalThis.loadPyodide({indexURL: indexUrl});
    logs.push({step: 'runtime', ms: Math.round(performance.now() - t1)});

    const t2 = performance.now();
    const out = await py.runPythonAsync(`
import sys, json, gzip, hashlib, datetime
json.dumps({'a': 1})
gzip.compress(b'x' * 1000)
hashlib.sha256(b'y').hexdigest()[:8]
sys.version.split()[0]
`);
    logs.push({step: 'runPython', ms: Math.round(performance.now() - t2)});
    return {ok: true, python: out, logs: logs,
            totalMs: Math.round(performance.now() - t0)};
  } catch (e) {
    return {ok: false, err: String(e).slice(0, 200), logs: logs,
            totalMs: Math.round(performance.now() - t0)};
  }
}
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--url", default="", help="默认用登录后的订单查询页")
    args = ap.parse_args()

    from login_browser import PROFILE_DIR, ORDER_QUERY  # noqa: E402

    url = args.url or ORDER_QUERY
    if not url:
        sys.exit("缺少 OC_PORTAL / OC_SSO_URL，或用 --url 指定一个页面")

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
        print(f"打开 {url} ...")
        page.goto(url, wait_until="commit", timeout=90000)
        page.wait_for_timeout(3000)

        info = page.evaluate(JS_CSP_PROBE)
        print(f"  origin: {info['origin']}")
        print(f"  meta CSP: {info['metaCsp'] or '(无)'}")

        # 1) 各 CDN 的下载速度（只读 2MB）
        print("\n[1] CDN 连通性与带宽（各读前 2MB）:")
        reachable = []
        for name, base in CDNS:
            r = page.evaluate(JS_SPEED, [base + "pyodide.asm.wasm", 2_000_000])
            if r.get("ok"):
                print(f"  {name:9s} 200 OK  {r['bytes']/1e6:.1f}MB {r['ms']/1000:.1f}s  {r['mbps']}MB/s")
                reachable.append((name, base))
            else:
                print(f"  {name:9s} 失败: {r.get('err') or r.get('status')}")
        if not reachable:
            print("\n✗ 所有 CDN 都不通 → 方案 B 必须把运行时装进仓库或本机")
            ctx.close()
            return

        # 2) 真加载 Pyodide 并跑 Python
        name, base = reachable[0]
        print(f"\n[2] 用 {name} 真正加载 Pyodide（首次约 10MB）:")
        t0 = time.time()
        res = page.evaluate(JS_LOAD_PYODIDE, [base])
        print(f"  墙钟 {time.time()-t0:.1f}s")
        if res.get("ok"):
            print(f"  ✓ Python {res['python']} 就绪，可跑 json/gzip/hashlib")
            for lg in res["logs"]:
                print(f"    {lg['step']:11s} {lg['ms']/1000:.1f}s")
            print(f"   合计 {res['totalMs']/1000:.1f}s")
        else:
            print(f"  ✗ 失败: {res.get('err')}")
            print(json.dumps(res.get("logs"), ensure_ascii=False))

        # 3) 页面本身没被搞坏
        alive = page.evaluate("() => !!document.body && document.body.innerText.length > 0")
        print(f"\n[3] 原页面仍可交互: {alive}")
        ctx.close()


if __name__ == "__main__":
    main()
