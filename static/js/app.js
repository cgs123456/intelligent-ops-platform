/* 中型企业智能运营平台 · 前端逻辑（增强版 v2）
 * 修复内容：
 *   P0-1 闭环执行 loading + 按钮禁用 + 状态文字
 *   P0-2 步骤完成后 toast 引导跳转
 *   P0-3 智能问答 SSE 流式输出
 *   P1-1 接入 SSE 实时进度
 *   P1-2 表格搜索/排序/分页
 *   P1-3 审核数量修改弹窗
 *   P1-4 错误信息脱敏 + 仪表盘自动刷新
 *   P2 键盘快捷键/导出/引导重开/快捷入口
 */

// ---- Token 管理 ----
let accessToken = localStorage.getItem('access_token') || '';
let refreshToken = localStorage.getItem('refresh_token') || '';

function getAuthHeaders() {
    return {
        'Content-Type': 'application/json',
        'Authorization': accessToken ? `Bearer ${accessToken}` : '',
    };
}

// P1-4: 统一错误脱敏 — 不向前端暴露 Python 堆栈
function sanitizeError(msg) {
    if (!msg) return '未知错误';
    const s = String(msg);
    // 拦截 Python 堆栈特征
    if (/Traceback|File "[^"]+", line \d+|raise \w+Error|sqlalchemy|werkzeug/i.test(s)) {
        return '服务异常，请联系管理员（详见服务端日志）';
    }
    // 截断超长错误
    if (s.length > 200) return s.slice(0, 200) + '...';
    return s;
}

const API = (path, opts) => fetch(path, {
    headers: getAuthHeaders(),
    ...opts
}).then(r => {
    if (r.status === 401) {
        return tryRefresh().then(ok => {
            if (ok) return fetch(path, { headers: getAuthHeaders(), ...opts }).then(r => r.json());
            showLogin();
            throw new Error('请重新登录');
        });
    }
    return r.json();
}).catch(e => {
    // P1-4: 网络异常也走脱敏
    throw new Error(sanitizeError(e.message));
});

// ---- Toast 通知（P0-2 引导跳转 + P1-4 错误提示）----
function showToast(msg, type = 'info', duration = 3500) {
    let host = document.getElementById('toastHost');
    if (!host) {
        host = document.createElement('div');
        host.id = 'toastHost';
        host.className = 'toast-host';
        document.body.appendChild(host);
    }
    const colors = {info: 'var(--primary)', success: 'var(--green)', warn: 'var(--amber)', error: 'var(--red)'};
    const div = document.createElement('div');
    div.className = 'toast';
    div.style.cssText = `background:var(--card);border-left:4px solid ${colors[type]||colors.info};` +
        `box-shadow:0 4px 12px rgba(0,0,0,.1);padding:12px 16px;margin-top:8px;border-radius:6px;` +
        `font-size:13px;color:var(--text);cursor:pointer;animation:toastIn .3s ease;`;
    div.innerHTML = msg;
    div.onclick = () => div.remove();
    host.appendChild(div);
    setTimeout(() => { div.style.opacity = '0'; setTimeout(() => div.remove(), 300); }, duration);
}

async function doLogin() {
    const username = document.getElementById('loginUser').value;
    const password = document.getElementById('loginPass').value;
    try {
        const r = await fetch('/api/v1/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password })
        }).then(r => r.json());
        if (r.error) {
            document.getElementById('loginError').textContent = r.error;
            return;
        }
        accessToken = r.access_token;
        refreshToken = r.refresh_token;
        localStorage.setItem('access_token', accessToken);
        localStorage.setItem('refresh_token', refreshToken);
        document.getElementById('loginPage').style.display = 'none';
        document.getElementById('app').style.display = '';
        loadDashboard(); loadOrders(); loadReport();
        // P1-4: 登录后必须改密提示
        if (r.must_change_password) {
            showToast('管理员首次登录，请尽快修改密码', 'warn', 6000);
        }
    } catch (e) {
        document.getElementById('loginError').textContent = '登录失败：' + sanitizeError(e.message);
    }
}

