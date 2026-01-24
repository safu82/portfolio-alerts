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
import requests
from bs4 import BeautifulSoup
import re
import time

# =============================================================================
# CONFIGURATION
# =============================================================================

SUPABASE_URL = os.getenv('SUPABASE_URL', 'https://hcgyncghmcvylnrmcivj.supabase.co')
SUPABASE_KEY = os.getenv('SUPABASE_KEY', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhjZ3luY2dobWN2eWxucm1jaXZqIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTc1MTQwMTEsImV4cCI6MjA3MzA5MDAxMX0.n8vFVCJe1y_3o8fpAY0IgasZ4eKl7DAogEM3OlHB8Ww')

# Market Cap/GDP Configuration
# UPDATE WEEKLY: Visit https://www.nseindia.com/ and update CURRENT_MARKET_CAP
# GDP is updated QUARTERLY from MoSPI data

# CURRENT MARKET CAP (Update weekly - every Monday)
# Visit NSE homepage, find "Market Capitalization: Lac Crs XXX | Tn $ Y.YY"
# Last updated: 2026-01-21
CURRENT_MARKET_CAP = {
    'inr_lakh_crore': 479.03,  # From NSE homepage: "Lac Crs 479.03"
    'usd_billions': 5320,       # From NSE homepage: "Tn $ 5.32" * 1000
    'last_updated': '2026-01-21',
    'source': 'nse_homepage'
}

# QUARTERLY GDP (Update quarterly from MoSPI)
MC_GDP_CONFIG = {
    'Q1_2026': {
        'gdp_usd_billions': 3970,  # Nominal GDP FY 2025-26 estimate (₹357 lakh crore)
        'market_cap_usd_billions': 5320,  # Fallback if scraping fails
        'ratio': 134.0,  # 5320 / 3970 * 100
        'last_updated': '2026-01-07',
        'notes': 'GDP from FY25-26 First Advance Estimates (MoSPI)'
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

def get_nse_market_cap():
    """
    Fetch NSE total market capitalization via API
    
    Returns:
        tuple: (market_cap_inr_lakh_crore, market_cap_usd_billions) or (None, None)
    """
    HOME = "https://www.nseindia.com"
    API = "https://www.nseindia.com/api/marketStatus"
    
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-IN,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
        "Connection": "keep-alive",
    }
    
    try:
        s = requests.Session()
        s.headers.update(HEADERS)
        
        # 1. Prime cookies by visiting homepage first
        h = s.get(HOME, timeout=30)
        if h.status_code != 200:
            print(f"  ⚠️ NSE home page failed: {h.status_code}")
            return None, None
        
        # Random delay between 1-3 seconds to avoid rate limiting
        import random
        time.sleep(random.uniform(1.5, 3.0))
        
        # 2. Fetch market cap from API
        r = s.get(API, timeout=30)
        if r.status_code != 200:
            print(f"  ⚠️ NSE API failed: {r.status_code}")
            return None, None
        
        data = r.json()
        
        # Extract market cap data (same as standalone script)
        mcap = data.get("marketcap") or (data.get("marketStatus") or {}).get("marketcap")
        if not mcap:
            print("  ⚠️ Market cap not found in API response")
            return None, None
        
        market_cap_inr = float(mcap["marketCapinLACCRRupeesFormatted"])
        market_cap_usd_tn = float(mcap["marketCapinTRDollars"])
        market_cap_usd = market_cap_usd_tn * 1000  # Convert trillion to billion
        
        return market_cap_inr, market_cap_usd
        
    except Exception as e:
        print(f"  ⚠️ Error fetching NSE market cap: {e}")
        return None, None

def get_latest_price(ticker: str, days_back: int = 5) -> float:
    """Get latest available price for a ticker"""
    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days_back)
        
        data = yf.download(ticker, start=start_date, end=end_date, progress=False)
        
        if data.empty:
            return None
        
        # Get the most recent close price
        if isinstance(data['Close'], pd.Series):
            latest_price = data['Close'].iloc[-1]
        else:
            # MultiIndex case
            latest_price = data['Close'].iloc[-1].item() if hasattr(data['Close'].iloc[-1], 'item') else data['Close'].iloc[-1]
        return float(latest_price)
        
    except Exception as e:
        print(f"  ❌ Error fetching {ticker}: {e}")
        return None

def get_current_mc_gdp_ratio():
    """
    Get current Market Cap/GDP ratio
    First tries to scrape NSE for live market cap, falls back to manual config
    """
    # GDP is updated quarterly - use manual config
    quarters = sorted(MC_GDP_CONFIG.keys(), reverse=True)
    latest_quarter = quarters[0]
    config = MC_GDP_CONFIG[latest_quarter]
    gdp_usd_billions = config['gdp_usd_billions']
    
    # Try to get LIVE market cap from NSE
    print("  🔍 Attempting to scrape NSE for live market cap...")
    market_cap_inr, market_cap_usd = get_nse_market_cap()
    
    if market_cap_usd:
        # Successfully scraped!
        ratio = (market_cap_usd / gdp_usd_billions) * 100
        print(f"  ✅ Scraped NSE: ₹{market_cap_inr} Lakh Crore = ${market_cap_usd:.0f}B")
        print(f"  📊 Calculated ratio: {ratio:.1f}%")
        
        return {
            'ratio': round(ratio, 1),
            'market_cap': market_cap_usd,
            'market_cap_inr_lakh_crore': market_cap_inr,
            'gdp': gdp_usd_billions,
            'quarter': latest_quarter,
            'source': 'nse_scraped'
        }
    else:
        # Fallback to manual config
        print(f"  ⚠️ NSE scraping failed, using manual config")
        return {
            'ratio': config['ratio'],
            'market_cap': config['market_cap_usd_billions'],
            'market_cap_inr_lakh_crore': config.get('market_cap_inr_lakh_crore', 465.75),
            'gdp': gdp_usd_billions,
            'quarter': latest_quarter,
            'source': 'manual_config'
        }

def fetch_market_ratios():
    """Fetch all market ratios for today"""
    print("\n" + "="*80)
    print("📊 MARKET RATIOS SCANNER")
    print("="*80)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    ratios = {}
    
    # FETCH NSE MARKET CAP FIRST (before any other requests)
    print("\n🇮🇳 Getting Market Cap/GDP Ratio...")
    mc_gdp_data = get_current_mc_gdp_ratio()
    ratios['market_cap_gdp_ratio'] = mc_gdp_data['ratio']
    ratios['india_gdp_usd_billions'] = mc_gdp_data['gdp']
    ratios['nse_total_mcap_usd_billions'] = mc_gdp_data['market_cap']
    print(f"  ✅ Market Cap/GDP: {mc_gdp_data['ratio']:.1f}% ({mc_gdp_data['source']})")
    print(f"  📈 Market Cap: ${mc_gdp_data['market_cap']:.0f}B")
    print(f"  📊 GDP: ${mc_gdp_data['gdp']:.0f}B (Q: {mc_gdp_data['quarter']})")
    
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
