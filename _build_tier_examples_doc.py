"""Algo Paper Trading — worked-numbers tier reference (v2, with EARLY_TRAIL_PCT)."""
from docx import Document
from docx.shared import Pt, RGBColor, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH

OUT = r"C:\Users\Sarfaraz Khimani\Documents\portfolio-alerts-new\algo-tier-worked-examples.docx"

doc = Document()
for s in doc.sections:
    s.top_margin = Cm(2.0); s.bottom_margin = Cm(2.0)
    s.left_margin = Cm(2.0); s.right_margin = Cm(2.0)
doc.styles['Normal'].font.name = 'Calibri'
doc.styles['Normal'].font.size = Pt(11)


def h1(t): doc.add_heading(t, level=1)
def h2(t): doc.add_heading(t, level=2)


def para(text, bold=False, italic=False):
    p = doc.add_paragraph()
    r = p.add_run(text)
    r.bold = bold; r.italic = italic
    return p


def bullet(text, level=0):
    p = doc.add_paragraph(style='List Bullet')
    p.paragraph_format.left_indent = Cm(0.6 + level * 0.6)
    p.add_run(text)


def code_block(text):
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Cm(0.4)
    r = p.add_run(text)
    r.font.name = 'Consolas'
    r.font.size = Pt(9.5)


def callout(text):
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Cm(0.3)
    r = p.add_run(text)
    r.bold = True
    r.font.color.rgb = RGBColor(0x1e, 0x40, 0xaf)


def table(headers, rows, widths_cm=None):
    t = doc.add_table(rows=1, cols=len(headers))
    t.style = 'Light Grid Accent 1'
    hdr = t.rows[0].cells
    for i, h in enumerate(headers):
        hdr[i].text = h
        for run in hdr[i].paragraphs[0].runs:
            run.bold = True
            run.font.size = Pt(10)
    for row in rows:
        rc = t.add_row().cells
        for i, v in enumerate(row):
            rc[i].text = str(v)
            for p in rc[i].paragraphs:
                for run in p.runs:
                    run.font.size = Pt(10)
    if widths_cm:
        for row in t.rows:
            for i, w in enumerate(widths_cm):
                row.cells[i].width = Cm(w)


def fmt_inr(n):
    s = f"{int(round(n)):,}"
    return f"₹{s}"


# ──────────────────────────────────────────────────────────────────────
# Title
# ──────────────────────────────────────────────────────────────────────
t = doc.add_paragraph(); t.alignment = WD_ALIGN_PARAGRAPH.CENTER
r = t.add_run("Algo Paper Trading — Tier-by-Tier Worked Examples")
r.bold = True; r.font.size = Pt(20)

sub = doc.add_paragraph(); sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
r = sub.add_run("v2 — includes EARLY_TRAIL_PCT (+10%) safety net and MFE/MAE tracking")
r.italic = True; r.font.size = Pt(11)
r.font.color.rgb = RGBColor(0x64, 0x74, 0x8b)
doc.add_paragraph()

# ──────────────────────────────────────────────────────────────────────
# 1. Mental Model
# ──────────────────────────────────────────────────────────────────────
h1("1. The Mental Model")
para("Five concepts drive everything that follows:", bold=True)
code_block(
    "R                = entry_price − initial_stop      (per-share risk in ₹)\n"
    "stop_dist        = 2 × ATR_14                       (= 1R, by construction)\n"
    "SLEEVE           = ₹25,00,000                       (fixed paper capital)\n"
    "EARLY_TRAIL_PCT  = 10.0                             (MFE threshold to arm trailing stop)\n"
    "trail_stop       = close − 2 × ATR_14               (ratchets up daily, never down)"
)

para("Two new terms you'll see in the dashboard:", bold=True)
bullet("MFE — Maximum Favorable Excursion. The highest unrealized % gain ever reached during a trade. Tells you 'how good did this trade get?'")
bullet("MAE — Maximum Adverse Excursion. The lowest unrealized % loss ever reached. Tells you 'how much pain did you sit through?'")
para(
    "Both are captured daily as bar-high vs entry (MFE) and bar-low vs entry (MAE) by "
    "`paper_trader.py:process_exits`. Stored in `paper_trades.max_unrealized_pct` and "
    "`min_unrealized_pct`. After 30-60 days of paper trades we use the MFE distribution to "
    "answer questions like: 'of trades that never hit the 1st partial target, what % "
    "peaked above +10%?' That tells us whether our R-targets are calibrated or whether "
    "we're leaving money on the table."
)

