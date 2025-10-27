"""
Integrated Portfolio Alert Scanner
Combines EMA crossovers and promoter buying detection for portfolio stocks only
"""
import os
import requests
from datetime import datetime, timedelta
from supabase import create_client, Client
import yfinance as yf
import pandas as pd
import numpy as np
from bs4 import BeautifulSoup
import warnings
warnings.filterwarnings('ignore')

# ================== CONFIGURATION ==================
SUPABASE_URL = 'https://hcgyncghmcvylnrmcivj.supabase.co'
SUPABASE_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhjZ3luY2dobWN2eWxucm1jaXZqIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTc1MTQwMTEsImV4cCI6MjA3MzA5MDAxMX0.n8vFVCJe1y_3o8fpAY0IgasZ4eKl7DAogEM3OlHB8Ww'
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

EMA_CONFIG = {
    'short_ema': 20,
    'long_ema': 50,
    'filter_ema': 200,
    'lookback_days': 15,  # Check last 1 days for crossover
    'volume_multiplier': 1.5,
    'rsi_period': 14,
    'enable_volume_filter': False,  # Disabled for portfolio scanning
    'enable_rsi_filter': False,
    'enable_200ema_filter': False,
}

# New indicator configs
VOLUME_CONFIG = {
    'multiplier': 2.0,  # Volume must be 2x average
    'avg_period': 20,   # 20-day average volume
}

RSI_CONFIG = {
    'oversold': 30,     # RSI < 30 = Oversold (Bullish opportunity)
    'overbought': 70,   # RSI > 70 = Overbought (Bearish warning)
    'period': 14,
}

CONSOLIDATION_CONFIG = {
    'period': 200,      # 200 days
    'range_pct': 5,     # Within 5% range
}

# ================== PORTFOLIO FUNCTIONS ==================

def get_portfolio_stocks():
    """Fetch current portfolio stocks from Supabase"""
    try:
        # Get Indian portfolio
        indian_response = supabase.table('current_portfolio').select('ticker, name').eq('portfolio', 'INDIAN').execute()
        indian_stocks = []
        
        for stock in indian_response.data:
            ticker_raw = stock['ticker']
            # Remove .NS or .BO suffix if present
            symbol = ticker_raw.replace('.NS', '').replace('.BO', '')
            
            indian_stocks.append({
                'ticker': symbol,  # Plain symbol for yfinance
                'symbol': symbol,  # For display
                'name': stock['name'], 
                'portfolio': 'INDIAN'
            })
        
        # Get US portfolio - skip for EMA scanner (different market dynamics)
        # us_response = supabase.table('current_portfolio').select('ticker, name').eq('portfolio', 'US').execute()
        # us_stocks = [{'ticker': stock['ticker'], 'symbol': stock['ticker'], 'name': stock['name'], 'portfolio': 'US'} 
        #             for stock in us_response.data]
        
        return indian_stocks  # + us_stocks for US stocks
    except Exception as e:
        print(f"Error fetching portfolio: {e}")
        return []

# ================== EMA CROSSOVER FUNCTIONS ==================

def calculate_ema(prices, period):
    """Calculate Exponential Moving Average"""
    if len(prices) < period:
        return None
    return prices.ewm(span=period, adjust=False).mean()

def calculate_rsi(prices, period=14):
    """Calculate Relative Strength Index"""
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def detect_crossover(short_ema, long_ema, lookback_days):
    """Detect EMA crossover in the lookback period"""
    for i in range(min(lookback_days, len(short_ema) - 1)):
        idx = -(i + 1)
        prev_idx = -(i + 2)
        
        current_short = short_ema.iloc[idx]
        current_long = long_ema.iloc[idx]
        prev_short = short_ema.iloc[prev_idx]
        prev_long = long_ema.iloc[prev_idx]
        
        # Check for valid values
        if pd.isna([current_short, current_long, prev_short, prev_long]).any():
            continue
        
        # Golden cross (bullish)
        if prev_short <= prev_long and current_short > current_long:
            crossover_date = short_ema.index[idx].date()
            return {'type': 'bullish', 'days_ago': i, 'date': crossover_date}
        
        # Death cross (bearish)
        if prev_short >= prev_long and current_short < current_long:
            crossover_date = short_ema.index[idx].date()
            return {'type': 'bearish', 'days_ago': i, 'date': crossover_date}
    
    return None

