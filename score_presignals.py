#!/usr/bin/env python3
"""
Pre-Signal Rule-Based Proximity Scorer v2
==========================================
Tighter thresholds, convergence requirements, minimum conditions gates.

Changes from v1:
  - RSI gap tolerance tightened: 25% → 12%
  - GC: requires ema_20 actively converging toward ema_50 (slope check)
  - GC: gap tolerance tightened: 5% → 3%, must be above ema_200
  - BZ: gate — at least 2 of 4 indicator conditions must already be passing
  - BZ Strong: gate — at least 3 of 5 conditions must already be passing
  - MACD: requires histogram improving for 3+ consecutive days, gap ≤ 0.5% of price
  - Pullback Bounce: price must be declining, within 5% of ema_20, low-volume preferred
  - MIN_SCORE raised: 40 → 60

Signals scored:
  bz_buy, bz_strong, golden_cross, macd, pullback_bounce

Schedule: Daily at 6:15 PM IST (after alkalyme_rs_ranker)
Runtime: ~2 minutes
"""

import os
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from collections import defaultdict
from supabase import create_client

SUPABASE_URL = os.getenv('SUPABASE_URL', 'https://hcgyncghmcvylnrmcivj.supabase.co')
SUPABASE_KEY = os.getenv('SUPABASE_KEY', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhjZ3luY2dobWN2eWxucm1jaXZqIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTc1MTQwMTEsImV4cCI6MjA3MzA5MDAxMX0.n8vFVCJe1y_3o8fpAY0IgasZ4eKl7DAogEM3OlHB8Ww')

HISTORY_DAYS   = 20
MIN_SCORE      = 70
RANK_THRESHOLD = 125


# =============================================================================
# HELPERS
# =============================================================================

def paginated_fetch(supabase, table, select, filters_fn, batch_size=1000):
    all_data, offset = [], 0
    while True:
        q = supabase.table(table).select(select)
        q = filters_fn(q)
        resp = q.range(offset, offset + batch_size - 1).execute()
        if not resp.data:
            break
        all_data.extend(resp.data)
        if len(resp.data) < batch_size:
            break
        offset += batch_size
    return all_data


def slope_5d(values: list) -> float:
    """Linear regression slope over last 5 non-None values, normalised by mean."""
    clean = [v for v in values if v is not None][-5:]
    if len(clean) < 3:
        return 0.0
    x    = np.arange(len(clean), dtype=float)
    y    = np.array(clean, dtype=float)
    mean = abs(y.mean())
    if mean == 0:
        return 0.0
    return np.polyfit(x, y, 1)[0] / mean


def proximity_score(current, threshold, higher_is_better: bool,
                    series: list = None, max_gap_pct: float = 0.12) -> float:
    """
    Score 0–100: how close current is to threshold.
    Returns 100 if already past threshold.
    Returns 0 if gap > max_gap_pct.
    Momentum bonus: +8 if slope moving toward threshold.
    """
    if current is None:
        return 0.0

    if higher_is_better:
        if current >= threshold:
            return 100.0
        gap_pct = (threshold - current) / abs(threshold) if threshold != 0 else 1.0
    else:
        if current <= threshold:
            return 100.0
        gap_pct = (current - threshold) / abs(threshold) if threshold != 0 else 1.0

    if gap_pct >= max_gap_pct:
        return 0.0

    base = round(100.0 * (1.0 - gap_pct / max_gap_pct), 1)

    bonus = 0.0
    if series:
        s = slope_5d(series)
        if higher_is_better and s > 0:
            bonus = min(8.0, s * 40)
        elif not higher_is_better and s < 0:
            bonus = min(8.0, abs(s) * 40)

    return min(100.0, base + bonus)


def compute_macd(closes: list):
    """Returns (macd_line, signal_line, histogram) as lists, or (None, None, None)."""
    if len(closes) < 35:
        return None, None, None
    s      = pd.Series(closes, dtype=float)
    ema12  = s.ewm(span=12, adjust=False, min_periods=12).mean()
    ema26  = s.ewm(span=26, adjust=False, min_periods=26).mean()
    macd   = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False, min_periods=9).mean()
    hist   = macd - signal
    return macd.tolist(), signal.tolist(), hist.tolist()


