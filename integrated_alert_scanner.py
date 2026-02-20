"""
Integrated Portfolio Alert Scanner - ZERODHA VERSION
MIGRATED FROM YAHOO FINANCE TO ZERODHA/SUPABASE
Combines EMA crossovers and promoter buying detection for portfolio stocks only
"""
import os
import requests
from datetime import datetime, timedelta
from supabase import create_client, Client
import pandas as pd
import numpy as np
from bs4 import BeautifulSoup
import warnings
warnings.filterwarnings('ignore')

# ================== ZERODHA DATA FETCHER ==================
def fetch_ohlc_from_supabase(ticker: str, days: int = 730) -> pd.DataFrame:
    """
    Fetch OHLC data from Supabase daily_stock_snapshots table
    Replacement for yf.Ticker().history()
    """
    try:
        end_date = datetime.now().strftime('%Y-%m-%d')
        start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        
        # Ensure ticker has .NS suffix for Supabase query
        if not ticker.endswith('.NS') and not ticker.endswith('.BO'):
            ticker = f"{ticker}.NS"
        
        response = supabase.table('daily_stock_snapshots')\
            .select('*')\
            .eq('ticker', ticker)\
            .gte('snapshot_date', start_date)\
            .lte('snapshot_date', end_date)\
            .order('snapshot_date')\
            .execute()
        
        if not response.data or len(response.data) == 0:
            return pd.DataFrame()
        
        df = pd.DataFrame(response.data)
        df['snapshot_date'] = pd.to_datetime(df['snapshot_date'])
        df.set_index('snapshot_date', inplace=True)
        
        # Rename to match yfinance convention
        df.rename(columns={
            'open': 'Open',
            'high': 'High',
            'low': 'Low',
            'close': 'Close',
            'volume': 'Volume'
        }, inplace=True)
        
        return df
        
    except Exception as e:
        print(f"Error fetching {ticker}: {e}")
        return pd.DataFrame()



# ================== IMPORTANT UPDATE (Feb 2026) ==================
# Migrated from Yahoo Finance to Zerodha/Supabase
# EMAs are now pre-calculated by OHLC fetcher and stored in daily_stock_snapshots
# Scanner reads EMAs directly instead of recalculating
# ======================================================================
# ================== CONFIGURATION ==================
SUPABASE_URL = 'https://hcgyncghmcvylnrmcivj.supabase.co'
SUPABASE_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhjZ3luY2dobWN2eWxucm1jaXZqIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTc1MTQwMTEsImV4cCI6MjA3MzA5MDAxMX0.n8vFVCJe1y_3o8fpAY0IgasZ4eKl7DAogEM3OlHB8Ww'
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

EMA_CONFIG = {
    'short_ema': 20,
    'long_ema': 50,
    'filter_ema': 200,
    'lookback_days': 1,  # Check last 1 days for crossover
    'volume_multiplier': 1.5,
    'rsi_period': 14,
    'enable_volume_filter': False,  # Disabled for portfolio scanning
    'enable_rsi_filter': False,
    'enable_200ema_filter': False,
}

# Volume breakout config
VOLUME_CONFIG = {
    'multiplier': 2.0,  # Volume must be 2x average
    'avg_period': 20,   # 20-day average volume
}

# RSI config
RSI_CONFIG = {
    'oversold': 30,     # RSI < 30 = Oversold (Bullish opportunity)
    'overbought': 70,   # RSI > 70 = Overbought (Bearish warning)
    'period': 14,
}

# 200 EMA Breakout config (relaxed - Option B)
BREAKOUT_200_CONFIG = {
    'consolidation_days': 20,      # 20 days of consolidation below 200 EMA (relaxed)
    'range_pct_min': 3,            # Minimum 3% range (more flexible)
    'range_pct_max': 15,           # Maximum 15% range (wider range)
    'volume_multiplier': 1.5,      # Volume must be 1.5x average (easier)
    'breakout_min_pct': 0.5,       # Must close 0.5% above 200 EMA (easier)
}

# 200 EMA Retest config
RETEST_200_CONFIG = {
    'ema_period': 200,
    'proximity_pct': 2,            # Within 2% of 200 EMA
    'volume_multiplier': 1.5,      # Volume must be 1.5x average
    'min_bounce_pct': 0.5,         # Must bounce at least 0.5%
}


