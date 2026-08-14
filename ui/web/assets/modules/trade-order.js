/**
 * Owns the manual-order UI state machine: drafting, previewing, confirming,
 * retrying a connection failure with the same client order ID, and cancellation.
 *
 * @param {{
 *   requestJson: (path: string, options?: object) => Promise<unknown>,
 *   requestErrorType: typeof Error,
 *   element: (id: string) => HTMLElement,
 *   renderHtml: (element: HTMLElement, markup: string) => void,
 *   escapeHtml: (value: unknown) => string,
 *   formatMoney: (value: number) => string,
 *   formatQuantity: (value: number) => string,
 *   onFilled: (order: object) => Promise<void>,
 * }} dependencies
 */
export const createTradeOrder = ({
  requestJson,
  requestErrorType,
  element,
  renderHtml,
  escapeHtml,
  formatMoney,
  formatQuantity,
  onFilled,
}) => {
  let action = 'BUY';
  let pendingOrder = null;
  let returnFocus = null;
  let executionAbort = null;

  const message = (text, error = false) => {
    const target = element('trade-msg');
    if (!target) return;
    target.className = `trade-msg ${error ? 'err' : 'ok'}`;
    target.textContent = text;
  };

  const close = () => {
    executionAbort?.abort();
    element('trade-confirm-overlay').classList.remove('open');
    element('trade-confirm-modal').classList.remove('open');
    pendingOrder = null;
    returnFocus?.focus();
  };

  const setAction = (nextAction) => {
    action = nextAction === 'SELL' ? 'SELL' : 'BUY';
    const buy = element('seg-buy');
    const sell = element('seg-sell');
    if (buy) buy.className = action === 'BUY' ? 'buy-on' : '';
    if (sell) sell.className = action === 'SELL' ? 'sell-on' : '';
  };

  const render = (detail) => {
    const holdings = detail.portfolio.holdings || [];
    const holdingOptions = holdings
      .map((holding) => `<option value="${escapeHtml(holding.ticker)}">${escapeHtml(holding.ticker)} (${formatQuantity(holding.quantity)} @ ${formatMoney(holding.current_price)})</option>`)
      .join('');
    renderHtml(element('tab-trade'), `
      <div class="trade-form">
        <div class="seg"><button id="seg-buy" class="buy-on" data-action="set-trade-action" data-arg="BUY">Buy</button><button id="seg-sell" data-action="set-trade-action" data-arg="SELL">Sell</button></div>
        <div><label>Ticker</label><input id="trade-ticker" placeholder="e.g. AAPL" list="hold-list" autocomplete="off" /><datalist id="hold-list">${holdingOptions}</datalist></div>
        <div><label>Amount (USD)</label><input id="trade-amount" type="number" min="0.01" step="0.01" placeholder="500" /></div>
        <div id="trade-context" class="decision-msg">Review an instrument before placing an order.</div>
        <button class="submit-btn" id="trade-submit" data-action="review-trade" data-arg="${escapeHtml(detail.username)}">Review order</button>
        <div id="trade-msg"></div><div class="detail-meta">Estimated values are non-binding. The execution engine enforces all guardrails on a fresh quote.</div>
      </div>`);
    setAction(action);
  };

  const review = async (username) => {
    const ticker = (element('trade-ticker').value || '').trim().toUpperCase();
    const amount = Number(element('trade-amount').value);
    const submit = element('trade-submit');
    if (!ticker || !Number.isFinite(amount) || amount <= 0) {
      message('Enter a ticker and a positive USD amount.', true);
      return;
    }
    submit.disabled = true;
    element('trade-context').textContent = 'Fetching current instrument and portfolio estimate…';
    try {
      const preview = await requestJson('/api/trade/preview', {
        method: 'POST',
        body: { username, ticker, action, amount_dollars: amount },
      });
      pendingOrder = { username, ticker, amount, action, clientOrderId: crypto.randomUUID(), preview };
      const warnings = (preview.warnings || []).map((warning) => `<li>${escapeHtml(warning.message)}</li>`).join('');
      element('trade-context').textContent = `${preview.instrument.company} · ${formatMoney(preview.quote.price)} · estimated ${formatQuantity(preview.estimated_quantity)} shares`;
      element('trade-confirm-title').textContent = `Review simulated ${action.toLowerCase()}`;
      renderHtml(element('trade-confirm-body'), `<strong>${escapeHtml(preview.action)} ${escapeHtml(preview.instrument.ticker)} (${escapeHtml(preview.instrument.company)})</strong><div>Requested: ${formatMoney(preview.requested_amount)} · Estimated fill: ${formatMoney(preview.estimated_executable_amount)}</div><div>Estimated ${formatQuantity(preview.estimated_quantity)} shares @ ${formatMoney(preview.quote.price)}</div><div>Fee: ${formatMoney(preview.fee)} · Cash after: ${formatMoney(preview.estimated_cash_after)}</div><div>Holding after: ${formatQuantity(preview.estimated_holding_quantity)} shares (${(preview.estimated_holding_weight * 100).toFixed(1)}%)</div>${warnings ? `<ul>${warnings}</ul>` : ''}`);
      element('trade-confirm-submit').textContent = `Confirm simulated ${action.toLowerCase()}`;
      returnFocus = submit;
      element('trade-confirm-overlay').classList.add('open');
      element('trade-confirm-modal').classList.add('open');
      element('trade-confirm-submit').focus();
    } catch (error) {
      element('trade-context').textContent = '';
      message(error.message, true);
    } finally {
      submit.disabled = false;
    }
  };

  const confirm = async () => {
    if (!pendingOrder || executionAbort) return;
    const order = pendingOrder;
    const submit = element('trade-confirm-submit');
    submit.disabled = true;
    let executionAccepted = false;
    executionAbort = new AbortController();
    try {
      const result = await requestJson('/api/trade', {
        method: 'POST',
        body: {
          username: order.username,
          ticker: order.ticker,
          action: order.action,
          amount_dollars: order.amount,
          client_order_id: order.clientOrderId,
        },
        signal: executionAbort.signal,
      });
      if (!result.ok) {
        close();
        message(`${result.error || 'Trade rejected.'} Correct the order and review again.`, true);
        return;
      }
      executionAccepted = true;
      executionAbort = null;
      close();
      const transaction = result.transaction;
      message(`${transaction.action} filled: ${formatQuantity(transaction.quantity)} ${transaction.ticker} @ ${formatMoney(transaction.price)} = ${formatMoney(transaction.total)}; fee ${formatMoney(transaction.fee)}.`, false);
      element('trade-amount').value = '';
      await onFilled(order);
    } catch (error) {
      if (executionAccepted) {
        message(`Trade filled, but the dashboard could not refresh: ${error.message}`, true);
      } else if (error instanceof requestErrorType) {
        close();
        message(`${error.message} Correct the order and review again.`, true);
      } else if (error.name !== 'AbortError') {
        submit.textContent = `Retry simulated ${order.action.toLowerCase()}`;
        element('trade-confirm-body').insertAdjacentText('beforeend', `\nConnection failed: ${error.message}. Retry this confirmation; it uses the same order ID.`);
      }
    } finally {
      submit.disabled = false;
      executionAbort = null;
    }
  };

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && element('trade-confirm-modal').classList.contains('open')) close();
  });

  return {
    get action() {
      return action;
    },
    render,
    setAction,
    review,
    confirm,
    close,
  };
};
