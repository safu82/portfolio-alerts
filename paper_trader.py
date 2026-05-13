#!/usr/bin/env python3
"""
Paper Trading Executor (Strategy v3 — Conviction Stack)
========================================================
Daily simulation of an algorithmic strategy on top of the existing signal
infrastructure. NO real orders are placed — every trade is logged to
`paper_trades` with mode='paper'.

Entry buckets (ranked priority A > B > C > D; signal_score breaks ties):
    T1_MULTI_STRONG (A) — >=2 distinct strong entry_signal types from
        {blue_zone_strong, golden_cross_strong, macd_strong,
         bull_flag_strong, vcp_strong}.
        Sizing: 1.5% risk, Rs 3.0L cap.
    T2_STRONG_REG   (B) — >=2 distinct entry_signal families with EXACTLY
        one strong family + >=1 regular family (family dedupe applied).
        Sizing: 1.0% risk, Rs 2.5L cap.
    T3_MULTI_REG    (C) — >=2 distinct entry_signal families, no strong.
        Sizing: 0.66% risk, Rs 1.5L cap.
    T4_RS_ACCEL     (D) — From presignal_scores: rule_rs_acceleration==100
        AND any other rule (bz_buy/bz_strong/golden_cross/macd/
        pullback_bounce) > 80 AND rank_slope <= -15 AND rs_30d_high=true.
        Sizing: 0.5% risk, Rs 1.0L cap.

Universal gates (all buckets):
  - Sector must be in Leading or Improving quadrant (RRG via
    get_all_sector_rankings RPC).
  - Earnings filter: latest quarter YoY positive AND improving for BOTH
    revenue and net profit (latest YoY > prior YoY). Requires >=6 quarters
    in stock_fundamentals.quarterly_financials; reject otherwise.
  - Not in real holdings, not in open paper, 21-trading-day cooldown
    after a close.
  - Position floor Rs 40k, sector concentration <=25%, <=8 open, <=3
    new entries per day.
  - Kill switches: pause new entries if daily P&L < -2% or DD > -8% ATH.

Execution flow (added 2026-05-13 to remove look-ahead bias):
  D0 22:00 IST — this script writes approved candidates as PENDING rows
    (status='pending') with the D0 ATR captured. Placeholder entry_price /
    qty / stop are derived from D0 close so sector concentration and floor
    checks are valid; they will be overwritten at fill time.
  D1 09:20 IST — `paper_fill_pending.py` (separate workflow) reads pending
    rows, looks up the first live_prices tick of the session, recomputes
    entry_price / qty / initial_stop using D1 open (price * 1.0015 for
    slippage), and flips status='open' with entry_date=D1.
  Pending rows older than 2 trading days are auto-closed by the fill job
    with exit_reason='fill_expired'.

Exit logic:
  - Initial stop: 2 x ATR_14 below entry.
  - T1: 33% at +3R + breakeven, 33% at +6R + 2xATR trail, rest trails.
  - T2/T3/T4: 33% at +2R + breakeven, 33% at +4R + 2xATR trail, rest trails.
  - Universal: book 25% if up >25% within 15 trading days.
  - Time stop: close if flat (-2%..+2%) after 25 trading days.
  - Intraday stop: zerodha_rest_updater_railway.py exits at LTP if LTP
    <= current_stop. Partials/trailing/time-stop remain EOD here.

Schedule: weekdays at 22:00 IST (16:30 UTC).
Inputs:   presignal_scores, entry_signals, daily_stock_snapshots, holdings,
          indian_stock_sectors, stock_fundamentals, paper_trades, paper_equity,
          get_all_sector_rankings RPC.
Outputs:  paper_trades (insert/update), paper_equity (upsert),
          paper_run_log (insert).
"""

import json
import os
import sys
import traceback
from collections import defaultdict
from datetime import date, datetime, timedelta

from supabase import create_client

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------

SUPABASE_URL = os.getenv(
    'SUPABASE_URL',
    'https://hcgyncghmcvylnrmcivj.supabase.co',
)
SUPABASE_KEY = os.getenv(
    'SUPABASE_KEY',
    'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhjZ3luY2dobWN2eWxucm1jaXZqIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTc1MTQwMTEsImV4cCI6MjA3MzA5MDAxMX0.n8vFVCJe1y_3o8fpAY0IgasZ4eKl7DAogEM3OlHB8Ww',
)

SLEEVE = 2_500_000  # Rs 25L paper sleeve

LOOKBACK_DAYS = 2
COOLDOWN_DAYS = 21

TIER_PARAMS = {
    'T1_MULTI_STRONG': {'risk_pct': 0.015,  'cap': 300_000, 'partial_R': [3, 6]},
    'T2_STRONG_REG':   {'risk_pct': 0.010,  'cap': 250_000, 'partial_R': [2, 4]},
    'T3_MULTI_REG':    {'risk_pct': 0.0066, 'cap': 150_000, 'partial_R': [2, 4]},
    'T4_RS_ACCEL':     {'risk_pct': 0.005,  'cap': 100_000, 'partial_R': [2, 4]},
}
POSITION_FLOOR = 40_000
SLIPPAGE_BPS = 15

