#!/usr/bin/env python
"""生成可公开托管的加密看板（GitHub Pages / Cloudflare Pages 通用）。

产物在 site/ 下：
    index.html   模板：不含任何业务数据，只有锁屏和一个解密引导脚本
    data.bin     业务数据：gzip -> AES-256-GCM，口令用 PBKDF2-SHA256 派生

为什么要「模板公开 + 数据加密」而不是整页加密：
    整页加密后密文不可压缩，3.8MB 的 HTML 会原样下载；分开之后
    data.bin 是「先压缩再加密」，实测 110KB 左右，而且模板可被浏览器缓存。

用法：
    python build_site.py                     # 口令取 .env 的 OC_SITE_PASSWORD
    python build_site.py --password 你的口令   # 临时指定
    python build_site.py --out dist          # 换输出目录
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import struct
import sys
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "orders.json"
MAGIC = b"OCDB1"
ITERATIONS = 200_000


def _env(key: str, default: str = "") -> str:
    """读 .env（不依赖 python-dotenv，与其它脚本保持一致）。"""
    val = os.environ.get(key, "")
    if val:
        return val
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    return default


def encrypt(payload: dict, password: str) -> bytes:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    packed = gzip.compress(raw, 9)
    salt = os.urandom(16)
    iv = os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=ITERATIONS).derive(password.encode("utf-8"))
    ct = AESGCM(key).encrypt(iv, packed, MAGIC)
    return MAGIC + struct.pack(">I", ITERATIONS) + salt + iv + ct


LOCK_CSS = """
#lockWrap{position:fixed;inset:0;z-index:99999;display:flex;align-items:center;justify-content:center;
  background:#f6f6f4;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}
#lockCard{width:340px;padding:28px 26px;background:#fff;border-radius:14px;
  box-shadow:0 1px 2px rgba(0,0,0,.06),0 8px 28px rgba(0,0,0,.06);text-align:center}
#lockCard h1{margin:0 0 6px;font-size:15px;font-weight:500;color:#2c2c2a;letter-spacing:.2px}
#lockCard p{margin:0 0 18px;font-size:12px;color:#888780;line-height:1.6}
#lockPw{width:100%;box-sizing:border-box;padding:9px 11px;font-size:13px;border:1px solid #dddcd6;
  border-radius:8px;outline:none;background:#fbfbfa;color:#2c2c2a}
#lockPw:focus{border-color:#378add;background:#fff}
#lockBtn{margin-top:12px;width:100%;padding:9px;font-size:13px;border:0;border-radius:8px;
  background:#378add;color:#fff;cursor:pointer}
#lockBtn:disabled{opacity:.55;cursor:default}
#lockMsg{margin-top:12px;font-size:12px;color:#c0392b;min-height:16px}
"""

LOCK_HTML = """
<div id="lockWrap">
  <div id="lockCard">
    <h1>订单时效看板</h1>
    <p>请输入访问口令</p>
    <input id="lockPw" type="password" autocomplete="current-password" autofocus>
    <button id="lockBtn">进入看板</button>
    <div id="lockMsg"></div>
  </div>
