#!/usr/bin/env python3
"""
Sector Fundamentals Ranker
===========================
Computes within-sector percentile ranks for 9 key fundamental metrics.
Reads from: stock_fundamentals + indian_stock_sectors
Writes to:  stock_fundamentals (sector_pct_* columns)

Percentile convention:
  100 = best in sector for that metric
  0   = worst in sector

For PE and Debt/Equity, lower is better so ranking is inverted.
For all others, higher is better.

Schedule: Saturday after fetch_screener_fundamentals.yml (~10:00 AM IST)
Runtime:  < 1 minute
"""

import os
from datetime import datetime
from collections import defaultdict
from supabase import create_client

SUPABASE_URL = os.getenv('SUPABASE_URL', 'https://hcgyncghmcvylnrmcivj.supabase.co')
SUPABASE_KEY = os.getenv('SUPABASE_KEY', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhjZ3luY2dobWN2eWxucm1jaXZqIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTc1MTQwMTEsImV4cCI6MjA3MzA5MDAxMX0.n8vFVCJe1y_3o8fpAY0IgasZ4eKl7DAogEM3OlHB8Ww')

# Metric config: (source_column, output_column, higher_is_better)
METRICS = [
    ('pe_ttm',           'sector_pct_pe',            False),  # lower PE = better
    ('roe',              'sector_pct_roe',            True),
    ('roce',             'sector_pct_roce',           True),
    ('net_margin',       'sector_pct_net_margin',     True),
    ('ebitda_margin',    'sector_pct_ebitda_margin',  True),
    ('debt_to_equity',   'sector_pct_debt_equity',    False),  # lower debt = better
    ('sales_growth_3y',  'sector_pct_sales_3y',       True),
    ('sales_growth_5y',  'sector_pct_sales_5y',       True),
    ('profit_growth_3y', 'sector_pct_profit_3y',      True),
    ('profit_growth_5y', 'sector_pct_profit_5y',      True),
]


def paginated_fetch(supabase, table, select, batch_size=1000):
    all_data, offset = [], 0
    while True:
        resp = supabase.table(table).select(select)\
            .range(offset, offset + batch_size - 1).execute()
        if not resp.data:
            break
        all_data.extend(resp.data)
        if len(resp.data) < batch_size:
            break
        offset += batch_size
    return all_data


def compute_percentile_rank(values: list, target_value, higher_is_better: bool) -> float:
    """
    Returns percentile rank of target_value within values (0–100).
    100 = best (highest if higher_is_better, lowest if not).
    Uses interpolated percentile for ties.
    """
    if not values or target_value is None:
        return None

    n = len(values)
    if n == 1:
        return 100.0

    sorted_vals = sorted(values)

    if higher_is_better:
        # Count how many are strictly below target
        below = sum(1 for v in sorted_vals if v < target_value)
        equal = sum(1 for v in sorted_vals if v == target_value)
        pct = (below + 0.5 * equal) / n * 100
    else:
        # Lower is better → invert
        above = sum(1 for v in sorted_vals if v > target_value)
        equal = sum(1 for v in sorted_vals if v == target_value)
        pct = (above + 0.5 * equal) / n * 100

    return round(pct, 1)


def main():
    print("=" * 60)
    print("📊 SECTOR FUNDAMENTALS RANKER")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    # ── 1. Fetch sector mapping ───────────────────────────────────────────────
    print("\n📋 Fetching sector mapping...")
    sector_rows = paginated_fetch(supabase, 'indian_stock_sectors', 'ticker, sector')
    ticker_to_sector = {r['ticker']: r['sector'] for r in sector_rows if r.get('sector')}
    print(f"  {len(ticker_to_sector)} tickers mapped to sectors")

    # ── 2. Fetch fundamentals ─────────────────────────────────────────────────
    print("\n📈 Fetching stock fundamentals...")
    cols = 'ticker,' + ','.join(m[0] for m in METRICS)
    fund_rows = paginated_fetch(supabase, 'stock_fundamentals', cols)
    print(f"  {len(fund_rows)} stocks with fundamentals")

    # ── 3. Group by sector ────────────────────────────────────────────────────
    # sector → metric → list of (ticker, value)
    sector_data = defaultdict(lambda: defaultdict(list))

    for row in fund_rows:
        ticker = row['ticker']
        sector = ticker_to_sector.get(ticker)
        if not sector:
            continue
        for src_col, _, _ in METRICS:
            val = row.get(src_col)
            if val is not None:
                try:
                    sector_data[sector][src_col].append((ticker, float(val)))
                except:
                    pass

    sectors = sorted(sector_data.keys())
    print(f"  {len(sectors)} sectors with data")

    # ── 4. Compute percentiles ────────────────────────────────────────────────
    print("\n🔢 Computing sector percentile ranks...")

    # ticker → {output_col: percentile}
    ticker_percentiles = defaultdict(dict)

    for sector in sectors:
        for src_col, out_col, higher_is_better in METRICS:
            pairs = sector_data[sector][src_col]  # [(ticker, value), ...]
            if not pairs:
                continue
            values = [v for _, v in pairs]
            for ticker, val in pairs:
                pct = compute_percentile_rank(values, val, higher_is_better)
                if pct is not None:
                    ticker_percentiles[ticker][out_col] = pct

    print(f"  Computed ranks for {len(ticker_percentiles)} tickers")

    # ── 5. Write back to Supabase ─────────────────────────────────────────────
    print(f"\n💾 Writing percentile ranks...")
    success, failed = 0, 0

    for ticker, percentiles in ticker_percentiles.items():
        if not percentiles:
            continue
        try:
            supabase.table('stock_fundamentals')\
                .update(percentiles)\
                .eq('ticker', ticker)\
                .execute()
            success += 1
        except Exception as e:
            print(f"  ⚠️ {ticker}: {e}")
            failed += 1

    # ── 6. Summary ────────────────────────────────────────────────────────────
    print(f"\n✅ Done: {success} updated, {failed} failed")

    # Sample output for validation
    print("\n📊 Sample — top 5 by ROE sector percentile:")
    top_roe = sorted(
        [(t, p.get('sector_pct_roe', 0)) for t, p in ticker_percentiles.items() if p.get('sector_pct_roe')],
        key=lambda x: -x[1]
    )[:5]
    for ticker, pct in top_roe:
        sector = ticker_to_sector.get(ticker, '—')
        print(f"  {ticker:22s} {sector:25s} ROE pct: {pct:.0f}")

    print(f"\nCompleted: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)


if __name__ == "__main__":
    main()
