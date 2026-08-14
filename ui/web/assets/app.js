const $ = (id) => document.getElementById(id);
if (window.ChartZoom) Chart.register(ChartZoom);
const fmt$ = (n) => '$' + Number(n || 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtPct = (n) => (n >= 0 ? '+' : '') + Number(n || 0).toFixed(2) + '%';
const fmtQty = (n) => Number(n || 0).toLocaleString('en-US', { maximumFractionDigits: 4 });
const cls = (n) => n >= 0 ? 'pos' : 'neg';
const transactionClass = (type) => type === 'BUY' || type === 'DIVIDEND' ? 'pos' : 'neg';
const initials = (s) => (s || '?').slice(0, 2).toUpperCase();
const formatChartTimestamp = (value) => new Date(value).toLocaleString();
const badgeFor = (t, architecture) => {
  if (t === 'index_fund') return '<span class="badge index">Index</span>';
  if (architecture === 'multi_model') return '<span class="badge ensemble">AI Ensemble</span>';
  if (t === 'ai' || t === 'llm' || t === 'llm_agent') return '<span class="badge ai">AI</span>';
  return '<span class="badge human">Human</span>';
};

let lbData = [];
let sortKey = 'rank', sortDir = 1;
const riskCache = {}; // username -> {volatility, maxdd, pnl_history}
let decisionBatchStatus = null;

function decisionLabel(status) {
  return ({queued: 'Queued', running: 'Running', completed: 'Completed', completed_with_errors: 'Completed with errors', failed: 'Failed', interrupted: 'Interrupted', due: 'Due now', not_due: 'Not scheduled'})[status] || 'Ready to run';
}
function decisionDate(value, timezone) {
  return value ? new Intl.DateTimeFormat(undefined, {dateStyle: 'medium', timeStyle: 'short', timeZone: timezone}).format(new Date(value)) : 'Never';
}
function batchStatus(week) { return (week.current_batch || week.latest_batch || {}).status || 'idle'; }
function accountDecisionStatusText(detail) {
  const agent = decisionBatchStatus?.agents?.[detail.username];
  return `AI decision status: ${decisionLabel(agent?.status)}${agent?.completed_at ? ` · Last completed: ${new Date(agent.completed_at).toLocaleString()}` : ''}`;
}
function updateAccountDecisionStatus() {
  const status = $('account-decision-status');
  if (status && currentDetail?.user_type === 'llm_agent') status.textContent = accountDecisionStatusText(currentDetail);
}
function renderDecisionBatchStatus(status) {
  const week = status.days ? status : {days: [], current_batch: status.status === 'running' ? status : null, latest_batch: status, timezone: undefined, ai_account_count: status.counts?.total || 0};
  decisionBatchStatus = {...week, status: batchStatus(week), agents: (week.current_batch || week.latest_batch || {}).agents || {}};
  if (!$('view-leaderboard').hidden) renderTable();
  const btn = $('batch-decision-btn'), msg = $('batch-decision-msg'), times = $('batch-decision-times'), strip = $('decision-week');
  if (!btn || !msg || !times || !strip) return;
  const batch = week.current_batch || week.latest_batch || {};
  const running = batch.status === 'running';
  const eligible = !batch.next_eligible_at || new Date(batch.next_eligible_at) <= new Date();
  btn.disabled = running || !eligible;
  const counts = batch.counts || {};
  btn.textContent = batch.status === 'failed' || batch.status === 'interrupted' ? 'Retry decisions' : 'Run decisions now';
  msg.textContent = running ? `Running — ${counts.completed || 0} of ${counts.total || week.ai_account_count || 0} accounts complete${counts.failed ? ` · ${counts.failed} failed` : ''}` : batch.status === 'completed' || batch.status === 'completed_with_errors' ? `${decisionLabel(batch.status)} today · ${counts.completed || 0} completed${counts.failed ? ` · ${counts.failed} failed` : ''}` : (week.days.some(day => day.state === 'due') ? 'Due today — not run' : 'Ready to run');
  times.textContent = `Last run: ${decisionDate(batch.last_completed_at || batch.last_triggered_at, week.timezone)}${batch.next_eligible_at ? ` · Available again: ${decisionDate(batch.next_eligible_at, week.timezone)}` : ''}`;
  strip.replaceChildren(...week.days.map(day => {
    const state = day.state; const symbol = ({completed: '✓', completed_with_errors: '✓', due: '!', failed: '↻', interrupted: '↻', running: '…', not_due: '—'})[state] || '—';
    const label = `${day.weekday}, ${day.date}: ${decisionLabel(state)}${day.due_at ? `; reminder ${decisionDate(day.due_at, week.timezone)}` : ''}${day.run_count > 1 ? `; ${day.run_count} runs` : ''}`;
    const cell = document.createElement('div'); cell.className = `week-day${day.is_today ? ' today' : ''}`; cell.tabIndex = 0; cell.setAttribute('aria-label', label); cell.title = label;
    cell.innerHTML = `<span class="week-initial">${day.weekday.slice(0, 1)}</span><span class="week-date">${new Date(`${day.date}T12:00:00`).getDate()}</span><span class="week-state ${state}" aria-hidden="true">${symbol}</span>`;
    return cell;
  }));
  updateAccountDecisionStatus();
}
async function loadDecisionBatchStatus() {
  try { renderDecisionBatchStatus(await (await fetch('/api/decision-batches/week')).json()); } catch (_) {}
}
async function triggerDecisionBatch() {
  const btn = $('batch-decision-btn'); btn.disabled = true;
  try {
    const response = await fetch('/api/decision-batches', {method: 'POST'});
    const status = await response.json();
    if (!response.ok) renderDecisionBatchStatus(status); else await loadDecisionBatchStatus();
  } catch (error) { $('batch-decision-msg').textContent = `Failed: ${error.message}`; }
}
setInterval(() => { if (decisionBatchStatus?.current_batch?.status === 'running') loadDecisionBatchStatus(); }, 3000);

function renderFunnelStatus(status) {
  const btn = $('funnel-refresh-btn'), msg = $('funnel-refresh-msg'), times = $('funnel-refresh-times');
  if (!msg || !times) return;
  if (btn) btn.disabled = status.in_progress;
  const failed = !status.in_progress && status.last_result?.error;
  msg.textContent = status.in_progress ? 'Refresh running…' : failed ? 'Last refresh failed' : status.last_run ? 'Refresh complete' : 'Not run yet';
  const runLabel = status.in_progress ? 'Started' : 'Last run';
  times.textContent = `${runLabel}: ${decisionDate(status.last_run)}${status.next_run ? ` · Next scheduled: ${decisionDate(status.next_run)}` : ''}${failed ? ` · ${failed}` : ''}`;
}
async function loadFunnelStatus() {
  try { renderFunnelStatus(await (await fetch('/api/cycle/status')).json()); } catch (_) {}
}
async function triggerManualRefresh() {
  const btn = $('funnel-refresh-btn');
  btn.disabled = true;
  try {
    const response = await fetch('/api/cycle', {method: 'POST'});
    if (!response.ok) throw new Error((await response.json()).detail || 'Unable to start refresh');
    await loadFunnelStatus();
  } catch (error) {
    $('funnel-refresh-msg').textContent = `Failed: ${error.message}`;
    btn.disabled = false;
  }
}
setInterval(loadFunnelStatus, 30000);

// ---- Risk metrics computed client-side from pnl_history ----
function computeRisk(pnlHist) {
  const pts = (pnlHist || []).map(p => p.pnl_pct).filter(v => v != null);
  if (pts.length < 2) return { volatility: 0, maxdd: 0 };
  // period returns from pnl% series (relative to starting value baseline)
  const rets = [];
  for (let i = 1; i < pts.length; i++) rets.push((pts[i] - pts[i - 1]));
  const mean = rets.reduce((a, b) => a + b, 0) / rets.length;
  const variance = rets.reduce((a, b) => a + (b - mean) ** 2, 0) / rets.length;
  const volatility = Math.sqrt(variance);
  // max drawdown on equity (1 + pnl%/100)
  let peak = -Infinity, maxdd = 0;
  for (const v of pts) {
    const eq = 1 + v / 100;
    if (eq > peak) peak = eq;
    const dd = (peak - eq) / peak * 100;
    if (dd > maxdd) maxdd = dd;
  }
  return { volatility, maxdd };
}

async function fetchRisk(username) {
  if (riskCache[username]) return riskCache[username];
  try {
    const d = await (await fetch(`/api/agent-detail/${username}`)).json();
    const r = computeRisk(d.pnl_history);
    riskCache[username] = { ...r, pnl_history: d.pnl_history, detail: d };
  } catch { riskCache[username] = { volatility: 0, maxdd: 0, pnl_history: [] }; }
  return riskCache[username];
}

// ---- Sparkline ----
function sparkline(pnlHist) {
  const pts = (pnlHist || []).map(p => p.pnl_pct).filter(v => v != null);
  if (pts.length < 2) return '<span class="muted-text">—</span>';
  const w = 90, h = 26, min = Math.min(...pts), max = Math.max(...pts);
  const range = max - min || 1;
  const coords = pts.map((v, i) => {
    const x = (i / (pts.length - 1)) * w;
    const y = h - ((v - min) / range) * (h - 4) - 2;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  const color = pts[pts.length - 1] >= pts[0] ? 'var(--green)' : 'var(--red)';
  return `<svg class="spark" viewBox="0 0 ${w} ${h}"><polyline points="${coords}" fill="none" stroke="${color}" stroke-width="1.5"/></svg>`;
}

// ---- Leaderboard ----
async function loadLeaderboard({ includeSupplementary = false } = {}) {
  const data = await (await fetch('/api/leaderboard')).json();
  lbData = data;
  renderKPIs(data);
  renderTable();
  await renderLbChart();
  if (!includeSupplementary) return;
  loadPopular();
  for (const row of data) fetchRisk(row.username).then(renderTable);
}

let leaderboardRefreshInFlight = null, leaderboardRefreshPending = false;
async function refreshLeaderboard() {
  if (leaderboardRefreshInFlight) {
    leaderboardRefreshPending = true;
    return leaderboardRefreshInFlight;
  }
  leaderboardRefreshInFlight = (async () => {
    do {
      leaderboardRefreshPending = false;
      await loadLeaderboard();
      if (currentDetail) {
        const detail = await (await fetch(`/api/agent-detail/${currentDetail.username}`)).json();
        if (!detail.error && currentDetail?.username === detail.username) {
          currentDetail = detail;
          renderPortfolio(detail); renderHistory(detail);
          $('d-sub').innerHTML = `${fmt$(detail.portfolio.total_value)} · <span class="${cls(detail.portfolio.pnl_percent)}">${fmtPct(detail.portfolio.pnl_percent)}</span>`;
        }
      }
    } while (leaderboardRefreshPending);
  })();
  try { await leaderboardRefreshInFlight; } finally { leaderboardRefreshInFlight = null; }
}

const LB_COLORS = ['#0969da','#1a7f37','#9a6700','#cf222e','#8250df','#0550ae','#116329','#bc4c00','#a40e26','#953800'];
let lbChart = null, lbChartRequest = 0;
function syncLbChartZoomState() {
  const reset = $('lb-chart-reset');
  if (reset) reset.disabled = !lbChart?.isZoomedOrPanned?.();
}
function fitLbChartYAxis() {
  if (!lbChart) return;
  const { min, max } = lbChart.scales.x;
  const values = lbChart.data.datasets.flatMap(dataset => dataset.data)
    .filter(point => point && Number.isFinite(point.x) && Number.isFinite(point.y) && point.x >= min && point.x <= max)
    .map(point => point.y);
  if (!values.length) return;
  const low = Math.min(...values), high = Math.max(...values), padding = Math.max((high - low) * .05, 1);
  Object.assign(lbChart.options.scales.y, { min: low - padding, max: high + padding });
  lbChart.update('none');
}
function syncLbChartNavigation() {
  fitLbChartYAxis();
  syncLbChartZoomState();
}
function resetLbChartZoom() {
  if (!lbChart) return;
  lbChart.resetZoom();
  delete lbChart.options.scales.y.min;
  delete lbChart.options.scales.y.max;
  lbChart.update('none');
  syncLbChartZoomState();
}
async function renderLbChart() {
  const request = ++lbChartRequest;
  const el = $('lbChart');
  if (!el) return;
  try {
    const { history, users } = await (await fetch('/api/portfolio-history')).json();
    if (request !== lbChartRequest) return;
    const rankingsByUserId = new Map(lbData.map(ranking => [String(ranking.user_id), ranking]));
    const uids = [...new Set([...Object.keys(history), ...rankingsByUserId.keys()])];
    if (!uids.length) { el.parentElement.querySelector('.section-title').insertAdjacentHTML('afterend', ''); return; }
    const liveAt = new Date().toISOString();
    // Union of persisted snapshot timestamps plus the valuation shown in the table.
    const allTimes = [...new Set([...uids.flatMap(userId => (history[userId] || []).map(point => point.time)), liveAt])].sort();
    const datasets = uids.map((u, i) => {
      const ranking = rankingsByUserId.get(u);
      const byTime = Object.fromEntries((history[u] || []).map(point => [point.time, point.value]));
      let last = null;
      const data = allTimes.map(t => {
        if (t === liveAt && ranking) last = ranking.total_value;
        else if (byTime[t] != null) last = byTime[t];
        return { x: new Date(t).getTime(), y: last };
      });
      const color = LB_COLORS[i % LB_COLORS.length];
      return { label: users[u] || ranking?.username || u, data, borderColor: color, backgroundColor: 'transparent', tension: .25, pointRadius: 0, borderWidth: 2, spanGaps: true };
    });
    const previousRange = lbChart && lbChart.isZoomedOrPanned?.()
      ? { min: lbChart.scales.x.min, max: lbChart.scales.x.max } : null;
    if (lbChart) {
      lbChart.data.datasets = datasets;
      const values = datasets.flatMap(dataset => dataset.data.map(point => point.x));
      const first = Math.min(...values), last = Math.max(...values);
      if (previousRange && previousRange.min >= first && previousRange.max <= last) {
        Object.assign(lbChart.options.scales.x, previousRange);
      } else {
        delete lbChart.options.scales.x.min;
        delete lbChart.options.scales.x.max;
      }
      lbChart.update('none');
      syncLbChartNavigation();
      return;
    }
    lbChart = new Chart(el, {
      type: 'line',
      data: { datasets },
      options: {
        interaction: { mode: 'index', intersect: false },
        plugins: { legend: { position: 'top', labels: { color: '#1f2328', boxWidth: 12, font: { size: 11 } } },
          tooltip: { callbacks: {
            title: items => formatChartTimestamp(items[0].parsed.x),
            label: c => `${c.dataset.label}: $${Number(c.parsed.y).toLocaleString('en-US',{maximumFractionDigits:0})}`
          } },
          zoom: {
            limits: { x: { min: 'original', max: 'original', minRange: 86_400_000 } },
            pan: { enabled: true, mode: 'x', onPanComplete: syncLbChartNavigation },
            zoom: { mode: 'x', wheel: { enabled: true }, pinch: { enabled: true }, onZoomComplete: syncLbChartNavigation }
          } },
        onResize: syncLbChartZoomState,
        scales: {
          x: { type: 'linear', ticks: { color: '#656d76', maxTicksLimit: 8, callback: value => new Date(value).toLocaleDateString() }, grid: { color: '#d0d7de' } },
          y: { ticks: { color: '#656d76', callback: v => '$' + Number(v).toLocaleString('en-US',{maximumFractionDigits:0}) }, grid: { color: '#d0d7de' } }
        }
      }
    });
    syncLbChartNavigation();
  } catch (e) { console.error('leaderboard chart failed', e); }
}

let instrumentFilter = '';
function setInstrumentFilter(filter) {
  instrumentFilter = filter;
  document.querySelectorAll('[data-instrument-filter]').forEach(b => b.classList.toggle('active', b.dataset.instrumentFilter === filter));
  loadPopular();
}

async function loadPopular() {
  const el = $('popular-list');
  if (!el) return;
  try {
    const params = new URLSearchParams({limit: '100'});
    if (instrumentFilter) params.set('instrument_type', instrumentFilter);
    const data = await (await fetch(`/api/watchlist?${params}`)).json();
    const top = data
      .filter(s => s.price)
      .sort((a, b) => (b.volume || 0) - (a.volume || 0))
      .slice(0, 20);
    if (!top.length) { el.innerHTML = '<div class="loading">No data.</div>'; return; }
    el.innerHTML = top.map(s => {
      const ch = s.change_percent || 0;
      const vol = s.volume ? (s.volume >= 1e6 ? (s.volume / 1e6).toFixed(1) + 'M' : (s.volume / 1e3).toFixed(0) + 'K') : '';
      return `<div class="pop-row" data-action="open-drawer-ticker" data-arg="${s.ticker}">
        <div><div class="pop-t">${s.ticker}${s.instrument_type === 'etf' ? '<span class="badge etf">ETF</span>' : ''}</div><div class="pop-vol">${s.category || (s.sector !== 'Unknown' ? s.sector : '')}${vol ? ' · Vol ' + vol : ''}</div></div>
        <div class="pop-px"><div class="p">${fmt$(s.price)}</div><div class="c ${cls(ch)}">${fmtPct(ch)}</div></div>
      </div>`;
    }).join('');
  } catch (e) { console.error('popular stocks failed', e); el.innerHTML = '<div class="loading">Unavailable.</div>'; }
}

let suggestionTimer = null;
let suggestionRequest = null;
let suggestionSequence = 0;
let suggestions = [];
let highlightedSuggestion = -1;

function clearSuggestions() {
  suggestionSequence++;
  clearTimeout(suggestionTimer);
  suggestionRequest?.abort();
  suggestionRequest = null;
  suggestions = [];
  highlightedSuggestion = -1;
  const list = $('instrument-suggestions');
  list.replaceChildren();
  list.hidden = true;
  $('stock-search-input').setAttribute('aria-expanded', 'false');
  $('stock-search-input').removeAttribute('aria-activedescendant');
}

function renderSuggestions(status) {
  const list = $('instrument-suggestions');
  list.replaceChildren();
  if (status) {
    const item = document.createElement('li');
    item.className = 'instrument-suggestion-status';
    item.textContent = status;
    list.append(item);
  } else {
    suggestions.forEach((suggestion, index) => {
      const item = document.createElement('li');
      const option = document.createElement('button');
      option.type = 'button';
      option.id = `instrument-suggestion-${index}`;
      option.className = `instrument-suggestion${index === highlightedSuggestion ? ' active' : ''}`;
      option.setAttribute('role', 'option');
      option.setAttribute('aria-selected', String(index === highlightedSuggestion));
      const ticker = document.createElement('span');
      ticker.className = 'instrument-suggestion-ticker';
      ticker.textContent = suggestion.ticker;
      const company = document.createElement('span');
      company.className = 'instrument-suggestion-company';
      company.textContent = ` · ${suggestion.company_name || suggestion.ticker}`;
      option.append(ticker, company);
      const metadata = [suggestion.instrument_type === 'etf' ? 'ETF' : 'Equity', suggestion.exchange || suggestion.category].filter(Boolean).join(' · ');
      if (metadata) {
        const meta = document.createElement('span');
        meta.className = 'instrument-suggestion-meta';
        meta.textContent = metadata;
        option.append(meta);
      }
      option.addEventListener('mousedown', event => event.preventDefault());
      option.addEventListener('click', () => selectSuggestion(index));
      item.append(option);
      list.append(item);
    });
  }
  list.hidden = false;
  $('stock-search-input').setAttribute('aria-expanded', 'true');
  if (highlightedSuggestion >= 0) $('stock-search-input').setAttribute('aria-activedescendant', `instrument-suggestion-${highlightedSuggestion}`);
  else $('stock-search-input').removeAttribute('aria-activedescendant');
}

function selectSuggestion(index) {
  const suggestion = suggestions[index];
  if (!suggestion) return;
  $('stock-search-input').value = '';
  clearSuggestions();
  openDrawerTicker(suggestion.ticker);
}

function searchStock() {
  if (highlightedSuggestion >= 0) return selectSuggestion(highlightedSuggestion);
  const input = $('stock-search-input');
  const ticker = (input.value || '').trim().toUpperCase();
  if (!ticker) return;
  input.value = '';
  clearSuggestions();
  openDrawerTicker(ticker);
}

function requestSuggestions() {
  const query = $('stock-search-input').value.trim();
  if (query.length < 2) return clearSuggestions();
  const requestId = ++suggestionSequence;
  suggestionRequest?.abort();
  suggestionRequest = new AbortController();
  renderSuggestions('Loading…');
  fetch(`/api/instrument-suggestions?${new URLSearchParams({query})}`, {signal: suggestionRequest.signal})
    .then(response => response.ok ? response.json() : Promise.reject(new Error(`Request failed (${response.status})`)))
    .then(data => {
      if (requestId !== suggestionSequence) return;
      suggestions = data.suggestions || [];
      highlightedSuggestion = -1;
      if (suggestions.length) renderSuggestions();
      else renderSuggestions('No matching active instruments.');
    })
    .catch(error => {
      if (error.name !== 'AbortError' && requestId === suggestionSequence) clearSuggestions();
    });
}

$('stock-search-input').addEventListener('input', () => {
  clearTimeout(suggestionTimer);
  suggestionTimer = setTimeout(requestSuggestions, 250);
});
$('stock-search-input').addEventListener('keydown', event => {
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    if (!suggestions.length) return;
    event.preventDefault();
    highlightedSuggestion = (highlightedSuggestion + (event.key === 'ArrowDown' ? 1 : suggestions.length - 1)) % suggestions.length;
    renderSuggestions();
  } else if (event.key === 'Enter') {
    event.preventDefault();
    searchStock();
  } else if (event.key === 'Escape') {
    clearSuggestions();
  }
});
document.addEventListener('click', event => {
  if (!event.target.closest('.stock-search')) clearSuggestions();
});

let stockChart = null;
let selectedStockRange = '1M';
let stockChartRequest = 0;

function renderStockChart(ohlcv) {
  const canvas = $('stockChart');
  if (!canvas || !ohlcv?.length) return;
  const prev = Chart.getChart('stockChart');
  if (prev) prev.destroy();
  const labels = ohlcv.map(o => new Date(o.date).toLocaleString([], {
    month: 'short', day: 'numeric', ...(selectedStockRange === '1D' ? { hour: 'numeric', minute: '2-digit' } : {})
  }));
  const closes = ohlcv.map(o => o.close);
  const up = closes[closes.length - 1] >= closes[0];
  stockChart = new Chart(canvas, {
    type: 'line',
    data: { labels, datasets: [{ data: closes, borderColor: up ? '#1a7f37' : '#cf222e', backgroundColor: 'transparent', tension: .25, pointRadius: 0, borderWidth: 2 }] },
    options: {
      plugins: { legend: { display: false }, tooltip: { callbacks: {
        title: items => formatChartTimestamp(ohlcv[items[0].dataIndex].date)
      } } },
      scales: {
        x: { ticks: { color: '#656d76', maxTicksLimit: 6 }, grid: { color: '#d0d7de' } },
        y: { ticks: { color: '#656d76', callback: v => '$' + Number(v).toFixed(0) }, grid: { color: '#d0d7de' } }
      }
    }
  });
}

async function selectStockRange(range) {
  const ticker = $('s-name').textContent;
  if (!ticker || ticker === 'Loading…') return;
  selectedStockRange = range;
  document.querySelectorAll('[data-stock-range]').forEach(button => button.classList.toggle('active', button.dataset.stockRange === range));
  $('stock-chart-title').textContent = `Price (${range})`;
  const request = ++stockChartRequest;
  try {
    const response = await fetch(`/api/stock/${encodeURIComponent(ticker)}?chart_range=${range}`);
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
    const data = await response.json();
    if (request !== stockChartRequest) return;
    const canvas = $('stockChart');
    if (!canvas) return;
    $('stock-chart-empty')?.remove();
    const existing = Chart.getChart('stockChart');
    if (existing) existing.destroy();
    if (data.ohlcv?.length) renderStockChart(data.ohlcv);
    else canvas.insertAdjacentHTML('afterend', '<div class="loading" id="stock-chart-empty">No price history for this range.</div>');
  } catch (error) {
    if (request === stockChartRequest) console.error('stock chart range failed:', error);
  }
}

async function openDrawerTicker(ticker) {
  const symbol = String(ticker ?? '').trim();
  if (!symbol) return;
  $('stock-overlay').classList.add('open');
  $('stock-drawer').classList.add('open');
  $('s-name').textContent = symbol;
  $('s-sub').textContent = 'Loading…';
  $('stock-body').innerHTML = '<div class="loading">Loading…</div>';
  try {
    const d = await (await fetch(`/api/stock/${encodeURIComponent(symbol)}`)).json();
    if (!d.price) {
      $('s-sub').textContent = 'No data';
      $('stock-body').innerHTML = `<div class="loading">No market data for "${ticker}". Check the ticker symbol.</div>`;
      return;
    }
    const ch = d.change_percent || 0;
    const company = (d.company && d.company !== d.ticker) ? d.company : '';
    const sector = (d.sector && d.sector !== 'Unknown') ? d.sector : '';
    const subBits = [company, d.instrument_type === 'etf' ? 'ETF' : '', d.category, d.issuer, sector].filter(Boolean).join(' · ');
    $('s-name').textContent = d.ticker;
    $('s-sub').innerHTML = `${subBits ? subBits + ' · ' : ''}<strong>${fmt$(d.price)}</strong> <span class="${cls(ch)}">${fmtPct(ch)}</span>`;

    const holders = (d.holders || []).map(h => `<tr>
      <td>${h.display_name || h.username}${badgeFor(h.user_type, h.decision_architecture)}</td>
      <td class="num">${fmtQty(h.quantity)}</td>
      <td class="num">${fmt$(h.avg_cost)}</td>
      <td class="num ${cls(h.pnl_percent)}">${fmtPct(h.pnl_percent)}</td>
    </tr>`).join('');

    const trades = (d.recent_trades || []).map(t => `<div class="detail-row">
      <span class="txn-type ${transactionClass(t.transaction_type)}">${t.transaction_type}</span>
      <strong>${t.username}</strong>
      <span class="detail-meta">${fmtQty(t.quantity)} @ ${fmt$(t.price_per_share)}</span>
      <span class="detail-date">${t.executed_at ? new Date(t.executed_at).toLocaleDateString() : ''}</span>
    </div>`).join('');

    const news = (d.news || []).map(n => `<div class="detail-block">
      <div class="detail-title">${n.title}</div>
      <div class="detail-meta detail-meta-spaced">${n.publisher || ''}${n.published_at ? ' · ' + new Date(n.published_at).toLocaleDateString() : ''}</div>
    </div>`).join('');

    selectedStockRange = d.chart_range || '1M';
    $('stock-body').innerHTML = `
      <div class="chart-header"><div class="section-title" id="stock-chart-title">Price (${selectedStockRange})</div><div class="chart-range" aria-label="Price chart range">${['1D', '1W', '1M', '3M', '6M', '1Y'].map(range => `<button type="button" data-stock-range="${range}" class="${range === selectedStockRange ? 'active' : ''}" data-action="select-stock-range" data-arg="${range}">${range}</button>`).join('')}</div></div>
      <canvas id="stockChart" height="200"></canvas>${(d.ohlcv && d.ohlcv.length) ? '' : '<div class="loading" id="stock-chart-empty">No price history for this range.</div>'}
      ${currentDetail?.user_type === 'human' ? `<button class="submit-btn" data-action="trade-instrument" data-arg="${d.ticker}">Trade this instrument</button>` : ''}
      <div class="section-title">Holders</div>
      ${holders ? `<table class="mini-table"><thead><tr><th>Player</th><th class="num">Qty</th><th class="num">Avg</th><th class="num">P&L</th></tr></thead><tbody>${holders}</tbody></table>` : '<div class="loading">No holders yet.</div>'}
      <div class="section-title">Recent trades</div>
      ${trades || '<div class="loading">No trades in this ticker.</div>'}
      <div class="section-title">News</div>
      ${news || '<div class="loading">No recent news.</div>'}`;

    if (d.ohlcv && d.ohlcv.length) {
      try { renderStockChart(d.ohlcv); } catch (error) { console.error('stock chart failed', error); }
    }
  } catch (e) {
    console.error('openDrawerTicker failed:', e);
    $('s-sub').textContent = '';
    $('stock-body').innerHTML = `<div class="loading">Failed to load: ${e.message}</div>`;
  }
}
function closeStockDrawer() {
  $('stock-overlay').classList.remove('open');
  $('stock-drawer').classList.remove('open');
}

function renderKPIs(data) {
  const best = data.reduce((a, b) => (b.pnl_percent > a.pnl_percent ? b : a), data[0]);
  const worst = data.reduce((a, b) => (b.pnl_percent < a.pnl_percent ? b : a), data[0]);
  $('kpis').innerHTML = `
    <div class="kpi"><div class="label">Players</div><div class="value">${data.length}</div></div>
    <div class="kpi"><div class="label">Best</div><div class="value small">${best.display_name || best.username} <span class="${cls(best.pnl_percent)}">${fmtPct(best.pnl_percent)}</span></div></div>
    <div class="kpi"><div class="label">Worst</div><div class="value small">${worst.display_name || worst.username} <span class="${cls(worst.pnl_percent)}">${fmtPct(worst.pnl_percent)}</span></div></div>
    <div class="kpi"><div class="label">Avg Return</div><div class="value ${cls(data.reduce((a,b)=>a+b.pnl_percent,0)/data.length)}">${fmtPct(data.reduce((a,b)=>a+b.pnl_percent,0)/data.length)}</div></div>`;
}

function decisionIndicatorFor(row) {
  return row.user_type === 'llm_agent' && decisionBatchStatus?.agents?.[row.username]?.status === 'running'
    ? '<span class="ai-decision-indicator" role="status" aria-label="AI is analyzing" title="AI is analyzing"></span>'
    : '';
}

function renderTable() {
  const rows = [...lbData].sort((a, b) => {
    let av, bv;
    if (sortKey === 'volatility' || sortKey === 'maxdd') {
      av = (riskCache[a.username] || {})[sortKey] ?? 0;
      bv = (riskCache[b.username] || {})[sortKey] ?? 0;
    } else { av = a[sortKey]; bv = b[sortKey]; }
    if (typeof av === 'string') return av.localeCompare(bv) * sortDir;
    return (av - bv) * sortDir;
  });
  $('lb-body').innerHTML = rows.map(r => {
    const risk = riskCache[r.username] || {};
    return `<tr data-action="open-drawer" data-arg="${r.username}">
      <td class="num rank r${r.rank}">${r.rank}</td>
      <td><div class="name-cell"><span class="avatar">${initials(r.display_name || r.username)}</span>${r.display_name || r.username}${decisionIndicatorFor(r)}${badgeFor(r.user_type, r.decision_architecture)}</div></td>
      <td class="num">${fmt$(r.total_value)}</td>
      <td class="num hide-mobile">${fmt$(r.holdings_value)}</td>
      <td class="num hide-mobile">${fmt$(r.cash_balance)}</td>
      <td class="num ${cls(r.pnl_percent)}">${fmtPct(r.pnl_percent)}</td>
      <td class="num hide-mobile">${risk.volatility != null ? risk.volatility.toFixed(2) + '%' : '…'}</td>
      <td class="num hide-mobile neg">${risk.maxdd != null ? '-' + risk.maxdd.toFixed(2) + '%' : '…'}</td>
      <td class="hide-mobile">${risk.pnl_history ? sparkline(risk.pnl_history) : ''}</td>
    </tr>`;
  }).join('');
}

document.querySelectorAll('#lb-table th[data-sort]').forEach(th => {
  th.addEventListener('click', () => {
    const k = th.dataset.sort;
    if (sortKey === k) sortDir *= -1; else { sortKey = k === 'username' ? 1 : (k === 'rank' ? 1 : -1); sortKey = k; }
    renderTable();
  });
});

// ---- Activity ----
async function loadActivity() {
  const data = await (await fetch('/api/transactions?limit=50')).json();
  $('act-body').innerHTML = data.length ? data.map(t => `
    <tr>
      <td class="hide-mobile" title="${t.execution_quote_source || 'legacy record'}; ${t.execution_market_state || 'unknown market state'}">${t.execution_quote_captured_at ? `Quote ${new Date(t.execution_quote_captured_at).toLocaleString()}` : `Recorded ${new Date(t.executed_at).toLocaleString()}`}</td>
      <td>${t.username}</td>
      <td class="${transactionClass(t.transaction_type)} txn-type">${t.transaction_type}</td>
      <td>${t.ticker}</td>
      <td class="num">${fmtQty(t.quantity)}</td>
      <td class="num">${fmt$(t.price_per_share)}</td>
      <td class="num">${fmt$(t.total_value)}</td>
    </tr>`).join('') : '<tr><td colspan="7" class="loading">No transactions yet.</td></tr>';
}

// ---- Views ----
function showView(v) {
  $('view-leaderboard').hidden = v !== 'leaderboard';
  $('view-activity').hidden = v !== 'activity';
  $('nav-lb').classList.toggle('active', v === 'leaderboard');
  $('nav-act').classList.toggle('active', v === 'activity');
  if (v === 'activity') loadActivity();
}

// ---- Drawer ----
let currentDetail = null, perfChart = null;
async function openDrawer(username) {
  $('overlay').classList.add('open');
  $('drawer').classList.add('open');
  $('d-name').textContent = username;
  $('d-sub').textContent = 'Loading…';
  $('tab-portfolio').innerHTML = '<div class="loading">Loading…</div>';
  showTab('portfolio');
  try {
    const cached = riskCache[username] && riskCache[username].detail;
    const d = cached || await (await fetch(`/api/agent-detail/${username}`)).json();
    if (!d || !d.portfolio) throw new Error('No portfolio data in response');
    currentDetail = d;
    const p = d.portfolio;
    $('d-name').innerHTML = `${d.display_name || username} ${badgeFor(d.user_type, d.decision_architecture)}`;
    $('d-sub').innerHTML = `${fmt$(p.total_value)} · <span class="${cls(p.pnl_percent)}">${fmtPct(p.pnl_percent)}</span>`;
    renderPortfolio(d);
    renderHistory(d);
    renderTrade(d);
    $('tab-btn-trade').hidden = d.user_type !== 'human';
  } catch (e) {
    console.error('openDrawer failed:', e);
    $('d-sub').textContent = '';
    $('tab-portfolio').innerHTML = `<div class="loading">Failed to load: ${e.message}</div>`;
  }
}
function closeDrawer() {
  $('overlay').classList.remove('open');
  $('drawer').classList.remove('open');
}
function showTab(t) {
  ['portfolio', 'history', 'performance', 'trade'].forEach(x => {
    $('tab-' + x).hidden = x !== t;
    const btn = document.querySelector(`.tabs button[data-tab="${x}"]`);
    if (btn) btn.classList.toggle('active', x === t);
  });
  if (t === 'performance' && currentDetail) renderPerformance(currentDetail);
}

function renderPortfolio(d) {
  const p = d.portfolio, s = d.stats;
  const formatBuyDate = value => {
    const date = new Date(value);
    return value && !Number.isNaN(date.valueOf()) ? date.toLocaleDateString() : '—';
  };
  const latestCommitteeSteps = d.decision_architecture === 'multi_model' ? (d.committee_steps || []).slice(0, 4) : [];
  const committeeEstimatedCost = latestCommitteeSteps.reduce((total, step) => total + Number(step.estimated_cost_usd || 0), 0);
  const committeeAudit = latestCommitteeSteps.length
    ? `<div class="section-title">Latest committee model steps · estimated pi cost $${committeeEstimatedCost.toFixed(4)}</div><div class="decision-msg">${latestCommitteeSteps.map(step => `${step.role}: ${step.model_name} — ${step.response_status}${step.estimated_cost_usd != null ? ` ($${Number(step.estimated_cost_usd).toFixed(4)})` : ''}`).join(' · ')}</div>` : '';
  const noTradeDecision = d.decision_architecture === 'multi_model' ? d.no_trade_decision : null;
  const holdings = (p.holdings || []).map(h => {
    const weight = p.total_value > 0 ? (h.market_value / p.total_value * 100) : 0;
    return `<tr>
      <td>${h.ticker}</td><td class="hide-mobile">${formatBuyDate(h.opened_at)}</td><td class="num">${fmtQty(h.quantity)}</td>
      <td class="num">${fmt$(h.average_cost)}</td><td class="num">${fmt$(h.current_price)}</td>
      <td class="num">${fmt$(h.market_value)}</td>
      <td class="num ${cls(h.pnl)}">${fmt$(h.pnl)} (${fmtPct(h.pnl_percent)})</td>
      <td class="num">${weight.toFixed(1)}%</td>
    </tr>`;
  }).join('');
  $('tab-portfolio').innerHTML = `
    ${strategyHtml(d)}
    ${d.user_type === 'llm_agent' ? `<div class="decision-bar"><span class="decision-msg" id="account-decision-status">${accountDecisionStatusText(d)}</span></div>` : ''}
    ${committeeAudit}
    ${noTradeDecision ? `<section class="committee-decision" aria-labelledby="committee-no-trade-title"><div class="committee-decision-header"><div><p class="committee-decision-eyebrow">Today’s committee decision</p><h3 class="committee-decision-title" id="committee-no-trade-title">No trade</h3></div><span class="committee-decision-status">HOLD</span></div><p class="committee-decision-outcome" id="committee-no-trade-outcome"></p><div class="committee-rationale"><p class="committee-rationale-label">Chair rationale</p><p class="committee-rationale-text" id="committee-no-trade-reason"></p></div></section>` : ''}
    <div class="stat-grid">
      <div class="stat"><div class="l">Cash</div><div class="v">${fmt$(p.cash_balance)}</div></div>
      <div class="stat"><div class="l">Dividends Earned</div><div class="v ${cls(s.dividend_income)}">${fmt$(s.dividend_income)}</div></div>
      <div class="stat"><div class="l">Holdings</div><div class="v">${fmt$(p.holdings_value)}</div></div>
      <div class="stat"><div class="l">Realized P&L</div><div class="v ${cls(p.realized_pnl)}">${fmt$(p.realized_pnl)}</div></div>
      <div class="stat"><div class="l">Win Rate</div><div class="v">${s.win_rate}%</div></div>
      <div class="stat"><div class="l">Total Trades</div><div class="v">${s.total_trades}</div></div>
      <div class="stat"><div class="l">Largest Trade</div><div class="v">${fmt$(s.largest_trade)}</div></div>
    </div>
    <div class="section-title">Holdings (${p.holdings_count})</div>
    ${holdings ? `<div class="table-scroll"><table class="mini-table"><thead><tr><th>Ticker</th><th class="hide-mobile">Buy date</th><th class="num">Qty</th><th class="num">Avg</th><th class="num">Price</th><th class="num">Value</th><th class="num">P&L</th><th class="num">Wt</th></tr></thead><tbody>${holdings}</tbody></table></div>` : '<div class="loading">No open positions.</div>'}
    <div class="section-title">Sector Allocation</div>
    <div class="sector-chart"><canvas id="sectorChart"></canvas></div>`;
  if (noTradeDecision) {
    const reason = noTradeDecision.reasoning || 'The committee did not provide a rationale.';
    const rejection = noTradeDecision.rejection;
    const rejectionMessage = typeof rejection === 'object' && rejection !== null
      ? rejection.message || rejection.code || JSON.stringify(rejection)
      : rejection;
    $('committee-no-trade-outcome').textContent = noTradeDecision.execution_status === 'rejected'
      ? `A proposed ${noTradeDecision.decision || 'trade'} was blocked by an execution guardrail: ${rejectionMessage || 'No reason recorded.'}`
      : 'The chair chose to hold rather than place a trade.';
    $('committee-no-trade-reason').textContent = reason;
  }
  renderSectorChart(d.sectors);
}

let sectorChart = null;
function renderSectorChart(sectors) {
  const labels = Object.keys(sectors || {}), vals = Object.values(sectors || {});
  const el = $('sectorChart');
  if (!el) return;
  if (!labels.length) { el.parentElement.innerHTML = '<div class="loading">No sector data.</div>'; return; }
  try {
    const prev = Chart.getChart('sectorChart');
    if (prev) prev.destroy();
    sectorChart = new Chart(el, {
    type: 'doughnut',
    data: { labels, datasets: [{ data: vals, backgroundColor: ['#0969da','#1a7f37','#9a6700','#cf222e','#8250df','#0550ae','#116329','#bc4c00','#a40e26'] }] },
    options: { plugins: { legend: { position: 'right', labels: { color: '#1f2328', boxWidth: 12, font: { size: 11 } } } } }
  });
  } catch (e) { console.error('sector chart failed', e); el.parentElement.innerHTML = '<div class="loading">Chart unavailable.</div>'; }
}

let histFilter = 'ALL';
function renderHistory(d) {
  const trades = (d.trades || []).filter(t => histFilter === 'ALL' || t.action === histFilter);
  $('tab-history').innerHTML = `
    <div class="filter-row">
      ${['ALL', 'BUY', 'SELL'].map(f => `<button class="${histFilter === f ? 'active' : ''}" data-action="set-history-filter" data-arg="${f}">${f}</button>`).join('')}
    </div>
    ${trades.length ? trades.map(t => `
      <div class="history-item">
        <div class="history-summary">
          <span class="txn-type ${transactionClass(t.action)}">${t.action}</span>
          <strong>${t.ticker}</strong>
          <span class="detail-meta">${fmtQty(t.quantity)} @ ${fmt$(t.price)}</span>
          <span class="history-total">${fmt$(t.total)}</span>
        </div>
        <div class="detail-meta history-time">${t.time ? new Date(t.time).toLocaleString() : ''}</div>
        ${t.reasoning ? `<div class="reason">${t.reasoning}</div>` : ''}
      </div>`).join('') : '<div class="loading">No trades.</div>'}`;
}
function setHistFilter(f) { histFilter = f; renderHistory(currentDetail); }

function renderPerformance(d) {
  const hist = d.pnl_history || [];
  $('tab-performance').innerHTML = hist.length
    ? '<div class="section-title">Return over time (%)</div><canvas id="perfChart" height="220"></canvas>'
    : '<div class="loading">No performance history yet.</div>';
  if (!hist.length) return;
  const prev = Chart.getChart('perfChart');
  if (prev) prev.destroy();
  const labels = hist.map(h => new Date(h.time).toLocaleDateString());
  const vals = hist.map(h => h.pnl_pct);
  const up = vals[vals.length - 1] >= 0;
  try {
  perfChart = new Chart($('perfChart'), {
    type: 'line',
    data: { labels, datasets: [{ data: vals, borderColor: up ? '#1a7f37' : '#cf222e', backgroundColor: 'transparent', tension: .25, pointRadius: 0, borderWidth: 2 }] },
    options: {
      plugins: { legend: { display: false }, tooltip: { callbacks: {
        title: items => formatChartTimestamp(hist[items[0].dataIndex].time)
      } } },
      scales: {
        x: { ticks: { color: '#656d76', maxTicksLimit: 6 }, grid: { color: '#d0d7de' } },
        y: { ticks: { color: '#656d76', callback: v => v + '%' }, grid: { color: '#d0d7de' } }
      }
    }
  });
  } catch (e) { console.error('perf chart failed', e); $('tab-performance').innerHTML = '<div class="loading">Chart unavailable.</div>'; }
}

let tradeAction = 'BUY', pendingTrade = null, tradeReturnFocus = null, tradeAbort = null;
function renderTrade(d) {
  const holdings = d.portfolio.holdings || [];
  const holdOpts = holdings.map(h => `<option value="${h.ticker}">${h.ticker} (${fmtQty(h.quantity)} @ ${fmt$(h.current_price)})</option>`).join('');
  $('tab-trade').innerHTML = `
    <div class="trade-form">
      <div class="seg"><button id="seg-buy" class="buy-on" data-action="set-trade-action" data-arg="BUY">Buy</button><button id="seg-sell" data-action="set-trade-action" data-arg="SELL">Sell</button></div>
      <div><label>Ticker</label><input id="trade-ticker" placeholder="e.g. AAPL" list="hold-list" autocomplete="off" /><datalist id="hold-list">${holdOpts}</datalist></div>
      <div><label>Amount (USD)</label><input id="trade-amount" type="number" min="0.01" step="0.01" placeholder="500" /></div>
      <div id="trade-context" class="decision-msg">Review an instrument before placing an order.</div>
      <button class="submit-btn" id="trade-submit" data-action="review-trade" data-arg="${d.username}">Review order</button>
      <div id="trade-msg"></div><div class="detail-meta">Estimated values are non-binding. The execution engine enforces all guardrails on a fresh quote.</div>
    </div>`;
  setTradeAction(tradeAction);
}
function setTradeAction(a) { tradeAction = a; const buy = $('seg-buy'), sell = $('seg-sell'); if (buy) buy.className = a === 'BUY' ? 'buy-on' : ''; if (sell) sell.className = a === 'SELL' ? 'sell-on' : ''; }
function tradeInstrument(ticker) { if (!currentDetail || currentDetail.user_type !== 'human') return; closeStockDrawer(); showTab('trade'); $('trade-ticker').value = ticker; $('trade-ticker').focus(); }
function tradeMessage(text, error = false) { const msg = $('trade-msg'); if (!msg) return; msg.className = `trade-msg ${error ? 'err' : 'ok'}`; msg.textContent = text; }
async function readJson(response) {
  const text = await response.text();
  try { return JSON.parse(text); } catch { throw new Error(`Server error (HTTP ${response.status}). Please try again.`); }
}
async function reviewTrade(username) {
  const ticker = ($('trade-ticker').value || '').trim().toUpperCase(), amount = Number($('trade-amount').value), btn = $('trade-submit');
  if (!ticker || !Number.isFinite(amount) || amount <= 0) return tradeMessage('Enter a ticker and a positive USD amount.', true);
  btn.disabled = true; $('trade-context').textContent = 'Fetching current instrument and portfolio estimate…';
  try {
    const response = await fetch('/api/trade/preview', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({username, ticker, action: tradeAction, amount_dollars: amount})});
    const preview = await readJson(response); if (!response.ok) throw new Error(preview.error || 'Unable to review this order.');
    pendingTrade = {username, ticker, amount, action: tradeAction, clientOrderId: crypto.randomUUID(), preview};
    const p = preview, warnings = (p.warnings || []).map(w => `<li>${w.message}</li>`).join('');
    $('trade-context').textContent = `${p.instrument.company} · ${fmt$(p.quote.price)} · estimated ${fmtQty(p.estimated_quantity)} shares`;
    $('trade-confirm-title').textContent = `Review simulated ${tradeAction.toLowerCase()}`;
    $('trade-confirm-body').innerHTML = `<strong>${p.action} ${p.instrument.ticker} (${p.instrument.company})</strong><div>Requested: ${fmt$(p.requested_amount)} · Estimated fill: ${fmt$(p.estimated_executable_amount)}</div><div>Estimated ${fmtQty(p.estimated_quantity)} shares @ ${fmt$(p.quote.price)}</div><div>Fee: ${fmt$(p.fee)} · Cash after: ${fmt$(p.estimated_cash_after)}</div><div>Holding after: ${fmtQty(p.estimated_holding_quantity)} shares (${(p.estimated_holding_weight * 100).toFixed(1)}%)</div>${warnings ? `<ul>${warnings}</ul>` : ''}`;
    $('trade-confirm-submit').textContent = `Confirm simulated ${tradeAction.toLowerCase()}`; tradeReturnFocus = btn;
    $('trade-confirm-overlay').classList.add('open'); $('trade-confirm-modal').classList.add('open'); $('trade-confirm-submit').focus();
  } catch (error) { $('trade-context').textContent = ''; tradeMessage(error.message, true); } finally { btn.disabled = false; }
}
function closeTradeConfirmation() { tradeAbort?.abort(); $('trade-confirm-overlay').classList.remove('open'); $('trade-confirm-modal').classList.remove('open'); pendingTrade = null; tradeReturnFocus?.focus(); }
async function confirmTrade() {
  if (!pendingTrade || tradeAbort) return; const order = pendingTrade, btn = $('trade-confirm-submit'); btn.disabled = true;
  tradeAbort = new AbortController();
  try {
    const response = await fetch('/api/trade', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({username: order.username, ticker: order.ticker, action: order.action, amount_dollars: order.amount, client_order_id: order.clientOrderId}), signal: tradeAbort.signal});
    const result = await readJson(response);
    if (!response.ok || !result.ok) {
      closeTradeConfirmation(); tradeMessage(`${result.error || 'Trade rejected.'} Correct the order and review again.`, true); return;
    }
    closeTradeConfirmation(); const t = result.transaction; tradeMessage(`${t.action} filled: ${fmtQty(t.quantity)} ${t.ticker} @ ${fmt$(t.price)} = ${fmt$(t.total)}; fee ${fmt$(t.fee)}.`, false); $('trade-amount').value = '';
    delete riskCache[order.username]; const fresh = await (await fetch(`/api/agent-detail/${order.username}`)).json(); currentDetail = fresh; renderPortfolio(fresh); renderHistory(fresh); renderTrade(fresh); $('d-sub').innerHTML = `${fmt$(fresh.portfolio.total_value)} · <span class="${cls(fresh.portfolio.pnl_percent)}">${fmtPct(fresh.portfolio.pnl_percent)}</span>`; loadLeaderboard();
  } catch (error) {
    if (error.name !== 'AbortError') {
      btn.textContent = `Retry simulated ${order.action.toLowerCase()}`;
      $('trade-confirm-body').insertAdjacentText('beforeend', `\nConnection failed: ${error.message}. Retry this confirmation; it uses the same order ID.`);
    }
  } finally { btn.disabled = false; tradeAbort = null; }
}
document.addEventListener('keydown', event => { if (event.key === 'Escape' && $('trade-confirm-modal').classList.contains('open')) closeTradeConfirmation(); });
function strategyHtml(d) {
  const s = d.strategy;
  if (!s || (!s.label && !s.summary)) return '';
  const c = s.config || {};
  const percent = value => `${Number(value).toLocaleString(undefined, {maximumFractionDigits: 2})}%`;
  const allocationPercent = value => percent(Number(value) * 100);
  const item = (condition, text) => condition ? `<li>${text}</li>` : '';
  const sellCriteria = [
    item(c.sell_gain_pct != null, `Sell a holding after it gains more than ${percent(c.sell_gain_pct)}.`),
    item(c.sell_loss_pct != null, `Sell a holding after it loses more than ${percent(Math.abs(c.sell_loss_pct))}.`),
  ].join('');
  const buyCriteria = [
    item(c.prefer_dips === true, 'When choosing a new investment, favour quality companies whose price has recently fallen.'),
    item(c.prefer_dips === false && c.min_move_pct != null, `Look for a clear price move of at least ${percent(c.min_move_pct)} before buying.`),
    item(c.max_volatility_pct != null, `Avoid buying stocks that have moved more than ${percent(c.max_volatility_pct)} over the last five days.`),
  ].join('');
  const portfolioCriteria = [
    item(c.cash_reserve_pct != null, `Keep at least ${percent(c.cash_reserve_pct)} of the portfolio in cash.`),
    item(c.max_positions != null, `Hold no more than ${c.max_positions} different investments.`),
    item(c.max_allocation != null, `Put no more than ${allocationPercent(c.max_allocation)} of the portfolio into one investment.`),
  ].join('');
  const group = (title, criteria) => criteria ? `<div class="st-group"><div class="st-group-title">${title}</div><ul>${criteria}</ul></div>` : '';
  const ensemble = d.decision_architecture === 'multi_model';
  const roster = ensemble ? [...(d.model_roster?.advisers || []).map(member => `${member.role}: ${member.model}`), `chair: ${d.model_roster?.judge?.model || 'unavailable'}`] : [];
  return `<div class="agent-strategy">
    <div class="st-label">📊 ${s.label || 'Strategy'}</div>
    ${s.summary ? `<div class="st-sum">${s.summary}</div>` : ''}
    <div class="st-intro">${ensemble ? 'Independent GitHub Copilot models propose decisions and a separate chair model makes the final choice.' : 'How this AI makes decisions.'} These are its guidelines; simulator safety rules can still limit a trade.</div>
    ${ensemble ? group('AI Ensemble model roster', roster.map(model => `<li>${model}</li>`).join('')) : ''}
    <div class="st-criteria">
      ${group('When it sells', sellCriteria)}
      ${group('When it buys', buyCriteria)}
      ${group('How it manages the portfolio', portfolioCriteria)}
    </div>
  </div>`;
}

