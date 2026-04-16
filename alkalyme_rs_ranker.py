#!/usr/bin/env python3
"""
Alkalyme RS Ranker
==================
Reads alkalyme_rs scores from daily_stock_snapshots for today's date,
sorts all stocks descending, and writes rs_rank (1 = strongest) back.

Rank 1-125 = top 25% of NSE 500 universe — the filter used by Portfolio A.

Schedule: Daily at 5:00 PM IST (after OHLC fetcher at 4:30 PM)
Runtime: < 1 minute
"""

import os
from datetime import datetime
from supabase import create_client

SUPABASE_URL = os.getenv('SUPABASE_URL', 'https://hcgyncghmcvylnrmcivj.supabase.co')
SUPABASE_KEY = os.getenv('SUPABASE_KEY', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhjZ3luY2dobWN2eWxucm1jaXZqIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTc1MTQwMTEsImV4cCI6MjA3MzA5MDAxMX0.n8vFVCJe1y_3o8fpAY0IgasZ4eKl7DAogEM3OlHB8Ww')


def main():
    print("=" * 60)
    print("📊 ALKALYME RS RANKER")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    # Get latest date with alkalyme_rs populated
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

    # Fetch all stocks with RS scores for today
    print("\n📈 Fetching RS scores...")
    all_data = []
    offset = 0
    batch_size = 1000

    while True:
        response = supabase.table('daily_stock_snapshots')\
            .select('ticker, alkalyme_rs')\
            .eq('snapshot_date', today)\
            .not_.is_('alkalyme_rs', 'null')\
            .neq('ticker', 'NIFTY50.NS')\
            .range(offset, offset + batch_size - 1)\
            .execute()

        if not response.data:
            break
        all_data.extend(response.data)
        if len(response.data) < batch_size:
            break
        offset += batch_size

    print(f"  Found {len(all_data)} stocks with RS scores")

    if not all_data:
        print("❌ No data to rank.")
        return

    # Sort descending by alkalyme_rs — rank 1 = strongest
    sorted_data = sorted(all_data, key=lambda x: x['alkalyme_rs'] or 0, reverse=True)

    # Assign ranks and build upsert records
    updates = []
    for rank, row in enumerate(sorted_data, 1):
        updates.append({
            'ticker': row['ticker'],
            'snapshot_date': today,
            'rs_rank': rank
        })

    top_25_cutoff = len(sorted_data) // 4
    print(f"\n  Total ranked: {len(updates)}")
    print(f"  Top 25% cutoff: rank ≤ {top_25_cutoff}")
    print(f"\n  Top 10 stocks by RS:")
    for row in updates[:10]:
        rs = next(d['alkalyme_rs'] for d in all_data if d['ticker'] == row['ticker'])
        print(f"    #{row['rs_rank']:3d}  {row['ticker']:20s}  RS={rs:.2f}")

    # Upsert ranks in batches
    print(f"\n💾 Writing {len(updates)} ranks to Supabase...")
    # Use UPDATE not upsert — rows must already exist from OHLC fetcher
    # Batch by updating in groups using .in_() filter trick:
    # For each rank value, update all tickers with that rank at once
    # This is more efficient than one-by-one but still correct
    success = 0
    batch_size = 50
    for i in range(0, len(updates), batch_size):
        batch = updates[i:i + batch_size]
        for row in batch:
            try:
                supabase.table('daily_stock_snapshots')\
                    .update({'rs_rank': row['rs_rank']})\
                    .eq('ticker', row['ticker'])\
                    .eq('snapshot_date', row['snapshot_date'])\
                    .execute()
                success += 1
            except Exception as e:
                print(f"  ⚠️  Failed {row['ticker']}: {e}")
        if (i // batch_size) % 2 == 0:
            print(f"  ✅ Progress: {success}/{len(updates)} ranks written")

    print(f"\n✅ RS ranking complete for {today}")
    print(f"   {len(updates)} stocks ranked")
    print(f"   Top 25% = rank 1-{top_25_cutoff} (Portfolio A signal filter threshold)")
    print(f"Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)


if __name__ == "__main__":
    main()
