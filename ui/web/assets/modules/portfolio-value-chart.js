const CHART_COLORS = ['#0969da', '#1a7f37', '#9a6700', '#cf222e', '#8250df', '#0550ae', '#116329', '#bc4c00', '#a40e26', '#953800'];
const DAY = 86_400_000;

const numeric = (value) => Number.isFinite(Number(value)) ? Number(value) : null;
const timestamp = (value) => {
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
};

const colorFor = (identity) => {
  let hash = 0;
  for (const character of String(identity)) hash = ((hash << 5) - hash) + character.charCodeAt(0) | 0;
  return CHART_COLORS[Math.abs(hash) % CHART_COLORS.length];
};

const comparePlayers = (left, right) =>
  left.rank - right.rank || left.label.localeCompare(right.label) || left.id.localeCompare(right.id);

const signedMoney = (value, formatMoney) => {
  if (!Number.isFinite(value)) return null;
  return `${value >= 0 ? '+' : '-'}${formatMoney(Math.abs(value))}`;
};

const signedPercent = (value) => Number.isFinite(value) ? `${value >= 0 ? '+' : ''}${value.toFixed(2)}%` : null;

const normalizeHistory = (history) => new Map(Object.entries(history || {}).map(([id, entries]) => {
  const snapshots = new Map();
  for (const entry of Array.isArray(entries) ? entries : []) {
    const at = timestamp(entry?.time);
    const value = numeric(entry?.value);
    if (at === null || value === null) continue;
    snapshots.set(at, {
      time: at,
      value,
      pnl: numeric(entry?.pnl),
      returnPercent: numeric(entry?.pnl_percent),
    });
  }
  return [String(id), [...snapshots.values()].sort((left, right) => left.time - right.time)];
}));