STOP_ATR_MULT = 2.0
TRAIL_ATR_MULT = 2.0
TIME_STOP_DAYS = 25
TIME_STOP_LOW = -2.0
TIME_STOP_HIGH = 2.0
UNIVERSAL_BOOK_DAYS = 15
UNIVERSAL_BOOK_PCT = 25.0
UNIVERSAL_BOOK_QTY_PCT = 25
PARTIAL_QTY_PCT = 33

MAX_OPEN_POSITIONS = 8
MAX_SECTOR_CONC = 0.25
MAX_NEW_PER_DAY = 3
DAILY_KILL_PCT = -2.0
DD_KILL_PCT = -8.0

# entry_signal types treated as STRONG for bucket A (matches dashboard
# Multi-Strong Buy definition).
STRONG_ENTRY_TYPES = {
    'blue_zone_strong', 'golden_cross_strong', 'macd_strong',
    'bull_flag_strong', 'vcp_strong',
}

# entry_signals.signal_type -> family (used for B/C dedupe so a Strong+Buy
# pair from the same family doesn't qualify as multi-signal).
ENTRY_FAMILY_MAP = {
    'blue_zone_buy':        'BLUE_ZONE',
    'blue_zone_strong':     'BLUE_ZONE',
    'golden_cross_buy':     'GOLDEN_CROSS',
    'golden_cross_strong':  'GOLDEN_CROSS',
    'macd_buy':             'MACD',
    'macd_strong':          'MACD',
    'bull_flag_buy':        'BULL_FLAG',
    'bull_flag_strong':     'BULL_FLAG',
    'vcp_buy':              'VCP',
    'vcp_strong':           'VCP',
    'narrow_cpr_breakaway': 'CPR_BREAKAWAY',
    'darvas_box':           'DARVAS',
}

# Pre-signal Bucket D thresholds
RS_ACCEL_REQUIRED_SCORE = 100
RS_ACCEL_OTHER_RULE_MIN = 80
RS_ACCEL_RANK_SLOPE_MAX = -15
RS_ACCEL_OTHER_RULES = (
    'rule_bz_buy', 'rule_bz_strong', 'rule_golden_cross',
    'rule_macd', 'rule_pullback_bounce',
)

# Sector RRG quadrants that pass the universal sector gate
ALLOWED_SECTOR_QUADRANTS = {'leading', 'improving'}

# Earnings filter: min quarters in quarterly_financials to evaluate the
# "YoY positive AND improving" test. Latest YoY = Q[0]/Q[4]; prior YoY =
# Q[1]/Q[5]; so 6 quarters minimum.
EARNINGS_MIN_QUARTERS = 6


# ----------------------------------------------------------------------
# UTILS
# ----------------------------------------------------------------------

def log(msg):
    print(f"[{datetime.utcnow().isoformat()}Z] {msg}", flush=True)


def paginate(query, batch_size=1000):
    out, off = [], 0
    while True:
        resp = query.range(off, off + batch_size - 1).execute()
        if not resp.data:
            break
        out.extend(resp.data)
        if len(resp.data) < batch_size:
            break
        off += batch_size
    return out


def to_float(v, default=None):
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def to_int(v, default=0):
    if v is None:
        return default
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def trading_days_between(start: date, end: date) -> int:
    if end <= start:
        return 0
    n, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


# ----------------------------------------------------------------------
# DATA LOADERS
# ----------------------------------------------------------------------

_BENCHMARK_TICKERS_FOR_DATES = ('RELIANCE.NS', 'HDFCBANK.NS', 'ICICIBANK.NS', 'INFY.NS')


def get_recent_trading_dates(sb, days_back=10):
    # Querying the whole daily_stock_snapshots table hits PostgREST's 1000-row
    # response cap (~1.7 days with 585 tickers), which silently truncates the
    # signal lookback window. Use a single liquid benchmark instead — one row
    # per trading day, no cap concerns.
    for bench in _BENCHMARK_TICKERS_FOR_DATES:
        resp = (sb.table('daily_stock_snapshots')
                  .select('snapshot_date')
                  .eq('ticker', bench)
                  .order('snapshot_date', desc=True)
                  .limit(days_back)
                  .execute())
        rows = resp.data or []
        if len(rows) >= days_back:
            return [r['snapshot_date'] for r in rows]
    # Fallback: paginate through the full table if no benchmark has enough rows.
    dates_seen = []
    seen_set = set()
    offset = 0
    while len(seen_set) < days_back:
        resp = (sb.table('daily_stock_snapshots')
                  .select('snapshot_date')
                  .order('snapshot_date', desc=True)
                  .range(offset, offset + 999)
                  .execute())
        if not resp.data:
            break
        for r in resp.data:
            d = r['snapshot_date']
            if d not in seen_set:
                seen_set.add(d)
                dates_seen.append(d)
                if len(seen_set) >= days_back:
                    break
        if len(resp.data) < 1000:
            break
        offset += 1000
    return dates_seen[:days_back]


def load_snapshots_for_date(sb, dt):
    rows = paginate(sb.table('daily_stock_snapshots').select('*').eq('snapshot_date', dt))
    return {r['ticker']: r for r in rows}


def load_presignals(sb, dt_from, dt_to):
    return paginate(
        sb.table('presignal_scores').select('*')
          .gte('score_date', dt_from).lte('score_date', dt_to)
    )


def load_entry_signals(sb, dt_from, dt_to):
    return paginate(
        sb.table('entry_signals').select('*')
          .gte('alert_date', dt_from).lte('alert_date', dt_to)
    )


