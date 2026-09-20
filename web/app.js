/* ============================================================
   Codex 模型降级监控 · 前端逻辑
   - 数据来源：/api/stream (SSE) —— 首帧 snapshot，后续 patch
   - 纯前端渲染，不做任何请求轮询（除按钮触发的诊断/配置）
   ============================================================ */
'use strict';

const MAX_RENDER = 300;      // 单屏最多渲染卡片数
const MAX_LOGS   = 400;

const S = {
  responses: new Map(),      // rid -> record
  logs:      [],
  stats:     {},
  filter:    'all',
  q:         '',
  connected: false,
  dirty:     false,
  lastScan:  null,
};

const $ = (id) => document.getElementById(id);
const el = {
  dot: $('live-dot'), pill: $('status-pill'),
  mPid:$('m-pid'), mExpect:$('m-expect'), mHz:$('m-hz'), mCost:$('m-cost'),
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
  return rid.length > 26 ? rid.slice(0,14) + '…' + rid.slice(-10) : rid;
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
  S.responses.clear();
  (data.responses || []).forEach(r => S.responses.set(r.rid, r));
  S.logs = (data.logs || []).slice(-MAX_LOGS);
  S.stats = data.stats || {};
  renderLogPanel(true);
  markDirty();
}

function applyPatch(msg){
  (msg.upserts || []).forEach(r => S.responses.set(r.rid, r));
  // 顶部告警条已去掉；降级只留卡片本身 + 一次性提示
  if (msg.alerts && msg.alerts.length) toast('⚠ 检测到模型降级', true);
  if (msg.logs && msg.logs.length){
    S.logs = S.logs.concat(msg.logs).slice(-MAX_LOGS);
    renderLogPanel(false);
  }
  if (msg.stats) S.stats = msg.stats;
  markDirty();
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

  el.mPid.textContent      = st.pid ?? '—';
  el.mExpect.textContent   = st.expect ?? '—';
  el.mHz.textContent       = st.hz ? st.hz.toFixed(1) + ' 轮/秒' : '—';
  el.mCost.textContent     = st.scan_cost != null ? st.scan_cost.toFixed(3) + ' s' : '—';
  el.mBytes.textContent    = fmtBytes(st.scan_bytes);
  el.mWorkers.textContent  = (st.active_workers != null && st.workers != null)
                               ? `${st.active_workers} / ${st.workers}` : '—';
  el.mIdle.textContent     = st.idle > 0 ? st.idle + ' s'
                           : (st.idle === 0 ? '连续扫描' : '—');
  el.mRounds.textContent   = st.rounds ?? 0;
  el.mCaptured.textContent = st.captured ?? S.responses.size;
  el.mPaired.textContent   = st.paired ?? 0;
  S.lastScan = st.last_scan || null;
  el.mLast.textContent     = S.lastScan ? hhmmss(S.lastScan) : '—';

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
  const req = r.req_model;

  let swapCls = 'unknown', swapInner;
  if (req){
    swapCls = (req === r.model) ? 'ok' : 'bad';
    swapInner = `<em>请求</em><span class="req">${esc(req)}</span>
                 <span class="arrow">→</span>
                 <em>响应</em><span class="res">${esc(r.model)}</span>`;
  } else {
    // 没采到请求模型 —— 采集缺口，不是降级证据，所以标黄不标红
    swapInner = `<em>请求模型未采到</em><span class="res">无法配对，不能据此判降级</span>`;
  }

  const effortCls = r.effort === 'high' || r.effort === 'xhigh' ? 'effort-high'
                  : r.effort === 'low' ? 'effort-low' : '';

  const meta = (label, val, cls) =>
    `<span class="meta-item"><em>${label}</em><b class="${cls || ''}">${esc(val ?? '-')}</b></span>`;

  return `<article class="card v-${v}" data-rid="${esc(r.rid)}">
    <div class="card-main">
      <span class="badge${v==='downgrade'?' blink':''}">${VERDICT_CN[v] || v}</span>
      <span class="model" title="${esc(r.model)}">${esc(r.model)}</span>
      <span class="swapline ${swapCls}">${swapInner}</span>
      <span class="status" data-s="${esc(r.status || '')}">
        <i class="sd"></i>${esc(r.status || '-')}
      </span>
    </div>

    <div class="card-sub">
      <span class="rid" title="点击复制完整响应ID">${esc(r.rid)}</span>
      ${meta('effort', r.effort, effortCls)}
      ${meta('格式', r.text_format)}
      ${meta('创建', r.created_at)}
      ${meta('完成', r.completed_at)}
      ${meta('标识', r.safety_id)}
      ${meta('标记', r.marks)}
      ${r.updates ? `<span class="upd">状态更新 ${r.updates} 次</span>` : ''}
      <span class="subtime">${esc(hhmmss(r.last_seen))}</span>
    </div>
  </article>`;
}

