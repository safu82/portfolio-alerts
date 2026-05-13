#!/usr/bin/env python3
"""
Paper Trading Executor (Strategy v2)
=====================================
Daily simulation of an algorithmic strategy on top of the existing signal
infrastructure. NO real orders are placed — every trade is logged to
`paper_trades` with mode='paper'.

Strategy:
  - Confluence entries: >=2 distinct signal families from
    presignal_scores + entry_signals (within a 2-trading-day window).
  - Family dedupe: e.g. bz_buy + bz_strong = 1 family at strong tier.
  - Tiering drives sizing + queue priority:
        T1_MULTI_STRONG (>=2 strong families) : 1.5% risk, Rs 3.0L cap
        T2_STRONG_REG   (1 strong + >=1 reg)   : 1.0% risk, Rs 2.5L cap
        T3_MULTI_REG    (>=2 regular, no strong): 0.66% risk, Rs 1.5L cap
  - Confirmation filters: top_rule_score >=85, rs_rank <=100,
    close > ema_50, vol_ratio >=1.2, weekly_rsi_ema_9 >=45.
  - Initial stop: 2 x ATR_14 below entry.
  - Scale-out:
        T1: 33% at +3R + breakeven, 33% at +6R + 2xATR trail, rest trails
        T2/T3: 33% at +2R + breakeven, 33% at +4R + 2xATR trail, rest trails
  - Universal: book 25% if up >25% within 15 trading days.
  - Time stop: close if flat (-2%..+2%) after 25 trading days.
  - Risk caps: 8 max open, 25% sector concentration, 3 new/day.
  - Kill switches: pause new entries if daily P&L < -2% or DD > -8% from ATH.

Schedule: weekdays at 22:00 IST (16:30 UTC).
Inputs:   presignal_scores, entry_signals, daily_stock_snapshots, holdings,
          indian_stock_sectors, paper_trades, paper_equity.
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

FILTER_MIN_SCORE = 85
FILTER_MAX_RS_RANK = 100
FILTER_MIN_VOL_RATIO = 1.2
FILTER_MIN_WKLY_RSI = 45

TIER_PARAMS = {
    'T1_MULTI_STRONG': {'risk_pct': 0.015,  'cap': 300_000, 'partial_R': [3, 6]},
    'T2_STRONG_REG':   {'risk_pct': 0.010,  'cap': 250_000, 'partial_R': [2, 4]},
    'T3_MULTI_REG':    {'risk_pct': 0.0066, 'cap': 150_000, 'partial_R': [2, 4]},
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

# signal_type -> (family, strength)
FAMILY_MAP = {
    # presignal_scores.top_signal
    'bz_buy':               ('BLUE_ZONE',     'regular'),
    'bz_strong':            ('BLUE_ZONE',     'strong'),
    'rs_acceleration':      ('RS_ACCEL',      'regular'),
    'golden_cross':         ('GOLDEN_CROSS',  'regular'),
    'macd':                 ('MACD',          'regular'),
    'pullback_bounce':      ('PULLBACK',      'regular'),
    # entry_signals.signal_type
    'blue_zone_buy':        ('BLUE_ZONE',     'regular'),
    'blue_zone_strong':     ('BLUE_ZONE',     'strong'),
    'golden_cross_buy':     ('GOLDEN_CROSS',  'regular'),
    'golden_cross_strong':  ('GOLDEN_CROSS',  'strong'),
    'macd_buy':             ('MACD',          'regular'),
    'macd_strong':          ('MACD',          'strong'),
    'narrow_cpr_breakaway': ('CPR_BREAKAWAY', 'strong'),
    'vcp_buy':              ('VCP',           'regular'),
    'bull_flag_buy':        ('BULL_FLAG',     'regular'),
    'darvas_box':           ('DARVAS',        'strong'),
}

# rs_acceleration with score >= this is treated as 'strong'
RS_ACCEL_STRONG_SCORE = 95


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

def get_recent_trading_dates(sb, days_back=10):
    # Pull a chunk of recent snapshot dates (deduped).
    resp = (sb.table('daily_stock_snapshots')
              .select('snapshot_date')
              .order('snapshot_date', desc=True)
              .limit(days_back * 700)
              .execute())
    dates = sorted({r['snapshot_date'] for r in (resp.data or [])}, reverse=True)
    return dates[:days_back]


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


# ----------------------------------------------------------------------
# SIGNAL DEDUPE + TIER
# ----------------------------------------------------------------------

def group_signals_by_ticker(presignals, entry_signals):
    by_ticker = defaultdict(
        lambda: {'presignal_ids': [], 'entry_signal_ids': [], 'rows': []}
    )
    for r in presignals:
        if not r.get('top_signal'):
            continue
        by_ticker[r['ticker']]['presignal_ids'].append(r['id'])
        by_ticker[r['ticker']]['rows'].append(('presignal', r))
    for r in entry_signals:
        by_ticker[r['ticker']]['entry_signal_ids'].append(r['id'])
        by_ticker[r['ticker']]['rows'].append(('entry_signal', r))
    return by_ticker


def derive_families(rows):
    fam_strength = {}
    for source, rec in rows:
        if source == 'presignal':
            sig = rec.get('top_signal')
            score = to_float(rec.get('top_rule_score'), 0)
        else:
            sig = rec.get('signal_type')
            score = 0
        if not sig or sig not in FAMILY_MAP:
            continue
        fam, strength = FAMILY_MAP[sig]
        if sig == 'rs_acceleration' and score >= RS_ACCEL_STRONG_SCORE:
            strength = 'strong'
        prev = fam_strength.get(fam)
        if prev == 'strong':
            continue
        fam_strength[fam] = 'strong' if strength == 'strong' else (prev or 'regular')
    return fam_strength


def classify_tier(fam_strength):
    n_strong = sum(1 for s in fam_strength.values() if s == 'strong')
    n_total = len(fam_strength)
    if n_total < 2:
        return None
    if n_strong >= 2:
        return 'T1_MULTI_STRONG'
    if n_strong == 1:
        return 'T2_STRONG_REG'
    return 'T3_MULTI_REG'


# ----------------------------------------------------------------------
# FILTERS + SIZING
# ----------------------------------------------------------------------

def passes_filters(presignal_rows, signal_snapshot):
    if presignal_rows:
        ps = max(presignal_rows, key=lambda r: to_float(r.get('top_rule_score'), 0))
        score = to_float(ps.get('top_rule_score'), 0)
        if score < FILTER_MIN_SCORE:
            return False, f'score_below_{FILTER_MIN_SCORE}'
        rs_rank = to_int(ps.get('rs_rank'), 9999)
        vol_ratio = to_float(ps.get('vol_ratio'), 0)
        wkly_rsi = to_float(ps.get('weekly_rsi_ema_9'), 0)
        close = to_float(ps.get('close'), 0)
        ema_50 = to_float(ps.get('ema_50'), 0)
    else:
        snap = signal_snapshot or {}
        rs_rank = to_int(snap.get('rs_rank'), 9999)
        vol_ratio = to_float(snap.get('vol_ratio'), 0)
        wkly_rsi = to_float(snap.get('weekly_rsi_ema_9'), 0)
        close = to_float(snap.get('close'), 0)
        ema_50 = to_float(snap.get('ema_50'), 0)

    if rs_rank > FILTER_MAX_RS_RANK:
        return False, f'rs_rank_{rs_rank}'
    if vol_ratio < FILTER_MIN_VOL_RATIO:
        return False, 'vol_ratio_low'
    if wkly_rsi < FILTER_MIN_WKLY_RSI:
        return False, 'wkly_rsi_low'
    if not close or not ema_50 or close <= ema_50:
        return False, 'below_ema_50'
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
    tier_order = {'T1_MULTI_STRONG': 0, 'T2_STRONG_REG': 1, 'T3_MULTI_REG': 2}
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
        open_today = to_float(snap.get('open'))
        atr14 = to_float(snap.get('atr_14'))
        if not open_today or not atr14 or atr14 <= 0:
            continue

        entry_price = open_today * (1 + SLIPPAGE_BPS / 10_000)
        qty, notional, _ = size_position(c['tier'], entry_price, atr14)
        if qty == 0:
            continue

        sector = sector_map.get(ticker, 'Unknown')
        sector_notional_after = sector_exposure.get(sector, 0) + notional
        if sector != 'Unknown' and sector_notional_after > MAX_SECTOR_CONC * SLEEVE:
            continue

        initial_stop = entry_price - STOP_ATR_MULT * atr14
        initial_risk = qty * (entry_price - initial_stop)

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
            'entry_date': today_date.isoformat(),
            'entry_price': round(entry_price, 4),
            'entry_atr': round(atr14, 4),
            'initial_quantity': qty,
            'current_quantity': qty,
            'entry_value': round(qty * entry_price, 2),
            'initial_risk': round(initial_risk, 2),
            'initial_stop': round(initial_stop, 4),
            'current_stop': round(initial_stop, 4),
            'status': 'open',
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
        'families_too_few': 0, 'failed_filter': 0,
        'in_holdings': 0, 'in_cooldown': 0, 'already_open': 0,
        'qualified': 0, 'inserted': 0,
        'filter_reasons': {},
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
        signal_snap_to = load_snapshots_for_date(sb, signal_date_to)
        holdings = load_holdings(sb)
        open_trades = load_open_paper_trades(sb)
        recent_closed = load_recent_closed_tickers(sb, today_date)
        sector_map = load_sectors(sb)
        equity_history = load_recent_equity(sb)
        log(f'open_trades={len(open_trades)} holdings={len(holdings)} '
            f'cooldown={len(recent_closed)}')

        # 1. EXITS first using today's bar
        exit_actions, closed_today, realised_today = process_exits(
            sb, open_trades, today_snap, today_date
        )
        log(f'exits processed: {len(exit_actions)} (closed={closed_today})')
        open_trades = load_open_paper_trades(sb)
        open_tickers = {t['ticker'] for t in open_trades}

        # 2. Sector exposure (post-exit baseline)
        sector_exposure = defaultdict(float)
        for tr in open_trades:
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
        elif len(open_trades) >= MAX_OPEN_POSITIONS:
            log(f'At max open positions ({MAX_OPEN_POSITIONS}) -- skipping new entries')
        else:
            slots = min(MAX_NEW_PER_DAY, MAX_OPEN_POSITIONS - len(open_trades))
            presignals = load_presignals(sb, signal_date_from, signal_date_to)
            entry_signals_data = load_entry_signals(sb, signal_date_from, signal_date_to)
            funnel['signal_rows'] = len(presignals) + len(entry_signals_data)
            by_ticker = group_signals_by_ticker(presignals, entry_signals_data)
            funnel['unique_tickers'] = len(by_ticker)
            log(f'signals: presignal={len(presignals)} entry={len(entry_signals_data)} '
                f'tickers={len(by_ticker)}')

            candidates = []
            for ticker, bundle in by_ticker.items():
                fam_strength = derive_families(bundle['rows'])
                if len(fam_strength) < 2:
                    funnel['families_too_few'] += 1
                    continue
                tier = classify_tier(fam_strength)
                if not tier:
                    continue
                presignal_rows_for_ticker = [
                    r for src, r in bundle['rows'] if src == 'presignal'
                ]
                ok, reason = passes_filters(
                    presignal_rows_for_ticker, signal_snap_to.get(ticker)
                )
                if not ok:
                    funnel['failed_filter'] += 1
                    funnel['filter_reasons'][reason] = (
                        funnel['filter_reasons'].get(reason, 0) + 1
                    )
                    continue
                if ticker in holdings:
                    funnel['in_holdings'] += 1; continue
                if ticker in recent_closed:
                    funnel['in_cooldown'] += 1; continue
                if ticker in open_tickers:
                    funnel['already_open'] += 1; continue
                score = max(
                    [to_float(r.get('top_rule_score'), 0)
                     for r in presignal_rows_for_ticker],
                    default=50.0,
                )
                candidates.append({
                    'ticker': ticker,
                    'tier': tier,
                    'families': sorted(fam_strength.keys()),
                    'family_strength': fam_strength,
                    'signal_ids': {
                        'presignal_ids': bundle['presignal_ids'],
                        'entry_signal_ids': bundle['entry_signal_ids'],
                    },
                    'signal_score': score,
                })
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
