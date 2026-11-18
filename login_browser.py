#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SSO 登录助手：把 hk-sit 续上并写回 .env。

为什么不能纯脚本登录
--------------------
这个 SSO 是 LDAP 账号 + 图形验证码（4 位数字），另外还有飞书扫码入口。
本机试过三条验证码识别路线，都不行：
  · ddddocr      —— PyPI 上没有 Python 3.13 可用的包
  · Windows OCR  —— 对 120x40 的验证码 5 张全错（干扰线太重，返回空或乱码）
  · 自建模板     —— 数字字体带扭曲，孔洞特征分不开 2/3/5/7
所以保留"人看一眼验证码"这一步，其余（开浏览器、填账号、抓 cookie、写 .env、验证）全自动。

用法
----
    python login_browser.py            # 需要时弹浏览器：自动填账号密码，你输 4 位验证码
    python login_browser.py --check    # 只检查 .env 里的 cookie 还有效没
    python login_browser.py --force    # 强制重新登录（即使当前 cookie 还有效）
    python login_browser.py --refresh  # 登录后顺便跑一轮 refresh_all.py

让账号密码自动填充，两条路任选其一：
  1) 让 Chrome 自己记住（推荐，密码不落盘到 .env）
     弹窗登录时 Chrome 会问"是否保存密码"，点「保存」即可。下次自动填充。
     前提是走持久化配置目录 .pw-profile（本脚本已内置），保存的密码才会保留。
  2) 写进 .env（明文，最省事但会留下密码文件）
    OC_USER=你的域账号
    OC_PASS=密码

登录态保存在 data/_auth_state.json（含 session cookie，可跨浏览器重启复用）。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
ENV_FILE = BASE / ".env"
STATE_FILE = BASE / "data" / "_auth_state.json"
# 持久化浏览器配置目录：用 launch_persistent_context 打开，Chrome 的「保存密码」和
# 已登录会话都存在这里，下次弹窗时账号密码自动填充，人只需要输 4 位验证码。
# 有了它就不必把明文密码写进 .env。
PROFILE_DIR = BASE / ".pw-profile"
SSO_URL = "https://sso-portal-hk.orangeconnex.com/"
PORTAL_MAIN = "https://sso-portal-hk.orangeconnex.com/main"
APP_TITLE = "SPK-UK智慧物流管理系统"          # 门户里的应用卡片标题
ORDER_QUERY = "https://oc-uk.orangeconnex.com/orderManage/OrderComplexQuery"
COOKIE_NAME = "hk-sit"


# --------------------------------------------------------------------------
# .env 读写
# --------------------------------------------------------------------------
def read_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if not ENV_FILE.exists():
        return out
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def write_cookie(cookie: str) -> None:
    """只改 OC_COOKIE 一行，其余原样保留（注释、顺序都不动）。"""
    text = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
    line = f"OC_COOKIE={cookie}"
    if re.search(r"^OC_COOKIE=.*$", text, flags=re.M):
        text = re.sub(r"^OC_COOKIE=.*$", line.replace("\\", "\\\\"), text, count=1, flags=re.M)
    else:
        text = text.rstrip("\n") + "\n" + line + "\n"
    ENV_FILE.write_text(text, encoding="utf-8")


def env_credentials() -> tuple[str, str]:
    env = read_env()
    user = env.get("OC_USER") or env.get("OC_USERNAME") or ""
    pwd = env.get("OC_PASS") or env.get("OC_PASSWORD") or ""
    return user, pwd