async function tryRefresh() {
    try {
        const r = await fetch('/api/v1/auth/refresh', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ refresh_token: refreshToken })
        }).then(r => r.json());
        if (r.access_token) {
            accessToken = r.access_token;
            localStorage.setItem('access_token', accessToken);
            return true;
        }
    } catch (e) {}
    return false;
}

function showLogin() {
    document.getElementById('loginPage').style.display = 'flex';
    document.getElementById('app').style.display = 'none';
}

async function logout() {
    try {
        await fetch('/api/v1/auth/logout', {
            method: 'POST',
            headers: getAuthHeaders(),
            body: JSON.stringify({ refresh_token: refreshToken })
        });
    } catch (e) {}
    accessToken = '';
    refreshToken = '';
    localStorage.removeItem('access_token');
    localStorage.removeItem('refresh_token');
    showLogin();
}

// Token 自动续期
setInterval(async () => {
    if (!refreshToken) return;
    const ok = await tryRefresh();
    if (!ok) showLogin();
}, 100 * 60 * 1000);

document.addEventListener('visibilitychange', () => {
    if (!document.hidden && accessToken) {
        try {
            const payload = JSON.parse(atob(accessToken.split('.')[1]));
            const exp = payload.exp * 1000;
            if (Date.now() > exp - 5 * 60 * 1000) {
                tryRefresh().then(ok => { if (!ok) showLogin(); });
            }
        } catch (e) {}
    }
});

// ---- Tab 切换 ----
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => switchTab(tab.dataset.tab));
});

function switchTab(target) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    const tabEl = document.querySelector(`.tab[data-tab="${target}"]`);
    if (tabEl) tabEl.classList.add('active');
    const panel = document.getElementById('tab-' + target);
    if (panel) panel.classList.add('active');
    if (target === 'dashboard') { loadDashboard(); loadOrders(); loadReport(); }
    if (target === 'erp') { loadInventory(); loadAccount(); loadWarehouses(); }
    if (target === 'rpa') { loadQuotes(); loadScheduleStatus(); }
    if (target === 'fde') { loadWarehouseStats(); loadAds(); loadLineage(); loadDQ(); }
    if (target === 'aigc') { loadSuggestions(); loadChatHistory(); }
    if (target === 'loop') { loadLoopStatus(); }
}

const fmt = n => '¥' + Number(n).toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2});
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// ---- 仪表盘 ----
async function loadDashboard() {
  try {
    const d = await API('/api/dashboard');
    document.getElementById('m-skus').textContent = d.total_skus;
    document.getElementById('m-inv').textContent = fmt(d.inventory_value);
    document.getElementById('m-sales').textContent = fmt(d.sales_7d);
    document.getElementById('m-low').textContent = d.low_stock_count + ' 个';
    document.getElementById('m-low-card').classList.toggle('alert', d.low_stock_count > 0);
    document.getElementById('m-pending').textContent = d.pending_suggestions;
  } catch (e) { showToast('仪表盘加载失败：' + sanitizeError(e.message), 'error'); }
}
// P1-4: 仪表盘 30 秒自动刷新
setInterval(() => {
    if (document.querySelector('.tab.active')?.dataset.tab === 'dashboard') loadDashboard();
}, 30 * 1000);

async function loadReport() {
  const r = await API('/api/aigc/report');
  document.getElementById('dailyReport').textContent = r.text;
}
async function genReport() {
  try {
    const r = await API('/api/aigc/generate-report', {method:'POST'});
    document.getElementById('dailyReport').textContent = r.text || '生成失败';
    loadDashboard();
    showToast('日报已生成', 'success');
  } catch (e) { showToast('生成失败：' + sanitizeError(e.message), 'error'); }
}
async function loadOrders() {
  const orders = await API('/api/v1/erp/orders');
  const html = orders.map(o => `
    <div class="order-item">
      <span><span class="tag ${o.type==='采购'?'tag-confirmed':'tag-ok'}">${o.type}</span> ${esc(o.product)} ×${o.qty}</span>
      <span style="color:var(--text2)">${esc(o.party)} · ${fmt(o.amount)} · ${o.time}</span>
    </div>`).join('');
  document.getElementById('recentOrders').innerHTML = html || '<div class="hint">暂无单据</div>';
}

