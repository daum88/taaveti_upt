import { createActivity } from './modules/activity.js';
import { createAgentDrawer } from './modules/agent-drawer.js';
import { ApiRequestError, requestJson } from './modules/api-client.js';
import { destroyChart, registerChartZoom, replaceChart } from './modules/charts.js';
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
  renderHtml,
  transactionClass,
} from './modules/presentation.js';
import { createRealtimeRouter, startRealtime } from './modules/realtime.js';
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
          drawer.renderDetail(detail);
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
  replaceChart,
  destroyChart,
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
  replaceChart,
  getDecisionBatchStatus: () => decisionBatchStatus,
  getCachedDetail: (username) => leaderboard.getCachedDetail(username),
  renderTradeTab: (detail) => tradeOrder.render(detail),
  isTradeUser: (detail) => detail.user_type === 'human',
});
const { openDrawer, closeDrawer, showTab, renderPortfolio, strategyHtml } = drawer;

const activity = createActivity({
  requestJson,
  element: $,
  renderHtml,
  escapeHtml,
  fmt$,
  fmtQty,
  transactionClass,
});

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
    drawer.renderDetail(fresh);
    tradeOrder.render(fresh);
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
const realtimeRouter = createRealtimeRouter({
  isViewVisible: (name) => !$(`view-${name}`).hidden,
  actions: {
    renderDecisionBatchStatus: (data) => runtimeActions.renderDecisionBatchStatus(data),
    loadActivity: () => runtimeActions.loadActivity(),
    refreshLeaderboard: () => runtimeActions.refreshLeaderboard(),
  },
});

function handleWebSocketMessage(message) {
  realtimeRouter.handleMessage(message);
}

const clickActions = {
  'show-view': activity.showView,
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
  loadActivity: activity.load,
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