def scan_ema_crossover(stock):
    """Scan a single stock for EMA crossover"""
    ticker = stock['ticker']
    symbol = stock['symbol']
    name = stock['name']
    portfolio = stock['portfolio']
    
    try:
        # Get 1 year of price data - add .NS suffix for Indian stocks
        yf_ticker = yf.Ticker(f"{ticker}.NS")
        hist = yf_ticker.history(period='1y')
        
        if hist.empty or len(hist) < EMA_CONFIG['filter_ema']:
            return None
        
        # Calculate EMAs
        hist['short_ema'] = calculate_ema(hist['Close'], EMA_CONFIG['short_ema'])
        hist['long_ema'] = calculate_ema(hist['Close'], EMA_CONFIG['long_ema'])
        hist['filter_ema'] = calculate_ema(hist['Close'], EMA_CONFIG['filter_ema'])
        hist['rsi'] = calculate_rsi(hist['Close'], EMA_CONFIG['rsi_period'])
        hist['volume_avg'] = hist['Volume'].rolling(window=20).mean()
        
        # Get current values
        cmp = hist['Close'].iloc[-1]
        current_short_ema = hist['short_ema'].iloc[-1]
        current_long_ema = hist['long_ema'].iloc[-1]
        current_filter_ema = hist['filter_ema'].iloc[-1]
        current_rsi = hist['rsi'].iloc[-1]
        current_volume = hist['Volume'].iloc[-1]
        avg_volume = hist['volume_avg'].iloc[-1]
        
        # Check for NaN values
        if pd.isna([current_short_ema, current_long_ema]).any():
            return None
        
        # Detect crossover
        crossover = detect_crossover(hist['short_ema'], hist['long_ema'], EMA_CONFIG['lookback_days'])
        
        if not crossover:
            return None
        
        # Calculate volume ratio
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0
        
        # Apply RSI filter
        if EMA_CONFIG['enable_rsi_filter'] and not pd.isna(current_rsi):
            if crossover['type'] == 'bullish' and current_rsi < 50:
                return None
            if crossover['type'] == 'bearish' and current_rsi > 50:
                return None
        
# Check EMA stacking
        if crossover['type'] == 'bullish':
            stacked_emas = bool(cmp > current_short_ema > current_long_ema)
        else:  # bearish
            stacked_emas = bool(cmp < current_short_ema < current_long_ema)
        
        # Prepare alert data
        alert_type = 'ema_golden_cross' if crossover['type'] == 'bullish' else 'ema_death_cross'
        alert_category = 'BULLISH' if crossover['type'] == 'bullish' else 'BEARISH'
        alert_title = f"{name} - {'Golden Cross' if crossover['type'] == 'bullish' else 'Death Cross'} (20/50 EMA)"
        alert_description = f"20 EMA crossed {'above' if crossover['type'] == 'bullish' else 'below'} 50 EMA"
        
        details = {
            '20_ema': round(current_short_ema, 2),
            '50_ema': round(current_long_ema, 2),
            '200_ema': round(current_filter_ema, 2) if not pd.isna(current_filter_ema) else None,
            'rsi': round(current_rsi, 2) if not pd.isna(current_rsi) else None,
            'volume_ratio': round(volume_ratio, 2),
            'emas_stacked': bool(stacked_emas),
            'days_ago': crossover['days_ago']
        }
        
        return {
            'portfolio': portfolio,
            'ticker': symbol,
            'stock_name': name,
            'alert_type': alert_type,
            'alert_category': alert_category,
            'alert_title': alert_title,
            'alert_description': alert_description,
            'price': round(cmp, 2),
            'alert_date': crossover['date'].isoformat(),
            'details': details
        }
        
    except Exception as e:
        print(f"  ❌ Error scanning {symbol}: {e}")
        return None

