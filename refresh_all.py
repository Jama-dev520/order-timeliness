#!/usr/bin/env python3
"""定时刷新入口：抓订单 → 重算取消清单。

K8S 里由 CronJob 定时跑（见 k8s/oc-timeliness.yaml），本地也能直接执行：

    python refresh_all.py

三条保护，避免定时任务把线上数据搞坏：

1. **没登录态直接退出**，不去碰已有快照；
2. **抓到 0 单不覆盖**（Cookie 过期、接口变更、网络不通都长这样），
   旧快照继续服务，退出码非 0 便于配告警；
3. **订单抓成功才跑取消清单**，避免把好清单写成空。

退出码：0 成功 / 2 缺 Cookie / 3 抓取异常 / 4 抓到 0 单。
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def _apply_dotenv() -> None:
    """以 `.env` 为准覆盖 `OC_*` 环境变量。

    坑：本脚本多半由 serve.py 拉起，而 serve.py 是几小时前启动的——那时 scraper 已经把
    `.env` 读进 os.environ，之后改 `.env` 不会回灌。子进程继承的是**旧值**，
    结果「刷新数据」按几小时前的区域/日期去抓（实测踩过：.env 已是 us + 10/19，
    刷新却抓回 UK+US、10/29 起）。这里先纠正一次，子进程再继承就都是新值。
    容器里没有 `.env` 时不动，沿用 ConfigMap。
    """
    f = BASE / ".env"
    if not f.exists():
        return
    changed = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k.startswith("OC_") and os.environ.get(k) != v:
            old = os.environ.get(k)
            # 凭证只写「已更新」，不进日志
            if any(s in k for s in ("COOKIE", "PASS", "TOKEN", "SECRET")):
                changed.append(f"{k}: 已按 .env 更新")
            else:
                changed.append(f"{k}: {old!r} -> {v!r}")
            os.environ[k] = v
    if changed:
        log("按 .env 纠正继承来的环境变量：" + "；".join(changed))


_apply_dotenv()          # 必须在 import scraper 之前：scraper 在导入时按环境变量定常量

import scraper as sc  # noqa: E402


def window() -> tuple[str, str]:
    """取数窗口，与 serve.py 保持一致：配了 OC_START_DATE 就固定起点。"""
    start_date = (os.getenv("OC_START_DATE") or "").strip()
    days = int(os.getenv("OC_DAYS", "14"))
    if start_date:
        return (sc._norm_date(start_date),
                datetime.now(sc.TZ_CN).strftime("%Y/%m/%d 23:59:59"))
    return sc._range(days, None, None)


def main() -> int:
    if not sc.COOKIE:
        log("缺少 OC_COOKIE，无法抓取，保留旧快照")
        return 2

    start, end = window()
    log(f"抓取窗口 {start} ~ {end}")

    # 已妥投超过 N 天的单不再拉事件：妥投是终态，事件不会再变，直接复用上一轮快照。
    # 设 0 可关闭（整轮全抓）。实测这类单约占 15%，而事件请求是耗时主体。
    try:
        skip_days = int(os.getenv("OC_SKIP_DELIVERED_DAYS", "3"))
    except ValueError:
        skip_days = 3
    if skip_days > 0:
        log(f"已妥投超 {skip_days} 天的单将跳过事件请求（OC_SKIP_DELIVERED_DAYS=0 可关闭）")

    try:
        orders = sc.fetch_real(start, end, 0, skip_delivered_days=skip_days,
                               dump_events=True)
    except Exception as exc:  # noqa: BLE001
        # 后台对 startDate 有下限（实测 2026/10/19，约「今天往前 17 天」）。写得更早时不仅不给数据，
        # 还会静默返回全库；scraper 的守卫会抛错，这里自动收缩窗口重试一次，避免定时任务直接挂掉。
        if "忽略日期条件" in str(exc):
            fb = (datetime.now(sc.TZ_CN) - timedelta(days=16)).strftime("%Y/%m/%d 00:00:00")
            log(f"起始日 {start} 超出接口下限，本次改用 {fb} 重试（明细起点会自动收缩）")
            start = fb
            try:
                orders = sc.fetch_real(start, end, 0, skip_delivered_days=skip_days,
                                       dump_events=True)
            except Exception as exc2:  # noqa: BLE001
                log(f"抓取异常：{exc2}")
                return 3
        else:
            log(f"抓取异常：{exc}")
            return 3

    if not orders:
        log("抓到 0 单，判定为登录态失效或接口异常，**不覆盖**已有快照")
        return 4

    meta = sc.build_meta(start, end, orders, demo=False)
    sc.save(orders, meta)
    log(f"订单快照已更新：{len(orders)} 单")

    # 取消清单：订单刚抓成功说明登录态正常，这步失败不影响主流程
    # 下面三个清单原本各自重拉一遍列表+事件（等于整轮抓 4 遍），现在统一复用
    # 刚才 fetch_real 落下的 data/_ev_run.json —— 同一份事件，口径完全一致。
    ev_cache = BASE / "data" / "_ev_run.json"
    cache_args = ["--cache", str(ev_cache)] if ev_cache.exists() else []
    if not cache_args:
        log("警告：事件快照缺失，下游脚本将自行重拉事件（会明显变慢）")

    env = dict(os.environ)
    env.setdefault("STUCK_HTML_OUT", str(BASE / "data" / "stuck_shipcc.html"))
    cmd = [sys.executable, str(BASE / "stuck_shipcc.py"),
           "--from", start[:10], "--to", end[:10]] + cache_args
    log("重算取消清单 …")
    rc = subprocess.run(cmd, cwd=str(BASE), env=env).returncode
    if rc != 0:
        log(f"取消清单重算失败（退出码 {rc}），订单快照不受影响")

    # 串场检测：PUDO 点投递 → LMS 司机取走。失败只跳过这一项
    log("重算串场单（PUDO 投递 → LMS 取件）…")
    rc = subprocess.run([sys.executable, str(BASE / "cross_vendor.py"),
                         "--from", start[:10], "--to", end[:10]] + cache_args,
                        cwd=str(BASE), env=env).returncode
    if rc != 0:
        log(f"串场检测失败（退出码 {rc}），其余数据不受影响")

    # 投递取消异常：已投递（FIRST_SCAN）却被取消（SHIPCCUS001）的订单
    log("重算投递取消异常清单 …")
    env.setdefault("DROPOFF_HTML_OUT", str(BASE / "data" / "dropoff_cancel.html"))
    rc = subprocess.run([sys.executable, str(BASE / "dropoff_cancel.py"),
                         "--from", start[:10], "--to", end[:10]] + cache_args,
                        cwd=str(BASE), env=env).returncode
    if rc != 0:
        log(f"投递取消异常清单重算失败（退出码 {rc}），其余数据不受影响")

    # LMS 同供：建单(OCUS001)与投递(DOUS002/DOUD002)的 vendorCode 都是 LM。
    # 读的是刚写好的 orders.json，事件优先取本轮快照，只补快照里没有的单
    log("重算 LMS 同供标记 …")
    lms_cmd = [sys.executable, str(BASE / "lms_same_vendor.py")]
    if ev_cache.exists():
        lms_cmd += ["--share", str(ev_cache)]
    rc = subprocess.run(lms_cmd, cwd=str(BASE), env=env).returncode
    if rc != 0:
        log(f"LMS 同供标记重算失败（退出码 {rc}），其余数据不受影响")

    # 本地静态看板：单文件 HTML，双击就能看，不依赖 serve.py / 隧道
    log("生成本地静态看板 dashboard.html …")
    rc = subprocess.run([sys.executable, str(BASE / "dashboard.py")],
                        cwd=str(BASE), env=env).returncode
    if rc != 0:
        log(f"静态看板生成失败（退出码 {rc}），服务端看板不受影响")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
