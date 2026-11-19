"""一条命令续 hk-sit 并同步到 GitHub —— 就是「更新token」。

起因：login_browser.py 只把新 cookie 写回本地 .env，线上加密版跑在 GitHub Actions 上，
还要手工改 Secret、手工点 Run workflow。这几步串成一条命令，少踩「粘贴带空格」的坑。

用法：
  python renew_token.py            # 检查 → 需要就弹浏览器登录 → 同步 Secret → 触发部署 → 验证
  python renew_token.py --check     # 只看当前 cookie 还有效没，不动任何东西
  python renew_token.py --no-run    # 同步 Secret 但不触发部署
  python renew_token.py --force     # 不管有没有效，强制重新登录一次

人只需要做一件事：浏览器弹出来后输 4 位验证码（账号密码由 Chrome 记忆自动填）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import struct
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

BASE = Path(__file__).resolve().parent
REPO = "Jama-dev520/order-timeliness"
WORKFLOW = "refresh.yml"
SITE = "https://jama-dev520.github.io/order-timeliness"
GH_FALLBACKS = [
    Path.home() / "gh_portable" / "bin" / "gh.exe",
    Path(r"C:\Users\182251\gh_portable\bin\gh.exe"),
]


def log(msg: str = "") -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:            # Windows GBK 控制台兜底
        print(msg.encode("utf-8", "replace").decode("utf-8", "replace"), flush=True)


def need_env() -> None:
    """补上 site-packages，保证从任意目录都能 import 同目录模块。"""
    if str(BASE) not in sys.path:
        sys.path.insert(0, str(BASE))


def find_gh() -> str:
    exe = shutil.which("gh")
    if exe:
        return exe
    for p in GH_FALLBACKS:
        if p.is_file():
            return str(p)
    raise SystemExit("找不到 gh（GitHub CLI）。请安装，或把路径加到 PATH。")


def gh(*args: str, input_bytes: bytes | None = None, retries: int = 3) -> subprocess.CompletedProcess:
    """调 gh，网络抖动重试。"""
    exe = find_gh()
    last = None
    for i in range(retries):
        cp = subprocess.run([exe, *args], input=input_bytes,
                            capture_output=True, cwd=str(BASE))
        if cp.returncode == 0:
            return cp
        last = cp
        err = (cp.stderr or b"").decode("utf-8", "replace")
        if any(t in err for t in ("TLS", "timeout", "connection", "Could not resolve")):
            time.sleep(3 * (i + 1))
            continue
        break
    err = (last.stderr or b"").decode("utf-8", "replace") if last else ""
    raise SystemExit(f"gh {' '.join(args[:3])} 失败：{err.strip()[:300]}")


# ---------------------------------------------------------------------------
# 1. 检查
# ---------------------------------------------------------------------------
def check_cookie(cookie: str) -> tuple[bool, str]:
    need_env()
    import login_browser as lb
    return lb.verify_cookie(cookie)


# ---------------------------------------------------------------------------
# 2. 同步 Secret
# ---------------------------------------------------------------------------
def push_secret(cookie: str) -> None:
    """stdin 传值：走命令行参数会带空格/换行，之前就是这么挂的。"""
    cookie = cookie.strip()
    if cookie.startswith("hk-sit=") is False:
        raise SystemExit("cookie 格式不对：必须以 hk-sit= 开头")
    if len(cookie) != 43:
        log(f"⚠ cookie 长度 {len(cookie)}（正常是 43），仍按原值写入，请留意")
    gh("secret", "set", "OC_COOKIE", "-R", REPO, input_bytes=cookie.encode())
    log(f"✓ 已写入 GitHub Secret OC_COOKIE（{len(cookie)} 字符）")


def secret_updated_at() -> str:
    try:
        cp = gh("secret", "list", "-R", REPO, "--json", "name,updatedAt")
        for it in json.loads(cp.stdout.decode("utf-8", "replace") or "[]"):
            if it.get("name") == "OC_COOKIE":
                return str(it.get("updatedAt") or "")
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        pass
    return ""


# ---------------------------------------------------------------------------
# 3. 触发并等待
# ---------------------------------------------------------------------------
def trigger(reason: str) -> int:
    gh("api", "-X", "POST",
       f"repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches",
       "-f", "ref=main", "-f", f"inputs[reason]={reason}")
    log("✓ 已触发部署工作流")


def latest_run(since_ts: float) -> dict | None:
    cp = gh("run", "list", "-R", REPO, "--limit", "5",
            "--json", "databaseId,status,conclusion,createdAt,displayTitle")
    runs = json.loads(cp.stdout.decode("utf-8", "replace") or "[]")
    return runs[0] if runs else None


def wait_run(timeout: int = 900) -> tuple[bool, str]:
    """等最近一次运行结束。返回 (成功?, run id)。"""
    t0 = time.time()
    seen = None
    while time.time() - t0 < timeout:
        try:
            r = latest_run(t0)
        except SystemExit:
            raise
        except Exception:  # noqa: BLE001
            r = None
        if r:
            rid = str(r.get("databaseId"))
            st = (r.get("status") or "").lower()
            if seen is None:
                seen = rid
                log(f"  运行 {rid} 已排队（{r.get('displayTitle') or ''}）")
            if st == "completed":
                ok = (r.get("conclusion") or "") == "success"
                return ok, rid
        time.sleep(12)
    return False, seen or ""


# ---------------------------------------------------------------------------
# 4. 验证线上密文
# ---------------------------------------------------------------------------
def verify_site() -> bool:
    need_env()
    import gzip

    import requests

    import build_site as bs
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    pw = bs._env("OC_SITE_PASSWORD", "")
    s = requests.Session()
    s.trust_env = False
    r = s.get(f"{SITE}/data.bin", timeout=30)
    r.raise_for_status()
    b = r.content
    if b[:5] != b"OCDB1":
        log("✗ data.bin 魔数不对")
        return False
    it = struct.unpack(">I", b[5:9])[0]
    salt, iv, ct = b[9:25], b[25:37], b[37:]
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=it).derive(pw.encode())
    d = json.loads(gzip.decompress(AESGCM(key).decrypt(iv, ct, b"OCDB1")))
    m = d.get("meta") or {}
    log(f"✓ 线上数据可解密：{len(d.get('orders') or [])} 单 | 生成于 {m.get('generatedAt')} "
        f"| 窗口 {m.get('dateFrom')} ~ {m.get('dateTo')}")
    return True


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="续 hk-sit 并同步到 GitHub（「更新token」）")
    ap.add_argument("--check", action="store_true", help="只检查 cookie，不改任何东西")
    ap.add_argument("--no-run", action="store_true", help="同步 Secret 但不触发部署")
    ap.add_argument("--force", action="store_true", help="强制重新登录")
    ap.add_argument("--wait", type=int, default=600, help="等人工输入验证码的秒数")
    ap.add_argument("--timeout", type=int, default=900, help="等部署完成的上限秒数")
    args = ap.parse_args()

    need_env()
    import login_browser as lb
    import scraper  # noqa: F401  —— 导入即把 .env 灌进 os.environ（scraper 模块级做的）

    # 必须直接读文件，不能走 lb._env_val：它优先 os.environ，登录后拿到的是内存里的旧值
    cookie = (lb.read_env().get("OC_COOKIE") or "").strip()

    log(f"① 检查当前 cookie（{len(cookie)} 字符，{secret_updated_at()[:16] or '未同步过'} 写入 Secret）")
    ok, msg = check_cookie(cookie)
    log(f"   {'✓ 有效' if ok else '✗ 已失效'}：{msg}")

    if args.check:
        log("\n（--check 模式，未做任何改动）")
        return 0 if ok else 1

    if not ok or args.force:
        log("\n② 需要在浏览器里登录一次（你只需输入 4 位验证码）")
        cmd = [sys.executable, str(BASE / "login_browser.py"), "--wait", str(args.wait)]
        if args.force:
            cmd.append("--force")
        rc = subprocess.call(cmd, cwd=str(BASE))
        if rc != 0:
            log(f"✗ 登录流程退出码 {rc}，未继续同步")
            return rc
        cookie = (lb.read_env().get("OC_COOKIE") or "").strip()
        ok2, msg2 = check_cookie(cookie)
        if not ok2:
            log(f"✗ 新 cookie 仍不可用：{msg2}")
            return 3
        log(f"   新 cookie 验证通过：{msg2}")
    else:
        log("\n② cookie 仍有效，跳过登录")

    log("\n③ 同步到 GitHub Secret")
    push_secret(cookie)

    if args.no_run:
        log("\n（--no-run：未触发部署）")
        return 0

    log("\n④ 触发线上部署并等待")
    trigger("更新 cookie 后重建站点")
    done, rid = wait_run(args.timeout)
    if not done:
        log(f"✗ 运行 {rid} 未成功。看日志：gh run view {rid} -R {REPO} --log-failed")
        return 4
    log(f"   运行 {rid} 成功")

    log("\n⑤ 验证线上数据")
    return 0 if verify_site() else 5


if __name__ == "__main__":
    raise SystemExit(main())