# ================== PROMOTER BUYING FUNCTIONS ==================

def get_recent_promoter_transactions():
    """Scrape recent promoter transactions from Trendlyne"""
    url = "https://trendlyne.com/equity/group-insider-trading-sast/"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 200:
            return parse_promoter_transactions(response.text)
        else:
            print(f"  ⚠️ Failed to fetch Trendlyne data: {response.status_code}")
            return []
    except Exception as e:
        print(f"  ❌ Error fetching promoter data: {e}")
        return []

def parse_promoter_transactions(html):
    """Parse promoter transactions from HTML"""
    soup = BeautifulSoup(html, 'html.parser')
    transactions = []
    rows = soup.find_all('tr')
    
    for row in rows:
        cols = row.find_all('td')
        if len(cols) >= 7:
            try:
                company_link = cols[0].find('a')
                if not company_link:
                    continue
                
                company_name = company_link.text.strip()
                company_url = company_link.get('href', '')
                
                import re
                symbol_match = re.search(r'/([A-Z0-9]+)/', company_url)
                symbol = symbol_match.group(1) if symbol_match else ''
                
                person_link = cols[1].find('a')
                person = person_link.text.strip() if person_link else cols[1].text.strip()
                category = cols[2].text.strip()
                transaction_type = cols[3].text.strip()
                date = cols[4].text.strip()
                
                shares_text = cols[5].text.strip().replace(',', '')
                try:
                    shares = int(shares_text) if shares_text and shares_text != '-' else 0
                except:
                    shares = 0
                
                holding_text = cols[6].text.strip()
                pct_change = cols[7].text.strip() if len(cols) > 7 else ''
                
                # Only include promoter transactions
                if 'promoter' not in category.lower():
                    continue
                
                transaction = {
                    'company_name': company_name,
                    'symbol': symbol,
                    'person_entity': person,
                    'category': category,
                    'transaction_type': transaction_type,
                    'date': date,
                    'shares': shares,
                    'post_holding': holding_text,
                    'pct_change': pct_change,
                }
                transactions.append(transaction)
            except:
                continue
    
    return transactions

def scan_promoter_buying(portfolio_stocks):
    """Scan for promoter buying in portfolio stocks"""
    # Get all recent promoter transactions
    all_transactions = get_recent_promoter_transactions()
    
    if not all_transactions:
        print("  ⚠️ No promoter transactions found")
        return []
    
    # Get portfolio stock symbols (without .NS)
    portfolio_symbols = set([stock['symbol'].upper() for stock in portfolio_stocks])
    
    # Filter for portfolio stocks only
    portfolio_transactions = [
        t for t in all_transactions 
        if t['symbol'].upper() in portfolio_symbols
    ]
    
    print(f"  ✅ Found {len(portfolio_transactions)} promoter transactions in portfolio")
    
    alerts = []
    
    for txn in portfolio_transactions:
        # Determine if it's buying or selling
        is_buying = 'acquisition' in txn['transaction_type'].lower() or 'purchase' in txn['transaction_type'].lower()
        is_selling = 'disposal' in txn['transaction_type'].lower() or 'sale' in txn['transaction_type'].lower()
        
        if not (is_buying or is_selling):
            continue
        
        # Find stock name from portfolio
        stock_name = next((s['name'] for s in portfolio_stocks if s['symbol'].upper() == txn['symbol'].upper()), txn['company_name'])
        
        alert_type = 'promoter_buying' if is_buying else 'promoter_selling'
        alert_category = 'FUNDAMENTAL' if is_buying else 'INFO'
        alert_title = f"{stock_name} - Promoter {'Buying' if is_buying else 'Selling'}"
        alert_description = f"{txn['person_entity'][:50]} - {txn['transaction_type']}"
        
        details = {
            'person_entity': txn['person_entity'],
            'transaction_type': txn['transaction_type'],
            'shares': txn['shares'],
            'post_holding': txn['post_holding'],
            'pct_change': txn['pct_change'],
            'category': txn['category']
        }
        
        # Parse date (assuming format like "27 Oct 2025")
        try:
            alert_date = datetime.strptime(txn['date'], "%d %b %Y").date().isoformat()
        except:
            alert_date = datetime.now().date().isoformat()
        
        alerts.append({
            'portfolio': 'INDIAN',
            'ticker': txn['symbol'],
            'stock_name': stock_name,
            'alert_type': alert_type,
            'alert_category': alert_category,
            'alert_title': alert_title,
            'alert_description': alert_description,
            'price': None,
            'alert_date': alert_date,
            'details': details
        })
    
    return alerts

