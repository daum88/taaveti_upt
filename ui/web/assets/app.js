import { createActivity } from './modules/activity.js';
import { createAgentDrawer } from './modules/agent-drawer.js';
import { ApiRequestError, requestJson } from './modules/api-client.js';
import { destroyChart, registerChartZoom, replaceChart } from './modules/charts.js';
import { createDecisionStatus } from './modules/decision-status.js';
import { startDelegatedActions } from './modules/delegated-actions.js';
import { createInstruments } from './modules/instruments.js';
import { createLeaderboard } from './modules/leaderboard.js';
import { createOperations } from './modules/operations.js';
import { createPortfolioValueChart } from './modules/portfolio-value-chart.js';
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
import { createRefreshCoordinator } from './modules/refresh-coordinator.js';
import { createTradeOrder } from './modules/trade-order.js';
import { createViews } from './modules/views.js';

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

// ---- Risk metrics, sparkline, KPIs, and table live in leaderboard.js ----

async function loadLeaderboard() {
  await leaderboard.load();
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
const { setInstrumentFilter, loadMarketCatalogue, searchStock, selectStockRange, openDrawerTicker, closeStockDrawer } = instruments;

const portfolioChart = createPortfolioValueChart({
  canvas: $('lbChart'),
  controls: {
    player: $('lb-chart-player'),
    legend: $('lb-chart-legend'),
    summary: $('lb-chart-summary'),
    ranges: [...document.querySelectorAll('[data-lb-chart-range]')],
    reset: $('lb-chart-reset'),
    description: $('lb-chart-description'),
    hint: $('lb-chart-hint'),
    status: $('lb-chart-status'),
    retry: $('lb-chart-retry'),
    announcer: $('lb-chart-announcements'),
  },
  formatTimestamp: formatChartTimestamp,
  formatMoney: fmt$,
});

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
  portfolioChart,
  getDecisionBatchStatus: () => decisionBatchStatus,
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
  renderTradeTab: (detail) => tradeOrder.render(detail),
  isTradeUser: (detail) => detail.user_type === 'human',
});
const { openDrawer, closeDrawer, showTab, renderPortfolio, strategyHtml } = drawer;

const refreshCoordinator = createRefreshCoordinator({
  requestJson,
  leaderboard,
  getCurrentDetail: () => drawer.getCurrentDetail(),
  renderDetail: (detail) => drawer.renderDetail(detail),
});
function refreshLeaderboard() { return refreshCoordinator.refresh(); }

const activity = createActivity({
  requestJson,
  element: $,
  renderHtml,
  escapeHtml,
  fmt$,
  fmtQty,
  transactionClass,
});
const views = createViews({
  element: $,
  loadActivity: activity.load,
  loadMarkets: loadMarketCatalogue,
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
  loadMarketCatalogue,
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
    applyLeaderboardUpdate: (data) => runtimeActions.applyLeaderboardUpdate(data),
    refreshLeaderboard: () => runtimeActions.refreshLeaderboard(),
  },
});

function handleWebSocketMessage(message) {
  realtimeRouter.handleMessage(message);
}

const clickActions = {
  'show-view': views.show,
  'open-agent-modal': openAgentModal,
  'open-instrument-modal': openInstrumentModal,
  'trigger-decision-batch': decisionStatus.trigger,
  'trigger-manual-refresh': operations.triggerManualRefresh,
  'trigger-filing-warmup': operations.triggerFilingWarmup,
  'reset-lb-chart-zoom': portfolioChart.resetView,
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
  'load-more-decisions': drawer.loadMoreDecisions,
  'set-trade-action': setTradeAction,
  'review-trade': tradeOrder.review,
};

startDelegatedActions({
  clickActions,
  changeActions: { 'apply-style-preset': () => operations.applyStylePreset() },
});

const runtimeActions = {
  loadActivity: activity.load,
  openDrawerTicker,
  applyLeaderboardUpdate: leaderboard.applyLiveUpdate,
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
  syncLbChartZoomState: portfolioChart.syncNavigation,
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
  applyLeaderboardUpdate: {
    get: () => runtimeActions.applyLeaderboardUpdate,
    set: (value) => { runtimeActions.applyLeaderboardUpdate = value; },
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
    get: () => refreshCoordinator.inFlight,
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

loadLeaderboard();
decisionStatus.start();
operations.start();
startRealtime({ onMessage: handleWebSocketMessage, onResume: operations.checkFunnelAfterResume });