def load_holdings(sb):
    resp = sb.table('holdings').select('ticker').eq('portfolio', 'INDIAN').execute()
    return {r['ticker'] for r in (resp.data or [])}


def load_open_paper_trades(sb):
    resp = (sb.table('paper_trades').select('*')
              .eq('status', 'open').eq('mode', 'paper').execute())
    return resp.data or []


def load_pending_paper_trades(sb):
    resp = (sb.table('paper_trades').select('*')
              .eq('status', 'pending').eq('mode', 'paper').execute())
    return resp.data or []


def load_recent_closed_tickers(sb, today):
    cutoff = (today - timedelta(days=COOLDOWN_DAYS + 7)).isoformat()
    resp = (sb.table('paper_trades').select('ticker, exit_date')
              .eq('status', 'closed').gte('exit_date', cutoff).execute())
    return {r['ticker']: r['exit_date'] for r in (resp.data or [])}


def load_sectors(sb):
    rows = paginate(sb.table('indian_stock_sectors').select('ticker, sector'))
    return {r['ticker']: r['sector'] for r in rows}


def load_recent_equity(sb, limit=60):
    resp = (sb.table('paper_equity').select('*')
              .order('snapshot_date', desc=True).limit(limit).execute())
    return resp.data or []


def load_cumulative_closed_pnl(sb):
    resp = sb.table('paper_trades').select('total_pnl').eq('status', 'closed').execute()
    return sum(to_float(r.get('total_pnl'), 0) for r in (resp.data or []))


def load_sector_quadrants(sb):
    """sector_name -> rrg_quadrant (lowercase). Empty dict on any failure.

    The RPC `get_all_sector_rankings` returns a JSON object. supabase-py may
    deliver it as a parsed dict, a JSON-encoded string, or (if the function
    returns a list) a list directly. Handle all three.
    """
    try:
        resp = sb.rpc('get_all_sector_rankings').execute()
        data = resp.data
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                log('load_sector_quadrants: data is str but not JSON')
                return {}
        # RPC payload shape: {'as_of': date, 'total_sectors': N,
        #   'sectors': { 'IT': {sector, rrg_quadrant, ...}, 'FMCG': {...}, ... } }
        # Older callers may see a list — accept both.
        if isinstance(data, dict):
            sectors_obj = data.get('sectors')
        elif isinstance(data, list):
            sectors_obj = data
        else:
            sectors_obj = None

        if isinstance(sectors_obj, dict):
            iterable = sectors_obj.values()
        elif isinstance(sectors_obj, list):
            iterable = sectors_obj
        else:
            iterable = []

        out = {}
        for s in iterable:
            if not isinstance(s, dict):
                continue
            name = s.get('sector')
            q = s.get('rrg_quadrant')
            if name and q:
                out[name] = str(q).lower()
        return out
    except Exception as e:
        log(f'load_sector_quadrants failed: {e}')
        return {}


def load_fundamentals(sb, tickers):
    """ticker -> quarterly_financials list (newest first). Skips missing/short."""
    out = {}
    if not tickers:
        return out
    tickers = list(tickers)
    BATCH = 100
    for i in range(0, len(tickers), BATCH):
        chunk = tickers[i:i + BATCH]
        try:
            resp = (sb.table('stock_fundamentals')
                      .select('ticker, quarterly_financials')
                      .in_('ticker', chunk).execute())
        except Exception as e:
            log(f'load_fundamentals batch failed: {e}')
            continue
        for r in (resp.data or []):
            qf = r.get('quarterly_financials')
            if isinstance(qf, str):
                try:
                    qf = json.loads(qf)
                except json.JSONDecodeError:
                    qf = None
            if isinstance(qf, list):
                out[r['ticker']] = qf
    return out


# ----------------------------------------------------------------------
# BUCKET CLASSIFICATION
# ----------------------------------------------------------------------

def group_signals_by_ticker(presignals, entry_signals):
    by_ticker = defaultdict(lambda: {
        'presignal_ids': [], 'entry_signal_ids': [],
        'presignal_rows': [], 'entry_signal_rows': [],
    })
    for r in presignals:
        by_ticker[r['ticker']]['presignal_ids'].append(r['id'])
        by_ticker[r['ticker']]['presignal_rows'].append(r)
    for r in entry_signals:
        by_ticker[r['ticker']]['entry_signal_ids'].append(r['id'])
        by_ticker[r['ticker']]['entry_signal_rows'].append(r)
    return by_ticker