para(
    "Because stop_dist is fixed at 2×ATR, R always equals 2×ATR. So every R-multiple "
    "in the strategy translates directly into an ATR multiple of distance from entry:"
)
table(
    ["R-multiple", "In ATR units", "Meaning"],
    [
        ["1R", "2 × ATR", "Per-share risk. Distance from entry to initial stop."],
        ["2R", "4 × ATR", "First partial target for T2/T3/T4."],
        ["3R", "6 × ATR", "First partial target for T1."],
        ["4R", "8 × ATR", "Second partial target for T2/T3/T4."],
        ["6R", "12 × ATR", "Second partial target for T1."],
    ],
    [2.5, 3.0, 11.5],
)

h2("The EARLY_TRAIL_PCT safety net (added 2026-05-15)")
para(
    "Problem the rule solves: for high-ATR names, the R-target ladder sits far above "
    "entry. SAIL with 2 ATR = 6.7% has its T1 1st partial at +20% — meaning a trade "
    "that ran to +12% and reversed all the way back used to hit the initial stop at "
    "−6.7%. The fix:"
)
code_block(
    "Inside process_exits, AFTER R-partial check, BEFORE trail update:\n"
    "  if not trail_armed and max_unrealized_pct >= 10.0:\n"
    "      trail_armed = True\n"
    "      trail_armed_reason = 'pct_threshold'\n"
    "      current_stop = max(current_stop, entry_price)   # floor at entry\n\n"
    "Then the regular daily trail update runs:\n"
    "  if trail_armed:\n"
    "      new_stop = close − 2 × ATR_14\n"
    "      if new_stop > current_stop:\n"
    "          current_stop = new_stop"
)
para("Important properties of this rule:", bold=True)
bullet("Does NOT book any profit. Just flips the trail switch and floors the stop at entry. r_multiple bookkeeping stays clean — a trade that exits at +9R is still recorded as +9R.")
bullet("Is a no-op for low-ATR names where the formal 1st partial fires below +10%. There, the partial arms breakeven first and our rule arms nothing new.")
bullet("Only bites when 1st formal partial sits above +10% (i.e., high-ATR stocks). That's the case it was designed for.")
bullet("`trail_armed_reason` column ('partial_2' vs 'pct_threshold') lets us compare outcomes by trigger — critical for tuning.")

para(
    "Position size is constrained by two limits — whichever produces the SMALLER qty wins."
)
code_block(
    "qty_by_risk = floor( SLEEVE × risk_pct / stop_dist )    # dollar risk constraint\n"
    "qty_by_cap  = floor( cap / entry_price )                # notional cap constraint\n"
    "qty         = min(qty_by_risk, qty_by_cap)\n"
    "REJECT if qty × entry_price < ₹40,000  (position floor)"
)
table(
    ["Tier", "risk_pct", "Dollar risk @ ₹25L", "Notional cap"],
    [
        ["T1_MULTI_STRONG", "1.50%", "₹37,500", "₹3,00,000"],
        ["T2_STRONG_REG",   "1.00%", "₹25,000", "₹2,50,000"],
        ["T3_MULTI_REG",    "0.66%", "₹16,500", "₹1,50,000"],
        ["T4_RS_ACCEL",     "0.50%", "₹12,500", "₹1,00,000"],
    ],
    [4.5, 2.5, 4.0, 4.0],
)