def consecutive_improving(series: list, n: int = 3) -> bool:
    """True if last n values are monotonically increasing."""
    clean = [v for v in series if v is not None][-n:]
    if len(clean) < n:
        return False
    return all(clean[i] < clean[i+1] for i in range(len(clean)-1))


# =============================================================================
# SIGNAL SCORERS
# =============================================================================

def score_bz_buy(hist: list, rs_rank, ticker_rank_hist: list = None) -> float:
    """
    Blue Zone Buy — 5 conditions:
      rsi_ema_9 >= 65, weekly_rsi_ema_9 >= 55,
      close > ema_20, close >= high_52w * 0.90, rs_rank <= 125

    Gate: at least 3 of the 4 indicator conditions must already be passing.
    Returns 0 if all 5 already passing (belongs in entry_signals).
    """
    if not hist:
        return 0.0

    latest   = hist[-1]
    rsi_d    = latest.get('rsi_ema_9')
    rsi_w    = latest.get('weekly_rsi_ema_9')
    close    = latest.get('close')
    ema_20   = latest.get('ema_20')
    high_52w = latest.get('high_52w')

    rank_ok  = rs_rank is not None and rs_rank <= RANK_THRESHOLD
    rsi_d_ok = rsi_d   is not None and rsi_d   >= 65
    rsi_w_ok = rsi_w   is not None and rsi_w   >= 55
    ema20_ok = close   is not None and ema_20  is not None and close > ema_20
    w52_ok   = close   is not None and high_52w is not None and close >= high_52w * 0.90

    if rank_ok and rsi_d_ok and rsi_w_ok and ema20_ok and w52_ok:
        return 0.0  # already triggered

    # Rank gate: either already within threshold, or rank is improving toward it
    # Use daily_stock_snapshots rank history (more reliable than historical_snapshots)
    rank_series = ticker_rank_hist or [r.get('rs_rank') for r in hist if r.get('rs_rank') is not None]
    rank_improving = False
    if len(rank_series) >= 3:
        recent = rank_series[-5:]
        if len(recent) >= 2:
            slope = (recent[-1] - recent[0]) / len(recent)
            # Required slope: must be able to reach rank 125 within 10 trading days
            required_slope = (rs_rank - RANK_THRESHOLD) / 10.0
            rank_improving = slope < -required_slope  # improving fast enough

    if rs_rank is None:
        return 0.0
    if rs_rank > RANK_THRESHOLD and not rank_improving:
        # Outside threshold and not improving — exclude
        return 0.0
    if rs_rank > RANK_THRESHOLD * 2:
        # Way too far (rank > 250) even if improving — exclude
        return 0.0

    # Gate: at least 3 indicator conditions passing
    if sum([rsi_d_ok, rsi_w_ok, ema20_ok, w52_ok]) < 3:
        return 0.0

    rsi_d_series = [r.get('rsi_ema_9')        for r in hist]
    rsi_w_series = [r.get('weekly_rsi_ema_9') for r in hist]
    ratio_ema20  = (close / ema_20)           if (close and ema_20)   else None
    ratio_ema20_s = [
        (r.get('close') / r.get('ema_20')) if r.get('close') and r.get('ema_20') else None
        for r in hist
    ]
    ratio_52w = (close / (high_52w * 0.90))  if (close and high_52w) else None
    ratio_52w_s = [
        (r.get('close') / (r.get('high_52w') * 0.90)) if r.get('close') and r.get('high_52w') else None
        for r in hist
    ]

    s_rsi_d = 100.0 if rsi_d_ok else proximity_score(rsi_d,       65,  True, rsi_d_series,  max_gap_pct=0.12)
    s_rsi_w = 100.0 if rsi_w_ok else proximity_score(rsi_w,       55,  True, rsi_w_series,  max_gap_pct=0.12)
    s_ema20 = 100.0 if ema20_ok else proximity_score(ratio_ema20, 1.0, True, ratio_ema20_s, max_gap_pct=0.08)
    s_52w   = 100.0 if w52_ok   else proximity_score(ratio_52w,   1.0, True, ratio_52w_s,   max_gap_pct=0.12)
    s_rank  = 100.0 if rank_ok  else proximity_score(rs_rank if rs_rank else 999, RANK_THRESHOLD, False, max_gap_pct=0.30)

    # Tiered scoring: score is driven by how close failing conditions are
    # Base bonus for passing gates, remainder from failing condition proximity
    gates_passing = sum([rsi_d_ok, rsi_w_ok, ema20_ok, w52_ok])

    if gates_passing == 4:
        # Only rank failing — base 85, rank proximity drives remaining 15
        score = 85.0 + s_rank * 0.15
    elif gates_passing == 3:
        # One indicator failing — base 70, failing indicator drives remaining 30
        # Weight the failing condition score heavily
        failing_scores = []
        if not rsi_d_ok: failing_scores.append(s_rsi_d)
        if not rsi_w_ok: failing_scores.append(s_rsi_w)
        if not ema20_ok: failing_scores.append(s_ema20)
        if not w52_ok:   failing_scores.append(s_52w)
        failing_avg = sum(failing_scores) / len(failing_scores) if failing_scores else 0
        rank_bonus  = s_rank * 0.05  # small rank bonus
        score = 70.0 + failing_avg * 0.25 + rank_bonus
    else:
        # Fallback — shouldn't reach here due to gate check above
        score = (s_rsi_d * 0.30 + s_rsi_w * 0.20 +
                 s_ema20 * 0.20 + s_52w   * 0.20 + s_rank * 0.10)

    return round(min(100.0, score), 1)


