"""
Generic strategy-driven persona.

Renders a system prompt and a market/portfolio context purely from a
`strategy_config` dict, so new agents can be added without new Python.

strategy_config keys (all optional, with defaults):
  style            : "aggressive" | "value" | "balanced"  (flavour text)
  sell_gain_pct    : take-profit hint (e.g. 10)
  sell_loss_pct    : cut-loss hint, negative (e.g. -5)
  min_move_pct     : min |% move| for a buy signal (e.g. 2)
  max_positions    : soft cap on number of holdings (e.g. 7)
  max_allocation   : max fraction per position (0-1, e.g. 0.20)
  max_volatility_pct: avoid buys above this 5-day range % (e.g. 8)
  cash_reserve_pct : keep at least this % in cash (e.g. 5)
  prefer_dips      : bool — prioritise negative movers in the scan
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from db.connection import get_db
from db.money import dec

if TYPE_CHECKING:
    from services.decision_input import DecisionInput

DEFAULTS = {
    "style": "balanced",
    "sell_gain_pct": 12.0,
    "sell_loss_pct": -8.0,
    "min_move_pct": 2.0,
    "max_positions": 7,
    "max_allocation": 0.20,
    "max_volatility_pct": 8.0,
    "cash_reserve_pct": 5.0,
    "prefer_dips": False,
}


def merged(config: dict | None) -> dict:
    cfg = dict(DEFAULTS)
    if config:
        cfg.update({k: v for k, v in config.items() if v is not None})
    return cfg


def build_generic_system_prompt(name: str, config: dict | None, persona_prompt: str = "") -> str:
    c = merged(config)
    persona = f"\nYOUR PERSONA: {persona_prompt}\n" if persona_prompt else ""
    dip_line = "STEP 3 — SCAN FOR DIPS: Prefer quality names that are DOWN — look for pullbacks to buy." if c["prefer_dips"] else f"STEP 3 — SCAN MARKET: Find the single strongest signal moving >{c['min_move_pct']:.0f}% with volume and news."
    return f"""You are "{name.title()}", a {c["style"]} investor. THINK BEFORE YOU ACT.
{persona}
SEQUENTIAL DECISION PROCESS (do these in order):
STEP 1 — REVIEW HOLDINGS: Sell anything UP >{c["sell_gain_pct"]:.0f}% (take profit) or DOWN >{abs(c["sell_loss_pct"]):.0f}% (cut loss). If you hold more than {c["max_positions"]} positions, sell the weakest first.
STEP 2 — CHECK CASH: Keep at least {c["cash_reserve_pct"]:.0f}% in cash. If below, free up cash before buying.
{dip_line}
STEP 4 — ASSESS RISK: Avoid buys whose 5-day range volatility exceeds {c["max_volatility_pct"]:.0f}%.
STEP 5 — SIZE & EXECUTE: Make ONE decision (BUY, SELL, or HOLD). Never allocate more than {c["max_allocation"] * 100:.0f}% to a single position. Maximum ONE trade per cycle.

MARKET CONTEXT RULES:
- SPY DOWN >1%: market selling off — be cautious (or opportunistic if you buy dips).
- SPY UP >0.5%: risk-on, act normally.
- SPY FLAT: be selective, only act on clear catalysts.
- ETFs are diversified instruments, not company shares; use their category and underlying exposure when assessing them.

