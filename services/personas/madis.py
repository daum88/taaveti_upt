"""
Madis — Aggressive Momentum Investor
Enhanced with market context, performance tracking, forced sequential reasoning.
"""

MADIS_SYSTEM_PROMPT = """You are "Madis," an aggressive momentum trader. THINK BEFORE YOU ACT.

SEQUENTIAL DECISION PROCESS (do these in order):
STEP 1 — REVIEW HOLDINGS: Check every position. Is anything UP >10%? SELL. DOWN >5%? SELL. FLAT for multiple cycles? SELL.
STEP 2 — CHECK CASH: How much do you have? If <$500, you MUST sell something to free up cash before buying.
STEP 3 — SCAN MARKET: Look for stocks moving >2% with volume and news. Pick the ONE best signal.
STEP 4 — SIZE IT: How confident? 8+/10 → 20-25%. 5-7/10 → 10-15%. <5/10 → HOLD or small 5% bet.
STEP 5 — EXECUTE: Make ONE decision. BUY, SELL, or HOLD. Maximum ONE trade per cycle.

MARKET CONTEXT RULES:
- If SPY is DOWN >1%: market is selling off. Be cautious. Only buy if the stock is bucking the trend.
- If SPY is UP >0.5%: risk-on. Normal aggression.
- If SPY is FLAT: be selective. Only act on clear catalysts.

PERFORMANCE AWARENESS:
- Your total P&L and win rate are shown. If you're losing, be more conservative.
- Don't repeat losing patterns. If a sector keeps burning you, avoid it.
- Your best trade shows what works. Your worst trade shows what to avoid.

RESPONSE FORMAT — JSON only:
{"ticker":"SYMBOL","decision":"BUY","allocation_percentage":0.20,"reasoning":"STEP 1: [review holdings]. STEP 2: [cash check]. STEP 3: [best signal found — cite price, % move, volume, news]. STEP 4: [conviction X/10, why this size]. STEP 5: [final decision]."}
"""

def build_madis_context(funnel_stocks, holdings, cash, portfolio_value, market_open=True, trade_history=None):
    from db.connection import get_db
    from db.money import dec
    cash = dec(cash); portfolio_value = dec(portfolio_value)
    cp = (cash / portfolio_value * 100) if portfolio_value > 0 else 100

    # S&P 500 context
    spy_price = None
    spy_change = 0
    try:
        from services.market_data import fetch_prices_batch
        spy_data = fetch_prices_batch(["SPY"])
        if "SPY" in spy_data:
            spy_price = spy_data["SPY"]["price"]
            spy_change = spy_data["SPY"].get("change_percent", 0) or 0
    except Exception:
        pass

    # Performance stats
    wins = sum(1 for t in (trade_history or []) if t.get("action") == "SELL")  # rough proxy
    total_trades = len(trade_history or [])
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

    lines = [
        f"=== MADIS — ${cash:,.2f} cash ({cp:.0f}%) | ${portfolio_value:,.2f} total | {'LIVE' if market_open else 'CLOSED'} ===",
    ]
    if spy_price:
        spy_dir = "📈 RISK-ON" if spy_change > 0.5 else "📉 CAUTIOUS" if spy_change < -1 else "➡️ SELECTIVE"
        lines.append(f"S&P 500 (SPY): ${spy_price:.2f} ({spy_change:+.2f}%) → {spy_dir}")
    if total_trades > 0:
        lines.append(f"Your record: {total_trades} trades | Estimated win rate: {win_rate:.0f}% | {'🟢 Doing well' if win_rate > 50 else '🔴 Needs improvement'}")

    # Holdings with SELL urgency
    unrealized_pnl = 0
    if holdings:
        lines.append("\n=== STEP 1: REVIEW YOUR HOLDINGS ===")
        for h in holdings:
            with get_db() as conn:
                ps = conn.execute("SELECT price FROM price_snapshots WHERE ticker=? ORDER BY snapshot_at DESC LIMIT 1", (h["ticker"],)).fetchone()
            cur = dec(ps["price"]) if ps else h["average_cost_per_share"]
            pnl = (cur - h["average_cost_per_share"]) * h["quantity"]
            pnl_pct = ((cur / h["average_cost_per_share"]) - 1) * 100
            unrealized_pnl += pnl
            pos_pct = ((h['quantity'] * cur) / portfolio_value * 100) if portfolio_value > 0 else 0

            if pnl_pct > 10:
                action = " 🔴 MUST SELL — up >10%! Lock in ${:+,.2f}".format(pnl)
            elif pnl_pct > 5:
                action = " 🟡 Consider selling — up {:+.1f}%".format(pnl_pct)
            elif pnl_pct < -5:
                action = " 🔴 CUT LOSS — down {:+.1f}%".format(pnl_pct)
            elif abs(pnl_pct) < 0.3:
                action = " ⚠️ DEAD MONEY — flat, consider freeing cash"
            else:
                action = ""

            lines.append(f"  {h['ticker']}: {h['quantity']:.2f}×${h['average_cost_per_share']:.2f} → ${cur:.2f} | P&L ${pnl:+,.2f} ({pnl_pct:+.1f}%) | {pos_pct:.0f}% of portfolio{action}")
        lines.append(f"  → Net unrealized: ${unrealized_pnl:+,.2f}")
        lines.append(f"  → Cash remaining: ${cash:,.2f}")
        if cash < 500:
            lines.append("  ⚠️ CASH BELOW $500 — YOU MUST SELL SOMETHING FIRST!")
    else:
        lines.append("\n=== STEP 1: NO HOLDINGS — skip to buying ===")

    # Trade history
    if trade_history:
        lines.append(f"\n=== YOUR LAST {len(trade_history)} TRADES ===")
        for t in trade_history:
            lines.append(f"  {t['action']} {t['ticker']} {t['quantity']:.2f}×${t['price']:.2f} = ${t['total']:,.2f}")
        # Best/worst trade analysis
        if trade_history:
            buys = [t for t in trade_history if t.get("action") == "BUY"]
            if buys:
                lines.append(f"  → You've made {len(buys)} buys. Learn from what worked and what didn't.")

    # Market scan
    top = sorted(funnel_stocks, key=lambda s: abs(s.get("change_percent", 0) or 0), reverse=True)
    lines.append(f"\n=== STEP 3: MARKET SCAN ({len(funnel_stocks)} stocks, showing top {len(top)}) ===")

    for s in top:
        ch = s.get("change_percent", 0) or 0
        direction = "🚀" if ch > 5 else "📈" if ch > 2 else "📉" if ch < -2 else "➡️"
        vol = f"{s.get('volume',0):,}" if s.get('volume') else "?"
        with get_db() as conn:
            oh = conn.execute("SELECT high, low, close FROM ohlcv_cache WHERE ticker=? ORDER BY date DESC LIMIT 5", (s["ticker"],)).fetchall()
        rng = f"${min(r['low'] for r in oh):.0f}–${max(r['high'] for r in oh):.0f}" if oh and len(oh) >= 2 else "?"

        lines.append(f"  {direction} {s['ticker']} ${s.get('price',0):.2f} Δ{ch:+.2f}% Vol:{vol} 5d:{rng}")
        if s.get("news_headlines"):
            for n in s["news_headlines"][:5]:
                lines.append(f"    📰 {n[:100]}")

    lines.append("\n=== STEP 5: DECIDE ===")
    lines.append("Pick ONE action: BUY the best signal, SELL a weak holding, or HOLD if nothing meets criteria.")
    lines.append("Follow the 5-step process in your reasoning. Cite specific numbers at each step.")
    return "\n".join(lines)
