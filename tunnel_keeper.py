#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公网通道保活：本地服务 + Cloudflare 快速隧道，一直在线。

为什么需要它
------------
Cloudflare **快速隧道**（trycloudflare.com）有两个特性：
  1. 每次启动随机换一个域名，**不能指定、也不能复用**；
  2. 域名和 cloudflared 进程绑定，进程一死，旧链接立刻失效（Cloudflare 侧 530/1033）。
另外这台机器 UDP 7844 不通（必须 --protocol http2），而 DNS 解析出来的边缘节点
有时连不上，会一直握手超时。

所以这个脚本干四件事，不做别的：
  1. 保证本地 serve.py 在跑（端口 8000），挂了 8 秒后重启；
  2. 保证 cloudflared 在跑；连不上边缘节点就换一个（换域名）再来；
  3. 把当前生效的域名写进 logs/public_url.txt（历史在 logs/public_url_history.log）；
  4. **顺带管数据**：定时验 hk-sit，失效就弹浏览器续期（人只输 4 位验证码），
     cookie 正常但数据旧了就自动跑 refresh_all.py —— 不用人每天手动跑。

用法：
    python tunnel_keeper.py            # 前台常驻（推荐，Ctrl+C 退出）
    python tunnel_keeper.py --once     # 只跑一次体检
    python tunnel_keeper.py --no-data  # 只管通道，不验 cookie、不刷数据
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
LOGS = BASE / "logs"
CLOUDFLARED = BASE / "bin" / "cloudflared.exe"
URL_FILE = LOGS / "public_url.txt"
HIST_FILE = LOGS / "public_url_history.log"
TUNNEL_LOG = LOGS / "tunnel.log"
SERVE_LOG = LOGS / "serve.log"

PORT = 8000
TOKEN = os.getenv("OC_DASHBOARD_TOKEN", "IElsInhkviyy")
# 日志里出现过的两种形态："|  https://xxx.trycloudflare.com   |" 和裸域名
URL_RE = re.compile(r"https://(?!api\.|tunnel\.|www\.|update\.)[a-z0-9][a-z0-9-]*\.trycloudflare\.com")
METRICS_RE = re.compile(r"Starting metrics server on (127\.0\.0\.1:\d+)/metrics")
# 公网探测默认关闭：这台机器从内部访问自己的 trycloudflare 域名几乎必然 TLS 超时，
# 结果只能当参考，绝不能当"隧道死了"的判据。
PUBLIC_PROBE = os.getenv("OC_PUBLIC_PROBE", "0") == "1"
# 重建隧道 = 永久丢掉当前域名。所以"不就绪"要长时间容忍：
# cloudflared 自己有内建重连，多数抖动 10~30 秒就恢复，宁可等也不要换域名。
# 20s × 15 = 5 分钟仍未就绪，才认定进程假死并重建。
READY_FAIL_LIMIT = 15

# ---------------------------------------------------------------------------
# 数据守护：cookie 体检 + 定时刷新（不用人每天记得跑两个脚本）
#   1. 每 COOKIE_TICK 秒验一次 .env 里的 hk-sit（只打一次列表接口，约 1 秒）；
#   2. 失效 → 弹浏览器续期，人只输 4 位验证码，成功后下一轮自动刷一轮数据；
#   3. 有效但 data/orders.json 旧于 REFRESH_MAX_AGE → 自动跑 refresh_all.py（约 3 分钟）。
# 全部关掉：OC_KEEPER_DATA=0；节奏都可以用环境变量调（见 .env.example）。
# 子进程都挂在本进程下异步跑，不阻塞隧道保活的那 20 秒心跳。
# ---------------------------------------------------------------------------
DATA_GUARD = os.getenv("OC_KEEPER_DATA", "1") != "0"
COOKIE_TICK = int(os.getenv("OC_COOKIE_TICK", "1800"))          # 30 分钟验一次
REFRESH_MAX_AGE = int(os.getenv("OC_REFRESH_MAX_AGE", "3600"))  # 数据超 60 分钟就刷
LOGIN_COOLDOWN = int(os.getenv("OC_LOGIN_COOLDOWN", "1800"))    # 续期失败后 30 分钟内不重复弹窗
REFRESH_TIMEOUT = int(os.getenv("OC_REFRESH_TIMEOUT", "1200"))  # 刷新最多跑 20 分钟
LOGIN_TIMEOUT = int(os.getenv("OC_LOGIN_TIMEOUT", "900"))       # 登录窗最多等 15 分钟
DATA_LOG = LOGS / "data.log"
DATA_STATE = LOGS / "data_status.json"
ORDERS_FILE = BASE / "data" / "orders.json"


