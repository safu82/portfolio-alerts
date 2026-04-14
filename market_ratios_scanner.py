#!/usr/bin/env python3
"""
Market Ratios Scanner
Fetches and calculates key market ratios for macro analysis:
- Gold/Silver Ratio
- India VIX
- DXY (US Dollar Index) — replaces MC/GDP (NSE blocks cloud IPs)
- Brent Crude Oil

Schedule: Daily at 7:00 AM IST (before market open)
Runtime: ~30 seconds
"""

import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from supabase import create_client, Client
import os

# =============================================================================
# CONFIGURATION
# =============================================================================

SUPABASE_URL = os.getenv('SUPABASE_URL', 'https://hcgyncghmcvylnrmcivj.supabase.co')
SUPABASE_KEY = os.getenv('SUPABASE_KEY', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhjZ3luY2dobWN2eWxucm1jaXZqIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTc1MTQwMTEsImV4cCI6MjA3MzA5MDAxMX0.n8vFVCJe1y_3o8fpAY0IgasZ4eKl7DAogEM3OlHB8Ww')

TICKERS = {
    'gold':      'GC=F',      # Gold Futures
    'silver':    'SI=F',      # Silver Futures
    'india_vix': '^INDIAVIX', # India VIX
    'nifty_50':  '^NSEI',     # Nifty 50
    'dxy':       'DX-Y.NYB',  # US Dollar Index
    'crude_oil': 'BZ=F',      # Brent Crude Futures
}

# =============================================================================
# FUNCTIONS
# =============================================================================

def init_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)

def get_latest_price(ticker: str, days_back: int = 5) -> float:
    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days_back)
        data = yf.download(ticker, start=start_date, end=end_date, progress=False)
        if data.empty:
            return None
        if isinstance(data['Close'], pd.Series):
            return float(data['Close'].iloc[-1])
        return float(data['Close'].iloc[-1].item())
    except Exception as e:
        print(f"  ❌ Error fetching {ticker}: {e}")
        return None

def fetch_market_ratios():
    print("\n" + "="*60)
    print("📊 MARKET RATIOS SCANNER")
    print("="*60)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    ratios = {}

    # Gold
    print("\n🥇 Fetching Gold...")
    gold = get_latest_price(TICKERS['gold'])
    if gold:
        ratios['gold_price'] = gold
        print(f"  ✅ Gold: ${gold:.2f}")

    # Silver
    print("🥈 Fetching Silver...")
    silver = get_latest_price(TICKERS['silver'])
    if silver:
        ratios['silver_price'] = silver
        print(f"  ✅ Silver: ${silver:.2f}")

    # Gold/Silver Ratio
    if gold and silver:
        ratios['gold_silver_ratio'] = round(gold / silver, 2)
        print(f"  ✅ Gold/Silver Ratio: {ratios['gold_silver_ratio']}")

    # India VIX
    print("📊 Fetching India VIX...")
    vix = get_latest_price(TICKERS['india_vix'])
    if vix:
        ratios['india_vix'] = vix
        print(f"  ✅ India VIX: {vix:.2f}")

    # Nifty 50
    print("📈 Fetching Nifty 50...")
    nifty = get_latest_price(TICKERS['nifty_50'])
    if nifty:
        ratios['nifty_50'] = nifty
        print(f"  ✅ Nifty 50: {nifty:.2f}")

    # DXY
    print("💵 Fetching DXY...")
    dxy = get_latest_price(TICKERS['dxy'])
    if dxy:
        ratios['dxy'] = dxy
        print(f"  ✅ DXY: {dxy:.2f}")

    # Brent Crude
    print("🛢️  Fetching Brent Crude...")
    crude = get_latest_price(TICKERS['crude_oil'])
    if crude:
        ratios['crude_oil'] = crude
        print(f"  ✅ Brent Crude: ${crude:.2f}")

    return ratios

def save_to_supabase(supabase: Client, ratios: dict):
    try:
        today = datetime.now().date().isoformat()
        record = {'snapshot_date': today, **ratios}
        supabase.table('market_ratios').upsert(
            record, on_conflict='snapshot_date'
        ).execute()
        print(f"\n💾 Saved {len(ratios)} ratios for {today}")
        return True
    except Exception as e:
        print(f"\n❌ Failed to save: {e}")
        return False

def main():
    supabase = init_supabase()
    ratios = fetch_market_ratios()
    if ratios:
        save_to_supabase(supabase, ratios)

    print("\n" + "="*60)
    print(f"✅ Done — {len(ratios)} ratios saved")
    print(f"Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*60)

if __name__ == "__main__":
    main()