# ──────────────────────────────────────────────────────────────────────
# 2. Shared Setup
# ──────────────────────────────────────────────────────────────────────
h1("2. Shared Worked-Example Setup")
para(
    "Every tier uses the SAME underlying stock so differences are purely from risk_pct, "
    "cap, and the partial-R ladder."
)
code_block(
    "Hypothetical stock: XYZ.NS\n"
    "  D1 09:15 first tick     = ₹499.25\n"
    "  Slippage 15bps          → entry_price = ₹500.00\n"
    "  ATR_14 (from D0)        = ₹15.00       (3% of price — a typical mid/large cap)\n"
    "  stop_dist  (2 × ATR)    = ₹30.00\n"
    "  initial_stop            = ₹470.00\n"
    "  R                       = ₹30 per share\n"
    "  1R as % of entry        = 6.0%\n"
    "  EARLY_TRAIL trigger     = entry × 1.10 = ₹550.00"
)
para("Six exit scenarios per tier (one new since v1):", bold=True)
bullet("A — Trend works: hits both R-partials, residual trails up to +9R then stops.")
bullet("B — Pop then reverse: 1st partial fills, breakeven stop catches the pullback.")
bullet("C — Stopped cold: straight down to initial stop. Worst case.")
bullet("D — Vertical pop: +25% inside 15 trading days → universal 25% book triggers.")
bullet("E — Time stop: 25 td of dead money, returns inside [−2%, +2%].")
bullet("F — Early-trail save (NEW): pop to +11% then reverses BEFORE any formal R-partial. Without the +10% rule this would round-trip to −1R. With the rule, the trail arms, stop floors at entry, and we exit at a small profit on the trail next session.")


