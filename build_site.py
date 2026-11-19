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

# 本地实时模式要一起发布的静态资源：代理脚本 + 计算链源码（原样发布，浏览器里用 Pyodide 跑）
LIVE_SRC = BASE_DIR / "live"
LIVE_DEPS = ["scraper.py", "cross_vendor.py", "dropoff_cancel.py",
             "lms_same_vendor.py", "dashboard.py"]


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
  // 本地实时模式：数据由书签在内部系统页面抓取后 postMessage 过来，不走口令解密
  if (location.search.indexOf('live=1') >= 0) return;
  var SS_KEY = 'oc_site_pw';
  var msg = document.getElementById('lockMsg');
  var btn = document.getElementById('lockBtn');
  var inp = document.getElementById('lockPw');

  function setMsg(t){ msg.textContent = t || ''; }

  async function fetchData(pw){
    var res = await fetch('data.bin', {cache: 'no-store'});
    if (!res.ok) throw new Error('数据文件加载失败（' + res.status + '）');
    // 记下这个数据包的身份，供「刷新数据 → 检查有没有新数据」比对（HTTP 头，无业务含义）
    window.__OC_BIN_SIG = res.headers.get('etag') || res.headers.get('last-modified')
                          || res.headers.get('content-length') || '';
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


INSTALL_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="robots" content="noindex">
<title>安装本地取数书签 · 订单时效看板</title>
<style>
  body{margin:0;padding:44px 20px;background:#f6f6f4;color:#2c2c2a;
    font:14px/1.75 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}
  .card{max-width:640px;margin:0 auto;background:#fff;border-radius:14px;padding:30px 34px;
    box-shadow:0 1px 2px rgba(0,0,0,.06),0 8px 28px rgba(0,0,0,.06)}
  h1{margin:0 0 4px;font-size:18px;font-weight:600}
  .sub{color:#888780;font-size:12px;margin-bottom:22px}
  h2{font-size:13px;font-weight:600;margin:22px 0 8px;color:#2c2c2a}
  p{margin:0 0 10px;color:#55554f;font-size:13px}
  ol{margin:0;padding-left:20px;color:#55554f;font-size:13px}
  li{margin-bottom:6px}
  .drag{display:inline-block;margin:6px 0 2px;padding:9px 18px;border-radius:8px;
    background:#378add;color:#fff;text-decoration:none;font-size:13px;cursor:grab}
  .warn{background:#fff8e6;border:1px solid #f2e2b8;border-radius:8px;padding:10px 12px;
    font-size:12px;color:#8a6d1f;margin-top:18px;line-height:1.7}
  code{background:#f1f0ec;padding:1px 5px;border-radius:4px;font-size:12px}
  input{width:100%;box-sizing:border-box;margin:6px 0 4px;padding:8px 10px;border-radius:8px;
    border:1px solid #dcdbd5;font:13px/1.5 ui-monospace,Consolas,monospace;color:#2c2c2a}
  input:focus{outline:none;border-color:#378add}
</style></head><body>
<div class="card">
  <h1>安装本地取数书签</h1>
  <div class="sub">装一次，之后在内部系统页面点一下就能看到当期数据</div>

  <h2>为什么是这样</h2>
  <p>看板的数据用你自己在内部系统的登录态抓取，全程在你的浏览器内存里完成，
     不上传、不落盘、不需要任何人维护 token。</p>

  <h2>第 1 步：填接口地址</h2>
  <p>内部系统后台的接口域名（在订单查询页按 <code>F12</code> → Network 里能看到以
     <code>…-api.</code> 开头的请求）。这个地址只会写进你本地书签，不会上传。</p>
  <input id="apiIn" placeholder="https://xxx-api.example.com" autocomplete="off" spellcheck="false">

  <h2>第 2 步：把按钮拖到书签栏</h2>
  <ol>
    <li>确认浏览器书签栏已显示（<code>Ctrl+Shift+B</code>）。</li>
    <li>把下面这个按钮<b>拖到书签栏</b>：<br>
        <a id="bm" class="drag" href="#" onclick="return false">取当期订单</a></li>
  </ol>

  <h2>第 3 步：在内部系统里点它</h2>
  <p>先登录内部系统，进到<b>订单综合查询</b>页面（地址栏是 <code>oc-</code> 开头那个系统），
     再点书签栏里的「取当期订单」。抓完会自动弹出看板窗口，数据在那里计算并渲染。</p>

  <div class="warn" id="hint"></div>
</div>
<script>
(function(){
  var h = location.hash || '';
  var initApi = decodeURIComponent((h.match(/api=([^&]+)/) || [])[1] || '');
  var days = (h.match(/days=(\d+)/) || [])[1] || '17';
  // 站点可能挂在子路径下（GitHub Pages 是 /<repo>/），必须按当前目录拼，不能写死根路径
  var BASE = location.origin + location.pathname.replace(/[^/]*$/, '');
  var inp = document.getElementById('apiIn');
  var a = document.getElementById('bm');
  var hint = document.getElementById('hint');

  function render() {
    var api = (inp.value || '').trim().replace(/\/+$/, '');
    var code = "(function(){var d=document,s=d.createElement('script');"
      + (api ? "d.documentElement.setAttribute('data-oc-api','" + api + "');" : "")
      + "s.src='" + BASE + "live/agent.js?v=4&days=" + days + "';"
      + "d.head.appendChild(s);})();";
    a.href = 'javascript:' + code;
    if (!api) {
      hint.innerHTML = '还没填接口地址。不填也能装，但点下去只会提示「找不到接口地址」'
        + '（除非你在此之前已经在页面上点过一次「搜 索」）。';
    } else if (!/^https?:\/\//.test(api)) {
      hint.innerHTML = '地址要以 <code>https://</code> 开头。';
    } else {
      hint.innerHTML = '已就绪。书签里只存这个地址，不存任何账号或口令；'
        + '登录态由浏览器自己带着（HttpOnly，脚本读不到）。';
    }
  }
  inp.value = initApi;
  inp.addEventListener('input', render);
  render();
})();
</script>
</body></html>
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
    # 顺序有讲究：先放锁屏 DOM，再挂 live.js（它会摘掉锁屏），最后才是解密引导
    html = html.replace("</body>",
                        LOCK_HTML
                        + '<script src="live/live.js?v=4"></script>\n'
                        + UNLOCK_JS + "</body>", 1)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(html, encoding="utf-8")
    blob = encrypt(payload, password)
    (out_dir / "data.bin").write_bytes(blob)

    # 本地实时模式的静态资源：代理脚本 + 计算链源码
    live_out = out_dir / "live"
    live_out.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in ("agent.js", "live.js", "pipeline.py"):
        src = LIVE_SRC / name
        if src.exists():
            (live_out / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            copied.append(name)
    for name in LIVE_DEPS:
        src = BASE_DIR / name
        if src.exists():
            (live_out / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            copied.append(name)
    (out_dir / "install.html").write_text(INSTALL_HTML, encoding="utf-8")

    # 顺手放一个禁止搜索引擎收录的标记，避免链接被爬到
    (out_dir / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")

    print(f"[site] index.html  {len(html)/1048576:.2f} MB")
    print(f"[site] data.bin    {len(blob)/1024:.0f} KB（gzip {len(gzip.compress(json.dumps(payload, ensure_ascii=False).encode('utf-8'), 9))/1024:.0f} KB）")
    print(f"[site] 订单 {len(orders)} 单 · 口令 {len(password)} 位 · PBKDF2 {ITERATIONS} 次")
    print(f"[site] live/        {len(copied)} 个文件（agent.js + 计算链源码）")
    print(f"[site] install.html 书签安装页；实时入口 /?live=1")
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
