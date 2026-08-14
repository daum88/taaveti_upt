export const $ = (id) => document.getElementById(id);

export const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (character) => ({
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
}[character]));

export const renderHtml = (element, markup) => {
  element.replaceChildren();
  element.innerHTML = markup;
};

export const fmt$ = (value) => `$${Number(value || 0).toLocaleString('en-US', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})}`;
export const fmtPct = (value) => `${value >= 0 ? '+' : ''}${Number(value || 0).toFixed(2)}%`;
export const fmtQty = (value) => Number(value || 0).toLocaleString('en-US', { maximumFractionDigits: 4 });
export const cls = (value) => (value >= 0 ? 'pos' : 'neg');
export const transactionClass = (type) => (type === 'BUY' || type === 'DIVIDEND' ? 'pos' : 'neg');
export const initials = (value) => (value || '?').slice(0, 2).toUpperCase();
export const formatChartTimestamp = (value) => new Date(value).toLocaleString();

export const badgeFor = (type, architecture) => {
  if (type === 'index_fund') return '<span class="badge index">Index</span>';
  if (architecture === 'multi_model') return '<span class="badge ensemble">AI Ensemble</span>';
  if (type === 'ai' || type === 'llm' || type === 'llm_agent') return '<span class="badge ai">AI</span>';
  return '<span class="badge human">Human</span>';
};
