export const createInstruments = ({
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
  getCurrentDetail,
  resolveInstrument,
}) => {
  const MARKET_PAGE_SIZE = 50;
  let instrumentFilter = '';
  let loadedFilter = null;
  let loadedInstruments = 0;
  let instrumentTotal = 0;
  let hasMoreInstruments = false;
  let catalogueInFlight = null;
  let catalogueRequest = 0;

  function marketElements() {
    return {
      list: $('popular-list'),
      status: $('market-catalogue-status'),
      loadMore: $('market-load-more'),
    };
  }

  function renderMarketRows(instruments) {
    return instruments.map(instrument => {
      const change = instrument.change_percent || 0;
      const volume = instrument.volume
        ? (instrument.volume >= 1e6 ? `${(instrument.volume / 1e6).toFixed(1)}M` : `${(instrument.volume / 1e3).toFixed(0)}K`)
        : '';
      const ticker = escapeHtml(instrument.ticker);
      const category = escapeHtml(instrument.category || (instrument.sector !== 'Unknown' ? instrument.sector || '' : ''));
      const metadata = [category, volume && `Vol ${volume}`].filter(Boolean).join(' · ');
      const quote = instrument.price ? `<div class="p">${fmt$(instrument.price)}</div><div class="c ${cls(change)}">${fmtPct(change)}</div>` : '<div class="p muted-text">—</div><div class="c muted-text">Quote unavailable</div>';
      return `<div class="pop-row" data-action="open-drawer-ticker" data-arg="${ticker}">
        <div><div class="pop-t">${ticker}${instrument.instrument_type === 'etf' ? '<span class="badge etf">ETF</span>' : ''}</div><div class="pop-vol">${metadata}</div></div>
        <div class="pop-px">${quote}</div>
      </div>`;
    }).join('');
  }

  function updateMarketControls() {
    const { status, loadMore } = marketElements();
    if (loadedInstruments) status.textContent = `Showing ${loadedInstruments} of ${instrumentTotal} active instruments.`;
    loadMore.hidden = !hasMoreInstruments;
    loadMore.disabled = Boolean(catalogueInFlight);
    loadMore.textContent = 'Load more instruments';
  }

  function resetMarketCatalogue() {
    catalogueRequest++;
    loadedFilter = null;
    loadedInstruments = 0;
    instrumentTotal = 0;
    hasMoreInstruments = false;
    const { list, status, loadMore } = marketElements();
    renderHtml(list, '<div class="loading">Loading instruments…</div>');
    status.textContent = '';
    loadMore.hidden = true;
  }

  async function loadNextMarketPage({ reset = false } = {}) {
    if (catalogueInFlight && !reset) return catalogueInFlight;
    if (!reset && !hasMoreInstruments && loadedInstruments) return;
    if (reset) resetMarketCatalogue();

    const filter = instrumentFilter;
    const request = catalogueRequest;
    const offset = loadedInstruments;
    const { list, status, loadMore } = marketElements();
    loadMore.disabled = true;
    if (offset) status.textContent = 'Loading more instruments…';

    let load;
    load = (async () => {
      try {
        const params = new URLSearchParams({ limit: String(MARKET_PAGE_SIZE), offset: String(offset) });
        if (filter) params.set('instrument_type', filter);
        const page = await requestJson(`/api/watchlist?${params}`);
        if (request !== catalogueRequest || filter !== instrumentFilter) return;

        const instruments = Array.isArray(page) ? page : [];
        if (!offset) list.replaceChildren();
        if (!instruments.length) {
          if (!offset) {
            renderHtml(list, '<div class="loading">No active instruments.</div>');
            status.textContent = '';
            return;
          }
          instrumentTotal = loadedInstruments;
          hasMoreInstruments = false;
          updateMarketControls();
          return;
        }
        list.insertAdjacentHTML('beforeend', renderMarketRows(instruments));
        loadedInstruments += instruments.length;
        instrumentTotal = Number(instruments[0]?.total ?? loadedInstruments);
        hasMoreInstruments = instruments.length > 0 && loadedInstruments < instrumentTotal;
        loadedFilter = filter;
        updateMarketControls();
      } catch (error) {
        if (request !== catalogueRequest || filter !== instrumentFilter) return;
        console.error('market catalogue failed', error);
        if (!offset) renderHtml(list, '<div class="loading">Instruments are unavailable.</div>');
        status.textContent = 'Couldn’t load instruments. Try again.';
        loadMore.hidden = false;
        loadMore.textContent = 'Retry';
        loadMore.disabled = false;
      } finally {
        if (catalogueInFlight === load) catalogueInFlight = null;
        if (request === catalogueRequest && filter === instrumentFilter && hasMoreInstruments) updateMarketControls();
      }
    })();
    catalogueInFlight = load;
    return load;
  }

  function setInstrumentFilter(filter) {
    instrumentFilter = filter;
    document.querySelectorAll('[data-instrument-filter]').forEach(button => button.classList.toggle('active', button.dataset.instrumentFilter === filter));
    loadNextMarketPage({ reset: true });
  }

  function loadMarketCatalogue({ force = false } = {}) {
    if (!force && loadedFilter === instrumentFilter) return Promise.resolve();
    return loadNextMarketPage({ reset: true });
  }

  const marketSentinel = $('market-load-sentinel');
  if ('IntersectionObserver' in window) {
    new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) loadNextMarketPage();
    }, { rootMargin: '240px' }).observe(marketSentinel);
  }
  $('market-load-more').addEventListener('click', () => loadNextMarketPage());

  let suggestionTimer = null;
  let suggestionRequest = null;
  let suggestionSequence = 0;
  let suggestions = [];
  let highlightedSuggestion = -1;

  function clearSuggestions() {
    suggestionSequence++;
    clearTimeout(suggestionTimer);
    suggestionRequest?.abort();
    suggestionRequest = null;
    suggestions = [];
    highlightedSuggestion = -1;
    const list = $('instrument-suggestions');
    list.replaceChildren();
    list.hidden = true;
    $('stock-search-input').setAttribute('aria-expanded', 'false');
    $('stock-search-input').removeAttribute('aria-activedescendant');
  }

  function renderSuggestions(status) {
    const list = $('instrument-suggestions');
    list.replaceChildren();
    if (status) {
      const item = document.createElement('li');
      item.className = 'instrument-suggestion-status';
      item.textContent = status;
      list.append(item);
    } else {
      suggestions.forEach((suggestion, index) => {
        const item = document.createElement('li');
        const option = document.createElement('button');
        option.type = 'button';
        option.id = `instrument-suggestion-${index}`;
        option.className = `instrument-suggestion${index === highlightedSuggestion ? ' active' : ''}`;
        option.setAttribute('role', 'option');
        option.setAttribute('aria-selected', String(index === highlightedSuggestion));
        const ticker = document.createElement('span');
        ticker.className = 'instrument-suggestion-ticker';
        ticker.textContent = suggestion.ticker;
        const company = document.createElement('span');
        company.className = 'instrument-suggestion-company';
        company.textContent = ` · ${suggestion.company_name || suggestion.ticker}`;
        option.append(ticker, company);
        const metadata = [suggestion.instrument_type === 'etf' ? 'ETF' : 'Equity', suggestion.exchange || suggestion.category].filter(Boolean).join(' · ');
        if (metadata) {
          const meta = document.createElement('span');
          meta.className = 'instrument-suggestion-meta';
          meta.textContent = metadata;
          option.append(meta);
        }
        option.addEventListener('mousedown', event => event.preventDefault());
        option.addEventListener('click', () => selectSuggestion(index));
        item.append(option);
        list.append(item);
      });
    }
    list.hidden = false;
    $('stock-search-input').setAttribute('aria-expanded', 'true');
    if (highlightedSuggestion >= 0) $('stock-search-input').setAttribute('aria-activedescendant', `instrument-suggestion-${highlightedSuggestion}`);
    else $('stock-search-input').removeAttribute('aria-activedescendant');
  }

  function selectSuggestion(index) {
    const suggestion = suggestions[index];
    if (!suggestion) return;
    $('stock-search-input').value = '';
    clearSuggestions();
    resolveInstrument(suggestion.ticker);
  }

  function searchStock() {
    if (highlightedSuggestion >= 0) return selectSuggestion(highlightedSuggestion);
    const input = $('stock-search-input');
    const ticker = (input.value || '').trim().toUpperCase();
    if (!ticker) return;
    input.value = '';
    clearSuggestions();
    resolveInstrument(ticker);
  }

  function requestSuggestions() {
    const query = $('stock-search-input').value.trim();
    if (query.length < 2) return clearSuggestions();
    const requestId = ++suggestionSequence;
    suggestionRequest?.abort();
    suggestionRequest = new AbortController();
    renderSuggestions('Loading…');
    requestJson(`/api/instrument-suggestions?${new URLSearchParams({query})}`, {signal: suggestionRequest.signal})
      .then(data => {
        if (requestId !== suggestionSequence) return;
        suggestions = data.suggestions || [];
        highlightedSuggestion = -1;
        if (suggestions.length) renderSuggestions();
        else renderSuggestions('No matching active instruments.');
      })
      .catch(error => {
        if (error.name !== 'AbortError' && requestId === suggestionSequence) clearSuggestions();
      });
  }

  $('stock-search-input').addEventListener('input', () => {
    clearTimeout(suggestionTimer);
    suggestionTimer = setTimeout(requestSuggestions, 250);
  });
  $('stock-search-input').addEventListener('keydown', event => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      if (!suggestions.length) return;
      event.preventDefault();
      highlightedSuggestion = (highlightedSuggestion + (event.key === 'ArrowDown' ? 1 : suggestions.length - 1)) % suggestions.length;
      renderSuggestions();
    } else if (event.key === 'Enter') {
      event.preventDefault();
      searchStock();
    } else if (event.key === 'Escape') {
      clearSuggestions();
    }
  });
  document.addEventListener('click', event => {
    if (!event.target.closest('.stock-search')) clearSuggestions();
  });

  const STOCK_RANGES = ['1D', '1W', '1M', '3M', '6M', '1Y'];
  let stockChart = null;
  let selectedStockRange = '1M';
  let stockChartRequest = 0;

  function renderStockChart(ohlcv) {
    const canvas = $('stockChart');
    if (!canvas || !ohlcv?.length) return;
    const labels = ohlcv.map(o => new Date(o.date).toLocaleString([], {
      month: 'short', day: 'numeric', ...(selectedStockRange === '1D' ? { hour: 'numeric', minute: '2-digit' } : {})
    }));
    const closes = ohlcv.map(o => o.close);
    const up = closes[closes.length - 1] >= closes[0];
    stockChart = replaceChart(canvas, {
      type: 'line',
      data: { labels, datasets: [{ data: closes, borderColor: up ? '#1a7f37' : '#cf222e', backgroundColor: 'transparent', tension: .25, pointRadius: 0, borderWidth: 2 }] },
      options: {
        plugins: { legend: { display: false }, tooltip: { callbacks: {
          title: items => formatChartTimestamp(ohlcv[items[0].dataIndex].date)
        } } },
        scales: {
          x: { ticks: { color: '#656d76', maxTicksLimit: 6 }, grid: { color: '#d0d7de' } },
          y: { ticks: { color: '#656d76', callback: v => '$' + Number(v).toFixed(0) }, grid: { color: '#d0d7de' } }
        }
      }
    });
  }

  async function selectStockRange(range) {
    const ticker = $('s-name').textContent;
    if (!ticker || ticker === 'Loading…') return;
    selectedStockRange = range;
    document.querySelectorAll('[data-stock-range]').forEach(button => button.classList.toggle('active', button.dataset.stockRange === range));
    $('stock-chart-title').textContent = `Price (${range})`;
    const request = ++stockChartRequest;
    try {
      const data = await requestJson(`/api/stock/${encodeURIComponent(ticker)}?chart_range=${range}`);
      if (request !== stockChartRequest) return;
      const canvas = $('stockChart');
      if (!canvas) return;
      $('stock-chart-empty')?.remove();
      destroyChart('stockChart');
      if (data.ohlcv?.length) renderStockChart(data.ohlcv);
      else {
        const empty = document.createElement('div');
        empty.className = 'loading';
        empty.id = 'stock-chart-empty';
        empty.textContent = 'No price history for this range.';
        canvas.after(empty);
      }
    } catch (error) {
      if (request === stockChartRequest) console.error('stock chart range failed:', error);
    }
  }

  async function openDrawerTicker(ticker) {
    const symbol = String(ticker ?? '').trim();
    if (!symbol) return;
    $('stock-overlay').classList.add('open');
    $('stock-drawer').classList.add('open');
    $('s-name').textContent = symbol;
    $('s-sub').textContent = 'Loading…';
    renderHtml($('stock-body'), '<div class="loading">Loading…</div>');
    try {
      const d = await requestJson(`/api/stock/${encodeURIComponent(symbol)}`);
      if (!d.price) {
        $('s-sub').textContent = 'No data';
        renderHtml($('stock-body'), `<div class="loading">No market data for "${escapeHtml(ticker)}". Check the ticker symbol.</div>`);
        return;
      }
      const ch = d.change_percent || 0;
      const company = (d.company && d.company !== d.ticker) ? d.company : '';
      const sector = (d.sector && d.sector !== 'Unknown') ? d.sector : '';
      const subBits = [company, d.instrument_type === 'etf' ? 'ETF' : '', d.category, d.issuer, sector].filter(Boolean).join(' · ');
      $('s-name').textContent = d.ticker;
      renderHtml($('s-sub'), `${subBits ? escapeHtml(subBits) + ' · ' : ''}<strong>${fmt$(d.price)}</strong> <span class="${cls(ch)}">${fmtPct(ch)}</span>`);

      const holders = (d.holders || []).map(h => `<tr>
        <td>${escapeHtml(h.display_name || h.username)}${badgeFor(h.user_type, h.decision_architecture)}</td>
        <td class="num">${fmtQty(h.quantity)}</td>
        <td class="num">${fmt$(h.avg_cost)}</td>
        <td class="num ${cls(h.pnl_percent)}">${fmtPct(h.pnl_percent)}</td>
      </tr>`).join('');

      const trades = (d.recent_trades || []).map(t => `<div class="detail-row">
        <span class="txn-type ${transactionClass(t.transaction_type)}">${escapeHtml(t.transaction_type)}</span>
        <strong>${escapeHtml(t.username)}</strong>
        <span class="detail-meta">${fmtQty(t.quantity)} @ ${fmt$(t.price_per_share)}</span>
        <span class="detail-date">${t.executed_at ? new Date(t.executed_at).toLocaleDateString() : ''}</span>
      </div>`).join('');

      const news = (d.news || []).map(n => `<div class="detail-block">
        <div class="detail-title">${escapeHtml(n.title)}</div>
        <div class="detail-meta detail-meta-spaced">${escapeHtml(n.publisher)}${n.published_at ? ' · ' + new Date(n.published_at).toLocaleDateString() : ''}</div>
      </div>`).join('');

      selectedStockRange = STOCK_RANGES.includes(d.chart_range) ? d.chart_range : '1M';
      renderHtml($('stock-body'), `
        <div class="chart-header"><div class="section-title" id="stock-chart-title">Price (${selectedStockRange})</div><div class="chart-range" aria-label="Price chart range">${STOCK_RANGES.map(range => `<button type="button" data-stock-range="${range}" class="${range === selectedStockRange ? 'active' : ''}" data-action="select-stock-range" data-arg="${range}">${range}</button>`).join('')}</div></div>
        <canvas id="stockChart" height="200"></canvas>${(d.ohlcv && d.ohlcv.length) ? '' : '<div class="loading" id="stock-chart-empty">No price history for this range.</div>'}
        ${getCurrentDetail()?.user_type === 'human' ? `<button class="submit-btn" data-action="trade-instrument" data-arg="${escapeHtml(d.ticker)}">Trade this instrument</button>` : ''}
        <div class="section-title">Holders</div>
        ${holders ? `<table class="mini-table"><thead><tr><th>Player</th><th class="num">Qty</th><th class="num">Avg</th><th class="num">P&L</th></tr></thead><tbody>${holders}</tbody></table>` : '<div class="loading">No holders yet.</div>'}
        <div class="section-title">Recent trades</div>
        ${trades || '<div class="loading">No trades in this ticker.</div>'}
        <div class="section-title">News</div>
        ${news || '<div class="loading">No recent news.</div>'}`);

      if (d.ohlcv && d.ohlcv.length) {
        try { renderStockChart(d.ohlcv); } catch (error) { console.error('stock chart failed', error); }
      }
    } catch (e) {
      console.error('openDrawerTicker failed:', e);
      $('s-sub').textContent = '';
      renderHtml($('stock-body'), `<div class="loading">Failed to load: ${escapeHtml(e.message)}</div>`);
    }
  }

  function closeStockDrawer() {
    $('stock-overlay').classList.remove('open');
    $('stock-drawer').classList.remove('open');
  }

  return { setInstrumentFilter, loadMarketCatalogue, searchStock, selectStockRange, openDrawerTicker, closeStockDrawer };
};
