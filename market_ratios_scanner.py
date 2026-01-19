#!/usr/bin/env python3
"""
Market Ratios Scanner
Fetches and calculates key market ratios for macro analysis:
- Gold/Silver Ratio
- India VIX
- Small Cap/Large Cap Ratio (Nifty SmallCap 100 / Nifty 50)
- Market Cap/GDP Ratio

Schedule: Daily at 9:00 AM IST (after market open)
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

# Market Cap/GDP Configuration (Update quarterly)
# Source: NSE + MoSPI data
MC_GDP_CONFIG = {
    'Q1_2026': {
        'ratio': 129.0,  # Updated: NSE market cap ₹465.75 lakh crore ($5.14T) / GDP ₹357 lakh crore ($3.97T) = 130.5%, rounded conservatively to 129%
        'market_cap_usd_billions': 5140,  # NSE market cap as of Jan 19, 2026
        'gdp_usd_billions': 3970,  # Nominal GDP FY 2025-26 estimate (₹357 lakh crore)
        'last_updated': '2026-01-19',
        'notes': 'NSE market cap from Jan 19, 2026; GDP from FY25-26 advance estimates'
    }
}

# Yahoo Finance Tickers
TICKERS = {
    'gold': 'GC=F',           # Gold Futures
    'silver': 'SI=F',          # Silver Futures
    'india_vix': '^INDIAVIX',  # India VIX
    'nifty_50': '^NSEI',       # Nifty 50
    # 'nifty_smallcap': Not available on Yahoo Finance
}

# =============================================================================
# FUNCTIONS
# =============================================================================

def init_supabase() -> Client:
    """Initialize Supabase client"""
    return create_client(SUPABASE_URL, SUPABASE_KEY)

def get_latest_price(ticker: str, days_back: int = 5) -> float:
    """Get latest available price for a ticker"""
    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days_back)
        
        data = yf.download(ticker, start=start_date, end=end_date, progress=False)
        
        if data.empty:
            return None
        
        # Get the most recent close price
        latest_price = data['Close'].iloc[-1]
        return float(latest_price)
        
    except Exception as e:
        print(f"  ❌ Error fetching {ticker}: {e}")
        return None

def get_current_mc_gdp_ratio():
    """Get current Market Cap/GDP ratio from config"""
    # Get most recent quarter
    quarters = sorted(MC_GDP_CONFIG.keys(), reverse=True)
    latest_quarter = quarters[0]
    
    config = MC_GDP_CONFIG[latest_quarter]
    
    return {
        'ratio': config['ratio'],
        'market_cap': config['market_cap_usd_billions'],
        'gdp': config['gdp_usd_billions'],
        'quarter': latest_quarter
    }

def fetch_market_ratios():
    """Fetch all market ratios for today"""
    print("\n" + "="*80)
    print("📊 MARKET RATIOS SCANNER")
    print("="*80)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    ratios = {}
    
    # Fetch Gold
    print("\n🥇 Fetching Gold price...")
    gold_price = get_latest_price(TICKERS['gold'])
    if gold_price:
        ratios['gold_price'] = gold_price
        print(f"  ✅ Gold: ${gold_price:.2f}")
    else:
        print("  ❌ Failed to fetch Gold price")
    
    # Fetch Silver
    print("\n🥈 Fetching Silver price...")
    silver_price = get_latest_price(TICKERS['silver'])
    if silver_price:
        ratios['silver_price'] = silver_price
        print(f"  ✅ Silver: ${silver_price:.2f}")
    else:
        print("  ❌ Failed to fetch Silver price")
    
    # Calculate Gold/Silver Ratio
    if gold_price and silver_price:
        gold_silver_ratio = gold_price / silver_price
        ratios['gold_silver_ratio'] = gold_silver_ratio
        print(f"  ✅ Gold/Silver Ratio: {gold_silver_ratio:.2f}")
    
    # Fetch India VIX
    print("\n📊 Fetching India VIX...")
    india_vix = get_latest_price(TICKERS['india_vix'])
    if india_vix:
        ratios['india_vix'] = india_vix
        print(f"  ✅ India VIX: {india_vix:.2f}")
    else:
        print("  ❌ Failed to fetch India VIX")
    
    # Fetch Nifty 50
    print("\n📈 Fetching Nifty 50...")
    nifty_50 = get_latest_price(TICKERS['nifty_50'])
    if nifty_50:
        ratios['nifty_50'] = nifty_50
        print(f"  ✅ Nifty 50: {nifty_50:.2f}")
    else:
        print("  ❌ Failed to fetch Nifty 50")
    
    # Nifty SmallCap 100 - Not available on Yahoo Finance
    # Skipping for now - can add manually from NSE if needed
    
    # Get Market Cap/GDP Ratio
    print("\n🇮🇳 Getting Market Cap/GDP Ratio...")
    mc_gdp_data = get_current_mc_gdp_ratio()
    ratios['market_cap_gdp_ratio'] = mc_gdp_data['ratio']
    ratios['india_gdp_usd_billions'] = mc_gdp_data['gdp']
    ratios['nse_total_mcap_usd_billions'] = mc_gdp_data['market_cap']
    print(f"  ✅ Market Cap/GDP: {mc_gdp_data['ratio']:.1f}% (Q: {mc_gdp_data['quarter']})")
    
    return ratios

def save_to_supabase(supabase: Client, ratios: dict):
    """Save ratios to Supabase"""
    try:
        today = datetime.now().date().isoformat()
        
        record = {
            'snapshot_date': today,
            **ratios
        }
        
        # Upsert (insert or update if date already exists)
        response = supabase.table('market_ratios').upsert(
            record,
            on_conflict='snapshot_date'
        ).execute()
        
        print(f"\n💾 Saved ratios to database for {today}")
        return True
        
    except Exception as e:
        print(f"\n❌ Failed to save to database: {e}")
        return False

def main():
    """Main execution"""
    # Initialize Supabase
    supabase = init_supabase()
    
    # Fetch ratios
    ratios = fetch_market_ratios()
    
    # Save to database
    if ratios:
        save_to_supabase(supabase, ratios)
    else:
        print("\n⚠️ No ratios to save")
    
    # Summary
    print("\n" + "="*80)
    print("📈 SUMMARY")
    print("="*80)
    print(f"✅ Ratios fetched: {len(ratios)}")
    print(f"Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*80)

if __name__ == "__main__":
    main()
