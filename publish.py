#!/usr/bin/env python3
"""一键对外发布看板：本机服务 + Cloudflare 快速隧道。

    python publish.py                  # 口令取 .env 的 OC_DASHBOARD_TOKEN
    python publish.py --token a,b      # 临时指定，逗号分隔多个 Key
    python publish.py --port 8010      # 换端口（默认 8000）
    python publish.py --no-tunnel      # 只起本地服务，不出公网

Ctrl+C 会同时关掉服务和隧道。

快速隧道的地址是临时的：每次重启都会换一个新域名，且 Cloudflare 不保证可用性。
要固定域名，需要注册 Cloudflare 账号建 named tunnel（cloudflared tunnel create）。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
CF = BASE / "bin" / "cloudflared.exe"
URL_RE = re.compile(
    r"https://(?!api\.|tunnel\.|www\.|update\.)[a-z0-9][a-z0-9-]*\.trycloudflare\.com")


def read_dotenv() -> dict[str, str]:
    f = BASE / ".env"
    out: dict[str, str] = {}
    if not f.exists():
        return out
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def main() -> int:
    try:  # 管道/重定向时也保持实时输出
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--token", default=None, help="覆盖 .env 里的口令，支持逗号分隔多个")
    ap.add_argument("--no-tunnel", action="store_true", help="只起本地服务")
    ap.add_argument("--fetch", action="store_true", help="启动时顺便抓一次数据（默认读快照）")
    args = ap.parse_args()

    dotenv = read_dotenv()
    token = (args.token if args.token is not None
             else dotenv.get("OC_DASHBOARD_TOKEN", "")).strip()
    keys = [k.strip() for k in token.replace(";", ",").split(",") if k.strip()]

    print("=" * 66)
    if keys:
        print(f"  访问口令 {len(keys)} 个：" + " / ".join(keys[:3]) + (" …" if len(keys) > 3 else ""))
        print("  分发给用户时，只给他自己那一个；某人离职或泄露就删掉他那一个再重启。")
    else:
        print("  ！没有配置口令，任何人拿到地址都能看。建议设 OC_DASHBOARD_TOKEN。")
    print("=" * 66)

    srv_cmd = [sys.executable, "serve.py", "--host", "0.0.0.0", "--port", str(args.port),
               "--token", token]
    if not args.fetch:
        srv_cmd.append("--no-fetch")
    srv = subprocess.Popen(srv_cmd, cwd=str(BASE))

    time.sleep(1.5)
    if srv.poll() is not None:
        print("！本地服务启动失败，看上面的报错。")
        return 1
    print(f"\n本地：http://127.0.0.1:{args.port}\n")

    tun: subprocess.Popen | None = None
    if args.no_tunnel:
        print("（--no-tunnel：未开公网隧道，仅本机/内网可访问）\n")
    else:
        if not CF.exists():
            print(f"！找不到 {CF}\n  下载：https://github.com/cloudflare/cloudflared/releases/"
                  f"latest/download/cloudflared-windows-amd64.exe\n")
            srv.terminate()
            return 2
        env = {k: v for k, v in os.environ.items()
               if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
        tun = subprocess.Popen(
            [str(CF), "tunnel", "--url", f"http://localhost:{args.port}",
             "--no-autoupdate", "--protocol", "http2"],
            cwd=str(BASE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=env)

        found = threading.Event()

        def pump():
            assert tun is not None and tun.stdout is not None
            for line in tun.stdout:
                m = URL_RE.search(line)
                if m and not found.is_set():
                    found.set()
                    print("\n" + "=" * 66)
                    print(f"  公网地址（直接发给用户）：\n\n    {m.group(0)}\n")
                    print("  打开后输入口令即可，登录一次 12 小时内免输。")
                    print("  这是临时域名，重启 publish.py 会变。Ctrl+C 结束。")
                    print("=" * 66 + "\n")
                elif "ERR" in line and "precheck" not in line:
                    sys.stdout.write("  [tunnel] " + line.strip()[:160] + "\n")

        threading.Thread(target=pump, daemon=True).start()
        if not found.wait(timeout=45):
            print("！45 秒内没拿到公网地址，隧道可能没连上。"
                  "检查是否能出站访问 TCP 443（协议已强制 http2）。\n")

    try:
        while True:
            time.sleep(1)
            if srv.poll() is not None:
                print("本地服务已退出。")
                break
            if tun is not None and tun.poll() is not None:
                print("隧道已断开（公网地址失效），本地服务仍在跑。")
                break
    except KeyboardInterrupt:
        print("\n正在关闭…")
    finally:
        for p in (tun, srv):
            if p is not None and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
    print("已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
