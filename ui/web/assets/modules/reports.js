const toMonthValue = (date) => date.toISOString().slice(0, 7);

function monthRange(monthValue) {
  const [year, month] = monthValue.split('-').map(Number);
  const start = new Date(Date.UTC(year, month - 1, 1));
  const end = new Date(Date.UTC(year, month, 0));
  return { start: start.toISOString().slice(0, 10), end: end.toISOString().slice(0, 10) };
}

function presetRange(preset) {
  const now = new Date();
  const today = now.toISOString().slice(0, 10);
  if (preset === 'this-month') return monthRange(toMonthValue(now)).end > today
    ? { start: `${toMonthValue(now)}-01`, end: today }
    : monthRange(toMonthValue(now));
  if (preset === 'last-month') {
    const previous = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - 1, 1));
    return monthRange(toMonthValue(previous));
  }
  if (preset === 'ytd') return { start: `${now.getUTCFullYear()}-01-01`, end: today };
  return null;
}

export function createReports({ requestJson, element, renderHtml, escapeHtml, fmt$, fmtPct, cls, replaceChart }) {
  let accounts = [];
  let started = false;

  const stat = (label, value, tone) => {
    const present = value !== null && value !== undefined;
    const color = typeof tone === 'number' ? cls(tone) : '';
    return `
    <div class="stat"><div class="l">${escapeHtml(label)}</div>
    <div class="v ${color}">${present ? value : '—'}</div></div>`;
  };

  const money = (value) => (value === null || value === undefined ? null : fmt$(value));
  const pct = (value) => (value === null || value === undefined ? null : fmtPct(value));

  function currentParams() {
    const userId = element('report-account').value;
    const customStart = element('report-start').value;
    const customEnd = element('report-end').value;
    if (customStart && customEnd) {
      return `user_id=${encodeURIComponent(userId)}&start=${customStart}&end=${customEnd}`;
    }
    const month = element('report-month').value;
    return `user_id=${encodeURIComponent(userId)}&month=${month}`;
  }

  async function loadAccounts() {
    accounts = await requestJson('/api/reports/accounts');
    const select = element('report-account');
    select.innerHTML = accounts
      .map((a) => `<option value="${a.user_id}">${escapeHtml(a.display_name)}${a.is_benchmark ? ' (benchmark)' : ''}</option>`)
      .join('');
  }

  function applyRange({ start, end }) {
    element('report-start').value = '';
    element('report-end').value = '';
    element('report-month').value = start.slice(0, 7);
    const range = monthRange(start.slice(0, 7));
    if (range.start !== start || range.end !== end) {
      element('report-start').value = start;
      element('report-end').value = end;
    }
  }

  async function load() {
    if (!started) {
      started = true;
      element('report-month').value = toMonthValue(new Date());
      element('report-month').max = toMonthValue(new Date());
      const today = new Date().toISOString().slice(0, 10);
      element('report-start').max = today;
      element('report-end').max = today;
      try {
        await loadAccounts();
      } catch (error) {
        renderHtml(element('report-status'), escapeHtml(error.message));
        return;
      }
    }
    if (!accounts.length) {
      renderHtml(element('report-status'), 'No accounts to report on yet.');
      return;
    }
    renderHtml(element('report-status'), 'Loading report…');
    renderHtml(element('report-body'), '');
    try {
      const report = await requestJson(`/api/reports/monthly?${currentParams()}`);
      render(report);
      renderHtml(element('report-status'), '');
    } catch (error) {
      renderHtml(element('report-status'), escapeHtml(error.message));
    }
  }

  function tradeLine(trade, label) {
    if (!trade) return stat(label, null);
    return stat(label, `${escapeHtml(trade.ticker)} · ${fmt$(trade.realized_pnl)}`, trade.realized_pnl);
  }

  function renderStrategy(strategy) {
    if (!strategy) return '';
    const chips = [];
    if (strategy.label) chips.push(`<span class="chip strategy-label">${escapeHtml(strategy.label)}</span>`);
    if (strategy.model) chips.push(`<span class="chip">${escapeHtml(strategy.model)}</span>`);
    const c = strategy.constraints;
    if (c) {
      chips.push(`<span class="chip">max ${c.max_positions} positions</span>`);
      chips.push(`<span class="chip">≤${c.max_allocation_percent}% per position</span>`);
      chips.push(`<span class="chip">≥${c.cash_reserve_percent}% cash reserve</span>`);
      chips.push(`<span class="chip">≤${c.max_sector_allocation_percent}% per sector</span>`);
      if (c.eligible_instruments) chips.push(`<span class="chip">universe: ${c.eligible_instruments.length} tickers</span>`);
    }
    return `
      <div class="section-title">Stated principles</div>
      <div class="principle-chips">${chips.join('')}</div>
      ${strategy.summary ? `<p class="strategy-summary">${escapeHtml(strategy.summary)}</p>` : ''}
      ${strategy.persona_prompt ? `<details class="strategy-persona"><summary>Persona prompt</summary><p>${escapeHtml(strategy.persona_prompt)}</p></details>` : ''}`;
  }

  function renderFindings(findings) {
    if (!findings || !findings.length) return '';
    const items = findings
      .map(
        (f) => `<li class="finding ${f.tone}">
          <span class="finding-title">${escapeHtml(f.title)}</span>
          <span class="finding-detail">${escapeHtml(f.detail)}</span>
        </li>`,
      )
      .join('');
    return `<div class="section-title">What's driving this</div><ul class="findings">${items}</ul>`;
  }

  function renderAiBlock(report) {
    if (report.account.user_type !== 'llm_agent') return '';
    return `
      <div class="section-title">AI assessment</div>
      <div class="report-ai">
        <button type="button" data-action="report-ai-analysis">Generate AI assessment</button>
        <div id="report-ai-body" class="report-ai-body"></div>
      </div>`;
  }

  async function aiAnalysis() {
    const body = element('report-ai-body');
    if (!body) return;
    renderHtml(body, '<span class="loading">Generating assessment…</span>');
    try {
      const result = await requestJson(`/api/reports/analysis?${currentParams()}`);
      renderHtml(
        body,
        `<p class="report-ai-text">${escapeHtml(result.narrative)}</p>\n         <div class="report-ai-meta">Generated by ${escapeHtml(result.model || 'the configured model')}</div>`,
      );
    } catch (error) {
      renderHtml(body, `<span class="report-ai-error">${escapeHtml(error.message)}</span>`);
    }
  }

  function render(report) {
    if (!report.has_data) {
      renderHtml(
        element('report-body'),
        `<div class="loading">No activity for ${escapeHtml(report.account.display_name)} in ${escapeHtml(report.period.start)} → ${escapeHtml(report.period.end)}.</div>`,
      );
      return;
    }
    const v = report.value;
    const p = report.pnl;
    const t = report.trading;
    const r = report.risk;
    const c = report.cash;
    const tickerRows = report.per_ticker
      .map(
        (row) => `<tr>
          <td>${escapeHtml(row.ticker)}</td>
          <td class="num">${row.trades}</td>
          <td class="num ${cls(row.realized_pnl)}">${fmt$(row.realized_pnl)}</td>
          <td class="num">${fmt$(row.bought)}</td>
          <td class="num">${fmt$(row.sold)}</td>
        </tr>`,
      )
      .join('');
    const benchmarkRows = report.benchmarks
      .map(
        (b) => `<tr>
          <td>${escapeHtml(b.display_name)}</td>
          <td class="num ${typeof b.change_percent === 'number' ? cls(b.change_percent) : ''}">${pct(b.change_percent) ?? '—'}</td>
          <td class="num ${typeof b.alpha_percent === 'number' ? cls(b.alpha_percent) : ''}">${b.alpha_percent === null ? '—' : fmtPct(b.alpha_percent)}</td>
        </tr>`,
      )
      .join('');

    renderHtml(
      element('report-body'),
      `
      <div class="section-title">${escapeHtml(report.account.display_name)} · ${escapeHtml(report.period.start)} → ${escapeHtml(report.period.end)}</div>
      ${renderStrategy(report.strategy)}
      ${renderFindings(report.findings)}
      ${renderAiBlock(report)}
      <div class="section-title">Portfolio value</div>
      <div class="stat-grid">
        ${stat('Start', money(v.start))}
        ${stat('End', money(v.end))}
        ${stat('Change', v.change === null ? null : `${fmt$(v.change)} (${fmtPct(v.change_percent)})`, v.change)}
        ${stat('Period high', money(v.high))}
        ${stat('Period low', money(v.low))}
        ${stat('Max drawdown', pct(r.max_drawdown_percent))}
      </div>
      <div class="section-title">P&amp;L decomposition</div>
      <div class="stat-grid">
        ${stat('Total P&L change', money(p.total_change), p.total_change)}
        ${stat('Realized', money(p.realized), p.realized)}
        ${stat('Unrealized Δ', money(p.unrealized_change), p.unrealized_change)}
        ${stat('Dividends', money(p.dividends), p.dividends)}
        ${stat('Fees', money(p.fees), -p.fees)}
      </div>
      <div class="section-title">Trading activity</div>
      <div class="stat-grid">
        ${stat('Trades', t.total_trades)}
        ${stat('Buys / Sells', `${t.buys} / ${t.sells}`)}
        ${stat('Turnover', pct(t.turnover_percent))}
        ${stat('Win rate', pct(t.win_rate))}
        ${stat('Avg win', money(t.avg_win), t.avg_win)}
        ${stat('Avg loss', money(t.avg_loss), t.avg_loss)}
        ${tradeLine(t.best_trade, 'Best trade')}
        ${tradeLine(t.worst_trade, 'Worst trade')}
      </div>
      <div class="section-title">Risk &amp; cash</div>
      <div class="stat-grid">
        ${stat('Best day', r.best_day ? `${escapeHtml(r.best_day.date)} (${fmtPct(r.best_day.change_percent)})` : null)}
        ${stat('Worst day', r.worst_day ? `${escapeHtml(r.worst_day.date)} (${fmtPct(r.worst_day.change_percent)})` : null)}
        ${stat('End cash', money(c.end))}
        ${stat('Avg cash %', pct(c.avg_percent))}
        ${stat('Min cash', money(c.min))}
      </div>
      ${benchmarkRows ? `
      <div class="section-title">Benchmark comparison</div>
      <table><thead><tr><th>Benchmark</th><th class="num">Change</th><th class="num">Alpha</th></tr></thead>
      <tbody>${benchmarkRows}</tbody></table>` : ''}
      ${tickerRows ? `
      <div class="section-title">Per-ticker realized P&amp;L</div>
      <table><thead><tr><th>Ticker</th><th class="num">Trades</th><th class="num">Realized P&amp;L</th><th class="num">Bought</th><th class="num">Sold</th></tr></thead>
      <tbody>${tickerRows}</tbody></table>` : ''}
      <div class="section-title">Equity curve</div>
      <div class="report-chart"><canvas id="reportChart" height="240" role="img" aria-label="Equity curve for the selected period"></canvas></div>
      `,
    );
    renderChart(report.equity_curve);
  }

  function renderChart(points) {
    const canvas = element('reportChart');
    if (!canvas) return;
    if (!points.length) {
      canvas.replaceWith(Object.assign(document.createElement('div'), { className: 'loading', textContent: 'No valuation snapshots in this period.' }));
      return;
    }
    const values = points.map((p) => p.value);
    const positive = values.length > 1 && values[values.length - 1] >= values[0];
    replaceChart(canvas, {
      type: 'line',
      data: {
        labels: points.map((p) => p.time.slice(0, 10)),
        datasets: [
          {
            label: 'Portfolio value',
            data: values,
            borderColor: positive ? '#3fb950' : '#f85149',
            backgroundColor: positive ? 'rgba(63,185,80,0.12)' : 'rgba(248,81,73,0.12)',
            fill: true,
            tension: 0.2,
            pointRadius: 2,
          },
        ],
      },
      options: {
        plugins: { legend: { display: false } },
        scales: { y: { ticks: { callback: (value) => fmt$(value) } } },
      },
    });
  }

  function preset(kind) {
    const range = presetRange(kind);
    if (range) applyRange(range);
    return load();
  }

  function clearCustom() {
    element('report-start').value = '';
    element('report-end').value = '';
    return load();
  }

  return { load, preset, clearCustom, aiAnalysis };
}
