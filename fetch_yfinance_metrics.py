#!/usr/bin/env python3
"""
Lightweight Yahoo Finance Fetcher — ATH, Beta, Market Cap, 3Y/5Y Returns
Runs daily after OHLC (6:15 PM IST). Fetches only what can't be derived
from stored Supabase data. Uses batch downloads to minimise API calls.

Key design:
- yf.download() in batches of 50 for price history (ATH + returns)
- yf.Tickers() in batches of 50 for metadata (beta, market cap)
- Graceful per-stock failure — one bad stock never blocks the rest
- Upserts into stock_fundamentals — won't overwrite Screener data

Schedule: 0 13 * * 1-5  (6:30 PM IST / 13:00 UTC), Mon-Fri
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, date
from supabase import create_client
import os, time, traceback, json

# ── Config ────────────────────────────────────────────────────────────────────
SUPABASE_URL = os.getenv('SUPABASE_URL', 'https://hcgyncghmcvylnrmcivj.supabase.co')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')
BATCH_SIZE   = 50   # stocks per yf.download() call
SLEEP_BATCH  = 3    # seconds between batches (rate limit protection)

# ── Fetch tickers from Supabase (all stocks in indian_stock_sectors) ──────────
def get_all_tickers(supabase):
    """Get all .NS tickers from indian_stock_sectors table."""
    result = supabase.table('indian_stock_sectors').select('ticker').execute()
    tickers = [r['ticker'] for r in result.data if r.get('ticker', '').endswith('.NS')]
    print(f"📋 {len(tickers)} tickers from indian_stock_sectors")
    return tickers

# ── Batch download price history ──────────────────────────────────────────────
def fetch_price_metrics_batch(tickers_batch):
    """
    Download max history for a batch of tickers.
    Returns dict: ticker -> {ath_price, ath_date, return_3y, return_5y}
    """
    results = {}
    try:
        # Download max history for all tickers in one call
        hist = yf.download(
            tickers_batch,
            period='max',
            interval='1d',
            auto_adjust=True,
            progress=False,
            threads=True,
        )

        if hist.empty:
            return results

        # Handle single vs multi ticker response
        if len(tickers_batch) == 1:
            ticker = tickers_batch[0]
            closes = hist['Close'] if 'Close' in hist.columns else None
            highs  = hist['High']  if 'High'  in hist.columns else None
            if closes is not None and not closes.empty:
                results[ticker] = _compute_price_metrics(highs, closes)
        else:
            closes = hist['Close'] if 'Close' in hist else None
            highs  = hist['High']  if 'High'  in hist else None
            if closes is not None:
                for ticker in tickers_batch:
                    if ticker in closes.columns:
                        t_closes = closes[ticker].dropna()
                        t_highs  = highs[ticker].dropna() if highs is not None and ticker in highs.columns else t_closes
                        if len(t_closes) > 10:
                            results[ticker] = _compute_price_metrics(t_highs, t_closes)

    except Exception as e:
        print(f"    ⚠️  Batch download error: {e}")

    return results

def _compute_price_metrics(highs, closes):
    """Compute ATH and returns from price series."""
    today_price = float(closes.iloc[-1])

    # ATH
    ath_price = float(highs.max())
    ath_idx   = highs.idxmax()
    ath_date  = ath_idx.date().isoformat() if hasattr(ath_idx, 'date') else str(ath_idx)[:10]

    # Returns
    def ret(n_days):
        if len(closes) > n_days:
            past = float(closes.iloc[-n_days])
            return round((today_price - past) / past * 100, 2) if past > 0 else None
        return None

    # YTD
    year_start = str(date.today().year) + '-01-01'
    ytd_slice  = closes[closes.index >= year_start]
    ytd = round((today_price - float(ytd_slice.iloc[0])) / float(ytd_slice.iloc[0]) * 100, 2) if len(ytd_slice) > 1 else None

    return {
        'ath_price':   round(ath_price, 2),
        'ath_date':    ath_date,
        'return_1w':   ret(5),
        'return_1m':   ret(21),
        'return_3m':   ret(63),
        'return_6m':   ret(126),
        'return_ytd':  ytd,
        'return_1y':   ret(252),
        'return_3y':   ret(756),
        'return_5y':   ret(1260),
    }

# ── Batch fetch metadata ───────────────────────────────────────────────────────
def fetch_metadata_batch(tickers_batch):
    """
    Fetch beta, market cap, PE TTM and valuation ratios for a batch.
    Returns dict: ticker -> metrics
    """
    results = {}
    try:
        tks = yf.Tickers(' '.join(tickers_batch))
        for ticker in tickers_batch:
            try:
                info = tks.tickers[ticker].info or {}
                mcap = info.get('marketCap')
                # Margins from yfinance are 0-1 decimals, convert to %
                def pct(v):
                    s = _safe(v)
                    return round(s * 100, 2) if s is not None and abs(s) <= 1 else s
                results[ticker] = {
                    'beta':              _safe(info.get('beta')),
                    'market_cap_cr':     round(mcap / 1e7, 2) if mcap else None,
                    'pe_ttm':            _safe(info.get('trailingPE')),
                    'eps_ttm':           _safe(info.get('trailingEps')),
                    'price_to_book':     _safe(info.get('priceToBook')),
                    'price_to_sales':    _safe(info.get('priceToSalesTrailing12Months')),
                    'ev_to_ebitda':      _safe(info.get('enterpriseToEbitda')),
                    'roe':               pct(info.get('returnOnEquity')),
                    'net_margin':        pct(info.get('profitMargins')),
                    'ebitda_margin':     pct(info.get('ebitdaMargins')),
                    'gross_margin':      pct(info.get('grossMargins')),
                    'debt_to_equity':    _safe(info.get('debtToEquity')),
                    'dividend_yield':    pct(info.get('dividendYield')),
                    'book_value_per_share': _safe(info.get('bookValue')),
                    'revenue_growth_yoy':   pct(info.get('revenueGrowth')),
                    'earnings_growth_yoy':  pct(info.get('earningsGrowth')),
                }
                # Remove None values
                results[ticker] = {k: v for k, v in results[ticker].items() if v is not None}
            except Exception:
                pass
    except Exception as e:
        print(f"    ⚠️  Metadata batch error: {e}")
    return results

def _safe(val, decimals=4):
    try:
        if val is None or (isinstance(val, float) and np.isnan(val)):
            return None
        return round(float(val), decimals)
    except:
        return None

# ── Save to Supabase ──────────────────────────────────────────────────────────
def save_batch(supabase, records):
    """Upsert a batch of records into stock_fundamentals."""
    if not records:
        return 0, 0
    success, failed = 0, 0
    for record in records:
        try:
            supabase.table('stock_fundamentals').upsert(
                record, on_conflict='ticker'
            ).execute()
            success += 1
        except Exception as e:
            print(f"    ⚠️  Save error {record.get('ticker')}: {e}")
            failed += 1
    return success, failed

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 70)
    print("⚡ LIGHTWEIGHT YAHOO FINANCE FETCHER")
    print(f"   ATH · Beta · Market Cap · Returns (1W–5Y)")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    tickers  = get_all_tickers(supabase)

    if not tickers:
        print("❌ No tickers found")
        return

    # Process in batches
    total_batches = (len(tickers) + BATCH_SIZE - 1) // BATCH_SIZE
    all_price_metrics = {}
    all_metadata      = {}

    print(f"\n📊 Fetching price history in {total_batches} batches of {BATCH_SIZE}...")
    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        print(f"  Batch {batch_num}/{total_batches} ({len(batch)} stocks)...", end=' ', flush=True)
        metrics = fetch_price_metrics_batch(batch)
        all_price_metrics.update(metrics)
        print(f"✓ {len(metrics)}/{len(batch)} succeeded")
        if batch_num < total_batches:
            time.sleep(SLEEP_BATCH)

    print(f"\n📋 Fetching metadata in {total_batches} batches...")
    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        print(f"  Batch {batch_num}/{total_batches}...", end=' ', flush=True)
        meta = fetch_metadata_batch(batch)
        all_metadata.update(meta)
        print(f"✓ {len(meta)}/{len(batch)} succeeded")
        if batch_num < total_batches:
            time.sleep(SLEEP_BATCH)

    # Merge and save
    print(f"\n💾 Saving to Supabase...")
    records = []
    for ticker in tickers:
        pm = all_price_metrics.get(ticker, {})
        md = all_metadata.get(ticker, {})
        if not pm and not md:
            continue
        record = {
            'ticker':          ticker,
            'last_updated':    datetime.utcnow().isoformat(),
            'source_yfinance': True,
        }
        record.update({k: v for k, v in pm.items() if v is not None})
        record.update({k: v for k, v in md.items() if v is not None})
        records.append(record)

    # Save in batches of 50
    total_success, total_failed = 0, 0
    for i in range(0, len(records), 50):
        s, f = save_batch(supabase, records[i:i+50])
        total_success += s
        total_failed  += f

    print(f"\n{'='*70}")
    print(f"✅ Saved: {total_success} | ❌ Failed: {total_failed}")
    print(f"   Price metrics: {len(all_price_metrics)} stocks")
    print(f"   Metadata:      {len(all_metadata)} stocks")
    print(f"Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

if __name__ == '__main__':
    main()
