const decisionDate = (value, timezone) => value
  ? new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: timezone,
  }).format(new Date(value))
  : 'Never';

const STYLE_PRESETS = {
  aggressive: {
    gain: 10, loss: -5, maxpos: 6, maxalloc: 25, minmove: 2, maxvol: 12, cash: 2, dips: 'false',
    persona: 'Aggressive momentum trader — chases volatility, news and FOMO plays with large positions.',
  },
  balanced: {
    gain: 12, loss: -8, maxpos: 7, maxalloc: 15, minmove: 1.5, maxvol: 8, cash: 5, dips: 'false',
    persona: 'Balanced investor — moderate risk, mixes momentum and value.',
  },
  value: {
    gain: 10, loss: -8, maxpos: 7, maxalloc: 10, minmove: 1, maxvol: 8, cash: 8, dips: 'true',
    persona: 'Conservative value investor — buys quality blue-chips on dips, avoids volatility.',
  },
};

/**
 * Owns operator actions: the automation panel's scheduled market/history/news refresh,
 * the AI-agent creation modal, and the instrument-catalogue management modal.
 *
 * @param {{
 *   requestJson: (path: string, options?: object) => Promise<unknown>,
 *   element: (id: string) => HTMLElement,
 *   loadMarketCatalogue: (options?: {force?: boolean}) => Promise<void> | void,
 *   loadLeaderboard: () => Promise<void> | void,
 * }} dependencies
 */
