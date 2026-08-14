const decisionLabel = (status) => ({
  queued: 'Queued',
  running: 'Running',
  completed: 'Completed',
  completed_with_errors: 'Completed with errors',
  failed: 'Failed',
  interrupted: 'Interrupted',
  due: 'Due now',
  not_due: 'Not scheduled',
}[status] || 'Ready to run');

const decisionDate = (value, timezone) => value
  ? new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: timezone,
  }).format(new Date(value))
  : 'Never';

const batchStatus = (week) => (week.current_batch || week.latest_batch || {}).status || 'idle';

/**
 * Owns AI-decision dashboard state, rendering, polling, and manual triggering.
 *
 * @param {{
 *   requestJson: (path: string, options?: object) => Promise<unknown>,
 *   requestErrorType: typeof Error,
 *   onStatusChange?: (status: object) => void,
 * }} dependencies
 */
export const createDecisionStatus = ({ requestJson, requestErrorType, onStatusChange = () => {} }) => {
  let status = null;
  let pollTimer;

  const element = (id) => document.getElementById(id);

  const render = (response) => {
    const week = response.days
      ? response
      : {
        days: [],
        current_batch: response.status === 'running' ? response : null,
        latest_batch: response,
        timezone: undefined,
        ai_account_count: response.counts?.total || 0,
      };
    status = {
      ...week,
      status: batchStatus(week),
      agents: (week.current_batch || week.latest_batch || {}).agents || {},
    };
    onStatusChange(status);

    const button = element('batch-decision-btn');
    const message = element('batch-decision-msg');
    const times = element('batch-decision-times');
    const strip = element('decision-week');
    if (!button || !message || !times || !strip) return;

    const batch = week.current_batch || week.latest_batch || {};
    const running = batch.status === 'running';
    const eligible = !batch.next_eligible_at || new Date(batch.next_eligible_at) <= new Date();
    const counts = batch.counts || {};
    button.disabled = running || !eligible;
    button.textContent = batch.status === 'failed' || batch.status === 'interrupted'
      ? 'Retry decisions'
      : 'Run decisions now';
    message.textContent = running
      ? `Running — ${counts.completed || 0} of ${counts.total || week.ai_account_count || 0} accounts complete${counts.failed ? ` · ${counts.failed} failed` : ''}`
      : batch.status === 'completed' || batch.status === 'completed_with_errors'
        ? `${decisionLabel(batch.status)} today · ${counts.completed || 0} completed${counts.failed ? ` · ${counts.failed} failed` : ''}`
        : week.days.some((day) => day.state === 'due')
          ? 'Due today — not run'
          : 'Ready to run';
    times.textContent = `Last run: ${decisionDate(batch.last_completed_at || batch.last_triggered_at, week.timezone)}${batch.next_eligible_at ? ` · Available again: ${decisionDate(batch.next_eligible_at, week.timezone)}` : ''}`;
    strip.replaceChildren(...week.days.map((day) => {
      const state = day.state;
      const symbol = ({
        completed: '✓',
        completed_with_errors: '✓',
        due: '!',
        failed: '↻',
        interrupted: '↻',
        running: '…',
        not_due: '—',
      })[state] || '—';
      const label = `${day.weekday}, ${day.date}: ${decisionLabel(state)}${day.due_at ? `; reminder ${decisionDate(day.due_at, week.timezone)}` : ''}${day.run_count > 1 ? `; ${day.run_count} runs` : ''}`;
      const cell = document.createElement('div');
      cell.className = `week-day${day.is_today ? ' today' : ''}`;
      cell.tabIndex = 0;
      cell.setAttribute('aria-label', label);
      cell.title = label;
      const initial = document.createElement('span');
      initial.className = 'week-initial';
      initial.textContent = day.weekday.slice(0, 1);
      const date = document.createElement('span');
      date.className = 'week-date';
      date.textContent = String(new Date(`${day.date}T12:00:00`).getDate());
      const stateIndicator = document.createElement('span');
      stateIndicator.className = `week-state ${state}`;
      stateIndicator.setAttribute('aria-hidden', 'true');
      stateIndicator.textContent = symbol;
      cell.append(initial, date, stateIndicator);
      return cell;
    }));
  };

  const load = async () => {
    try {
      render(await requestJson('/api/decision-batches/week'));
    } catch {}
  };

  const trigger = async () => {
    const button = element('batch-decision-btn');
    button.disabled = true;
    try {
      await requestJson('/api/decision-batches', { method: 'POST' });
      await load();
    } catch (error) {
      if (error instanceof requestErrorType && error.data) render(error.data);
      else element('batch-decision-msg').textContent = `Failed: ${error.message}`;
    }
  };

  return {
    get status() {
      return status;
    },
    render,
    trigger,
    start() {
      load();
      pollTimer = setInterval(() => {
        if (status?.current_batch?.status === 'running') load();
      }, 3_000);
      return () => clearInterval(pollTimer);
    },
  };
};
