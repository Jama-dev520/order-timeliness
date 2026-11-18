#!/usr/bin/env python
"""探测 OC 接口的 CORS 策略：判断「浏览器端直连抓数」是否可行。

测三件事：
  1. GET 带 Origin（模拟 oc-uk 页面里发 XHR），看响应有没有 access-control-allow-origin
  2. OPTIONS 预检（带 Access-Control-Request-Method/Headers），看网关是否放行
  3. 不带任何凭证时的返回，确认接口确实要求 hk-sit

用法：python probe_cors.py [--origin https://<后台门户>]   # 默认取 .env 的 OC_PORTAL
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent


def _env(key: str, default: str = "") -> str:
    env_file = BASE / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    return default


def _show(tag: str, r: requests.Response) -> None:
    cors = {k: v for k, v in r.headers.items() if k.lower().startswith("access-control")}
    print(f"\n=== {tag} ===")
    print(f"  HTTP {r.status_code}")
    if cors:
        for k, v in cors.items():
            print(f"  {k}: {v}")
    else:
        print("  (无任何 Access-Control-* 响应头)")
    body = r.text[:200].replace("\n", " ")
    print(f"  body[:200]: {body}")


def main() -> None:
    ap = argparse.ArgumentParser()
    # 默认取 .env 里的后台门户地址：仓库要能公开，源码里不能写死内部域名
    ap.add_argument("--origin", default=_env("OC_PORTAL").rstrip("/"))
    args = ap.parse_args()
    if not args.origin:
        sys.exit("缺少 --origin：请在 .env 配 OC_PORTAL，或命令行显式指定")

    host = _env("OC_HOST_US").rstrip("/")
    portal = _env("OC_PORTAL")
    cookie = _env("OC_COOKIE")
    if not host:
        sys.exit("缺少 OC_HOST_US")

    url = host + "/aquarius-order/order/tracking-number/v1/tracking-number-list"
    params = {"page": 1, "limit": 1, "startDate": "2026-11-17", "endDate": "2026-11-18",
              "destinationCountry": "US"}
    base_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Origin": args.origin,
    }
    if portal:
        base_headers["Referer"] = f"{portal}/orderManage/OrderComplexQuery"

    s = requests.Session()
    s.trust_env = False

    # 1) 带凭证 + 带 Origin 的真实 GET
    h = dict(base_headers)
    if cookie:
        h["Cookie"] = cookie
    try:
        _show("1. GET  with cookie + Origin", s.get(url, params=params, headers=h, timeout=40))
    except Exception as e:  # noqa: BLE001
        print(f"\n=== 1. GET 失败：{e}")

    # 2) OPTIONS 预检
    pre = dict(base_headers)
    pre["Access-Control-Request-Method"] = "GET"
    pre["Access-Control-Request-Headers"] = "content-type,accept"
    try:
        _show("2. OPTIONS preflight", s.options(url, headers=pre, timeout=40))
    except Exception as e:  # noqa: BLE001
        print(f"\n=== 2. OPTIONS 失败：{e}")

    # 3) 无凭证
    try:
        r = s.get(url, params=params, headers=base_headers, timeout=40)
        _show("3. GET  without cookie", r)
        try:
            d = r.json()
            print(f"  success={d.get('success')} errorCode={d.get('errorCode')}")
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001
        print(f"\n=== 3. 无凭证 GET 失败：{e}")


if __name__ == "__main__":
    main()
