"""
Market Scanner for Nifty 500 Stocks
Scans all Nifty 500 stocks for technical alerts:
- Blue Zone (momentum + position)
- Volume Breakout (2x average + 3% price move)
- EMA Crossovers (20/50)
"""

import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from supabase import create_client, Client
import json

# ================== SUPABASE SETUP ==================
SUPABASE_URL = 'https://hcgyncghmcvylnrmcivj.supabase.co'
SUPABASE_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhjZ3luY2dobWN2eWxucm1jaXZqIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTc1MTQwMTEsImV4cCI6MjA3MzA5MDAxMX0.n8vFVCJe1y_3o8fpAY0IgasZ4eKl7DAogEM3OlHB8Ww'

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ================== CONFIGURATION ==================

# Volume breakout config
VOLUME_CONFIG = {
    'multiplier': 2.0,  # Volume must be 2x average
    'avg_period': 20,   # 20-day average volume
    'min_price_change': 3.0,  # Price must move 3% (changed from 0.5%)
}

# EMA config
EMA_CONFIG = {
    'fast': 20,
    'slow': 50,
    'lookback_days': 5,
}

# Blue Zone config
BLUE_ZONE_CONFIG = {
    'rsi_period': 14,
    'rsi_ema_period': 9,
    'rsi_threshold': 70,
    'atr_period': 14,
    'datr_multiplier': 1.5,
}

# 200 EMA Breakout config (relaxed - Option B)
BREAKOUT_200_CONFIG = {
    'consolidation_days': 20,  # Days stock must stay below 200 EMA
    'range_pct_min': 3,  # Minimum consolidation range
    'range_pct_max': 15,  # Maximum consolidation range
    'volume_multiplier': 1.5,  # Volume must be 1.5x average
    'breakout_min_pct': 0.5,  # Minimum breakout percentage
}

# 200 EMA Retest config
RETEST_200_CONFIG = {
    'ema_period': 200,
    'proximity_pct': 2,  # Within 2% of 200 EMA
    'volume_multiplier': 1.5,  # Volume must be 1.5x average
    'min_bounce_pct': 0.5,  # Minimum bounce percentage
}

# ================== HELPER FUNCTIONS ==================

def calculate_rsi(prices, period=14):
    """Calculate RSI"""
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calculate_ema(values, period):
    """Calculate EMA"""
    return values.ewm(span=period, adjust=False).mean()

def calculate_atr(high, low, close, period=14):
    """Calculate ATR"""
    tr1 = high - low
    tr2 = abs(high - close.shift())
    tr3 = abs(low - close.shift())
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()
    return atr

# ================== SCANNER FUNCTIONS ==================

