export const createLeaderboard = ({
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
  getDecisionBatchStatus,
}) => {
  let lbData = [];
  let sortKey = 'rank', sortDir = 1;
  const riskCache = {}; // username -> {volatility, maxdd, pnl_history}
  let chartHistory = null;
  let chartUsers = null;
  let lbChartRequest = 0;

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

  function normalizePnlHistory(history) {
    const entries = Array.isArray(history) ? history : [];
    const firstValue = entries.map(entry => Number(entry?.value)).find(Number.isFinite);
    return entries.flatMap((entry) => {
      const value = Number(entry?.value);
      if (!Number.isFinite(value)) return [];
      const recordedPercent = Number(entry?.pnl_percent);
      const pnlPct = Number.isFinite(recordedPercent)
        ? recordedPercent
        : Number.isFinite(firstValue) && firstValue !== 0 ? (value - firstValue) / firstValue * 100 : 0;
      return [{ pnl_pct: pnlPct }];
    });
  }

  function applyHistoryMetrics(history) {
    for (const username of Object.keys(riskCache)) delete riskCache[username];
    for (const row of lbData) {
      const pnlHistory = normalizePnlHistory(history?.[String(row.user_id)]);
      riskCache[row.username] = { ...computeRisk(pnlHistory), pnl_history: pnlHistory };
    }
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
  async function load() {
    const data = await requestJson('/api/leaderboard');
    lbData = data;
    renderKPIs(data);
    renderTable();
    await renderLbChart();
  }

  async function renderLbChart() {
    const request = ++lbChartRequest;
    portfolioChart.setLoading();
    try {
      const { history, users } = await requestJson('/api/portfolio-history');
      if (request !== lbChartRequest) return;
      chartHistory = history;
      chartUsers = users;
      applyHistoryMetrics(history);
      portfolioChart.update({ history, users, rankings: lbData });
      renderTable();
    } catch (error) {
      if (request !== lbChartRequest) return;
      console.error('leaderboard chart failed', error);
      portfolioChart.showError(renderLbChart);
    }
  }

  function applyLiveUpdate(data) {
    if (!Array.isArray(data)) return;
    lbData = data;
    renderKPIs(data);
    renderTable();
    if (chartHistory && chartUsers) portfolioChart.update({ history: chartHistory, users: chartUsers, rankings: lbData });
  }

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
    return row.user_type === 'llm_agent' && getDecisionBatchStatus()?.agents?.[row.username]?.status === 'running'
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

  return {
    load,
    applyLiveUpdate,
    renderTable,
    invalidate: (username) => { delete riskCache[username]; },
    get data() { return lbData; },
    set data(value) { lbData = value; },
    get sortKey() { return sortKey; },
    set sortKey(value) { sortKey = value; },
    get sortDir() { return sortDir; },
    set sortDir(value) { sortDir = value; },
  };
};