# ================== NEW TECHNICAL INDICATORS ==================

def scan_volume_breakout(stock):
    """Detect volume breakout (Volume > 2x average)"""
    ticker = stock['ticker']
    symbol = stock['symbol']
    name = stock['name']
    portfolio = stock['portfolio']
    
    try:
        # Get price data
        yf_ticker = yf.Ticker(f"{ticker}.NS")
        hist = yf_ticker.history(period='3mo')  # 3 months for volume analysis
        
        if hist.empty or len(hist) < VOLUME_CONFIG['avg_period']:
            return None
        
        # Calculate volume average
        hist['volume_avg'] = hist['Volume'].rolling(window=VOLUME_CONFIG['avg_period']).mean()
        
        # Get current values
        current_volume = hist['Volume'].iloc[-1]
        avg_volume = hist['volume_avg'].iloc[-1]
        current_price = hist['Close'].iloc[-1]
        prev_price = hist['Close'].iloc[-2]
        
        if pd.isna(avg_volume) or avg_volume == 0:
            return None
        
        volume_ratio = current_volume / avg_volume
        
        # Check if volume breakout occurred
        if volume_ratio < VOLUME_CONFIG['multiplier']:
            return None
        
        # Determine if bullish or bearish based on price movement
        price_change_pct = ((current_price - prev_price) / prev_price) * 100
        
        if abs(price_change_pct) < 0.5:  # Ignore if price didn't move much
            return None
        
        is_bullish = price_change_pct > 0
        
        alert_type = 'volume_breakout_bullish' if is_bullish else 'volume_breakout_bearish'
        alert_category = 'BULLISH' if is_bullish else 'BEARISH'
        alert_title = f"{name} - Volume Breakout ({'Bullish' if is_bullish else 'Bearish'})"
        alert_description = f"Volume {volume_ratio:.1f}x average with {abs(price_change_pct):.1f}% {'gain' if is_bullish else 'loss'}"
        
        details = {
            'current_volume': int(current_volume),
            'avg_volume': int(avg_volume),
            'volume_ratio': round(volume_ratio, 2),
            'price_change_pct': round(price_change_pct, 2),
            'direction': 'up' if is_bullish else 'down'
        }
        
        return {
            'portfolio': portfolio,
            'ticker': symbol,
            'stock_name': name,
            'alert_type': alert_type,
            'alert_category': alert_category,
            'alert_title': alert_title,
            'alert_description': alert_description,
            'price': round(current_price, 2),
            'alert_date': datetime.now().date().isoformat(),
            'details': details
        }
        
    except Exception as e:
        print(f"  ❌ Error scanning volume for {symbol}: {e}")
        return None

