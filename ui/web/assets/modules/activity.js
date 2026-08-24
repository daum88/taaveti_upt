function displayTimestamp(t) {
  return t.execution_quote_captured_at || t.executed_at || null;
}

function dateKey(value) {
  if (!value) return 'unknown';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? 'unknown' : date.toDateString();
}

function groupLabel(key) {
  if (key === 'unknown') return 'Unknown date';
  const date = new Date(key);
  const formatted = date.toLocaleDateString(undefined, {
    weekday: 'short', month: 'short', day: 'numeric', year: 'numeric',
  });
  const yesterday = new Date();
  yesterday.setDate(yesterday.getDate() - 1);
  if (key === new Date().toDateString()) return `Today · ${formatted}`;
  if (key === yesterday.toDateString()) return `Yesterday · ${formatted}`;
  return formatted;
}

export function createActivity({ requestJson, element, renderHtml, escapeHtml, fmt$, fmtQty, cls, transactionClass }) {
  function pnlCell(t) {
    const pnl = Number(t.realized_pnl);
    if (t.transaction_type !== 'SELL' || !Number.isFinite(pnl)) return '<td class="num">—</td>';
    const sign = pnl >= 0 ? '+' : '-';
    return `<td class="num ${cls(pnl)}">${sign}${fmt$(Math.abs(pnl))}</td>`;
  }

  async function load() {
    const data = await requestJson('/api/transactions?limit=50');
    if (!data.length) {
      renderHtml(element('act-body'), '<tr><td colspan="8" class="loading">No transactions yet.</td></tr>');
      return;
    }
    const rows = [];
    let lastKey = null;
    for (const t of data) {
      const timestamp = displayTimestamp(t);
      const key = dateKey(timestamp);
      if (key !== lastKey) {
        rows.push(`<tr class="date-group"><td colspan="8">${escapeHtml(groupLabel(key))}</td></tr>`);
        lastKey = key;
      }
      const date = timestamp ? new Date(timestamp) : null;
      const valid = date && !Number.isNaN(date.getTime());
      const prefix = t.execution_quote_captured_at ? 'Quote' : 'Recorded';
      const timeText = valid ? date.toLocaleTimeString() : '—';
      const titleParts = [t.execution_quote_source || 'legacy record', t.execution_market_state || 'unknown market state'];
      if (valid) titleParts.push(date.toLocaleString());
      rows.push(`
    <tr>
      <td class="hide-mobile" title="${escapeHtml(titleParts.join('; '))}">${prefix} ${escapeHtml(timeText)}</td>
      <td>${escapeHtml(t.username)}</td>
      <td class="${transactionClass(t.transaction_type)} txn-type">${escapeHtml(t.transaction_type)}</td>
      <td><button type="button" class="ticker-link" data-action="open-drawer-ticker" data-arg="${escapeHtml(t.ticker)}">${escapeHtml(t.ticker)}</button></td>
      <td class="num">${fmtQty(t.quantity)}</td>
      <td class="num">${fmt$(t.price_per_share)}</td>
      <td class="num">${fmt$(t.total_value)}</td>
      ${pnlCell(t)}
    </tr>`);
    }
    renderHtml(element('act-body'), rows.join(''));
  }

  return { load };
}