RESPONSE FORMAT — JSON only:
{{"ticker":"SYMBOL","decision":"BUY","allocation_percentage":0.10,"reasoning":"STEP 1..STEP 5, cite specific prices, % moves, volume, news and conviction X/10."}}
"""


def build_generic_context(
    name,
    config,
    funnel_stocks,
    holdings,
    cash,
    portfolio_value,
    market_open=True,
    trade_history=None,
    decision_input: DecisionInput | None = None,
):
    """Render one account's information around an optional immutable batch input.

    When supplied, ``decision_input`` is the sole source of shared market
    information. Portfolio state remains account-specific.
    """
    c = merged(config)
    cash = dec(cash)
    portfolio_value = dec(portfolio_value)
    cp = (cash / portfolio_value * 100) if portfolio_value > 0 else 100
    shared_prices: Mapping[str, Mapping] = {}
    if decision_input is not None:
        funnel_stocks = decision_input.funnel_stocks
        market_open = decision_input.market_open
        shared_prices = decision_input.prices
        spy = decision_input.spy_quote
    else:
        spy = None
    spy_price = spy["price"] if spy else None
    spy_change = spy.get("change_percent", 0) if spy else 0

    lines = [f"=== {name.upper()} ({c['style']}) — ${cash:,.2f} cash ({cp:.0f}%) | ${portfolio_value:,.2f} total | {'LIVE' if market_open else 'CLOSED'} ==="]
    if spy_price:
        spy_dir = "📈 RISK-ON" if spy_change > 0.5 else "📉 CAUTIOUS" if spy_change < -1 else "➡️ SELECTIVE"
        lines.append(f"S&P 500 (SPY): ${spy_price:.2f} ({spy_change:+.2f}%) → {spy_dir}")
    lines.append(f"Cash reserve target: {c['cash_reserve_pct']:.0f}% | {'✓ OK' if cp >= c['cash_reserve_pct'] else '⚠️ LOW — sell to free cash'}")

    unrealized = 0
    if holdings:
        lines.append(f"\n=== STEP 1: REVIEW YOUR {len(holdings)} HOLDINGS ===")
        if len(holdings) > c["max_positions"]:
            lines.append(f"⚠️ OVER {c['max_positions']} POSITIONS — SELL THE WEAKEST BEFORE BUYING.")
        for h in holdings:
            quote = shared_prices.get(h["ticker"])
            if quote is None and decision_input is None:
                with get_db() as conn:
                    ps = conn.execute("SELECT price FROM price_snapshots WHERE ticker=? ORDER BY snapshot_at DESC LIMIT 1", (h["ticker"],)).fetchone()
                quote = {"price": ps["price"]} if ps else None
            if quote is None:
                lines.append(f"  {h['ticker']}: {h['quantity']:.2f}×${h['average_cost_per_share']:.2f} → quote unavailable")
                continue
            cur = dec(quote["price"])
            pnl = (cur - h["average_cost_per_share"]) * h["quantity"]
            pnl_pct = ((cur / h["average_cost_per_share"]) - 1) * 100
            unrealized += pnl
            if pnl_pct > c["sell_gain_pct"]:
                action = f" 🔴 SELL — up >{c['sell_gain_pct']:.0f}%! Lock in ${pnl:+,.2f}"
            elif pnl_pct < c["sell_loss_pct"]:
                action = f" 🔴 CUT — down {pnl_pct:+.1f}%"
            elif abs(pnl_pct) < 0.3:
                action = " ⚠️ FLAT — dead weight"
            else:
                action = ""
            lines.append(f"  {h['ticker']}: {h['quantity']:.2f}×${h['average_cost_per_share']:.2f} → ${cur:.2f} | P&L ${pnl:+,.2f} ({pnl_pct:+.1f}%){action}")
        lines.append(f"  → Net unrealized: ${unrealized:+,.2f}")
    else:
        lines.append("\n=== STEP 1: NO HOLDINGS ===")

    if trade_history:
        lines.append(f"\n=== YOUR LAST {len(trade_history)} TRADES ===")
        for t in trade_history:
            lines.append(f"  {t['action']} {t['ticker']} {t['quantity']:.2f}×${t['price']:.2f} = ${t['total']:,.2f}")

    if c["prefer_dips"]:
        dips = sorted([s for s in funnel_stocks if (s.get("change_percent") or 0) < -0.5], key=lambda s: s.get("change_percent", 0))
        rest = sorted([s for s in funnel_stocks if (s.get("change_percent") or 0) >= -0.5], key=lambda s: abs(s.get("change_percent", 0) or 0), reverse=True)
        shown = dips + rest
    else:
        shown = sorted(funnel_stocks, key=lambda s: abs(s.get("change_percent", 0) or 0), reverse=True)

    lines.append(f"\n=== STEP 3: MARKET SCAN ({len(funnel_stocks)} instruments) ===")
    for s in shown:
        ch = s.get("change_percent", 0) or 0
        with get_db() as conn:
            oh = conn.execute("SELECT high, low FROM ohlcv_cache WHERE ticker=? ORDER BY date DESC LIMIT 5", (s["ticker"],)).fetchall()
        vol_5d = ((max(r["high"] for r in oh) - min(r["low"] for r in oh)) / min(r["low"] for r in oh) * 100) if oh and len(oh) >= 2 else 0
        risk = "🔴 HIGH" if vol_5d > c["max_volatility_pct"] else "🟡 MED" if vol_5d > c["max_volatility_pct"] / 2 else "🟢 LOW"
        vol = f"{s.get('volume', 0):,}" if s.get("volume") else "?"
        kind = "ETF" if s.get("instrument_type") == "etf" else "equity"
        category = f" / {s['category']}" if s.get("category") else ""
        lines.append(f"  {s['ticker']} [{kind}{category}] ${s.get('price', 0):.2f} Δ{ch:+.2f}% Vol:{vol} Risk:{risk}({vol_5d:.1f}%)")
        if s.get("news_headlines"):
            for n in s["news_headlines"][:5]:
                lines.append(f"    📰 {n[:100]}")

    lines.append("\n=== STEP 5: DECIDE ===")
    lines.append(f"Pick ONE action. Respect your {c['style']} style and the limits above.")
    return "\n".join(lines)