# --------------------------------------------------------------------------
# cookie 校验 / 存取
# --------------------------------------------------------------------------
def verify_cookie(cookie: str) -> tuple[bool, str]:
    """拿 cookie 打一次列表接口：success=false 就是失效了。

    固定用 **US 主机** 校验：`OC_REGION=all` 时 FIRST_CFG 是 UK，拿它验会报出 UK 的单量，
    和后台 US tab 看到的数字对不上，容易误判成"数据不对"。
    """
    try:
        import requests

        import scraper as sc
    except Exception as exc:  # noqa: BLE001
        return False, f"依赖缺失：{exc}"

    if not cookie:
        return False, "cookie 为空"
    cfg = sc.REGIONS.get("us") or sc.FIRST_CFG
    today = datetime.now(sc.TZ_CN).strftime("%Y/%m/%d")
    url = cfg["host"] + sc.API_PREFIX + sc.API["list"]
    params = {"page": 1, "limit": 1,
              "startDate": f"{today} 00:00:00", "endDate": f"{today} 23:59:59"}
    if cfg.get("dest"):
        params["destinationCountry"] = cfg["dest"]
    s = requests.Session()
    s.trust_env = False          # 别走企业代理，否则连自己也 502
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Cookie": cookie,
    })
    try:
        r = s.get(url, params=params, timeout=40)
        r.raise_for_status()
        d = r.json()
    except Exception as exc:  # noqa: BLE001
        return False, f"请求失败：{exc}"
    if d.get("success") is False:
        return False, f"接口拒绝：{d.get('errorCode')} {d.get('errorMsg') or ''}".strip()
    total = int((d.get("result") or {}).get("total") or 0)
    return True, f"US 接口正常（今天 {total} 单）"


def cookie_from_state() -> str:
    """从上次保存的浏览器登录态里取 hk-sit（不需要开浏览器）。"""
    if not STATE_FILE.exists():
        return ""
    try:
        d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    for c in d.get("cookies") or []:
        if c.get("name") == COOKIE_NAME and c.get("value"):
            return f"{COOKIE_NAME}={c['value']}"
    return ""


# --------------------------------------------------------------------------
# 进入应用：门户 → SPK-UK智慧物流 → 订单综合查询 → US 区查一次
# --------------------------------------------------------------------------
# 为什么登录后还要走这一段：
#   SSO 通过后就下发 hk-sit，接口凭它就能取数 —— "能调接口"不等于"人能用"。
#   走一遍真实路径能顺带验证：门户能进、应用能开、US 区能查到单。
#   任何一步失败只报警、不阻断（cookie 已经用接口验过了）。
def enter_app(ctx, page, verbose: bool = True) -> tuple[bool, str]:
    def say(msg: str) -> None:
        if verbose:
            print("  " + msg)

    # 1) 门户首页
    try:
        if "/main" not in page.url:
            page.goto(PORTAL_MAIN, wait_until="commit", timeout=60000)
        page.wait_for_selector(".app-card-title", timeout=30000)
    except Exception as exc:  # noqa: BLE001
        return False, f"门户打不开（{type(exc).__name__}）"
    say(f"门户已打开：{page.url}")

    # 2) 点「SPK-UK智慧物流管理系统」
    try:
        card = page.locator(".app-card-title", has_text=APP_TITLE).first
        if card.count() == 0:
            return False, f"门户里没有「{APP_TITLE}」这张卡片"
        n_before = len(ctx.pages)
        card.click()
    except Exception as exc:  # noqa: BLE001
        return False, f"点应用卡片失败（{type(exc).__name__}）"

    target = page
    deadline = 25
    while deadline > 0:
        if len(ctx.pages) > n_before:
            target = ctx.pages[-1]
            break
        page.wait_for_timeout(800)
        deadline -= 1
    if target is page and "oc-" not in page.url:
        # 同 tab 跳转时给一点时间
        page.wait_for_timeout(3000)
        target = page
    say(f"应用已打开：{target.url.split('?')[0][:80]}")

    # 3) 订单综合查询 → 切 US → 搜索
    #    应用是 SPA，首次进入要下 5MB 主包；新开的 tab 还可能正在初始化，
    #    所以 goto 失败就 reload 一次，selector 给足 90 秒。
    try:
        target.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:  # noqa: BLE001
        pass
    reached = False
    for attempt in (1, 2):
        try:
            target.goto(ORDER_QUERY, wait_until="commit", timeout=90000)
            target.wait_for_selector("label.ivu-radio-wrapper", timeout=90000)
            reached = True
            break
        except Exception as exc:  # noqa: BLE001
            say(f"第 {attempt} 次打开查询页失败：{type(exc).__name__}")
            try:
                target.reload(wait_until="commit", timeout=60000)
            except Exception:  # noqa: BLE001
                pass
    if not reached:
        snippet = ""
        try:
            snippet = (target.inner_text("body") or "")[:120].replace("\n", " ")
        except Exception:  # noqa: BLE001
            pass
        return False, f"订单综合查询页打不开（当前 {target.url.split('?')[0][:60]}｜{snippet}）"

    try:
        lab = target.locator("label.ivu-radio-wrapper").filter(has_text="US").first
        lab.click()
        target.wait_for_timeout(2500)
        target.locator("button", has_text="搜").first.click()
    except Exception as exc:  # noqa: BLE001
        return False, f"切 US / 点查询失败（{type(exc).__name__}）"

    rows = 0
    for _ in range(25):
        rows = target.locator("tbody tr").count()
        if rows > 0:
            break
        target.wait_for_timeout(1000)
    if rows == 0:
        return False, "US 区查询返回 0 行（可能是当天还没单）"
    return True, f"US 区查询正常，当前页 {rows} 行"


