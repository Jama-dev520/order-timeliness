/* 订单时效看板 · 本地取数代理（在内部系统页面里执行）
 *
 * 运行环境：使用者已登录的内部系统页面（oc-uk 下任意页面），由书签注入。
 * 职责只有一件事：用使用者自己的登录态把「当期订单 + 事件流」抓下来，
 * 然后交给看板窗口在本地计算。**不读、不传、不留任何 token**——
 * hk-sit 是 HttpOnly，脚本根本读不到它，浏览器发请求时自动带。
 *
 * 接口主机不写死在这里（这个文件是公开托管的），来源优先级：
 *   1. 书签注入的 <html data-oc-api="...">
 *   2. localStorage 里上次成功用过的地址
 *   3. 页面网络记录里已有的接口请求（用户先点过「搜 索」的情况）
 */
(function () {
  'use strict';

  if (window.__ocLocalAgent) {                 // 重复点击：正在跑就提示，跑完了再跑一次
    window.__ocLocalAgent.run();
    return;
  }

  var SELF = document.currentScript;
  // 正常情况从自己的 src 推导看板地址；以「内联注入」方式调试时用 __OC_DASH 兜底。
  // 注意要拆成两个：origin 用于 postMessage 校验，base 用于拼落地的看板 URL
  // （GitHub Pages 是 /<repo>/ 子路径，两者不能混用）。
  var SRC = (SELF && SELF.src) ? SELF.src : '';
  var DEBUG_DASH = window.__OC_DASH || '';
  var DASH_BASE = SRC ? SRC.replace(/\/live\/agent\.js.*$/, '')
                      : (DEBUG_DASH || '');
  var DASH_ORIGIN = (function () {
    try { return new URL(DASH_BASE).origin; } catch (e) { return DASH_BASE; }
  })();
  var VER = SRC ? (new URL(SRC).searchParams.get('v') || '1') : '1';
  var DAYS = SRC ? (+new URL(SRC).searchParams.get('days') || 17)
                 : (+window.__OC_DAYS || 17);
  var LS_API = 'oc_api_host';

  var LIST = '/aquarius-order/order/tracking-number/v1/tracking-number-list';
  var EV = '/aquarius-order/order/tracking-number/v1/tracking-event-list/';
  var LIST_WORKERS = 8;
  var EV_WORKERS = 12;
  var SANITY_MAX = 5000;

  var st = {abort: false, busy: false, t0: 0};

  /* ---------------- 面板 ---------------- */
  var panel, bar, msg, sub;
  var lastMsg = '准备中…', lastSub = '', lastProg = 0;

  // 内部系统是 SPA，脚本可能在 body 出现之前就执行（书签早期点击 / 测试注入）。
  // 先记住要显示的内容，等 body 就绪再挂载，避免直接抛错导致整个脚本静默失败。
  function ui() {
    if (!document.body) {
      if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', ui, {once: true});
      } else {
        setTimeout(ui, 200);
      }
      return;
    }
    if (document.getElementById('__oc_panel')) return;
    panel = document.createElement('div');
    panel.id = '__oc_panel';
    panel.style.cssText = [
      'position:fixed', 'right:18px', 'bottom:18px', 'z-index:2147483647',
      'width:300px', 'padding:14px 16px', 'border-radius:12px',
      'background:#fff', 'color:#2c2c2a', 'font:13px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif',
      'box-shadow:0 2px 6px rgba(0,0,0,.08),0 12px 40px rgba(0,0,0,.14)',
      'border:1px solid #e8e7e2'
    ].join(';');
    panel.innerHTML =
      '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">' +
        '<b style="font-weight:600;font-size:13px">本地取数</b>' +
        '<span id="__oc_close" style="margin-left:auto;cursor:pointer;color:#9a9a94;font-size:16px;line-height:1">×</span>' +
      '</div>' +
      '<div id="__oc_msg" style="color:#55554f">准备中…</div>' +
      '<div style="height:4px;background:#eeece7;border-radius:2px;margin:9px 0 6px;overflow:hidden">' +
        '<div id="__oc_bar" style="height:100%;width:0;background:#378add;border-radius:2px;transition:width .25s"></div>' +
      '</div>' +
      '<div id="__oc_sub" style="color:#9a9a94;font-size:11px;line-height:1.5"></div>';
    document.body.appendChild(panel);
    bar = panel.querySelector('#__oc_bar');
    msg = panel.querySelector('#__oc_msg');
    sub = panel.querySelector('#__oc_sub');
    panel.querySelector('#__oc_close').onclick = function () {
      st.abort = true;
      st.busy = false;
      hide();
    };
    // 挂载时补上等待期间产生的进度
    if (msg) msg.textContent = lastMsg;
    if (sub) sub.textContent = lastSub;
    if (bar) bar.style.width = lastProg + '%';
  }
  function say(t, extra) {
    lastMsg = t;
    lastSub = extra || '';
    if (msg) msg.textContent = t;
    if (sub) sub.textContent = lastSub;
    try { console.log('[oc-live] ' + t + (extra ? ' | ' + extra : '')); } catch (e) {}
  }
  function prog(p) {
    lastProg = Math.max(0, Math.min(100, p));
    if (bar) bar.style.width = lastProg + '%';
  }
  function hide() { if (panel && panel.parentNode) panel.parentNode.removeChild(panel); panel = null; }
  function done(text, extra, ms) {
    st.busy = false;
    say(text, extra);
    prog(100);
    setTimeout(hide, ms || 4000);
  }

  /* ---------------- 接口主机 ---------------- */
  function resolveApi() {
    var fromTag = document.documentElement.getAttribute('data-oc-api');
    if (fromTag) {
      var clean = fromTag.trim().replace(/\/+$/, '');
      try { localStorage.setItem(LS_API, clean); } catch (e) {}
      return clean;
    }
    try {
      var cached = localStorage.getItem(LS_API);
      if (cached) return cached;
    } catch (e) {}
    // 兜底：页面已经查过数时，网络记录里就有接口地址
    var es = performance.getEntriesByType('resource') || [];
    for (var i = 0; i < es.length; i++) {
      if (es[i].name.indexOf('/aquarius-order/') >= 0) {
        var u = new URL(es[i].name);
        try { localStorage.setItem(LS_API, u.origin); } catch (e) {}
        return u.origin;
      }
    }
    return '';
  }

  /* ---------------- 日期窗口（北京墙钟，与接口一致）---------------- */
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function dstr(d) { return d.getFullYear() + '/' + pad(d.getMonth() + 1) + '/' + pad(d.getDate()); }
  function windowRange() {
    var now = new Date();
    // 接口只放最近约 17 天，起点再往前会被静默忽略并返回全库（约 2.5 万单）
    var from = new Date(now.getTime() - (DAYS - 1) * 86400000);
    return {start: dstr(from) + ' 00:00:00', end: dstr(now) + ' 23:59:59'};
  }

  /* ---------------- 抓取 ---------------- */
  function getJSON(url) {
    return fetch(url, {credentials: 'include', headers: {'Accept': 'application/json'}})
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      });
  }

  function listPage(api, page, start, end) {
    var u = new URL(api + LIST);
    u.searchParams.set('page', page);
    u.searchParams.set('limit', 10);          // 服务端只认 10；100 也能给但单页要 9 秒
    u.searchParams.set('startDate', start);
    u.searchParams.set('endDate', end);
    u.searchParams.set('destinationCountry', 'US');
    return getJSON(u.toString()).then(function (d) {
      if (d && d.success === false) {
        var code = d.errorCode;
        throw new Error(code === 302 || code === '302'
          ? '登录态已失效，请重新登录后再点一次'
          : ('接口拒绝 errorCode=' + code));
      }
      var res = (d && d.result) || {};
      return {list: res.list || [], total: +(res.total || 0)};
    });
  }

  function evPage(api, tn) {
    return getJSON(api + EV + encodeURIComponent(tn))
      .then(function (d) { var r = d && d.result; return Array.isArray(r) ? r : []; })
      .catch(function () { return []; });   // 事件缺失不影响主流程，与本地抓数一致
  }

  function pool(items, workers, fn, tick) {
    var i = 0, out = new Array(items.length);
    function worker() {
      if (st.abort) return Promise.resolve();
      if (i >= items.length) return Promise.resolve();
      var k = i++;
      return Promise.resolve(fn(items[k])).then(function (v) {
        out[k] = v;
        if (tick) tick();
        return worker();
      });
    }
    var ws = [];
    for (var w = 0; w < Math.min(workers, items.length); w++) ws.push(worker());
    return Promise.all(ws).then(function () { return out; });
  }

  function run() {
    if (st.busy) { say('正在抓取中…', '等这一轮跑完，或点右上角 × 取消'); return; }
    var api = resolveApi();
    if (!api) {
      say('找不到接口地址', '请在书签里带上接口地址，或先在页面上点一次「搜 索」');
      return;
    }
    if (!panel) ui();
    st.busy = true;
    var win = windowRange();
    st.t0 = Date.now();
    st.abort = false;
    say('正在读取订单列表…', win.start.slice(0, 10) + ' ~ ' + win.end.slice(0, 10));
    prog(3);

    listPage(api, 1, win.start, win.end).then(function (first) {
      if (st.abort) return;
      var total = first.total;
      if (total > SANITY_MAX) {
        st.busy = false;
        say('单量异常（' + total + ' 单）', '接口忽略了日期条件，请把天数调小后重试');
        return;
      }
      if (!total) { st.busy = false; say('这个窗口没有订单', ''); return; }

      var pages = Math.ceil(total / 10);
      var records = first.list.slice();
      var pagesDone = 1;
      prog(8);
      say('订单列表 1/' + pages + ' 页', '共 ' + total + ' 单，并发 ' + LIST_WORKERS + ' 页');

      var idx = [];
      for (var p = 2; p <= pages; p++) idx.push(p);
      return pool(idx, LIST_WORKERS, function (p) {
        return listPage(api, p, win.start, win.end).then(function (r) { return r.list; });
      }, function () {
        pagesDone++;
        prog(8 + 22 * pagesDone / pages);
        say('订单列表 ' + pagesDone + '/' + pages + ' 页', '已取 ' + pagesDone * 10 + ' 单');
      }).then(function (chunks) {
        if (st.abort) return;
        chunks.forEach(function (c) { records = records.concat(c); });
        // 去重（并发翻页时后台可能有新增单导致页边界重叠）
        var seen = {}, uniq = [];
        records.forEach(function (r) {
          var k = r.trackingNumber || r.ocOrderNo;
          if (k && !seen[k]) { seen[k] = 1; uniq.push(r); }
        });
        records = uniq;
        say('订单 ' + records.length + ' 单，开始拉事件明细', '每单 1 个请求，并发 ' + EV_WORKERS);
        prog(32);

        var evDone = 0, evTotal = records.length, evT0 = Date.now();
        return pool(records, EV_WORKERS, function (r) {
          return evPage(api, r.trackingNumber || '');
        }, function () {
          evDone++;
          if (evDone % 5 === 0 || evDone === evTotal) {
            prog(32 + 52 * evDone / evTotal);
            var el = (Date.now() - evT0) / 1000;
            var eta = evDone ? Math.max(0, el / evDone * (evTotal - evDone)) : 0;
            say('事件明细 ' + evDone + '/' + evTotal,
                '本阶段已用 ' + Math.round(el) + 's，预计还需 ' + Math.round(eta) + 's');
          }
        }).then(function (evLists) {
          if (st.abort) return;
          var events = {};
          records.forEach(function (r, i) {
            var tn = r.trackingNumber || '';
            if (tn && evLists[i] && evLists[i].length) events[tn] = evLists[i];
          });
          prog(86);
          say('抓取完成，正在本地计算…', records.length + ' 单 / ' +
              Object.keys(events).length + ' 单有事件');
          handoff({records: records, events: events,
                   start: win.start, end: win.end, region: 'us', api: api});
        });
      });
    }).catch(function (e) {
      st.busy = false;
      say('失败：' + (e && e.message ? e.message : e), '可重试；若提示登录态失效请刷新页面重新登录');
      prog(0);
    });
  }

  /* ---------------- 交给看板窗口 ---------------- */
  function handoff(data) {
    var w = window.open(DASH_BASE + '/?live=1&v=' + VER, '_blank');
    if (!w) { st.busy = false; say('弹窗被拦截', '请允许本站弹窗后重试'); return; }
    var sent = false, tries = 0;

    function onMsg(e) {
      if (e.origin !== DASH_ORIGIN) return;
      if (!e.data || e.data.type !== 'oc-live-ready') return;
      e.source.postMessage({type: 'oc-live-data', payload: data}, DASH_ORIGIN);
      sent = true;
      cleanup();
      done('已交给看板窗口', '计算与渲染在那边完成，本页可以关掉了', 6000);
    }
    function cleanup() {
      window.removeEventListener('message', onMsg);
      clearInterval(timer);
    }
    window.addEventListener('message', onMsg);
    // 看板还没加载好就重复打招呼，最多等 3 分钟
    var timer = setInterval(function () {
      if (sent || ++tries > 180) {
        cleanup();
        if (!sent) { st.busy = false; say('看板窗口没有响应', '请检查是否被拦截，或手动打开看板后重试'); }
        return;
      }
      try { w.postMessage({type: 'oc-live-ping'}, DASH_ORIGIN); } catch (e) {}
    }, 1000);
  }

  window.__ocLocalAgent = {show: function () { if (!panel) { ui(); } }, run: run};
  ui();
  run();
})();