</div>
"""

UNLOCK_JS = r"""
<script>
(function(){
  var SS_KEY = 'oc_site_pw';
  var msg = document.getElementById('lockMsg');
  var btn = document.getElementById('lockBtn');
  var inp = document.getElementById('lockPw');

  function setMsg(t){ msg.textContent = t || ''; }

  async function fetchData(pw){
    var res = await fetch('data.bin', {cache: 'no-store'});
    if (!res.ok) throw new Error('数据文件加载失败（' + res.status + '）');
    var buf = await res.arrayBuffer();
    var magic = new TextDecoder().decode(new Uint8Array(buf, 0, 5));
    if (magic !== 'OCDB1') throw new Error('数据文件格式不对');
    var iter = new DataView(buf).getUint32(5);
    var salt = new Uint8Array(buf, 9, 16);
    var iv = new Uint8Array(buf, 25, 12);
    var ct = new Uint8Array(buf, 37);

    var baseKey = await crypto.subtle.importKey('raw', new TextEncoder().encode(pw),
      'PBKDF2', false, ['deriveKey']);
    var key = await crypto.subtle.deriveKey(
      {name: 'PBKDF2', salt: salt, iterations: iter, hash: 'SHA-256'},
      baseKey, {name: 'AES-GCM', length: 256}, false, ['decrypt']);
    var plain;
    try {
      plain = await crypto.subtle.decrypt(
        {name: 'AES-GCM', iv: iv, additionalData: new TextEncoder().encode('OCDB1')},
        key, ct);
    } catch (e) {
      throw new Error('口令不正确');
    }
    var stream = new Blob([plain]).stream().pipeThrough(new DecompressionStream('gzip'));
    var text = await new Response(stream).text();
    return JSON.parse(text);
  }

  function boot(d){
    window.META = d.meta;
    window.ALL = d.orders;
    window.CROSS = d.cross;
    window.DROPOFF = d.dropoff;
    window.LMSSAME = d.lmsSame;
    window.RAW = d.raw || {};
    window.RAW_SNAP = d.rawSnap || '';
    window.RAW_CUTOFF = d.rawCut || '';
    window.RAW_DAYS = d.rawDays || 0;
    var w = document.getElementById('lockWrap');
    if (w) w.remove();
    window.__start();
  }

  async function tryUnlock(pw, remember){
    btn.disabled = true;
    setMsg('正在解密…');
    try {
      var d = await fetchData(pw);
      if (remember) { try { sessionStorage.setItem(SS_KEY, pw); } catch (e) {} }
      setMsg('');
      boot(d);
    } catch (e) {
      btn.disabled = false;
      setMsg(e.message || String(e));
      try { sessionStorage.removeItem(SS_KEY); } catch (e2) {}
    }
  }

  btn.addEventListener('click', function(){ 
    var pw = inp.value;
    if (!pw) { setMsg('请先输入口令'); return; }
    tryUnlock(pw, true);
  });
  inp.addEventListener('keydown', function(e){ if (e.key === 'Enter') btn.click(); });

  var saved = null;
  try { saved = sessionStorage.getItem(SS_KEY); } catch (e) {}
  if (saved) { inp.value = saved; tryUnlock(saved, false); }
})();
</script>
"""


def build(out_dir: Path, password: str) -> tuple[Path, Path]:
    import dashboard

    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    meta = data.get("meta", {})
    orders = data.get("orders", [])

    html = dashboard.render(meta, orders, dynamic=False, secure=True)
    payload = dashboard.SECURE_PAYLOAD
    if not payload:
        raise SystemExit("render(secure=True) 没有产出 SECURE_PAYLOAD")

    # 关掉自动启动：数据要先解密才能跑 __start()
    head_probe = "</head>"
    if head_probe not in html:
        raise SystemExit("模板里找不到 </head>，无法注入解锁逻辑")
    html = html.replace(head_probe,
                        "<style>" + LOCK_CSS + "</style>\n"
                        "<script>window.__autoStart=false;</script>\n</head>", 1)

    if "</body>" not in html:
        raise SystemExit("模板里找不到 </body>")
    html = html.replace("</body>", LOCK_HTML + UNLOCK_JS + "</body>", 1)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(html, encoding="utf-8")
    blob = encrypt(payload, password)
    (out_dir / "data.bin").write_bytes(blob)

    # 顺手放一个禁止搜索引擎收录的标记，避免链接被爬到
    (out_dir / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")

    print(f"[site] index.html  {len(html)/1048576:.2f} MB")
    print(f"[site] data.bin    {len(blob)/1024:.0f} KB（gzip {len(gzip.compress(json.dumps(payload, ensure_ascii=False).encode('utf-8'), 9))/1024:.0f} KB）")
    print(f"[site] 订单 {len(orders)} 单 · 口令 {len(password)} 位 · PBKDF2 {ITERATIONS} 次")
    return out_dir / "index.html", out_dir / "data.bin"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--password", default="", help="访问口令，默认读 .env 的 OC_SITE_PASSWORD")
    ap.add_argument("--out", default="site", help="输出目录（默认 site/）")
    args = ap.parse_args()

    password = args.password or _env("OC_SITE_PASSWORD", "")
    if not password:
        sys.exit("缺少口令：请设置 .env 的 OC_SITE_PASSWORD，或用 --password 指定")

    out = (BASE_DIR / args.out).resolve()
    build(out, password)
    print(f"[site] 输出目录 {out}")


if __name__ == "__main__":
    main()