_stop = False


def _on_sig(*_a):
    global _stop
    _stop = True


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def local_ok() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/healthz", timeout=8) as r:
            return r.status == 200
    except Exception:
        return False


def public_ok(url: str, timeout: int = 20) -> bool:
    """探测公网地址。urllib 走系统代理，必须允许重试（代理到 trycloudflare 偶发超时）。"""
    for _ in range(3):
        try:
            with urllib.request.urlopen(url + "/healthz", timeout=timeout) as r:
                return r.status == 200
        except urllib.error.HTTPError as e:
            return e.code not in (502, 503, 530)
        except Exception:
            time.sleep(3)
    return False


def metrics_addr() -> str:
    """从隧道日志里捞 cloudflared 的本地 metrics 地址（如 127.0.0.1:20241）。"""
    try:
        text = TUNNEL_LOG.read_text(encoding="utf-8", errors="ignore")[-60000:]
    except OSError:
        return ""
    m = METRICS_RE.findall(text)
    return m[-1] if m else ""


def tunnel_ready() -> bool:
    """cloudflared 自带的 /ready 端点 —— 唯一可信的健康判据。

    为什么不看公网探测：这台机器走企业代理，从内部访问自己的
    *.trycloudflare.com 域名几乎必然 TLS 握手超时（外部浏览器却完全正常）。
    以前拿 public_ok() 当判死依据，于是每 40 秒就"判定隧道死了"重建一次，
    域名跟着全换，链接自然打不开。/ready 是本机回环请求，瞬时可靠。
    """
    addr = metrics_addr()
    if not addr:
        return False
    try:
        with urllib.request.urlopen(f"http://{addr}/ready", timeout=5) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        return e.code == 200
    except Exception:
        return False


def ensure_server() -> None:
    if local_ok():
        return
    log("本地服务没在跑，拉起 serve.py …")
    with SERVE_LOG.open("a", encoding="utf-8") as f:
        f.write(f"\n===== keeper start serve.py {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        subprocess.Popen(
            [sys.executable, str(BASE / "serve.py"), "--host", "0.0.0.0",
             "--port", str(PORT), "--no-fetch", "--token", TOKEN],
            cwd=str(BASE), stdout=f, stderr=subprocess.STDOUT, env=dict(os.environ))
    for _ in range(20):
        time.sleep(1)
        if local_ok():
            log("serve.py 就绪")
            return
    log("serve.py 20 秒内没就绪，先不管，继续搞隧道")


def cloudflared_running() -> bool:
    out = subprocess.run('tasklist /FI "IMAGENAME eq cloudflared.exe"', shell=True,
                         capture_output=True, text=True, encoding="utf-8",
                         errors="ignore").stdout or ""
    return "cloudflared.exe" in out


class _Adopted:
    """接管一个已经在跑的 cloudflared（它不是我们的子进程，poll 永远返回 None）。"""

    def poll(self):
        return None


def latest_url() -> str:
    try:
        text = TUNNEL_LOG.read_text(encoding="utf-8", errors="ignore")[-40000:]
    except OSError:
        return ""
    m = URL_RE.findall(text)
    return m[-1] if m else ""


def adopt_existing() -> tuple[_Adopted | None, str]:
    """已经有能用的隧道就直接接手，别重建（重建 = 换域名）。

    判据只看"进程在 + 日志里有域名 + /ready 就绪"，**不做公网探测**：
    公网探测在这台机器上永远失败，会让 keeper 一启动就杀掉好隧道。
    """
    if not cloudflared_running():
        return None, ""
    url = latest_url()
    if url and tunnel_ready():
        log(f"接管已在运行的隧道：{url}")
        return _Adopted(), url
    if url:
        log(f"发现隧道进程但 /ready 未就绪，暂不接管：{url}")
    return None, ""


