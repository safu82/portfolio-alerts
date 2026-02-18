"""
Zerodha Live Price Updater - REST API Version
==============================================
Uses Zerodha's REST API to poll prices every 5 seconds during market hours.
More reliable than WebSocket, works with any network/firewall setup.

Run during market hours: python zerodha_rest_updater.py
"""

from kiteconnect import KiteConnect
from supabase import create_client
from datetime import datetime
import os
import time
import pytz
from dotenv import load_dotenv

load_dotenv()

# Configuration
ZERODHA_API_KEY = os.getenv('ZERODHA_API_KEY')
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_SERVICE_KEY')

# Initialize
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
ist = pytz.timezone('Asia/Kolkata')

# Ticker mappings
TICKER_MAPPING = {
    'KPEL': 'KPENERGY.NS',
    'TARIL': 'TRIL.NS',
    'GENUS': 'GENUSPOWER.NS',
    'DENTA': 'DENTAWATER.NS'
}

def get_portfolio_tickers():
    """Get all tickers from portfolio and transactions"""
    tickers = set()
    
    try:
        # From holdings
        holdings = supabase.table('holdings').select('ticker').execute()
        for h in holdings.data:
            if h['ticker'] and (h['ticker'].endswith('.NS') or h['ticker'].endswith('.BO')):
                tickers.add(h['ticker'])
        
        # From transactions
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
    ticker_to_token = {}
    found = []
    missing = []
    
    for yahoo_ticker in tickers:
        # Determine exchange and symbol
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
                # Special case: GENUS and DENTA are on BSE
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
                ticker_to_token[yahoo_ticker] = token
                found.append(f"{yahoo_ticker} ({exchange}:{symbol})")
                found_inst = True
                break
        
        if not found_inst:
            missing.append(f"{yahoo_ticker} ({exchange}:{symbol})")
    
    print(f"✅ Mapped {len(found)} stocks")
    if missing:
        print(f"⚠️  Could not find {len(missing)} stocks:")
        for m in missing[:5]:  # Show first 5
            print(f"   - {m}")
        if len(missing) > 5:
            print(f"   ... and {len(missing) - 5} more")
    
    return token_to_ticker, ticker_to_token

def is_market_open():
    """Check if market is currently open (9:15 AM - 3:30 PM IST, Mon-Fri)"""
    now = datetime.now(ist)
    
    # Check if weekday (0 = Monday, 6 = Sunday)
    if now.weekday() >= 5:  # Saturday or Sunday
        return False
    
    # Check market hours
    market_start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_end = now.replace(hour=15, minute=30, second=0, microsecond=0)
    
    return market_start <= now <= market_end

def fetch_and_update_prices(kite, token_to_ticker):
    """Fetch quotes from Zerodha and update Supabase"""
    if not token_to_ticker:
        print("⚠️  No instruments to fetch")
        return 0
    
    try:
        # Build instrument keys for Zerodha API
        # Format: EXCHANGE:SYMBOL or just the token
        tokens = list(token_to_ticker.keys())
        
        # Zerodha quote() accepts instrument tokens
        quotes = kite.quote(tokens)
        
        if not quotes:
            print("⚠️  No quotes returned")
            return 0
        
        # Process quotes
        updates = []
        now = datetime.now(ist)
        
        for token_str, quote_data in quotes.items():
            # Extract token from response key (might be "NSE:RELIANCE" or just token)
            if ':' in str(token_str):
                # It's in format "EXCHANGE:SYMBOL", need to find token
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
    print("ZERODHA LIVE PRICE UPDATER - REST API VERSION")
    print("=" * 60)
    print()
    
    # Read access token
    try:
        with open('zerodha_access_token.txt', 'r') as f:
            access_token = f.read().strip()
        print(f"✅ Loaded access token: {access_token[:20]}...")
    except FileNotFoundError:
        print("❌ zerodha_access_token.txt not found!")
        print("Run: python zerodha_auto_token_with_supabase.py")
        return
    
    # Initialize Kite
    kite = KiteConnect(api_key=ZERODHA_API_KEY)
    kite.set_access_token(access_token)
    print("✅ Connected to Zerodha API\n")
    
    # Get portfolio tickers
    print("📊 Getting your portfolio stocks...")
    tickers = get_portfolio_tickers()
    print(f"✅ Found {len(tickers)} stocks in portfolio\n")
    
    if not tickers:
        print("⚠️  No stocks in portfolio!")
        return
    
    # Map to instrument tokens
    token_to_ticker, ticker_to_token = get_instrument_mappings(kite, tickers)
    
    if not token_to_ticker:
        print("❌ No instruments mapped!")
        return
    
    print()
    print("🔴 STARTING LIVE PRICE UPDATES")
    print("=" * 60)
    print("Polling interval: 5 seconds")
    print("Market hours: 9:15 AM - 3:30 PM IST (Mon-Fri)")
    print("Press Ctrl+C to stop")
    print()
    
    update_count = 0
    last_ticker_check = time.time()
    
    try:
        while True:
            # Check if market is open
            if not is_market_open():
                now = datetime.now(ist)
                print(f"⏸️  Market closed - {now.strftime('%I:%M %p')} (waiting...)")
                time.sleep(60)  # Check every minute
                continue
            
            # Fetch and update prices
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
                    token_to_ticker, ticker_to_token = get_instrument_mappings(kite, tickers)
                    print()
                else:
                    print("✅ No new stocks\n")
                last_ticker_check = time.time()
            
            # Wait 5 seconds before next update
            time.sleep(5)
            
    except KeyboardInterrupt:
        print("\n\n👋 Stopping gracefully...")
        print(f"✅ Total updates: {update_count}")
        print("✅ Prices cached in Supabase")

if __name__ == '__main__':
    main()
