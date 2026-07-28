"""
Mari — Conservative Value Investor
Enhanced with market context, performance tracking, forced sequential reasoning.
"""

MARI_SYSTEM_PROMPT = """You are "Mari," a conservative value investor. THINK BEFORE YOU ACT.

SEQUENTIAL DECISION PROCESS (do these in order):
STEP 1 — REVIEW HOLDINGS: Check every position. UP >10%? SELL. DOWN >8% and thesis broken? SELL. Over 5 positions? SELL the weakest.
STEP 2 — CHECK DIVERSIFICATION: What sectors are you in? Don't add to an already-heavy sector.
STEP 3 — SCAN FOR DIPS: Look for quality companies down 0.5-3%. Avoid anything surging >3%.
STEP 4 — ASSESS RISK: Check 5-day volatility. <4% = safe. >8% = avoid.
STEP 5 — EXECUTE: Make ONE decision. BUY, SELL, or HOLD. Maximum 10% allocation.

MARKET CONTEXT RULES:
- If SPY is DOWN >1%: excellent buying opportunity — quality on sale. But check if the dip is broad or stock-specific.
- If SPY is UP >1%: be extra picky. Don't chase. Quality is expensive in a rally.
- If SPY is FLAT: normal caution. Look for individual dips.

PORTFOLIO DISCIPLINE:
- Never hold more than 7 positions. Over-diversification dilutes returns.
- If you have 7+ positions, your first decision MUST be to SELL the worst performer.
- Cash reserve: always keep at least 5-10% in cash for opportunities.

RESPONSE FORMAT — JSON only:
{"ticker":"SYMBOL","decision":"BUY","allocation_percentage":0.08,"reasoning":"STEP 1: [review holdings]. STEP 2: [diversification check]. STEP 3: [best dip found — cite quality, price, % dip, news]. STEP 4: [risk: X% volatility = safe/risky]. STEP 5: [final decision with conviction X/10]."}
"""

def build_mari_context(funnel_stocks, holdings, cash, portfolio_value, market_open=True, trade_history=None):
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

    # Sector exposure
    sec = {}
    for h in holdings:
        with get_db() as conn:
            w = conn.execute("SELECT sector FROM watchlist WHERE ticker=?", (h["ticker"],)).fetchone()
        s = w["sector"] if w else "Unknown"
        sec[s] = sec.get(s, 0) + 1

    total_trades = len(trade_history or [])

    lines = [
        f"=== MARI — ${cash:,.2f} cash ({cp:.0f}%) | ${portfolio_value:,.2f} total | {'LIVE' if market_open else 'CLOSED'} ===",
    ]
    if spy_price:
        spy_dir = "🟢 BUYING OPPORTUNITY" if spy_change < -1 else "🟡 BE SELECTIVE" if spy_change > 1 else "🟢 NORMAL"
        lines.append(f"S&P 500 (SPY): ${spy_price:.2f} ({spy_change:+.2f}%) → {spy_dir}")
    if sec:
        lines.append(f"Sectors: {', '.join(f'{s}={n}' for s,n in sorted(sec.items()))}")
    lines.append(f"Total trades: {total_trades} | Cash reserve: {cp:.0f}% {'✓ Healthy' if cp > 5 else '⚠️ LOW — sell something first' if cp < 5 else ''}")

    # Holdings with SELL urgency
    unrealized_pnl = 0
    if holdings:
        lines.append(f"\n=== STEP 1: REVIEW YOUR {len(holdings)} HOLDINGS ===")
        if len(holdings) >= 7:
            lines.append("⚠️ 7+ POSITIONS — OVER-DIVERSIFIED. YOU MUST SELL THE WEAKEST BEFORE BUYING.")
        for h in holdings:
            with get_db() as conn:
                ps = conn.execute("SELECT price FROM price_snapshots WHERE ticker=? ORDER BY snapshot_at DESC LIMIT 1", (h["ticker"],)).fetchone()
            cur = dec(ps["price"]) if ps else h["average_cost_per_share"]
            pnl = (cur - h["average_cost_per_share"]) * h["quantity"]
            pnl_pct = ((cur / h["average_cost_per_share"]) - 1) * 100
            unrealized_pnl += pnl

            if pnl_pct > 10:
                action = " 🔴 SELL — up >10%! Lock in ${:+,.2f}".format(pnl)
            elif pnl_pct < -8:
                action = " 🔴 CUT — down {:+.1f}%, thesis may be broken".format(pnl_pct)
            elif abs(pnl_pct) < 0.3:
                action = " ⚠️ FLAT — dead weight, consider selling"
            else:
                action = ""

            lines.append(f"  {h['ticker']}: {h['quantity']:.2f}×${h['average_cost_per_share']:.2f} → ${cur:.2f} | P&L ${pnl:+,.2f} ({pnl_pct:+.1f}%){action}")
        lines.append(f"  → Net unrealized: ${unrealized_pnl:+,.2f}")
    else:
        lines.append("\n=== STEP 1: NO HOLDINGS ===")

    # Trade history
    if trade_history:
        lines.append(f"\n=== YOUR LAST {len(trade_history)} TRADES ===")
        for t in trade_history:
            lines.append(f"  {t['action']} {t['ticker']} {t['quantity']:.2f}×${t['price']:.2f} = ${t['total']:,.2f}")

    # Market scan — prioritize dips
    dips = sorted([s for s in funnel_stocks if (s.get("change_percent") or 0) < -0.5], key=lambda s: s.get("change_percent", 0))
    rest = sorted([s for s in funnel_stocks if (s.get("change_percent") or 0) >= -0.5], key=lambda s: abs(s.get("change_percent", 0) or 0), reverse=True)
    shown = (dips + rest)

    lines.append(f"\n=== STEP 3: MARKET SCAN ({len(funnel_stocks)} stocks, dips prioritized) ===")

    for s in shown:
        ch = s.get("change_percent", 0) or 0
        sig = "🛒 DIP!" if ch < -2 else "🛒 mild dip" if ch < -0.5 else "⚠️ SURGE" if ch > 3 else "➡️"
        with get_db() as conn:
            oh = conn.execute("SELECT high, low FROM ohlcv_cache WHERE ticker=? ORDER BY date DESC LIMIT 5", (s["ticker"],)).fetchall()
        vol_5d = ((max(r['high'] for r in oh) - min(r['low'] for r in oh)) / min(r['low'] for r in oh) * 100) if oh and len(oh) >= 2 else 0
        risk = "🔴 HIGH" if vol_5d > 8 else "🟡 MED" if vol_5d > 4 else "🟢 LOW"

        lines.append(f"  {sig} {s['ticker']} ${s.get('price',0):.2f} Δ{ch:+.2f}% Risk:{risk}({vol_5d:.1f}%)")
        if s.get("news_headlines"):
            for n in s["news_headlines"][:5]:
                lines.append(f"    📰 {n[:100]}")

    lines.append("\n=== STEP 5: DECIDE ===")
    lines.append("Pick ONE action. If you have 7+ positions, you MUST sell first.")
    lines.append("Follow the 5-step process. Be the cautious, disciplined investor you are.")
    return "\n".join(lines)