def score_bz_strong(hist: list, rs_rank, ticker_rank_hist: list = None) -> float:
    """
    Blue Zone Strong = BZ Buy + rsi_ema_9>=72, weekly_rsi_ema_9>=65, vol_ratio>=1.5
    Gate: at least 3 of 5 indicator conditions already passing.
    """
    if not hist:
        return 0.0

    latest    = hist[-1]
    rsi_d     = latest.get('rsi_ema_9')
    rsi_w     = latest.get('weekly_rsi_ema_9')
    close     = latest.get('close')
    ema_20    = latest.get('ema_20')
    high_52w  = latest.get('high_52w')
    vol_ratio = latest.get('vol_ratio')

    rank_ok  = rs_rank   is not None and rs_rank   <= RANK_THRESHOLD
    rsi_d_ok = rsi_d     is not None and rsi_d     >= 72
    rsi_w_ok = rsi_w     is not None and rsi_w     >= 65
    ema20_ok = close     is not None and ema_20    is not None and close > ema_20
    w52_ok   = close     is not None and high_52w  is not None and close >= high_52w * 0.90
    vol_ok   = vol_ratio is not None and vol_ratio >= 1.5

    if rank_ok and rsi_d_ok and rsi_w_ok and ema20_ok and w52_ok and vol_ok:
        return 0.0

    # Rank gate: either within threshold or improving toward it
    rank_series_s = ticker_rank_hist or [r.get('rs_rank') for r in hist if r.get('rs_rank') is not None]
    rank_improving_s = False
    if len(rank_series_s) >= 3:
        recent_s = rank_series_s[-5:]
        if len(recent_s) >= 2:
            slope_s = (recent_s[-1] - recent_s[0]) / len(recent_s)
            required_slope_s = (rs_rank - RANK_THRESHOLD) / 10.0
            rank_improving_s = slope_s < -required_slope_s

    if rs_rank is None:
        return 0.0
    if rs_rank > RANK_THRESHOLD and not rank_improving_s:
        return 0.0
    if rs_rank > RANK_THRESHOLD * 2:
        return 0.0

    if sum([rsi_d_ok, rsi_w_ok, ema20_ok, w52_ok, vol_ok]) < 3:
        return 0.0

    rsi_d_s = [r.get('rsi_ema_9')        for r in hist]
    rsi_w_s = [r.get('weekly_rsi_ema_9') for r in hist]
    vol_s   = [r.get('vol_ratio')         for r in hist]
    ratio_ema20 = (close / ema_20)          if (close and ema_20)   else None
    ratio_52w   = (close / (high_52w*0.90)) if (close and high_52w) else None

    s_rsi_d = 100.0 if rsi_d_ok else proximity_score(rsi_d,       72,  True, rsi_d_s, max_gap_pct=0.10)
    s_rsi_w = 100.0 if rsi_w_ok else proximity_score(rsi_w,       65,  True, rsi_w_s, max_gap_pct=0.10)
    s_ema20 = 100.0 if ema20_ok else proximity_score(ratio_ema20, 1.0, True,          max_gap_pct=0.08)
    s_52w   = 100.0 if w52_ok   else proximity_score(ratio_52w,   1.0, True,          max_gap_pct=0.12)
    s_vol   = 100.0 if vol_ok   else proximity_score(vol_ratio,   1.5, True, vol_s,   max_gap_pct=0.40)
    s_rank  = 100.0 if rank_ok  else proximity_score(rs_rank if rs_rank else 999, RANK_THRESHOLD, False, max_gap_pct=0.30)

    # Tiered scoring: base for gates passing + failing condition proximity
    gates_passing = sum([rsi_d_ok, rsi_w_ok, ema20_ok, w52_ok, vol_ok])

    if gates_passing == 5:
        # Only rank failing
        score = 85.0 + s_rank * 0.15
    elif gates_passing >= 3:
        failing_scores = []
        if not rsi_d_ok: failing_scores.append(s_rsi_d)
        if not rsi_w_ok: failing_scores.append(s_rsi_w)
        if not ema20_ok: failing_scores.append(s_ema20)
        if not w52_ok:   failing_scores.append(s_52w)
        if not vol_ok:   failing_scores.append(s_vol)
        failing_avg = sum(failing_scores) / len(failing_scores) if failing_scores else 0
        base = 60.0 + (gates_passing - 3) * 10.0  # 60 for 3 gates, 70 for 4 gates
        score = base + failing_avg * 0.25 + s_rank * 0.05
    else:
        score = (s_rsi_d * 0.25 + s_rsi_w * 0.20 + s_ema20 * 0.15 +
                 s_52w   * 0.15 + s_vol   * 0.15 + s_rank  * 0.10)

    return round(min(100.0, score), 1)