def scan_rsi_extremes(stock):
    """Detect RSI oversold (<30) or overbought (>70) conditions"""
    ticker = stock['ticker']
    symbol = stock['symbol']
    name = stock['name']
    portfolio = stock['portfolio']
    
    try:
        # Get price data
        yf_ticker = yf.Ticker(f"{ticker}.NS")
        hist = yf_ticker.history(period='3mo')
        
        if hist.empty or len(hist) < RSI_CONFIG['period'] + 5:
            return None
        
        # Calculate RSI
        hist['rsi'] = calculate_rsi(hist['Close'], RSI_CONFIG['period'])
        
        current_rsi = hist['rsi'].iloc[-1]
        current_price = hist['Close'].iloc[-1]
        
        if pd.isna(current_rsi):
            return None
        
        # Check for oversold (bullish opportunity)
        if current_rsi < RSI_CONFIG['oversold']:
            alert_type = 'rsi_oversold'
            alert_category = 'BULLISH'
            alert_title = f"{name} - RSI Oversold (Bullish Opportunity)"
            alert_description = f"RSI at {current_rsi:.1f} - Potentially oversold"
            
        # Check for overbought (bearish warning)
        elif current_rsi > RSI_CONFIG['overbought']:
            alert_type = 'rsi_overbought'
            alert_category = 'BEARISH'
            alert_title = f"{name} - RSI Overbought (Bearish Warning)"
            alert_description = f"RSI at {current_rsi:.1f} - Potentially overbought"
            
        else:
            return None  # RSI in normal range
        
        details = {
            'rsi': round(current_rsi, 2),
            'rsi_oversold_level': RSI_CONFIG['oversold'],
            'rsi_overbought_level': RSI_CONFIG['overbought'],
        }
        
        return {
            'portfolio': portfolio,
            'ticker': symbol,
            'stock_name': name,
            'alert_type': alert_type,
            'alert_category': alert_category,
            'alert_title': alert_title,
            'alert_description': alert_description,
            'price': round(current_price, 2),
            'alert_date': datetime.now().date().isoformat(),
            'details': details
        }
        
    except Exception as e:
        print(f"  ❌ Error scanning RSI for {symbol}: {e}")
        return None

def scan_consolidation(stock):
    """Detect 200-day price consolidation (within 5% range)"""
    ticker = stock['ticker']
    symbol = stock['symbol']
    name = stock['name']
    portfolio = stock['portfolio']
    
    try:
        # Get price data
        yf_ticker = yf.Ticker(f"{ticker}.NS")
        hist = yf_ticker.history(period='1y')
        
        if hist.empty or len(hist) < CONSOLIDATION_CONFIG['period']:
            return None
        
        # Get last 200 days
        consolidation_period = hist['Close'].iloc[-CONSOLIDATION_CONFIG['period']:]
        
        highest = consolidation_period.max()
        lowest = consolidation_period.min()
        current_price = hist['Close'].iloc[-1]
        
        # Calculate range percentage
        range_pct = ((highest - lowest) / lowest) * 100
        
        # Check if within consolidation range
        if range_pct > CONSOLIDATION_CONFIG['range_pct']:
            return None
        
        alert_type = 'consolidation_200day'
        alert_category = 'INFO'
        alert_title = f"{name} - 200-Day Consolidation"
        alert_description = f"Price consolidating in {range_pct:.1f}% range for 200 days"
        
        details = {
            'consolidation_days': CONSOLIDATION_CONFIG['period'],
            'range_pct': round(range_pct, 2),
            'highest': round(highest, 2),
            'lowest': round(lowest, 2),
            'current_price': round(current_price, 2)
        }
        
        return {
            'portfolio': portfolio,
            'ticker': symbol,
            'stock_name': name,
            'alert_type': alert_type,
            'alert_category': alert_category,
            'alert_title': alert_title,
            'alert_description': alert_description,
            'price': round(current_price, 2),
            'alert_date': datetime.now().date().isoformat(),
            'details': details
        }
        
    except Exception as e:
        print(f"  ❌ Error scanning consolidation for {symbol}: {e}")
        return None

# ================== ALERT INSERTION ==================