// ---- P1-2: 表格搜索/排序/分页通用工具 ----
class TableState {
    constructor(id, cols, opts = {}) {
        this.id = id;
        this.cols = cols;          // [{key:'sku', label:'SKU', sortable:true}]
        this.allData = [];
        this.search = '';
        this.sortKey = null;
        this.sortDir = 1;
        this.page = 1;
        this.perPage = opts.perPage || 10;
    }
    setData(data) {
        this.allData = data || [];
        this.page = 1;
        this.render();
    }
    render() {
        let rows = this.allData;
        // 搜索
        if (this.search) {
            const q = this.search.toLowerCase();
            rows = rows.filter(r => this.cols.some(c => String(r[c.key] ?? '').toLowerCase().includes(q)));
        }
        // 排序
        if (this.sortKey) {
            rows = [...rows].sort((a, b) => {
                const av = a[this.sortKey], bv = b[this.sortKey];
                if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * this.sortDir;
                return String(av).localeCompare(String(bv)) * this.sortDir;
            });
        }
        // 分页
        const total = rows.length;
        const pages = Math.max(1, Math.ceil(total / this.perPage));
        this.page = Math.min(this.page, pages);
        const start = (this.page - 1) * this.perPage;
        const pageRows = rows.slice(start, start + this.perPage);
        this._renderHead();
        this._renderBody(pageRows);
        this._renderPager(total, pages);
    }
    _renderHead() {
        const thead = document.querySelector(`#${this.id} thead`);
        if (!thead) return;
        const sortIcon = k => this.sortKey === k ? (this.sortDir > 0 ? ' ▲' : ' ▼') : '';
        thead.innerHTML = `<tr>${this.cols.map(c =>
            `<th data-key="${c.key}" ${c.sortable ? 'class="sortable"' : ''}>${c.label}${c.sortable ? sortIcon(c.key) : ''}</th>`
        ).join('')}</tr>`;
        thead.querySelectorAll('th.sortable').forEach(th => {
            th.onclick = () => {
                const k = th.dataset.key;
                if (this.sortKey === k) this.sortDir *= -1;
                else { this.sortKey = k; this.sortDir = 1; }
                this.render();
            };
        });
    }
    _renderBody(rows) {
        const tbody = document.querySelector(`#${this.id} tbody`);
        if (!tbody) return;
        tbody.innerHTML = rows.map(r => `<tr>${this.cols.map(c =>
            `<td>${c.render ? c.render(r) : esc(r[c.key] ?? '')}</td>`
        ).join('')}</tr>`).join('');
    }
    _renderPager(total, pages) {
        let pager = document.getElementById(this.id + '-pager');
        if (!pager) {
            pager = document.createElement('div');
            pager.id = this.id + '-pager';
            pager.className = 'table-pager';
            document.querySelector(`#${this.id}`).after(pager);
        }
        if (pages <= 1) { pager.innerHTML = `共 ${total} 条`; return; }
        pager.innerHTML = `共 ${total} 条 · 第 ${this.page}/${pages} 页 ` +
            `<button class="btn btn-sm" onclick="tableGoPage('${this.id}',-1)" ${this.page===1?'disabled':''}>上一页</button>` +
            `<button class="btn btn-sm" onclick="tableGoPage('${this.id}',1)" ${this.page===pages?'disabled':''}>下一页</button>`;
    }
}
const _tableStates = {};
function tableSearch(id, q) { _tableStates[id].search = q; _tableStates[id].page = 1; _tableStates[id].render(); }
function tableGoPage(id, delta) { _tableStates[id].page += delta; _tableStates[id].render(); }

// ---- ERP ----
const invTable = new TableState('invTable', [
    {key:'sku', label:'SKU', sortable:true},
    {key:'name', label:'品名'},
    {key:'stock_qty', label:'库存', sortable:true},
    {key:'safety_stock', label:'安全线', sortable:true},
    {key:'is_low', label:'状态', render:r => `<span class="tag ${r.is_low?'tag-low':'tag-ok'}">${r.is_low?'低于安全线':'正常'}</span>`},
], {perPage:8});
_tableStates['invTable'] = invTable;