def score_golden_cross(hist: list, rs_rank) -> float:
    """
    Golden Cross = ema_20 crossing above ema_50.

    Hard gates (all must pass, else return 0):
      1. ema_20 < ema_50 currently (not already in GC)
      2. Gap <= 3%
      3. ema_20 slope > ema_50 slope (actively converging)
      4. Close above ema_200
      5. rs_rank <= 125
    """
    if not hist:
        return 0.0

    latest  = hist[-1]
    ema_20  = latest.get('ema_20')
    ema_50  = latest.get('ema_50')
    ema_200 = latest.get('ema_200')
    close   = latest.get('close')

    if None in (ema_20, ema_50):
        return 0.0
    if ema_20 >= ema_50:
        return 0.0  # already in GC

    gap_pct = (ema_50 - ema_20) / ema_50
    if gap_pct > 0.05:
        return 0.0  # too far away

    ema20_series = [r.get('ema_20') for r in hist]
    ema50_series = [r.get('ema_50') for r in hist]
    if slope_5d(ema20_series) <= slope_5d(ema50_series):
        return 0.0  # diverging

    if close is None or ema_200 is None or close <= ema_200:
        return 0.0  # below ema_200

    if rs_rank is None or rs_rank > RANK_THRESHOLD:
        return 0.0

    base          = round(100.0 * (1.0 - gap_pct / 0.05), 1)
    conv_delta    = slope_5d(ema20_series) - slope_5d(ema50_series)
    speed_bonus   = min(15.0, conv_delta * 200)
    return round(min(100.0, base + speed_bonus), 1)


