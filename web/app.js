/* ============================================================
   降智雷达 · 前端逻辑
   - 数据来源：/api/stream (SSE) —— 首帧 snapshot，后续 patch
   - 历史：/api/responses（服务端筛选分页），SSE 只通知刷新当前页
   - 判定来自后端 verdict；「请求错误 / 未完成」是 status 维度，
     不是判定档：failed 只出现在 normal/subtask 里，不计入降级。
   ============================================================ */
'use strict';

const DEFAULT_PAGE_SIZE = 50;
const THEME_KEY = 'cdm.theme';          // system | dark | light

/* 判定 / 证据 / 思考档位的文案与配色都跟后端语义一一对应 */
const VERDICT_CN = { normal:'正常', subtask:'子任务', incomplete:'不完整', downgrade:'降级' };
const EVIDENCE_CN = { memory_websocket:'内存配对', rollout_token_usage:'会话记录', codex_log_prefix:'codex 日志' };
const PENDING_STATES = ['queued', 'in_progress'];
const ERROR_STATES = ['failed', 'cancelled'];
const STATUS_CN = { failed:'请求错误', cancelled:'已取消' };

const S = {
  responses: new Map(),      // rid -> record
  stats:     {},
  filter:    'all',
  q:         '',
  page: 1, pageSize: DEFAULT_PAGE_SIZE, total: 0, pages: 1,
  start: null, end: null, range: 'all',
  loading: false, pageError: '',
  connected: false,
  dirty:     false,
  lastScan:  null,
  selectedRid: null, selected: null,
};

const $ = (id) => document.getElementById(id);
const el = {
  win: $('win'),
  navVerdict: $('nav-verdict'),
  viewTitle: $('view-title'), viewCount: $('view-count'),
  list: $('list'), empty: $('empty'), emptyTitle: $('empty-title'), emptyHint: $('empty-hint'),
  foot: $('foot-summary'), footErr: $('foot-error'), footLoading: $('foot-loading'), footPage: $('page-label'),
  inspector: $('inspector'),
  search: $('search'),
  toast: $('toast'),
  sheetWrap: $('sheet-wrap'), sheet: $('sheet'), sheetTitle: $('sheet-title'),
  sheetBody: $('sheet-body'), sheetFoot: $('sheet-foot'),
};