async function loadInventory() {
  const inv = await API('/api/v1/erp/inventory');
  invTable.setData(inv);
}
async function loadAccount() {
  const a = await API('/api/v1/erp/account');
  const payable = a.payable || a.total_payable || 0;
  const receivable = a.net_receivable || a.receivable || a.total_receivable || 0;
  document.getElementById('accountSummary').innerHTML = `
    <div class="acct-row"><span class="label">应付账款</span><span class="value" style="color:var(--red)">${fmt(payable)}</span></div>
    <div class="acct-row"><span class="label">应收账款(净)</span><span class="value" style="color:var(--green)">${fmt(receivable)}</span></div>
    <div class="acct-row"><span class="label">退款</span><span class="value">${fmt(a.refund || 0)}</span></div>`;
}
async function loadWarehouses() {
  const ws = await API('/api/v1/erp/warehouses');
  const html = ws.map(w => `<div class="wh-item">${esc(w.code)} ${esc(w.name)} (${esc(w.location||'')})</div>`).join('');
  const el = document.getElementById('warehouseList');
  if (el) el.innerHTML = html;
}

// ---- RPA ----
const quoteTable = new TableState('quoteTable', [
    {key:'supplier', label:'供应商', sortable:true},
    {key:'product', label:'产品'},
    {key:'price', label:'报价', sortable:true, render:r => fmt(r.price)},
    {key:'source', label:'来源'},
    {key:'date', label:'日期', sortable:true},
], {perPage:10});
_tableStates['quoteTable'] = quoteTable;

async function loadQuotes() {
  const quotes = await API('/api/v1/rpa/quotes');
  quoteTable.setData(quotes);
}
async function syncOrders() {
  try {
    const r = await API('/api/v1/rpa/sync-orders', {method:'POST'});
    const html = r.results.map(it => `
    <div class="sync-item">
      <span class="${it.status==='ok'?'sync-ok':'sync-fail'}">${it.status==='ok'?'✓':'✗'}</span>
      ${esc(it.platform || '')} · ${esc(it.product || it.reason || '')}
      ${it.status==='ok' ? `→ ${esc(it.order_no)}` : `（${esc(it.reason||'')}）`}
    </div>`).join('');
    document.getElementById('syncResult').innerHTML = html || '<div class="hint">无待同步订单</div>';
    loadDashboard();
    showToast(`同步完成：${r.results.filter(x=>x.status==='ok').length} 成功 / ${r.results.filter(x=>x.status!=='ok').length} 失败`, 'success');
  } catch (e) { showToast('同步失败：' + sanitizeError(e.message), 'error'); }
}
async function loadScheduleStatus() {
  const s = await API('/api/v1/rpa/schedule/status');
  const el = document.getElementById('scheduleStatus');
  if (el) el.innerHTML = `<div class="hint">调度：${s.schedule_enabled||s.enabled?'已启用':'未启用'} · 报价cron: ${esc(s.quote_cron||'-')} · 同步cron: ${esc(s.sync_cron||'-')}</div>`;
}