def insert_alert(alert_data):
    """Insert alert into Supabase (avoid duplicates)"""
    try:
        # Check if similar alert already exists in last 7 days
        existing = supabase.table('alerts').select('id').eq(
            'ticker', alert_data['ticker']
        ).eq(
            'alert_type', alert_data['alert_type']
        ).gte(
            'alert_date', (datetime.now() - timedelta(days=7)).date().isoformat()
        ).execute()
        
        if len(existing.data) > 0:
            print(f"  ⚠️ Alert already exists for {alert_data['ticker']} - {alert_data['alert_type']}")
            return False
        
        # Insert new alert
        response = supabase.table('alerts').insert(alert_data).execute()
        print(f"  ✅ Inserted: {alert_data['ticker']} - {alert_data['alert_type']}")
        return True
    except Exception as e:
        print(f"  ❌ Error inserting alert: {e}")
        return False

# ================== MAIN EXECUTION ==================

def main():
    print("=" * 70)
    print("📈 PORTFOLIO ALERT SCANNER")
    print(f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    
    # Step 1: Get portfolio stocks
    print("\n📊 Fetching portfolio stocks...")
    portfolio_stocks = get_portfolio_stocks()
    print(f"  ✅ Found {len(portfolio_stocks)} stocks in portfolio")
    
    if not portfolio_stocks:
        print("  ❌ No stocks found. Exiting.")
        return
    
    total_alerts = 0
    
    # Step 2: Scan for EMA crossovers
    print("\n🔍 Scanning for EMA Crossovers (20/50)...")
    print(f"  Configuration: 20/50 EMA, Lookback: {EMA_CONFIG['lookback_days']} days, RSI Filter: {EMA_CONFIG['enable_rsi_filter']}")
    
    for stock in portfolio_stocks:
        print(f"\n  📊 {stock['name']} ({stock['symbol']})...")
        alert = scan_ema_crossover(stock)
        if alert:
            if insert_alert(alert):
                total_alerts += 1
    
    # Step 3: Scan for promoter buying
    print("\n\n💼 Scanning for Promoter Transactions...")
    promoter_alerts = scan_promoter_buying(portfolio_stocks)
    
    for alert in promoter_alerts:
        if insert_alert(alert):
            total_alerts += 1
    
    # Step 4: Scan for Volume Breakouts
    print("\n\n📈 Scanning for Volume Breakouts...")
    print(f"  Configuration: Volume > {VOLUME_CONFIG['multiplier']}x average ({VOLUME_CONFIG['avg_period']}-day)")
    
    for stock in portfolio_stocks:
        print(f"  📊 {stock['name']} ({stock['symbol']})...")
        alert = scan_volume_breakout(stock)
        if alert:
            if insert_alert(alert):
                total_alerts += 1
    
    # Step 5: Scan for RSI Extremes
    print("\n\n📉 Scanning for RSI Oversold/Overbought...")
    print(f"  Configuration: Oversold < {RSI_CONFIG['oversold']}, Overbought > {RSI_CONFIG['overbought']}")
    
    for stock in portfolio_stocks:
        print(f"  📊 {stock['name']} ({stock['symbol']})...")
        alert = scan_rsi_extremes(stock)
        if alert:
            if insert_alert(alert):
                total_alerts += 1
    
    # Step 6: Scan for 200-Day Consolidation
    print("\n\n📊 Scanning for 200-Day Consolidation...")
    print(f"  Configuration: {CONSOLIDATION_CONFIG['period']} days within {CONSOLIDATION_CONFIG['range_pct']}% range")
    
    for stock in portfolio_stocks:
        print(f"  📊 {stock['name']} ({stock['symbol']})...")
        alert = scan_consolidation(stock)
        if alert:
            if insert_alert(alert):
                total_alerts += 1
    
    # Summary
    print("\n" + "=" * 70)
    print(f"✅ SCAN COMPLETE - {total_alerts} new alerts inserted")
    print("=" * 70)

if __name__ == '__main__':
    main()