export const createOperations = ({ requestJson, element, loadMarketCatalogue, loadLeaderboard }) => {
  const renderHistoryStatus = (status) => {
    const summary = element('data-health-summary');
    if (!summary) return;
    const history = status.history;
    const refreshing = status.history_in_progress;
    const state = history?.status;
    const unavailable = history ? Math.max(0, history.total - history.ready) : null;
    summary.classList.toggle('warning', state !== 'healthy' && state !== 'failed');
    summary.classList.toggle('error', state === 'failed');
    summary.classList.toggle('running', Boolean(status.in_progress));
    summary.textContent = state === 'failed'
      ? `Historical data check failed: ${history.error || 'Unknown error'}. Do not assume the data is healthy.`
      : history
        ? `${state === 'healthy' ? 'Historical data ready' : 'Data quality warning'} — ${history.ready}/${history.total} instruments have the required technical history.${unavailable ? ` ${unavailable} unavailable.` : ''}${history.error ? ` ${history.error}` : ''}${refreshing ? ' Rechecking…' : ''}`
        : refreshing ? 'Checking and refreshing historical data…' : 'Historical data has not been checked yet.';
    element('data-health-times').textContent = `Last checked: ${decisionDate(history?.checked_at)}${history?.required_session ? ` · Required session: ${history.required_session}` : ''}`;
    const issues = history?.issues || [];
    element('data-health-issues').hidden = !issues.length;
    const list = element('data-health-issue-list');
    list.replaceChildren(...issues.map(issue => {
      const row = document.createElement('li');
      row.textContent = `${issue.ticker}: ${issue.reason}; last daily bar ${issue.last_session || 'none'}${issue.missing_sessions?.length ? `; missing sessions: ${issue.missing_sessions.join(', ')}` : ''}`;
      return row;
    }));
  };

  const renderFunnelStatus = (status) => {
    renderHistoryStatus(status);
    const btn = element('funnel-refresh-btn');
    const msg = element('funnel-refresh-msg');
    const times = element('funnel-refresh-times');
    if (!msg || !times) return;
    if (btn) {
      btn.disabled = status.in_progress;
      btn.textContent = status.in_progress ? 'Refresh running…' : 'Refresh now';
    }
    const failed = !status.in_progress && status.last_result?.error;
    msg.textContent = status.in_progress
      ? 'Refresh running…'
      : failed ? 'Last refresh failed'
        : status.last_run ? 'Refresh complete' : 'Not run yet';
    const runLabel = status.in_progress ? 'Started' : 'Last run';
    times.textContent = `${runLabel}: ${decisionDate(status.last_run)}${status.next_run ? ` · Next scheduled: ${decisionDate(status.next_run)}` : ''}${failed ? ` · ${failed}` : ''}`;
  };

  const loadFunnelStatus = async () => {
    try {
      const status = await requestJson('/api/cycle/status');
      renderFunnelStatus(status);
      return status;
    } catch (error) {
      renderHistoryStatus({history: {status: 'failed', error: `Status unavailable: ${error.message}`}});
      return null;
    }
  };

  const renderFilingWarmupStatus = (status) => {
    const btn = element('filing-warmup-btn');
    const msg = element('filing-warmup-msg');
    const times = element('filing-warmup-times');
    if (!msg || !times) return;
    if (btn) btn.disabled = status.running;
    const failed = !status.running && status.last_result?.error;
    msg.textContent = status.running
      ? 'Warmup running…'
      : failed ? 'Last warmup failed' : status.last_run ? 'Warmup complete' : 'Not run yet';
    const counts = status.last_result?.counts;
    const detail = counts ? ` · ${counts.new_documents} new, ${counts.cached} cached of ${status.last_result.tickers_processed}` : '';
    times.textContent = `${status.running ? 'Started' : 'Last run'}: ${decisionDate(status.last_run)}${detail}${failed ? ` · ${status.last_result.error}` : ''}`;
  };

  const loadFilingWarmupStatus = async () => {
    try {
      renderFilingWarmupStatus(await requestJson('/api/filing-briefs/status'));
    } catch {}
  };

  const triggerFilingWarmup = async () => {
    const btn = element('filing-warmup-btn');
    btn.disabled = true;
    try {
      await requestJson('/api/filing-briefs/refresh', { method: 'POST' });
      await loadFilingWarmupStatus();
    } catch (error) {
      element('filing-warmup-msg').textContent = `Failed: ${error.message}`;
      btn.disabled = false;
    }
  };

  const triggerManualRefresh = async () => {
    const btn = element('funnel-refresh-btn');
    btn.disabled = true;
    btn.textContent = 'Starting refresh…';
    const summary = element('data-health-summary');
    if (summary) {
      summary.textContent = 'Starting refresh…';
      summary.classList.add('running');
    }
    try {
      const result = await requestJson('/api/cycle', { method: 'POST' });
      await loadFunnelStatus();
      if (result?.ok === false && summary) {
        summary.textContent = `${summary.textContent} Refresh already in progress.`;
      }
    } catch (error) {
      element('funnel-refresh-msg').textContent = `Failed: ${error.message}`;
      renderHistoryStatus({history: {status: 'failed', error: `Refresh could not start: ${error.message}`}});
      btn.disabled = false;
      btn.textContent = 'Refresh now';
    }
  };

  const checkFunnelAfterResume = async () => {
    try {
      renderFunnelStatus((await requestJson('/api/cycle/check', { method: 'POST' })).scheduler);
    } catch {
      await loadFunnelStatus();
    }
  };

  const applyStylePreset = () => {
    const p = STYLE_PRESETS[element('ag-style').value];
    element('ag-gain').value = p.gain;
    element('ag-loss').value = p.loss;
    element('ag-maxpos').value = p.maxpos;
    element('ag-maxalloc').value = p.maxalloc;
    element('ag-minmove').value = p.minmove;
    element('ag-maxvol').value = p.maxvol;
    element('ag-cash').value = p.cash;
    element('ag-dips').value = p.dips;
    element('ag-persona').value = p.persona;
  };

  const openAgentModal = () => {
    element('agent-overlay').classList.add('open');
    element('agent-modal').classList.add('open');
    element('ag-msg').textContent = '';
    applyStylePreset();
  };
  const closeAgentModal = () => {
    element('agent-overlay').classList.remove('open');
    element('agent-modal').classList.remove('open');
  };

  const submitAgent = async () => {
    const btn = element('ag-submit');
    const msg = element('ag-msg');
    const username = element('ag-username').value.trim();
    if (!username) { msg.textContent = 'Username required.'; return; }
    const body = {
      username,
      style: element('ag-style').value,
      persona: element('ag-persona').value.trim(),
      summary: element('ag-persona').value.trim(),
      config: {
        sell_gain_pct: +element('ag-gain').value,
        sell_loss_pct: +element('ag-loss').value,
        max_positions: +element('ag-maxpos').value,
        max_allocation: (+element('ag-maxalloc').value) / 100,
        min_move_pct: +element('ag-minmove').value,
        max_volatility_pct: +element('ag-maxvol').value,
        cash_reserve_pct: +element('ag-cash').value,
        prefer_dips: element('ag-dips').value === 'true',
      },
    };
    btn.disabled = true;
    msg.textContent = 'Creating…';
    try {
      const data = await requestJson('/api/agents', { method: 'POST', body });
      if (!data.ok) { msg.textContent = 'Failed: ' + (data.error || 'unknown error'); return; }
      msg.textContent = `Created ${data.agent.username}. Include it in the next manual decision batch.`;
      loadLeaderboard();
      setTimeout(closeAgentModal, 1200);
    } catch (e) {
      msg.textContent = 'Failed: ' + e.message;
    } finally {
      btn.disabled = false;
    }
  };

  const openInstrumentModal = () => {
    element('instrument-overlay').classList.add('open');
    element('instrument-modal').classList.add('open');
    element('ins-msg').textContent = '';
  };
  const closeInstrumentModal = () => {
    element('instrument-overlay').classList.remove('open');
    element('instrument-modal').classList.remove('open');
  };

  const submitInstrument = async () => {
    const ticker = element('ins-ticker').value.trim();
    const msg = element('ins-msg');
    if (!ticker) { msg.textContent = 'Ticker is required.'; return; }
    element('ins-submit').disabled = true;
    msg.textContent = 'Validating…';
    try {
      const result = await requestJson('/api/instruments', {
        method: 'POST',
        body: { ticker, instrument_type: element('ins-type').value, category: element('ins-category').value || null },
      });
      msg.textContent = `${result.instrument.ticker} is active and eligible for the next AI cycle.`;
      element('ins-ticker').value = '';
      element('ins-category').value = '';
      loadMarketCatalogue({ force: true });
    } catch (error) {
      msg.textContent = error.message;
    } finally {
      element('ins-submit').disabled = false;
    }
  };

  const importEtfs = async () => {
    const msg = element('ins-msg');
    if (!confirm('Import or refresh the curated ETF catalogue? Existing operator metadata is preserved.')) return;
    msg.textContent = 'Importing…';
    try {
      const result = await requestJson('/api/instruments/import-etfs', { method: 'POST' });
      msg.textContent = `${result.imported} of ${result.count} catalogue ETFs imported.`;
      loadMarketCatalogue({ force: true });
    } catch (error) {
      msg.textContent = error.message;
    }
  };

  return {
    renderFunnelStatus,
    loadFunnelStatus,
    triggerManualRefresh,
    renderFilingWarmupStatus,
    triggerFilingWarmup,
    checkFunnelAfterResume,
    applyStylePreset,
    openAgentModal,
    closeAgentModal,
    submitAgent,
    openInstrumentModal,
    closeInstrumentModal,
    submitInstrument,
    importEtfs,
    start() {
      let timer;
      const poll = async () => {
        const status = await loadFunnelStatus();
        loadFilingWarmupStatus();
        timer = setTimeout(poll, status?.in_progress ? 4_000 : 30_000);
      };
      poll();
      return () => clearTimeout(timer);
    },
  };
};
