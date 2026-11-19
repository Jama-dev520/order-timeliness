#!/usr/bin/env python
"""端到端测试：书签 → 抓数 → 看板窗口本地计算 → 渲染。

需要先起一个本地静态服务（默认 http://127.0.0.1:8123 指向构建产物），
用它代替 GitHub Pages，避免每次都要先部署。

用法：
    python build_site.py --out /tmp/site_e2e
    python -m http.server 8123 --directory /tmp/site_e2e
    python test_live_e2e.py --days 3
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))


def env(key: str, default: str = "") -> str:
    f = BASE / ".env"
    if not f.exists():
        return default
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return default


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3, help="取数窗口天数（测试用小的）")
    ap.add_argument("--dash", default="http://127.0.0.1:8123", help="看板地址（构建产物）")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--via-url", action="store_true",
                    help="按真实书签方式从站点加载 agent.js（默认是内联注入）")
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()

    from login_browser import PROFILE_DIR, ORDER_QUERY
    from playwright.sync_api import sync_playwright

    api = env("OC_HOST_US").rstrip("/")
    if not api:
        sys.exit("OC_HOST_US 为空")
    agent_js = (BASE / "live" / "agent.js").read_text(encoding="utf-8")

    logs: list[str] = []

    def mk_logger(tag: str):
        def _log(m):
            line = f"[{tag}:{m.type}] {m.text[:220]}"
            logs.append(line)
            if m.type in ("error", "warning") or "[oc-live]" in m.text:
                print("    " + line)
        return _log

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), channel="chrome",
            headless=not args.headed, args=["--no-proxy-server"],
            viewport={"width": 1366, "height": 860},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("console", mk_logger("agent"))
        page.on("pageerror", lambda e: logs.append(f"[agent:pageerror] {str(e)[:300]}"))

        # 新弹出的看板窗口从第一刻就接管日志（否则早期的加载失败会被漏掉）
        def on_new_page(p):
            p.on("console", mk_logger("dash"))
            p.on("pageerror", lambda e: logs.append(f"[dash:pageerror] {str(e)[:300]}"))
            p.on("requestfailed",
                 lambda r: logs.append(f"[dash:reqfail] {r.url[:120]} {r.failure}"))
            p.on("response",
                 lambda r: logs.append(f"[dash:404] {r.url[:120]}") if r.status >= 400 else None)

        ctx.on("page", on_new_page)

        print(f"[1] 打开内部页面 {ORDER_QUERY}")
        page.goto(ORDER_QUERY, wait_until="commit", timeout=90000)
        page.wait_for_timeout(2500)

        print(f"[2] 注入代理脚本（接口 {api} / 看板 {args.dash} / {args.days} 天）")
        t0 = time.time()
        page.evaluate(
            "([api]) => document.documentElement.setAttribute('data-oc-api', api)", [api])
        if args.via_url:
            # 与真实书签一致：从站点加载 agent.js，由它自己从 src 推导看板地址
            url = args.dash.rstrip("/") + f"/live/agent.js?v=3&days={args.days}"
            print(f"    via url: {url}")
            page.add_script_tag(url=url)
        else:
            page.evaluate(
                "([dash, days]) => { window.__OC_DASH = dash; window.__OC_DAYS = days; }",
                [args.dash, args.days])
            page.add_script_tag(content=agent_js)

        # 抓数期间靠页面上的面板文字看进度（agent.js 同时会打 [oc-live] 到 console）
        dash = None
        last = ""
        while time.time() - t0 < args.timeout:
            for p in ctx.pages:
                if p is not page and "live=1" in p.url:
                    dash = p
                    break
            if dash:
                break
            try:
                cur = page.evaluate(
                    "() => { var m=document.getElementById('__oc_msg'),"
                    " s=document.getElementById('__oc_sub');"
                    " if(!m) return '(面板未挂载)';"
                    " return m.textContent + (s && s.textContent ? ' | ' + s.textContent : ''); }")
            except Exception:  # noqa: BLE001
                cur = ""
            if cur and cur != last:
                print(f"    {int(time.time()-t0):3d}s  {cur}")
                last = cur
            time.sleep(0.5)
        if dash is None:
            print(f"✗ {args.timeout}s 内没弹出看板窗口")
            print("\n".join(logs[-25:]))
            ctx.close()
            sys.exit(1)

        print(f"[3] 看板窗口已打开 {dash.url}")

        print(f"[4] 等本地计算与渲染（超时 {args.timeout}s）")
        deadline = time.time() + args.timeout
        st = {}
        last = ""
        while time.time() < deadline:
            try:
                st = dash.evaluate("""() => {
                  var b=document.getElementById('__live_t'),
                      s=document.getElementById('__live_s');
                  return {all: (window.ALL||[]).length,
                          box: !!document.getElementById('__live_box'),
                          t: b ? b.textContent : '',
                          s: s ? s.textContent : ''};
                }""")
            except Exception:  # noqa: BLE001
                st = {}
            cur = f"ALL={st.get('all')} box={st.get('box')} {st.get('t','')} | {st.get('s','')}"
            if cur != last:
                print(f"    {int(time.time()-t0):3d}s  {cur[:220]}")
                last = cur
            if st.get("all"):
                break
            if "失败" in (st.get("t") or ""):
                break
            time.sleep(0.6)

        if not st.get("all"):
            print(f"✗ 渲染未完成：{st.get('t','')} | {st.get('s','')}")
            print("--- 日志 ---")
            print("\n".join(logs[-25:]))
            dash.screenshot(path=str(BASE / "data" / "_live_fail.png"))
            ctx.close()
            sys.exit(1)

        res = dash.evaluate("""() => ({
          orders: (window.ALL || []).length,
          cross: Object.keys(window.CROSS || {}).length,
          dropoff: Object.keys(window.DROPOFF || {}).length,
          lms: Object.keys(window.LMSSAME || {}).length,
          raw: Object.keys(window.RAW || {}).length,
          from: (window.META || {}).dateFrom,
          to: (window.META || {}).dateTo,
          rows: document.querySelectorAll('#tbody tr, table tbody tr').length,
          tz: (document.querySelector('#tzBar') || {}).children ?
              document.querySelector('#tzBar').children.length : 0
        })""")
        print("\n[5] 结果")
        print(json.dumps(res, ensure_ascii=False, indent=2))

        ok = res["orders"] > 0 and res["raw"] > 0 and res["rows"] > 0
        # KPI 区域有渲染出来才算真正跑通
        kpi = dash.evaluate("() => document.body.innerText.length")
        print(f"    页面文本长度 {kpi}")

        dash.screenshot(path=str(BASE / "data" / "_live_e2e.png"), full_page=False)
        print(f"    截图 -> data/_live_e2e.png")

        if logs:
            print("\n[日志摘录]")
            for line in logs[-12:]:
                print("  " + line)

        print("\n" + ("✓ 端到端跑通" if ok else "✗ 数据为空"))
        ctx.close()
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