// ---- FDE ----
async function loadWarehouseStats() {
  const s = await API('/api/v1/fde/stats');
  const layers = [
    {key:'ODS', cls:'ods', name:'ODS 贴源层'},
    {key:'DWD', cls:'dwd', name:'DWD 明细层'},
    {key:'DWS', cls:'dws', name:'DWS 汇总层'},
    {key:'ADS', cls:'ads', name:'ADS 应用层'}
  ];
  const html = layers.map(l => {
    const items = Object.entries(s[l.key] || {}).map(([k,v]) => `<div class="wh-stat"><span>${k}</span><span>${v}</span></div>`).join('');
    return `<div class="wh-layer ${l.cls}"><h4>${l.name}</h4>${items}</div>`;
  }).join('');
  document.getElementById('warehouseStats').innerHTML = html;
}
async function runETL() {
  try {
    const r = await API('/api/v1/fde/run', {method:'POST'});
    showToast(`ETL 完成：ODS +${r.ods} / DWD +${r.dwd} / DWS +${r.dws} / ADS +${r.ads}`, 'success');
    loadWarehouseStats(); loadAds(); loadDashboard();
  } catch (e) { showToast('ETL 失败：' + sanitizeError(e.message), 'error'); }
}
const adsTable = new TableState('adsTable', [
    {key:'product_name', label:'产品', sortable:true},
    {key:'recent_7d_sales', label:'近7日销量', sortable:true},
    {key:'current_stock', label:'当前库存', sortable:true},
    {key:'in_transit', label:'在途', sortable:true},
    {key:'suggested_qty', label:'建议采购', sortable:true, render:r =>
        r.suggested_qty > 0 ? `<strong style="color:var(--red)">${r.suggested_qty}</strong>` : '无需补货'},
    {key:'suggested_supplier_name', label:'建议供应商'},
], {perPage:8});
_tableStates['adsTable'] = adsTable;
async function loadAds() {
  const d = await API('/api/v1/fde/ads');
  adsTable.setData(d.suggestions || []);
}
async function loadLineage() {
  const lin = await API('/api/v1/fde/lineage');
  const el = document.getElementById('lineageBox');
  if (!el) return;
  const html = lin.map(l => `<div class="lineage-item">${esc(l.upstream_table)} → ${esc(l.downstream_table)} <span class="hint">(${esc(l.layer||'')})</span></div>`).join('');
  el.innerHTML = html || '<div class="hint">暂无血缘</div>';
}
async function loadDQ() {
  const dq = await API('/api/v1/fde/data-quality');
  const el = document.getElementById('dqBox');
  if (!el) return;
  const html = Object.entries(dq).map(([table, tests]) => {
    const items = Object.entries(tests).map(([t, s]) => `<span class="tag ${s==='pass'?'tag-ok':'tag-low'}">${t}:${s}</span>`).join(' ');
    return `<div class="dq-item"><strong>${esc(table)}</strong> ${items}</div>`;
  }).join('');
  el.innerHTML = html || '<div class="hint">运行数据质量测试查看结果</div>';
}

// ---- AIGC ----
let selectedSuggestions = new Set();
async function loadSuggestions() {
  const list = await API('/api/v1/aigc/suggestions');
  selectedSuggestions.clear();
  if (!list.length) {
    document.getElementById('suggestionList').innerHTML = '<div class="hint">暂无待审核建议。可执行闭环步骤1生成建议。</div>';
    updateBatchBar();
    return;
  }
  const html = list.map(s => `
    <div class="suggestion-item" data-id="${s.id}">
      <div class="sug-head">
        <label class="sug-check"><input type="checkbox" onchange="toggleSelect(${s.id}, this.checked)"></label>
        <span class="sug-product">${esc(s.product_name)}</span>
        <span class="sug-amount">${fmt(s.amount)}</span>
        <span class="sug-conf">置信度 ${(s.confidence*100).toFixed(0)}%</span>
      </div>
      <div class="sug-detail">
        供应商：${esc(s.suggested_supplier_name)} · 数量：<span id="qty-${s.id}">${s.suggested_qty}件</span> · 单价：${fmt(s.unit_price)}<br>
        ${esc(s.reason)}
      </div>
      <div class="sug-actions">
        <button class="btn btn-sm btn-approve" onclick="reviewSuggestion(${s.id},'approve')">批准</button>
        <button class="btn btn-sm" onclick="editQty(${s.id}, ${s.suggested_qty})">修改数量</button>
        <button class="btn btn-sm btn-reject" onclick="reviewSuggestion(${s.id},'reject')">拒绝</button>
      </div>
    </div>`).join('');
  document.getElementById('suggestionList').innerHTML = html;
  updateBatchBar();
}
function toggleSelect(id, checked) {
  if (checked) selectedSuggestions.add(id); else selectedSuggestions.delete(id);
  updateBatchBar();
}
function updateBatchBar() {
  const bar = document.getElementById('batchBar');
  if (!bar) return;
  bar.style.display = selectedSuggestions.size > 0 ? 'flex' : 'none';
  bar.querySelector('.batch-count').textContent = selectedSuggestions.size;
}
async function batchReview(action) {
  const ids = Array.from(selectedSuggestions);
  try {
    await API('/api/v1/aigc/batch-review', {method:'POST', body: JSON.stringify({ids, action})});
    showToast(`已批量${action==='approve'?'批准':'拒绝'} ${ids.length} 条`, 'success');
    loadSuggestions(); loadLoopStatus(); loadDashboard();
  } catch (e) { showToast('批量审核失败：' + sanitizeError(e.message), 'error'); }
}

