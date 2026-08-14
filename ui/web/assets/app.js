import { createAgentDrawer } from './modules/agent-drawer.js';
import { ApiRequestError, requestJson } from './modules/api-client.js';
import { createDecisionStatus } from './modules/decision-status.js';
import { createInstruments } from './modules/instruments.js';
import { createLeaderboard } from './modules/leaderboard.js';
import { createOperations } from './modules/operations.js';
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

const decisionStatus = createDecisionStatus({
  requestJson,
  requestErrorType: ApiRequestError,
  onStatusChange: (status) => {
    decisionBatchStatus = status;
    if (!$('view-leaderboard').hidden) leaderboard.renderTable();
    drawer.updateAccountDecisionStatus();
  },
});

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

const operations = createOperations({
  requestJson,
  element: $,
  loadPopular,
  loadLeaderboard,
});
const {
  renderFunnelStatus,
  triggerManualRefresh,
  openAgentModal,
  closeAgentModal,
  submitAgent,
  openInstrumentModal,
  closeInstrumentModal,
  submitInstrument,
  importEtfs,
} = operations;

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

const clickActions = {
  'show-view': showView,
  'open-agent-modal': openAgentModal,
  'open-instrument-modal': openInstrumentModal,
  'trigger-decision-batch': decisionStatus.trigger,
  'trigger-manual-refresh': operations.triggerManualRefresh,
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
  if (event.target.matches('[data-change-action="apply-style-preset"]')) operations.applyStylePreset();
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
operations.start();
startRealtime({ onMessage: handleWebSocketMessage, onResume: operations.checkFunnelAfterResume });
