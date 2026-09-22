/* ============================================================
   降智雷达 · 前端逻辑
   - 数据来源：/api/stream (SSE) —— 首帧 snapshot，后续 patch
   - 纯前端渲染，不做任何请求轮询（除按钮触发的诊断/配置）
   ============================================================ */
'use strict';

const DEFAULT_PAGE_SIZE = 50;
const MAX_LOGS   = 400;

const S = {
  responses: new Map(),      // rid -> record
  logs:      [],
  stats:     {},
  filter:    'all',
  q:         '',
  page: 1, pageSize: DEFAULT_PAGE_SIZE, total: 0, pages: 1,
  start: '', end: '', loading: false, pageError: '',
  connected: false,
  dirty:     false,
  lastScan:  null,
};

const $ = (id) => document.getElementById(id);
const el = {
  dot: $('live-dot'), pill: $('status-pill'),
  mPid:$('m-pid'), mHz:$('m-hz'), mCost:$('m-cost'),
  mBytes:$('m-bytes'), mWorkers:$('m-workers'), mIdle:$('m-idle'),
  mRounds:$('m-rounds'), mCaptured:$('m-captured'),
  mPaired:$('m-paired'), mLast:$('m-last'),
  cNormal:$('c-normal'), cSubtask:$('c-subtask'),
  cIncomplete:$('c-incomplete'), cDowngrade:$('c-downgrade'),
  cards:$('cards'), empty:$('empty'), emptyHint:$('empty-hint'), more:$('more'),
  logPanel:$('log-panel'), logToggle:$('log-toggle'), logCount:$('log-count'),
  modal:$('modal'), modalTitle:$('modal-title'), modalBody:$('modal-body'),
  modalClose:$('modal-close'), modalSave:$('modal-save'),
  toast:$('toast'), search:$('search'),
};

function fmtBytes(n){
  if (n == null) return '—';
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n/1024).toFixed(1) + ' KB';
  if (n < 1073741824) return (n/1048576).toFixed(1) + ' MB';
  return (n/1073741824).toFixed(2) + ' GB';
}

