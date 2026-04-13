#!/usr/bin/env python3
"""
Market Cap Updater
Fetches market cap from yfinance fast_info and updates holdings table in Supabase.

Schedule: Weekly on Sundays at 6:00 AM IST (00:30 UTC)
Runtime: ~2-3 minutes for ~35 holdings
"""

import yfinance as yf
from supabase import create_client, Client
import time
import os
from datetime import datetime

# =============================================================================
# CONFIGURATION
# =============================================================================

SUPABASE_URL = os.getenv('SUPABASE_URL', 'https://hcgyncghmcvylnrmcivj.supabase.co')
SUPABASE_KEY = os.getenv('SUPABASE_KEY', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhjZ3luY2dobWN2eWxucm1jaXZqIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTc1MTQwMTEsImV4cCI6MjA3MzA5MDAxMX0.n8vFVCJe1y_3o8fpAY0IgasZ4eKl7DAogEM3OlHB8Ww')

RATE_LIMIT_DELAY = 1.0  # seconds between yfinance calls

# Tickers that yfinance cannot resolve — assign fixed market cap manually
# ETFs and BSE-only stocks often fail fast_info
MANUAL_MARKET_CAPS = {
    'GOLDBEES.NS':   0,   # ETF
    'SILVERBEES.NS': 0,   # ETF
    'METALIETF.NS':  0,   # ETF
    'MODEFENCE.NS':  0,   # ETF
    'NARMP.BO':      500, # Small BSE stock, ~₹500Cr estimate
}

# =============================================================================
# MAIN
# =============================================================================

def init_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def fetch_market_cap_crores(ticker: str) -> float | None:
    """
    Fetch market cap from yfinance fast_info.
    Returns value in crores, or None on failure.
    """
    try:
        fast_info = yf.Ticker(ticker).fast_info
        market_cap = fast_info.market_cap  # in absolute currency units (INR for .NS)
        if market_cap and market_cap > 0:
            return round(market_cap / 1e7, 2)  # convert to crores
        return None
    except Exception as e:
        print(f"  ⚠️  yfinance error for {ticker}: {e}")
        return None


def main():
    print("=" * 60)
    print("📊 MARKET CAP UPDATER")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    supabase = init_supabase()

    # Fetch all Indian portfolio holdings
    print("\n📋 Fetching holdings from Supabase...")
    response = supabase.table('holdings').select('ticker').eq('portfolio', 'INDIAN').execute()

    if not response.data:
        print("❌ No holdings found. Exiting.")
        return

    tickers = [r['ticker'] for r in response.data]
    print(f"✅ Found {len(tickers)} holdings\n")

    updated = 0
    failed = 0
    manual = 0

    for ticker in tickers:
        # Use manual override if defined
        if ticker in MANUAL_MARKET_CAPS:
            market_cap = MANUAL_MARKET_CAPS[ticker]
            source = "manual"
            manual += 1
        else:
            market_cap = fetch_market_cap_crores(ticker)
            source = "yfinance"
            time.sleep(RATE_LIMIT_DELAY)

        if market_cap is None:
            print(f"  ❌ {ticker:25} — fetch failed, keeping existing value")
            failed += 1
            continue

        # Upsert market cap into holdings
        try:
            supabase.table('holdings')\
                .update({'market_cap_crores': market_cap})\
                .eq('ticker', ticker)\
                .eq('portfolio', 'INDIAN')\
                .execute()

            print(f"  ✅ {ticker:25} ₹{market_cap:>10,.0f} Cr  [{source}]")
            updated += 1

        except Exception as e:
            print(f"  ❌ {ticker:25} DB error: {e}")
            failed += 1

    # Summary
    print("\n" + "=" * 60)
    print("📈 SUMMARY")
    print("=" * 60)
    print(f"✅ Updated : {updated}")
    print(f"📝 Manual  : {manual}")
    print(f"❌ Failed  : {failed}")
    print(f"Completed  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)


if __name__ == "__main__":
    main()
