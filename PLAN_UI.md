# UI Plan — Taaveti UPT (client-only)

## Goal
A simple, client-only web UI (single `index.html` + JS, no build step) that consumes the existing FastAPI endpoints. Three primary views, leaderboard as the home screen.

## Game context (rules.txt)
- 10–15 humans + 6–8 AI models compete
- €10,000 starting capital, US stocks/ETFs only
- Trades once per week, 9–12 months, no leverage/shorting/options
- Winner judged on **return AND risk metrics** → UI must surface both

## Existing endpoints to reuse (no backend work needed)
| View | Endpoint | Notes |
|------|----------|-------|
| Leaderboard | `GET /api/leaderboard` | rank, pnl%, value per player |
| Transaction history (global) | `GET /api/transactions?limit=` | recent trades with usernames |
| Transaction history (per player) | `GET /api/trades/{username}` | player's trade log |
| Player portfolio + stats | `GET /api/agent-detail/{username}` | snapshot, holdings, sectors, stats, pnl_history |
| Equity curve | `pnl_history` inside agent-detail | for sparkline / chart |

## Views

### 1. Leaderboard (main / landing)
- Sortable table, ranked by total portfolio value / return %.
- Columns: rank, avatar+name, human/AI badge, portfolio value (€), return % (green/red),
  and a **risk column** (volatility / max drawdown) since winner depends on risk too.
- Small equity sparkline per row (from `pnl_history`).
- Row click → opens Player Detail view (drill-down).
- Header KPIs: number of players, days remaining, best/worst performer.

### 2. Player Detail (portfolio + history in one drill-down)
Tabbed panel opened from a leaderboard row:
- **Portfolio tab (default):** current holdings table (ticker, qty, avg cost, current price,
  market value, unrealized P&L, weight %), cash balance, sector allocation donut,
  headline stats (win rate, total trades, largest trade).
- **History tab:** chronological transaction list (BUY/SELL, ticker, qty, price, total,
  timestamp, LLM reasoning if present). Filter by BUY/SELL.
- **Performance tab:** equity curve line chart from `pnl_history`.

### 3. Global Activity (optional secondary)
- Live feed of all players' recent transactions (`/api/transactions`), so you can watch
  the weekly trading round unfold. Auto-refresh via existing websocket if present.

## Layout & tech
- Single-page, client-only. Plain HTML + vanilla JS (or lightweight Alpine.js) + fetch.
- Charts: a tiny lib (uPlot / Chart.js via CDN) or inline SVG sparklines — no bundler.
- Responsive: leaderboard collapses to cards on mobile; detail opens as a side drawer/modal.
- Currency shown in **USD ($)** — matches backend storage/compute. Multi-currency is NOT supported; rules mention €10k but the system runs $10k. No conversion.

## Navigation model
```
Leaderboard (home)
   └─ click player ─▶ Player Detail drawer
                        ├─ Portfolio (current holdings)
                        ├─ History (transactions)
                        └─ Performance (equity curve)
   └─ Activity feed (top-level tab)
```

## Decisions
- **Currency: USD ($)** — confirmed. No EUR conversion (would be backend work).

## Open questions
1. Risk metric for leaderboard — is volatility/drawdown available or do we compute client-side from `pnl_history`?
2. Extend the current `ui/web/index.html` or start a fresh clean page?