export const createPortfolioValueChart = ({ canvas, controls, formatTimestamp, formatMoney }) => {
  let chart = null;
  let selectedPlayerId = '';
  let selectedRange = 'ALL';
  let retryAction = null;
  let model = { players: [], timestamps: [], latestTimestamp: null };

  const playerControl = controls.player;
  const summary = controls.summary;
  const rangeControls = controls.ranges;
  const resetControl = controls.reset;
  const description = controls.description;
  const hint = controls.hint;
  const status = controls.status;
  const retryControl = controls.retry;
  const announcer = controls.announcer;

  const selectedPlayer = () => model.players.find((player) => player.id === selectedPlayerId) || null;

  const rangeBounds = (range = selectedRange) => {
    const first = model.timestamps[0];
    const latest = model.latestTimestamp;
    if (!Number.isFinite(first) || !Number.isFinite(latest)) return null;
    const requestedStart = range === '7D' ? latest - 7 * DAY : range === '30D' ? latest - 30 * DAY : first;
    return { min: Math.max(first, requestedStart), max: latest };
  };

  const rangeLabel = (range = selectedRange) => ({
    '7D': 'the last 7 days',
    '30D': 'the last 30 days',
    ALL: 'all available history',
  })[range] || 'all available history';

  const currentRange = () => chart && { min: chart.scales.x.min, max: chart.scales.x.max };

  const rangeMatches = (left, right) => left && right
    && Math.abs(left.min - right.min) < 1
    && Math.abs(left.max - right.max) < 1;

  const hasAdditionalNavigation = () => !rangeMatches(currentRange(), rangeBounds());

  const updateResetControl = () => {
    resetControl.disabled = !chart || !hasAdditionalNavigation();
  };

  const updateControls = (usable) => {
    playerControl.disabled = !usable;
    for (const button of rangeControls) button.disabled = !usable;
    updateResetControl();
  };

  const updateRangeControls = () => {
    for (const button of rangeControls) {
      const active = button.dataset.lbChartRange === selectedRange;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    }
  };

  const updateStatus = (message, state = '') => {
    status.textContent = message;
    if (state) status.dataset.state = state;
    else delete status.dataset.state;
    canvas.setAttribute('aria-busy', String(state === 'loading'));
  };

  const clearRetry = () => {
    retryAction = null;
    retryControl.hidden = true;
    retryControl.disabled = true;
  };

  const announce = (message) => {
    announcer.textContent = message;
  };

  const setUnavailableDescription = (label, text) => {
    summary.hidden = true;
    summary.textContent = '';
    canvas.setAttribute('aria-label', label);
    description.textContent = text;
  };

  const clearChart = () => {
    if (!chart) return;
    chart.destroy();
    chart = null;
  };

  const setLoading = () => {
    const hasChart = Boolean(chart);
    clearRetry();
    updateStatus(hasChart ? 'Refreshing portfolio history…' : 'Loading portfolio history…', 'loading');
    if (!hasChart) {
      updateControls(false);
      setUnavailableDescription('Portfolio value chart loading', 'Portfolio value history is loading.');
    }
  };

  const showEmpty = () => {
    clearChart();
    clearRetry();
    updateControls(false);
    setUnavailableDescription(
      'Portfolio value chart unavailable because no history exists',
      'No portfolio history is available yet.',
    );
    updateStatus('No portfolio history is available yet. Check back after the first valuation.', 'empty');
  };

  const showError = (retry) => {
    retryAction = retry;
    retryControl.hidden = false;
    retryControl.disabled = false;
    updateStatus('Couldn’t load portfolio history. Please try again.', 'error');
    if (!chart) {
      updateControls(false);
      setUnavailableDescription(
        'Portfolio value chart unavailable because history could not be loaded',
        'Portfolio value history could not be loaded. Use Retry to try again.',
      );
    }
  };

  const setReady = () => {
    clearRetry();
    updateStatus('');
  };

  const visibleDatasets = () => chart.data.datasets.filter((_, index) => chart.isDatasetVisible(index));

  const fitYAxis = () => {
    if (!chart) return;
    const { min, max } = chart.scales.x;
    const values = visibleDatasets().flatMap((dataset) => dataset.data)
      .filter((point) => Number.isFinite(point?.x) && Number.isFinite(point?.y) && point.x >= min && point.x <= max)
      .map((point) => point.y);
    if (!values.length) return;
    const low = Math.min(...values);
    const high = Math.max(...values);
    const padding = Math.max((high - low) * .05, 1);
    Object.assign(chart.options.scales.y, { min: low - padding, max: high + padding });
  };

  const syncNavigation = () => {
    if (!chart) return;
    fitYAxis();
    chart.update('none');
    updateResetControl();
  };

  const updateSummary = () => {
    const player = selectedPlayer();
    if (!player) {
      summary.hidden = true;
      summary.textContent = '';
      canvas.setAttribute('aria-label', 'Portfolio value comparison chart for all players');
      description.textContent = 'Compare portfolio values over time. Choose a player to focus on one portfolio.';
      return;
    }
    const current = player.frames.findLast((frame) => Number.isFinite(frame.y));
    const parts = [current ? formatMoney(current.y) : 'No valuation'];
    if (Number.isFinite(player.returnPercent)) parts.push(signedPercent(player.returnPercent));
    if (Number.isFinite(player.rank)) parts.push(`#${player.rank}`);
    summary.textContent = `${player.label}: ${parts.join(' · ')}`;
    summary.hidden = false;
    canvas.setAttribute('aria-label', `Portfolio value chart for ${player.label}`);
    description.textContent = `Portfolio value over time for ${player.label}. Select All players to compare every portfolio.`;
  };

  const applyDatasetState = () => {
    if (!chart) return;
    const selected = selectedPlayer();
    chart.data.datasets.forEach((dataset, index) => {
      const focused = Boolean(selected) && dataset.portfolioUserId === selected.id;
      chart.setDatasetVisibility(index, !selected || focused);
      dataset.borderWidth = focused ? 3 : 2;
      dataset.pointHoverRadius = focused ? 6 : 5;
    });
    updateSummary();
  };

  const rankAt = (time, player) => {
    const valuations = model.players.map((candidate) => ({
      player: candidate,
      frame: candidate.frames.find((frame) => frame.x === time),
    })).filter(({ frame }) => Number.isFinite(frame?.y));
    valuations.sort((left, right) => right.frame.y - left.frame.y || comparePlayers(left.player, right.player));
    return valuations.findIndex(({ player: candidate }) => candidate.id === player.id) + 1;
  };

  const tooltipLabel = (context) => {
    const player = model.players.find((candidate) => candidate.id === context.dataset.portfolioUserId);
    const frame = context.raw;
    if (!player || !frame || !Number.isFinite(frame.y)) return '';
    const currentRank = rankAt(frame.x, player);
    const change = signedMoney(frame.change, formatMoney);
    const changePercent = signedPercent(frame.changePercent);
    const asOf = frame.actual ? '' : ` · as of ${formatTimestamp(frame.observedAt)}`;
    if (!selectedPlayerId) {
      const delta = [change, changePercent].filter(Boolean).join(' ');
      return `#${currentRank} ${player.label}: ${formatMoney(frame.y)}${delta ? ` (${delta})` : ''}${asOf}`;
    }
    const lines = [`${formatMoney(frame.y)}${asOf}`];
    if (change) lines.push(`Change: ${change}${changePercent ? ` (${changePercent})` : ''}`);
    const totalPnl = signedMoney(frame.totalPnl, formatMoney);
    const totalReturn = signedPercent(frame.totalReturn);
    if (totalPnl || totalReturn) lines.push(`Total return: ${[totalPnl, totalReturn].filter(Boolean).join(' ')}`);
    return lines;
  };

  const tooltipTextColor = (context) => {
    const change = context.raw?.change;
    if (!Number.isFinite(change) || change === 0) return '#1f2328';
    return change > 0 ? '#1a7f37' : '#cf222e';
  };

  const hoverGuide = {
    id: 'portfolioValueHoverGuide',
    afterDraw(currentChart) {
      const active = currentChart.tooltip?.getActiveElements?.();
      if (!active?.length) return;
      const x = active[0].element.x;
      const { top, bottom } = currentChart.chartArea;
      const context = currentChart.ctx;
      context.save();
      context.strokeStyle = '#656d76';
      context.lineWidth = 1;
      context.setLineDash([4, 4]);
      context.beginPath();
      context.moveTo(x, top);
      context.lineTo(x, bottom);
      context.stroke();
      context.restore();
    },
  };

  const createChart = (datasets, initialRange) => {
    chart = new Chart(canvas, {
      type: 'line',
      data: { datasets },
      plugins: [hoverGuide],
      options: {
        animation: false,
        maintainAspectRatio: false,
        transitions: { active: { animation: { duration: 0 } } },
        interaction: { mode: 'index', intersect: false, axis: 'x' },
        events: ['mousemove', 'mouseout', 'click', 'touchstart', 'touchmove'],
        plugins: {
          legend: { display: false },
          tooltip: {
            mode: 'index',
            intersect: false,
            position: 'nearest',
            backgroundColor: '#ffffff',
            titleColor: '#1f2328',
            bodyColor: '#1f2328',
            borderColor: '#d0d7de',
            borderWidth: 1,
            padding: 10,
            caretPadding: 8,
            boxWidth: 10,
            boxHeight: 10,
            usePointStyle: true,
            itemSort: (left, right) => {
              const valueDifference = right.parsed.y - left.parsed.y;
              if (valueDifference) return valueDifference;
              const leftPlayer = model.players.find((player) => player.id === left.dataset.portfolioUserId);
              const rightPlayer = model.players.find((player) => player.id === right.dataset.portfolioUserId);
              return comparePlayers(leftPlayer, rightPlayer);
            },
            callbacks: {
              title: (items) => formatTimestamp(items[0].parsed.x),
              label: tooltipLabel,
              labelTextColor: tooltipTextColor,
            },
          },
          zoom: {
            limits: { x: { min: 'original', max: 'original', minRange: DAY } },
            pan: { enabled: true, mode: 'x', onPanComplete: syncNavigation },
            zoom: {
              mode: 'x',
              wheel: { enabled: true, modifierKey: 'ctrl' },
              pinch: { enabled: true },
              onZoomComplete: syncNavigation,
            },
          },
        },
        onResize: updateResetControl,
        scales: {
          x: {
            type: 'linear',
            min: initialRange.min,
            max: initialRange.max,
            ticks: { color: '#656d76', maxTicksLimit: 8, callback: (value) => new Date(value).toLocaleDateString() },
            grid: { color: '#d0d7de' },
          },
          y: {
            ticks: { color: '#656d76', callback: (value) => formatMoney(value) },
            grid: { color: '#d0d7de' },
          },
        },
      },
    });
  };

  const renderPlayerOptions = () => {
    const preservedSelection = selectedPlayerId;
    playerControl.replaceChildren(new Option('All players', ''));
    for (const player of model.players) {
      const prefix = Number.isFinite(player.rank) ? `#${player.rank} ` : '';
      playerControl.add(new Option(`${prefix}${player.label}`, player.id));
    }
    const selectedPlayerWasRemoved = Boolean(preservedSelection)
      && !model.players.some((player) => player.id === preservedSelection);
    if (selectedPlayerWasRemoved) selectedPlayerId = '';
    playerControl.value = selectedPlayerId;
    return selectedPlayerWasRemoved;
  };

  const setRange = (range, shouldAnnounce = false) => {
    const rangeChanged = selectedRange !== range;
    selectedRange = range;
    const bounds = rangeBounds();
    if (!chart || !bounds) return;
    chart.resetZoom();
    Object.assign(chart.options.scales.x, bounds);
    fitYAxis();
    chart.update('none');
    updateRangeControls();
    updateResetControl();
    if (shouldAnnounce && rangeChanged) announce(`Showing ${rangeLabel(range)}.`);
  };

  const setSelectedPlayer = (playerId, shouldAnnounce = false) => {
    const selectionChanged = selectedPlayerId !== playerId;
    selectedPlayerId = playerId;
    const bounds = rangeBounds();
    if (!chart || !bounds) return;
    applyDatasetState();
    Object.assign(chart.options.scales.x, bounds);
    fitYAxis();
    chart.update('none');
    updateResetControl();
    if (shouldAnnounce && selectionChanged) {
      const player = selectedPlayer();
      announce(player ? `Showing ${player.label} only.` : 'Showing all players.');
    }
  };

  const resetView = () => {
    if (!chart) return;
    const bounds = rangeBounds();
    chart.resetZoom();
    Object.assign(chart.options.scales.x, bounds);
    fitYAxis();
    chart.update('none');
    updateResetControl();
    announce(`View reset to ${rangeLabel()}.`);
  };

  const retry = () => {
    const action = retryAction;
    if (!action) return;
    setLoading();
    Promise.resolve().then(action).catch(() => showError(action));
  };

  playerControl.addEventListener('change', () => setSelectedPlayer(playerControl.value, true));
  for (const button of rangeControls) button.addEventListener('click', () => setRange(button.dataset.lbChartRange, true));
  retryControl.addEventListener('click', retry);

  if (hint) hint.textContent = 'Use Ctrl/⌘ + scroll or pinch to zoom · drag to pan';
  setLoading();

  return {
    update({ history, users, rankings }) {
      const previousRange = chart && hasAdditionalNavigation() ? currentRange() : null;
      const snapshotsByPlayer = normalizeHistory(history);
      const rankingsByPlayer = new Map((rankings || []).flatMap((ranking) => {
        const id = ranking?.user_id;
        return id === null || id === undefined ? [] : [[String(id), ranking]];
      }));
      const playerIds = new Set([...snapshotsByPlayer.keys(), ...rankingsByPlayer.keys(), ...Object.keys(users || {}).map(String)]);
      const liveAt = Date.now();
      const timestamps = [...new Set([
        ...[...snapshotsByPlayer.values()].flatMap((snapshots) => snapshots.map((snapshot) => snapshot.time)),
        ...(rankingsByPlayer.size ? [liveAt] : []),
      ])].sort((left, right) => left - right);
      const players = [...playerIds].map((id) => {
        const ranking = rankingsByPlayer.get(id);
        const label = users?.[id] || ranking?.display_name || ranking?.username || id;
        const rank = numeric(ranking?.rank) ?? Number.POSITIVE_INFINITY;
        const snapshots = snapshotsByPlayer.get(id) || [];
        const snapshotsAt = new Map(snapshots.map((snapshot) => [snapshot.time, snapshot]));
        let lastActual = null;
        const frames = timestamps.map((time) => {
          const liveSnapshot = time === liveAt && ranking && numeric(ranking.total_value) !== null ? {
            time,
            value: numeric(ranking.total_value),
            pnl: null,
            returnPercent: numeric(ranking.pnl_percent),
          } : null;
          const actual = snapshotsAt.get(time) || liveSnapshot;
          if (actual) {
            const previous = lastActual;
            lastActual = {
              ...actual,
              change: previous ? actual.value - previous.value : null,
              changePercent: previous?.value ? (actual.value - previous.value) / previous.value * 100 : null,
            };
          }
          if (!lastActual) return { x: time, y: null };
          return {
            x: time,
            y: lastActual.value,
            actual: Boolean(actual),
            observedAt: lastActual.time,
            change: lastActual.change,
            changePercent: lastActual.changePercent,
            totalPnl: lastActual.pnl,
            totalReturn: lastActual.returnPercent,
          };
        });
        return {
          id,
          label: String(label),
          rank,
          returnPercent: numeric(ranking?.pnl_percent),
          frames,
        };
      }).sort(comparePlayers);
      model = { players, timestamps, latestTimestamp: timestamps.at(-1) ?? null };
      const selectedPlayerWasRemoved = renderPlayerOptions();
      const hasHistory = [...snapshotsByPlayer.values()].some((snapshots) => snapshots.length > 0);
      const usable = hasHistory && players.some((player) => player.frames.some((frame) => Number.isFinite(frame.y)));
      if (!usable) {
        showEmpty();
        return;
      }
      const datasets = players.map((player) => ({
        label: player.label,
        portfolioUserId: player.id,
        data: player.frames,
        borderColor: colorFor(player.id),
        backgroundColor: 'transparent',
        tension: .25,
        pointRadius: 0,
        pointHoverRadius: 5,
        borderWidth: 2,
        spanGaps: true,
      }));
      const defaultRange = rangeBounds();
      if (!chart) createChart(datasets, defaultRange);
      else {
        chart.data.datasets = datasets;
        const keepRange = previousRange && previousRange.min >= timestamps[0] && previousRange.max <= timestamps.at(-1);
        Object.assign(chart.options.scales.x, keepRange ? previousRange : defaultRange);
      }
      applyDatasetState();
      fitYAxis();
      chart.update('none');
      updateRangeControls();
      updateControls(true);
      setReady();
      if (selectedPlayerWasRemoved) announce('The selected player is no longer available. Showing all players.');
    },
    resetView,
    setLoading,
    showError,
    syncNavigation,
  };
};
