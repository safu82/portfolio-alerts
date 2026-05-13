#!/usr/bin/env python3
"""
Paper Trade Fill Job
====================
Runs at D1 09:20 IST (after the scanner has produced pending rows at D0 22:00).
For each pending paper_trades row:

  1. Look up live_prices.price for the ticker.
  2. If found: recompute entry_price, qty, initial_stop using D1 open + D0 ATR,
     update the row to status='open' with entry_date=today.
  3. If not found: leave pending; next fill run will retry.
  4. If pending is older than 2 trading days: close with exit_reason='fill_expired'.

The position floor (Rs 40k) and sizing maths are re-applied — if D1's open is
high enough that qty drops to 0, the row is closed with exit_reason='fill_rejected'.

Schedule: weekdays at 03:50 UTC (09:20 IST). Idempotent.
"""

import os
import sys
import traceback
from datetime import date, datetime

from supabase import create_client

# Reuse constants and helpers from paper_trader so they stay in sync.
from paper_trader import (
    SUPABASE_URL, SUPABASE_KEY,
    SLIPPAGE_BPS, STOP_ATR_MULT, TIER_PARAMS, POSITION_FLOOR,
    size_position, trading_days_between, to_float, to_int, log,
    load_pending_paper_trades,
)


PENDING_MAX_TRADING_DAYS = 2


def fetch_live_price(sb, ticker):
    """Return the latest live_prices.price for ticker, or None."""
    resp = (sb.table('live_prices').select('price, updated_at')
              .eq('ticker', ticker).limit(1).execute())
    row = (resp.data or [None])[0]
    if not row:
        return None
    return to_float(row.get('price'))


def expire_pending(sb, tr, today, reason):
    sb.table('paper_trades').update({
        'status': 'closed',
        'exit_date': today.isoformat(),
        'exit_reason': reason,
        'exit_price': None,
        'total_pnl': 0,
        'total_pnl_pct': 0,
        'current_quantity': 0,
        'updated_at': datetime.utcnow().isoformat(),
    }).eq('id', tr['id']).execute()


def fill_pending(sb, tr, live_price, today):
    """Convert a pending row to an open row using D1 open (live_price)."""
    tier = tr['strategy_tier']
    entry_atr = to_float(tr.get('entry_atr'))
    if not entry_atr or entry_atr <= 0:
        expire_pending(sb, tr, today, 'fill_rejected_missing_atr')
        return 'rejected_no_atr'

    entry_price = live_price * (1 + SLIPPAGE_BPS / 10_000)
    qty, _, _ = size_position(tier, entry_price, entry_atr)
    if qty == 0:
        expire_pending(sb, tr, today, 'fill_rejected_position_floor')
        return 'rejected_qty_zero'

    initial_stop = entry_price - STOP_ATR_MULT * entry_atr
    initial_risk = qty * (entry_price - initial_stop)

    sb.table('paper_trades').update({
        'entry_date': today.isoformat(),
        'entry_price': round(entry_price, 4),
        'initial_quantity': qty,
        'current_quantity': qty,
        'entry_value': round(qty * entry_price, 2),
        'initial_risk': round(initial_risk, 2),
        'initial_stop': round(initial_stop, 4),
        'current_stop': round(initial_stop, 4),
        'status': 'open',
        'updated_at': datetime.utcnow().isoformat(),
    }).eq('id', tr['id']).execute()
    return 'filled'


def main():
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    today = datetime.now().date()
    log(f'paper_fill_pending  today={today}')

    pending = load_pending_paper_trades(sb)
    log(f'pending_rows={len(pending)}')
    if not pending:
        return

    counts = {'filled': 0, 'no_live_price': 0, 'expired': 0,
              'rejected_no_atr': 0, 'rejected_qty_zero': 0, 'errors': 0}

    for tr in pending:
        ticker = tr['ticker']
        try:
            scan_date = date.fromisoformat(tr['entry_date'])
            age_td = trading_days_between(scan_date, today)

            live_price = fetch_live_price(sb, ticker)
            if not live_price or live_price <= 0:
                if age_td > PENDING_MAX_TRADING_DAYS:
                    expire_pending(sb, tr, today, 'fill_expired')
                    counts['expired'] += 1
                    log(f'  {ticker}: expired (age={age_td}td, no live price)')
                else:
                    counts['no_live_price'] += 1
                    log(f'  {ticker}: no live price yet (age={age_td}td) -- retry next run')
                continue

            outcome = fill_pending(sb, tr, live_price, today)
            counts[outcome] = counts.get(outcome, 0) + 1
            if outcome == 'filled':
                log(f'  {ticker}: filled @ {live_price:.2f} ({tr["strategy_tier"]})')
            else:
                log(f'  {ticker}: {outcome}')
        except Exception as e:
            counts['errors'] += 1
            log(f'  {ticker}: ERROR {e}')
            traceback.print_exc()

    log(f'done: {counts}')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
