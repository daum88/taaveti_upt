import { createAgentDrawer } from './modules/agent-drawer.js';
import { ApiRequestError, requestJson } from './modules/api-client.js';
import { createDecisionStatus } from './modules/decision-status.js';
import { createInstruments } from './modules/instruments.js';
import { createLeaderboard } from './modules/leaderboard.js';
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

let decisionBatchStatus = null;

function decisionDate(value, timezone) {
  return value ? new Intl.DateTimeFormat(undefined, {dateStyle: 'medium', timeStyle: 'short', timeZone: timezone}).format(new Date(value)) : 'Never';
}
const decisionStatus = createDecisionStatus({
  requestJson,
  requestErrorType: ApiRequestError,
  onStatusChange: (status) => {
    decisionBatchStatus = status;
    if (!$('view-leaderboard').hidden) leaderboard.renderTable();
    drawer.updateAccountDecisionStatus();
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

// ---- Risk metrics, sparkline, KPIs, table, and portfolio-value chart live in leaderboard.js ----

async function loadLeaderboard(options) {
  await leaderboard.load(options);
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
      const currentDetail = drawer.getCurrentDetail();
      if (currentDetail) {
        const detail = await requestJson(`/api/agent-detail/${currentDetail.username}`);
        if (!detail.error && drawer.getCurrentDetail()?.username === detail.username) {
          drawer.setCurrentDetail(detail);
          drawer.renderPortfolio(detail); drawer.renderHistory(detail);
          renderHtml($('d-sub'), `${fmt$(detail.portfolio.total_value)} · <span class="${cls(detail.portfolio.pnl_percent)}">${fmtPct(detail.portfolio.pnl_percent)}</span>`);
        }
      }
    } while (leaderboardRefreshPending);
  })();
  try { await leaderboardRefreshInFlight; } finally { leaderboardRefreshInFlight = null; }
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
  getCurrentDetail: () => drawer.getCurrentDetail(),
  resolveInstrument: (ticker) => runtimeActions.openDrawerTicker(ticker),
});
const { setInstrumentFilter, loadPopular, searchStock, selectStockRange, openDrawerTicker, closeStockDrawer } = instruments;

const leaderboard = createLeaderboard({
  requestJson,
  element: $,
  renderHtml,
  escapeHtml,
  fmt$,
  fmtPct,
  cls,
  initials,
  badgeFor,
  formatChartTimestamp,
  getDecisionBatchStatus: () => decisionBatchStatus,
  loadPopular,
});

const drawer = createAgentDrawer({
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
  getDecisionBatchStatus: () => decisionBatchStatus,
  getCachedDetail: (username) => leaderboard.getCachedDetail(username),
  renderTradeTab: (detail) => tradeOrder.render(detail),
  isTradeUser: (detail) => detail.user_type === 'human',
});
const { openDrawer, closeDrawer, showTab, renderPortfolio, strategyHtml } = drawer;

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
const tradeOrder = createTradeOrder({
  requestJson,
  requestErrorType: ApiRequestError,
  element: $,
  renderHtml,
  escapeHtml,
  formatMoney: fmt$,
  formatQuantity: fmtQty,
  onFilled: async (order) => {
    leaderboard.invalidate(order.username);
    const fresh = await requestJson(`/api/agent-detail/${order.username}`);
    drawer.setCurrentDetail(fresh);
    drawer.renderPortfolio(fresh);
    drawer.renderHistory(fresh);
    tradeOrder.render(fresh);
    renderHtml($('d-sub'), `${fmt$(fresh.portfolio.total_value)} · <span class="${cls(fresh.portfolio.pnl_percent)}">${fmtPct(fresh.portfolio.pnl_percent)}</span>`);
    loadLeaderboard();
  },
});

function setTradeAction(action) { tradeOrder.setAction(action); }
function tradeInstrument(ticker) {
  const currentDetail = drawer.getCurrentDetail();
  if (!currentDetail || currentDetail.user_type !== 'human') return;
  closeStockDrawer();
  showTab('trade');
  $('trade-ticker').value = ticker;
  $('trade-ticker').focus();
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
  'reset-lb-chart-zoom': leaderboard.resetLbChartZoom,
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
  'set-history-filter': drawer.setHistFilter,
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
  syncLbChartZoomState: leaderboard.syncLbChartZoomState,
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
    get: () => leaderboard.data,
    set: (value) => { leaderboard.data = value; },
  },
  leaderboardRefreshInFlight: {
    get: () => leaderboardRefreshInFlight,
  },
  sortDir: {
    get: () => leaderboard.sortDir,
    set: (value) => { leaderboard.sortDir = value; },
  },
  sortKey: {
    get: () => leaderboard.sortKey,
    set: (value) => { leaderboard.sortKey = value; },
  },
  tradeAction: {
    get: () => tradeOrder.action,
  },
});

loadLeaderboard({ includeSupplementary: true });
decisionStatus.start();
loadFunnelStatus();
startRealtime({ onMessage: handleWebSocketMessage, onResume: checkFunnelAfterResume });