def render_tier(section_no, tier_name, tier_label, risk_pct, risk_pct_pretty, cap, partial_R):
    sleeve = 2_500_000; entry = 500.0; atr = 15.0
    stop_dist = 30.0; R = 30.0
    R1, R2 = partial_R

    h1(f"{section_no}. {tier_name} — {tier_label}")

    # ─── Sizing ───
    h2("Position sizing")
    risk_inr = sleeve * risk_pct
    qty_by_risk = int(risk_inr // stop_dist)
    qty_by_cap = int(cap // entry)
    qty = max(0, min(qty_by_risk, qty_by_cap))
    bind = ("notional cap" if qty == qty_by_cap and qty < qty_by_risk
            else "dollar risk" if qty == qty_by_risk and qty < qty_by_cap
            else "tie")
    notional = qty * entry
    actual_risk = qty * R

    code_block(
        f"risk_inr     = ₹25,00,000 × {risk_pct_pretty}  = {fmt_inr(risk_inr)}\n"
        f"qty_by_risk  = floor( {fmt_inr(risk_inr)} / ₹30 )  = {qty_by_risk}\n"
        f"qty_by_cap   = floor( {fmt_inr(cap)} / ₹500 )       = {qty_by_cap}\n"
        f"qty          = min({qty_by_risk}, {qty_by_cap})  = {qty}   ← {bind} binds\n"
        f"notional     = {qty} × ₹500  = {fmt_inr(notional)}\n"
        f"actual_risk  = {qty} × ₹30   = {fmt_inr(actual_risk)}   ({actual_risk/sleeve*100:.2f}% of sleeve)"
    )

    # ─── Stop + R ladder ───
    h2("Stop, R ladder, and early-trail trigger")
    t1_price = entry + R1 * R
    t2_price = entry + R2 * R
    partial_qty = max(1, int(qty * 33 / 100))
    residual_qty = qty - 2 * partial_qty
    one_R_pct = R / entry * 100  # = 6%

    early_pos = "BEFORE the 1st R-partial" if R1 * one_R_pct > 10 else "AFTER the 1st R-partial fires"
    code_block(
        f"initial_stop  = ₹500 − (2 × ₹15)  = ₹470.00   (−6% / −1R)\n"
        f"R             = ₹500 − ₹470       = ₹30 per share  (1R = 6.0% of entry)\n\n"
        f"1st partial:   +{R1}R = ₹{t1_price:.2f}  ({R1*one_R_pct:+.1f}%)\n"
        f"2nd partial:   +{R2}R = ₹{t2_price:.2f}  ({R2*one_R_pct:+.1f}%)\n"
        f"EARLY_TRAIL:   +10% = ₹550.00    ← fires {early_pos}\n\n"
        f"partial_qty   = floor({qty} × 33%) = {partial_qty} shares per R-partial\n"
        f"residual_qty  = {qty} − 2 × {partial_qty} = {residual_qty} shares (trails)"
    )
    if R1 * one_R_pct > 10:
        para(
            f"Because 1st formal partial sits at +{R1*one_R_pct:.1f}% (above +10%), the "
            "EARLY_TRAIL rule will fire FIRST on any trade that crosses +10%. The trail "
            "arms and the stop floors at entry well before the formal partial target.",
            italic=True,
        )
    else:
        para(
            f"Because 1st formal partial sits at +{R1*one_R_pct:.1f}% (below or near +10%), "
            "the formal partial will fire first on most trades. The EARLY_TRAIL rule is "
            "effectively a no-op here — but it provides a useful safety net for trades "
            "that pop to +10% and reverse before the formal partial.",
            italic=True,
        )

    # ─── Scenario A ───
    h2("Scenario A — Trend works (best case)")
    para(
        "Price rallies. At +10% (₹550), EARLY_TRAIL arms early — stop floors at entry, "
        "trail starts ratcheting daily. At ₹{:.2f} (+{}R) the 1st partial fires. At "
        "₹{:.2f} (+{}R) the 2nd partial fires. Residual {} shares ride the trail "
        "until it catches at ~+9R (₹770).".format(t1_price, R1, t2_price, R2, residual_qty)
    )
    trail_exit = entry + 9 * R
    pnl_p1 = (t1_price - entry) * partial_qty
    pnl_p2 = (t2_price - entry) * partial_qty
    pnl_res = (trail_exit - entry) * residual_qty
    total_A = pnl_p1 + pnl_p2 + pnl_res
    table(
        ["Milestone", "Price", "Qty", "P&L", "Cumulative P&L"],
        [
            ["EARLY_TRAIL armed @ +10%", "₹550.00", "—", "(no booking)", fmt_inr(0)],
            [f"1st partial at +{R1}R",    f"₹{t1_price:.2f}", str(partial_qty), fmt_inr(pnl_p1), fmt_inr(pnl_p1)],
            [f"2nd partial at +{R2}R",    f"₹{t2_price:.2f}", str(partial_qty), fmt_inr(pnl_p2), fmt_inr(pnl_p1 + pnl_p2)],
            ["Trail residual @ +9R",      f"₹{trail_exit:.2f}", str(residual_qty), fmt_inr(pnl_res), fmt_inr(total_A)],
        ],
        [6.0, 2.5, 2.0, 3.0, 3.5],
    )
    callout(f"Scenario A total: {fmt_inr(total_A)}  ({total_A/actual_risk:.2f}R)")

    # ─── Scenario B ───
    h2("Scenario B — Pop then reverse")
    para(
        f"Hits +{R1}R (₹{t1_price:.2f}), 1st partial fires (33% booked, breakeven armed). "
        "Trail also armed early at +10% on the way up, so stop is already trailing. Price "
        "reverses, trailing stop catches around breakeven on the remaining qty."
    )
    pnl_B_p1 = (t1_price - entry) * partial_qty
    total_B = pnl_B_p1
    table(
        ["Milestone", "Price", "Qty", "P&L", "Cumulative P&L"],
        [
            ["EARLY_TRAIL armed @ +10%", "₹550.00", "—", "(no booking)", fmt_inr(0)],
            [f"1st partial at +{R1}R",   f"₹{t1_price:.2f}", str(partial_qty), fmt_inr(pnl_B_p1), fmt_inr(pnl_B_p1)],
            ["Trail stop catches BE",    "₹500.00", str(qty - partial_qty), fmt_inr(0), fmt_inr(total_B)],
        ],
        [6.0, 2.5, 2.0, 3.0, 3.5],
    )
    callout(f"Scenario B total: {fmt_inr(total_B)}  ({total_B/actual_risk:.2f}R)")

    # ─── Scenario C ───
    h2("Scenario C — Stopped cold (worst case)")
    para("Price drops straight to the initial stop. Never reaches +10%, so EARLY_TRAIL never fires.")
    pnl_C = -actual_risk
    table(
        ["Milestone", "Price", "Qty", "P&L", "Cumulative P&L"],
        [["Initial stop hit", "₹470.00", str(qty), fmt_inr(pnl_C), fmt_inr(pnl_C)]],
        [6.0, 2.5, 2.0, 3.0, 3.5],
    )
    callout(f"Scenario C total: {fmt_inr(pnl_C)}  (−1.00R)")

    # ─── Scenario D ───
    h2("Scenario D — Vertical pop (universal 25% rule)")
    para(
        "Price gains +25% (to ₹625) within 15 td. EARLY_TRAIL fires at +10% on the way "
        "up. Universal book takes 25% of initial qty at ₹625. Trade continues to +R2 "
        f"(₹{t2_price:.2f}) and trails out at +9R (₹770)."
    )
    uni_qty = max(1, int(qty * 25 / 100))
    pnl_uni = (625 - entry) * uni_qty
    remain = qty - uni_qty
    p1_q = min(partial_qty, remain); pnl_D_p1 = (t1_price - entry) * p1_q; remain -= p1_q
    p2_q = min(partial_qty, remain); pnl_D_p2 = (t2_price - entry) * p2_q; remain -= p2_q
    pnl_D_res = (trail_exit - entry) * remain
    total_D = pnl_uni + pnl_D_p1 + pnl_D_p2 + pnl_D_res
    table(
        ["Milestone", "Price", "Qty", "P&L", "Cumulative P&L"],
        [
            ["EARLY_TRAIL armed @ +10%", "₹550.00", "—", "(no booking)", fmt_inr(0)],
            ["Universal 25% book",       "₹625.00", str(uni_qty), fmt_inr(pnl_uni), fmt_inr(pnl_uni)],
            [f"1st partial at +{R1}R",   f"₹{t1_price:.2f}", str(p1_q), fmt_inr(pnl_D_p1), fmt_inr(pnl_uni + pnl_D_p1)],
            [f"2nd partial at +{R2}R",   f"₹{t2_price:.2f}", str(p2_q), fmt_inr(pnl_D_p2), fmt_inr(pnl_uni + pnl_D_p1 + pnl_D_p2)],
            ["Trail residual @ +9R",     f"₹{trail_exit:.2f}", str(remain), fmt_inr(pnl_D_res), fmt_inr(total_D)],
        ],
        [6.0, 2.5, 2.0, 3.0, 3.5],
    )
    callout(f"Scenario D total: {fmt_inr(total_D)}  ({total_D/actual_risk:.2f}R)")

    # ─── Scenario E ───
    h2("Scenario E — Time stop (dead money)")
    para(
        "25 td later, price = ₹505 (inside [₹490, ₹510]). Never hit +10% high during the "
        "window, so EARLY_TRAIL never fired. Time stop closes everything at close."
    )
    pnl_E = (505 - entry) * qty
    table(
        ["Milestone", "Price", "Qty", "P&L", "Cumulative P&L"],
        [["Time stop @ day 25", "₹505.00", str(qty), fmt_inr(pnl_E), fmt_inr(pnl_E)]],
        [6.0, 2.5, 2.0, 3.0, 3.5],
    )
    callout(f"Scenario E total: {fmt_inr(pnl_E)}  ({pnl_E/actual_risk:+.2f}R)")

    # ─── Scenario F (NEW) ───
    h2("Scenario F — Early-trail save (NEW)")
    para(
        f"Price pops to bar-high ₹555 (+11% MFE), closes at ₹550 (+10%). EARLY_TRAIL arms: "
        f"stop floors at entry, trail update sets new stop = 550 − 30 = ₹520 (+4%). Next "
        f"session price gaps down and trips ₹520 intraday. Railway closes the trade at LTP "
        f"₹520. WITHOUT the EARLY_TRAIL rule, this trade would have round-tripped past entry "
        f"all the way to initial stop ₹470 = −1R."
    )
    early_exit = 520.0
    pnl_F = (early_exit - entry) * qty
    pnl_F_without = -actual_risk
    delta = pnl_F - pnl_F_without
    table(
        ["Milestone", "Price", "Qty", "P&L", "Cumulative P&L"],
        [
            ["MFE hits +11%; EARLY_TRAIL armed", "₹555.00 high", "—", "(no booking)", fmt_inr(0)],
            ["Trail stop set at close − 2×ATR",  "stop = ₹520",  "—", "—",            "—"],
            ["Next session: trail stop hits",    "₹520.00",      str(qty), fmt_inr(pnl_F), fmt_inr(pnl_F)],
        ],
        [6.0, 2.5, 2.0, 3.0, 3.5],
    )
    callout(
        f"Scenario F total: {fmt_inr(pnl_F)}  ({pnl_F/actual_risk:+.2f}R)  |  "
        f"Without EARLY_TRAIL: {fmt_inr(pnl_F_without)} (−1.00R)  |  "
        f"Difference: {fmt_inr(delta)} ({delta/actual_risk:+.2f}R)"
    )

    # ─── Tier summary ───
    h2("Scenario summary for this tier")
    table(
        ["Scenario", "Total P&L", "R-multiple", "% of sleeve"],
        [
            ["A — Trend works",        fmt_inr(total_A), f"+{total_A/actual_risk:.2f}R", f"{total_A/sleeve*100:+.2f}%"],
            ["B — Pop then reverse",   fmt_inr(total_B), f"+{total_B/actual_risk:.2f}R", f"{total_B/sleeve*100:+.2f}%"],
            ["C — Stopped cold",       fmt_inr(pnl_C),   "−1.00R",                       f"{pnl_C/sleeve*100:+.2f}%"],
            ["D — Vertical pop",       fmt_inr(total_D), f"+{total_D/actual_risk:.2f}R", f"{total_D/sleeve*100:+.2f}%"],
            ["E — Time stop",          fmt_inr(pnl_E),   f"{pnl_E/actual_risk:+.2f}R",    f"{pnl_E/sleeve*100:+.2f}%"],
            ["F — Early-trail save",   fmt_inr(pnl_F),   f"{pnl_F/actual_risk:+.2f}R",    f"{pnl_F/sleeve*100:+.2f}%"],
        ],
        [6.0, 3.5, 3.0, 3.0],
    )

    return {
        'tier': tier_name, 'qty': qty, 'notional': notional, 'risk': actual_risk,
        'A': total_A, 'B': total_B, 'C': pnl_C, 'D': total_D, 'E': pnl_E, 'F': pnl_F,
    }


t1 = render_tier(3, "Tier T1", "Multi-Strong (highest conviction)", 0.015, "1.50%", 300_000, (3, 6))
t2 = render_tier(4, "Tier T2", "Strong + Regular", 0.010, "1.00%", 250_000, (2, 4))
t3 = render_tier(5, "Tier T3", "Multi-Regular", 0.0066, "0.66%", 150_000, (2, 4))
t4 = render_tier(6, "Tier T4", "RS Acceleration (presignal-only)", 0.005, "0.50%", 100_000, (2, 4))

# ──────────────────────────────────────────────────────────────────────
# 7. Side-by-side
# ──────────────────────────────────────────────────────────────────────
h1("7. Side-by-Side: Same Stock, All Tiers")
para(
    "Same hypothetical XYZ trade (entry ₹500, ATR ₹15, R = ₹30) sized and run through "
    "all six scenarios for each tier."
)
table(
    ["Metric", "T1", "T2", "T3", "T4"],
    [
        ["Qty (shares)",   t1['qty'], t2['qty'], t3['qty'], t4['qty']],
        ["Notional",       fmt_inr(t1['notional']), fmt_inr(t2['notional']), fmt_inr(t3['notional']), fmt_inr(t4['notional'])],
        ["Actual ₹ risk",  fmt_inr(t1['risk']),     fmt_inr(t2['risk']),     fmt_inr(t3['risk']),     fmt_inr(t4['risk'])],
        ["1st partial",    "₹590 (+3R)", "₹560 (+2R)", "₹560 (+2R)", "₹560 (+2R)"],
        ["2nd partial",    "₹680 (+6R)", "₹620 (+4R)", "₹620 (+4R)", "₹620 (+4R)"],
        ["EARLY_TRAIL",    "₹550 (+10% — fires BEFORE 1st partial)",
                           "₹550 (+10% — fires BEFORE 1st partial)",
                           "₹550 (+10% — fires BEFORE 1st partial)",
                           "₹550 (+10% — fires BEFORE 1st partial)"],
        ["A — Trend works",       fmt_inr(t1['A']), fmt_inr(t2['A']), fmt_inr(t3['A']), fmt_inr(t4['A'])],
        ["B — Pop & reverse",     fmt_inr(t1['B']), fmt_inr(t2['B']), fmt_inr(t3['B']), fmt_inr(t4['B'])],
        ["C — Stopped cold",      fmt_inr(t1['C']), fmt_inr(t2['C']), fmt_inr(t3['C']), fmt_inr(t4['C'])],
        ["D — Vertical pop",      fmt_inr(t1['D']), fmt_inr(t2['D']), fmt_inr(t3['D']), fmt_inr(t4['D'])],
        ["E — Time stop",         fmt_inr(t1['E']), fmt_inr(t2['E']), fmt_inr(t3['E']), fmt_inr(t4['E'])],
        ["F — Early-trail save",  fmt_inr(t1['F']), fmt_inr(t2['F']), fmt_inr(t3['F']), fmt_inr(t4['F'])],
    ],
    [4.5, 3.0, 3.0, 3.0, 3.0],
)
para("Key observations:", bold=True)
bullet(
    "For this ₹500 / ATR ₹15 stock, EARLY_TRAIL at +10% sits BELOW the formal 1st partial "
    "for every tier (T1 = +18%, T2/T3/T4 = +12%). So in every winning scenario, the trail "
    "arms first and locks in entry as the worst case before any R-target fires.")
bullet(
    "Scenario F is the new mechanic to watch. Without EARLY_TRAIL, every tier would have "
    "lost −1R (its full dollar risk) on this trade. With the rule, every tier exits at +4% "
    "= roughly +2/3R. Difference per trade: T1 saves ₹27,000, T4 saves ₹9,000.")
bullet(
    "Scenarios A and D look identical to v1 in P&L terms — the trail catches at the same "
    "+9R level regardless of when it armed. The difference is only visible on trades that "
    "fail to reach the formal R-partials (Scenario F).")
bullet(
    "Worst case (C) is unchanged. The trade has to actually print +10% before EARLY_TRAIL "
    "can fire; a direct drop to the stop never crosses that threshold.")

# ──────────────────────────────────────────────────────────────────────
# 8. High-ATR case
# ──────────────────────────────────────────────────────────────────────
h1("8. What if the stock is more volatile? (High-ATR case)")
para(
    "All numbers above assume ATR = 3% of price. For a higher-ATR name, stop_dist gets "
    "bigger AND partial targets live further away. Here is where EARLY_TRAIL really earns "
    "its keep — without it, the geometry is genuinely broken for high-ATR T1."
)
code_block(
    "Hypothetical stock: ZZZ.NS  (small/mid-cap, higher ATR)\n"
    "  entry_price             = ₹500.00\n"
    "  ATR_14                  = ₹40.00       (8% of price)\n"
    "  stop_dist  (2 × ATR)    = ₹80.00\n"
    "  initial_stop            = ₹420.00      (−16% / −1R)\n"
    "  R                       = ₹80 per share  (1R = 16% of entry)\n"
    "  1st partial T1 (3R)     = ₹740          (+48%)\n"
    "  1st partial T2/T3/T4    = ₹660          (+32%)\n"
    "  EARLY_TRAIL trigger     = ₹550          (+10% — fires WELL before any partial)"
)

para(
    "In this scenario the formal R-ladder is so far out that without EARLY_TRAIL almost "
    "every trade that fails to make a 32-48% run would round-trip to −16%. With "
    "EARLY_TRAIL, any trade that prints +10% locks in entry as the floor; from there the "
    "trail ratchets up 2×ATR (₹80) below close — so the locked-in profit grows as the "
    "trade extends."
)

sleeve_hi = 2_500_000; entry_hi = 500.0; stop_dist_hi = 80.0; R_hi = 80.0
rows_hi = []
for tier, rp, cap in [("T1", 0.015, 300_000), ("T2", 0.010, 250_000), ("T3", 0.0066, 150_000), ("T4", 0.005, 100_000)]:
    qty_r = int((sleeve_hi * rp) // stop_dist_hi)
    qty_c = int(cap // entry_hi)
    qty = min(qty_r, qty_c)
    bind = "RISK" if qty_r <= qty_c else "CAP"
    notional = qty * entry_hi
    actual_risk = qty * R_hi
    floor_ok = "OK" if notional >= 40_000 else "REJECTED"
    rows_hi.append([tier, qty_r, qty_c, qty, bind, fmt_inr(notional),
                    fmt_inr(actual_risk), floor_ok])

table(
    ["Tier", "qty_by_risk", "qty_by_cap", "Final qty", "Binds", "Notional", "Risk", "Floor"],
    rows_hi,
    [1.8, 2.2, 2.2, 2.0, 1.6, 2.5, 2.0, 2.0],
)

para("Scenario F (early-trail save) on this volatile stock:", bold=True)
para(
    "Stock pops to +11% high (₹555), closes at +10% (₹550). EARLY_TRAIL arms: stop = "
    "max(stop, entry) = ₹500, then trail update sets new stop = 550 − 80 = ₹470. But "
    "₹470 < ₹500, so stop stays at entry (₹500). Next session the trade gaps down to "
    "₹490 intraday — Railway closes at LTP ₹500 (the breakeven floor). Saved: −1R = "
    "−16% of position notional, which on T1 (468 shares × ₹500 = ₹2.34L notional) is "
    "₹37,440. THAT'S the real win of the rule on high-ATR stocks."
)

# ──────────────────────────────────────────────────────────────────────
# 9. MFE/MAE — what to look for in the data
# ──────────────────────────────────────────────────────────────────────
h1("9. MFE / MAE Tracking — What We're Building Toward")
para(
    "Every open and closed trade now has `max_unrealized_pct` (MFE) and `min_unrealized_pct` "
    "(MAE) populated daily. The dashboard surfaces these in both the Open Positions and "
    "Closed Trades tables. The Closed Trades table also computes a derived **Peak R** "
    "column (= MFE % ÷ R %), so you can immediately see how close a trade got to its "
    "formal partial targets."
)
para("After 30-60 days of paper trading we'll have enough data to answer:", bold=True)
bullet("Of trades that exited at a loss, what was their median MFE? If it's ≥ +10%, EARLY_TRAIL is well-placed. If it's lower, we might dial the threshold down (say to +7%).")
bullet("Of trades that hit the 1st R-partial, what fraction reached the 2nd? If it's < 30%, the 2nd partial sits too far out and the trail does most of the work.")
bullet("For each tier, what's the median Peak R achieved? If T1 trades typically peak at +2.5R while the 1st partial is at +3R, we have a geometry problem.")
bullet("How does Peak R differ between trail_armed_reason = 'partial_2' vs 'pct_threshold'? That tells us whether EARLY_TRAIL is helping or capping potential trends prematurely.")
bullet("How does MAE correlate with eventual outcome? If trades that printed MAE ≥ −0.7R rarely come back to win, that's a candidate for a tighter stop.")

para(
    "These are exactly the tuning levers we deferred earlier. The point of the "
    "instrument-first approach is that 30 days from now we tune from the MFE/MAE "
    "distribution, not from intuition.",
    italic=True,
)

# ──────────────────────────────────────────────────────────────────────
# 10. Exit reason codes
# ──────────────────────────────────────────────────────────────────────
h1("10. Exit Reason Codes")
table(
    ["exit_reason", "Triggered by", "What happened"],
    [
        ["stop",            "Intraday (Railway) or EOD batch",
         "Initial stop hit BEFORE any partial or trail. Scenario C."],
        ["trail_stop",      "Intraday (Railway) or EOD batch",
         "Stop hit AFTER trail was armed (formal 2nd partial OR EARLY_TRAIL). Scenario A's tail, Scenario F."],
        ["partials_full",   "EOD batch only",
         "Both partial targets filled in the same bar — no residual qty left."],
        ["time_stop",       "EOD batch",
         "25 trading days held, return inside [−2%, +2%]. Scenario E."],
        ["fill_expired",    "Railway fill job",
         "Pending row aged > 2 trading days with no live tick."],
        ["fill_rejected_position_floor", "Railway fill job",
         "D1 open moved enough that sizing produced qty=0 or notional < ₹40k."],
        ["fill_rejected_missing_atr",    "Railway fill job",
         "entry_atr was null on the pending row (data quality issue)."],
    ],
    [4.5, 4.0, 8.0],
)

para(
    "Tip: when exit_reason='trail_stop', check trail_armed_reason on the row to know "
    "whether the trail came from a formal 2nd partial ('partial_2') or from EARLY_TRAIL "
    "('pct_threshold'). The Peak R column tells you how far the trade actually ran."
)

doc.save(OUT)
print(f"Wrote: {OUT}")