def scan_blue_zone(ticker, name):
    """Scan for Blue Zone setup"""
    try:
        yf_ticker = yf.Ticker(f"{ticker}.NS")
        hist = yf_ticker.history(period='1y')
        
        if hist.empty or len(hist) < 180:
            return None
        
        # Calculate indicators
        hist['rsi'] = calculate_rsi(hist['Close'], BLUE_ZONE_CONFIG['rsi_period'])
        hist['rsi_ema'] = calculate_ema(hist['rsi'], BLUE_ZONE_CONFIG['rsi_ema_period'])
        hist['atr'] = calculate_atr(hist['High'], hist['Low'], hist['Close'], BLUE_ZONE_CONFIG['atr_period'])
        
        # Calculate 52W high with adaptive min_periods to avoid NaN
        # Use up to 252 days but require at least 70% of available data (minimum 120 days)
        lookback_days = min(len(hist), 252)
        min_periods_needed = min(int(lookback_days * 0.7), 120)
        
        if len(hist) < 120:  # Need at least ~6 months of data
            return None
            
        hist['high_52w'] = hist['High'].rolling(window=lookback_days, min_periods=min_periods_needed).max()
        hist['volume_avg'] = hist['Volume'].rolling(window=VOLUME_CONFIG['avg_period']).mean()
        
        # Get current values
        current_price = hist['Close'].iloc[-1]
        current_rsi_ema = hist['rsi_ema'].iloc[-1]
        current_atr = hist['atr'].iloc[-1]
        high_52w = hist['high_52w'].iloc[-1]
        current_volume = hist['Volume'].iloc[-1]
        avg_volume = hist['volume_avg'].iloc[-1]
        
        if pd.isna([current_rsi_ema, current_atr, high_52w]).any():
            return None
        
        # Check conditions
        distance_from_high = high_52w - current_price
        datr_distance = distance_from_high / current_atr
        
        condition_1 = current_rsi_ema > BLUE_ZONE_CONFIG['rsi_threshold']
        condition_2 = datr_distance <= BLUE_ZONE_CONFIG['datr_multiplier']
        
        if not (condition_1 and condition_2):
            return None
        
        # Count consecutive trading days in Blue Zone
        days_in_blue_zone = 1  # Start with today
        for i in range(len(hist) - 2, -1, -1):  # Loop backwards from yesterday
            try:
                past_rsi_ema = hist['rsi_ema'].iloc[i]
                past_price = hist['Close'].iloc[i]
                past_high_52w = hist['high_52w'].iloc[i]
                past_atr = hist['atr'].iloc[i]
                
                if pd.isna([past_rsi_ema, past_atr, past_high_52w]).any():
                    break
                
                past_datr = (past_high_52w - past_price) / past_atr
                
                # Check if met Blue Zone conditions
                if past_rsi_ema > BLUE_ZONE_CONFIG['rsi_threshold'] and past_datr <= BLUE_ZONE_CONFIG['datr_multiplier']:
                    days_in_blue_zone += 1
                else:
                    break  # Stop counting when conditions not met
            except Exception as e:
                # Debug: Uncomment to see errors
                # print(f"  ⚠️ Error counting days for {ticker} at index {i}: {e}")
                break  # Stop on any error
        
        # Calculate volume metrics
        pct_from_high = ((high_52w - current_price) / high_52w) * 100
        volume_ratio = float(current_volume / avg_volume) if avg_volume > 0 else 0.0
        has_volume_breakout = volume_ratio >= VOLUME_CONFIG['multiplier']
        
        alert_description = f"Strong momentum (RSI EMA: {current_rsi_ema:.1f}) with {pct_from_high:.1f}% pullback from 52W high • {days_in_blue_zone} days in Blue Zone"
        if has_volume_breakout:
            alert_description += f" + Volume breakout ({volume_ratio:.1f}x) 🔥"
        
        return {
            'ticker': ticker,
            'stock_name': name,
            'alert_type': 'blue_zone_stocks',
            'alert_category': 'BULLISH',
            'alert_title': f"{name} - Blue Zone Stock",
            'alert_description': alert_description,
            'price': round(float(current_price), 2),
            'alert_date': hist.index[-1].date().isoformat(),
            'details': {
                'rsi_ema_9': round(float(current_rsi_ema), 2),
                'rsi_threshold': BLUE_ZONE_CONFIG['rsi_threshold'],
                'current_price': round(float(current_price), 2),
                'high_52w': round(float(high_52w), 2),
                'pct_from_high': round(float(pct_from_high), 2),
                'datr_distance': round(float(datr_distance), 2),
                'datr_limit': BLUE_ZONE_CONFIG['datr_multiplier'],
                'current_atr': round(float(current_atr), 2),
                'current_volume': int(current_volume),
                'avg_volume': int(avg_volume),
                'volume_ratio': round(float(volume_ratio), 2),
                'volume_breakout': bool(has_volume_breakout),
                'volume_threshold': VOLUME_CONFIG['multiplier'],
                'days_in_blue_zone': days_in_blue_zone
            }
        }
    except Exception as e:
        print(f"  ❌ Error scanning {ticker}: {e}")
        return None

