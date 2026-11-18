#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键跑通：抓取 -> 存数据 -> 生成看板

  python run.py --demo              示例数据
  python run.py --real              真实数据（默认近 7 天、US 路向）
  python run.py --real --days 30    近 30 天
  python run.py --real --from 2026/10/20 --to 2026/11/03
"""

import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def run(script, *args):
    cmd = [sys.executable, str(BASE / script), *args]
    print(f"\n$ {' '.join(cmd)}")
    r = subprocess.run(cmd)
    if r.returncode != 0:
        sys.exit(r.returncode)


if __name__ == "__main__":
    args = sys.argv[1:]
    # --real / --demo 连同 --region / --days 等参数原样透传给 scraper.py
    run("scraper.py", *(args if ("--real" in args or "--demo" in args) else ["--demo"]))
    run("dashboard.py")
    print(f"\n完成，打开看板: {BASE / 'dashboard.html'}")