def classify_entry_bucket(entry_signal_rows):
    """Classify ticker into T1/T2/T3 from entry_signals. Returns
    (bucket, families_list, family_strength_dict, signal_score) or (None,...)."""
    types_seen = set()
    for r in entry_signal_rows:
        t = r.get('signal_type')
        if t in ENTRY_FAMILY_MAP:
            types_seen.add(t)
    if not types_seen:
        return None, [], {}, 0.0

    # Bucket A: >=2 distinct STRONG signal_types (signal-type-level, not family)
    strong_types = types_seen & STRONG_ENTRY_TYPES
    if len(strong_types) >= 2:
        families = sorted({ENTRY_FAMILY_MAP[s] for s in types_seen})
        fam_strength = {ENTRY_FAMILY_MAP[s]: 'strong' for s in strong_types}
        for s in types_seen - strong_types:
            fam_strength.setdefault(ENTRY_FAMILY_MAP[s], 'regular')
        score = len(strong_types) * 100.0
        return 'T1_MULTI_STRONG', families, fam_strength, score

    # B / C: dedupe to families
    fam_strength = {}
    for t in types_seen:
        fam = ENTRY_FAMILY_MAP[t]
        is_strong = t in STRONG_ENTRY_TYPES
        prev = fam_strength.get(fam)
        if prev == 'strong':
            continue
        fam_strength[fam] = 'strong' if is_strong else (prev or 'regular')

    if len(fam_strength) < 2:
        return None, [], {}, 0.0

    n_strong = sum(1 for v in fam_strength.values() if v == 'strong')
    families = sorted(fam_strength.keys())
    # Score: families count + strong-family weighting (for intra-bucket ranking)
    score = len(fam_strength) * 50.0 + n_strong * 30.0
    if n_strong == 1:
        return 'T2_STRONG_REG', families, fam_strength, score
    # n_strong == 0  (n_strong >= 2 already handled by Bucket A path above)
    return 'T3_MULTI_REG', families, fam_strength, score


def classify_presignal_bucket(presignal_rows):
    """Bucket D (T4_RS_ACCEL): rule_rs_acceleration==100 + any other rule>80
    + rank_slope<=-15 + rs_30d_high=true in the SAME row.
    Returns (bucket, families, family_strength, signal_score, qualifying_row)
    or (None, ..., ..., ..., None)."""
    best_row = None
    best_score = -1.0
    for r in presignal_rows:
        if to_float(r.get('rule_rs_acceleration'), 0) < RS_ACCEL_REQUIRED_SCORE:
            continue
        other_rules_passing = [
            (k, to_float(r.get(k), 0)) for k in RS_ACCEL_OTHER_RULES
            if to_float(r.get(k), 0) > RS_ACCEL_OTHER_RULE_MIN
        ]
        if not other_rules_passing:
            continue
        if to_float(r.get('rank_slope'), 0) > RS_ACCEL_RANK_SLOPE_MAX:
            continue
        if not r.get('rs_30d_high'):
            continue
        max_other = max(v for _, v in other_rules_passing)
        # Score: weight the partner-rule strength
        row_score = 100.0 + max_other
        if row_score > best_score:
            best_score = row_score
            best_row = r
    if best_row is None:
        return None, [], {}, 0.0, None
    return ('T4_RS_ACCEL', ['RS_ACCEL'], {'RS_ACCEL': 'strong'},
            best_score, best_row)


# ----------------------------------------------------------------------
# UNIVERSAL FILTERS
# ----------------------------------------------------------------------

def passes_sector_filter(ticker, sector_map, sector_quadrants):
    sector = sector_map.get(ticker)
    if not sector:
        return False, 'sector_unknown'
    q = sector_quadrants.get(sector)
    if not q:
        return False, 'sector_quadrant_missing'
    if q not in ALLOWED_SECTOR_QUADRANTS:
        return False, f'sector_{q}'
    return True, None


def passes_earnings_filter(ticker, fundamentals_map):
    """Latest YoY positive AND > prior YoY, for BOTH revenue and net profit.
    quarterly_financials is newest-first (Q[0] = latest)."""
    qf = fundamentals_map.get(ticker)
    if not qf:
        return False, 'fundamentals_missing'
    if len(qf) < EARNINGS_MIN_QUARTERS:
        return False, f'quarters_{len(qf)}'

    def yoy(q_now, q_yr_ago, key):
        a = to_float(q_now.get(key))
        b = to_float(q_yr_ago.get(key))
        if a is None or b is None or b <= 0:
            return None
        return (a - b) / b

    latest_rev = yoy(qf[0], qf[4], 'revenue_cr')
    prior_rev  = yoy(qf[1], qf[5], 'revenue_cr')
    latest_np  = yoy(qf[0], qf[4], 'net_income_cr')
    prior_np   = yoy(qf[1], qf[5], 'net_income_cr')

    if latest_rev is None or prior_rev is None:
        return False, 'revenue_data_incomplete'
    if latest_np is None or prior_np is None:
        return False, 'profit_data_incomplete'

    if latest_rev <= 0:
        return False, 'revenue_yoy_not_positive'
    if latest_rev <= prior_rev:
        return False, 'revenue_yoy_not_improving'
    if latest_np <= 0:
        return False, 'profit_yoy_not_positive'
    if latest_np <= prior_np:
        return False, 'profit_yoy_not_improving'
    return True, None