// P1-3: 修改数量弹窗 + final_qty
function editQty(id, currentQty) {
    const v = prompt(`修改最终采购数量（原建议 ${currentQty} 件）：`, currentQty);
    if (v === null) return;
    const qty = parseInt(v, 10);
    if (isNaN(qty) || qty < 0) { showToast('数量必须为非负整数', 'warn'); return; }
    const note = prompt('审核备注（可选）：', '') || '';
    reviewSuggestion(id, 'approve', {final_qty: qty, note});
}

async function reviewSuggestion(id, action, extra = {}) {
  try {
    const body = {id, action, ...extra};
    const r = await API('/api/v1/aigc/review', {method:'POST', body: JSON.stringify(body)});
    if (r.error) { showToast(sanitizeError(r.error), 'error'); return; }
    // P1-4: 即时 UI 反馈 — 无需手动刷新
    const card = document.querySelector(`.suggestion-item[data-id="${id}"]`);
    if (card) {
        card.style.opacity = '0.4';
        card.style.pointerEvents = 'none';
        card.querySelector('.sug-actions').innerHTML = `<span class="tag ${action==='approve'?'tag-ok':'tag-low'}">${action==='approve'?'已批准':'已拒绝'}</span>`;
    }
    showToast(`建议 ${id} 已${action==='approve'?'批准':'拒绝'}`, 'success', 2000);
    // 1 秒后异步刷新列表
    setTimeout(() => { loadSuggestions(); loadLoopStatus(); loadDashboard(); }, 800);
  } catch (e) { showToast('审核失败：' + sanitizeError(e.message), 'error'); }
}

// ---- 智能问答（P0-3 SSE 流式）----
let currentSessionId = localStorage.getItem('chat_session') || '';
let chatStreaming = false;

async function sendQuery() {
  const input = document.getElementById('chatInput');
  const q = input.value.trim();
  if (!q || chatStreaming) return;
  addChat(q, 'user');
  input.value = '';
  chatStreaming = true;
  // P0-3: 走 SSE 流式输出
  await streamQuery(q);
  chatStreaming = false;
}
function quickAsk(q) {
  document.getElementById('chatInput').value = q;
  sendQuery();
}
function addChat(text, role) {
  const box = document.getElementById('chatBox');
  const div = document.createElement('div');
  div.className = 'chat-msg ' + role;
  div.innerHTML = `<pre></pre>`;
  div.querySelector('pre').textContent = text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  return div.querySelector('pre');  // 返回 pre 以便流式追加
}
async function streamQuery(q) {
    // 创建一个空的 bot 消息 pre 用于流式追加
    const box = document.getElementById('chatBox');
    const div = document.createElement('div');
    div.className = 'chat-msg bot';
    const pre = document.createElement('pre');
    pre.textContent = '思考中...';
    div.appendChild(pre);
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;

    try {
        const resp = await fetch('/api/v1/aigc/query-stream', {
            method: 'POST',
            headers: getAuthHeaders(),
            body: JSON.stringify({question: q, session_id: currentSessionId || undefined}),
        });
        if (resp.status === 401) {
            const ok = await tryRefresh();
            if (!ok) { showLogin(); return; }
            return streamQuery(q);
        }
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let firstChunk = true;
        while (true) {
            const {done, value} = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, {stream: true});
            const lines = buffer.split('\n');
            buffer = lines.pop();
            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                const payload = line.slice(6).trim();
                if (!payload) continue;
                try {
                    const evt = JSON.parse(payload);
                    if (evt.type === 'session') {
                        if (evt.session_id) {
                            currentSessionId = evt.session_id;
                            localStorage.setItem('chat_session', evt.session_id);
                        }
                    } else if (evt.type === 'chunk') {
                        if (firstChunk) { pre.textContent = ''; firstChunk = false; }
                        pre.textContent += evt.text;
                        box.scrollTop = box.scrollHeight;
                    } else if (evt.type === 'done') {
                        // 完成
                    } else if (evt.type === 'error') {
                        pre.textContent = '错误：' + sanitizeError(evt.error);
                    }
                } catch (e) {}
            }
        }
    } catch (e) {
        pre.textContent = '请求失败：' + sanitizeError(e.message);
    }
}
async function loadChatHistory() {
  if (!currentSessionId) return;
  const msgs = await API('/api/v1/aigc/chat-history/' + currentSessionId);
  if (Array.isArray(msgs) && msgs.length > 0) {
    const box = document.getElementById('chatBox');
    box.innerHTML = '';
    msgs.forEach(m => {
      const div = document.createElement('div');
      div.className = 'chat-msg ' + (m.role === 'user' ? 'user' : 'bot');
      div.innerHTML = `<pre>${esc(m.content)}</pre>`;
      box.appendChild(div);
    });
    box.scrollTop = box.scrollHeight;
  }
}

