const SECTOR_COLORS = ['#0969da', '#1a7f37', '#9a6700', '#cf222e', '#8250df', '#0550ae', '#116329', '#bc4c00', '#a40e26'];

const STRATEGY_TABS = ['portfolio', 'history', 'performance', 'trade'];

const DECISION_PAGE_SIZE = 10;

const EXECUTION_STATUS = {
  executed: ['Executed', 'executed'],
  hold: ['No trade', 'hold'],
  rejected: ['Blocked', 'rejected'],
  not_attempted: ['Not attempted', 'na'],
  pending: ['Pending', 'na'],
};

const RESPONSE_FAILURE = {
  malformed: 'Unreadable response',
  provider_failed: 'Provider failed',
  configuration_failed: 'Configuration failed',
};

export function createAgentDrawer({
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
  getDecisionBatchStatus,
  renderTradeTab,
  isTradeUser,
}) {
  let currentDetail = null;
  let sectorChart = null;
  let perfChart = null;
  let histFilter = 'ALL';
  let decisionsState = { username: null, items: [], done: true, loading: false, error: null };
  let decisionsRequestId = 0;
  const detailCache = new Map();

  function accountDecisionStatusText(detail) {
    const agent = getDecisionBatchStatus()?.agents?.[detail.username];
    const status = ({
      queued: 'Queued', running: 'Running', completed: 'Completed', completed_with_errors: 'Completed with errors', failed: 'Failed', interrupted: 'Interrupted',
    })[agent?.status] || 'Ready to run';
    return `AI decision status: ${status}${agent?.completed_at ? ` · Last completed: ${new Date(agent.completed_at).toLocaleString()}` : ''}`;
  }

  function updateAccountDecisionStatus() {
    const status = $('account-decision-status');
    if (status && currentDetail?.user_type === 'llm_agent') status.textContent = accountDecisionStatusText(currentDetail);
  }

  async function openDrawer(username) {
    $('overlay').classList.add('open');
    $('drawer').classList.add('open');
    $('d-name').textContent = username;
    $('d-sub').textContent = 'Loading…';
    renderHtml($('tab-portfolio'), '<div class="loading">Loading…</div>');
    showTab('portfolio');
    try {
      const d = detailCache.get(username) || await requestJson(`/api/agent-detail/${username}`);
      if (!d || !d.portfolio) throw new Error('No portfolio data in response');
      detailCache.set(username, d);
      currentDetail = d;
      renderHtml($('d-name'), `${escapeHtml(d.display_name || username)} ${badgeFor(d.user_type, d.decision_architecture)}`);
      renderSubtitle(d);
      renderPortfolio(d);
      renderHistory(d);
      renderTradeTab(d);
      $('tab-btn-trade').hidden = !isTradeUser(d);
    } catch (e) {
      console.error('openDrawer failed:', e);
      $('d-sub').textContent = '';
      renderHtml($('tab-portfolio'), `<div class="loading">Failed to load: ${escapeHtml(e.message)}</div>`);
    }
  }

  function renderSubtitle(d) {
    const p = d.portfolio;
    renderHtml($('d-sub'), `${fmt$(p.total_value)} · <span class="${cls(p.pnl_percent)}">${fmtPct(p.pnl_percent)}</span>`);
  }

  function renderDetail(d) {
    detailCache.set(d.username, d);
    currentDetail = d;
    renderSubtitle(d);
    renderPortfolio(d);
    renderHistory(d);
  }

  function closeDrawer() {
    $('overlay').classList.remove('open');
    $('drawer').classList.remove('open');
  }

  function showTab(t) {
    STRATEGY_TABS.forEach(x => {
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
        <td><button type="button" class="ticker-link" data-action="open-drawer-ticker" data-arg="${escapeHtml(h.ticker)}">${escapeHtml(h.ticker)}</button></td><td class="hide-mobile">${formatBuyDate(h.opened_at)}</td><td class="num">${fmtQty(h.quantity)}</td>
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
      <div class="sector-chart"><canvas id="sectorChart"></canvas></div>
      ${d.user_type === 'llm_agent' ? '<div class="section-title">Decision history</div><div id="decision-history"><div class="loading">Fetching decisions…</div></div>' : ''}`);
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
    syncDecisionHistory(d);
  }

  function decisionsUrl(username, beforeId) {
    const params = new URLSearchParams({ limit: String(DECISION_PAGE_SIZE) });
    if (beforeId != null) params.set('before_id', String(beforeId));
    return `/api/agents/${encodeURIComponent(username)}/decisions?${params}`;
  }

  async function syncDecisionHistory(detail) {
    if (detail.user_type !== 'llm_agent' || !detail.username) return;
    const sameAgent = decisionsState.username === detail.username;
    if (!sameAgent) decisionsState = { username: detail.username, items: [], done: false, loading: false, error: null };
    const requestId = ++decisionsRequestId;
    decisionsState.loading = true;
    let shouldRender = false;
    try {
      const page = await requestJson(decisionsUrl(detail.username, null));
      if (requestId !== decisionsRequestId) return;
      const items = Array.isArray(page) ? page : [];
      const unchanged = sameAgent
        && items.length === Math.min(decisionsState.items.length, DECISION_PAGE_SIZE)
        && items.every((item, index) => item.id === decisionsState.items[index]?.id);
      if (!unchanged) {
        decisionsState.items = items;
        decisionsState.done = items.length < DECISION_PAGE_SIZE;
        decisionsState.error = null;
        shouldRender = true;
      }
    } catch (e) {
      if (requestId !== decisionsRequestId) return;
      if (!decisionsState.items.length) {
        decisionsState.error = e.message;
        shouldRender = true;
      }
    }
    decisionsState.loading = false;
    if (shouldRender) renderDecisions();
  }

  async function loadMoreDecisions() {
    const { username, items, done, loading } = decisionsState;
    if (loading || done || !username || !items.length) return;
    const requestId = ++decisionsRequestId;
    decisionsState.loading = true;
    renderDecisions();
    try {
      const page = await requestJson(decisionsUrl(username, items[items.length - 1].id));
      if (requestId !== decisionsRequestId) return;
      const more = Array.isArray(page) ? page : [];
      decisionsState.items = [...items, ...more];
      decisionsState.done = more.length < DECISION_PAGE_SIZE;
      decisionsState.error = null;
    } catch (e) {
      if (requestId !== decisionsRequestId) return;
      decisionsState.error = e.message;
    } finally {
      if (requestId === decisionsRequestId) {
        decisionsState.loading = false;
        renderDecisions();
      }
    }
  }

  function rejectionMessage(rejection) {
    if (!rejection) return '';
    if (typeof rejection === 'object') return rejection.message || rejection.code || JSON.stringify(rejection);
    return String(rejection);
  }

  function decisionItemHtml(item) {
    const badge = item.decision
      ? `<span class="txn-type ${item.decision === 'BUY' ? 'pos' : item.decision === 'SELL' ? 'neg' : ''}">${escapeHtml(item.decision)}</span>`
      : `<span class="txn-type">${escapeHtml(RESPONSE_FAILURE[item.response_status] || 'Model call failed')}</span>`;
    const ticker = item.ticker ? `<button type="button" class="ticker-link" data-action="open-drawer-ticker" data-arg="${escapeHtml(item.ticker)}">${escapeHtml(item.ticker)}</button>` : '';
    const allocation = item.allocation_percentage > 0 ? `<span class="detail-meta">${Math.round(item.allocation_percentage * 100)}% of portfolio</span>` : '';
    const [statusLabel, statusClass] = EXECUTION_STATUS[item.execution_status] || [item.execution_status || 'Unknown', 'na'];
    const model = item.model_name || item.provider || '';
    const rejection = rejectionMessage(item.rejection);
    return `<div class="history-item decision-item">
      <div class="history-summary">${badge}${ticker}${allocation}<span class="history-total"><span class="decision-status-chip ${statusClass}">${escapeHtml(statusLabel)}</span></span></div>
      <div class="detail-meta history-time">${item.time ? new Date(item.time).toLocaleString() : ''}${model ? ` · ${escapeHtml(model)}` : ''}</div>
      ${rejection ? `<div class="detail-meta decision-rejection">${item.execution_status === 'rejected' ? 'Blocked: ' : ''}${escapeHtml(rejection)}</div>` : ''}
      ${item.reasoning ? `<details class="decision-reason"><summary>Why</summary><div class="reason">${escapeHtml(item.reasoning)}</div></details>` : ''}
    </div>`;
  }

  function renderDecisions() {
    const container = $('decision-history');
    if (!container) return;
    const { items, done, loading, error } = decisionsState;
    if (!items.length) {
      if (error) { renderHtml(container, `<div class="loading">Failed to load decisions: ${escapeHtml(error)}</div>`); return; }
      renderHtml(container, loading ? '<div class="loading">Fetching decisions…</div>' : '<div class="loading">No decisions recorded yet.</div>');
      return;
    }
    const loadMore = done ? '' : `<div class="load-more-row"><button type="button" class="load-more-btn" data-action="load-more-decisions" ${loading ? 'disabled' : ''}>${loading ? 'Loading…' : 'Show older decisions'}</button></div>`;
    renderHtml(container, items.map(decisionItemHtml).join('') + loadMore);
  }

  function renderSectorChart(sectors) {
    const labels = Object.keys(sectors || {}), vals = Object.values(sectors || {});
    const el = $('sectorChart');
    if (!el) return;
    if (!labels.length) { renderHtml(el.parentElement, '<div class="loading">No sector data.</div>'); return; }
    try {
      sectorChart = replaceChart(el, {
        type: 'doughnut',
        data: { labels, datasets: [{ data: vals, backgroundColor: SECTOR_COLORS }] },
        options: { plugins: { legend: { position: 'right', labels: { color: '#1f2328', boxWidth: 12, font: { size: 11 } } } } }
      });
    } catch (e) { console.error('sector chart failed', e); renderHtml(el.parentElement, '<div class="loading">Chart unavailable.</div>'); }
  }

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
            <button type="button" class="ticker-link" data-action="open-drawer-ticker" data-arg="${escapeHtml(t.ticker)}">${escapeHtml(t.ticker)}</button>
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
    const labels = hist.map(h => new Date(h.time).toLocaleDateString());
    const vals = hist.map(h => h.pnl_pct);
    const up = vals[vals.length - 1] >= 0;
    try {
      perfChart = replaceChart($('perfChart'), {
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

  return {
    openDrawer,
    closeDrawer,
    showTab,
    renderDetail,
    renderPortfolio,
    renderHistory,
    renderPerformance,
    setHistFilter,
    loadMoreDecisions,
    strategyHtml,
    updateAccountDecisionStatus,
    getCurrentDetail: () => currentDetail,
    setCurrentDetail: (detail) => { currentDetail = detail; },
  };
}