def scan_volume_breakout(ticker, name):
    """Scan for volume breakout with price movement"""
    try:
        yf_ticker = yf.Ticker(f"{ticker}.NS")
        hist = yf_ticker.history(period='1y')
        
        if hist.empty or len(hist) < VOLUME_CONFIG['avg_period']:
            return None
        
        hist['volume_avg'] = hist['Volume'].rolling(window=VOLUME_CONFIG['avg_period']).mean()
        
        current_volume = hist['Volume'].iloc[-1]
        avg_volume = hist['volume_avg'].iloc[-1]
        current_price = hist['Close'].iloc[-1]
        prev_price = hist['Close'].iloc[-2]
        
        if pd.isna(avg_volume) or avg_volume == 0:
            return None
        
        volume_ratio = current_volume / avg_volume
        
        if volume_ratio < VOLUME_CONFIG['multiplier']:
            return None
        
        price_change_pct = ((current_price - prev_price) / prev_price) * 100
        
        if abs(price_change_pct) < VOLUME_CONFIG['min_price_change']:
            return None
        
        is_bullish = price_change_pct > 0
        
        alert_type = 'volume_breakout_bullish' if is_bullish else 'volume_breakout_bearish'
        alert_category = 'BULLISH' if is_bullish else 'BEARISH'
        alert_description = f"Volume {volume_ratio:.1f}x average with {abs(price_change_pct):.1f}% {'gain' if is_bullish else 'loss'}"
        
        return {
            'ticker': ticker,
            'stock_name': name,
            'alert_type': alert_type,
            'alert_category': alert_category,
            'alert_title': f"{name} - Volume Breakout ({'Bullish' if is_bullish else 'Bearish'})",
            'alert_description': alert_description,
            'price': round(float(current_price), 2),
            'alert_date': hist.index[-1].date().isoformat(),
            'details': {
                'current_volume': int(current_volume),
                'avg_volume': int(avg_volume),
                'volume_ratio': round(float(volume_ratio), 2),
                'price_change_pct': round(float(price_change_pct), 2),
                'direction': 'up' if is_bullish else 'down'
            }
        }
    except Exception as e:
        print(f"  ❌ Error scanning {ticker}: {e}")
        return None

def scan_ema_crossover(ticker, name):
    """Scan for EMA 20/50 crossover"""
    try:
        yf_ticker = yf.Ticker(f"{ticker}.NS")
        hist = yf_ticker.history(period='6mo')
        
        if hist.empty or len(hist) < max(EMA_CONFIG['fast'], EMA_CONFIG['slow']) + EMA_CONFIG['lookback_days']:
            return None
        
        hist['ema_20'] = calculate_ema(hist['Close'], EMA_CONFIG['fast'])
        hist['ema_50'] = calculate_ema(hist['Close'], EMA_CONFIG['slow'])
        
        # Calculate 20-day average volume for the entire history
        hist['volume_avg'] = hist['Volume'].rolling(window=20).mean()
        
        recent_data = hist.tail(EMA_CONFIG['lookback_days'])
        
        # Check for golden cross (bullish) or death cross (bearish)
        for i in range(len(recent_data) - 1):
            prev_idx = recent_data.index[i]
            curr_idx = recent_data.index[i + 1]
            
            prev_20 = recent_data.loc[prev_idx, 'ema_20']
            prev_50 = recent_data.loc[prev_idx, 'ema_50']
            curr_20 = recent_data.loc[curr_idx, 'ema_20']
            curr_50 = recent_data.loc[curr_idx, 'ema_50']
            
            current_price = recent_data.loc[curr_idx, 'Close']
            prev_price = recent_data.loc[prev_idx, 'Close']
            current_volume = recent_data.loc[curr_idx, 'Volume']
            avg_volume = recent_data.loc[curr_idx, 'volume_avg']
            
            # Skip if avg_volume is NaN or zero
            if pd.isna(avg_volume) or avg_volume == 0:
                continue
            
            # Calculate price change and volume ratio
            price_change_pct = ((current_price - prev_price) / prev_price) * 100
            volume_ratio = current_volume / avg_volume
            
            # Golden cross: 20 EMA crosses above 50 EMA
            if prev_20 <= prev_50 and curr_20 > curr_50:
                # Check filters: 3% price move AND 2x volume
                if abs(price_change_pct) >= 3.0 and volume_ratio >= 2.0:
                    return {
                        'ticker': ticker,
                        'stock_name': name,
                        'alert_type': 'ema_golden_cross',
                        'alert_category': 'BULLISH',
                        'alert_title': f"{name} - Golden Cross (20/50 EMA)",
                        'alert_description': f"20 EMA crossed above 50 EMA with {price_change_pct:.1f}% gain and {volume_ratio:.1f}x volume",
                        'price': round(float(current_price), 2),
                        'alert_date': curr_idx.date().isoformat(),
                        'details': {
                            '20_ema': round(float(curr_20), 2),
                            '50_ema': round(float(curr_50), 2),
                            'crossover_date': curr_idx.date().isoformat(),
                            'price_change_pct': round(float(price_change_pct), 2),
                            'volume_ratio': round(float(volume_ratio), 2),
                            'current_volume': int(current_volume),
                            'avg_volume': int(avg_volume)
                        }
                    }
            
            # Death cross: 20 EMA crosses below 50 EMA
            if prev_20 >= prev_50 and curr_20 < curr_50:
                # Check filters: 3% price move AND 2x volume
                if abs(price_change_pct) >= 3.0 and volume_ratio >= 2.0:
                    return {
                        'ticker': ticker,
                        'stock_name': name,
                        'alert_type': 'ema_death_cross',
                        'alert_category': 'BEARISH',
                        'alert_title': f"{name} - Death Cross (20/50 EMA)",
                        'alert_description': f"20 EMA crossed below 50 EMA with {abs(price_change_pct):.1f}% loss and {volume_ratio:.1f}x volume",
                        'price': round(float(current_price), 2),
                        'alert_date': curr_idx.date().isoformat(),
                        'details': {
                            '20_ema': round(float(curr_20), 2),
                            '50_ema': round(float(curr_50), 2),
                            'crossover_date': curr_idx.date().isoformat(),
                            'price_change_pct': round(float(price_change_pct), 2),
                            'volume_ratio': round(float(volume_ratio), 2),
                            'current_volume': int(current_volume),
                            'avg_volume': int(avg_volume)
                        }
                    }
        
        return None
    except Exception as e:
        print(f"  ❌ Error scanning {ticker}: {e}")
        return None