// ---- 闭环 ----
async function loadLoopStatus() {
  const s = await API('/api/v1/loop/status');
  document.getElementById('runId').textContent = `轮次 #${s.run_id}`;
  const html = s.steps.map(st => {
    const cls = st.status === 'done' ? 'done' : (st.status === 'running' ? 'running' : (st.status === 'failed' ? 'failed' : ''));
    const cur = st.step === s.current_step ? 'current' : '';
    const statusText = {done:'✓ 完成', running:'执行中', pending:'待执行', failed:'✗ 失败', rolled_back:'↩ 已回滚'}[st.status] || st.status;
    return `<div class="loop-step ${cls} ${cur}">
      <div class="step-num">${st.step}</div>
      <div class="step-name">${st.name}</div>
      <div class="step-owner">${st.owner}</div>
      <div class="step-status">${statusText}</div>
      <div class="step-detail">${st.detail ? esc(st.detail) : ''}</div>
      ${st.status === 'done' ? `<button class="btn btn-sm btn-ghost" onclick="rollbackStep(${st.step})" style="margin-top:6px">回滚</button>` : ''}
    </div>`;
  }).join('');
  document.getElementById('loopSteps').innerHTML = html;
  const btn = document.getElementById('runStepBtn');
  const curStep = s.steps.find(x => x.step === s.current_step);
  // P0-1: 按钮状态根据执行中动态调整
  const isRunning = s.steps.some(x => x.status === 'running');
  if (isRunning) {
    btn.textContent = '执行中...'; btn.disabled = true;
  } else if (s.current_step > 5 || s.steps.every(x => x.status === 'done')) {
    btn.textContent = '闭环已完成'; btn.disabled = true;
  } else if (s.current_step === 2) {
    btn.textContent = '等待人工审核（去 AIGC Tab 审核）'; btn.disabled = true;
  } else {
    btn.textContent = `执行步骤${s.current_step}：${curStep ? curStep.name : ''}`; btn.disabled = false;
  }
}