# --------------------------------------------------------------------------
# 浏览器登录
# --------------------------------------------------------------------------
def login_with_browser(user: str, pwd: str, wait_seconds: int = 300, enter: bool = True) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺少 playwright：python -m pip install playwright -i https://pypi.org/simple")
        return ""

    print("打开浏览器…（窗口里会自动填好账号密码，你只需要输 4 位验证码）")
    with sync_playwright() as pw:
        # --no-proxy-server 是必须的：环境里若有 HTTP(S)_PROXY（沙箱代理、抓包工具等），
        # Chrome 会优先走它，而那个代理多半连不上 SSO，表现为 goto 60 秒超时。
        # 用 persistent context（固定 user_data_dir）而不是 new_context：
        # 只有前者会让 Chrome 的密码库/会话落盘，下次才能自动填充账号密码。
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",
            headless=False,
            args=["--start-maximized", "--no-proxy-server"],
            no_viewport=True,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            # 不能用 domcontentloaded：SSO 页要等飞书二维码 CDN，而那个 CDN 的证书在这台机器上
            # 校验失败（ERR_CERT_COMMON_NAME_INVALID），DOMContentLoaded 迟迟不触发 → 60 秒超时。
            # 改用 commit 先把文档拿下来，再显式等登录框出现。
            try:
                page.goto(SSO_URL, wait_until="commit", timeout=45000)
            except Exception as exc:  # noqa: BLE001
                print(f"  首页提交超时（{type(exc).__name__}），reload 重试一次")
                page.reload(wait_until="commit", timeout=45000)
            try:
                page.wait_for_selector(
                    'input[placeholder="用户名"], input[placeholder="验证码"]',
                    timeout=40000, state="attached")
            except Exception:  # noqa: BLE001
                print("  登录框 40 秒内没出现，继续等（窗口里可以手动刷新一下）")
            page.wait_for_timeout(1200)

            def current_cookie() -> str:
                for c in ctx.cookies():
                    if c.get("name") == COOKIE_NAME and c.get("value"):
                        return f"{COOKIE_NAME}={c['value']}"
                return ""

            cookie = current_cookie()
            if not cookie:
                # 自动填账号密码；验证码留给人
                filled = []
                for sel, val in (('input[placeholder="用户名"]', user),
                                 ('input[placeholder="密码"]', pwd)):
                    if not val:
                        continue
                    try:
                        page.fill(sel, val, timeout=8000)
                        filled.append(sel)
                    except Exception:  # noqa: BLE001
                        pass
                if filled:
                    print(f"已自动填入 {len(filled)} 项（账号/密码），请输验证码后点「登 录」")
                    try:
                        page.click('input[placeholder="验证码"]', timeout=5000)
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    print("没读到 OC_USER / OC_PASS，请在窗口里手动登录（账号密码或飞书扫码都行）")

            deadline = datetime.now().timestamp() + wait_seconds
            last_tip = 0
            while datetime.now().timestamp() < deadline:
                cookie = current_cookie()
                if cookie:
                    print("登录成功（SSO 已下发 hk-sit）")
                    # 按真实路径进一遍应用：门户 → SPK-UK智慧物流 → 订单综合查询 → US
                    if enter:
                        print("正在进应用核对（门户 → SPK-UK智慧物流 → 订单综合查询 → US）…")
                        ok, msg = enter_app(ctx, page)
                        print(("  ✓ " if ok else "  ! ") + msg)
                        if not ok:
                            print("  （不影响取数：cookie 已能用接口验过，只是页面走查没通过）")
                    STATE_FILE.parent.mkdir(exist_ok=True)
                    ctx.storage_state(path=str(STATE_FILE))
                    return cookie
                page.wait_for_timeout(1000)
                left = int(deadline - datetime.now().timestamp())
                if left <= 30 and left != last_tip and left % 10 == 0:
                    print(f"  还有 {left} 秒…（登录成功会自动继续）")
                    last_tip = left
            print(f"{wait_seconds} 秒内没检测到登录，退出。")
            return ""
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def check_app() -> int:
    """用已保存的登录态走一遍应用页面（不开登录窗口），验证人能用。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺少 playwright：python -m pip install playwright -i https://pypi.org/simple")
        return 2
    if not STATE_FILE.exists():
        print("还没有登录态文件，先跑一次 python login_browser.py")
        return 2
    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome", headless=True)
        ctx = b.new_context(storage_state=str(STATE_FILE), viewport={"width": 1600, "height": 950})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            ok, msg = enter_app(ctx, page)
            print(("✓ " if ok else "✗ ") + msg)
            ctx.storage_state(path=str(STATE_FILE))
            return 0 if ok else 1
        finally:
            try:
                ctx.close()
                b.close()
            except Exception:  # noqa: BLE001
                pass


def main() -> int:
    ap = argparse.ArgumentParser(description="SSO 登录助手（续 hk-sit）")
    ap.add_argument("--check", action="store_true", help="只检查当前 cookie")
    ap.add_argument("--enter", action="store_true", help="只走一遍应用页面（门户→SPK-UK智慧物流→US查询）")
    ap.add_argument("--no-enter", action="store_true", help="登录后不进应用，只取 cookie")
    ap.add_argument("--force", action="store_true", help="强制重新登录")
    ap.add_argument("--refresh", action="store_true", help="登录后跑一轮 refresh_all.py")
    ap.add_argument("--wait", type=int, default=300, help="等人工登录的秒数，默认 300")
    args = ap.parse_args()

    if args.enter:
        return check_app()

    env = read_env()
    cookie = (env.get("OC_COOKIE") or "").strip()

    if args.check:
        ok, msg = verify_cookie(cookie)
        print(("✓ 有效：" if ok else "✗ 失效：") + msg)
        return 0 if ok else 1

    # 1) 现有 .env 里的 cookie 还有效就直接用，不开浏览器
    if cookie and not args.force:
        ok, msg = verify_cookie(cookie)
        if ok:
            print(f"✓ .env 里的 cookie 还有效（{msg}），不用重新登录")
        else:
            print(f"✗ .env 里的 cookie 已失效（{msg}）")
            cookie = ""
    elif args.force:
        cookie = ""

    # 2) 失效时先试上次保存的浏览器登录态
    if not cookie:
        cached = cookie_from_state()
        if cached:
            ok, msg = verify_cookie(cached)
            if ok:
                print(f"✓ 用上次保存的浏览器登录态恢复成功（{msg}）")
                cookie = cached
            else:
                print("上次保存的登录态也过期了")

    # 3) 还不行就开浏览器让人登录
    if not cookie:
        user, pwd = env_credentials()
        if not user and not pwd:
            print("\n提示：.env 里没有 OC_USER / OC_PASS，这次需要你在窗口里手动登录。")
            print("      登录时 Chrome 若问「是否保存密码」，点【保存】——")
            print("      以后账号密码会自动填充（存在 .pw-profile 里），只剩 4 位验证码要人输。")
            print("      也可以改成在 .env 里写 OC_USER / OC_PASS（明文）。\n")
        cookie = login_with_browser(user, pwd, args.wait, enter=not args.no_enter)
        if not cookie:
            return 2
        ok, msg = verify_cookie(cookie)
        if not ok:
            print(f"✗ 拿到 cookie 但接口仍不通：{msg}")
            return 3
        print(f"✓ 新 cookie 验证通过（{msg}）")

    # 4) 写回 .env（只在内容变化时写）
    if (env.get("OC_COOKIE") or "").strip() != cookie:
        write_cookie(cookie)
        print("✓ 已更新 .env 的 OC_COOKIE")
    else:
        print("· .env 里就是这条，无需改动")

    # 5) 可选：刷一轮数据
    if args.refresh:
        print("\n跑一轮 refresh_all.py …")
        rc = subprocess.call([sys.executable, str(BASE / "refresh_all.py")], cwd=str(BASE))
        print(f"refresh_all 退出码 {rc}")
        return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