# Blue Zone Stocks config (Strong Momentum Pullback)
BLUE_ZONE_CONFIG = {
    'rsi_ema_period': 9,           # EMA period for RSI smoothing
    'rsi_period': 14,              # RSI calculation period
    'daily_rsi_threshold': 75,     # Daily RSI EMA(9) must be > 75 (UPDATED)
    'weekly_rsi_threshold': 70,    # Weekly RSI EMA(9) must be > 70 (ADDED)
    'datr_multiplier': 1.5,        # Within 1.5 DATR of 52-week high
    'atr_period': 14,              # ATR calculation period
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


def calculate_atr(high, low, close, period=14):
    """Calculate Average True Range"""
    tr1 = high - low
    tr2 = abs(high - close.shift())
    tr3 = abs(low - close.shift())
    
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()
    return atr


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
        # Get data from Supabase (already has EMAs calculated)
        hist = fetch_ohlc_from_supabase(ticker, days=730)
        
        if hist.empty or len(hist) < EMA_CONFIG['filter_ema']:
            return None
        
        # Use pre-calculated EMAs from Supabase (NO recalculation!)
        if 'ema_20' in hist.columns and 'ema_50' in hist.columns and 'ema_200' in hist.columns:
            hist['short_ema'] = hist['ema_20']
            hist['long_ema'] = hist['ema_50']
            hist['filter_ema'] = hist['ema_200']
        else:
            # Fallback: calculate if not available (shouldn't happen with Zerodha data)
            hist['short_ema'] = calculate_ema(hist['Close'], EMA_CONFIG['short_ema'])
            hist['long_ema'] = calculate_ema(hist['Close'], EMA_CONFIG['long_ema'])
            hist['filter_ema'] = calculate_ema(hist['Close'], EMA_CONFIG['filter_ema'])
        
        # Calculate RSI and volume (not pre-calculated in daily snapshots)
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
        # UPDATED: Promoter Buying -> BULLISH, Promoter Selling -> BEARISH
        alert_category = 'BULLISH' if is_buying else 'BEARISH'
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

# ================== TECHNICAL INDICATORS ==================

def scan_volume_breakout(stock):
    """Detect volume breakout (Volume > 2x average)"""
    ticker = stock['ticker']
    symbol = stock['symbol']
    name = stock['name']
    portfolio = stock['portfolio']
    
    try:
        # Get price data - USE 1 YEAR for consistency with Blue Zone scanner
        hist = fetch_ohlc_from_supabase(ticker, days=730)  # Changed from 3mo to 1y for consistency
        
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
        
        if abs(price_change_pct) < 3.0:  # Ignore if price didn't move much
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


# ================== BLUE ZONE STOCKS SCANNER ==================

def scan_blue_zone_stocks(stock):
    """
    Identify strong momentum stocks (Blue Zone)
    Conditions:
    1. Daily RSI EMA(9) > 75 (strong momentum)
    2. Weekly RSI EMA(9) > 70 (weekly confirmation)
    3. Within 1.5 DATR of 52-week high (pullback in uptrend)
    Counts consecutive trading days in Blue Zone
    """
    ticker = stock['ticker']
    symbol = stock['symbol']
    name = stock['name']
    portfolio = stock['portfolio']
    
    try:
        # Get 1 year of price data
        hist = fetch_ohlc_from_supabase(ticker, days=730)
        
        # Need at least 120 days of data (6 months minimum)
        if hist.empty or len(hist) < 120:
            return None
        
        # Calculate RSI
        hist['rsi'] = calculate_rsi(hist['Close'], BLUE_ZONE_CONFIG['rsi_period'])
        
        # Calculate EMA of RSI
        hist['rsi_ema'] = calculate_ema(hist['rsi'], BLUE_ZONE_CONFIG['rsi_ema_period'])
        
        # Weekly RSI is already pre-calculated in Supabase, just use it directly
        if 'weekly_rsi_ema_9' in hist.columns:
            hist['weekly_rsi_ema'] = hist['weekly_rsi_ema_9']
        
        # Calculate ATR
        hist['atr'] = calculate_atr(hist['High'], hist['Low'], hist['Close'], BLUE_ZONE_CONFIG['atr_period'])
        
        # Calculate 52W high with adaptive min_periods to avoid NaN
        # Use up to 252 days but require at least 70% of available data (minimum 120 days)
        lookback_days = min(len(hist), 252)
        min_periods_needed = min(int(lookback_days * 0.7), 120)
        
        hist['high_52w'] = hist['High'].rolling(window=lookback_days, min_periods=min_periods_needed).max()
        
        # Calculate volume metrics
        hist['volume_avg'] = hist['Volume'].rolling(window=VOLUME_CONFIG['avg_period']).mean()
        
        # Get current values
        current_price = hist['Close'].iloc[-1]
        current_rsi_ema = hist['rsi_ema'].iloc[-1]
        current_weekly_rsi_ema = hist['weekly_rsi_ema'].iloc[-1] if 'weekly_rsi_ema' in hist.columns else None
        current_atr = hist['atr'].iloc[-1]
        high_52w = hist['high_52w'].iloc[-1]
        current_volume = hist['Volume'].iloc[-1]
        avg_volume = hist['volume_avg'].iloc[-1]
        
        # Check for NaN values
        if pd.isna([current_rsi_ema, current_atr, high_52w]).any():
            return None
        
        # Check weekly RSI if available
        if current_weekly_rsi_ema is None or pd.isna(current_weekly_rsi_ema):
            return None
        
        # Calculate distance from 52-week high in DATR
        distance_from_high = high_52w - current_price
        datr_distance = distance_from_high / current_atr
        
        # Check all three conditions (UPDATED)
        condition_1 = current_rsi_ema > BLUE_ZONE_CONFIG['daily_rsi_threshold']  # Daily RSI > 75
        condition_2 = current_weekly_rsi_ema > BLUE_ZONE_CONFIG['weekly_rsi_threshold']  # Weekly RSI > 70
        condition_3 = datr_distance <= BLUE_ZONE_CONFIG['datr_multiplier']
        
        if not (condition_1 and condition_2 and condition_3):
            return None
        
        # Count consecutive trading days in Blue Zone
        days_in_blue_zone = 1  # Start with today
        for i in range(len(hist) - 2, -1, -1):  # Loop backwards from yesterday
            try:
                past_rsi_ema = hist['rsi_ema'].iloc[i]
                past_weekly_rsi_ema = hist['weekly_rsi_ema'].iloc[i] if 'weekly_rsi_ema' in hist.columns else None
                past_price = hist['Close'].iloc[i]
                past_high_52w = hist['high_52w'].iloc[i]
                past_atr = hist['atr'].iloc[i]
                
                if pd.isna([past_rsi_ema, past_atr, past_high_52w]).any():
                    break
                
                if past_weekly_rsi_ema is None or pd.isna(past_weekly_rsi_ema):
                    break
                
                past_datr = (past_high_52w - past_price) / past_atr
                
                # Check if met Blue Zone conditions (UPDATED with new thresholds)
                if (past_rsi_ema > BLUE_ZONE_CONFIG['daily_rsi_threshold'] and 
                    past_weekly_rsi_ema > BLUE_ZONE_CONFIG['weekly_rsi_threshold'] and
                    past_datr <= BLUE_ZONE_CONFIG['datr_multiplier']):
                    days_in_blue_zone += 1
                else:
                    break  # Stop counting when conditions not met
            except Exception as e:
                break  # Stop on any error
        
        # Calculate additional metrics for display
        pct_from_high = ((high_52w - current_price) / high_52w) * 100
        
        # Check volume breakout
        volume_ratio = float(current_volume / avg_volume) if avg_volume > 0 else 0.0
        has_volume_breakout = volume_ratio >= VOLUME_CONFIG['multiplier']
        
        alert_type = 'blue_zone_stocks'
        alert_category = 'BULLISH'
        alert_title = f"{name} - Blue Zone Stock"
        
        # Update description to include days and volume breakout status
        alert_description = f"Strong momentum (Daily RSI: {current_rsi_ema:.1f}, Weekly RSI: {current_weekly_rsi_ema:.1f}) with {pct_from_high:.1f}% pullback from 52W high • {days_in_blue_zone} days in Blue Zone"
        if has_volume_breakout:
            alert_description += f" + Volume breakout ({volume_ratio:.1f}x) 🔥"
        
        details = {
            'daily_rsi_ema_9': round(current_rsi_ema, 2),
            'weekly_rsi_ema_9': round(current_weekly_rsi_ema, 2),
            'daily_rsi_threshold': BLUE_ZONE_CONFIG['daily_rsi_threshold'],
            'weekly_rsi_threshold': BLUE_ZONE_CONFIG['weekly_rsi_threshold'],
            'current_price': round(current_price, 2),
            'high_52w': round(high_52w, 2),
            'pct_from_high': round(pct_from_high, 2),
            'datr_distance': round(datr_distance, 2),
            'datr_limit': BLUE_ZONE_CONFIG['datr_multiplier'],
            'current_atr': round(current_atr, 2),
            'current_volume': int(current_volume),
            'avg_volume': int(avg_volume),
            'volume_ratio': round(volume_ratio, 2),
            'volume_breakout': bool(has_volume_breakout),
            'volume_threshold': VOLUME_CONFIG['multiplier'],
            'days_in_blue_zone': days_in_blue_zone
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
        print(f"  ❌ Error scanning Blue Zone for {symbol}: {e}")
        return None

# ================== RSI SCANNER (COMMENTED OUT - NOT IN USE) ==================
# The following RSI functions have been commented out and replaced with Blue Zone Stocks
# Uncomment if you want to re-enable RSI-based alerts

# def find_peaks_and_troughs(series, window=5):
#     """Find peaks (local maxima) and troughs (local minima) in a series"""
#     peaks = []
#     troughs = []
    
#     for i in range(window, len(series) - window):
#         # Check if it's a peak
#         if series.iloc[i] == max(series.iloc[i-window:i+window+1]):
#             peaks.append(i)
#         # Check if it's a trough
#         if series.iloc[i] == min(series.iloc[i-window:i+window+1]):
#             troughs.append(i)
    
#     return peaks, troughs

# def scan_rsi_bullish_divergence(stock):
#     """Detect RSI bullish divergence - price lower lows, RSI higher lows"""
#     ticker = stock['ticker']
#     symbol = stock['symbol']
#     name = stock['name']
#     portfolio = stock['portfolio']
    
#     try:
#         # Get price data
#         yf_ticker = yf.Ticker(f"{ticker}.NS")
#         hist = yf_ticker.history(period='3mo')
        
#         if hist.empty or len(hist) < 30:
#             return None
        
#         # Calculate RSI
#         hist['rsi'] = calculate_rsi(hist['Close'], RSI_CONFIG['period'])
        
#         # Look at last 30 days
#         window_data = hist.iloc[-30:]
        
#         if len(window_data) < 20:
#             return None
        
#         # Find price troughs and RSI troughs
#         price_troughs_idx, _ = find_peaks_and_troughs(window_data['Close'])
#         rsi_troughs_idx, _ = find_peaks_and_troughs(window_data['rsi'])
        
#         # Need at least 2 troughs to compare
#         if len(price_troughs_idx) < 2 or len(rsi_troughs_idx) < 2:
#             return None
        
#         # Get the last 2 price troughs
#         last_price_trough_idx = price_troughs_idx[-1]
#         prev_price_trough_idx = price_troughs_idx[-2]
        
#         last_price_trough = window_data['Close'].iloc[last_price_trough_idx]
#         prev_price_trough = window_data['Close'].iloc[prev_price_trough_idx]
        
#         # Get the last 2 RSI troughs
#         last_rsi_trough_idx = rsi_troughs_idx[-1]
#         prev_rsi_trough_idx = rsi_troughs_idx[-2]
        
#         last_rsi_trough = window_data['rsi'].iloc[last_rsi_trough_idx]
#         prev_rsi_trough = window_data['rsi'].iloc[prev_rsi_trough_idx]
        
#         # Check for divergence: price making lower lows, RSI making higher lows
#         if last_price_trough < prev_price_trough and last_rsi_trough > prev_rsi_trough:
#             # Additional confirmation: last trough should be recent (within last 5 days)
#             if last_price_trough_idx < len(window_data) - 5:
#                 return None
            
#             current_price = hist['Close'].iloc[-1]
#             current_rsi = hist['rsi'].iloc[-1]
            
#             alert_type = 'rsi_bullish_divergence'
#             alert_category = 'BULLISH'
#             alert_title = f"{name} - RSI Bullish Divergence"
#             alert_description = f"Price making lower lows but RSI making higher lows"
            
#             details = {
#                 'current_rsi': round(current_rsi, 2),
#                 'last_price_trough': round(last_price_trough, 2),
#                 'prev_price_trough': round(prev_price_trough, 2),
#                 'last_rsi_trough': round(last_rsi_trough, 2),
#                 'prev_rsi_trough': round(prev_rsi_trough, 2),
#                 'price_change_pct': round(((last_price_trough - prev_price_trough) / prev_price_trough) * 100, 2),
#                 'rsi_change': round(last_rsi_trough - prev_rsi_trough, 2)
#             }
            
#             return {
#                 'portfolio': portfolio,
#                 'ticker': symbol,
#                 'stock_name': name,
#                 'alert_type': alert_type,
#                 'alert_category': alert_category,
#                 'alert_title': alert_title,
#                 'alert_description': alert_description,
#                 'price': round(current_price, 2),
#                 'alert_date': datetime.now().date().isoformat(),
#                 'details': details
#             }
        
#         return None
        
#     except Exception as e:
#         print(f"  ❌ Error scanning RSI bullish divergence for {symbol}: {e}")
#         return None

# def scan_rsi_oversold_recovery(stock):
#     """Detect RSI crossing above 30 after being oversold"""
#     ticker = stock['ticker']
#     symbol = stock['symbol']
#     name = stock['name']
#     portfolio = stock['portfolio']
    
#     try:
#         # Get price data
#         yf_ticker = yf.Ticker(f"{ticker}.NS")
#         hist = yf_ticker.history(period='2mo')
        
#         if hist.empty or len(hist) < 20:
#             return None
        
#         # Calculate RSI
#         hist['rsi'] = calculate_rsi(hist['Close'], RSI_CONFIG['period'])
        
#         current_rsi = hist['rsi'].iloc[-1]
        
#         if pd.isna(current_rsi):
#             return None
        
#         # Check last 5 days for oversold condition
#         recent_rsi = hist['rsi'].iloc[-6:-1]  # Last 5 days (excluding today)
        
#         # Was oversold in last 5 days and now crossed above 30
#         was_oversold = any(recent_rsi < RSI_CONFIG['oversold'])
#         crossed_above = current_rsi >= RSI_CONFIG['oversold']
        
#         # Find the lowest RSI in recent period
#         if was_oversold and crossed_above:
#             lowest_rsi = recent_rsi.min()
            
#             # Additional filter: current RSI should be between 30-45 (not too high)
#             if current_rsi > 45:
#                 return None
            
#             current_price = hist['Close'].iloc[-1]
            
#             alert_type = 'rsi_oversold_recovery'
#             alert_category = 'BULLISH'
#             alert_title = f"{name} - RSI Oversold Recovery"
#             alert_description = f"RSI crossed above 30 after reaching {lowest_rsi:.1f}"
            
#             details = {
#                 'current_rsi': round(current_rsi, 2),
#                 'lowest_rsi': round(lowest_rsi, 2),
#                 'rsi_oversold_level': RSI_CONFIG['oversold'],
#                 'recovery_strength': 'Strong' if current_rsi > 35 else 'Moderate'
#             }
            
#             return {
#                 'portfolio': portfolio,
#                 'ticker': symbol,
#                 'stock_name': name,
#                 'alert_type': alert_type,
#                 'alert_category': alert_category,
#                 'alert_title': alert_title,
#                 'alert_description': alert_description,
#                 'price': round(current_price, 2),
#                 'alert_date': datetime.now().date().isoformat(),
#                 'details': details
#             }
        
#         return None
        
#     except Exception as e:
#         print(f"  ❌ Error scanning RSI oversold recovery for {symbol}: {e}")
#         return None

# def scan_rsi_bearish_divergence(stock):
#     """Detect RSI bearish divergence - price higher highs, RSI lower highs"""
#     ticker = stock['ticker']
#     symbol = stock['symbol']
#     name = stock['name']
#     portfolio = stock['portfolio']
    
#     try:
#         # Get price data
#         yf_ticker = yf.Ticker(f"{ticker}.NS")
#         hist = yf_ticker.history(period='3mo')
        
#         if hist.empty or len(hist) < 30:
#             return None
        
#         # Calculate RSI
#         hist['rsi'] = calculate_rsi(hist['Close'], RSI_CONFIG['period'])
        
#         # Look at last 30 days
#         window_data = hist.iloc[-30:]
        
#         if len(window_data) < 20:
#             return None
        
#         # Find price peaks and RSI peaks
#         _, price_peaks_idx = find_peaks_and_troughs(window_data['Close'])
#         _, rsi_peaks_idx = find_peaks_and_troughs(window_data['rsi'])
        
#         # Need at least 2 peaks to compare
#         if len(price_peaks_idx) < 2 or len(rsi_peaks_idx) < 2:
#             return None
        
#         # Get the last 2 price peaks
#         last_price_peak_idx = price_peaks_idx[-1]
#         prev_price_peak_idx = price_peaks_idx[-2]
        
#         last_price_peak = window_data['Close'].iloc[last_price_peak_idx]
#         prev_price_peak = window_data['Close'].iloc[prev_price_peak_idx]
        
#         # Get the last 2 RSI peaks
#         last_rsi_peak_idx = rsi_peaks_idx[-1]
#         prev_rsi_peak_idx = rsi_peaks_idx[-2]
        
#         last_rsi_peak = window_data['rsi'].iloc[last_rsi_peak_idx]
#         prev_rsi_peak = window_data['rsi'].iloc[prev_rsi_peak_idx]
        
#         # Check for divergence: price making higher highs, RSI making lower highs
#         if last_price_peak > prev_price_peak and last_rsi_peak < prev_rsi_peak:
#             # Additional confirmation: last peak should be recent (within last 5 days)
#             if last_price_peak_idx < len(window_data) - 5:
#                 return None
            
#             current_price = hist['Close'].iloc[-1]
#             current_rsi = hist['rsi'].iloc[-1]
            
#             alert_type = 'rsi_bearish_divergence'
#             alert_category = 'BEARISH'
#             alert_title = f"{name} - RSI Bearish Divergence"
#             alert_description = f"Price making higher highs but RSI making lower highs"
            
#             details = {
#                 'current_rsi': round(current_rsi, 2),
#                 'last_price_peak': round(last_price_peak, 2),
#                 'prev_price_peak': round(prev_price_peak, 2),
#                 'last_rsi_peak': round(last_rsi_peak, 2),
#                 'prev_rsi_peak': round(prev_rsi_peak, 2),
#                 'price_change_pct': round(((last_price_peak - prev_price_peak) / prev_price_peak) * 100, 2),
#                 'rsi_change': round(last_rsi_peak - prev_rsi_peak, 2)
#             }
            
#             return {
#                 'portfolio': portfolio,
#                 'ticker': symbol,
#                 'stock_name': name,
#                 'alert_type': alert_type,
#                 'alert_category': alert_category,
#                 'alert_title': alert_title,
#                 'alert_description': alert_description,
#                 'price': round(current_price, 2),
#                 'alert_date': datetime.now().date().isoformat(),
#                 'details': details
#             }
        
#         return None
        
#     except Exception as e:
#         print(f"  ❌ Error scanning RSI bearish divergence for {symbol}: {e}")
#         return None

# def scan_rsi_overbought_breakdown(stock):
#     """Detect RSI crossing below 70 after being overbought"""
#     ticker = stock['ticker']
#     symbol = stock['symbol']
#     name = stock['name']
#     portfolio = stock['portfolio']
    
#     try:
#         # Get price data
#         yf_ticker = yf.Ticker(f"{ticker}.NS")
#         hist = yf_ticker.history(period='2mo')
        
#         if hist.empty or len(hist) < 20:
#             return None
        
#         # Calculate RSI
#         hist['rsi'] = calculate_rsi(hist['Close'], RSI_CONFIG['period'])
        
#         current_rsi = hist['rsi'].iloc[-1]
        
#         if pd.isna(current_rsi):
#             return None
        
#         # Check last 5 days for overbought condition
#         recent_rsi = hist['rsi'].iloc[-6:-1]  # Last 5 days (excluding today)
        
#         # Was overbought in last 5 days and now crossed below 70
#         was_overbought = any(recent_rsi > RSI_CONFIG['overbought'])
#         crossed_below = current_rsi <= RSI_CONFIG['overbought']
        
#         # Find the highest RSI in recent period
#         if was_overbought and crossed_below:
#             highest_rsi = recent_rsi.max()
            
#             # Additional filter: current RSI should be between 55-70 (not too low)
#             if current_rsi < 55:
#                 return None
            
#             current_price = hist['Close'].iloc[-1]
            
#             alert_type = 'rsi_overbought_breakdown'
#             alert_category = 'BEARISH'
#             alert_title = f"{name} - RSI Overbought Breakdown"
#             alert_description = f"RSI crossed below 70 after reaching {highest_rsi:.1f}"
            
#             details = {
#                 'current_rsi': round(current_rsi, 2),
#                 'highest_rsi': round(highest_rsi, 2),
#                 'rsi_overbought_level': RSI_CONFIG['overbought'],
#                 'breakdown_strength': 'Strong' if current_rsi < 65 else 'Moderate'
#             }
            
#             return {
#                 'portfolio': portfolio,
#                 'ticker': symbol,
#                 'stock_name': name,
#                 'alert_type': alert_type,
#                 'alert_category': alert_category,
#                 'alert_title': alert_title,
#                 'alert_description': alert_description,
#                 'price': round(current_price, 2),
#                 'alert_date': datetime.now().date().isoformat(),
#                 'details': details
#             }
        
#         return None
        
#     except Exception as e:
#         print(f"  ❌ Error scanning RSI overbought breakdown for {symbol}: {e}")
#         return None

def scan_200ema_breakout(stock):
    """Scan for 200 EMA breakout after consolidation"""
    ticker = stock['ticker']
    symbol = stock['symbol']
    name = stock['name']
    portfolio = stock['portfolio']
    
    try:
        hist = fetch_ohlc_from_supabase(ticker, days=730)
        
        if hist.empty or len(hist) < 250:
            return None
        
        # Calculate 200 EMA and volume average
        hist['ema_200'] = calculate_ema(hist['Close'], 200)
        hist['volume_avg'] = hist['Volume'].rolling(window=20).mean()
        
        # Get current values
        current_price = hist['Close'].iloc[-1]
        current_ema_200 = hist['ema_200'].iloc[-1]
        current_volume = hist['Volume'].iloc[-1]
        avg_volume = hist['volume_avg'].iloc[-1]
        
        if pd.isna([current_ema_200, avg_volume]).any():
            return None
        
        # Check if price broke above 200 EMA today
        prev_price = hist['Close'].iloc[-2]
        prev_ema_200 = hist['ema_200'].iloc[-2]
        
        # Breakout condition: previous close below EMA, current close above EMA
        if not (prev_price <= prev_ema_200 and current_price > current_ema_200):
            return None
        
        # Calculate breakout percentage
        breakout_pct = ((current_price - current_ema_200) / current_ema_200) * 100
        
        if breakout_pct < BREAKOUT_200_CONFIG['breakout_min_pct']:
            return None
        
        # Count consolidation days (price stayed below 200 EMA)
        consolidation_days = 0
        for i in range(len(hist) - 2, -1, -1):
            if hist['Close'].iloc[i] <= hist['ema_200'].iloc[i]:
                consolidation_days += 1
            else:
                break
        
        # Must have consolidated for minimum days
        if consolidation_days < BREAKOUT_200_CONFIG['consolidation_days']:
            return None
        
        # Calculate consolidation range
        consolidation_data = hist.iloc[-(consolidation_days+1):-1]
        consolidation_high = consolidation_data['High'].max()
        consolidation_low = consolidation_data['Low'].min()
        range_pct = ((consolidation_high - consolidation_low) / consolidation_low) * 100
        
        # Range must be within acceptable bounds
        if not (BREAKOUT_200_CONFIG['range_pct_min'] <= range_pct <= BREAKOUT_200_CONFIG['range_pct_max']):
            return None
        
        # Volume confirmation
        volume_ratio = float(current_volume / avg_volume) if avg_volume > 0 else 0.0
        
        if volume_ratio < BREAKOUT_200_CONFIG['volume_multiplier']:
            return None
        
        alert_description = f"Broke above 200 EMA after {consolidation_days}-day consolidation ({range_pct:.1f}% range) with {volume_ratio:.1f}x volume"
        
        return {
            'portfolio': portfolio,
            'ticker': symbol,
            'stock_name': name,
            'alert_type': '200ema_breakout',
            'alert_category': 'BULLISH',
            'alert_title': f"{name} - 200 EMA Breakout",
            'alert_description': alert_description,
            'price': round(float(current_price), 2),
            'alert_date': datetime.now().date().isoformat(),
            'details': {
                'current_price': round(float(current_price), 2),
                'ema_200': round(float(current_ema_200), 2),
                'breakout_pct': round(float(breakout_pct), 2),
                'consolidation_days': consolidation_days,
                'range_pct': round(float(range_pct), 2),
                'volume_ratio': round(float(volume_ratio), 2),
                'current_volume': int(current_volume),
                'avg_volume': int(avg_volume)
            }
        }
    except Exception as e:
        print(f"  ❌ Error scanning 200 EMA breakout for {symbol}: {e}")
        return None

def scan_200ema_retest(stock):
    """Scan for 200 EMA retest (pullback to support)"""
    ticker = stock['ticker']
    symbol = stock['symbol']
    name = stock['name']
    portfolio = stock['portfolio']
    
    try:
        hist = fetch_ohlc_from_supabase(ticker, days=730)
        
        if hist.empty or len(hist) < 250:
            return None
        
        # Calculate 200 EMA and volume average
        hist['ema_200'] = calculate_ema(hist['Close'], 200)
        hist['volume_avg'] = hist['Volume'].rolling(window=20).mean()
        
        # Get current values
        current_price = hist['Close'].iloc[-1]
        current_low = hist['Low'].iloc[-1]
        current_ema_200 = hist['ema_200'].iloc[-1]
        current_volume = hist['Volume'].iloc[-1]
        avg_volume = hist['volume_avg'].iloc[-1]
        
        if pd.isna([current_ema_200, avg_volume]).any():
            return None
        
        # Stock must be above 200 EMA (established uptrend)
        if current_price <= current_ema_200:
            return None
        
        # Check if stock came close to 200 EMA (retest)
        proximity_pct = ((current_low - current_ema_200) / current_ema_200) * 100
        
        if proximity_pct > RETEST_200_CONFIG['proximity_pct']:
            return None
        
        # Must have bounced from the retest
        prev_close = hist['Close'].iloc[-2]
        bounce_pct = ((current_price - prev_close) / prev_close) * 100
        
        if bounce_pct < RETEST_200_CONFIG['min_bounce_pct']:
            return None
        
        # Volume confirmation
        volume_ratio = float(current_volume / avg_volume) if avg_volume > 0 else 0.0
        
        if volume_ratio < RETEST_200_CONFIG['volume_multiplier']:
            return None
        
        # Find when stock initially broke above 200 EMA
        days_since_breakout = 0
        for i in range(len(hist) - 2, -1, -1):
            if hist['Close'].iloc[i] > hist['ema_200'].iloc[i]:
                days_since_breakout += 1
            else:
                break
        
        alert_description = f"Retested 200 EMA as support ({proximity_pct:.1f}% from EMA) and bounced {bounce_pct:.1f}% with {volume_ratio:.1f}x volume"
        
        return {
            'portfolio': portfolio,
            'ticker': symbol,
            'stock_name': name,
            'alert_type': '200ema_retest',
            'alert_category': 'BULLISH',
            'alert_title': f"{name} - 200 EMA Retest",
            'alert_description': alert_description,
            'price': round(float(current_price), 2),
            'alert_date': datetime.now().date().isoformat(),
            'details': {
                'current_price': round(float(current_price), 2),
                'ema_200': round(float(current_ema_200), 2),
                'proximity_pct': round(float(proximity_pct), 2),
                'bounce_pct': round(float(bounce_pct), 2),
                'days_since_breakout': days_since_breakout,
                'volume_ratio': round(float(volume_ratio), 2),
                'current_volume': int(current_volume),
                'avg_volume': int(avg_volume)
            }
        }
    except Exception as e:
        print(f"  ❌ Error scanning 200 EMA retest for {symbol}: {e}")
        return None

# ================== QUARTERLY RESULTS SCANNER ==================

def scan_upcoming_results(stock):
    """Scan for upcoming quarterly results in next 7 days using Trendlyne"""
    ticker = stock['ticker']
    symbol = stock['symbol']
    name = stock['name']
    portfolio = stock['portfolio']
    
    try:
        # Trendlyne URL for results calendar
        url = f"https://trendlyne.com/equity/{symbol}/results-calendar/"
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code != 200:
            return None
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Look for upcoming results date
        # Trendlyne typically shows this in a specific section
        # We'll look for text patterns like "Expected on: DD Mon YYYY" or similar
        
        # Try multiple selectors as Trendlyne structure may vary
        results_date = None
        quarter = None
        
        # Strategy 1: Look for "Expected on" or "Results on" text
        for text_elem in soup.find_all(string=True):
            text = str(text_elem).strip().lower()
            if 'expected' in text or 'results' in text:
                # Try to extract date from nearby elements
                parent = text_elem.parent
                if parent:
                    date_text = parent.get_text()
                    # Try to parse date patterns
                    import re
                    # Pattern: DD Mon YYYY or DD-MM-YYYY
                    date_patterns = [
                        r'(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{4})',
                        r'(\d{1,2})[/-](\d{1,2})[/-](\d{4})'
                    ]
                    
                    for pattern in date_patterns:
                        match = re.search(pattern, date_text, re.IGNORECASE)
                        if match:
                            try:
                                # Parse the date
                                from dateutil import parser
                                results_date = parser.parse(date_text, fuzzy=True).date()
                                break
                            except:
                                continue
                
                if results_date:
                    break
        
        # Strategy 2: Look in meta tags or structured data
        if not results_date:
            meta_tags = soup.find_all('meta')
            for meta in meta_tags:
                content = meta.get('content', '')
                if 'result' in content.lower() or 'earning' in content.lower():
                    # Try to parse date from content
                    try:
                        from dateutil import parser
                        results_date = parser.parse(content, fuzzy=True).date()
                        break
                    except:
                        continue
        
        if not results_date:
            # No upcoming results found
            return None
        
        # Calculate days until results
        today = datetime.now().date()
        days_until = (results_date - today).days
        
        # Only alert if within next 7 days and not past
        if days_until < 0 or days_until > 7:
            return None
        
        # Determine priority
        if days_until <= 3:
            priority = 'URGENT'
            priority_emoji = '🔴'
        else:
            priority = 'UPCOMING'
            priority_emoji = '🟡'
        
        # Determine quarter (rough approximation based on month)
        current_month = datetime.now().month
        if current_month <= 3:
            quarter = 'Q4'
        elif current_month <= 6:
            quarter = 'Q1'
        elif current_month <= 9:
            quarter = 'Q2'
        else:
            quarter = 'Q3'
        
        current_year = datetime.now().year
        fy_year = current_year if current_month <= 3 else current_year + 1
        quarter_label = f"{quarter} FY{fy_year}"
        
        alert_description = f"Results expected on {results_date.strftime('%b %d, %Y')} (in {days_until} {'day' if days_until == 1 else 'days'}) {priority_emoji}"
        
        return {
            'portfolio': portfolio,
            'ticker': symbol,
            'stock_name': name,
            'alert_type': 'quarterly_results',
            'alert_category': 'INFO',
            'alert_title': f"{name} - Quarterly Results Upcoming",
            'alert_description': alert_description,
            'price': None,  # Not relevant for results alerts
            'alert_date': today.isoformat(),
            'details': {
                'results_date': results_date.isoformat(),
                'days_until': days_until,
                'quarter': quarter_label,
                'priority': priority,
                'trendlyne_url': url
            }
        }
        
    except Exception as e:
        print(f"  ❌ Error scanning results for {symbol}: {e}")
        return None

# ================== ALERT INSERTION ==================

def insert_alert(alert_data):
    """Insert alert into Supabase (avoid duplicates)"""
    try:
        # Ensure details field is JSON serializable
        import json
        if 'details' in alert_data and alert_data['details']:
            # Convert numpy bools and other non-JSON types
            alert_data['details'] = json.loads(json.dumps(alert_data['details'], default=lambda x: str(x) if not isinstance(x, bool) else x))
        
        today = datetime.now().date().isoformat()
        
        # FIRST: Archive any old alerts (from previous days) for same ticker+type
        old_alerts = supabase.table('alerts').select('id').eq(
            'ticker', alert_data['ticker']
        ).eq(
            'alert_type', alert_data['alert_type']
        ).lt(
            'alert_date', today  # Before today
        ).eq(
            'status', 'NEW'
        ).execute()
        
        if len(old_alerts.data) > 0:
            # Archive them
            for old_alert in old_alerts.data:
                supabase.table('alerts').update({'status': 'ARCHIVED'}).eq('id', old_alert['id']).execute()
            print(f"  📦 Archived {len(old_alerts.data)} old alert(s) for {alert_data['ticker']}")
        
        # THEN: Check if alert for TODAY already exists
        existing_today = supabase.table('alerts').select('id').eq(
            'ticker', alert_data['ticker']
        ).eq(
            'alert_type', alert_data['alert_type']
        ).eq(
            'alert_date', today  # Same day
        ).eq(
            'status', 'NEW'
        ).execute()
        
        if len(existing_today.data) > 0:
            print(f"  ⚠️ Alert for TODAY already exists for {alert_data['ticker']} - {alert_data['alert_type']}")
            return False
        
        # Insert new alert
        response = supabase.table('alerts').insert(alert_data).execute()
        print(f"  ✅ Inserted: {alert_data['ticker']} - {alert_data['alert_type']}")
        return True
    except Exception as e:
        print(f"  ❌ Error inserting alert: {e}")
        return False

def auto_archive_old_alerts(days=1):
    """Auto-archive alerts older than specified days"""
    try:
        cutoff_date = (datetime.now() - timedelta(days=days)).date().isoformat()
        
        result = supabase.table('alerts').update(
            {'status': 'ARCHIVED'}
        ).eq('status', 'NEW').lt('alert_date', cutoff_date).execute()
        
        count = len(result.data) if result.data else 0
        if count > 0:
            print(f"  ✅ Auto-archived {count} alerts older than {days} days")
        else:
            print(f"  ℹ️ No alerts older than {days} days to archive")
        
        return count
    except Exception as e:
        print(f"  ❌ Error auto-archiving alerts: {e}")
        return 0

def archive_past_results_alerts():
    """Archive quarterly results alerts where results date has passed"""
    try:
        today = datetime.now().date().isoformat()
        
        # Get all active quarterly_results alerts
        alerts_response = supabase.table('alerts').select('id, details').eq(
            'alert_type', 'quarterly_results'
        ).eq(
            'status', 'NEW'
        ).execute()
        
        archived_count = 0
        for alert in alerts_response.data:
            if alert.get('details') and alert['details'].get('results_date'):
                results_date = alert['details']['results_date']
                # If results date has passed, archive it
                if results_date < today:
                    supabase.table('alerts').update(
                        {'status': 'ARCHIVED'}
                    ).eq('id', alert['id']).execute()
                    archived_count += 1
        
        if archived_count > 0:
            print(f"  ✅ Archived {archived_count} past results alerts")
        
        return archived_count
    except Exception as e:
        print(f"  ❌ Error archiving results alerts: {e}")
        return 0
        
        return count
    except Exception as e:
        print(f"  ❌ Error auto-archiving alerts: {e}")
        return 0

# ================== MAIN EXECUTION ==================

# ================== MAIN EXECUTION ==================

def main():
    # Check if today is a weekday (Monday=0, Sunday=6)
    if datetime.now().weekday() >= 5:  # Saturday=5, Sunday=6
        print("=" * 70)
        print("📅 WEEKEND DETECTED")
        print(f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("⏸️  Market is closed. Scanner will not run on weekends.")
        print("=" * 70)
        return
    
    print("=" * 70)
    print("📈 PORTFOLIO ALERT SCANNER")
    print(f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    
    # Step 1: Get portfolio stocks
    print("\n📊 Fetching portfolio stocks...")
    portfolio_stocks = get_portfolio_stocks()
    print(f"  ✅ Found {len(portfolio_stocks)} stocks in portfolio")

# Step 1.5: Auto-archive old alerts
    print("\n📦 Auto-archiving old alerts...")
    auto_archive_old_alerts(days=1)
    archive_past_results_alerts()  # Archive results alerts where date has passed
    
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
    
    # Step 3: Scan for promoter buying/selling
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

    
    # Step 5: Scan for Blue Zone Stocks (Strong Momentum Pullback)
    print("\n\n🔵 Scanning for Blue Zone Stocks...")
    print(f"  Configuration: RSI EMA(9) > {BLUE_ZONE_CONFIG['rsi_threshold']}, within {BLUE_ZONE_CONFIG['datr_multiplier']} DATR of 52W high")
    
    for stock in portfolio_stocks:
        print(f"  📊 {stock['name']} ({stock['symbol']})...")
        alert = scan_blue_zone_stocks(stock)
        if alert:
            if insert_alert(alert):
                total_alerts += 1
    
    # Step 5A: Scan for RSI Bullish Divergence
    # print("\n\n📈 Scanning for RSI Bullish Divergence...")
    # print(f"  Configuration: Price lower lows, RSI higher lows")
    
    # for stock in portfolio_stocks:
        # print(f"  📊 {stock['name']} ({stock['symbol']})...")
        # alert = scan_rsi_bullish_divergence(stock)
        # if alert:
            # if insert_alert(alert):
                # total_alerts += 1
    
    # Step 5B: Scan for RSI Oversold Recovery
    # print("\n\n✅ Scanning for RSI Oversold Recovery...")
    # print(f"  Configuration: RSI crossing above {RSI_CONFIG['oversold']} after oversold")
    
    # for stock in portfolio_stocks:
        # print(f"  📊 {stock['name']} ({stock['symbol']})...")
        # alert = scan_rsi_oversold_recovery(stock)
        # if alert:
            # if insert_alert(alert):
                # total_alerts += 1
    
    # Step 5C: Scan for RSI Bearish Divergence
    # print("\n\n📉 Scanning for RSI Bearish Divergence...")
    # print(f"  Configuration: Price higher highs, RSI lower highs")
    
    # for stock in portfolio_stocks:
        # print(f"  📊 {stock['name']} ({stock['symbol']})...")
        # alert = scan_rsi_bearish_divergence(stock)
        # if alert:
            # if insert_alert(alert):
                # total_alerts += 1
    
    # Step 5D: Scan for RSI Overbought Breakdown
    # print("\n\n⚠️ Scanning for RSI Overbought Breakdown...")
    # print(f"  Configuration: RSI crossing below {RSI_CONFIG['overbought']} after overbought")
    
    # for stock in portfolio_stocks:
        # print(f"  📊 {stock['name']} ({stock['symbol']})...")
        # alert = scan_rsi_overbought_breakdown(stock)
        # if alert:
            # if insert_alert(alert):
                # total_alerts += 1
    
    # Step 6: Scan for 200 EMA Breakout
    print("\n\n🚀 Scanning for 200 EMA Breakout After Consolidation...")
    print(f"  Configuration: {BREAKOUT_200_CONFIG['consolidation_days']}-day consolidation, {BREAKOUT_200_CONFIG['range_pct_min']}-{BREAKOUT_200_CONFIG['range_pct_max']}% range")
    
    for stock in portfolio_stocks:
        print(f"  📊 {stock['name']} ({stock['symbol']})...")
        alert = scan_200ema_breakout(stock)
        if alert:
            if insert_alert(alert):
                total_alerts += 1
    
    # Step 7: Scan for 200 EMA Retest
    print("\n\n✅ Scanning for 200 EMA Retest Pattern...")
    print(f"  Configuration: Proximity {RETEST_200_CONFIG['proximity_pct']}%, bounce {RETEST_200_CONFIG['min_bounce_pct']}%+")
    
    for stock in portfolio_stocks:
        print(f"  📊 {stock['name']} ({stock['symbol']})...")
        alert = scan_200ema_retest(stock)
        if alert:
            if insert_alert(alert):
                total_alerts += 1
    
    # Step 8: Scan for Upcoming Quarterly Results
    print("\n\n📅 Scanning for Upcoming Quarterly Results (Next 7 Days)...")
    print(f"  Checking earnings calendar for all portfolio stocks...")
    
    for stock in portfolio_stocks:
        print(f"  📊 {stock['name']} ({stock['symbol']})...")
        alert = scan_upcoming_results(stock)
        if alert:
            if insert_alert(alert):
                total_alerts += 1
    
    # Summary
    print("\n" + "=" * 70)
    print(f"✅ SCAN COMPLETE - {total_alerts} new alerts inserted")
    print("=" * 70)

if __name__ == '__main__':
    main()
