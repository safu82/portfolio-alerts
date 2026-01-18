#!/usr/bin/env python3
"""
PRODUCTION: Entry Signals Scanner for Nifty 500
Scans for: Narrow CPR, Blue Zone, Golden Cross (with ADX), MACD (with ADX), 200 EMA Retest, Promoter Buying

UPDATED CRITERIA (Jan 2026):
- Narrow CPR: < 0.3% width, within 0.5% of L3, volume > 1.2x
- Blue Zone: RSI EMA(9) > 60, pullback 5-10%, volume > 1.5x (Strong only)
- Golden Cross: 20×50 EMA cross, ADX > 25 + +DI > -DI required for Strong Buy
- MACD: Bullish crossover, ADX > 25 + +DI > -DI required for Strong Buy
- 200 EMA: Within 2%, peak 8%+ above, previous touch with volume
- Promoter Buying: Recent promoter acquisitions in last 30 days

Schedule: Daily at 8:00 AM IST (after OHLC fetch at 7:00 AM)
Runtime: ~15-20 minutes for 500 stocks
Expected Signals: 25-40 high-quality opportunities
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from supabase import create_client, Client
from typing import List, Dict, Tuple, Optional
import time
import requests
from bs4 import BeautifulSoup

# ============================================
# CONFIGURATION
# ============================================

import os

# Read from environment variables (set by GitHub Actions)
# Fallback to hardcoded values for local testing
SUPABASE_URL = os.getenv('SUPABASE_URL', 'https://hcgyncghmcvylnrmcivj.supabase.co')
SUPABASE_KEY = os.getenv('SUPABASE_KEY', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhjZ3luY2dobWN2eWxucm1jaXZqIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTc1MTQwMTEsImV4cCI6MjA3MzA5MDAxMX0.n8vFVCJe1y_3o8fpAY0IgasZ4eKl7DAogEM3OlHB8Ww')

# Stock name mapping (ticker -> name)
STOCK_NAMES = {}  # Will be populated from company info

# ============================================
# TECHNICAL INDICATOR CALCULATIONS
# ============================================

def calculate_ema(series: pd.Series, period: int) -> pd.Series:
    """Calculate Exponential Moving Average"""
    return series.ewm(span=period, adjust=False).mean()

def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Calculate RSI"""
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calculate_macd(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calculate MACD (Moving Average Convergence Divergence)
    Standard settings: 12/26/9
    """
    ema_12 = calculate_ema(df['close'], 12)
    ema_26 = calculate_ema(df['close'], 26)
    macd_line = ema_12 - ema_26
    signal_line = calculate_ema(macd_line, 9)
    histogram = macd_line - signal_line
    
    df['macd_line'] = macd_line
    df['macd_signal'] = signal_line
    df['macd_histogram'] = histogram
    
    return df

def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """
    Calculate ADX (Average Directional Index) with +DI and -DI
    """
    high = df['high']
    low = df['low']
    close = df['close']
    
    # True Range
    high_low = high - low
    high_close = np.abs(high - close.shift())
    low_close = np.abs(low - close.shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    
    # Directional Movement
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm < 0] = 0
    plus_dm[(plus_dm < minus_dm)] = 0
    minus_dm[(minus_dm < plus_dm)] = 0
    
    # Smooth using Wilder's method
    atr = tr.ewm(alpha=1/period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr)
    
    # DX and ADX
    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di)
    adx = dx.ewm(alpha=1/period, adjust=False).mean()
    
    df['plus_di'] = plus_di
    df['minus_di'] = minus_di
    df['adx'] = adx
    
    return df

def calculate_pivot_points(high: float, low: float, close: float) -> Dict[str, float]:
    """
    Calculate Floor Pivots and Camarilla levels
    
    Returns dict with PP, R1-R3, S1-S3, H1-H4, L1-L4, TC, BC
    """
    # Floor Pivots
    pp = (high + low + close) / 3
    r1 = 2 * pp - low
    r2 = pp + (high - low)
    r3 = r1 + (high - low)
    s1 = 2 * pp - high
    s2 = pp - (high - low)
    s3 = s1 - (high - low)
    
    # Central Pivot Range
    bc = (high + low) / 2
    tc = (pp - bc) + pp
    
    # Camarilla levels
    h1 = close + (high - low) * 1.1 / 12
    h2 = close + (high - low) * 1.1 / 6
    h3 = close + (high - low) * 1.1 / 4
    h4 = close + (high - low) * 1.1 / 2
    
    l1 = close - (high - low) * 1.1 / 12
    l2 = close - (high - low) * 1.1 / 6
    l3 = close - (high - low) * 1.1 / 4
    l4 = close - (high - low) * 1.1 / 2
    
    return {
        'pp': pp, 'r1': r1, 'r2': r2, 'r3': r3, 's1': s1, 's2': s2, 's3': s3,
        'tc': tc, 'bc': bc,
        'h1': h1, 'h2': h2, 'h3': h3, 'h4': h4,
        'l1': l1, 'l2': l2, 'l3': l3, 'l4': l4
    }

def calculate_cpr_width(tc: float, bc: float, pp: float) -> float:
    """Calculate CPR width as percentage"""
    cpr_width = abs(tc - bc)
    return (cpr_width / pp) * 100

# ============================================
# DATA FETCHING
# ============================================

def init_supabase() -> Client:
    """Initialize Supabase client"""
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        return supabase
    except Exception as e:
        print(f"❌ Supabase connection failed: {e}")
        return None

def fetch_stock_data_from_supabase(supabase: Client, ticker: str, days: int = 180) -> Optional[pd.DataFrame]:
    """Fetch OHLC data from Supabase daily_stock_snapshots table"""
    try:
        cutoff_date = (datetime.now() - timedelta(days=days)).date()
        
        response = supabase.table('daily_stock_snapshots')\
            .select('*')\
            .eq('ticker', ticker)\
            .gte('snapshot_date', str(cutoff_date))\
            .order('snapshot_date', desc=False)\
            .execute()
        
        if not response.data:
            return None
        
        df = pd.DataFrame(response.data)
        df['snapshot_date'] = pd.to_datetime(df['snapshot_date'])
        df = df.sort_values('snapshot_date').reset_index(drop=True)
        
        return df
        
    except Exception as e:
        return None

def get_stock_name(ticker: str) -> str:
    """Get stock name from yfinance or cache"""
    if ticker in STOCK_NAMES:
        return STOCK_NAMES[ticker]
    
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        name = info.get('longName', ticker.replace('.NS', ''))
        STOCK_NAMES[ticker] = name
        return name
    except:
        return ticker.replace('.NS', '')

# ============================================
# PROMOTER BUYING DETECTION
# ============================================

def get_recent_promoter_transactions() -> List[Dict]:
    """
    Scrape recent promoter transactions from Trendlyne
    Returns list of promoter transactions
    """
    url = 'https://trendlyne.com/insider-trading/insider-trading-trends/2126/promoter/'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            return parse_promoter_transactions(response.text)
        else:
            print(f"  ⚠️ Trendlyne returned status {response.status_code}")
            return []
    except Exception as e:
        print(f"  ❌ Error fetching promoter data: {e}")
        return []

def parse_promoter_transactions(html: str) -> List[Dict]:
    """Parse promoter transactions from Trendlyne HTML"""
    transactions = []
    
    try:
        soup = BeautifulSoup(html, 'html.parser')
        table = soup.find('table')
        
        if not table:
            return []
        
        rows = table.find_all('tr')[1:]  # Skip header
        
        for row in rows:
            cols = row.find_all('td')
            if len(cols) < 6:
                continue
            
            try:
                date_str = cols[0].text.strip()
                company_name = cols[1].text.strip()
                promoter_name = cols[2].text.strip()
                transaction_type = cols[3].text.strip()
                category = cols[4].text.strip()
                
                # Only include promoter transactions (not public/directors)
                if 'promoter' not in category.lower():
                    continue
                
                # Extract symbol from company name or link
                symbol_link = cols[1].find('a')
                if symbol_link and 'href' in symbol_link.attrs:
                    href = symbol_link['href']
                    # Extract symbol from URL like /equity/TATAMOTORS/
                    parts = href.split('/')
                    symbol = parts[2] if len(parts) > 2 else company_name
                else:
                    symbol = company_name
                
                transactions.append({
                    'date': date_str,
                    'company_name': company_name,
                    'symbol': symbol.upper(),
                    'promoter_name': promoter_name,
                    'transaction_type': transaction_type,
                    'category': category
                })
            except Exception as e:
                continue
        
        return transactions
        
    except Exception as e:
        print(f"  ❌ Error parsing promoter data: {e}")
        return []

def scan_promoter_buying_nifty500(supabase: Client, nifty_500_tickers: List[str]) -> List[Dict]:
    """
    Scan for promoter buying in Nifty 500 stocks
    Returns list of promoter buying signals
    """
    print("\n📊 Scanning for Promoter Buying...")
    
    # Get all recent promoter transactions
    all_transactions = get_recent_promoter_transactions()
    
    if not all_transactions:
        print("  ⚠️ No promoter transactions found")
        return []
    
    print(f"  ✅ Fetched {len(all_transactions)} total promoter transactions")
    
    # Get Nifty 500 symbols (without .NS)
    nifty500_symbols = set([ticker.replace('.NS', '').upper() for ticker in nifty_500_tickers])
    
    # Filter for Nifty 500 stocks only
    nifty_transactions = [
        t for t in all_transactions 
        if t['symbol'].upper() in nifty500_symbols
    ]
    
    print(f"  ✅ Found {len(nifty_transactions)} promoter transactions in Nifty 500")
    
    signals = []
    
    for txn in nifty_transactions:
        # Determine if it's buying or selling
        is_buying = 'acquisition' in txn['transaction_type'].lower() or 'purchase' in txn['transaction_type'].lower()
        
        if not is_buying:
            continue  # Only track buying, not selling
        
        ticker = txn['symbol'].upper() + '.NS'
        
        # Get stock name
        stock_name = get_stock_name(ticker)
        
        # Get latest price from database
        try:
            response = supabase.table('daily_stock_snapshots')\
                .select('close')\
                .eq('ticker', ticker)\
                .order('snapshot_date', desc=True)\
                .limit(1)\
                .execute()
            
            current_price = response.data[0]['close'] if response.data else None
        except:
            current_price = None
        
        signals.append({
            'ticker': ticker,
            'signal_type': 'promoter_buying',
            'signal_strength': 'BUY',
            'stock_name': stock_name,
            'price': current_price,
            'promoter_name': txn['promoter_name'],
            'transaction_type': txn['transaction_type'],
            'transaction_date': txn['date'],
            'detected_at': datetime.now().isoformat(),
            'notes': f"Promoter: {txn['promoter_name']} | Type: {txn['transaction_type']}"
        })
    
    print(f"  ✅ Generated {len(signals)} promoter buying signals")
    
    return signals

# ============================================
# SIGNAL DETECTION FUNCTIONS
# ============================================

def check_narrow_cpr_signal(
    daily_df: pd.DataFrame,
    current_price: float
) -> Optional[Dict]:
    """
    Check for Narrow CPR breakaway signal
    
    CRITERIA:
    - CPR width < 0.3%
    - Price within 0.5% of L3
    - Volume > 1.2x average
    """
    if len(daily_df) < 20:
        return None
    
    try:
        yesterday = daily_df.iloc[-2]
        latest = daily_df.iloc[-1]
        
        # Calculate yesterday's pivots
        pivots = calculate_pivot_points(
            yesterday['high'],
            yesterday['low'],
            yesterday['close']
        )
        
        # Check CPR width
        cpr_width = calculate_cpr_width(pivots['tc'], pivots['bc'], pivots['pp'])
        if cpr_width >= 0.3:  # Must be very narrow
            return None
        
        # Check if price near L3
        distance_to_l3 = abs(current_price - pivots['l3']) / pivots['l3'] * 100
        if distance_to_l3 > 0.5:  # Within 0.5% of L3
            return None
        
        # Volume check
        avg_volume = daily_df['volume'].tail(20).mean()
        current_volume = latest['volume']
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0
        
        if volume_ratio < 1.2:  # At least 1.2x average volume
            return None
        
        return {
            'signal_type': 'narrow_cpr_breakaway',
            'signal_strength': 'strong',
            'cpr_width': round(cpr_width, 3),
            'at_l3': True,
            'distance_to_level': round(distance_to_l3, 2),
            'volume_ratio': round(volume_ratio, 2)
        }
        
    except Exception as e:
        return None

def check_blue_zone_signal(
    daily_df: pd.DataFrame,
    current_price: float
) -> Optional[Dict]:
    """
    Check for Blue Zone signal (Strong or Buy)
    
    CRITERIA:
    Strong Buy:
    - Daily RSI EMA(9) >= 75
    - Weekly RSI EMA(9) >= 70
    - Within 10% of 52W high
    - Above EMA 50
    - Volume > 1.5x average (REQUIRED for Strong Buy)
    
    Buy:
    - Daily RSI EMA(9) >= 70
    - Weekly RSI EMA(9) >= 60
    - Within 10% of 52W high
    - Above EMA 20
    - No volume requirement
    """
    if len(daily_df) < 50:
        return None
    
    try:
        latest = daily_df.iloc[-1]
        
        # Get RSI values from Supabase (pre-calculated)
        daily_rsi_ema_9 = latest.get('rsi_ema_9')
        weekly_rsi_ema_9 = latest.get('weekly_rsi_ema_9')
        ema_20 = latest.get('ema_20')
        ema_50 = latest.get('ema_50')
        
        # Check if RSI data exists
        if pd.isna(daily_rsi_ema_9) or pd.isna(weekly_rsi_ema_9):
            return None
        
        # Calculate 52W high
        high_52w = daily_df['high'].tail(252).max()
        distance_from_52w_high = ((current_price - high_52w) / high_52w) * 100
        
        # Check within 10% of 52W high
        if distance_from_52w_high < -10:
            return None
        
        # Calculate volume ratio
        avg_volume = daily_df['volume'].tail(20).mean()
        current_volume = latest['volume']
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0
        
        # Determine signal strength
        above_ema_50 = current_price > ema_50 if pd.notna(ema_50) else False
        above_ema_20 = current_price > ema_20 if pd.notna(ema_20) else False
        
        # STRONG BUY
        if (daily_rsi_ema_9 >= 75 and 
            weekly_rsi_ema_9 >= 70 and 
            distance_from_52w_high >= -10 and
            above_ema_50 and
            volume_ratio > 1.5):
            signal_strength = 'strong'
            signal_type = 'blue_zone_strong'
        # BUY
        elif (daily_rsi_ema_9 >= 70 and 
              weekly_rsi_ema_9 >= 60 and 
              distance_from_52w_high >= -10 and
              above_ema_20):
            signal_strength = 'regular'
            signal_type = 'blue_zone_buy'
        else:
            return None
        
        return {
            'signal_type': signal_type,
            'signal_strength': signal_strength,
            'daily_rsi_ema_9': round(daily_rsi_ema_9, 2),
            'weekly_rsi_ema_9': round(weekly_rsi_ema_9, 2),
            'distance_from_52w_high': round(distance_from_52w_high, 2),
            'volume_ratio': round(volume_ratio, 2),
            'above_ema_50': above_ema_50,
            'above_ema_20': above_ema_20
        }
        
    except Exception as e:
        return None

def check_golden_cross(daily_df: pd.DataFrame, current_price: float) -> Optional[Dict]:
    """
    Check for Golden Cross signal (20 EMA crossing above 50 EMA)
    
    UPDATED CRITERIA (with ADX):
    Strong Buy:
    - Crossover 1-3 days old
    - Price > both 50 EMA AND 200 EMA
    - Volume > 2x (20-day average)
    - Close > 2% above 50 EMA
    - RSI > 55
    - Both 20 & 50 EMAs rising
    - ADX > 25 AND +DI > -DI (NEW - trend confirmation)
    
    Buy:
    - Crossover 4-7 days old
    - Price > 50 EMA (200 EMA optional)
    - Volume > 1.5x (20-day average)
    - Close > 1% above 50 EMA
    - RSI > 45
    - At least 20 EMA rising
    - No ADX requirement
    """
    if len(daily_df) < 50:
        return None
    
    try:
        # Calculate ADX for trend confirmation
        daily_df = calculate_adx(daily_df)
        
        latest = daily_df.iloc[-1]
        
        # Get EMA and RSI values from Supabase (pre-calculated)
        ema_20 = latest.get('ema_20')
        ema_50 = latest.get('ema_50')
        ema_200 = latest.get('ema_200')
        rsi_14 = latest.get('rsi_14')
        
        # Get ADX values
        adx_value = latest['adx']
        plus_di = latest['plus_di']
        minus_di = latest['minus_di']
        
        # Check if required data exists
        if pd.isna(ema_20) or pd.isna(ema_50) or pd.isna(rsi_14):
            return None
        
        # Find the crossover day (last 7 days)
        crossover_day = None
        days_since_crossover = None
        
        for i in range(1, min(8, len(daily_df))):
            prev_day = daily_df.iloc[-(i+1)]
            curr_day = daily_df.iloc[-i]
            
            prev_ema_20 = prev_day.get('ema_20')
            prev_ema_50 = prev_day.get('ema_50')
            curr_ema_20 = curr_day.get('ema_20')
            curr_ema_50 = curr_day.get('ema_50')
            
            # Skip if data missing
            if pd.isna(prev_ema_20) or pd.isna(prev_ema_50) or pd.isna(curr_ema_20) or pd.isna(curr_ema_50):
                continue
            
            # Check for crossover
            if prev_ema_20 <= prev_ema_50 and curr_ema_20 > curr_ema_50:
                crossover_day = i
                days_since_crossover = i
                break
        
        # No crossover found
        if crossover_day is None:
            return None
        
        # Price must be above 50 EMA
        if current_price <= ema_50:
            return None
        
        # Calculate metrics
        pct_above_50_ema = ((current_price - ema_50) / ema_50) * 100
        
        # Volume check
        avg_volume = daily_df['volume'].tail(20).mean()
        current_volume = latest['volume']
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0
        
        # Check if EMAs are rising
        ema_20_5d_ago = daily_df.iloc[-6].get('ema_20') if len(daily_df) >= 6 else None
        ema_50_10d_ago = daily_df.iloc[-11].get('ema_50') if len(daily_df) >= 11 else None
        
        ema_20_rising = ema_20 > ema_20_5d_ago if pd.notna(ema_20_5d_ago) else False
        ema_50_rising = ema_50 > ema_50_10d_ago if pd.notna(ema_50_10d_ago) else False
        
        # Check position relative to 200 EMA
        above_200_ema = current_price > ema_200 if pd.notna(ema_200) else False
        
        # Check ADX trending
        adx_trending = adx_value > 25
        adx_uptrend = plus_di > minus_di
        
        # Determine signal strength
        # STRONG BUY criteria (NOW REQUIRES ADX)
        if (days_since_crossover <= 3 and
            above_200_ema and
            volume_ratio > 2.0 and
            pct_above_50_ema > 2.0 and
            rsi_14 > 55 and
            ema_20_rising and
            ema_50_rising and
            adx_trending and  # NEW
            adx_uptrend):     # NEW
            signal_strength = 'strong'
            signal_type = 'golden_cross_strong'
        
        # BUY criteria (no ADX requirement)
        elif (days_since_crossover <= 7 and
              volume_ratio > 1.5 and
              pct_above_50_ema > 1.0 and
              rsi_14 > 45 and
              ema_20_rising):
            signal_strength = 'regular'
            signal_type = 'golden_cross_buy'
        
        else:
            return None
        
        return {
            'signal_type': signal_type,
            'signal_strength': signal_strength,
            'days_since_crossover': days_since_crossover,
            'pct_above_50_ema': round(pct_above_50_ema, 2),
            'above_200_ema': above_200_ema,
            'volume_ratio': round(volume_ratio, 2),
            'rsi_14': round(rsi_14, 2),
            'ema_20_rising': ema_20_rising,
            'ema_50_rising': ema_50_rising,
            'ema_20': round(ema_20, 2),
            'ema_50': round(ema_50, 2),
            'ema_200': round(ema_200, 2) if pd.notna(ema_200) else None,
            'adx': round(float(adx_value), 2),  # NEW
            'plus_di': round(float(plus_di), 2),  # NEW
            'minus_di': round(float(minus_di), 2),  # NEW
            'adx_trending': adx_trending  # NEW
        }
        
    except Exception as e:
        return None

def check_macd_signal(daily_df: pd.DataFrame, current_price: float) -> Optional[Dict]:
    """
    Check for MACD bullish crossover signal
    
    CRITERIA:
    Strong Buy:
    - Crossover 1-3 days old
    - Histogram expanding
    - Both MACD and signal > 0 (bullish zone)
    - Price > 200 EMA
    - ADX > 25 AND +DI > -DI (trending)
    
    Buy:
    - Crossover 4-5 days old
    - Histogram positive
    - Price > 50 EMA
    - No ADX requirement
    """
    if len(daily_df) < 50:
        return None
    
    try:
        # Calculate MACD and ADX
        daily_df = calculate_macd(daily_df)
        daily_df = calculate_adx(daily_df)
        
        latest = daily_df.iloc[-1]
        
        # Get EMAs from Supabase
        ema_50 = latest.get('ema_50')
        ema_200 = latest.get('ema_200')
        
        # Find crossover in last 5 days
        crossover_day = None
        days_since_crossover = None
        
        for i in range(1, min(6, len(daily_df))):
            prev_day = daily_df.iloc[-(i+1)]
            curr_day = daily_df.iloc[-i]
            
            prev_macd = prev_day['macd_line']
            prev_signal = prev_day['macd_signal']
            curr_macd = curr_day['macd_line']
            curr_signal = curr_day['macd_signal']
            
            # Check for bullish crossover
            if prev_macd <= prev_signal and curr_macd > curr_signal:
                crossover_day = i
                days_since_crossover = i
                break
        
        if crossover_day is None:
            return None
        
        # Get current values
        macd_line = latest['macd_line']
        signal_line = latest['macd_signal']
        histogram = latest['macd_histogram']
        adx_value = latest['adx']
        plus_di = latest['plus_di']
        minus_di = latest['minus_di']
        
        # Check histogram expanding
        histogram_yesterday = daily_df.iloc[-2]['macd_histogram']
        histogram_expanding = histogram > histogram_yesterday
        
        # Check if in bullish zone
        in_bullish_zone = macd_line > 0 and signal_line > 0
        
        # Check price position
        above_200_ema = current_price > ema_200 if pd.notna(ema_200) else False
        above_50_ema = current_price > ema_50 if pd.notna(ema_50) else False
        
        # Check ADX trending
        adx_trending = adx_value > 25
        adx_uptrend = plus_di > minus_di
        
        # Determine signal strength
        if (days_since_crossover <= 3 and
            histogram_expanding and
            in_bullish_zone and
            above_200_ema and
            adx_trending and
            adx_uptrend):
            signal_strength = 'strong'
            signal_type = 'macd_strong'
        elif (days_since_crossover <= 5 and
              histogram > 0 and
              above_50_ema):
            signal_strength = 'regular'
            signal_type = 'macd_buy'
        else:
            return None
        
        return {
            'signal_type': signal_type,
            'signal_strength': signal_strength,
            'days_since_crossover': days_since_crossover,
            'macd_line': round(float(macd_line), 3),
            'signal_line': round(float(signal_line), 3),
            'histogram': round(float(histogram), 3),
            'histogram_expanding': histogram_expanding,
            'in_bullish_zone': in_bullish_zone,
            'adx': round(float(adx_value), 2),
            'plus_di': round(float(plus_di), 2),
            'minus_di': round(float(minus_di), 2),
            'adx_trending': adx_trending,
            'above_200_ema': above_200_ema,
            'above_50_ema': above_50_ema
        }
        
    except Exception as e:
        return None

def check_200_ema_retest(daily_df: pd.DataFrame, current_price: float) -> Optional[Dict]:
    """
    Check for 200 EMA Retest signal
    
    UPDATED CRITERIA (High Quality):
    - Price within 2% of 200 EMA (close retest, not floating)
    - Stock was 10%+ above 200 EMA in last 20 days (recent strong trend)
    - 50 EMA > 3% above 200 EMA (clear trend separation - Minervini style)
    - 50 EMA rising (momentum intact)
    - Volume > 1.3x (bounce confirmation)
    - RSI > 40 (orderly pullback, not panic)
    """
    if len(daily_df) < 60:
        return None
    
    try:
        latest = daily_df.iloc[-1]
        
        # Get EMA values
        ema_50 = latest.get('ema_50')
        ema_200 = latest.get('ema_200')
        rsi_14 = latest.get('rsi_14')
        
        if pd.isna(ema_50) or pd.isna(ema_200) or pd.isna(rsi_14):
            return None
        
        # Check if 50 EMA above 200 EMA (trend intact)
        if ema_50 <= ema_200:
            return None
        
        # NEW: Check 50 EMA > 3% above 200 EMA (clear trend separation)
        ema_separation = ((ema_50 - ema_200) / ema_200) * 100
        if ema_separation < 3.0:
            return None
        
        # TIGHTENED: Check if price near 200 EMA (within 2%, was 5%)
        distance_from_200ema = ((current_price - ema_200) / ema_200) * 100
        if distance_from_200ema < -2 or distance_from_200ema > 2:
            return None
        
        # Check if 50 EMA rising
        ema_50_10d_ago = daily_df.iloc[-11].get('ema_50') if len(daily_df) >= 11 else None
        ema_50_rising = ema_50 > ema_50_10d_ago if pd.notna(ema_50_10d_ago) else False
        
        if not ema_50_rising:
            return None
        
        # TIGHTENED: Check recent strength (10%+ above 200 EMA in last 20 days, was 5% in 30 days)
        last_20_days = daily_df.tail(20)
        ema_200_values = last_20_days['ema_200']
        close_values = last_20_days['close']
        
        # Calculate max distance above 200 EMA in last 20 days
        distances = ((close_values - ema_200_values) / ema_200_values * 100).dropna()
        peak_above_200ema_20d = distances.max() if len(distances) > 0 else 0
        
        if peak_above_200ema_20d < 10:  # Wasn't recently strong enough
            return None
        
        # NEW: Volume confirmation (bounce with volume)
        avg_volume = daily_df['volume'].tail(20).mean()
        current_volume = latest['volume']
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0
        
        if volume_ratio < 1.3:  # Need volume confirmation
            return None
        
        # NEW: RSI check (orderly pullback, not panic)
        if rsi_14 < 40:
            return None
        
        return {
            'signal_type': '200_ema_retest',
            'signal_strength': 'regular',
            'distance_from_200ema': round(distance_from_200ema, 2),
            'ema_separation': round(ema_separation, 2),
            'ema_50_rising': ema_50_rising,
            'peak_above_200ema_20d': round(peak_above_200ema_20d, 2),
            'volume_ratio': round(volume_ratio, 2),
            'rsi_14': round(rsi_14, 2),
            'ema_50': round(ema_50, 2),
            'ema_200': round(ema_200, 2)
        }
        
    except Exception as e:
        return None

def check_200_ema_recovery_breakout(daily_df: pd.DataFrame, current_price: float) -> Optional[Dict]:
    """
    Check for 200 EMA Recovery Breakout signal (Stage 1 -> Stage 2 transition)
    
    Detects stocks breaking out AFTER consolidating below 200 EMA (Minervini-style base breakout)
    
    CRITERIA:
    - Stock was below 200 EMA for 15-40 days (consolidation period)
    - Stock was previously strong (10%+ above 200 EMA before consolidation)
    - 50 EMA still above 200 EMA (trend not broken)
    - Price closes above 200 EMA (breakout)
    - Volume > 2x (strong breakout confirmation)
    - ADX > 20 (trend forming)
    - RSI > 50 (momentum positive)
    - 50 EMA turning up (momentum building)
    - Consolidation range < 10% (tight base, not wild swings)
    """
    if len(daily_df) < 60:
        return None
    
    try:
        latest = daily_df.iloc[-1]
        
        # Get EMA and indicator values
        ema_50 = latest.get('ema_50')
        ema_200 = latest.get('ema_200')
        rsi_14 = latest.get('rsi_14')
        
        if pd.isna(ema_50) or pd.isna(ema_200) or pd.isna(rsi_14):
            return None
        
        # Calculate ADX for trend confirmation
        daily_df = calculate_adx(daily_df)
        adx_value = latest['adx']
        
        # Check if price just crossed above 200 EMA (breakout day)
        if current_price <= ema_200:
            return None
        
        # Look back to find consolidation period below 200 EMA
        days_below_200ema = 0
        consolidation_start_idx = None
        consolidation_highs = []
        consolidation_lows = []
        
        # Count consecutive days below 200 EMA before today
        for i in range(len(daily_df) - 2, max(0, len(daily_df) - 50), -1):
            row = daily_df.iloc[i]
            row_ema_200 = row.get('ema_200')
            
            if pd.isna(row_ema_200):
                break
            
            if row['close'] < row_ema_200:
                days_below_200ema += 1
                consolidation_highs.append(row['high'])
                consolidation_lows.append(row['low'])
                if consolidation_start_idx is None:
                    consolidation_start_idx = i
            else:
                break  # Stop when we hit a day above 200 EMA
        
        # Check if consolidation duration is in range (15-40 days)
        if days_below_200ema < 15 or days_below_200ema > 40:
            return None
        
        # Check consolidation range (must be tight, < 10%)
        if consolidation_highs and consolidation_lows:
            consolidation_range = ((max(consolidation_highs) - min(consolidation_lows)) / min(consolidation_lows)) * 100
            if consolidation_range > 10:
                return None  # Too wide, not a tight base
        else:
            return None
        
        # Check if 50 EMA still above 200 EMA (trend not broken during consolidation)
        if ema_50 <= ema_200:
            return None
        
        # Check if stock was previously strong (10%+ above 200 EMA before consolidation)
        if consolidation_start_idx and consolidation_start_idx > 20:
            pre_consolidation_days = daily_df.iloc[consolidation_start_idx - 20:consolidation_start_idx]
            pre_consolidation_ema_200 = pre_consolidation_days['ema_200']
            pre_consolidation_closes = pre_consolidation_days['close']
            
            distances_before = ((pre_consolidation_closes - pre_consolidation_ema_200) / pre_consolidation_ema_200 * 100).dropna()
            peak_before_consolidation = distances_before.max() if len(distances_before) > 0 else 0
            
            if peak_before_consolidation < 10:
                return None  # Wasn't strong enough before consolidation
        else:
            return None
        
        # Check if 50 EMA is turning up (momentum building)
        ema_50_5d_ago = daily_df.iloc[-6].get('ema_50') if len(daily_df) >= 6 else None
        ema_50_turning_up = ema_50 > ema_50_5d_ago if pd.notna(ema_50_5d_ago) else False
        
        if not ema_50_turning_up:
            return None
        
        # Volume confirmation (strong breakout)
        avg_volume = daily_df['volume'].tail(20).mean()
        current_volume = latest['volume']
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0
        
        if volume_ratio < 2.0:  # Need strong volume (2x, higher than retest)
            return None
        
        # ADX check (trend forming)
        if adx_value < 20:
            return None
        
        # RSI check (positive momentum, not weak bounce)
        if rsi_14 < 50:
            return None
        
        # Calculate how close we are to breaking out (distance from 200 EMA on breakout day)
        distance_from_200ema = ((current_price - ema_200) / ema_200) * 100
        
        return {
            'signal_type': '200_ema_recovery',
            'signal_strength': 'regular',
            'days_consolidated': days_below_200ema,
            'consolidation_range': round(consolidation_range, 2),
            'peak_before_consolidation': round(peak_before_consolidation, 2),
            'distance_from_200ema': round(distance_from_200ema, 2),
            'volume_ratio': round(volume_ratio, 2),
            'adx': round(float(adx_value), 2),
            'rsi_14': round(rsi_14, 2),
            'ema_50_turning_up': ema_50_turning_up,
            'ema_50': round(ema_50, 2),
            'ema_200': round(ema_200, 2)
        }
        
    except Exception as e:
        return None

# ============================================
# MAIN SCANNING LOGIC
# ============================================

def scan_stock(supabase: Client, ticker: str) -> List[Dict]:
    """
    Scan a single stock for ALL signals
    Returns list of signals found
    """
    try:
        # Fetch data
        daily_df = fetch_stock_data_from_supabase(supabase, ticker, days=365)
        
        if daily_df is None or len(daily_df) < 60:
            return []
        
        current_price = daily_df.iloc[-1]['close']
        stock_name = get_stock_name(ticker)
        
        signals = []
        
        # Check all signal types
        cpr_signal = check_narrow_cpr_signal(daily_df, current_price)
        if cpr_signal:
            cpr_signal['ticker'] = ticker
            cpr_signal['stock_name'] = stock_name
            cpr_signal['price'] = round(current_price, 2)
            signals.append(cpr_signal)
        
        blue_zone_signal = check_blue_zone_signal(daily_df, current_price)
        if blue_zone_signal:
            blue_zone_signal['ticker'] = ticker
            blue_zone_signal['stock_name'] = stock_name
            blue_zone_signal['price'] = round(current_price, 2)
            signals.append(blue_zone_signal)
        
        golden_cross_signal = check_golden_cross(daily_df, current_price)
        if golden_cross_signal:
            golden_cross_signal['ticker'] = ticker
            golden_cross_signal['stock_name'] = stock_name
            golden_cross_signal['price'] = round(current_price, 2)
            signals.append(golden_cross_signal)
        
        macd_signal = check_macd_signal(daily_df, current_price)
        if macd_signal:
            macd_signal['ticker'] = ticker
            macd_signal['stock_name'] = stock_name
            macd_signal['price'] = round(current_price, 2)
            signals.append(macd_signal)
        
        ema200_signal = check_200_ema_retest(daily_df, current_price)
        if ema200_signal:
            ema200_signal['ticker'] = ticker
            ema200_signal['stock_name'] = stock_name
            ema200_signal['price'] = round(current_price, 2)
            signals.append(ema200_signal)
        
        # NEW: Check for 200 EMA Recovery Breakout (if retest didn't trigger)
        if not ema200_signal:  # Only check recovery if retest didn't fire (avoid duplicate signals)
            recovery_signal = check_200_ema_recovery_breakout(daily_df, current_price)
            if recovery_signal:
                recovery_signal['ticker'] = ticker
                recovery_signal['stock_name'] = stock_name
                recovery_signal['price'] = round(current_price, 2)
                signals.append(recovery_signal)
        
        return signals
        
    except Exception as e:
        return []

def save_signals_to_supabase(supabase: Client, signals: List[Dict]) -> bool:
    """Save signals to entry_signals table"""
    try:
        if not signals:
            print("No signals to save")
            return True
        
        today = str(datetime.now().date())
        
        # Delete today's signals first (fresh start)
        print(f"🗑️  Deleting existing signals for {today}...")
        supabase.table('entry_signals').delete().eq('alert_date', today).execute()
        
        # Prepare records
        records = []
        for signal in signals:
            ticker = signal.pop('ticker')
            stock_name = signal.pop('stock_name')
            price = signal.pop('price')
            signal_type = signal.pop('signal_type')
            signal_strength = signal.pop('signal_strength')
            
            # Convert numpy types
            details = {}
            for key, value in signal.items():
                if isinstance(value, bool):
                    details[key] = value
                elif isinstance(value, (np.bool_, np.integer, np.floating)):
                    details[key] = value.item()
                elif pd.isna(value):
                    details[key] = None
                else:
                    details[key] = value
            
            record = {
                'ticker': ticker,
                'stock_name': stock_name,
                'signal_type': signal_type,
                'signal_strength': signal_strength,
                'price': price,
                'alert_date': today,
                'details': details
            }
            records.append(record)
        
        # Insert signals
        print(f"💾 Inserting {len(records)} signals...")
        response = supabase.table('entry_signals').insert(records).execute()
        
        print(f"✅ Successfully saved {len(records)} signals")
        return True
        
    except Exception as e:
        print(f"❌ Failed to save signals: {e}")
        return False

def main():
    """Main scanner function"""
    print("=" * 80)
    print("🔍 ENTRY SIGNALS SCANNER")
    print("=" * 80)
    print(f"Scanning: Narrow CPR, Blue Zone, Golden Cross, MACD, 200 EMA Retest, Promoter Buying")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Initialize Supabase
    supabase = init_supabase()
    if not supabase:
        print("\n❌ Cannot proceed without Supabase connection")
        return
    
    # Get ticker list
    print("\n📋 Fetching ticker list...")
    
    latest_response = supabase.table('daily_stock_snapshots')\
        .select('snapshot_date')\
        .order('snapshot_date', desc=True)\
        .limit(1)\
        .execute()
    
    if not latest_response.data:
        print("❌ No data found in database")
        return
    
    latest_date = latest_response.data[0]['snapshot_date']
    print(f"  Using latest date: {latest_date}")
    
    # Fetch all tickers
    all_tickers = []
    offset = 0
    batch_size = 1000
    
    while True:
        response = supabase.table('daily_stock_snapshots')\
            .select('ticker')\
            .eq('snapshot_date', latest_date)\
            .range(offset, offset + batch_size - 1)\
            .execute()
        
        if not response.data:
            break
        
        batch_tickers = [r['ticker'] for r in response.data]
        all_tickers.extend(batch_tickers)
        
        print(f"  Batch {offset//batch_size + 1}: Found {len(batch_tickers)} tickers")
        
        if len(response.data) < batch_size:
            break
        
        offset += batch_size
    
    tickers = sorted(list(set(all_tickers)))
    print(f"✅ Found {len(tickers)} unique stocks")
    
    # Scan for signals
    print(f"\n🔍 Scanning for entry signals...")
    print("=" * 80)
    
    all_signals = []
    signal_counts = {
        'narrow_cpr_breakaway': 0,
        'blue_zone_strong': 0,
        'blue_zone_buy': 0,
        'golden_cross_strong': 0,
        'golden_cross_buy': 0,
        'macd_strong': 0,
        'macd_buy': 0,
        '200_ema_retest': 0,
        '200_ema_recovery': 0,
        'promoter_buying': 0
    }
    
    for i, ticker in enumerate(tickers, 1):
        if i % 50 == 0:
            print(f"\n[{i}/{len(tickers)}] Progress: {(i/len(tickers)*100):.1f}%")
            print(f"  Signals: CPR={signal_counts['narrow_cpr_breakaway']}, "
                  f"BZ={signal_counts['blue_zone_strong']+signal_counts['blue_zone_buy']}, "
                  f"GC={signal_counts['golden_cross_strong']+signal_counts['golden_cross_buy']}, "
                  f"MACD={signal_counts['macd_strong']+signal_counts['macd_buy']}, "
                  f"200EMA={signal_counts['200_ema_retest']+signal_counts['200_ema_recovery']}")
        
        signals = scan_stock(supabase, ticker)
        
        for signal in signals:
            all_signals.append(signal)
            signal_type = signal['signal_type']
            signal_counts[signal_type] += 1
            
            # Print signal
            emoji = {
                'narrow_cpr_breakaway': '📍',
                'blue_zone_strong': '🟢🟢',
                'blue_zone_buy': '🟢',
                'golden_cross_strong': '⚡⚡',
                'golden_cross_buy': '⚡',
                'macd_strong': '📊📊',
                'macd_buy': '📊',
                '200_ema_retest': '📈',
                '200_ema_recovery': '🚀',
                'promoter_buying': '🏦'
            }
            
            label = {
                'narrow_cpr_breakaway': 'CPR',
                'blue_zone_strong': 'BLUE ZONE STRONG',
                'blue_zone_buy': 'BLUE ZONE BUY',
                'golden_cross_strong': 'GC STRONG',
                'golden_cross_buy': 'GC BUY',
                'macd_strong': 'MACD STRONG',
                'macd_buy': 'MACD BUY',
                '200_ema_retest': '200 EMA RETEST',
                '200_ema_recovery': '200 EMA RECOVERY',
                'promoter_buying': 'PROMOTER BUYING'
            }
            
            print(f"  {emoji[signal_type]} {ticker:20} - {label[signal_type]}")
        
        time.sleep(0.1)  # Rate limiting
    
    # Scan for promoter buying (separate from per-stock scan)
    promoter_signals = scan_promoter_buying_nifty500(supabase, tickers)
    for signal in promoter_signals:
        all_signals.append(signal)
        signal_counts['promoter_buying'] += 1
        print(f"  🏦 {signal['ticker']:20} - PROMOTER BUYING ({signal['promoter_name']})")
    
    # Save signals
    if all_signals:
        print(f"\n💾 Saving {len(all_signals)} signals to database...")
        save_signals_to_supabase(supabase, all_signals)
    else:
        print(f"\n⚪ No signals found")
    
    # Print summary
    print("\n" + "=" * 80)
    print("📈 SUMMARY")
    print("=" * 80)
    print(f"✅ Stocks scanned: {len(tickers)}")
    print(f"\n📊 Signals Found:")
    print(f"  📍 Narrow CPR: {signal_counts['narrow_cpr_breakaway']}")
    print(f"  🟢 Blue Zone: {signal_counts['blue_zone_strong']} Strong + {signal_counts['blue_zone_buy']} Buy")
    print(f"  ⚡ Golden Cross: {signal_counts['golden_cross_strong']} Strong + {signal_counts['golden_cross_buy']} Buy")
    print(f"  📊 MACD: {signal_counts['macd_strong']} Strong + {signal_counts['macd_buy']} Buy")
    print(f"  📈 200 EMA: {signal_counts['200_ema_retest']} Retest + {signal_counts['200_ema_recovery']} Recovery")
    print(f"  🏦 Promoter Buying: {signal_counts['promoter_buying']}")
    print(f"  📍 Total: {len(all_signals)}")
    
    print("\n" + "=" * 80)
    print(f"✅ DONE! Completed at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

if __name__ == "__main__":
    main()