const STYLE_PRESETS = {
  aggressive: { gain: 10, loss: -5, maxpos: 6, maxalloc: 25, minmove: 2, maxvol: 12, cash: 2, dips: 'false',
    persona: 'Aggressive momentum trader — chases volatility, news and FOMO plays with large positions.' },
  balanced:   { gain: 12, loss: -8, maxpos: 7, maxalloc: 15, minmove: 1.5, maxvol: 8, cash: 5, dips: 'false',
    persona: 'Balanced investor — moderate risk, mixes momentum and value.' },
  value:      { gain: 10, loss: -8, maxpos: 7, maxalloc: 10, minmove: 1, maxvol: 8, cash: 8, dips: 'true',
    persona: 'Conservative value investor — buys quality blue-chips on dips, avoids volatility.' },
};
function applyStylePreset() {
  const p = STYLE_PRESETS[$('ag-style').value];
  $('ag-gain').value = p.gain; $('ag-loss').value = p.loss; $('ag-maxpos').value = p.maxpos;
  $('ag-maxalloc').value = p.maxalloc; $('ag-minmove').value = p.minmove; $('ag-maxvol').value = p.maxvol;
  $('ag-cash').value = p.cash; $('ag-dips').value = p.dips; $('ag-persona').value = p.persona;
}
function openAgentModal() { $('agent-overlay').classList.add('open'); $('agent-modal').classList.add('open'); $('ag-msg').textContent=''; applyStylePreset(); }
function closeAgentModal() { $('agent-overlay').classList.remove('open'); $('agent-modal').classList.remove('open'); }
function openInstrumentModal() { $('instrument-overlay').classList.add('open'); $('instrument-modal').classList.add('open'); $('ins-msg').textContent = ''; }
function closeInstrumentModal() { $('instrument-overlay').classList.remove('open'); $('instrument-modal').classList.remove('open'); }
async function submitInstrument() {
  const ticker = $('ins-ticker').value.trim();
  const msg = $('ins-msg');
  if (!ticker) { msg.textContent = 'Ticker is required.'; return; }
  $('ins-submit').disabled = true; msg.textContent = 'Validating…';
  try {
    const response = await fetch('/api/instruments', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ticker, instrument_type: $('ins-type').value, category: $('ins-category').value || null})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Unable to add instrument.');
    msg.textContent = `${result.instrument.ticker} is active and eligible for the next AI cycle.`;
    $('ins-ticker').value = ''; $('ins-category').value = ''; loadPopular();
  } catch (error) { msg.textContent = error.message; } finally { $('ins-submit').disabled = false; }
}
async function importEtfs() {
  const msg = $('ins-msg');
  if (!confirm('Import or refresh the curated ETF catalogue? Existing operator metadata is preserved.')) return;
  msg.textContent = 'Importing…';
  try {
    const response = await fetch('/api/instruments/import-etfs', {method: 'POST'});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Import failed.');
    msg.textContent = `${result.imported} of ${result.count} catalogue ETFs imported.`; loadPopular();
  } catch (error) { msg.textContent = error.message; }
}
async function submitAgent() {
  const btn = $('ag-submit'), msg = $('ag-msg');
  const username = $('ag-username').value.trim();
  if (!username) { msg.textContent = 'Username required.'; return; }
  const body = {
    username, style: $('ag-style').value, persona: $('ag-persona').value.trim(),
    summary: $('ag-persona').value.trim(),
    config: {
      sell_gain_pct: +$('ag-gain').value, sell_loss_pct: +$('ag-loss').value,
      max_positions: +$('ag-maxpos').value, max_allocation: (+$('ag-maxalloc').value) / 100,
      min_move_pct: +$('ag-minmove').value, max_volatility_pct: +$('ag-maxvol').value,
      cash_reserve_pct: +$('ag-cash').value, prefer_dips: $('ag-dips').value === 'true',
    },
  };
  btn.disabled = true; msg.textContent = 'Creating…';
  try {
    const res = await fetch('/api/agents', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const data = await res.json();
    if (!res.ok || !data.ok) { msg.textContent = 'Failed: ' + (data.error || 'unknown error'); return; }
    msg.textContent = `Created ${data.agent.username}. Include it in the next manual decision batch.`;
    loadLeaderboard();
    setTimeout(closeAgentModal, 1200);
  } catch (e) { msg.textContent = 'Failed: ' + e.message; }
  finally { btn.disabled = false; }
}

// ---- WebSocket auto-refresh ----
function isExecutedTradeUpdate(message) {
  return message.type === 'GATEKEEPER_ALERT' && message.status === 'EXECUTED';
}

function handleWebSocketMessage(message) {
  if (message.type === 'DECISION_BATCH_UPDATED') renderDecisionBatchStatus(message.data);

  const affectsLeaderboard = message.type === 'LEADERBOARD_UPDATE';
  const affectsActivity = message.type === 'TRANSACTION_UPDATE'
    || message.type === 'PORTFOLIO_RESET'
    || isExecutedTradeUpdate(message);

  if (affectsActivity && !$('view-activity').hidden) loadActivity();
  if (affectsLeaderboard && !$('view-leaderboard').hidden) refreshLeaderboard();
}

function connectWS() {
  try {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onmessage = event => {
      try { handleWebSocketMessage(JSON.parse(event.data)); } catch (_) {}
    };
    ws.onclose = () => setTimeout(connectWS, 5000);
  } catch {}
}

async function checkFunnelAfterResume() {
  try {
    const response = await fetch('/api/cycle/check', {method: 'POST'});
    renderFunnelStatus((await response.json()).scheduler);
  } catch (_) {}
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') checkFunnelAfterResume();
});
window.addEventListener('focus', checkFunnelAfterResume);

