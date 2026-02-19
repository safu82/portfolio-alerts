"""
Zerodha Live Price Updater - Railway Cloud Version
==================================================
Runs 24/7 on Railway.app, automatically fetching prices during market hours.
All configuration from environment variables (no .env file needed).
"""

from kiteconnect import KiteConnect
from supabase import create_client
from datetime import datetime
import os
import time
import pytz

# Configuration from environment variables
ZERODHA_API_KEY = os.environ.get('ZERODHA_API_KEY')
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')

# Initialize
supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
ist = pytz.timezone('Asia/Kolkata')

# Ticker mappings
TICKER_MAPPING = {
    'KPEL': 'KPENERGY.NS',
    'TARIL': 'TRIL.NS',
    'GENUSPOWER': 'GENUSPOWER.NS',
    'DENTA': 'DENTAWATER.NS'
}

def get_access_token_from_supabase():
    """Fetch the latest access token from Supabase (updated daily by GitHub Actions)"""
    try:
        result = supabase.table('zerodha_config').select('value').eq('id', 'zerodha_access_token').single().execute()
        if result.data:
            return result.data['value']
    except Exception as e:
        print(f"❌ Error fetching token from Supabase: {e}")
    return None

def get_portfolio_tickers():
    """Get all tickers from portfolio and transactions"""
    tickers = set()
    
    try:
        holdings = supabase.table('holdings').select('ticker').execute()
        for h in holdings.data:
            if h['ticker'] and (h['ticker'].endswith('.NS') or h['ticker'].endswith('.BO')):
                tickers.add(h['ticker'])
        
        transactions = supabase.table('transactions').select('ticker').execute()
        for t in transactions.data:
            if t['ticker'] and (t['ticker'].endswith('.NS') or t['ticker'].endswith('.BO')):
                tickers.add(t['ticker'])
    except Exception as e:
        print(f"⚠️  Error getting portfolio tickers: {e}")
    
    return tickers

def get_instrument_mappings(kite, tickers):
    """Map Yahoo tickers to Zerodha instrument tokens"""
    print("📥 Downloading instruments from Zerodha...")
    
    nse_instruments = kite.instruments("NSE")
    bse_instruments = kite.instruments("BSE")
    all_instruments = nse_instruments + bse_instruments
    
    token_to_ticker = {}
    found = []
    missing = []
    
    for yahoo_ticker in tickers:
        if yahoo_ticker.endswith('.NS'):
            exchange = 'NSE'
            symbol = yahoo_ticker.replace('.NS', '')
        elif yahoo_ticker.endswith('.BO'):
            exchange = 'BSE'
            symbol = yahoo_ticker.replace('.BO', '')
        else:
            continue
        
        # Check special mappings
        for zerodha_sym, yahoo_sym in TICKER_MAPPING.items():
            if yahoo_ticker == yahoo_sym:
                symbol = zerodha_sym
                if yahoo_sym in ['GENUSPOWER.NS', 'DENTAWATER.NS']:
                    exchange = 'BSE'
                break
        
        # Find instrument
        found_inst = False
        for inst in all_instruments:
            if (inst['tradingsymbol'] == symbol and 
                inst['exchange'] == exchange and 
                inst['instrument_type'] == 'EQ'):
                
                token = inst['instrument_token']
                token_to_ticker[token] = yahoo_ticker
                found.append(f"{yahoo_ticker}")
                found_inst = True
                break
        
        if not found_inst:
            missing.append(f"{yahoo_ticker}")
    
    print(f"✅ Mapped {len(found)} stocks")
    if missing:
        print(f"⚠️  Could not find: {', '.join(missing[:3])}")
    
    return token_to_ticker

def is_market_open():
    """Check if market is currently open (9:15 AM - 3:30 PM IST, Mon-Fri)"""
    now = datetime.now(ist)
    
    # Check if weekday
    if now.weekday() >= 5:
        return False
    
    # Check market hours
    market_start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_end = now.replace(hour=15, minute=30, second=0, microsecond=0)
    
    return market_start <= now <= market_end

