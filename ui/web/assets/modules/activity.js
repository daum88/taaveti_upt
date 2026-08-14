export function createActivity({ requestJson, element, renderHtml, escapeHtml, fmt$, fmtQty, transactionClass }) {
  async function load() {
    const data = await requestJson('/api/transactions?limit=50');
    renderHtml(element('act-body'), data.length ? data.map(t => `
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

  function showView(view) {
    element('view-leaderboard').hidden = view !== 'leaderboard';
    element('view-activity').hidden = view !== 'activity';
    element('nav-lb').classList.toggle('active', view === 'leaderboard');
    element('nav-act').classList.toggle('active', view === 'activity');
    if (view === 'activity') load();
  }

  return { load, showView };
}