// P0-1: 闭环执行 loading + 状态文字 + 按钮禁用
async function runCurrentStep() {
  const btn = document.getElementById('runStepBtn');
  const msg = document.getElementById('loopMessage');
  const s = await API('/api/v1/loop/status');
  // 锁定按钮
  const originalText = btn.textContent;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> 执行中...';
  msg.textContent = `正在执行步骤${s.current_step}，请稍候（最长 60s）...`;
  try {
    const r = await API('/api/v1/loop/run-step', {method:'POST', body: JSON.stringify({step: s.current_step})});
    if (r.need_manual) {
        msg.textContent = r.message;
        // P0-2: 引导 toast 跳转
        showToast(`步骤${r.step}完成，需人工审核 → <a href="javascript:switchTab('aigc')">前往 AIGC 审核</a>`, 'warn', 6000);
    } else if (r.error) {
        msg.textContent = '错误：' + sanitizeError(r.error);
        showToast('步骤执行失败：' + sanitizeError(r.error), 'error');
    } else {
        msg.textContent = `步骤${r.step}完成：${r.detail || ''}`;
        // P0-2: 步骤完成后 toast 提示
        if (r.step === 2) {
            showToast(`建议已生成，请前往 AIGC 审核 → <a href="javascript:switchTab('aigc')">前往审核</a>`, 'success', 6000);
        } else if (r.step === 5) {
            showToast('闭环全流程已完成 ✅ 数据已刷新', 'success', 6000);
            loadDashboard();
        } else {
            showToast(`步骤${r.step}执行完成`, 'success', 2500);
        }
    }
    loadLoopStatus(); loadDashboard();
  } catch (e) {
    msg.textContent = '执行失败：' + sanitizeError(e.message);
    showToast('执行失败：' + sanitizeError(e.message), 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
    loadLoopStatus();  // 重新加载以恢复按钮状态
  }
}

// P1-1: SSE 实时进度（监听闭环状态变化）
let _loopEventSource = null;
function startLoopStream() {
    if (_loopEventSource) _loopEventSource.close();
    try {
        _loopEventSource = new EventSource('/api/v1/loop/stream');
        _loopEventSource.onmessage = (e) => {
            try {
                const data = JSON.parse(e.data);
                if (data.event === 'end') return;
                // 仅当用户在闭环 Tab 时才更新
                if (document.querySelector('.tab.active')?.dataset.tab === 'loop') {
                    loadLoopStatus();
                }
            } catch (err) {}
        };
        _loopEventSource.onerror = () => { _loopEventSource.close(); };
    } catch (e) {}
}

async function rollbackStep(step) {
  if (!confirm(`确认回滚步骤${step}？`)) return;
  try {
    const r = await API('/api/v1/loop/rollback', {method:'POST', body: JSON.stringify({step})});
    if (r.status === 'rolled_back') {
        showToast(`步骤${step}已回滚${r.compensations ? '（补偿 ' + r.compensations.length + ' 项）' : ''}`, 'warn');
    } else {
        showToast('回滚失败：' + sanitizeError(r.error), 'error');
    }
    loadLoopStatus();
  } catch (e) { showToast('回滚失败：' + sanitizeError(e.message), 'error'); }
}
async function resetLoop() {
  if (!confirm('确认重置闭环状态？业务数据保留。')) return;
  await API('/api/v1/loop/reset', {method:'POST'});
  document.getElementById('loopMessage').textContent = '已重置';
  showToast('闭环已重置', 'success');
  loadLoopStatus();
}

// ---- P2: 键盘快捷键 ----
document.addEventListener('keydown', (e) => {
    // Ctrl/Cmd + 1~6 切换 Tab
    if ((e.ctrlKey || e.metaKey) && e.key >= '1' && e.key <= '6') {
        e.preventDefault();
        const tabs = ['dashboard', 'loop', 'erp', 'rpa', 'fde', 'aigc'];
        switchTab(tabs[parseInt(e.key) - 1]);
    }
    // Ctrl/Cmd + Enter 发送聊天
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        if (document.activeElement?.id === 'chatInput') sendQuery();
    }
    // ESC 关闭引导浮层
    if (e.key === 'Escape') {
        const ov = document.getElementById('guideOverlay');
        if (ov && ov.style.display !== 'none') closeGuide();
    }
});

// ---- P2: 引导浮层重开 ----
function reopenGuide() {
    localStorage.removeItem('guide_shown');
    document.getElementById('guideOverlay').style.display = 'flex';
}

// ---- P2: CSV 导出 ----
function exportTable(tableId, filename) {
    const table = document.getElementById(tableId);
    if (!table) return;
    const rows = [...table.querySelectorAll('tr')];
    const csv = rows.map(tr =>
        [...tr.querySelectorAll('th,td')].map(td => {
            const t = td.innerText.replace(/[\n\r,]/g, ' ').trim();
            return `"${t}"`;
        }).join(',')
    ).join('\n');
    // BOM 头让 Excel 正确识别 UTF-8
    const blob = new Blob(['\ufeff' + csv], {type: 'text/csv;charset=utf-8'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
    showToast('已导出 ' + filename, 'success');
}

// ---- 初始化 ----
if (!accessToken) {
    showLogin();
} else {
    document.getElementById('loginPage').style.display = 'none';
    loadDashboard(); loadOrders(); loadReport();
    startLoopStream();  // P1-1: 启动 SSE 监听
}