def size_position(tier, entry_price, atr):
    p = TIER_PARAMS[tier]
    risk_inr = SLEEVE * p['risk_pct']
    stop_dist = STOP_ATR_MULT * atr
    if stop_dist <= 0 or entry_price <= 0:
        return 0, 0, 0
    qty_by_risk = int(risk_inr // stop_dist)
    qty_by_cap = int(p['cap'] // entry_price)
    qty = max(0, min(qty_by_risk, qty_by_cap))
    notional = qty * entry_price
    if notional < POSITION_FLOOR:
        return 0, 0, 0
    return qty, notional, risk_inr


# ----------------------------------------------------------------------
# EXIT PROCESSING
# ----------------------------------------------------------------------

def _close_trade(sb, tr, exit_price, exit_date, exit_reason,
                 realised_pnl, realised_qty, partials, partials_taken,
                 current_stop, breakeven_armed, trail_armed):
    entry_price = to_float(tr['entry_price'])
    initial_qty = to_int(tr['initial_quantity'])
    initial_stop = to_float(tr['initial_stop'])
    entry_value = entry_price * initial_qty
    total_pnl_pct = realised_pnl / entry_value * 100 if entry_value else 0
    risk_per_share = entry_price - initial_stop
    r_mult = ((realised_pnl / initial_qty) / risk_per_share
              if (initial_qty and risk_per_share > 0) else None)
    holding_days = trading_days_between(date.fromisoformat(tr['entry_date']), exit_date)
    sb.table('paper_trades').update({
        'exit_date': exit_date.isoformat(),
        'exit_price': round(exit_price, 4),
        'exit_reason': exit_reason,
        'total_pnl': round(realised_pnl, 2),
        'total_pnl_pct': round(total_pnl_pct, 3),
        'r_multiple': round(r_mult, 3) if r_mult is not None else None,
        'holding_days': holding_days,
        'current_quantity': 0,
        'realised_pnl': round(realised_pnl, 2),
        'realised_qty': realised_qty,
        'partials': partials,
        'partials_taken': partials_taken,
        'current_stop': round(current_stop, 4),
        'breakeven_armed': breakeven_armed,
        'trail_armed': trail_armed,
        'status': 'closed',
        'updated_at': datetime.utcnow().isoformat(),
    }).eq('id', tr['id']).execute()


def _update_open_trade(sb, trade_id, current_qty, current_stop, partials_taken,
                       partials, realised_pnl, realised_qty,
                       breakeven_armed, trail_armed):
    sb.table('paper_trades').update({
        'current_quantity': current_qty,
        'current_stop': round(current_stop, 4),
        'partials_taken': partials_taken,
        'partials': partials,
        'realised_pnl': round(realised_pnl, 2),
        'realised_qty': realised_qty,
        'breakeven_armed': breakeven_armed,
        'trail_armed': trail_armed,
        'updated_at': datetime.utcnow().isoformat(),
    }).eq('id', trade_id).execute()


def process_exits(sb, open_trades, today_snap, today_date):
    actions = []
    realised_today = 0.0
    closed_today = 0

    for tr in open_trades:
        ticker = tr['ticker']
        snap = today_snap.get(ticker)
        if not snap:
            continue
        bar_high = to_float(snap.get('high'))
        bar_low = to_float(snap.get('low'))
        bar_close = to_float(snap.get('close'))
        bar_atr = to_float(snap.get('atr_14'), to_float(tr.get('entry_atr')))
        if bar_close is None or bar_high is None or bar_low is None:
            continue

        entry_price = to_float(tr['entry_price'])
        initial_stop = to_float(tr['initial_stop'])
        current_stop = to_float(tr['current_stop'])
        initial_qty = to_int(tr['initial_quantity'])
        current_qty = to_int(tr['current_quantity'])
        if current_qty <= 0:
            continue
        risk_per_share = entry_price - initial_stop
        if risk_per_share <= 0:
            continue
        tier = tr['strategy_tier']
        partial_levels = TIER_PARAMS[tier]['partial_R']
        partials_taken = to_int(tr['partials_taken'])
        partials = tr.get('partials') or []
        if isinstance(partials, str):
            try:
                partials = json.loads(partials)
            except json.JSONDecodeError:
                partials = []
        realised_pnl = to_float(tr['realised_pnl'], 0)
        realised_qty = to_int(tr['realised_qty'], 0)
        breakeven_armed = bool(tr.get('breakeven_armed'))
        trail_armed = bool(tr.get('trail_armed'))
        partial_qty = max(1, int(initial_qty * PARTIAL_QTY_PCT / 100))

        # 1. Stop hit first
        if bar_low <= current_stop:
            exit_qty = current_qty
            pnl_chunk = (current_stop - entry_price) * exit_qty
            realised_pnl += pnl_chunk
            realised_today += pnl_chunk
            realised_qty += exit_qty
            current_qty = 0
            _close_trade(sb, tr, current_stop, today_date,
                         'trail_stop' if trail_armed else 'stop',
                         realised_pnl, realised_qty, partials, partials_taken,
                         current_stop, breakeven_armed, trail_armed)
            closed_today += 1
            actions.append({'id': tr['id'], 'ticker': ticker, 'action': 'stop'})
            continue

        # 2. Partial booking via R-multiples
        for idx, R in enumerate(partial_levels):
            if partials_taken > idx:
                continue
            target_price = entry_price + R * risk_per_share
            if bar_high < target_price:
                break  # didn't hit this level; won't hit higher levels
            book_qty = min(partial_qty, current_qty)
            if book_qty <= 0:
                break
            pnl_chunk = (target_price - entry_price) * book_qty
            realised_pnl += pnl_chunk
            realised_today += pnl_chunk
            realised_qty += book_qty
            current_qty -= book_qty
            partials_taken += 1
            partials.append({
                'date': today_date.isoformat(),
                'kind': f'R{R}',
                'price': round(target_price, 2),
                'qty': book_qty,
                'pnl': round(pnl_chunk, 2),
            })
            if partials_taken == 1 and not breakeven_armed:
                current_stop = max(current_stop, entry_price)
                breakeven_armed = True
            if partials_taken == 2 and not trail_armed:
                trail_armed = True
            if current_qty == 0:
                break

        if current_qty == 0:
            last_price = partials[-1]['price'] if partials else bar_close
            _close_trade(sb, tr, last_price, today_date, 'partials_full',
                         realised_pnl, realised_qty, partials, partials_taken,
                         current_stop, breakeven_armed, trail_armed)
            closed_today += 1
            actions.append({'id': tr['id'], 'ticker': ticker, 'action': 'partials_full'})
            continue

        # 3. Universal 25%/15-day partial (only if no R-partial taken yet)
        entry_date = date.fromisoformat(tr['entry_date'])
        holding_days = trading_days_between(entry_date, today_date)
        if partials_taken == 0 and holding_days < UNIVERSAL_BOOK_DAYS:
            unr_pct = (bar_close - entry_price) / entry_price * 100
            if unr_pct >= UNIVERSAL_BOOK_PCT:
                book_qty = min(max(1, int(initial_qty * UNIVERSAL_BOOK_QTY_PCT / 100)),
                               current_qty)
                pnl_chunk = (bar_close - entry_price) * book_qty
                realised_pnl += pnl_chunk
                realised_today += pnl_chunk
                realised_qty += book_qty
                current_qty -= book_qty
                partials.append({
                    'date': today_date.isoformat(),
                    'kind': 'universal_25pct',
                    'price': round(bar_close, 2),
                    'qty': book_qty,
                    'pnl': round(pnl_chunk, 2),
                })
                if not breakeven_armed:
                    current_stop = max(current_stop, entry_price)
                    breakeven_armed = True

        # 4. Trailing stop update
        if trail_armed and bar_atr:
            new_stop = bar_close - TRAIL_ATR_MULT * bar_atr
            if new_stop > current_stop:
                current_stop = new_stop

        # 5. Time stop
        if holding_days >= TIME_STOP_DAYS:
            unr_pct = (bar_close - entry_price) / entry_price * 100
            if TIME_STOP_LOW <= unr_pct <= TIME_STOP_HIGH:
                exit_qty = current_qty
                pnl_chunk = (bar_close - entry_price) * exit_qty
                realised_pnl += pnl_chunk
                realised_today += pnl_chunk
                realised_qty += exit_qty
                current_qty = 0
                _close_trade(sb, tr, bar_close, today_date, 'time_stop',
                             realised_pnl, realised_qty, partials, partials_taken,
                             current_stop, breakeven_armed, trail_armed)
                closed_today += 1
                actions.append({'id': tr['id'], 'ticker': ticker, 'action': 'time_stop'})
                continue

        # Update state on the open row
        _update_open_trade(
            sb, tr['id'], current_qty, current_stop, partials_taken, partials,
            realised_pnl, realised_qty, breakeven_armed, trail_armed,
        )

    return actions, closed_today, realised_today


# ----------------------------------------------------------------------
# ENTRY PROCESSING
# ----------------------------------------------------------------------

def process_entries(sb, candidates, today_snap, today_date, holdings,
                    recent_closed, open_tickers, sector_map,
                    sector_exposure, max_new):
    tier_order = {
        'T1_MULTI_STRONG': 0, 'T2_STRONG_REG': 1,
        'T3_MULTI_REG': 2,    'T4_RS_ACCEL': 3,
    }
    candidates.sort(key=lambda c: (tier_order[c['tier']], -c['signal_score']))

    inserted = []
    for c in candidates:
        if len(inserted) >= max_new:
            break
        ticker = c['ticker']
        if ticker in holdings or ticker in open_tickers or ticker in recent_closed:
            continue
        snap = today_snap.get(ticker)
        if not snap:
            continue
        close_today = to_float(snap.get('close'))
        atr14 = to_float(snap.get('atr_14'))
        if not close_today or not atr14 or atr14 <= 0:
            continue

        # Pending row: estimate entry/qty/stop from D0 close so sector
        # concentration + floor checks are sane. Fill job (paper_fill_pending.py)
        # overwrites these with real values using D1 open the next morning.
        est_entry_price = close_today * (1 + SLIPPAGE_BPS / 10_000)
        qty, notional, _ = size_position(c['tier'], est_entry_price, atr14)
        if qty == 0:
            continue

        sector = sector_map.get(ticker, 'Unknown')
        sector_notional_after = sector_exposure.get(sector, 0) + notional
        if sector != 'Unknown' and sector_notional_after > MAX_SECTOR_CONC * SLEEVE:
            continue

        est_initial_stop = est_entry_price - STOP_ATR_MULT * atr14
        est_initial_risk = qty * (est_entry_price - est_initial_stop)

        row = {
            'ticker': ticker,
            'sector': sector,
            'strategy_tier': c['tier'],
            'signal_families': c['families'],
            'strong_family_count': sum(
                1 for s in c['family_strength'].values() if s == 'strong'
            ),
            'signal_ids': c['signal_ids'],
            'signal_score': round(c['signal_score'], 2),
            'entry_date': today_date.isoformat(),  # scan date; fill job updates to D1
            'entry_price': round(est_entry_price, 4),  # placeholder, fill recomputes
            'entry_atr': round(atr14, 4),
            'initial_quantity': qty,  # placeholder, fill recomputes
            'current_quantity': qty,
            'entry_value': round(qty * est_entry_price, 2),
            'initial_risk': round(est_initial_risk, 2),
            'initial_stop': round(est_initial_stop, 4),
            'current_stop': round(est_initial_stop, 4),
            'status': 'pending',
            'mode': 'paper',
        }
        sb.table('paper_trades').insert(row).execute()
        inserted.append(row)
        sector_exposure[sector] = sector_exposure.get(sector, 0) + notional
        open_tickers.add(ticker)

    return inserted


# ----------------------------------------------------------------------
# MTM + KILL SWITCHES
# ----------------------------------------------------------------------

def positions_mtm(open_trades, today_snap):
    pv = 0.0
    for tr in open_trades:
        snap = today_snap.get(tr['ticker'])
        if not snap:
            continue
        close = to_float(snap.get('close'))
        if not close:
            continue
        pv += close * to_int(tr['current_quantity'])
    return pv


def open_capital_tied(open_trades):
    return sum(
        to_int(tr['current_quantity']) * to_float(tr['entry_price'])
        for tr in open_trades
    )


def kill_switch(equity_history, provisional_total):
    if not equity_history:
        return False, None
    ath = max(
        [SLEEVE] + [to_float(e['total_value'], SLEEVE) for e in equity_history]
    )
    dd_pct = (provisional_total - ath) / ath * 100 if ath else 0
    if dd_pct < DD_KILL_PCT:
        return True, f'drawdown_{dd_pct:.2f}%'
    last_value = to_float(equity_history[0].get('total_value'), SLEEVE)
    if last_value > 0:
        daily_pct = (provisional_total - last_value) / last_value * 100
        if daily_pct < DAILY_KILL_PCT:
            return True, f'daily_{daily_pct:.2f}%'
    return False, None


def write_equity_row(sb, today_date, open_trades, today_snap, equity_history,
                     cum_realised_closed, opened, closed, realised_today):
    pv = positions_mtm(open_trades, today_snap)
    cash = SLEEVE - open_capital_tied(open_trades) + cum_realised_closed
    total_value = cash + pv
    ath = max(
        [SLEEVE] + [to_float(e['total_value'], SLEEVE) for e in equity_history]
        + [total_value]
    )
    dd_pct = (total_value - ath) / ath * 100 if ath else 0

    sb.table('paper_equity').upsert({
        'snapshot_date': today_date.isoformat(),
        'cash': round(cash, 2),
        'positions_value': round(pv, 2),
        'total_value': round(total_value, 2),
        'drawdown_pct': round(dd_pct, 3),
        'open_positions': len(open_trades),
        'trades_opened_today': opened,
        'trades_closed_today': closed,
        'realised_today': round(realised_today, 2),
    }, on_conflict='snapshot_date').execute()
    return total_value, dd_pct


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------

def main():
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    funnel = {
        'signal_rows': 0, 'unique_tickers': 0,
        'no_bucket': 0,
        'sector_rejected': 0, 'earnings_rejected': 0,
        'in_holdings': 0, 'in_cooldown': 0, 'already_open': 0,
        'qualified': 0, 'inserted': 0,
        'bucket_counts': {},
        'sector_reasons': {}, 'earnings_reasons': {},
    }
    errors = []

    try:
        recent_dates = get_recent_trading_dates(sb, days_back=5)
        if len(recent_dates) < 2:
            log('Insufficient trading data; aborting.')
            return
        today_date = date.fromisoformat(recent_dates[0])
        signal_date_from = recent_dates[min(LOOKBACK_DAYS, len(recent_dates) - 1)]
        signal_date_to = recent_dates[1]
        log(f'today={today_date}  '
            f'signal_window={signal_date_from}..{signal_date_to}')

        today_snap = load_snapshots_for_date(sb, today_date.isoformat())
        holdings = load_holdings(sb)
        open_trades = load_open_paper_trades(sb)
        pending_trades = load_pending_paper_trades(sb)
        recent_closed = load_recent_closed_tickers(sb, today_date)
        sector_map = load_sectors(sb)
        sector_quadrants = load_sector_quadrants(sb)
        equity_history = load_recent_equity(sb)
        log(f'open_trades={len(open_trades)} pending={len(pending_trades)} '
            f'holdings={len(holdings)} cooldown={len(recent_closed)} '
            f'sectors_quadranted={len(sector_quadrants)}')

        # 1. EXITS first using today's bar (operates on 'open' only)
        exit_actions, closed_today, realised_today = process_exits(
            sb, open_trades, today_snap, today_date
        )
        log(f'exits processed: {len(exit_actions)} (closed={closed_today})')
        open_trades = load_open_paper_trades(sb)
        pending_trades = load_pending_paper_trades(sb)
        # Pending rows occupy slots + sector exposure so we don't over-commit
        # between the D0 scan and the D1 fill.
        committed_trades = open_trades + pending_trades
        open_tickers = {t['ticker'] for t in committed_trades}

        # 2. Sector exposure (post-exit baseline, includes pending)
        sector_exposure = defaultdict(float)
        for tr in committed_trades:
            sector_exposure[tr.get('sector') or 'Unknown'] += to_float(tr['entry_value'], 0)

        # 3. Provisional MTM + kill switch check
        cum_realised_closed = load_cumulative_closed_pnl(sb)
        pv_now = positions_mtm(open_trades, today_snap)
        provisional_total = (
            SLEEVE - open_capital_tied(open_trades) + cum_realised_closed + pv_now
        )
        kill, kill_reason = kill_switch(equity_history, provisional_total)

        inserted = []
        if kill:
            log(f'KILL SWITCH: {kill_reason} -- skipping new entries')
        elif len(committed_trades) >= MAX_OPEN_POSITIONS:
            log(f'At max open positions ({MAX_OPEN_POSITIONS}, incl. pending) '
                f'-- skipping new entries')
        else:
            slots = min(MAX_NEW_PER_DAY, MAX_OPEN_POSITIONS - len(committed_trades))
            presignals = load_presignals(sb, signal_date_from, signal_date_to)
            entry_signals_data = load_entry_signals(sb, signal_date_from, signal_date_to)
            funnel['signal_rows'] = len(presignals) + len(entry_signals_data)
            by_ticker = group_signals_by_ticker(presignals, entry_signals_data)
            funnel['unique_tickers'] = len(by_ticker)
            log(f'signals: presignal={len(presignals)} entry={len(entry_signals_data)} '
                f'tickers={len(by_ticker)}')

            # Stage 1: classify each ticker into the highest bucket it qualifies
            # for (T1 > T2 > T3 > T4), without yet applying universal gates.
            raw_candidates = []
            for ticker, bundle in by_ticker.items():
                # Try entry-signal buckets first (higher priority than D)
                bucket, families, fam_strength, score = classify_entry_bucket(
                    bundle['entry_signal_rows']
                )
                presignal_row = None
                if not bucket:
                    bucket, families, fam_strength, score, presignal_row = (
                        classify_presignal_bucket(bundle['presignal_rows'])
                    )
                if not bucket:
                    funnel['no_bucket'] += 1
                    continue
                raw_candidates.append({
                    'ticker': ticker,
                    'tier': bucket,
                    'families': families,
                    'family_strength': fam_strength,
                    'signal_score': score,
                    'signal_ids': {
                        'presignal_ids': (
                            [presignal_row['id']] if presignal_row
                            else bundle['presignal_ids']
                        ),
                        'entry_signal_ids': bundle['entry_signal_ids'],
                    },
                })

            # Stage 2: load fundamentals only for bucket-qualifying tickers
            fundamentals = load_fundamentals(
                sb, [c['ticker'] for c in raw_candidates]
            )

            # Stage 3: apply hygiene + sector + earnings gates
            candidates = []
            for c in raw_candidates:
                ticker = c['ticker']
                if ticker in holdings:
                    funnel['in_holdings'] += 1; continue
                if ticker in recent_closed:
                    funnel['in_cooldown'] += 1; continue
                if ticker in open_tickers:
                    funnel['already_open'] += 1; continue
                sec_ok, sec_reason = passes_sector_filter(
                    ticker, sector_map, sector_quadrants
                )
                if not sec_ok:
                    funnel['sector_rejected'] += 1
                    funnel['sector_reasons'][sec_reason] = (
                        funnel['sector_reasons'].get(sec_reason, 0) + 1
                    )
                    continue
                e_ok, e_reason = passes_earnings_filter(ticker, fundamentals)
                if not e_ok:
                    funnel['earnings_rejected'] += 1
                    funnel['earnings_reasons'][e_reason] = (
                        funnel['earnings_reasons'].get(e_reason, 0) + 1
                    )
                    continue
                funnel['bucket_counts'][c['tier']] = (
                    funnel['bucket_counts'].get(c['tier'], 0) + 1
                )
                candidates.append(c)

            funnel['qualified'] = len(candidates)
            inserted = process_entries(
                sb, candidates, today_snap, today_date, holdings, recent_closed,
                open_tickers, sector_map, sector_exposure, max_new=slots,
            )
            funnel['inserted'] = len(inserted)
            log(f'entries: qualified={len(candidates)} inserted={len(inserted)}')

        # 4. Final equity row (load open_trades again after entries)
        final_open = load_open_paper_trades(sb)
        total_value, dd_pct = write_equity_row(
            sb, today_date, final_open, today_snap, equity_history,
            cum_realised_closed, opened=len(inserted),
            closed=closed_today, realised_today=realised_today,
        )
        log(f'equity: total={total_value:,.0f} dd={dd_pct:.2f}% '
            f'open={len(final_open)}')

        # 5. Run log
        sb.table('paper_run_log').insert({
            'run_date': today_date.isoformat(),
            'signals_seen': funnel['signal_rows'],
            'signals_passed': funnel['qualified'],
            'signals_taken': funnel['inserted'],
            'exits_processed': len(exit_actions),
            'kill_switch_active': kill,
            'kill_switch_reason': kill_reason,
            'funnel': funnel,
            'errors': errors or None,
        }).execute()
        log('Done.')

    except Exception as e:
        log(f'FATAL: {e}')
        traceback.print_exc()
        errors.append({'fatal': str(e), 'trace': traceback.format_exc()[:2000]})
        try:
            sb.table('paper_run_log').insert({
                'run_date': date.today().isoformat(),
                'errors': errors,
            }).execute()
        except Exception:
            pass
        sys.exit(1)


if __name__ == '__main__':
    main()