def log_size() -> int:
    try:
        return TUNNEL_LOG.stat().st_size
    except OSError:
        return 0


def read_new(since: int) -> str:
    try:
        with TUNNEL_LOG.open("r", encoding="utf-8", errors="ignore") as f:
            f.seek(since)
            return f.read()
    except OSError:
        return ""


def kill_cloudflared() -> None:
    subprocess.run("taskkill /F /IM cloudflared.exe", shell=True,
                   capture_output=True, text=True, encoding="utf-8", errors="ignore")
    time.sleep(2)


def start_tunnel(wait: int = 45) -> tuple[subprocess.Popen | None, str]:
    """起一个 cloudflared，返回 (进程, 已注册成功的域名)。

    没注册成功就返回空域名，调用方会杀掉重试——每重试一次，DoH 解析出的
    边缘节点就换一批，有的通有的不通（这台机器的 198.18.* 边缘 IP 连不上，
    198.19.* 可以），所以「多试几次」是最省事的办法。
    """
    if not CLOUDFLARED.exists():
        log(f"找不到 {CLOUDFLARED}")
        return None, ""
    mark = log_size()
    with TUNNEL_LOG.open("a", encoding="utf-8") as f:
        f.write(f"\n===== keeper start cloudflared {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        p = subprocess.Popen(
            [str(CLOUDFLARED), "tunnel", "--url", f"http://localhost:{PORT}",
             "--no-autoupdate", "--protocol", "http2"],
            cwd=str(BASE), stdout=f, stderr=subprocess.STDOUT, env=dict(os.environ))
    url = ""
    deadline = time.time() + wait
    while time.time() < deadline and not _stop:
        time.sleep(3)
        text = read_new(mark)
        m = URL_RE.findall(text)
        if "Registered tunnel connection" in text and m:
            url = m[-1]
            log(f"隧道已连上：{url}")
            return p, url
        if p.poll() is not None:
            log("cloudflared 自己退出了（多半是申请域名的 API 超时）")
            return None, ""
    log(f"{wait}s 内没连上边缘节点，换一批边缘 IP 重试")
    return p, ""


def record(url: str) -> None:
    if not url:
        return
    old = URL_FILE.read_text(encoding="utf-8").strip() if URL_FILE.exists() else ""
    if old == url:
        return
    URL_FILE.write_text(url, encoding="utf-8")
    with HIST_FILE.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{url}\n")
    log(f"当前公网地址：{url}"
        + ("" if not old else f"（原 {old} 已失效）"))


# ---------------------------------------------------------------------------
# 数据守护
# ---------------------------------------------------------------------------
def _age_txt(sec: float) -> str:
    if sec == float("inf"):
        return "没有数据文件"
    m = sec / 60
    return f"{m:.0f} 分钟" if m < 120 else f"{m / 60:.1f} 小时"


def data_age() -> float:
    """data/orders.json 有多久没更新（秒）。"""
    try:
        return max(0.0, time.time() - ORDERS_FILE.stat().st_mtime)
    except OSError:
        return float("inf")


def cookie_state() -> tuple[bool, str]:
    """验一次 .env 里的 hk-sit。

    复用 login_browser.verify_cookie()：它固定用 **US 主机** 打一次列表接口，
    success=false 就是失效（返回 302）。整段只有一次请求，约 1 秒。
    """
    try:
        import login_browser as lb
    except Exception as exc:  # noqa: BLE001
        return False, f"加载 login_browser 失败：{exc}"
    try:
        import scraper  # noqa: F401  —— 导入即读取 .env
        cookie = os.getenv("OC_COOKIE", "").strip()
        return lb.verify_cookie(cookie)
    except Exception as exc:  # noqa: BLE001
        return False, f"校验异常：{exc}"


