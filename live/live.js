/* 订单时效看板 · 本地实时模式（?live=1）
 *
 * 与线上加密版的区别只有一个：数据不是从 data.bin 解密来的，而是由使用者在
 * 内部系统页面点书签、用他自己的登录态抓下来，通过 postMessage 递到这里。
 * 数据全程只在浏览器内存里，不上传、不落盘。
 *
 * 计算仍然跑原来的 Python（scraper / cross_vendor / dropoff_cancel / lms_same_vendor），
 * 只是换了个运行时（Pyodide），所以口径与 refresh_all.py 完全一致。
 */
(function () {
  'use strict';

  var QS = new URLSearchParams(location.search);
  if (QS.get('live') !== '1') return;

  var CDNS = [
    'https://cdn.jsdelivr.net/pyodide/v0.26.4/full/',
    'https://unpkg.com/pyodide@0.26.4/',
    'https://cdnjs.cloudflare.com/ajax/libs/pyodide/0.26.4/'
  ];
  var MODS = ['pipeline.py', 'scraper.py', 'cross_vendor.py', 'dropoff_cancel.py',
              'lms_same_vendor.py', 'dashboard.py'];

  var box, titleEl, subEl, barEl;

  function ui(text, sub, pct) {
    if (!box) {
      var lock = document.getElementById('lockWrap');
      if (lock && lock.parentNode) lock.parentNode.removeChild(lock);
      box = document.createElement('div');
      box.id = '__live_box';
      box.style.cssText = [
        'position:fixed', 'inset:0', 'z-index:99998', 'display:flex',
        'align-items:center', 'justify-content:center', 'background:#f6f6f4',
        'font:13px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif',
        'color:#2c2c2a'
      ].join(';');
      box.innerHTML =
        '<div style="width:380px;padding:26px 28px;background:#fff;border-radius:14px;' +
        'box-shadow:0 1px 2px rgba(0,0,0,.06),0 8px 28px rgba(0,0,0,.06);text-align:center">' +
          '<h1 style="margin:0 0 6px;font-size:15px;font-weight:500">订单时效看板 · 本地实时</h1>' +
          '<div id="__live_t" style="font-size:12px;color:#55554f;min-height:18px"></div>' +
          '<div style="height:4px;background:#eeece7;border-radius:2px;margin:14px 0 10px;overflow:hidden">' +
            '<div id="__live_b" style="height:100%;width:0;background:#378add;border-radius:2px;' +
            'transition:width .3s"></div>' +
          '</div>' +
          '<div id="__live_s" style="font-size:11px;color:#9a9a94;line-height:1.6"></div>' +
        '</div>';
      document.body.appendChild(box);
      titleEl = box.querySelector('#__live_t');
      subEl = box.querySelector('#__live_s');
      barEl = box.querySelector('#__live_b');
    }
    if (text !== undefined && text !== null) titleEl.textContent = text;
    if (sub !== undefined && sub !== null) subEl.innerHTML = sub;
    if (pct !== undefined) barEl.style.width = Math.max(0, Math.min(100, pct)) + '%';
  }

  function fail(msg, hint) {
    ui(msg, hint || '', 0);
    titleEl.style.color = '#c0392b';
  }

  /* ---------------- 1. 向 opener 打招呼，等数据 ---------------- */
  var got = false;
  var ping = setInterval(function () {
    if (got || !window.opener) return;
    try { window.opener.postMessage({type: 'oc-live-ready'}, '*'); } catch (e) {}
  }, 800);

  window.addEventListener('message', function (e) {
    if (!e.data || e.data.type !== 'oc-live-data') return;
    if (e.source !== window.opener) return;          // 只认开我这个窗口的那一方
    if (got) return;
    got = true;
    clearInterval(ping);
    run(e.data.payload);
  });

  ui('等待本地取数…',
     '请在弹出的内部系统页面里完成抓取；数据只在本机内存中传递，不经过任何服务器', 4);

  setTimeout(function () {
    if (!got) {
      ui('还没收到数据',
         '如果你没看到内部系统页面的「本地取数」面板，请回到那个页面重新点一次书签。' +
         '<br>这个窗口可以关掉。', 0);
    }
  }, 20000);

  /* ---------------- 2. 加载运行时 ---------------- */
  function inject(src) {
    return new Promise(function (res, rej) {
      var s = document.createElement('script');
      s.src = src;
      s.onload = res;
      s.onerror = function () { rej(new Error('加载失败 ' + src)); };
      document.head.appendChild(s);
    });
  }

  function loadPyodide() {
    var i = 0;
    function next() {
      if (i >= CDNS.length) return Promise.reject(new Error('所有 CDN 都不可达'));
      var base = CDNS[i++];
      return inject(base + 'pyodide.js')
        .then(function () { return globalThis.loadPyodide({indexURL: base}); })
        .catch(function () { return next(); });
    }
    return next();
  }

  function getText(f) {
    return fetch('live/' + f, {cache: 'no-cache'}).then(function (r) {
      if (!r.ok) throw new Error('读取 ' + f + ' 失败 HTTP ' + r.status);
      return r.text();
    });
  }

  function run(data) {
    var t0 = Date.now();
    ui('已收到 ' + data.records.length + ' 单，正在加载 Python 运行时…',
       '首次约 10MB，之后走浏览器缓存', 12);

    var srcMap = {};
    Promise.resolve()
      .then(function () {
        return Promise.all(MODS.map(getText)).then(function (texts) {
          MODS.forEach(function (m, i) { srcMap[m] = texts[i]; });
        });
      })
      .then(function () {
        ui('正在加载 Python 运行时…', 'Pyodide 0.26.4', 25);
        return loadPyodide();
      })
      .then(function (py) {
        ui('正在初始化计算模块…', '载入 scraper / 三个异常检测脚本', 55);
        py.globals.set('__oc_sources', JSON.stringify(srcMap));
        var r = py.runPython([
          'import os, sys, json',
          'os.makedirs("/oc_live", exist_ok=True)',
          '_src = json.loads(__oc_sources)',
          'for _n, _s in _src.items():',
          '    with open(os.path.join("/oc_live", _n), "w", encoding="utf-8") as _f:',
          '        _f.write(_s)',
          'if "/oc_live" not in sys.path:',
          '    sys.path.insert(0, "/oc_live")',
          'import pipeline',
          'pipeline.bootstrap(__oc_sources)'
        ].join('\n'));
        if (String(r) !== 'ok' && String(r) !== 'already') {
          throw new Error('模块初始化失败：' + String(r));
        }
        return py;
      })
      .then(function (py) {
        ui('正在本地计算…', data.records.length + ' 单 · 归一化 + 三项异常检测', 72);
        var payloadJson = JSON.stringify(data);
        // 直接传字符串，避免大对象在 JS/Python 之间反复转换
        py.globals.set('__oc_payload', payloadJson);
        var out = py.runPython('pipeline.run(__oc_payload)');
        var parsed = JSON.parse(out);
        if (!parsed.ok) throw new Error(parsed.error || '计算失败');
        return parsed;
      })
      .then(function (parsed) {
        ui('渲染看板…', '', 92);
        boot(parsed.payload);
        var s = parsed.stats || {};
        console.log('[live] 完成', s, '耗时', ((Date.now() - t0) / 1000).toFixed(1) + 's');
      })
      .catch(function (e) {
        console.error('[live]', e);
        fail('本地计算失败：' + (e && e.message ? e.message : e),
             '可以刷新页面重试；若反复失败，请改用带口令的线上版本。');
      });
  }

  /* ---------------- 3. 回填并启动（与解密版同一套全局变量）---------------- */
  function boot(d) {
    window.META = d.meta;
    window.ALL = d.orders;
    window.CROSS = d.cross;
    window.DROPOFF = d.dropoff;
    window.LMSSAME = d.lmsSame;
    window.RAW = d.raw || {};
    window.RAW_SNAP = d.rawSnap || '';
    window.RAW_CUT = d.rawCut || '';
    window.RAW_DAYS = d.rawDays || 0;

    if (box && box.parentNode) box.parentNode.removeChild(box);
    box = null;
    window.__start();

    var lv = d.live || {};
    if (lv.start) {
      var t = document.createElement('div');
      t.style.cssText = [
        'position:fixed', 'left:50%', 'transform:translateX(-50%)', 'bottom:14px',
        'z-index:9999', 'padding:6px 14px', 'border-radius:999px',
        'background:rgba(44,44,42,.86)', 'color:#fff', 'font:11px/1.6 -apple-system,sans-serif',
        'box-shadow:0 2px 10px rgba(0,0,0,.18)'
      ].join(';');
      t.textContent = '本地实时 · ' + lv.start.slice(0, 10) + ' ~ ' + lv.end.slice(0, 10) +
        ' · 抓取于 ' + (lv.generatedAt || '').slice(11, 16) + ' · 数据未离开本机';
      document.body.appendChild(t);
    }
  }
})();
