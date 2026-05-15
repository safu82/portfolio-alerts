#!/usr/bin/env python3
"""
One-shot backfill of max_unrealized_pct / min_unrealized_pct for paper_trades.

Walks every trade (defaults to open + pending; pass --include-closed for
historical analysis) and computes the running high-water (% gain from
bar_high) and low-water (% loss from bar_low) marks across the holding
window using daily_stock_snapshots.

Usage:
    python backfill_paper_mfe.py                 # open + pending only
    python backfill_paper_mfe.py --include-closed
    python backfill_paper_mfe.py --dry-run       # print, don't UPDATE
"""

import argparse
import os
from datetime import date, datetime

from supabase import create_client

SUPABASE_URL = os.getenv('SUPABASE_URL', 'https://hcgyncghmcvylnrmcivj.supabase.co')
SUPABASE_KEY = os.getenv(
    'SUPABASE_KEY',
    'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhjZ3luY2dobWN2eWxucm1jaXZqIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTc1MTQwMTEsImV4cCI6MjA3MzA5MDAxMX0.n8vFVCJe1y_3o8fpAY0IgasZ4eKl7DAogEM3OlHB8Ww',
)


def fnum(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def backfill_trade(sb, tr, dry_run=False):
    entry_price = fnum(tr['entry_price'])
    if not entry_price or entry_price <= 0:
        return None, None, 'no_entry_price'
    entry_date = tr['entry_date']
    today = date.today().isoformat()
    exit_date = tr.get('exit_date') or today

    # Pull all daily bars in the holding window for this ticker.
    snaps = (sb.table('daily_stock_snapshots')
               .select('snapshot_date, high, low')
               .eq('ticker', tr['ticker'])
               .gte('snapshot_date', entry_date)
               .lte('snapshot_date', exit_date)
               .order('snapshot_date')
               .execute()).data or []
    if not snaps:
        return None, None, 'no_snapshots'

    max_mfe = None
    min_mae = None
    for s in snaps:
        hi = fnum(s.get('high'))
        lo = fnum(s.get('low'))
        if hi is not None:
            pct = (hi - entry_price) / entry_price * 100
            max_mfe = pct if max_mfe is None else max(max_mfe, pct)
        if lo is not None:
            pct = (lo - entry_price) / entry_price * 100
            min_mae = pct if min_mae is None else min(min_mae, pct)

    if max_mfe is None or min_mae is None:
        return None, None, 'no_high_low_data'

    if not dry_run:
        sb.table('paper_trades').update({
            'max_unrealized_pct': round(max_mfe, 3),
            'min_unrealized_pct': round(min_mae, 3),
            'updated_at': datetime.utcnow().isoformat(),
        }).eq('id', tr['id']).execute()

    return max_mfe, min_mae, f'ok (n_bars={len(snaps)})'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--include-closed', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    statuses = ['open', 'pending']
    if args.include_closed:
        statuses.append('closed')

    rows = (sb.table('paper_trades')
              .select('id, ticker, status, strategy_tier, entry_date, exit_date, '
                      'entry_price, initial_stop')
              .in_('status', statuses)
              .eq('mode', 'paper')
              .order('entry_date')
              .execute()).data or []

    print(f'Backfilling {len(rows)} trades (statuses={statuses}, '
          f'dry_run={args.dry_run})')
    print(f'{"ticker":<16} {"tier":<18} {"entry_date":<12} {"MFE%":>8} '
          f'{"MAE%":>8}  status')
    print('-' * 80)

    for tr in rows:
        mfe, mae, status = backfill_trade(sb, tr, dry_run=args.dry_run)
        mfe_s = f'{mfe:+.2f}' if mfe is not None else '   —'
        mae_s = f'{mae:+.2f}' if mae is not None else '   —'
        print(f'{tr["ticker"]:<16} {tr["strategy_tier"]:<18} {tr["entry_date"]:<12} '
              f'{mfe_s:>8} {mae_s:>8}  {status}')


if __name__ == '__main__':
    main()