def fetch_and_update_prices(kite, token_to_ticker):
    """Fetch quotes from Zerodha and update Supabase"""
    if not token_to_ticker:
        return 0
    
    try:
        tokens = list(token_to_ticker.keys())
        quotes = kite.quote(tokens)
        
        if not quotes:
            return 0
        
        updates = []
        now = datetime.now(ist)
        
        for token_str, quote_data in quotes.items():
            if ':' in str(token_str):
                for token, ticker in token_to_ticker.items():
                    if ticker in str(token_str):
                        break
            else:
                token = int(token_str)
            
            if token not in token_to_ticker:
                continue
            
            yahoo_ticker = token_to_ticker[token]
            
            last_price = quote_data.get('last_price', 0)
            ohlc = quote_data.get('ohlc', {})
            prev_close = ohlc.get('close', 0)
            
            if last_price and prev_close:
                day_change = last_price - prev_close
                day_change_pct = (day_change / prev_close * 100) if prev_close > 0 else 0
                
                updates.append({
                    'ticker': yahoo_ticker,
                    'price': last_price,
                    'day_change': day_change,
                    'day_change_pct': day_change_pct,
                    'prev_close': prev_close,
                    'volume': quote_data.get('volume', 0),
                    'updated_at': now.isoformat()
                })
        
        if updates:
            supabase.table('live_prices').upsert(updates, on_conflict='ticker').execute()
            return len(updates)
        
        return 0
        
    except Exception as e:
        print(f"❌ Error fetching prices: {e}")
        return 0

def main():
    print("=" * 60)
    print("ZERODHA LIVE PRICE UPDATER - RAILWAY CLOUD VERSION")
    print("=" * 60)
    print()
    
    update_count = 0
    last_ticker_check = time.time()
    last_market_session = None  # Track which day/session we're in
    
    # Initialize these as None - will be loaded when market opens
    access_token = None
    kite = None
    tickers = None
    token_to_ticker = None
    
    while True:
        try:
            now = datetime.now(ist)
            current_date = now.date()
            
            # Check if market is open
            if not is_market_open():
                print(f"⏸️  Market closed - {now.strftime('%I:%M %p')} (waiting...)")
                time.sleep(60)
                continue
            
            # NEW: Check if this is a new market session (new day or first time)
            if last_market_session != current_date:
                print(f"\n{'='*60}")
                print(f"🌅 NEW MARKET SESSION: {current_date}")
                print(f"{'='*60}")
                
                # Get fresh access token from Supabase
                print("🔑 Fetching latest access token from Supabase...")
                access_token = get_access_token_from_supabase()
                
                if not access_token:
                    print("❌ No access token found in Supabase!")
                    print("Waiting 5 minutes before retry...")
                    time.sleep(300)
                    continue
                
                print(f"✅ Loaded access token: {access_token[:20]}...")
                
                # Initialize/Re-initialize Kite with fresh token
                kite = KiteConnect(api_key=ZERODHA_API_KEY)
                kite.set_access_token(access_token)
                print("✅ Connected to Zerodha API with fresh token\n")
                
                # Get portfolio tickers
                print("📊 Getting your portfolio stocks...")
                tickers = get_portfolio_tickers()
                print(f"✅ Found {len(tickers)} stocks\n")
                
                if not tickers:
                    print("⚠️  No stocks in portfolio! Waiting...")
                    time.sleep(300)
                    continue
                
                # Map to instrument tokens
                token_to_ticker = get_instrument_mappings(kite, tickers)
                
                if not token_to_ticker:
                    print("❌ No instruments mapped! Retrying in 5 minutes...")
                    time.sleep(300)
                    continue
                
                print()
                print("🔴 STARTING LIVE PRICE UPDATES")
                print("=" * 60)
                print("Polling interval: 5 seconds")
                print("Market hours: 9:15 AM - 3:30 PM IST (Mon-Fri)")
                print("Running 24/7 on Railway.app ☁️")
                print()
                
                # Mark this session as initialized
                last_market_session = current_date
                update_count = 0
            
            # Fetch and update prices (only if initialized)
            if kite and token_to_ticker:
                updated = fetch_and_update_prices(kite, token_to_ticker)
                update_count += 1
                
                now = datetime.now(ist)
                if updated > 0:
                    print(f"📈 {now.strftime('%H:%M:%S')} - Updated {updated}/{len(token_to_ticker)} prices (#{update_count})")
                else:
                    print(f"⚠️  {now.strftime('%H:%M:%S')} - No updates (#{update_count})")
                
                # Check for new stocks every 5 minutes
                if time.time() - last_ticker_check > 300:
                    print("\n🔄 Checking for new stocks...")
                    new_tickers = get_portfolio_tickers()
                    if len(new_tickers) > len(tickers):
                        print(f"📢 Found {len(new_tickers) - len(tickers)} new stocks!")
                        tickers = new_tickers
                        token_to_ticker = get_instrument_mappings(kite, tickers)
                        print()
                    else:
                        print("✅ No new stocks\n")
                    last_ticker_check = time.time()
            
            time.sleep(5)
            
        except KeyboardInterrupt:
            print("\n\n👋 Shutting down...")
            break
        except Exception as e:
            print(f"\n❌ Error in main loop: {e}")
            print("Retrying in 30 seconds...")
            time.sleep(30)

if __name__ == '__main__':
    main()