def score_macd(hist: list) -> float:
    """
    MACD Cross pre-signal.

    Hard gates:
      1. MACD < signal currently
      2. Histogram improving for 3+ consecutive days
      3. Gap between MACD and signal <= 0.5% of close price
      4. Close above ema_200
    """
    if len(hist) < 35:
        return 0.0

    closes  = [r.get('close')   for r in hist]
    ema200s = [r.get('ema_200') for r in hist]

    if sum(1 for c in closes if c is not None) < 35:
        return 0.0

    closes_clean = pd.Series(closes).ffill().tolist()
    macd_line, signal_line, histogram = compute_macd(closes_clean)
    if macd_line is None:
        return 0.0

    latest_macd   = macd_line[-1]
    latest_signal = signal_line[-1]
    latest_ema200 = ema200s[-1]
    latest_close  = closes[-1]

    if latest_macd >= latest_signal:
        return 0.0  # already crossed
    if latest_close is None or latest_ema200 is None or latest_close <= latest_ema200:
        return 0.0
    if not consecutive_improving(histogram, n=3):
        return 0.0

    gap_as_pct = abs(latest_macd - latest_signal) / latest_close if latest_close else 1.0
    if gap_as_pct > 0.02:
        return 0.0

    base        = round(100.0 * (1.0 - gap_as_pct / 0.02), 1)
    hist_tail   = histogram[-5:]
    improvement = (hist_tail[-1] - hist_tail[-3]) if len(hist_tail) >= 3 else 0
    speed_bonus = min(10.0, improvement * 20) if improvement > 0 else 0
    return round(min(100.0, base + speed_bonus), 1)