def other_job_running(script: str) -> bool:
    """是否有别的 python 进程已经在跑同一个脚本（例如人手敲了一次 refresh_all.py）。

    两条刷新同时跑会互相覆盖 data/orders.json，所以先让一步。查不到就当"没有"，
    绝不因为查询失败而卡住刷新。
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "ForEach-Object { $_.CommandLine }"],
            capture_output=True, text=True, encoding="utf-8",
            errors="ignore", timeout=25).stdout or ""
    except Exception:  # noqa: BLE001
        return False
    return script in out


class DataGuard:
    """cookie 体检 + 数据刷新。step() 每次心跳调一次，绝不阻塞超过一次接口请求。"""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.kind = ""                 # "login" | "refresh"
        self.started = 0.0
        self.next_check = 0.0          # 0 = 第一轮立刻验（服务启动先验 cookie）
        self.login_block_until = 0.0   # 续期失败后的冷却，避免每 20 秒弹一个浏览器
        self.last_note = ""

    # ---- 起子进程：日志单独一份，父进程不等它 ----
    def _spawn(self, kind: str, argv: list[str]) -> None:
        DATA_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DATA_LOG.open("a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} keeper 启动 {kind}："
                    f"{' '.join(argv[1:])} =====\n")
            f.flush()
            self.proc = subprocess.Popen(argv, cwd=str(BASE), stdout=f,
                                         stderr=subprocess.STDOUT, env=dict(os.environ))
        self.kind = kind
        self.started = time.time()

    def _write_status(self, ok: bool, msg: str, action: str) -> None:
        try:
            DATA_STATE.parent.mkdir(parents=True, exist_ok=True)
            DATA_STATE.write_text(json.dumps({
                "checkedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                "cookieOk": bool(ok),
                "msg": msg,
                "action": action,
                "dataAgeMin": (None if data_age() == float("inf")
                               else round(data_age() / 60, 1)),
                "job": self.kind or "-",
            }, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

    # ---- 主入口 ----
    def step(self) -> None:
        # ① 有活在做：只看是否结束/超时
        if self.proc is not None:
            rc = self.proc.poll()
            if rc is None:
                limit = LOGIN_TIMEOUT if self.kind == "login" else REFRESH_TIMEOUT
                if time.time() - self.started > limit:
                    log(f"[数据] {self.kind} 跑了 {limit // 60} 分钟还没结束，终止")
                    try:
                        self.proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
                    self.proc = None
                    self.kind = ""
                    self.next_check = time.time() + 300
                return
            dur = int(time.time() - self.started)
            kind = self.kind
            self.proc, self.kind = None, ""
            log(f"[数据] {kind} 结束（rc={rc}，{dur}s）")
            if kind == "login":
                ok, msg = cookie_state()
                if ok:
                    log(f"[数据] 续期成功：{msg}，下一轮立刻刷数据")
                    self.next_check = time.time() + 5
                    self._write_status(True, msg, "relogin-ok")
                else:
                    log(f"[数据] 续期没成：{msg}（{LOGIN_COOLDOWN // 60} 分钟后再说）")
                    self.login_block_until = time.time() + LOGIN_COOLDOWN
                    self.next_check = time.time() + COOKIE_TICK
                    self._write_status(False, msg, "relogin-failed")
            else:
                self.next_check = time.time() + COOKIE_TICK
                log(f"[数据] 刷新后数据年龄：{_age_txt(data_age())}")
                self._write_status(True, "刷新完成", "refresh-done")
            return

        # ② 到点体检
        if time.time() < self.next_check:
            return
        self.next_check = time.time() + COOKIE_TICK

        ok, msg = cookie_state()
        if not ok:
            log(f"[数据] cookie 失效：{msg}")
            self._write_status(False, msg, "cookie-invalid")
            if time.time() < self.login_block_until:
                left = int((self.login_block_until - time.time()) / 60) + 1
                log(f"[数据] 上次续期没成功，{left} 分钟后再弹登录窗")
                return
            log("[数据] 弹浏览器续期：只输 4 位验证码，登录成功后自动刷数据")
            self.login_block_until = time.time() + LOGIN_COOLDOWN
            self._spawn("login", [sys.executable, "-u", str(BASE / "login_browser.py"),
                                  "--wait", str(max(120, LOGIN_TIMEOUT - 180))])
            return

        log(f"[数据] cookie 有效：{msg}")
        age = data_age()
        if age > REFRESH_MAX_AGE:
            if other_job_running("refresh_all.py"):
                log("[数据] 已有 refresh_all.py 在跑，跳过本轮")
                return
            log(f"[数据] 数据 {_age_txt(age)}没更新，开始刷新（约 3 分钟）")
            self._write_status(True, msg, "refresh-start")
            self._spawn("refresh", [sys.executable, "-u", str(BASE / "refresh_all.py")])
        else:
            log(f"[数据] 数据 {_age_txt(age)}前刚更新过，本轮只验不刷")
            self._write_status(True, msg, "checked")

    def report(self) -> None:
        """--once / --data-once 用：只报状态，不拉起任何子进程。"""
        ok, msg = cookie_state()
        log(f"[数据] cookie {'有效' if ok else '失效'}：{msg}")
        log(f"[数据] 数据年龄：{_age_txt(data_age())}"
            f"（>{REFRESH_MAX_AGE // 60} 分钟就该刷）")


def main() -> int:
    ap = argparse.ArgumentParser(description="公网通道保活（服务 + 隧道 + 数据守护）")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--no-data", action="store_true", help="只管通道，不验 cookie、不刷数据")
    ap.add_argument("--data-once", action="store_true", help="只做一次 cookie/数据体检后退出")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _on_sig)
    signal.signal(signal.SIGTERM, _on_sig)

    guard = DataGuard() if (DATA_GUARD and not args.no_data) else None
    if args.data_once:
        (guard or DataGuard()).report()
        return 0
    if guard is None:
        log("数据守护已关闭（--no-data 或 OC_KEEPER_DATA=0）")
    elif args.once:
        # --once 只体检，不弹登录窗、不刷数据
        guard.report()
        guard = None

    proc = None
    url = ""
    bad = 0
    # 先看看有没有已经在跑且能用的隧道，有就接手（避免一启动就换域名）
    proc, url = adopt_existing()
    while not _stop:
        # 每轮心跳先过一遍数据守护：第一轮立刻验 cookie（服务启动先验），
        # 之后按 COOKIE_TICK 的节奏；子进程异步跑，不挡隧道保活。
        if guard is not None:
            try:
                guard.step()
            except Exception as exc:  # noqa: BLE001  —— 数据守护绝不能让保活挂掉
                log(f"[数据] 守护异常（忽略，继续保活）：{exc}")
        ensure_server()
        if proc is not None and proc.poll() is not None:
            log("隧道进程退出，重来")
            proc = None
        if proc is None:
            kill_cloudflared()
            proc, new_url = start_tunnel()
            if not new_url:
                # 没连上边缘节点：换一个新实例（换域名）再试
                kill_cloudflared()
                proc = None
                time.sleep(5)
                continue
            url = new_url
            record(new_url)
            bad = 0
        time.sleep(20)
        # 判死只认两件事：进程没了，或 cloudflared 自己的 /ready 连续不就绪。
        if not cloudflared_running():
            log("cloudflared 进程已消失，重来")
            proc = None
            bad = 0
            continue
        if not tunnel_ready():
            bad += 1
            log(f"隧道 /ready 未就绪（第 {bad} 次，最多等 {READY_FAIL_LIMIT} 次）"
                " —— 让 cloudflared 自己重连，不轻易重建")
            if bad >= READY_FAIL_LIMIT:
                log(f"连续 {READY_FAIL_LIMIT} 次未就绪（约 {READY_FAIL_LIMIT * 20 // 60} 分钟），"
                    "判定假死，重建隧道（会换域名）")
                kill_cloudflared()
                proc = None
                bad = 0
        else:
            if bad:
                log("隧道恢复就绪")
            bad = 0
            if PUBLIC_PROBE and url and not public_ok(url):
                # 仅诊断用：本机到 trycloudflare 的回环探测常年超时，不作为动作依据
                log("公网探测不通（参考信息，不重建）")
        if args.once:
            break
    log("保活退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