/* 卡片内容签名：这些字段任一变化才需要重建这张卡的内容 */
function cardSig(r){
  return [r.model, r.req_model, r.verdict, r.effort, r.status, r.text_format,
          r.created_at, r.completed_at, r.safety_id, r.marks, r.updates,
          r.last_seen].join('\u0002');
}

function createCardNode(r){
  const tmp = document.createElement('div');
  tmp.innerHTML = cardHtml(r);
  const node = tmp.firstElementChild;
  // 只有本次会话首次出现的响应才播入场动画；
  // 因为筛选/搜索被回收再重建的卡片不播，避免整屏闪。
  if (seenRids.has(r.rid)) node.classList.add('no-anim');
  else seenRids.add(r.rid);
  node.querySelector('.rid')?.addEventListener('click', ()=>copy(r.rid));
  return node;
}

function updateCardNode(el, r){
  const tmp = document.createElement('div');
  tmp.innerHTML = cardHtml(r);
  const fresh = tmp.firstElementChild;
  el.className = fresh.className;        // 判定可能变（如 不完整 -> 降级）
  el.innerHTML = fresh.innerHTML;        // 外层 article 本身保留，动画不重放
  el.querySelector('.rid')?.addEventListener('click', ()=>copy(r.rid));
}

function renderCards(){
  const all = [...S.responses.values()]
    .sort((a,b)=> (b.last_seen||0) - (a.last_seen||0));
  const list = all.filter(matchFilter);
  const shown = list.slice(0, MAX_RENDER);

  const listEmpty = list.length === 0;
  el.empty.classList.toggle('hidden', !listEmpty);
  el.cards.classList.toggle('hidden', listEmpty);
  if (listEmpty){
    el.empty.querySelector('h2').textContent = all.length > 0
      ? '没有符合当前筛选条件的卡片'
      : '尚未捕获到任何响应对象';
  }
  el.more.classList.toggle('hidden', list.length <= MAX_RENDER);
  if (list.length > MAX_RENDER){
    el.more.textContent = `已隐藏较早的 ${list.length - MAX_RENDER} 条（可缩小筛选范围）`;
  }

  // 内容签名一致 => 完全不动 DOM。心跳广播、纯统计更新都走这条路。
  const parts = [S.filter, S.q, list.length];
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
  navigator.clipboard?.writeText(text)
    .then(()=>toast('已复制：' + shortId(text)))
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

  openModal('诊断 · 采集线程最近一轮观测', head + table, false);
}

function statusLabel(s){
  return { running:'采集中', starting:'启动中', no_process:'未发现 codex.exe',
           denied:'进程无权限', stopped:'已停止' }[s] || s || '未知';
}

function doConfig(){
  const st = S.stats;
  openModal('配置', `
    <div class="row"><label>预期模型</label>
      <input id="cfg-expect" value="${esc(st.expect || '')}"></div>
    <div class="row"><label>轮间空闲(s)</label>
      <input id="cfg-idle" value="${esc(st.idle ?? 0)}"
             placeholder="0 = 连续扫描，不歇"></div>
    <p style="margin:0;color:var(--text-3)">
      并行扫描线程数需重启生效（<code>--workers N</code>）；
      轮间空闲可在此热调整。</p>
  `, true);
}

async function saveConfig(){
  const expect = ($('cfg-expect')?.value || '').trim();
  const idle = parseFloat($('cfg-idle')?.value);
  const r = await post('/api/config', { expect, idle });
  if (r && r.ok){ closeModal(); toast('配置已更新'); }
}

function bind(){
  document.querySelectorAll('.cnt').forEach(b=>{
    b.addEventListener('click', ()=>{
      S.filter = (S.filter === b.dataset.filter) ? 'all' : b.dataset.filter;
      syncFilterBtns(); markDirty();
    });
  });
  document.querySelectorAll('#filters .fbtn').forEach(b=>{
    b.addEventListener('click', ()=>{
      S.filter = b.dataset.filter; syncFilterBtns(); markDirty();
    });
  });

  let sTimer;
  el.search.addEventListener('input', ()=>{
    clearTimeout(sTimer);
    sTimer = setTimeout(()=>{ S.q = el.search.value.trim(); markDirty(); }, 120);
  });

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
    if (!confirm('确定清空当前已捕获的全部响应？采集不会停止。')) return;
    await post('/api/clear');
    S.responses.clear(); S.logs = [];
    resetCardRender();
    renderLogPanel(true); markDirty(); toast('已清空');
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