def scan_200ema_breakout(ticker, name):
    """Scan for 200 EMA breakout after consolidation"""
    try:
        yf_ticker = yf.Ticker(f"{ticker}.NS")
        hist = yf_ticker.history(period='1y')
        
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
        
        # Use FIXED 20-day consolidation window (matching test case)
        consolidation_days = BREAKOUT_200_CONFIG['consolidation_days']
        consolidation_start_idx = -(consolidation_days + 1)
        consolidation_end_idx = -1
        consolidation_data = hist.iloc[consolidation_start_idx:consolidation_end_idx]
        
        if len(consolidation_data) < consolidation_days:
            return None
        
        # Count days below 200 EMA (must be 70%+ like test case)
        prices_below = (consolidation_data['Close'] <= consolidation_data['ema_200']).sum()
        pct_below = (prices_below / len(consolidation_data)) * 100
        
        # Must have been below EMA for 70%+ of consolidation period
        if pct_below < 70:
            return None
        
        # Calculate consolidation range
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
        
        alert_description = f"Broke above 200 EMA after {consolidation_days}-day consolidation ({pct_below:.0f}% time below, {range_pct:.1f}% range) with {volume_ratio:.1f}x volume"
        
        return {
            'ticker': ticker,
            'stock_name': name,
            'alert_type': '200ema_breakout',
            'alert_category': 'BULLISH',
            'alert_title': f"{name} - 200 EMA Breakout",
            'alert_description': alert_description,
            'price': round(float(current_price), 2),
            'alert_date': hist.index[-1].date().isoformat(),
            'details': {
                'current_price': round(float(current_price), 2),
                'ema_200': round(float(current_ema_200), 2),
                'breakout_pct': round(float(breakout_pct), 2),
                'consolidation_days': consolidation_days,
                'pct_below_ema': round(float(pct_below), 1),
                'range_pct': round(float(range_pct), 2),
                'volume_ratio': round(float(volume_ratio), 2),
                'current_volume': int(current_volume),
                'avg_volume': int(avg_volume)
            }
        }
    except Exception as e:
        print(f"  ❌ Error scanning {ticker}: {e}")
        return None


