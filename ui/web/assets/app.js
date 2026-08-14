import { ApiRequestError, requestJson } from './modules/api-client.js';
import { createDecisionStatus } from './modules/decision-status.js';
import { createInstruments } from './modules/instruments.js';
import {
  $,
  badgeFor,
  cls,
  escapeHtml,
  fmt$,
  fmtPct,
  fmtQty,
  formatChartTimestamp,
  initials,
  registerChartZoom,
  renderHtml,
  transactionClass,
} from './modules/presentation.js';
import { startRealtime } from './modules/realtime.js';
import { createTradeOrder } from './modules/trade-order.js';

registerChartZoom();

let lbData = [];
let sortKey = 'rank', sortDir = 1;
const riskCache = {}; // username -> {volatility, maxdd, pnl_history}
let decisionBatchStatus = null;

function decisionDate(value, timezone) {
  return value ? new Intl.DateTimeFormat(undefined, {dateStyle: 'medium', timeStyle: 'short', timeZone: timezone}).format(new Date(value)) : 'Never';
}
function accountDecisionStatusText(detail) {
  const agent = decisionBatchStatus?.agents?.[detail.username];
  const status = ({
    queued: 'Queued', running: 'Running', completed: 'Completed', completed_with_errors: 'Completed with errors', failed: 'Failed', interrupted: 'Interrupted',
  })[agent?.status] || 'Ready to run';
  return `AI decision status: ${status}${agent?.completed_at ? ` · Last completed: ${new Date(agent.completed_at).toLocaleString()}` : ''}`;
}
function updateAccountDecisionStatus() {
  const status = $('account-decision-status');
  if (status && currentDetail?.user_type === 'llm_agent') status.textContent = accountDecisionStatusText(currentDetail);
}
const decisionStatus = createDecisionStatus({
  requestJson,
  requestErrorType: ApiRequestError,
  onStatusChange: (status) => {
    decisionBatchStatus = status;
    if (!$('view-leaderboard').hidden) renderTable();
    updateAccountDecisionStatus();
  },
});

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
  try { renderFunnelStatus(await requestJson('/api/cycle/status')); } catch (_) {}
}
async function triggerManualRefresh() {
  const btn = $('funnel-refresh-btn');
  btn.disabled = true;
  try {
    await requestJson('/api/cycle', {method: 'POST'});
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
    const d = await requestJson(`/api/agent-detail/${username}`);
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
  const data = await requestJson('/api/leaderboard');
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
        const detail = await requestJson(`/api/agent-detail/${currentDetail.username}`);
        if (!detail.error && currentDetail?.username === detail.username) {
          currentDetail = detail;
          renderPortfolio(detail); renderHistory(detail);
          renderHtml($('d-sub'), `${fmt$(detail.portfolio.total_value)} · <span class="${cls(detail.portfolio.pnl_percent)}">${fmtPct(detail.portfolio.pnl_percent)}</span>`);
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
    const { history, users } = await requestJson('/api/portfolio-history');
    if (request !== lbChartRequest) return;
    const rankingsByUserId = new Map(lbData.map(ranking => [String(ranking.user_id), ranking]));
    const uids = [...new Set([...Object.keys(history), ...rankingsByUserId.keys()])];
    if (!uids.length) return;
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

const instruments = createInstruments({
  requestJson,
  element: $,
  renderHtml,
  escapeHtml,
  fmt$,
  fmtPct,
  fmtQty,
  cls,
  badgeFor,
  transactionClass,
  formatChartTimestamp,
  getCurrentDetail: () => currentDetail,
  resolveInstrument: (ticker) => runtimeActions.openDrawerTicker(ticker),
});
const { setInstrumentFilter, loadPopular, searchStock, selectStockRange, openDrawerTicker, closeStockDrawer } = instruments;

function renderKPIs(data) {
  const best = data.reduce((a, b) => (b.pnl_percent > a.pnl_percent ? b : a), data[0]);
  const worst = data.reduce((a, b) => (b.pnl_percent < a.pnl_percent ? b : a), data[0]);
  renderHtml($('kpis'), `
    <div class="kpi"><div class="label">Players</div><div class="value">${data.length}</div></div>
    <div class="kpi"><div class="label">Best</div><div class="value small">${escapeHtml(best.display_name || best.username)} <span class="${cls(best.pnl_percent)}">${fmtPct(best.pnl_percent)}</span></div></div>
    <div class="kpi"><div class="label">Worst</div><div class="value small">${escapeHtml(worst.display_name || worst.username)} <span class="${cls(worst.pnl_percent)}">${fmtPct(worst.pnl_percent)}</span></div></div>
    <div class="kpi"><div class="label">Avg Return</div><div class="value ${cls(data.reduce((a,b)=>a+b.pnl_percent,0)/data.length)}">${fmtPct(data.reduce((a,b)=>a+b.pnl_percent,0)/data.length)}</div></div>`);
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
  renderHtml($('lb-body'), rows.map(r => {
    const risk = riskCache[r.username] || {};
    const username = escapeHtml(r.username);
    const displayName = escapeHtml(r.display_name || r.username);
    return `<tr data-action="open-drawer" data-arg="${username}">
      <td class="num rank r${Number(r.rank)}">${Number(r.rank)}</td>
      <td><div class="name-cell"><span class="avatar">${escapeHtml(initials(r.display_name || r.username))}</span>${displayName}${decisionIndicatorFor(r)}${badgeFor(r.user_type, r.decision_architecture)}</div></td>
      <td class="num">${fmt$(r.total_value)}</td>
      <td class="num hide-mobile">${fmt$(r.holdings_value)}</td>
      <td class="num hide-mobile">${fmt$(r.cash_balance)}</td>
      <td class="num ${cls(r.pnl_percent)}">${fmtPct(r.pnl_percent)}</td>
      <td class="num hide-mobile">${risk.volatility != null ? risk.volatility.toFixed(2) + '%' : '…'}</td>
      <td class="num hide-mobile neg">${risk.maxdd != null ? '-' + risk.maxdd.toFixed(2) + '%' : '…'}</td>
      <td class="hide-mobile">${risk.pnl_history ? sparkline(risk.pnl_history) : ''}</td>
    </tr>`;
  }).join(''));
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
  const data = await requestJson('/api/transactions?limit=50');
  renderHtml($('act-body'), data.length ? data.map(t => `
    <tr>
      <td class="hide-mobile" title="${escapeHtml(t.execution_quote_source || 'legacy record')}; ${escapeHtml(t.execution_market_state || 'unknown market state')}">${t.execution_quote_captured_at ? `Quote ${new Date(t.execution_quote_captured_at).toLocaleString()}` : `Recorded ${new Date(t.executed_at).toLocaleString()}`}</td>
      <td>${escapeHtml(t.username)}</td>
      <td class="${transactionClass(t.transaction_type)} txn-type">${escapeHtml(t.transaction_type)}</td>
      <td>${escapeHtml(t.ticker)}</td>
      <td class="num">${fmtQty(t.quantity)}</td>
      <td class="num">${fmt$(t.price_per_share)}</td>
      <td class="num">${fmt$(t.total_value)}</td>
    </tr>`).join('') : '<tr><td colspan="7" class="loading">No transactions yet.</td></tr>');
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
  renderHtml($('tab-portfolio'), '<div class="loading">Loading…</div>');
  showTab('portfolio');
  try {
    const cached = riskCache[username] && riskCache[username].detail;
    const d = cached || await requestJson(`/api/agent-detail/${username}`);
    if (!d || !d.portfolio) throw new Error('No portfolio data in response');
    currentDetail = d;
    const p = d.portfolio;
    renderHtml($('d-name'), `${escapeHtml(d.display_name || username)} ${badgeFor(d.user_type, d.decision_architecture)}`);
    renderHtml($('d-sub'), `${fmt$(p.total_value)} · <span class="${cls(p.pnl_percent)}">${fmtPct(p.pnl_percent)}</span>`);
    renderPortfolio(d);
    renderHistory(d);
    renderTrade(d);
    $('tab-btn-trade').hidden = d.user_type !== 'human';
  } catch (e) {
    console.error('openDrawer failed:', e);
    $('d-sub').textContent = '';
    renderHtml($('tab-portfolio'), `<div class="loading">Failed to load: ${escapeHtml(e.message)}</div>`);
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
    ? `<div class="section-title">Latest committee model steps · estimated pi cost $${committeeEstimatedCost.toFixed(4)}</div><div class="decision-msg">${latestCommitteeSteps.map(step => `${escapeHtml(step.role)}: ${escapeHtml(step.model_name)} — ${escapeHtml(step.response_status)}${step.estimated_cost_usd != null ? ` ($${Number(step.estimated_cost_usd).toFixed(4)})` : ''}`).join(' · ')}</div>` : '';
  const noTradeDecision = d.decision_architecture === 'multi_model' ? d.no_trade_decision : null;
  const holdings = (p.holdings || []).map(h => {
    const weight = p.total_value > 0 ? (h.market_value / p.total_value * 100) : 0;
    return `<tr>
      <td>${escapeHtml(h.ticker)}</td><td class="hide-mobile">${formatBuyDate(h.opened_at)}</td><td class="num">${fmtQty(h.quantity)}</td>
      <td class="num">${fmt$(h.average_cost)}</td><td class="num">${fmt$(h.current_price)}</td>
      <td class="num">${fmt$(h.market_value)}</td>
      <td class="num ${cls(h.pnl)}">${fmt$(h.pnl)} (${fmtPct(h.pnl_percent)})</td>
      <td class="num">${weight.toFixed(1)}%</td>
    </tr>`;
  }).join('');
  renderHtml($('tab-portfolio'), `
    ${strategyHtml(d)}
    ${d.user_type === 'llm_agent' ? `<div class="decision-bar"><span class="decision-msg" id="account-decision-status">${accountDecisionStatusText(d)}</span></div>` : ''}
    ${committeeAudit}
    ${noTradeDecision ? `<section class="committee-decision" aria-labelledby="committee-no-trade-title"><div class="committee-decision-header"><div><p class="committee-decision-eyebrow">Today’s committee decision</p><h3 class="committee-decision-title" id="committee-no-trade-title">No trade</h3></div><span class="committee-decision-status">HOLD</span></div><p class="committee-decision-outcome" id="committee-no-trade-outcome"></p><div class="committee-rationale"><p class="committee-rationale-label">Chair rationale</p><p class="committee-rationale-text" id="committee-no-trade-reason"></p></div></section>` : ''}
    <div class="stat-grid">
      <div class="stat"><div class="l">Cash</div><div class="v">${fmt$(p.cash_balance)}</div></div>
      <div class="stat"><div class="l">Dividends Earned</div><div class="v ${cls(s.dividend_income)}">${fmt$(s.dividend_income)}</div></div>
      <div class="stat"><div class="l">Holdings</div><div class="v">${fmt$(p.holdings_value)}</div></div>
      <div class="stat"><div class="l">Realized P&L</div><div class="v ${cls(p.realized_pnl)}">${fmt$(p.realized_pnl)}</div></div>
      <div class="stat"><div class="l">Win Rate</div><div class="v">${Number(s.win_rate)}%</div></div>
      <div class="stat"><div class="l">Total Trades</div><div class="v">${Number(s.total_trades)}</div></div>
      <div class="stat"><div class="l">Largest Trade</div><div class="v">${fmt$(s.largest_trade)}</div></div>
    </div>
    <div class="section-title">Holdings (${Number(p.holdings_count)})</div>
    ${holdings ? `<div class="table-scroll"><table class="mini-table"><thead><tr><th>Ticker</th><th class="hide-mobile">Buy date</th><th class="num">Qty</th><th class="num">Avg</th><th class="num">Price</th><th class="num">Value</th><th class="num">P&L</th><th class="num">Wt</th></tr></thead><tbody>${holdings}</tbody></table></div>` : '<div class="loading">No open positions.</div>'}
    <div class="section-title">Sector Allocation</div>
    <div class="sector-chart"><canvas id="sectorChart"></canvas></div>`);
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
  if (!labels.length) { renderHtml(el.parentElement, '<div class="loading">No sector data.</div>'); return; }
  try {
    const prev = Chart.getChart('sectorChart');
    if (prev) prev.destroy();
    sectorChart = new Chart(el, {
    type: 'doughnut',
    data: { labels, datasets: [{ data: vals, backgroundColor: ['#0969da','#1a7f37','#9a6700','#cf222e','#8250df','#0550ae','#116329','#bc4c00','#a40e26'] }] },
    options: { plugins: { legend: { position: 'right', labels: { color: '#1f2328', boxWidth: 12, font: { size: 11 } } } } }
  });
  } catch (e) { console.error('sector chart failed', e); renderHtml(el.parentElement, '<div class="loading">Chart unavailable.</div>'); }
}

let histFilter = 'ALL';
function renderHistory(d) {
  const trades = (d.trades || []).filter(t => histFilter === 'ALL' || t.action === histFilter);
  renderHtml($('tab-history'), `
    <div class="filter-row">
      ${['ALL', 'BUY', 'SELL'].map(f => `<button class="${histFilter === f ? 'active' : ''}" data-action="set-history-filter" data-arg="${f}">${f}</button>`).join('')}
    </div>
    ${trades.length ? trades.map(t => `
      <div class="history-item">
        <div class="history-summary">
          <span class="txn-type ${transactionClass(t.action)}">${escapeHtml(t.action)}</span>
          <strong>${escapeHtml(t.ticker)}</strong>
          <span class="detail-meta">${fmtQty(t.quantity)} @ ${fmt$(t.price)}</span>
          <span class="history-total">${fmt$(t.total)}</span>
        </div>
        <div class="detail-meta history-time">${t.time ? new Date(t.time).toLocaleString() : ''}</div>
        ${t.reasoning ? `<div class="reason">${escapeHtml(t.reasoning)}</div>` : ''}
      </div>`).join('') : '<div class="loading">No trades.</div>'}`);
}
function setHistFilter(f) { histFilter = f; renderHistory(currentDetail); }

function renderPerformance(d) {
  const hist = d.pnl_history || [];
  renderHtml($('tab-performance'), hist.length
    ? '<div class="section-title">Return over time (%)</div><canvas id="perfChart" height="220"></canvas>'
    : '<div class="loading">No performance history yet.</div>');
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
  } catch (e) { console.error('perf chart failed', e); renderHtml($('tab-performance'), '<div class="loading">Chart unavailable.</div>'); }
}

const tradeOrder = createTradeOrder({
  requestJson,
  requestErrorType: ApiRequestError,
  element: $,
  renderHtml,
  escapeHtml,
  formatMoney: fmt$,
  formatQuantity: fmtQty,
  onFilled: async (order) => {
    delete riskCache[order.username];
    const fresh = await requestJson(`/api/agent-detail/${order.username}`);
    currentDetail = fresh;
    renderPortfolio(fresh);
    renderHistory(fresh);
    tradeOrder.render(fresh);
    renderHtml($('d-sub'), `${fmt$(fresh.portfolio.total_value)} · <span class="${cls(fresh.portfolio.pnl_percent)}">${fmtPct(fresh.portfolio.pnl_percent)}</span>`);
    loadLeaderboard();
  },
});

function renderTrade(detail) { tradeOrder.render(detail); }
function setTradeAction(action) { tradeOrder.setAction(action); }
function tradeInstrument(ticker) {
  if (!currentDetail || currentDetail.user_type !== 'human') return;
  closeStockDrawer();
  showTab('trade');
  $('trade-ticker').value = ticker;
  $('trade-ticker').focus();
}
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
    item(c.max_positions != null, `Hold no more than ${Number(c.max_positions)} different investments.`),
    item(c.max_allocation != null, `Put no more than ${allocationPercent(c.max_allocation)} of the portfolio into one investment.`),
  ].join('');
  const group = (title, criteria) => criteria ? `<div class="st-group"><div class="st-group-title">${title}</div><ul>${criteria}</ul></div>` : '';
  const ensemble = d.decision_architecture === 'multi_model';
  const roster = ensemble ? [...(d.model_roster?.advisers || []).map(member => `${escapeHtml(member.role)}: ${escapeHtml(member.model)}`), `chair: ${escapeHtml(d.model_roster?.judge?.model || 'unavailable')}`] : [];
  return `<div class="agent-strategy">
    <div class="st-label">📊 ${escapeHtml(s.label || 'Strategy')}</div>
    ${s.summary ? `<div class="st-sum">${escapeHtml(s.summary)}</div>` : ''}
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
    const result = await requestJson('/api/instruments', {
      method: 'POST',
      body: {ticker, instrument_type: $('ins-type').value, category: $('ins-category').value || null},
    });
    msg.textContent = `${result.instrument.ticker} is active and eligible for the next AI cycle.`;
    $('ins-ticker').value = ''; $('ins-category').value = ''; loadPopular();
  } catch (error) { msg.textContent = error.message; } finally { $('ins-submit').disabled = false; }
}
async function importEtfs() {
  const msg = $('ins-msg');
  if (!confirm('Import or refresh the curated ETF catalogue? Existing operator metadata is preserved.')) return;
  msg.textContent = 'Importing…';
  try {
    const result = await requestJson('/api/instruments/import-etfs', {method: 'POST'});
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
    const data = await requestJson('/api/agents', {method: 'POST', body});
    if (!data.ok) { msg.textContent = 'Failed: ' + (data.error || 'unknown error'); return; }
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
  if (message.type === 'DECISION_BATCH_UPDATED') runtimeActions.renderDecisionBatchStatus(message.data);

  const affectsLeaderboard = message.type === 'LEADERBOARD_UPDATE';
  const affectsActivity = message.type === 'TRANSACTION_UPDATE'
    || message.type === 'PORTFOLIO_RESET'
    || isExecutedTradeUpdate(message);

  if (affectsActivity && !$('view-activity').hidden) runtimeActions.loadActivity();
  if (affectsLeaderboard && !$('view-leaderboard').hidden) runtimeActions.refreshLeaderboard();
}

async function checkFunnelAfterResume() {
  try {
    renderFunnelStatus((await requestJson('/api/cycle/check', {method: 'POST'})).scheduler);
  } catch (_) {}
}

const clickActions = {
  'show-view': showView,
  'open-agent-modal': openAgentModal,
  'open-instrument-modal': openInstrumentModal,
  'trigger-decision-batch': decisionStatus.trigger,
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
  'close-trade-confirmation': tradeOrder.close,
  'confirm-trade': tradeOrder.confirm,
  'close-stock-drawer': closeStockDrawer,
  'open-drawer-ticker': openDrawerTicker,
  'select-stock-range': selectStockRange,
  'trade-instrument': tradeInstrument,
  'open-drawer': openDrawer,
  'set-history-filter': setHistFilter,
  'set-trade-action': setTradeAction,
  'review-trade': tradeOrder.review,
};

document.addEventListener('click', event => {
  const target = event.target.closest('[data-action]');
  const action = target && clickActions[target.dataset.action];
  if (action) action(target.dataset.arg);
});

document.addEventListener('change', event => {
  if (event.target.matches('[data-change-action="apply-style-preset"]')) applyStylePreset();
});

const runtimeActions = {
  loadActivity,
  openDrawerTicker,
  refreshLeaderboard,
  renderDecisionBatchStatus: decisionStatus.render,
};

Object.assign(window, {
  closeDrawer,
  closeStockDrawer,
  handleWebSocketMessage,
  renderDecisionBatchStatus: decisionStatus.render,
  renderFunnelStatus,
  renderPortfolio,
  strategyHtml,
  syncLbChartZoomState,
  transactionClass,
  triggerManualRefresh,
});
Object.defineProperties(window, {
  loadActivity: {
    get: () => runtimeActions.loadActivity,
    set: (value) => { runtimeActions.loadActivity = value; },
  },
  openDrawerTicker: {
    get: () => runtimeActions.openDrawerTicker,
    set: (value) => { runtimeActions.openDrawerTicker = value; },
  },
  refreshLeaderboard: {
    get: () => runtimeActions.refreshLeaderboard,
    set: (value) => { runtimeActions.refreshLeaderboard = value; },
  },
  renderDecisionBatchStatus: {
    get: () => runtimeActions.renderDecisionBatchStatus,
    set: (value) => { runtimeActions.renderDecisionBatchStatus = value; },
  },
  lbData: {
    get: () => lbData,
    set: (value) => { lbData = value; },
  },
  leaderboardRefreshInFlight: {
    get: () => leaderboardRefreshInFlight,
  },
  sortDir: {
    get: () => sortDir,
    set: (value) => { sortDir = value; },
  },
  sortKey: {
    get: () => sortKey,
    set: (value) => { sortKey = value; },
  },
  tradeAction: {
    get: () => tradeOrder.action,
  },
});

loadLeaderboard({ includeSupplementary: true });
decisionStatus.start();
loadFunnelStatus();
startRealtime({ onMessage: handleWebSocketMessage, onResume: checkFunnelAfterResume });