/* ---------------------------------------------------------- 工具 */
function esc(s){
  return String(s == null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function shortId(rid){
  if (!rid) return '—';
  return String(rid).replace(/^resp_/, '').slice(0, 5);
}
function requestDuration(r){
  if (!r.created_at) return '—';
  if (!r.completed_at) return ['queued', 'in_progress'].includes(r.status) ? '未完成' : '—';
  // 兼容尚未重启的采集器；旧接口返回包含日期的本地时间。
  let seconds = r.duration_seconds;
  if (seconds === undefined){
    const start = Date.parse(r.created_at.replace(' ', 'T'));
    const end = Date.parse(r.completed_at.replace(' ', 'T'));
    seconds = (end - start) / 1000;
  }
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return '—';
  if (seconds < 60) return `${seconds} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  return `${Math.floor(seconds / 3600)} 时 ${Math.floor(seconds % 3600 / 60)} 分 ${seconds % 60} 秒`;
}
function relTime(ts){
  if (!ts) return '—';
  const d = Date.now()/1000 - ts;
  if (d < 0) return '刚刚';
  if (d < 60) return d.toFixed(0) + ' 秒前';
  if (d < 3600) return (d/60).toFixed(0) + ' 分钟前';
  return (d/3600).toFixed(1) + ' 小时前';
}
function hhmmss(ts){
  if (!ts) return '—';
  const d = new Date(ts*1000);
  const p = (n)=>String(n).padStart(2,'0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
const VERDICT_CN = { normal:'正常', subtask:'子任务',
                     incomplete:'不完整', downgrade:'降级' };
// 请求模型这条证据是从哪来的：内存配对是原有通道，后两个是只读旁路索引。
const EVIDENCE_CN = { memory_websocket:'内存配对', rollout_token_usage:'会话记录',
                      codex_log_prefix:'codex 日志' };

function toast(msg, isErr){
  el.toast.textContent = msg;
  el.toast.classList.toggle('err', !!isErr);
  el.toast.classList.remove('hidden');
  clearTimeout(toast._t);
  toast._t = setTimeout(()=>el.toast.classList.add('hidden'), 2400);
}

async function post(url, body){
  try{
    const r = await fetch(url, {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body || {}),
    });
    return await r.json();
  }catch(e){ toast('请求失败：' + e.message, true); return null; }
}

/* ---------------------------------------------------------- SSE */
function connect(){
  const es = new EventSource('/api/stream');

  es.onopen = () => { S.connected = true; markDirty(); };

  es.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch(e){ return; }
    if (msg.type === 'snapshot') applySnapshot(msg.data);
    else if (msg.type === 'patch') applyPatch(msg);
  };

  es.onerror = () => {
    S.connected = false;
    markDirty();
    // EventSource 自带重连；这里仅提示状态
  };
}

function applySnapshot(data){
  S.logs = (data.logs || []).slice(-MAX_LOGS);
  S.stats = data.stats || {};
  renderLogPanel(true);
  schedulePageRefresh(0);
  markDirty();
}

function applyPatch(msg){
  if (msg.upserts && msg.upserts.length) schedulePageRefresh();
  if (msg.logs_reset){ S.logs = []; renderLogPanel(true); }
  // 顶部告警条已去掉；降级只留卡片本身 + 一次性提示
  if (msg.alerts && msg.alerts.length) toast('⚠ 检测到模型降级', true);
  if (msg.logs && msg.logs.length){
    S.logs = S.logs.concat(msg.logs).slice(-MAX_LOGS);
    renderLogPanel(false);
  }
  if (msg.stats) S.stats = msg.stats;
  markDirty();
}

/* 历史由服务端筛选分页；SSE 只通知当前页刷新，不把全库塞进浏览器。 */
let pageTimer = null, pageController = null, pageGeneration = 0;
function schedulePageRefresh(delay = 250){
  if (pageTimer !== null) return;
  pageTimer = setTimeout(()=>{ pageTimer = null; loadPage(); }, delay);
}
function resetPage(){
  S.page = 1;
  clearTimeout(pageTimer); pageTimer = null;
  loadPage();
}
function dateSeconds(value){
  return value ? new Date(value).getTime() / 1000 : null;
}
async function loadPage(){
  const generation = ++pageGeneration;
  pageController?.abort();
  pageController = new AbortController();
  const start = dateSeconds(S.start), end = dateSeconds(S.end);
  if ((start !== null && !Number.isFinite(start)) || (end !== null && !Number.isFinite(end)) ||
      (start !== null && end !== null && start > end)){
    S.loading = false; S.pageError = '开始时间不能晚于结束时间，请检查所选时间。';
    markDirty(); return;
  }
  S.loading = true; S.pageError = ''; markDirty();
  const params = new URLSearchParams({page:S.page, page_size:S.pageSize, filter:S.filter, q:S.q});
  if (start !== null) params.set('start', start);
  if (end !== null) params.set('end', end);
  try{
    const response = await fetch('/api/responses?' + params, {signal:pageController.signal});
    const data = await response.json();
    if (!response.ok) throw new Error(data.reason || '历史记录加载失败');
    if (generation !== pageGeneration) return;
    S.responses = new Map((data.responses || []).map(r=>[r.rid,r]));
    S.total = data.total; S.page = data.page; S.pages = data.pages;
  }catch(error){
    if (generation !== pageGeneration || error.name === 'AbortError') return;
    S.pageError = '加载失败：' + error.message;
  }finally{
    if (generation === pageGeneration){ S.loading = false; markDirty(); }
  }
}

/* ---------------------------------------------------------- 渲染调度 */
function markDirty(){
  S.dirty = true;
  if (markDirty._raf) return;
  markDirty._raf = requestAnimationFrame(()=>{
    markDirty._raf = null;
    if (S.dirty){ S.dirty = false; render(); }
  });
}

/* ---------------------------------------------------------- 状态区 */
function renderStatus(){
  const st = S.stats;
  const status = st.status || (S.connected ? 'starting' : 'offline');

  el.dot.className = 'dot ' + ({
    running:'live', starting:'warn', no_process:'warn',
    denied:'dead', stopped:'dead', offline:'dead',
  }[status] || 'warn');

  const label = {
    running:'采集中', starting:'启动中', no_process:'未发现 codex.exe',
    denied:'进程无权限', stopped:'已停止', offline:'连接断开',
  }[status] || status;
  el.pill.textContent = S.connected ? label : '连接断开';
  el.pill.className = 'pill ' + (
    status === 'running' ? 'ok'
    : (status === 'no_process' || status === 'starting' || status === 'offline') ? 'warn'
    : 'err');

  $('m-backend').textContent = st.backend === 'cpp' ? 'C++ · 原生扫描' : 'Python';
  el.mPid.textContent      = st.pid ?? '—';
  el.mHz.textContent       = st.hz ? st.hz.toFixed(1) + ' 轮/秒' : '—';
  el.mCost.textContent     = st.scan_cost != null ? st.scan_cost.toFixed(3) + ' s' : '—';
  el.mBytes.textContent    = fmtBytes(st.scan_bytes);
  el.mWorkers.textContent  = (st.active_workers != null && st.workers != null)
                               ? `${st.active_workers} / ${st.workers}` : '—';
  const interval = st.min_interval_ms ?? (st.idle == null ? null : st.idle * 1000);
  el.mIdle.textContent     = interval === 0 ? '0 ms · 连续扫描'
                           : (interval == null ? '—' : interval + ' ms');
  el.mRounds.textContent   = st.rounds ?? 0;
  el.mCaptured.textContent = st.captured ?? S.responses.size;
  el.mPaired.textContent   = st.paired ?? 0;
  S.lastScan = st.last_scan || null;
  el.mLast.textContent     = S.lastScan ? hhmmss(S.lastScan) : '—';

  $('filter-suspect').textContent = `疑似样本 ${st.suspect_count || 0}`;
  const c = st.counts || {};
  el.cNormal.textContent     = c.normal ?? 0;
  el.cSubtask.textContent    = c.subtask ?? 0;
  el.cIncomplete.textContent = c.incomplete ?? 0;
  el.cDowngrade.textContent  = c.downgrade ?? 0;
  document.querySelectorAll('.cnt').forEach(b=>{
    const v = b.dataset.filter;
    b.dataset.active = (S.filter === v) ? '1' : '0';
  });

  el.emptyHint.textContent = st.status === 'no_process'
    ? '当前未发现运行中的 codex.exe —— 启动 Codex 后会自动接入。'
    : (st.status === 'denied'
        ? '打开进程失败，请尝试以管理员身份重新启动采集器。'
        : '点击「诊断」可查看一次原始内存扫描结果。');
}

/* ---------------------------------------------------------- 差量渲染状态
   之前的写法是每次 render 都 el.cards.innerHTML = ... 整表重建，
   节点一重建，.card 上的入场动画 cardin 就重放一次 ——
   而 render 会被每一次 SSE patch 触发（含每 2s 的心跳广播），
   于是整屏卡片持续闪烁。
   现在改为：节点复用 + 内容签名比对 + 只在本次会话首次出现的
   响应上播入场动画。 */
const cardEls = new Map();     // rid -> { el, sig }
const seenRids = new Set();    // 本次会话里已经渲染过的响应ID
let lastCardsSig = '';

/* ---------------------------------------------------------- 卡片 */
function matchFilter(r){
  if (S.filter !== 'all' && r.verdict !== S.filter) return false;
  if (!S.q) return true;
  const q = S.q.toLowerCase();
  return [r.rid, r.model, r.req_model, r.effort, r.status, r.text_format, r.prev]
    .some(v => v && String(v).toLowerCase().includes(q));
}

function cardHtml(r){
  const v = r.verdict || 'normal';
  const pending = r.status === 'in_progress' || r.status === 'queued';
  const badge = r.suspect_reason ? '疑似样本' : (VERDICT_CN[v] || v) + (pending ? '-未完成' : '');
  const req = r.req_model;

  let swapCls = 'unknown', swapInner;
  if (req){
    swapCls = (req === r.model) ? 'ok' : 'bad';
    swapInner = `<em>请求</em><span class="req">${esc(req)}</span>
                 <span class="arrow">→</span>
                 <em>响应</em><span class="res">${esc(r.model)}</span>`;
  } else {
    // 没采到请求模型 —— 采集缺口，不是降级证据，所以标黄不标红
    swapInner = `<span class="req">?</span>
                 <span class="arrow">→</span>
                 <span class="res">${esc(r.model)}</span>`;
  }

  // effort 只是请求侧档位，据此给文字上色；未知档位保持默认色。
  // minimal 与 low 同级，是同一端最低档的两种写法，用同一颜色
  const effortCls = r.effort === 'max' ? 'effort-max'
                  : r.effort === 'high' || r.effort === 'xhigh' ? 'effort-high'
                  : r.effort === 'medium' || r.effort === 'low' || r.effort === 'minimal' ? 'effort-low' : '';

  const meta = (label, val, cls) =>
    `<span class="meta-item"><em>${label}</em><b class="${cls || ''}">${esc(val ?? '-')}</b></span>`;

  return `<article class="card v-${v}${pending ? ' pending' : ''}${r.suspect_reason ? ' suspect' : ''}" data-rid="${esc(r.rid)}">
    <div class="card-main">
      <span class="badge${v==='downgrade'?' blink':''}">${esc(badge)}</span>
      <span class="model" title="${esc(r.model)}">${esc(r.model)}
        ${r.effort ? `<small class="model-effort ${effortCls}">${esc(r.effort)}</small>` : ''}
      </span>
      <span class="swapline ${swapCls}">${swapInner}</span>
      <span class="duration"><em>请求总耗时</em><strong>${esc(requestDuration(r))}</strong></span>
      <span class="status" data-s="${esc(r.status || '')}">
        <i class="sd"></i>${esc(r.status || '-')}
      </span>
    </div>

    <div class="card-sub">
      <button type="button" class="rid" title="${esc(r.rid)} · 双击复制完整请求号" aria-label="请求号 ${esc(shortId(r.rid))}，双击或按回车复制完整请求号">${esc(shortId(r.rid))}</button>
      ${r.expect ? meta('采集时预期', r.expect) : ''}
      ${r.evidence_source ? meta('证据来源', EVIDENCE_CN[r.evidence_source] || r.evidence_source) : ''}
      ${meta('格式', r.text_format)}
      ${meta('创建', r.created_at)}
      ${meta('完成', r.completed_at)}
      ${meta('标记', r.marks)}
      ${r.suspect_reason ? meta('样本说明', r.suspect_reason) : ''}
      ${r.updates ? `<span class="upd">状态更新 ${r.updates} 次</span>` : ''}
      <span class="subtime">${esc(hhmmss(r.last_seen))}</span>
    </div>
  </article>`;
}

/* 卡片内容签名：这些字段任一变化才需要重建这张卡的内容 */
function cardSig(r){
  return [r.model, r.req_model, r.expect, r.verdict, r.effort, r.status, r.text_format,
          r.created_at, r.completed_at, r.duration_seconds, r.marks, r.updates,
          r.last_seen, r.pairing_status, r.evidence_source, r.suspect_reason].join('\u0002');
}

function createCardNode(r){
  const tmp = document.createElement('div');
  tmp.innerHTML = cardHtml(r);
  const node = tmp.firstElementChild;
  // 只有本次会话首次出现的响应才播入场动画；
  // 因为筛选/搜索被回收再重建的卡片不播，避免整屏闪。
  if (seenRids.has(r.rid)) node.classList.add('no-anim');
  else seenRids.add(r.rid);
  bindRequestId(node, r.rid);
  return node;
}

function updateCardNode(el, r){
  const tmp = document.createElement('div');
  tmp.innerHTML = cardHtml(r);
  const fresh = tmp.firstElementChild;
  el.className = fresh.className + ' no-anim';
  el.innerHTML = fresh.innerHTML;        // 外层 article 本身保留，动画不重放
  bindRequestId(el, r.rid);
}

function bindRequestId(node, rid){
  const button = node.querySelector('.rid');
  button?.addEventListener('dblclick', ()=>copy(rid));
  button?.addEventListener('keydown', e=>{
    if (e.key === 'Enter' || e.key === ' '){ e.preventDefault(); copy(rid); }
  });
}

function renderCards(){
  const shown = [...S.responses.values()];
  $('result-count').textContent = `${S.total} 条请求`;
  $('page-summary').textContent = S.total ? `共 ${S.total} 条 · 第 ${S.page} / ${S.pages} 页` : '共 0 条 · 第 1 / 1 页';
  $('page-number').value = S.page;
  $('page-number').max = S.pages;
  $('page-prev').disabled = S.loading || S.page <= 1;
  $('page-next').disabled = S.loading || S.page >= S.pages;
  $('page-go').disabled = S.loading || !!S.pageError;
  $('page-loading').textContent = S.loading ? '加载中…' : '';
  $('history-error').textContent = S.pageError;
  $('history-error').classList.toggle('hidden', !S.pageError);
  const listEmpty = shown.length === 0;
  el.empty.classList.toggle('hidden', !listEmpty);
  el.cards.classList.toggle('hidden', listEmpty);
  el.cards.setAttribute('aria-busy', String(S.loading));
  if (listEmpty){
    el.empty.querySelector('h2').textContent = S.loading ? '正在加载历史记录…'
      : (S.filter !== 'all' || S.q || S.start || S.end ? '没有符合当前筛选条件的记录' : '尚未捕获到任何响应对象');
  }
  el.more.classList.add('hidden');

  // 内容签名一致 => 完全不动 DOM。心跳广播、纯统计更新都走这条路。
  const parts = [S.filter, S.q, S.page, shown.length];
  for (const r of shown) parts.push(r.rid, cardSig(r));
  const sig = parts.join('\u0001');
  if (sig === lastCardsSig) return;
  lastCardsSig = sig;

  // 1) 回收不再显示的卡片
  const wanted = new Set(shown.map(r=>r.rid));
  for (const [rid, item] of cardEls){
    if (!wanted.has(rid)){
      item.el.remove();
      cardEls.delete(rid);
    }
  }

  // 2) 复用 / 新建 / 就地更新
  for (const r of shown){
    const s = cardSig(r);
    const item = cardEls.get(r.rid);
    if (!item){
      const node = createCardNode(r);
      el.cards.appendChild(node);
      cardEls.set(r.rid, { el: node, sig: s });
    } else if (item.sig !== s){
      updateCardNode(item.el, r);
      item.sig = s;
    }
  }

  // 3) 排序：用 flex order，而不是挪动节点。
  //    appendChild/insertBefore 移动一个已挂载的节点，浏览器内部是「移除 → 插入」，
  //    这会重启它身上的 CSS 入场动画 —— 每来一条新响应整屏就闪一次。
  //    只改 order 不碰 DOM 结构，动画不受影响。赋相同值不会触发任何变化。
  for (let i = 0; i < shown.length; i++){
    const node = cardEls.get(shown[i].rid).el;
    const o = String(i);
    if (node.style.order !== o) node.style.order = o;
  }
}

/* ---------------------------------------------------------- 日志 */
function renderLogPanel(rebuild){
  el.logCount.textContent = S.logs.length;
  if (!rebuild && el.logPanel.classList.contains('hidden')) return;
  const html = S.logs.map(l=>
    `<div class="ln ${esc(l.level)}"><span class="t">${esc(l.ts)}</span>` +
    `<span class="l">${esc(l.level)}</span><span class="m">${esc(l.msg)}</span></div>`
  ).join('');
  el.logPanel.innerHTML = html;
  el.logPanel.scrollTop = el.logPanel.scrollHeight;
}

/* ---------------------------------------------------------- 主渲染 */
function render(){
  renderStatus();
  renderCards();
}

/* 清空数据时必须一并清掉差量渲染的缓存，否则旧节点会留在 DOM 里 */
function resetCardRender(){
  cardEls.clear();
  seenRids.clear();
  lastCardsSig = '';
  el.cards.innerHTML = '';
}

/* ---------------------------------------------------------- 交互 */
function copy(text){
  if (!text) return;
  if (!navigator.clipboard){ toast('当前环境不支持剪贴板，请使用本地浏览器打开', true); return; }
  navigator.clipboard.writeText(text)
    .then(()=>toast('已复制完整请求号'))
    .catch(()=>toast('复制失败', true));
}

function openModal(title, bodyHtml, withSave){
  el.modalTitle.textContent = title;
  el.modalBody.innerHTML = bodyHtml;
  el.modalSave.classList.toggle('hidden', !withSave);
  el.modal.classList.remove('hidden');
}
function closeModal(){ el.modal.classList.add('hidden'); }

async function doDiagnose(){
  const d = await fetch('/api/diagnose').then(r=>r.json()).catch(e=>({ok:false, reason:String(e)}));
  const s = d.sweep || null;

  const head = `<pre>采集线程状态 : ${esc(statusLabel(d.status))}${d.pid ? '  pid=' + esc(d.pid) : ''}
已扫轮数     : ${esc(d.rounds ?? 0)}   实测频率 : ${d.hz ? d.hz.toFixed(1) + ' 轮/秒' : '—'}   网页端订阅 : ${esc(d.clients ?? 0)}

最近一轮扫描（只读回放，未额外触发扫描）
  时间       : ${esc(s ? s.ts : '—')}
  单轮耗时   : ${esc(s ? s.cost + ' s' : '—')}
  单轮扫描量 : ${fmtBytes(s ? s.bytes : null)}
  可读区域数 : ${esc(s ? s.regions : '—')}   并行线程 : ${esc(s ? s.workers : '—')}
  区域枚举耗时: ${esc(s ? s.region_cost + ' s' : '—')}
  预筛命中块 : ${esc(s ? s.hit_blocks : '—')}
  原始响应对象: ${esc(s ? s.raw_responses : '—')}   请求侧映射 : ${esc(s ? s.raw_requests : '—')}
  新增/更新  : ${esc(s ? s.upserts : '—')}${d.reason ? '\n\n原因：' + esc(d.reason) : ''}</pre>`;

  const rows = (d.items || []).map(it=>`<tr>
      <td title="${esc(it.rid)}">${esc(shortId(it.rid))}</td>
      <td>${esc(it.model)}</td>
      <td>${esc(it.req_model || '未配对')}</td>
      <td>${esc(it.effort)}</td>
      <td>${esc(it.status)}</td>
      <td>${esc(it.marks)}</td>
    </tr>`).join('');

  const table = (d.items && d.items.length) ? `
    <table style="margin-top:12px">
      <thead><tr><th>响应ID</th><th>响应模型</th><th>请求模型</th><th>effort</th><th>状态</th><th>标记</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>` : `<p style="margin:12px 0 0;color:var(--text-3)">
      ${d.ok && d.raw_responses === 0
        ? '最近一轮没有从内存里抽到响应对象 —— 响应对象只在请求生命周期内存在，换个 Codex 正在干活的时刻再看。'
        : ''}</p>`;

  const native = s?.native;
  const phases = native ? `<h3>C++ 解析诊断</h3><pre>${esc(JSON.stringify(native, null, 2))}</pre>
    <p>read/parse/prefilter_worker_seconds 是各线程累计时间，不能相加当作单轮耗时。
    incomplete_candidates 包含无效内存片段，不等于漏采请求数；区域枚举耗时可能来自缓存。</p>` : '';

  // 旁路证据索引：只读 codex 自有日志/会话记录，用来补齐"找不到前序请求"的请求模型。
  const ev = d.evidence || null;
  const evSrc = ev?.index_sources || {};
  const evKeys = ev?.index_now || {};
  const evidence = ev ? `<h3>请求模型证据索引（只读旁路，不进内存扫描器）</h3><pre>状态       : ${ev.enabled ? '启用' : '未启用'}${ev.codex_home ? '   目录 : ' + esc(ev.codex_home) : ''}
轮询       : 每 ${esc(ev.poll_seconds)}s   已轮询 ${esc(ev.polls ?? 0)} 次   最近一轮 ${ev.last_poll ? esc(hhmmss(ev.last_poll)) : '—'}
日志库     : ${esc(ev.log_db || '—')}   游标 ${esc(ev.log_last_id ?? 0)}   已读 ${esc(ev.log_rows_indexed ?? 0)} 行   会话文件 ${esc(ev.rollout_files ?? 0)} 个
可配对键   : 响应号 ${esc(evKeys.resp_id ?? 0)} ｜ item 前缀 ${esc(evKeys.item ?? 0)} ｜ 冲突作废 ${esc(ev.index_rejected ?? 0)}
来源分布   : 会话记录 ${esc(evSrc.rollout_token_usage ?? 0)} ｜ codex 日志 ${esc(evSrc.codex_log_prefix ?? 0)}
回补历史   : ${esc(ev.backfilled ?? 0)} 条   补齐完成态 ${esc(ev.settled ?? 0)} 条   本进程遇到键冲突 ${esc(ev.conflicts ?? 0)} 条${ev.last_error ? '\n最近错误   : ' + esc(ev.last_error) : ''}</pre>
    <p>前缀是"响应 ID 与它产出的 output item ID 共享 23～25 位十六进制"这一观察结果，
    不是官方契约：同一前缀出现第二个模型时整个键会被作废，宁可没有证据也不猜。</p>` : '';

  const candidates = `<details><summary>最近一轮请求候选（最多 50 条）</summary>
    <p>candidate_only 表示仅识别到请求体形状，不参与自动配对；缺少 prev 的候选也不按时间猜测配对。</p>
    <pre>${esc(JSON.stringify(s?.request_candidates || [], null, 2))}</pre></details>`;
  openModal('诊断 · 采集线程最近一轮观测', head + table + phases + evidence + candidates, false);
}

function statusLabel(s){
  return { running:'采集中', starting:'启动中', no_process:'未发现 codex.exe',
           denied:'进程无权限', stopped:'已停止' }[s] || s || '未知';
}

function doConfig(){
  const st = S.stats;
  openModal('配置', `
    <div class="row"><label>预期模型</label>
      <input id="cfg-expect" value="${esc(st.expect || '')}" placeholder="留空时不主动标记子任务"></div>
    <div class="row"><label>最小采样间隔(ms)</label>
      <input id="cfg-interval" type="number" min="0" max="60000" step="any"
             value="${esc(st.min_interval_ms ?? (st.idle || 0) * 1000)}" placeholder="0 = 连续扫描"></div>
    <div class="row"><label>并行线程</label>
      <input id="cfg-workers" type="number" min="1" max="16" step="1" value="${esc(st.workers ?? 4)}"></div>
    <p style="margin:0;color:var(--text-3)">
      间隔从两轮开始时间计算，0 表示连续扫描。线程数在当前轮结束后生效。
      保存后下次启动继续使用；预期模型只影响新记录，已有记录保留采集时的设置。</p>
  `, true);
}

async function saveConfig(){
  const expect = ($('cfg-expect')?.value || '').trim();
  const intervalInput = $('cfg-interval'), workersInput = $('cfg-workers');
  if (!intervalInput.value || !intervalInput.checkValidity() || !workersInput.value || !workersInput.checkValidity()){
    toast('间隔须为 0～60000ms，线程数须为 1～16 的整数', true); return;
  }
  const min_interval_ms = Number(intervalInput.value), workers = Number(workersInput.value);
  const r = await post('/api/config', { expect, min_interval_ms, workers });
  if (r && r.ok){
    if (r.stats) S.stats = r.stats;
    markDirty(); closeModal(); toast('配置已保存');
  } else if (r) toast(r.reason || '配置保存失败', true);
}

function bind(){
  document.querySelectorAll('.cnt').forEach(b=>{
    b.addEventListener('click', ()=>{
      S.filter = (S.filter === b.dataset.filter) ? 'all' : b.dataset.filter;
      syncFilterBtns(); resetPage();
    });
  });
  document.querySelectorAll('#filters .fbtn').forEach(b=>{
    b.addEventListener('click', ()=>{
      S.filter = b.dataset.filter; syncFilterBtns(); resetPage();
    });
  });

  let sTimer;
  el.search.addEventListener('input', ()=>{
    clearTimeout(sTimer);
    sTimer = setTimeout(()=>{ S.q = el.search.value.trim(); resetPage(); }, 120);
  });

  for (const id of ['date-start','date-end']){
    $(id).addEventListener('change', ()=>{
      S.start = $('date-start').value; S.end = $('date-end').value; resetPage();
    });
    $(id).addEventListener('click', ()=>{
      try { $(id).showPicker?.(); } catch (_) { /* Browser's native input remains usable. */ }
    });
  }
  $('date-reset').addEventListener('click', ()=>{
    $('date-start').value = ''; $('date-end').value = ''; S.start = ''; S.end = ''; resetPage();
  });
  $('page-prev').addEventListener('click', ()=>{ if (S.page > 1){ --S.page; loadPage(); } });
  $('page-next').addEventListener('click', ()=>{ if (S.page < S.pages){ ++S.page; loadPage(); } });
  $('page-size').addEventListener('change', ()=>{ S.pageSize = Number($('page-size').value); resetPage(); });
  const go = ()=>{
    const value = Number($('page-number').value);
    if (!Number.isInteger(value) || value < 1 || value > S.pages){ toast('请输入有效页码', true); return; }
    S.page = value; loadPage();
  };
  $('page-go').addEventListener('click', go);
  $('page-number').addEventListener('keydown', event=>{ if (event.key === 'Enter') go(); });
  $('history-retry').addEventListener('click', ()=>loadPage());

  el.logToggle.addEventListener('click', ()=>{
    el.logPanel.classList.toggle('hidden');
    el.logToggle.classList.toggle('open');
    if (!el.logPanel.classList.contains('hidden')) renderLogPanel(true);
  });
  $('log-clear').addEventListener('click', ()=>{
    S.logs = []; renderLogPanel(true); toast('日志已清屏');
  });

  $('btn-diag').addEventListener('click', doDiagnose);
  $('btn-cfg').addEventListener('click', doConfig);
  $('btn-clear').addEventListener('click', async ()=>{
    const result = await post('/api/clear');
    if (result && result.ok){ S.logs = []; renderLogPanel(true); toast('运行日志已清屏，历史记录保留'); }
  });
  $('btn-stop').addEventListener('click', async ()=>{
    if (!confirm('确定停止采集并退出本地服务？')) return;
    await post('/api/shutdown');
    toast('已发送停止指令');
    setTimeout(()=>{
      el.pill.textContent = '服务已停止';
      el.pill.className = 'pill err';
      el.dot.className = 'dot dead';
    }, 400);
  });

  el.modalClose.addEventListener('click', closeModal);
  el.modalSave.addEventListener('click', saveConfig);
  el.modal.addEventListener('click', (e)=>{ if (e.target === el.modal) closeModal(); });
  document.addEventListener('keydown', (e)=>{ if (e.key === 'Escape') closeModal(); });
}

function syncFilterBtns(){
  document.querySelectorAll('#filters .fbtn').forEach(b=>{
    b.classList.toggle('active', b.dataset.filter === S.filter);
  });
}

/* ---------------------------------------------------------- 心跳：相对时间 */
setInterval(()=>{
  if (S.lastScan) el.mLast.textContent = hhmmss(S.lastScan) + ' · ' + relTime(S.lastScan);
}, 1000);

/* ---------------------------------------------------------- 启动 */
bind();
connect();
renderLogPanel(true);
