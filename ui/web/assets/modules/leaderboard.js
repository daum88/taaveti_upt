const LB_COLORS = ['#0969da','#1a7f37','#9a6700','#cf222e','#8250df','#0550ae','#116329','#bc4c00','#a40e26','#953800'];

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
  formatChartTimestamp,
  getDecisionBatchStatus,
  loadPopular,
}) => {
  let lbData = [];
  let sortKey = 'rank', sortDir = 1;
  const riskCache = {}; // username -> {volatility, maxdd, pnl_history, detail}

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
  async function load({ includeSupplementary = false } = {}) {
    const data = await requestJson('/api/leaderboard');
    lbData = data;
    renderKPIs(data);
    renderTable();
    await renderLbChart();
    if (!includeSupplementary) return;
    loadPopular();
    for (const row of data) fetchRisk(row.username).then(renderTable);
  }

  // ---- Leaderboard chart ----
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
    renderTable,
    syncLbChartZoomState,
    resetLbChartZoom,
    invalidate: (username) => { delete riskCache[username]; },
    getCachedDetail: (username) => riskCache[username] && riskCache[username].detail,
    get data() { return lbData; },
    set data(value) { lbData = value; },
    get sortKey() { return sortKey; },
    set sortKey(value) { sortKey = value; },
    get sortDir() { return sortDir; },
    set sortDir(value) { sortDir = value; },
  };
};
