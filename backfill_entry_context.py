#!/usr/bin/env python3
"""
One-shot backfill of the entry-context columns on paper_trades
(entry_dist_ema20_pct, entry_dist_ema50_pct, entry_atr_pct,
entry_prior_1d_pct, entry_prior_3d_pct, entry_bar_range_atr,
entry_market_regime).

For each trade, the context is computed AS-OF the D0 scan date — the
trading day before entry_date — using the exact same helpers the live
paper_trader uses, so backfilled rows match what a fresh run would write.

Usage:
    python backfill_entry_context.py            # open + pending
    python backfill_entry_context.py --include-closed
    python backfill_entry_context.py --dry-run
"""

import argparse
import os
from datetime import datetime

from supabase import create_client

from paper_trader import (
    SUPABASE_URL, SUPABASE_KEY,
    load_snapshots_for_date, compute_entry_context, compute_market_regime,
)

_BENCH = 'RELIANCE.NS'


def trading_dates_before(sb, before_date, n=5):
    """The n most recent trading dates strictly before before_date,
    newest first — taken from a liquid benchmark to dodge the 1000-row cap."""
    resp = (sb.table('daily_stock_snapshots').select('snapshot_date')
              .eq('ticker', _BENCH).lt('snapshot_date', before_date)
              .order('snapshot_date', desc=True).limit(n).execute())
    return [r['snapshot_date'] for r in (resp.data or [])]


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
              .select('id, ticker, status, strategy_tier, entry_date')
              .in_('status', statuses).eq('mode', 'paper')
              .order('entry_date').execute()).data or []

    print(f'Backfilling entry context for {len(rows)} trades '
          f'(statuses={statuses}, dry_run={args.dry_run})')

    snap_cache = {}

    def snap(dt):
        if dt not in snap_cache:
            snap_cache[dt] = load_snapshots_for_date(sb, dt)
        return snap_cache[dt]

    for tr in rows:
        ticker = tr['ticker']
        entry_date = tr['entry_date']
        dates = trading_dates_before(sb, entry_date, n=5)
        if not dates:
            print(f'  {ticker:<16} entry={entry_date}  SKIP — no prior trading dates')
            continue

        d0 = dates[0]                                  # scan date
        d_prior = dates[1] if len(dates) > 1 else None  # D0 - 1
        d_3 = dates[3] if len(dates) > 3 else None      # D0 - 3

        d0_snap = snap(d0)
        prior_snap = snap(d_prior) if d_prior else {}
        snap_3d = snap(d_3) if d_3 else {}
        regime = compute_market_regime(sb, d0)

        ctx = compute_entry_context(ticker, d0_snap, prior_snap, snap_3d, regime)

        print(f'  {ticker:<16} {tr["strategy_tier"]:<16} scan={d0}  '
              f'ema20={ctx.get("entry_dist_ema20_pct")}  '
              f'atr%={ctx.get("entry_atr_pct")}  '
              f'1d={ctx.get("entry_prior_1d_pct")}  '
              f'3d={ctx.get("entry_prior_3d_pct")}  '
              f'rangeATR={ctx.get("entry_bar_range_atr")}  '
              f'regime={ctx.get("entry_market_regime")}')

        if not args.dry_run:
            sb.table('paper_trades').update({
                **ctx,
                'updated_at': datetime.utcnow().isoformat(),
            }).eq('id', tr['id']).execute()

    print('Done.' + ('  (dry-run — no writes)' if args.dry_run else ''))


if __name__ == '__main__':
    main()