const clickActions = {
  'show-view': showView,
  'open-agent-modal': openAgentModal,
  'open-instrument-modal': openInstrumentModal,
  'trigger-decision-batch': triggerDecisionBatch,
  'trigger-manual-refresh': triggerManualRefresh,
  'reset-lb-chart-zoom': resetLbChartZoom,
  'search-stock': searchStock,
  'set-instrument-filter': setInstrumentFilter,
  'close-drawer': closeDrawer,
  'show-tab': showTab,
  'close-agent-modal': closeAgentModal,
  'submit-agent': submitAgent,
  'close-instrument-modal': closeInstrumentModal,
  'submit-instrument': submitInstrument,
  'import-etfs': importEtfs,
  'close-trade-confirmation': closeTradeConfirmation,
  'confirm-trade': confirmTrade,
  'close-stock-drawer': closeStockDrawer,
  'open-drawer-ticker': openDrawerTicker,
  'select-stock-range': selectStockRange,
  'trade-instrument': tradeInstrument,
  'open-drawer': openDrawer,
  'set-history-filter': setHistFilter,
  'set-trade-action': setTradeAction,
  'review-trade': reviewTrade,
};

document.addEventListener('click', event => {
  const target = event.target.closest('[data-action]');
  const action = target && clickActions[target.dataset.action];
  if (action) action(target.dataset.arg);
});

document.addEventListener('change', event => {
  if (event.target.matches('[data-change-action="apply-style-preset"]')) applyStylePreset();
});

loadLeaderboard({ includeSupplementary: true });
loadDecisionBatchStatus();
loadFunnelStatus();
connectWS();
