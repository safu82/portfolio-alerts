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

# 200 EMA Breakout config
BREAKOUT_200_CONFIG = {
    'consolidation_days': 60,      # 60 days of consolidation below 200 EMA
    'range_pct_min': 5,            # Minimum 5% range
    'range_pct_max': 10,           # Maximum 10% range
    'volume_multiplier': 2.0,      # Volume must be 2x average
    'breakout_min_pct': 1,         # Must close 1% above 200 EMA
}

# 200 EMA Retest config
RETEST_200_CONFIG = {
    'lookback_days': 15,           # Look for breakout in last 15 days
    'pullback_tolerance': 2,       # Within 2% of 200 EMA
    'must_hold_ema': True,         # Cannot close below 200 EMA
    'bounce_min_pct': 2,           # Must bounce at least 2%
    'volume_multiplier': 1.5,      # Volume on bounce > 1.5x average
}


# Blue Zone Stocks config (Strong Momentum Pullback)
BLUE_ZONE_CONFIG = {
    'rsi_ema_period': 9,           # EMA period for RSI smoothing
    'rsi_period': 14,              # RSI calculation period
    'rsi_threshold': 60,           # EMA(9) of RSI must be > 60
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


# ================== BLUE ZONE STOCKS SCANNER ==================

def scan_blue_zone_stocks(stock):
    """
    Identify strong momentum stocks (Blue Zone)
    Conditions:
    1. EMA(9) of RSI(14) > 60 (strong momentum)
    2. Within 1.5 DATR of 52-week high (pullback in uptrend)
    """
    ticker = stock['ticker']
    symbol = stock['symbol']
    name = stock['name']
    portfolio = stock['portfolio']
    
    try:
        # Get 1 year of price data
        yf_ticker = yf.Ticker(f"{ticker}.NS")
        hist = yf_ticker.history(period='1y')
        
        if hist.empty or len(hist) < 252:  # Need at least 1 year of data
            return None
        
        # Calculate RSI
        hist['rsi'] = calculate_rsi(hist['Close'], BLUE_ZONE_CONFIG['rsi_period'])
        
        # Calculate EMA of RSI
        hist['rsi_ema'] = calculate_ema(hist['rsi'], BLUE_ZONE_CONFIG['rsi_ema_period'])
        
        # Calculate ATR
        hist['atr'] = calculate_atr(hist['High'], hist['Low'], hist['Close'], BLUE_ZONE_CONFIG['atr_period'])
        
        # Calculate 52-week high
        hist['high_52w'] = hist['High'].rolling(window=252).max()
        
        # Get current values
        current_price = hist['Close'].iloc[-1]
        current_rsi_ema = hist['rsi_ema'].iloc[-1]
        current_atr = hist['atr'].iloc[-1]
        high_52w = hist['high_52w'].iloc[-1]
        
        # Check for NaN values
        if pd.isna([current_rsi_ema, current_atr, high_52w]).any():
            return None
        
        # Calculate distance from 52-week high in DATR
        distance_from_high = high_52w - current_price
        datr_distance = distance_from_high / current_atr
        
        # Check both conditions
        condition_1 = current_rsi_ema > BLUE_ZONE_CONFIG['rsi_threshold']
        condition_2 = datr_distance <= BLUE_ZONE_CONFIG['datr_multiplier']
        
        if not (condition_1 and condition_2):
            return None
        
        # Calculate additional metrics for display
        pct_from_high = ((high_52w - current_price) / high_52w) * 100
        
        alert_type = 'blue_zone_stocks'
        alert_category = 'BULLISH'
        alert_title = f"{name} - Blue Zone Stock"
        alert_description = f"Strong momentum (RSI EMA: {current_rsi_ema:.1f}) with {pct_from_high:.1f}% pullback from 52W high"
        
        details = {
            'rsi_ema_9': round(current_rsi_ema, 2),
            'rsi_threshold': BLUE_ZONE_CONFIG['rsi_threshold'],
            'current_price': round(current_price, 2),
            'high_52w': round(high_52w, 2),
            'pct_from_high': round(pct_from_high, 2),
            'datr_distance': round(datr_distance, 2),
            'datr_limit': BLUE_ZONE_CONFIG['datr_multiplier'],
            'current_atr': round(current_atr, 2)
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
    """Detect 200 EMA breakout after consolidation below it"""
    ticker = stock['ticker']
    symbol = stock['symbol']
    name = stock['name']
    portfolio = stock['portfolio']
    
    try:
        # Get 1 year of price data
        yf_ticker = yf.Ticker(f"{ticker}.NS")
        hist = yf_ticker.history(period='1y')
        
        if hist.empty or len(hist) < 200:
            return None
        
        # Calculate 200 EMA
        hist['ema_200'] = calculate_ema(hist['Close'], 200)
        hist['volume_avg'] = hist['Volume'].rolling(window=20).mean()
        
        if pd.isna(hist['ema_200'].iloc[-1]):
            return None
        
        # Get consolidation period (last 60 days before today)
        consolidation_start_idx = -BREAKOUT_200_CONFIG['consolidation_days'] - 1
        consolidation_end_idx = -1
        
        consolidation_data = hist.iloc[consolidation_start_idx:consolidation_end_idx]
        
        if len(consolidation_data) < BREAKOUT_200_CONFIG['consolidation_days']:
            return None
        
        # Check if price was consolidating BELOW 200 EMA
        prices_below_ema = (consolidation_data['Close'] < consolidation_data['ema_200']).sum()
        pct_below = (prices_below_ema / len(consolidation_data)) * 100
        
        if pct_below < 80:  # At least 80% of time below 200 EMA
            return None
        
        # Check if consolidation range is between 5-10%
        highest = consolidation_data['Close'].max()
        lowest = consolidation_data['Close'].min()
        range_pct = ((highest - lowest) / lowest) * 100
        
        if range_pct < BREAKOUT_200_CONFIG['range_pct_min'] or range_pct > BREAKOUT_200_CONFIG['range_pct_max']:
            return None
        
        # Check if current price broke ABOVE 200 EMA
        current_price = hist['Close'].iloc[-1]
        current_ema_200 = hist['ema_200'].iloc[-1]
        prev_price = hist['Close'].iloc[-2]
        prev_ema_200 = hist['ema_200'].iloc[-2]
        
        # Breakout condition: previously below, now above
        if not (prev_price <= prev_ema_200 and current_price > current_ema_200):
            return None
        
        # Check breakout strength (at least 1% above 200 EMA)
        breakout_pct = ((current_price - current_ema_200) / current_ema_200) * 100
        if breakout_pct < BREAKOUT_200_CONFIG['breakout_min_pct']:
            return None
        
        # Check volume spike
        current_volume = hist['Volume'].iloc[-1]
        avg_volume = hist['volume_avg'].iloc[-1]
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0
        
        if volume_ratio < BREAKOUT_200_CONFIG['volume_multiplier']:
            return None
        
        alert_type = 'ema_200_breakout'
        alert_category = 'BULLISH'
        alert_title = f"{name} - 200 EMA Breakout After Consolidation"
        alert_description = f"Broke above 200 EMA after {BREAKOUT_200_CONFIG['consolidation_days']}-day consolidation"
        
        details = {
            'current_price': round(current_price, 2),
            'ema_200': round(current_ema_200, 2),
            'breakout_pct': round(breakout_pct, 2),
            'consolidation_days': BREAKOUT_200_CONFIG['consolidation_days'],
            'consolidation_range_pct': round(range_pct, 2),
            'volume_ratio': round(volume_ratio, 2),
            'highest_in_consolidation': round(highest, 2),
            'lowest_in_consolidation': round(lowest, 2)
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
        print(f"  ❌ Error scanning 200 EMA breakout for {symbol}: {e}")
        return None

def scan_200ema_retest(stock):
    """Detect bullish retest of 200 EMA as support after breakout"""
    ticker = stock['ticker']
    symbol = stock['symbol']
    name = stock['name']
    portfolio = stock['portfolio']
    
    try:
        # Get price data
        yf_ticker = yf.Ticker(f"{ticker}.NS")
        hist = yf_ticker.history(period='3mo')
        
        if hist.empty or len(hist) < 200:
            return None
        
        # Calculate 200 EMA
        hist['ema_200'] = calculate_ema(hist['Close'], 200)
        hist['volume_avg'] = hist['Volume'].rolling(window=20).mean()
        
        if pd.isna(hist['ema_200'].iloc[-1]):
            return None
        
        current_price = hist['Close'].iloc[-1]
        current_ema_200 = hist['ema_200'].iloc[-1]
        current_volume = hist['Volume'].iloc[-1]
        avg_volume = hist['volume_avg'].iloc[-1]
        
        # Step 1: Find if there was a breakout in the last 15 days
        lookback_period = hist.iloc[-RETEST_200_CONFIG['lookback_days']:]
        
        breakout_found = False
        breakout_idx = None
        
        for i in range(1, len(lookback_period)):
            prev_price = lookback_period['Close'].iloc[i-1]
            prev_ema = lookback_period['ema_200'].iloc[i-1]
            curr_price = lookback_period['Close'].iloc[i]
            curr_ema = lookback_period['ema_200'].iloc[i]
            
            # Check if crossed above 200 EMA
            if prev_price <= prev_ema and curr_price > curr_ema:
                breakout_found = True
                breakout_idx = i
                break
        
        if not breakout_found:
            return None
        
        # Step 2: Check if there was a pullback to 200 EMA
        # Look at data AFTER breakout
        post_breakout_data = lookback_period.iloc[breakout_idx:]
        
        pullback_found = False
        pullback_low = None
        
        for i in range(len(post_breakout_data)):
            price = post_breakout_data['Close'].iloc[i]
            ema = post_breakout_data['ema_200'].iloc[i]
            
            # Check if price came within tolerance of 200 EMA
            distance_pct = abs(((price - ema) / ema) * 100)
            
            if distance_pct <= RETEST_200_CONFIG['pullback_tolerance']:
                pullback_found = True
                pullback_low = price
                
                # Check if price held ABOVE 200 EMA (didn't close below)
                if RETEST_200_CONFIG['must_hold_ema'] and price < ema:
                    pullback_found = False
                    continue
                
                break
        
        if not pullback_found:
            return None
        
        # Step 3: Check if price bounced from the pullback
        # Current price should be at least 2% higher than pullback low
        bounce_pct = ((current_price - pullback_low) / pullback_low) * 100
        
        if bounce_pct < RETEST_200_CONFIG['bounce_min_pct']:
            return None
        
        # Step 4: Check volume on bounce
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0
        
        if volume_ratio < RETEST_200_CONFIG['volume_multiplier']:
            return None
        
        alert_type = 'ema_200_retest'
        alert_category = 'BULLISH'
        alert_title = f"{name} - Successful Retest of 200 EMA"
        alert_description = f"Bounced {bounce_pct:.1f}% from 200 EMA support"
        
        details = {
            'current_price': round(current_price, 2),
            'ema_200': round(current_ema_200, 2),
            'pullback_low': round(pullback_low, 2),
            'bounce_pct': round(bounce_pct, 2),
            'volume_ratio': round(volume_ratio, 2),
            'days_since_breakout': len(post_breakout_data)
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
        print(f"  ❌ Error scanning 200 EMA retest for {symbol}: {e}")
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
).neq('status', 'ARCHIVED').execute()
        
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

def auto_archive_old_alerts(days=3):
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

# ================== MAIN EXECUTION ==================

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

# Step 1.5: Auto-archive old alerts
    print("\n📦 Auto-archiving old alerts...")
    auto_archive_old_alerts(days=3)
    
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
    print(f"  Configuration: Lookback {RETEST_200_CONFIG['lookback_days']} days, bounce {RETEST_200_CONFIG['bounce_min_pct']}%+")
    
    for stock in portfolio_stocks:
        print(f"  📊 {stock['name']} ({stock['symbol']})...")
        alert = scan_200ema_retest(stock)
        if alert:
            if insert_alert(alert):
                total_alerts += 1
    
    # Summary
    print("\n" + "=" * 70)
    print(f"✅ SCAN COMPLETE - {total_alerts} new alerts inserted")
    print("=" * 70)

if __name__ == '__main__':
    main()
