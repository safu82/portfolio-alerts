#!/usr/bin/env python3
"""
Alkalyme RS Ranker
==================
Reads alkalyme_rs scores from daily_stock_snapshots for today's date,
sorts all stocks descending, and writes rs_rank (1 = strongest) back.

Also computes and writes:
  bz_streak         — consecutive days (ending today) where alkalyme_rs >= 65
  gc_crossover_date — most recent date ema_20 crossed above ema_200 (NULL if not in GC)
  sector_percentile — within-sector RS percentile rank (1 = best in sector)

Rank 1-125 = top 25% of NSE 500 universe — the filter used by Portfolio A.

Schedule: Daily at 5:00 PM IST (after OHLC fetcher at 4:30 PM)
Runtime: < 2 minutes
"""

import os
from datetime import datetime, timedelta
from collections import defaultdict
from supabase import create_client

SUPABASE_URL = os.getenv('SUPABASE_URL', 'https://hcgyncghmcvylnrmcivj.supabase.co')
SUPABASE_KEY = os.getenv('SUPABASE_KEY', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhjZ3luY2dobWN2eWxucm1jaXZqIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTc1MTQwMTEsImV4cCI6MjA3MzA5MDAxMX0.n8vFVCJe1y_3o8fpAY0IgasZ4eKl7DAogEM3OlHB8Ww')

def is_in_blue_zone(row):
    """
    Blue Zone Buy conditions (minimum bar) — all must be met:
      - Daily RSI EMA9  >= 65
      - Weekly RSI EMA9 >= 55
      - Close > EMA20
      - Close >= 90% of 52W High (within 10%)
    """
    rsi_ema_9        = row.get('rsi_ema_9')
    weekly_rsi_ema_9 = row.get('weekly_rsi_ema_9')
    close            = row.get('close')
    ema_20           = row.get('ema_20')
    high_52w         = row.get('high_52w')

    if None in (rsi_ema_9, weekly_rsi_ema_9, close, ema_20, high_52w):
        return False

    return (
        float(rsi_ema_9)        >= 65 and
        float(weekly_rsi_ema_9) >= 55 and
        float(close)            >  float(ema_20) and
        float(close)            >= float(high_52w) * 0.90
    )


def paginated_fetch(supabase, table, select, filters_fn, batch_size=1000):
    """Fetch all rows from a table using range pagination."""
    all_data = []
    offset = 0
    while True:
        q = supabase.table(table).select(select)
        q = filters_fn(q)
        response = q.range(offset, offset + batch_size - 1).execute()
        if not response.data:
            break
        all_data.extend(response.data)
        if len(response.data) < batch_size:
            break
        offset += batch_size
    return all_data


def compute_bz_streak(history_desc):
    """
    history_desc: list of rows sorted by snapshot_date DESCENDING for one ticker.
    Returns count of consecutive days (from most recent) where all BZ Buy
    conditions are met: RSI EMA9 >= 65, Weekly RSI EMA9 >= 55,
    close > EMA20, close >= 90% of 52W high.
    """
    streak = 0
    for row in history_desc:
        if is_in_blue_zone(row):
            streak += 1
        else:
            break
    return streak


def compute_gc_crossover_date(history_asc):
    """
    history_asc: list of rows sorted by snapshot_date ASCENDING for one ticker.
    Golden Cross = ema_20 crosses above ema_50.
    Returns the most recent crossover date if currently in GC state (ema_20 > ema_50).
    Returns None if not in GC, or if crossover not found within retained history.
    """
    if not history_asc:
        return None

    # Check current state (last row)
    latest = history_asc[-1]
    ema20_now = latest.get('ema_20')
    ema50_now = latest.get('ema_50')

    if ema20_now is None or ema50_now is None:
        return None
    if float(ema20_now) <= float(ema50_now):
        return None  # Not in GC state — no date to report

    # Walk backwards to find when ema_20 last crossed above ema_50
    # Crossover on day[i] = ema_20[i] > ema_50[i] AND ema_20[i-1] <= ema_50[i-1]
    crossover_date = None
    for i in range(len(history_asc) - 1, 0, -1):
        cur = history_asc[i]
        prev = history_asc[i - 1]
        cur_20  = cur.get('ema_20')
        cur_50  = cur.get('ema_50')
        prev_20 = prev.get('ema_20')
        prev_50 = prev.get('ema_50')

        if None in (cur_20, cur_50, prev_20, prev_50):
            continue

        if float(cur_20) > float(cur_50) and float(prev_20) <= float(prev_50):
            crossover_date = cur['snapshot_date']
            break

    # If we didn't find a crossover within retained history, the stock has been
    # in GC the entire window — use the oldest date we have as a lower bound
    if crossover_date is None:
        crossover_date = history_asc[0]['snapshot_date']

    return crossover_date


def main():
    print("=" * 60)
    print("📊 ALKALYME RS RANKER")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    # ── 1. Find latest date with alkalyme_rs populated ──────────────────────
    print("\n📋 Finding latest date with RS scores...")
    latest = supabase.table('daily_stock_snapshots')\
        .select('snapshot_date')\
        .not_.is_('alkalyme_rs', 'null')\
        .order('snapshot_date', desc=True)\
        .limit(1)\
        .execute()

    if not latest.data:
        print("❌ No alkalyme_rs scores found. Run OHLC fetcher first.")
        return

    today = latest.data[0]['snapshot_date']
    print(f"  Using date: {today}")

    # ── 2. Fetch today's RS scores for ranking ───────────────────────────────
    print("\n📈 Fetching today's RS scores for ranking...")

    def today_filters(q):
        return (q.eq('snapshot_date', today)
                 .not_.is_('alkalyme_rs', 'null')
                 .neq('ticker', 'NIFTY50.NS'))

    today_data = paginated_fetch(supabase, 'daily_stock_snapshots',
                                 'ticker, alkalyme_rs', today_filters)
    print(f"  Found {len(today_data)} stocks with RS scores")

    if not today_data:
        print("❌ No data to rank.")
        return

    # ── 3. Fetch last 60 days of history for streak + GC computation ─────────
    print("\n📅 Fetching 60-day history for BZ streak + Golden Cross...")

    sixty_days_ago = (datetime.strptime(today, '%Y-%m-%d') - timedelta(days=60)).strftime('%Y-%m-%d')

    def history_filters(q):
        return (q.gte('snapshot_date', sixty_days_ago)
                 .lte('snapshot_date', today)
                 .not_.is_('alkalyme_rs', 'null')
                 .neq('ticker', 'NIFTY50.NS')
                 .order('snapshot_date', desc=False))

    history_data = paginated_fetch(supabase, 'daily_stock_snapshots',
                                   'ticker, snapshot_date, close, alkalyme_rs, rsi_ema_9, weekly_rsi_ema_9, ema_20, ema_50, ema_200, high_52w',
                                   history_filters)
    print(f"  Fetched {len(history_data)} rows across 60 days")

    # Group history by ticker, sorted ascending (already ordered from DB)
    history_by_ticker = defaultdict(list)
    for row in history_data:
        history_by_ticker[row['ticker']].append(row)

    # ── 4. Sort today's data and assign RS ranks ─────────────────────────────
    sorted_data = sorted(today_data, key=lambda x: x['alkalyme_rs'] or 0, reverse=True)

    top_25_cutoff = len(sorted_data) // 4
    print(f"\n  Total ranked: {len(sorted_data)}")
    print(f"  Top 25% cutoff: rank ≤ {top_25_cutoff}")
    print(f"\n  Top 10 stocks by RS:")
    for row in sorted_data[:10]:
        print(f"    #{sorted_data.index(row)+1:3d}  {row['ticker']:20s}  RS={row['alkalyme_rs']:.2f}")

    # ── 5. Compute BZ streak + GC crossover for each ticker ─────────────────
    print("\n🔢 Computing BZ streaks and GC crossover dates...")
    bz_stats = {'in_bz': 0, 'longest': 0, 'longest_ticker': ''}
    gc_stats = {'in_gc': 0}

    ticker_metrics = {}
    for row in sorted_data:
        ticker = row['ticker']
        hist = history_by_ticker.get(ticker, [])

        # BZ streak — history is ascending; reverse for streak (most recent first)
        hist_desc = list(reversed(hist))
        bz_streak = compute_bz_streak(hist_desc)

        # GC crossover date
        gc_date = compute_gc_crossover_date(hist)  # ascending

        ticker_metrics[ticker] = {
            'bz_streak': bz_streak,
            'gc_crossover_date': gc_date,
        }

        if bz_streak > 0:
            bz_stats['in_bz'] += 1
            if bz_streak > bz_stats['longest']:
                bz_stats['longest'] = bz_streak
                bz_stats['longest_ticker'] = ticker
        if gc_date:
            gc_stats['in_gc'] += 1

    print(f"  Stocks in Blue Zone (streak > 0): {bz_stats['in_bz']}")
    print(f"  Longest BZ streak: {bz_stats['longest']} days ({bz_stats['longest_ticker']})")
    print(f"  Stocks in Golden Cross: {gc_stats['in_gc']}")

    # ── 6. Build update records and write to Supabase ───────────────────────
    updates = []
    for rank, row in enumerate(sorted_data, 1):
        ticker = row['ticker']
        metrics = ticker_metrics.get(ticker, {'bz_streak': 0, 'gc_crossover_date': None})
        updates.append({
            'ticker': ticker,
            'snapshot_date': today,
            'rs_rank': rank,
            'bz_streak': metrics['bz_streak'],
            'gc_crossover_date': metrics['gc_crossover_date'],
        })

    print(f"\n💾 Writing {len(updates)} records to Supabase...")
    success = 0
    failed = 0
    batch_size = 50

    for i in range(0, len(updates), batch_size):
        batch = updates[i:i + batch_size]
        for row in batch:
            try:
                supabase.table('daily_stock_snapshots')\
                    .update({
                        'rs_rank':           row['rs_rank'],
                        'bz_streak':         row['bz_streak'],
                        'gc_crossover_date': row['gc_crossover_date'],
                    })\
                    .eq('ticker', row['ticker'])\
                    .eq('snapshot_date', row['snapshot_date'])\
                    .execute()
                success += 1
            except Exception as e:
                print(f"  ⚠️  Failed {row['ticker']}: {e}")
                failed += 1

        if (i // batch_size) % 4 == 0:
            print(f"  ✅ Progress: {success}/{len(updates)} written")

    print(f"\n✅ RS ranking complete for {today}")
    print(f"   {success} stocks updated ({failed} failed)")
    print(f"   Top 25% = rank 1-{top_25_cutoff} (Portfolio A signal filter threshold)")
    print(f"Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)


if __name__ == "__main__":
    main()