def scan_200ema_retest(ticker, name):
    """Scan for 200 EMA retest (pullback to support)"""
    try:
        yf_ticker = yf.Ticker(f"{ticker}.NS")
        hist = yf_ticker.history(period='1y')
        
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
            'ticker': ticker,
            'stock_name': name,
            'alert_type': '200ema_retest',
            'alert_category': 'BULLISH',
            'alert_title': f"{name} - 200 EMA Retest",
            'alert_description': alert_description,
            'price': round(float(current_price), 2),
            'alert_date': hist.index[-1].date().isoformat(),
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
        print(f"  ❌ Error scanning {ticker}: {e}")
        return None


# ================== MAIN EXECUTION ==================

def insert_alert(alert_data):
    """Insert alert into market_alerts table"""
    try:
        # Check for duplicates (same ticker + type in last 1 day, not archived)
        cutoff_date = (datetime.now() - timedelta(days=1)).date().isoformat()
        
        existing = supabase.table('market_alerts').select('id').eq(
            'ticker', alert_data['ticker']
        ).eq(
            'alert_type', alert_data['alert_type']
        ).eq(
            'archived', False  # Only check non-archived alerts
        ).gte(
            'alert_date', cutoff_date
        ).execute()
        
        if len(existing.data) > 0:
            return False
        
        # Insert new alert (mark as not archived)
        alert_data['archived'] = False
        response = supabase.table('market_alerts').insert(alert_data).execute()
        return True
    except Exception as e:
        print(f"  ❌ Error inserting alert: {e}")
        return False

def archive_old_alerts():
    """Archive alerts older than today (don't delete them)"""
    try:
        today = datetime.now().date().isoformat()
        
        # Mark old alerts as archived instead of deleting
        result = supabase.table('market_alerts').update({
            'archived': True
        }).lt(
            'alert_date', today
        ).eq(
            'archived', False  # Only archive if not already archived
        ).execute()
        
        print(f"  📦 Archived alerts older than {today}")
    except Exception as e:
        print(f"  ❌ Error archiving: {e}")

def load_nifty500_stocks():
    """Load Nifty 500 stock list"""
    stocks = []
    
    # Read from file
    with open('tickers_nifty500.txt', 'r') as f:
        for line in f:
            ticker = line.strip()
            if ticker:
                stocks.append({
                    'ticker': ticker,
                    'name': f"{ticker}",  # Can enhance with full names later
                })
    
    return stocks

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
    print("📊 NIFTY 500 MARKET SCANNER")
    print(f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    
    # Load stocks
    print("\n📥 Loading Nifty 500 stocks...")
    stocks = load_nifty500_stocks()
    print(f"  ✅ Loaded {len(stocks)} stocks")
    
    # Archive old alerts
    print("\n📦 Archiving old alerts...")
    archive_old_alerts()
    
    total_alerts = 0
    
    # Scan each stock
    print(f"\n🔍 Scanning {len(stocks)} stocks...")
    
    for i, stock in enumerate(stocks, 1):
        ticker = stock['ticker']
        name = stock['name']
        
        if i % 50 == 0:
            print(f"  Progress: {i}/{len(stocks)} stocks scanned...")
        
        # Scan for each alert type
        alerts = []
        
        # Blue Zone
        blue_zone_alert = scan_blue_zone(ticker, name)
        if blue_zone_alert:
            alerts.append(blue_zone_alert)
        
# Volume Breakout (COMMENTED OUT)
# volume_alert = scan_volume_breakout(ticker, name)
# if volume_alert:
#     alerts.append(volume_alert)
        
        # EMA Crossover
        ema_alert = scan_ema_crossover(ticker, name)
        if ema_alert:
            alerts.append(ema_alert)
        
        # 200 EMA Breakout
        breakout_200_alert = scan_200ema_breakout(ticker, name)
        if breakout_200_alert:
            alerts.append(breakout_200_alert)
        
        # 200 EMA Retest
        retest_200_alert = scan_200ema_retest(ticker, name)
        if retest_200_alert:
            alerts.append(retest_200_alert)
        
        # Insert alerts
        for alert in alerts:
            if insert_alert(alert):
                total_alerts += 1
                print(f"  ✅ {ticker}: {alert['alert_type']}")
    
    print("\n" + "=" * 70)
    print(f"✅ SCAN COMPLETE")
    print(f"📊 Total alerts generated: {total_alerts}")
    print("=" * 70)

if __name__ == '__main__':
    main()