/* ---------------------------------------------------------- 工具 */
function esc(s){
  return String(s == null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function fmtBytes(n){
  if (n == null) return '—';
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n/1024).toFixed(1) + ' KB';
  if (n < 1073741824) return (n/1048576).toFixed(1) + ' MB';
  return (n/1073741824).toFixed(2) + ' GB';
}
function parseTs(s){
  if (!s) return null;
  const d = new Date(String(s).replace(' ', 'T'));
  return Number.isNaN(d.getTime()) ? null : d;
}
function shortId(rid){
  if (!rid) return '—';
  return String(rid).replace(/^resp_/, '').slice(0, 5);
}
function requestDuration(r){
  const status = r.status || '';
  if (!r.created_at) return '—';
  if (PENDING_STATES.includes(status) || ERROR_STATES.includes(status)) {
    // 错误/进行中的请求没有可靠的完成时间，不拿创建时间假装耗时
    if (ERROR_STATES.includes(status)) return '—';
    if (!r.completed_at) return '进行中';
  }
  if (!r.completed_at) return PENDING_STATES.includes(status) ? '进行中' : '—';
  let seconds = r.duration_seconds;
  if (seconds === undefined){
    const a = parseTs(r.created_at), b = parseTs(r.completed_at);
    seconds = (a && b) ? (b - a) / 1000 : null;
  }
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return '—';
  if (seconds < 60) return `${seconds} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds/60)} 分 ${seconds%60} 秒`;
  return `${Math.floor(seconds/3600)} 时 ${Math.floor(seconds%3600/60)} 分 ${seconds%60} 秒`;
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
/* 思考档位：与 web/style.css 的 .effort-* 同一套语义
   max 紫 / high·xhigh 淡蓝 / medium·low·minimal 淡绿 / 未知默认灰 */
function effortClass(effort){
  if (effort === 'max') return 'e-max';
  if (effort === 'high' || effort === 'xhigh') return 'e-high';
  if (effort === 'medium' || effort === 'low' || effort === 'minimal') return 'e-low';
  return '';
}
function isPending(r){ return PENDING_STATES.includes(r.status); }
function isError(r){ return ERROR_STATES.includes(r.status); }
function verdictOf(r){
  const v = r.verdict || 'normal';
  return VERDICT_CN[v] ? v : 'normal';
}
/* 展示用的判定文案：错误/未完成优先于 verdict */
function verdictLabel(r){
  if (isError(r)) return STATUS_CN[r.status] || '请求错误';
  return (VERDICT_CN[verdictOf(r)] || verdictOf(r)) + (isPending(r) ? '-未完成' : '');
}
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

/* ---------------------------------------------------------- 配色方案 */
/* ?theme=dark|light 只覆盖本次会话，用于截图/演示，不落盘 */
const URL_THEME = (() => {
  const v = new URLSearchParams(location.search).get('theme');
  return (v === 'dark' || v === 'light') ? v : null;
})();
function themePref(){ return URL_THEME || localStorage.getItem(THEME_KEY) || 'system'; }
function systemTheme(){
  return window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
}
function applyTheme(pref){
  const resolved = pref === 'system' ? systemTheme() : pref;
  document.documentElement.dataset.theme = resolved;
}
function setThemePref(pref){
  localStorage.setItem(THEME_KEY, pref);
  applyTheme(pref);
}
applyTheme(themePref());
if (window.matchMedia){
  const mq = window.matchMedia('(prefers-color-scheme: light)');
  const onSys = () => { if (themePref() === 'system') applyTheme('system'); };
  mq.addEventListener ? mq.addEventListener('change', onSys) : mq.addListener(onSys);
}

/* ---------------------------------------------------------- 浮层 */
function openSheet({ title, body, note, buttons }){
  const specs = buttons || [{ label:'关闭', primary:true }];
  el.sheetTitle.textContent = title;
  el.sheetBody.innerHTML = body;
  el.sheetFoot.innerHTML =
    (note ? `<span class="sheet-note">${note}</span>` : '') +
    '<span class="grow"></span>' +
    specs.map((b, i)=>`<button type="button" class="sbtn${b.primary ? ' primary' : ''}${b.danger ? ' danger' : ''}" data-act="${i}">${esc(b.label)}</button>`).join('');
  el.sheetFoot.querySelectorAll('[data-act]').forEach(btn=>{
    btn.addEventListener('click', async ()=>{
      const spec = specs[Number(btn.dataset.act)];
      if (!spec) return;
      // 处理器自己决定要不要关闭：校验失败或保存失败时返回 false，弹层保持打开
      if (spec.onClick && await spec.onClick() === false) return;
      closeSheet();
    });
  });
  el.sheetWrap.classList.remove('hidden');
}
function closeSheet(){
  el.sheetWrap.classList.add('hidden');
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
  es.onerror = () => { S.connected = false; markDirty(); };
}
function applySnapshot(data){
  S.stats = data.stats || {};
  schedulePageRefresh(0);
  markDirty();
}
function applyPatch(msg){
  if (msg.upserts && msg.upserts.length) schedulePageRefresh();
  if (msg.alerts && msg.alerts.length) toast('⚠ 检测到模型降级', true);
  if (msg.stats) S.stats = msg.stats;
  markDirty();
}

/* ---------------------------------------------------------- 历史分页 */
let pageTimer = null, pageController = null, pageGeneration = 0;
function schedulePageRefresh(delay = 250){
  if (pageTimer !== null) return;
  pageTimer = setTimeout(()=>{ pageTimer = null; loadPage(); }, delay);
}
function resetPage(){ S.page = 1; clearTimeout(pageTimer); pageTimer = null; loadPage(); }
async function loadPage(){
  const generation = ++pageGeneration;
  pageController?.abort();
  pageController = new AbortController();
  if (S.start !== null && S.end !== null && S.start > S.end){
    S.loading = false; S.pageError = '开始时间不能晚于结束时间，请检查所选时间。';
    markDirty(); return;
  }
  S.loading = true; S.pageError = ''; markDirty();
  const params = new URLSearchParams({ page:S.page, page_size:S.pageSize, filter:S.filter, q:S.q });
  if (S.start !== null) params.set('start', S.start);
  if (S.end !== null) params.set('end', S.end);
  try{
    const response = await fetch('/api/responses?' + params, {signal:pageController.signal});
    const data = await response.json();
    if (!response.ok) throw new Error(data.reason || '历史记录加载失败');
    if (generation !== pageGeneration) return;
    S.responses = new Map((data.responses || []).map(r=>[r.rid, r]));
    S.total = data.total; S.page = data.page; S.pages = data.pages;
    if (S.selectedRid && S.responses.has(S.selectedRid)) S.selected = S.responses.get(S.selectedRid);
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

/* ---------------------------------------------------------- 侧边栏 */
const STATUS_MAP = {
  running:  ['live', '采集中'],
  starting: ['warn', '启动中'],
  no_process:['warn', '未发现 codex.exe'],
  denied:   ['dead', '进程无权限'],
  stopped:  ['dead', '已停止'],
  offline:  ['dead', '连接断开'],
};
function renderSidebar(){
  const st = S.stats;
  const status = st.status || (S.connected ? 'starting' : 'offline');
  const [dotCls, label] = STATUS_MAP[status] || ['warn', status];
  const liveDot = $('live-dot');
  liveDot.className = 'sdot ' + dotCls;
  const statusText = $('status-text');
  statusText.textContent = S.connected ? label : '连接断开';
  statusText.className = S.connected ? dotCls : 'dead';

  $('p-hz').textContent      = st.hz ? st.hz.toFixed(1) + ' 轮/秒' : '—';
  $('p-workers').textContent = (st.active_workers != null && st.workers != null)
                                ? `${st.active_workers} / ${st.workers}` : '—';
  $('p-cost').textContent    = st.scan_cost != null ? Math.round(st.scan_cost * 1000) + ' ms' : '—';
  $('p-rounds').textContent  = st.rounds ?? 0;

  $('s-captured').textContent = st.captured ?? 0;
  $('s-paired').textContent   = st.paired ?? 0;
  S.lastScan = st.last_scan || null;

  const c = st.counts || {};
  $('n-all').textContent        = st.captured ?? 0;
  $('n-normal').textContent     = c.normal ?? 0;
  $('n-subtask').textContent    = c.subtask ?? 0;
  $('n-incomplete').textContent = c.incomplete ?? 0;
  $('n-downgrade').textContent  = c.downgrade ?? 0;
  $('n-error').textContent      = c.error ?? 0;

  el.navVerdict.querySelectorAll('.nav-item').forEach(b=>{
    b.classList.toggle('on', b.dataset.filter === S.filter);
  });

  const hint = $('status-hint');
  hint.textContent = status === 'no_process'
    ? '当前未发现运行中的 codex.exe —— 启动 Codex 后会自动接入。'
    : (status === 'denied'
        ? '打开进程失败，请尝试以管理员身份重新启动采集器。'
        : (S.lastScan ? `最后采样 ${hhmmss(S.lastScan)} · ${relTime(S.lastScan)}` : ''));
}

/* ---------------------------------------------------------- 列表 */
const seenRids = new Set();
let lastListSig = '';

/* 展示用判定：请求错误是状态派生出来的判定档，橙色标记 */
function rowVerdict(r){ return isError(r) ? 'err' : verdictOf(r); }

function rowHtml(r){
  const v = rowVerdict(r);
  const pending = isPending(r);
  const error = isError(r);
  const req = r.req_model;
  const state = r.status || '';
  const cls = [
    'row',
    `v-${v}`,
    pending ? 'is-pending' : '',
    error ? 'is-error' : '',
    r.rid === S.selectedRid ? 'is-sel' : '',
    seenRids.has(r.rid) ? 'no-anim' : '',
  ].filter(Boolean).join(' ');
  if (!seenRids.has(r.rid)) seenRids.add(r.rid);

  const dot = `<i class="vdot v-${v}${(v === 'downgrade' || v === 'err') ? ' soft' : ''}"></i>`;
  const reqCell = req
    ? `<span class="cell model req" title="${esc(req)}">${esc(req)}</span>`
    : '<span class="cell model req gap" title="未采到请求模型">?</span>';
  const effort = r.effort
    ? `<span class="effort ${effortClass(r.effort)}" title="思考档位 ${esc(r.effort)}">${esc(r.effort)}</span>`
    : '<span class="effort"></span>';

  return `<article class="${cls}" data-rid="${esc(r.rid)}" tabindex="0">
    ${dot}
    ${reqCell}
    <svg class="arrow" viewBox="0 0 13 13" fill="none" aria-hidden="true"><path d="M1.5 6.5H10" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/><path d="M7.4 3.6L10.4 6.5L7.4 9.4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
    <span class="cell model res" title="${esc(r.model || '')}">${esc(r.model || '—')}</span>
    ${effort}
    <span></span>
    <span class="cell dur" title="请求总耗时">${esc(requestDuration(r))}</span>
    <i class="sdot st st-${esc(state)}"></i>
    <span class="cell state st-${esc(state)}" title="${esc(state)}">${esc(state || '—')}</span>
  </article>`;
}

function dayLabel(r){
  const d = parseTs(r.created_at) || (r.first_seen ? new Date(r.first_seen*1000) : null);
  if (!d) return '未知时间';
  const today = new Date(); today.setHours(0,0,0,0);
  const day = new Date(d); day.setHours(0,0,0,0);
  const diff = Math.round((today - day) / 86400000);
  const md = `${d.getMonth()+1} 月 ${d.getDate()} 日`;
  if (diff === 0) return `今天 · ${md}`;
  if (diff === 1) return `昨天 · ${md}`;
  if (diff === 2) return `前天 · ${md}`;
  return `${d.getFullYear()} 年 ${md}`;
}

const VIEW_TITLE = { all:'全部请求', normal:'正常响应', subtask:'子任务',
                     incomplete:'证据不完整', downgrade:'模型降级',
                     error:'请求错误', suspect:'疑似样本' };

function renderList(){
  const shown = [...S.responses.values()];
  el.viewTitle.textContent = VIEW_TITLE[S.filter] || '请求列表';
  el.viewCount.textContent = S.total;
  el.foot.textContent = `已显示 ${shown.length} / ${S.total} 条`;
  $('page-number').value = S.page;
  $('page-number').max = S.pages;
  el.footPage.textContent = S.pages;
  el.footErr.textContent = S.pageError || '';
  el.footLoading.textContent = S.loading ? '加载中…' : '';
  $('page-prev').disabled = S.loading || S.page <= 1;
  $('page-next').disabled = S.loading || S.page >= S.pages;
  el.list.setAttribute('aria-busy', String(S.loading));

  const listEmpty = shown.length === 0;
  el.empty.classList.toggle('hidden', !listEmpty);
  el.list.classList.toggle('hidden', listEmpty);
  if (listEmpty){
    el.emptyTitle.textContent = S.loading ? '正在加载历史记录…'
      : (S.filter !== 'all' || S.q || S.start !== null || S.end !== null
          ? '没有符合当前筛选条件的记录' : '尚未捕获到任何响应对象');
    const st = S.stats;
    el.emptyHint.textContent = st.status === 'no_process'
      ? '当前未发现运行中的 codex.exe —— 启动 Codex 后会自动接入。'
      : '点击「诊断」可查看一次原始内存扫描结果。';
  }

  const parts = [S.filter, S.q, S.page, S.range, S.start ?? '', S.end ?? '', S.selectedRid ?? '', shown.length];
  for (const r of shown) parts.push(r.rid, r.verdict, r.status, r.model, r.req_model, r.effort,
                                    r.created_at, r.completed_at, r.duration_seconds, r.marks, r.updates, r.suspect_reason);
  const sig = parts.join('\u0001');
  if (sig === lastListSig) return;
  lastListSig = sig;

  let html = '', lastDay = '';
  for (const r of shown){
    const day = dayLabel(r);
    if (day !== lastDay){ html += `<div class="group-head">${esc(day)}</div>`; lastDay = day; }
    html += rowHtml(r);
  }
  el.list.innerHTML = html;
  el.list.querySelectorAll('.row').forEach(node=>{
    node.addEventListener('click', ()=>toggleRow(node.dataset.rid));
    node.addEventListener('keydown', e=>{
      if (e.key === 'Enter' || e.key === ' '){ e.preventDefault(); toggleRow(node.dataset.rid); }
    });
  });
}

/* 再点同一条 = 取消选中并收回详情 */
function toggleRow(rid){
  if (S.selectedRid === rid) clearSelection();
  else selectRow(rid);
}

function selectRow(rid){
  clearTimeout(clearSelection._t);
  S.selectedRid = rid;
  S.selected = S.responses.get(rid) || null;
  el.list.querySelectorAll('.row').forEach(n=>n.classList.toggle('is-sel', n.dataset.rid === rid));
  renderInspector();
}

function clearSelection(){
  if (!S.selectedRid) return;
  el.win.classList.remove('insp-open');
  el.list.querySelectorAll('.row').forEach(n=>n.classList.remove('is-sel'));
  S.selectedRid = null;
  // 内容留到收起动画播完再清空，否则面板会在动画中途变白
  clearTimeout(clearSelection._t);
  clearSelection._t = setTimeout(()=>{ S.selected = null; renderInspector(); }, 260);
}

/* 详情收起时列表铺满：宽屏收起第三列，窄屏是覆盖列表的二级页面 */
function syncInspectorLayout(){
  el.win.classList.toggle('insp-open', !!S.selectedRid);
}

/* ---------------------------------------------------------- 检查器 */
const META_ROW = (label, val, cls) =>
  `<div class="metarow"><em>${esc(label)}</em><b class="${cls || ''}">${esc(val ?? '—')}</b></div>`;

function renderInspector(){
  const r = S.selected;
  syncInspectorLayout();
  if (!r){
    el.inspector.innerHTML = '';
    return;
  }
  const v = rowVerdict(r);
  const pending = isPending(r);
  const error = isError(r);
  const badgeCls = pending && !error ? 'v-pending' : `v-${v}`;
  const sameModel = r.req_model && r.req_model === r.model;
  const resSideCls = error ? 'err' : (r.req_model ? (sameModel ? 'ok' : '') : 'gap');
  const arrowCls = error ? ' err' : (sameModel ? ' ok' : '');
  const effColor = r.effort ? `var(--${effortClass(r.effort) || 'text-2'})` : 'var(--text-2)';
  const lastSeen = r.last_seen ? `最后更新 ${hhmmss(r.last_seen)}` : '';

  el.inspector.innerHTML = `<div class="insp-inner">
    <div class="insp-head">
      <button type="button" class="insp-back" id="insp-back">
        <svg viewBox="0 0 14 14" fill="none" aria-hidden="true"><path d="M8.6 2.8L4.4 7L8.6 11.2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
        返回
      </button>
      <h3>请求详情</h3>
      <button type="button" class="iconbtn insp-close" id="insp-close" aria-label="收起详情">
        <svg viewBox="0 0 11 11" fill="none" aria-hidden="true"><path d="M2.5 2.5L8.5 8.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path d="M8.5 2.5L2.5 8.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
      </button>
    </div>

    <div class="idcard">
      <div class="idcard-head">
        <span>请求号</span>
        <button type="button" class="copybtn" id="insp-copy" title="复制完整请求号" aria-label="复制完整请求号">
          <svg viewBox="0 0 15 15" fill="none" aria-hidden="true"><rect x="5.3" y="5.3" width="7.9" height="7.9" rx="2" stroke="currentColor" stroke-width="1.4"/><path d="M9.8 2.2H4.2C3.1 2.2 2.2 3.1 2.2 4.2V9.8" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>
        </button>
      </div>
      <div class="idcard-val" id="insp-rid" title="双击复制">${esc(r.rid || '—')}</div>
    </div>

    <div class="verdict-row">
      <span class="vbadge ${badgeCls}">${esc(verdictLabel(r))}</span>
      <span class="insp-time">${esc(lastSeen)}</span>
    </div>

    <div class="flowcard">
      <span class="card-title">模型流转</span>
      <div class="flowrow">
        <div class="flowside">
          <em>请求</em>
          <b>${esc(r.req_model || '?')}</b>
        </div>
        <svg class="flowarrow${arrowCls}" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M2 8H11.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><path d="M8.6 4.8L11.9 8L8.6 11.2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
        <div class="flowside res ${resSideCls}">
          <em>响应</em>
          <b>${esc(r.model || '—')}</b>
        </div>
      </div>
    </div>

    <div class="metatable">
      ${META_ROW('判定', verdictLabel(r))}
      ${META_ROW('思考档位', r.effort || '—', 'eff')}
      ${META_ROW('状态', r.status || '—', error ? 'bad' : (pending ? 'hl' : ''))}
      ${META_ROW('请求总耗时', requestDuration(r), 'sans hl')}
      ${META_ROW('创建', r.created_at)}
      ${META_ROW('完成', r.completed_at)}
      ${META_ROW('标记', r.marks)}
      ${META_ROW('状态更新', `${r.updates ?? 0} 次`, 'sans')}
      ${META_ROW('证据来源', EVIDENCE_CN[r.evidence_source] || r.evidence_source || '—', 'sans')}
      ${META_ROW('配对方式', r.pairing_status || '—', 'sans')}
      ${r.expect ? META_ROW('采集时预期', r.expect, 'sans') : ''}
    </div>

    <div class="tagline">
      ${r.text_format ? `<span class="tag">格式 ${esc(r.text_format)}</span>` : ''}
      ${r.suspect_reason ? `<span class="tag">样本说明 ${esc(r.suspect_reason)}</span>` : ''}
      ${shortId(r.rid) ? `<span class="tag">${esc(shortId(r.rid))}</span>` : ''}
    </div>
  </div>`;

  const effNode = el.inspector.querySelector('.metarow b.eff');
  if (effNode) effNode.style.color = effColor;

  $('insp-back').addEventListener('click', clearSelection);
  $('insp-close').addEventListener('click', clearSelection);
  const copy = ()=>copyText(r.rid);
  $('insp-copy').addEventListener('click', copy);
  $('insp-rid').addEventListener('dblclick', copy);
}

function copyText(text){
  if (!text) return;
  if (!navigator.clipboard){ toast('当前环境不支持剪贴板，请使用本地浏览器打开', true); return; }
  navigator.clipboard.writeText(text).then(()=>toast('已复制完整请求号')).catch(()=>toast('复制失败', true));
}

/* ---------------------------------------------------------- 日志 */

function render(){
  renderSidebar();
  renderList();
  renderInspector();
}

/* ---------------------------------------------------------- 时间范围 */
function rangeFor(kind){
  if (kind === 'today'){
    const d = new Date(); d.setHours(0,0,0,0);
    return [d.getTime()/1000, null];
  }
  if (kind === '7d') return [Date.now()/1000 - 7*86400, null];
  return [null, null];
}
function setRange(kind){
  S.range = kind;
  [S.start, S.end] = rangeFor(kind);
  const seg = $('range-seg');
  seg.querySelectorAll('button').forEach(b=>b.classList.toggle('on', b.dataset.range === kind));
  const link = $('range-custom');
  link.textContent = kind === 'custom' ? `${fmtRangeLabel()} · 点击修改` : '自定义时间范围…';
  resetPage();
}
function fmtRangeLabel(){
  const f = (s)=> s ? new Date(s*1000).toLocaleString('zh-CN', {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'}) : '不限';
  return `${f(S.start)} → ${f(S.end)}`;
}
function toLocalInput(s){
  if (s === null || s === undefined) return '';
  const d = new Date(s*1000);
  const p = (n)=>String(n).padStart(2,'0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function openRangeSheet(){
  openSheet({
    title:'自定义时间范围',
    body:`
      <div class="field"><label for="rg-start">开始时间</label>
        <input id="rg-start" type="datetime-local" step="1" value="${esc(toLocalInput(S.start))}"></div>
      <div class="field"><label for="rg-end">结束时间</label>
        <input id="rg-end" type="datetime-local" step="1" value="${esc(toLocalInput(S.end))}">
        <span class="hint">按创建时间筛选；留空表示不限。缺失创建时间时回退到首次采集时间。</span></div>`,
    buttons:[
      { label:'取消' },
      { label:'清除', onClick:()=>{ setRange('all'); toast('已清除时间筛选'); } },
      { label:'应用', primary:true, onClick:()=>{
          const a = $('rg-start').value, b = $('rg-end').value;
          const sa = a ? new Date(a).getTime()/1000 : null;
          const sb = b ? new Date(b).getTime()/1000 : null;
          if (sa !== null && sb !== null && sa > sb){ toast('开始时间不能晚于结束时间', true); return false; }
          S.range = 'custom'; S.start = sa; S.end = sb;
          $('range-seg').querySelectorAll('button').forEach(x=>x.classList.remove('on'));
          $('range-custom').textContent = `${fmtRangeLabel()} · 点击修改`;
          resetPage();
        } },
    ],
  });
}

/* ---------------------------------------------------------- 诊断 */
function statusLabel(s){
  return { running:'采集中', starting:'启动中', no_process:'未发现 codex.exe',
           denied:'进程无权限', stopped:'已停止' }[s] || s || '未知';
}
async function doDiagnose(){
  const d = await fetch('/api/diagnose').then(r=>r.json()).catch(e=>({ok:false, reason:String(e)}));
  const s = d.sweep || null;
  const head = `采集线程状态 : ${statusLabel(d.status)}${d.pid ? '  pid=' + d.pid : ''}
已扫轮数     : ${d.rounds ?? 0}   实测频率 : ${d.hz ? d.hz.toFixed(1) + ' 轮/秒' : '—'}   网页端订阅 : ${d.clients ?? 0}

最近一轮扫描（只读回放，未额外触发扫描）
  时间        : ${s ? s.ts : '—'}
  单轮耗时    : ${s ? s.cost + ' s' : '—'}
  单轮扫描量  : ${fmtBytes(s ? s.bytes : null)}
  可读区域数  : ${s ? s.regions : '—'}   并行线程 : ${s ? s.workers : '—'}
  预筛命中块  : ${s ? s.hit_blocks : '—'}
  原始响应对象: ${s ? s.raw_responses : '—'}   请求侧映射 : ${s ? s.raw_requests : '—'}
  新增 / 更新 : ${s ? s.upserts : '—'}${d.reason ? '\n\n原因：' + d.reason : ''}`;

  const rows = (d.items || []).map(it=>`<tr>
      <td title="${esc(it.rid)}">${esc(shortId(it.rid))}</td>
      <td>${esc(it.model)}</td>
      <td>${esc(it.req_model || '未配对')}</td>
      <td>${esc(it.effort)}</td>
      <td>${esc(it.status)}</td>
      <td>${esc(it.marks)}</td>
    </tr>`).join('');
  const isEmpty = !(d.items && d.items.length);
  const table = isEmpty ? `
    <h4>最近一轮响应对象</h4>
    <pre>${esc(d.ok && d.raw_responses === 0
      ? '最近一轮没有从内存里抽到响应对象 —— 响应对象只在请求生命周期内存在，换个 Codex 正在干活的时刻再看。'
      : '本轮没有可展示的响应对象。')}</pre>` : `
    <h4>最近一轮响应对象（最多 50 条）</h4>
    <table><thead><tr><th>响应ID</th><th>响应模型</th><th>请求模型</th><th>effort</th><th>状态</th><th>标记</th></tr></thead>
    <tbody>${rows}</tbody></table>`;

  const ev = d.evidence || null;
  const evSrc = ev?.index_sources || {};
  const evidence = ev ? `<h4>请求模型证据索引（只读旁路）</h4><pre>状态     : ${ev.enabled ? '启用' : '未启用'}${ev.codex_home ? '   目录 : ' + esc(ev.codex_home) : ''}
轮询     : 每 ${ev.poll_seconds}s   已轮询 ${ev.polls ?? 0} 次   最近一轮 ${ev.last_poll ? hhmmss(ev.last_poll) : '—'}
日志库   : ${esc(ev.log_db || '—')}   已读 ${ev.log_rows_indexed ?? 0} 行   会话文件 ${ev.rollout_files ?? 0} 个
来源分布 : 会话记录 ${evSrc.rollout_token_usage ?? 0} ｜ codex 日志 ${evSrc.codex_log_prefix ?? 0}
回补历史 : ${ev.backfilled ?? 0} 条   补齐完成态 ${ev.settled ?? 0} 条   键冲突 ${ev.conflicts ?? 0} 条${ev.last_error ? '\n最近错误 : ' + esc(ev.last_error) : ''}</pre>` : '';

  openSheet({
    title:'诊断 · 最近一轮观测',
    body:`<div class="diag"><pre>${esc(head)}</pre>${table}${evidence}</div>`,
    buttons:[{ label:'关闭', primary:true }],
  });
}

/* ---------------------------------------------------------- 设置 */
function doConfig(){
  const st = S.stats;
  const pref = themePref();
  openSheet({
    title:'设置',
    body:`
      <div class="field">
        <label for="cfg-expect">预期模型</label>
        <input id="cfg-expect" type="text" list="model-list" value="${esc(st.expect || '')}" placeholder="留空时不主动标记子任务">
        <span class="hint">对应 config.json 的 expect；只影响新记录，已有记录保留采集时的设置。</span>
      </div>
      <div class="field">
        <label for="cfg-interval">最小采样间隔 (ms)</label>
        <input id="cfg-interval" type="number" min="0" max="60000" step="any"
               value="${esc(st.min_interval_ms ?? (st.idle || 0) * 1000)}">
        <span class="hint">对应 min_interval_ms，0 表示连续扫描；从两轮开始时间计算。</span>
      </div>
      <div class="field">
        <label for="cfg-workers">并行线程</label>
        <input id="cfg-workers" type="number" min="1" max="16" step="1" value="${esc(st.workers ?? 4)}">
        <span class="hint">对应 workers，1～16，在当前轮结束后生效。</span>
      </div>
      <div class="field">
        <label>配色方案</label>
        <div class="seg lg" id="cfg-theme">
          <button type="button" data-pref="system"${pref === 'system' ? ' class="on"' : ''}>跟随系统</button>
          <button type="button" data-pref="dark"${pref === 'dark' ? ' class="on"' : ''}>深色</button>
          <button type="button" data-pref="light"${pref === 'light' ? ' class="on"' : ''}>浅色</button>
        </div>
        <span class="hint">仅作用于本机前端显示，保存在浏览器里，不写入 config.json。</span>
      </div>`,
    note:'保存后写入 config.json，立即生效，无需重启采集',
    buttons:[
      { label:'取消', onClick:()=>{ setThemePref(themePref()); } },
      { label:'保存', primary:true, onClick: saveConfig },
    ],
  });

  const seg = $('cfg-theme');
  const before = themePref();
  seg.querySelectorAll('button').forEach(b=>{
    b.addEventListener('click', ()=>{
      seg.querySelectorAll('button').forEach(x=>x.classList.toggle('on', x === b));
      applyTheme(b.dataset.pref);           // 即时预览
      seg.dataset.pending = b.dataset.pref; // 保存时才落盘
    });
  });
  seg.dataset.pending = before;
}

async function saveConfig(){
  const seg = $('cfg-theme');
  const chosen = seg?.dataset.pending || themePref();
  const expect = ($('cfg-expect')?.value || '').trim();
  const intervalInput = $('cfg-interval'), workersInput = $('cfg-workers');
  if (!intervalInput.value || !intervalInput.checkValidity() || !workersInput.value || !workersInput.checkValidity()){
    toast('间隔须为 0～60000ms，线程数须为 1～16 的整数', true);
    return false;                            // 校验失败：保持弹层打开
  }
  setThemePref(chosen);
  const min_interval_ms = Number(intervalInput.value), workers = Number(workersInput.value);
  const r = await post('/api/config', { expect, min_interval_ms, workers });
  if (r && r.ok){
    if (r.stats) S.stats = r.stats;
    markDirty();
    toast('设置已保存');
    return true;
  }
  if (r) toast(r.reason || '设置保存失败', true);
  return false;
}

/* ---------------------------------------------------------- 事件绑定 */
function bind(){
  el.navVerdict.querySelectorAll('.nav-item').forEach(b=>{
    b.addEventListener('click', ()=>{
      S.filter = b.dataset.filter;
      resetPage();
    });
  });

  $('range-seg').querySelectorAll('button').forEach(b=>{
    b.addEventListener('click', ()=>setRange(b.dataset.range));
  });
  $('range-custom').addEventListener('click', openRangeSheet);

  let searchTimer;
  el.search.addEventListener('input', ()=>{
    clearTimeout(searchTimer);
    searchTimer = setTimeout(()=>{ S.q = el.search.value.trim(); resetPage(); }, 120);
  });

  $('page-prev').addEventListener('click', ()=>{ if (S.page > 1){ --S.page; loadPage(); } });
  $('page-next').addEventListener('click', ()=>{ if (S.page < S.pages){ ++S.page; loadPage(); } });
  $('page-size').addEventListener('change', ()=>{ S.pageSize = Number($('page-size').value); resetPage(); });
  const gotoPage = ()=>{
    const value = Number($('page-number').value);
    if (!Number.isInteger(value) || value < 1 || value > S.pages){ toast('请输入有效页码', true); return; }
    S.page = value; loadPage();
  };
  $('page-number').addEventListener('keydown', e=>{ if (e.key === 'Enter') gotoPage(); });
  $('page-number').addEventListener('change', gotoPage);
  $('history-retry').addEventListener('click', ()=>loadPage());

  $('btn-diag').addEventListener('click', doDiagnose);
  $('status-detail').addEventListener('click', doDiagnose);
  $('btn-cfg').addEventListener('click', doConfig);
  $('btn-stop').addEventListener('click', doStop);

  $('sheet-close').addEventListener('click', closeSheet);
  el.sheetWrap.addEventListener('click', (e)=>{ if (e.target === el.sheetWrap) closeSheet(); });
  document.addEventListener('keydown', (e)=>{
    if (e.key !== 'Escape') return;
    if (!el.sheetWrap.classList.contains('hidden')) closeSheet();
    else if (S.selectedRid) clearSelection();
  });
}

/* 停止采集：用与设置一致的面板确认，不再用浏览器原生 confirm */
function doStop(){
  openSheet({
    title:'停止采集',
    body:`<div class="sheet-text">
      停止后采集线程会退出、本地服务随之关闭，本页面将失去连接。<br>
      历史记录与数据库不受影响，下次启动可以继续采集。
    </div>`,
    note:'',
    buttons:[
      { label:'取消' },
      { label:'停止采集', danger:true, onClick: async ()=>{
          await post('/api/shutdown');
          toast('已发送停止指令');
          setTimeout(()=>{
            const t = $('status-text');
            t.textContent = '服务已停止'; t.className = 'dead';
            $('live-dot').className = 'sdot dead';
          }, 400);
        } },
    ],
  });
}

/* ---------------------------------------------------------- 心跳：相对时间 */
setInterval(()=>{
  if (!S.lastScan) return;
  const hint = $('status-hint');
  hint.textContent = `最后采样 ${hhmmss(S.lastScan)} · ${relTime(S.lastScan)}`;
}, 1000);

/* ---------------------------------------------------------- 启动 */
bind();
setRange('all');       // 默认不限时间：避免打开时正好落在「今天」之外而看起来是空的
connect();
renderInspector();