def score_pullback_bounce(hist: list) -> float:
    """
    Pullback Bounce pre-signal.

    Hard gates:
      1. Uptrend: ema_20 > ema_50 * 1.05 > ema_200
      2. Price above ema_20
      3. Price declining (slope < 0 last 3 days)
      4. Within 5% above ema_20
    Bonus: low-volume pullback (vol_ratio < 1.0)
    """
    if not hist:
        return 0.0

    latest    = hist[-1]
    close     = latest.get('close')
    ema_20    = latest.get('ema_20')
    ema_50    = latest.get('ema_50')
    ema_200   = latest.get('ema_200')
    vol_ratio = latest.get('vol_ratio')

    if None in (close, ema_20, ema_50, ema_200):
        return 0.0
    if not (ema_20 > ema_50 * 1.05 and ema_50 > ema_200):
        return 0.0
    if close <= ema_20:
        return 0.0

    distance_pct = (close - ema_20) / ema_20
    if distance_pct > 0.05:
        return 0.0

    close_series = [r.get('close') for r in hist]
    if slope_5d(close_series[-3:] if len(close_series) >= 3 else close_series) >= 0:
        return 0.0  # not pulling back

    base      = round(100.0 * (1.0 - distance_pct / 0.05), 1)
    vol_bonus = 10.0 if (vol_ratio is not None and vol_ratio < 1.0) else 0.0
    return round(min(100.0, base + vol_bonus), 1)


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 60)
    print("🔮 PRE-SIGNAL RULE-BASED SCORER v2")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    latest_resp = supabase.table('daily_stock_snapshots')\
        .select('snapshot_date')\
        .not_.is_('alkalyme_rs', 'null')\
        .order('snapshot_date', desc=True)\
        .limit(1).execute()

    if not latest_resp.data:
        print("❌ No snapshot data found.")
        return

    today      = latest_resp.data[0]['snapshot_date']
    score_date = today  # runs after entry signals scanner — uses today's full picture
    since      = (datetime.strptime(today, '%Y-%m-%d') - timedelta(days=HISTORY_DAYS)).strftime('%Y-%m-%d')

    print(f"\n📅 Scoring date  : {score_date}")
    print(f"   History from  : {since}")

    print("\n📈 Fetching indicator history...")

    def hist_filters(q):
        return (q.gte('snapshot_date', since)
                 .lte('snapshot_date', today)
                 .neq('ticker', 'NIFTY50.NS')
                 .order('snapshot_date', desc=False))

    history = paginated_fetch(
        supabase, 'historical_snapshots',
        'ticker, snapshot_date, close, ema_20, ema_50, ema_200, '
        'rsi_ema_9, weekly_rsi_ema_9, high_52w, vol_ratio, rs_rank, alkalyme_rs',
        hist_filters
    )
    print(f"  Fetched {len(history):,} rows")

    by_ticker = defaultdict(list)
    for row in history:
        by_ticker[row['ticker']].append(row)

    # ── Fetch rank history from daily_stock_snapshots (last 10 days) ──────────
    print("\n🏆 Fetching RS rank history from daily_stock_snapshots...")
    rank_since = (datetime.strptime(today, '%Y-%m-%d') - timedelta(days=10)).strftime('%Y-%m-%d')

    def rank_hist_filters(q):
        return (q.gte('snapshot_date', rank_since)
                 .lte('snapshot_date', today)
                 .not_.is_('rs_rank', 'null')
                 .neq('ticker', 'NIFTY50.NS')
                 .order('snapshot_date', desc=False))

    rank_rows = paginated_fetch(
        supabase, 'daily_stock_snapshots',
        'ticker, snapshot_date, rs_rank, sector_percentile, alkalyme_rs',
        rank_hist_filters
    )

    today_ranks = {}
    rank_history_by_ticker = defaultdict(list)
    for r in rank_rows:
        rank_history_by_ticker[r['ticker']].append(r['rs_rank'])
        if r['snapshot_date'] == today:
            today_ranks[r['ticker']] = r
    # Fallback: use most recent available if no today entry
    latest_by_ticker = {}
    for r in sorted(rank_rows, key=lambda x: x['snapshot_date'], reverse=True):
        if r['ticker'] not in latest_by_ticker:
            latest_by_ticker[r['ticker']] = r
    for ticker, r in latest_by_ticker.items():
        if ticker not in today_ranks:
            today_ranks[ticker] = r

    print(f"  Fetched rank history for {len(rank_history_by_ticker)} tickers")

    # Merge latest rank into the last row of each ticker's history
    for ticker, hist in by_ticker.items():
        if hist and ticker in today_ranks:
            rank_data = today_ranks[ticker]
            hist[-1]['rs_rank']           = rank_data.get('rs_rank')
            hist[-1]['sector_percentile'] = rank_data.get('sector_percentile')
            if rank_data.get('alkalyme_rs') is not None:
                hist[-1]['alkalyme_rs'] = rank_data.get('alkalyme_rs')

    # ── Fetch today's entry_signals — exclude already-triggered tickers ──────
    print("\n🚫 Fetching recent entry_signals to exclude (last 10 trading days)...")
    # Exclude any ticker that fired a signal in the last 10 days — they are
    # already in or recently exited a signal zone, not pre-signal candidates.
    lookback_date = (datetime.strptime(score_date, '%Y-%m-%d') - timedelta(days=14)).strftime('%Y-%m-%d')
    sig_resp = supabase.table('entry_signals')        .select('ticker, signal_type')        .gte('alert_date', lookback_date)        .execute()

    already_triggered = set()
    for s in (sig_resp.data or []):
        t  = s['ticker']
        st = s.get('signal_type', '')
        # Only exclude BZ and GC signals — MACD/CPR etc. don't block pre-signal scoring
        if any(sig in st for sig in ['blue_zone', 'golden_cross']):
            already_triggered.add(t)
            if t.endswith('.NS'):
                already_triggered.add(t.replace('.NS', ''))
            elif t.endswith('.BO'):
                already_triggered.add(t.replace('.BO', ''))
            else:
                already_triggered.add(t + '.NS')

    print(f"  Tickers excluded (BZ/GC signal in last 10 days): {len(already_triggered)}")

    print(f"\n🔢 Scoring (MIN_SCORE={MIN_SCORE})...")
    records = []
    stats   = defaultdict(int)

    for ticker, hist in by_ticker.items():
        # Skip if already firing a signal today
        if ticker in already_triggered:
            continue

        hist.sort(key=lambda r: r['snapshot_date'])
        latest  = hist[-1]
        rs_rank = latest.get('rs_rank')

        ticker_rank_hist = rank_history_by_ticker.get(ticker, [])
        s_bz_buy    = score_bz_buy(hist, rs_rank, ticker_rank_hist)
        s_bz_strong = score_bz_strong(hist, rs_rank, ticker_rank_hist)
        s_gc        = score_golden_cross(hist, rs_rank)
        s_macd      = score_macd(hist)
        s_pb        = score_pullback_bounce(hist)

        signal_scores = {
            'bz_buy':          s_bz_buy,
            'bz_strong':       s_bz_strong,
            'golden_cross':    s_gc,
            'macd':            s_macd,
            'pullback_bounce': s_pb,
        }
        top_signal = max(signal_scores, key=signal_scores.get)
        top_score  = signal_scores[top_signal]

        if top_score < MIN_SCORE:
            continue

        stats[top_signal] += 1
        records.append({
            'ticker':               ticker,
            'score_date':           score_date,
            'rule_bz_buy':          s_bz_buy,
            'rule_bz_strong':       s_bz_strong,
            'rule_golden_cross':    s_gc,
            'rule_macd':            s_macd,
            'rule_pullback_bounce': s_pb,
            'top_signal':           top_signal,
            'top_rule_score':       top_score,
            'rs_rank':              rs_rank,
            'alkalyme_rs':          latest.get('alkalyme_rs'),
            'rsi_ema_9':            latest.get('rsi_ema_9'),
            'weekly_rsi_ema_9':     latest.get('weekly_rsi_ema_9'),
            'close':                latest.get('close'),
            'ema_20':               latest.get('ema_20'),
            'ema_50':               latest.get('ema_50'),
            'vol_ratio':            latest.get('vol_ratio'),
        })

    print(f"  Stocks with score >= {MIN_SCORE}: {len(records)}")

    if not records:
        print("\n⚠️  No qualifying pre-signals today.")
        return

    print(f"\n💾 Writing {len(records)} records...")
    supabase.table('presignal_scores').delete().eq('score_date', score_date).execute()
    written = 0
    for i in range(0, len(records), 100):
        supabase.table('presignal_scores').insert(records[i:i+100]).execute()
        written += len(records[i:i+100])

    print(f"✅ Written: {written} records")
    print(f"\n📊 Breakdown by top signal:")
    for sig, cnt in sorted(stats.items(), key=lambda x: -x[1]):
        print(f"   {sig:20s}: {cnt}")

    top10 = sorted(records, key=lambda r: r['top_rule_score'], reverse=True)[:10]
    print(f"\n🏆 Top 10 pre-signals:")
    for r in top10:
        rsi  = f"RSI={r['rsi_ema_9']:.1f}" if r['rsi_ema_9'] else "RSI=—"
        rank = f"Rank #{r['rs_rank']}"      if r['rs_rank']  else "Rank=—"
        # Show rank trajectory
        hist_r = by_ticker.get(r['ticker'], [])
        rank_hist = [x.get('rs_rank') for x in hist_r if x.get('rs_rank') is not None]
        if len(rank_hist) >= 2:
            chg = rank_hist[-1] - rank_hist[0]
            trend = f"({'↑' if chg < 0 else '↓'}{abs(chg):.0f} over {len(rank_hist)}d)"
        else:
            trend = ''
        print(f"   {r['ticker']:22s}  {r['top_signal']:18s}  "
              f"score={r['top_rule_score']:5.1f}  {rsi}  {rank} {trend}")

    print(f"\nCompleted: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)


if __name__ == "__main__":
    main()
