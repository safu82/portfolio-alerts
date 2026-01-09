#!/usr/bin/env python3
"""
PRODUCTION: Entry Signals Scanner for Nifty 500
Scans for: Narrow CPR Breakaway, Blue Zone (Strong/Buy), 200 EMA Retest

TIGHTENED CRITERIA (Production Quality):
- Narrow CPR: < 0.3% width, within 0.5% of L3, volume > 1.2x
- Blue Zone: RSI EMA(9) > 60, pullback 5-10%, volume > 1.5x
- 200 EMA: Within 2%, peak 8%+ above, previous touch with volume

Schedule: Daily at 5:00 PM IST (after OHLC fetch)
Runtime: ~10-15 minutes for 500 stocks
Expected Signals: 15-25 high-quality opportunities
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from supabase import create_client, Client
from typing import List, Dict, Tuple, Optional
import time

# ============================================
# CONFIGURATION
# ============================================

SUPABASE_URL = 'https://hcgyncghmcvylnrmcivj.supabase.co'
SUPABASE_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhjZ3luY2dobWN2eWxucm1jaXZqIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTc1MTQwMTEsImV4cCI6MjA3MzA5MDAxMX0.n8vFVCJe1y_3o8fpAY0IgasZ4eKl7DAogEM3OlHB8Ww'

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

def fetch_stock_data_from_supabase(supabase: Client, ticker: str, days: int = 60) -> Optional[pd.DataFrame]:
    """
    Fetch OHLC data from Supabase daily_stock_snapshots table
    """
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

def fetch_weekly_data(ticker: str, weeks: int = 52) -> Optional[pd.DataFrame]:
    """
    Fetch weekly OHLC data from Yahoo Finance
    """
    try:
        stock = yf.Ticker(ticker)
        end_date = datetime.now()
        start_date = end_date - timedelta(weeks=weeks)
        
        df = stock.history(start=start_date, end=end_date, interval='1wk')
        
        if df.empty:
            return None
        
        df = df.reset_index()
        return df
        
    except Exception as e:
        return None

def get_stock_name(ticker: str) -> str:
    """Get stock name from ticker"""
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        return info.get('longName', ticker.replace('.NS', ''))
    except:
        return ticker.replace('.NS', '')

# ============================================
# SIGNAL DETECTION FUNCTIONS
# ============================================

def check_narrow_cpr_breakaway(
    daily_df: pd.DataFrame, 
    current_price: float
) -> Optional[Dict]:
    """
    Check for Narrow CPR Breakaway signal
    
    TIGHTENED Criteria:
    - CPR width < 0.3% (very narrow, not 0.5%)
    - Price at L3 support (within 0.5%, not 1%) for reversal buy
    - Must have volume confirmation (> 1.2x average)
    """
    if len(daily_df) < 20:
        return None
    
    try:
        # Get yesterday's data
        yesterday = daily_df.iloc[-2]
        today = daily_df.iloc[-1]
        high = yesterday['high']
        low = yesterday['low']
        close = yesterday['close']
        
        # Calculate pivots
        pivots = calculate_pivot_points(high, low, close)
        
        # Calculate CPR width
        cpr_width = calculate_cpr_width(pivots['tc'], pivots['bc'], pivots['pp'])
        
        # TIGHTER: CPR must be very narrow (< 0.3%)
        if cpr_width >= 0.3:
            return None
        
        # Check if at L3 support (buy signal) - TIGHTER: within 0.5%
        distance_to_l3 = ((current_price - pivots['l3']) / pivots['l3']) * 100
        at_l3 = abs(distance_to_l3) <= 0.5  # Within 0.5% (was 1%)
        
        if not at_l3:
            return None
        
        # Volume confirmation - must have decent volume
        avg_volume = daily_df['volume'].tail(20).mean()
        current_volume = today['volume']
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
        
        return None
        
    except Exception as e:
        return None

def check_blue_zone_signal(
    daily_df: pd.DataFrame,
    weekly_df: Optional[pd.DataFrame],
    current_price: float
) -> Optional[Dict]:
    """
    Check for Blue Zone signal (Strong or Buy)
    
    ORIGINAL BOOK CRITERIA (TIGHTENED):
    Strong Buy:
    - Daily RSI EMA(9) > 60
    - Weekly RSI EMA(9) > 60
    - Within 15% of 52W high
    - Pulled back 5-10% from recent peak
    - Above EMA 50
    - Volume > 1.5x average
    
    Buy:
    - Daily RSI EMA(9) > 60
    - Weekly RSI EMA(9) > 50
    - Within 15% of 52W high
    - Pulled back 5-10% from recent peak
    - Above EMA 20
    - Volume > 1.5x average
    """
    if len(daily_df) < 50:
        return None
    
    try:
        # Calculate daily RSI and EMA
        daily_df['rsi'] = calculate_rsi(daily_df['close'], 14)
        daily_df['rsi_ema_9'] = calculate_ema(daily_df['rsi'], 9)
        daily_df['ema_20'] = calculate_ema(daily_df['close'], 20)
        daily_df['ema_50'] = calculate_ema(daily_df['close'], 50)
        
        latest = daily_df.iloc[-1]
        daily_rsi_ema_9 = latest['rsi_ema_9']
        
        # Check daily RSI EMA(9) > 60 (ORIGINAL)
        if daily_rsi_ema_9 <= 60:
            return None
        
        # Calculate weekly RSI EMA(9) if weekly data available
        weekly_rsi_ema_9 = None
        if weekly_df is not None and len(weekly_df) >= 14:
            weekly_df['rsi'] = calculate_rsi(weekly_df['Close'], 14)
            weekly_df['rsi_ema_9'] = calculate_ema(weekly_df['rsi'], 9)
            weekly_rsi_ema_9 = weekly_df['rsi_ema_9'].iloc[-1]
        
        # Calculate 52W high
        high_52w = daily_df['high'].tail(252).max()  # ~252 trading days in a year
        distance_from_52w_high = ((current_price - high_52w) / high_52w) * 100
        
        # Check within 15% of 52W high (ORIGINAL)
        if distance_from_52w_high < -15:
            return None
        
        # Calculate pullback from recent peak (last 20 days)
        recent_peak = daily_df['high'].tail(20).max()
        pullback_from_peak = ((current_price - recent_peak) / recent_peak) * 100
        
        # Check pullback 5-10% (ORIGINAL)
        if pullback_from_peak > -5 or pullback_from_peak < -10:
            return None
        
        # Calculate volume ratio
        avg_volume = daily_df['volume'].tail(20).mean()
        current_volume = latest['volume']
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0
        
        # Check volume > 1.5x (ORIGINAL)
        if volume_ratio < 1.5:
            return None
        
        # Determine signal strength
        above_ema_50 = current_price > latest['ema_50']
        above_ema_20 = current_price > latest['ema_20']
        
        if weekly_rsi_ema_9 and weekly_rsi_ema_9 > 60 and above_ema_50:
            signal_strength = 'strong'
            signal_type = 'blue_zone_strong'
        elif weekly_rsi_ema_9 and weekly_rsi_ema_9 > 50 and above_ema_20:
            signal_strength = 'regular'
            signal_type = 'blue_zone_buy'
        else:
            return None
        
        return {
            'signal_type': signal_type,
            'signal_strength': signal_strength,
            'daily_rsi_ema_9': round(daily_rsi_ema_9, 2),
            'weekly_rsi_ema_9': round(weekly_rsi_ema_9, 2) if weekly_rsi_ema_9 else None,
            'distance_from_52w_high': round(distance_from_52w_high, 2),
            'pullback_from_peak': round(pullback_from_peak, 2),
            'above_ema_50': above_ema_50,
            'volume_ratio': round(volume_ratio, 2)
        }
        
    except Exception as e:
        return None

def check_200_ema_retest(daily_df: pd.DataFrame, current_price: float) -> Optional[Dict]:
    """
    Check for 200 EMA Power Retest signal
    
    TIGHTENED CRITERIA:
    - Currently within 2% of 200 EMA (was 3%)
    - 50 EMA > 200 EMA
    - 50 EMA rising (today > 10 days ago)
    - Stock was at least 8% above 200 EMA in last 30 days (was 5%)
    - Previously came within 2% of 200 EMA in last 90 days
    - Volume on previous touch > 1.3x average
    """
    if len(daily_df) < 180:
        return None
    
    try:
        # Calculate EMAs
        daily_df['ema_50'] = calculate_ema(daily_df['close'], 50)
        daily_df['ema_200'] = calculate_ema(daily_df['close'], 200)
        
        latest = daily_df.iloc[-1]
        ema_50 = latest['ema_50']
        ema_200 = latest['ema_200']
        
        # Check within 2% of 200 EMA (ORIGINAL)
        distance_from_200ema = ((current_price - ema_200) / ema_200) * 100
        if abs(distance_from_200ema) > 2:
            return None
        
        # Check 50 EMA > 200 EMA
        if ema_50 <= ema_200:
            return None
        
        # Check 50 EMA rising
        ema_50_10d_ago = daily_df['ema_50'].iloc[-11] if len(daily_df) >= 11 else None
        ema_50_rising = ema_50 > ema_50_10d_ago if ema_50_10d_ago else False
        
        if not ema_50_rising:
            return None
        
        # Check stock was 8%+ above 200 EMA in last 30 days (TIGHTENED from 5%)
        last_30_days = daily_df.tail(30).copy()
        last_30_days['distance_200'] = ((last_30_days['close'] - last_30_days['ema_200']) / last_30_days['ema_200']) * 100
        peak_above_200ema_30d = last_30_days['distance_200'].max()
        
        if peak_above_200ema_30d < 8:
            return None
        
        # Check came within 2% of 200 EMA before in last 90 days
        last_90_days = daily_df.tail(90).copy()
        last_90_days['distance_200'] = abs((last_90_days['close'] - last_90_days['ema_200']) / last_90_days['ema_200']) * 100
        
        # Find instances where stock was within 2% of 200 EMA (ORIGINAL)
        near_200ema = last_90_days[last_90_days['distance_200'] <= 2]
        
        if len(near_200ema) < 2:  # Need at least 2 touches (including current)
            return None
        
        # Get previous touch (not including today)
        previous_touches = near_200ema.iloc[:-1]
        if len(previous_touches) == 0:
            return None
        
        previous_touch = near_200ema.index[-2]
        previous_touch_row = near_200ema.iloc[-2]
        
        # Calculate average volume and check previous touch had volume
        avg_volume = daily_df['volume'].tail(20).mean()
        previous_volume = previous_touch_row['volume']
        
        # Check volume on previous touch > 1.3x (ADDED BACK)
        if previous_volume < avg_volume * 1.3:
            return None
        
        return {
            'signal_type': '200_ema_retest',
            'signal_strength': 'regular',
            'distance_from_200ema': round(distance_from_200ema, 2),
            'ema_50_rising': True,
            'peak_above_200ema_30d': round(peak_above_200ema_30d, 2),
            'previous_touch_date': str(previous_touch.date()) if hasattr(previous_touch, 'date') else str(previous_touch)
        }
        
    except Exception as e:
        return None

# ============================================
# MAIN SCANNER
# ============================================

def scan_stock(supabase: Client, ticker: str) -> List[Dict]:
    """
    Scan a single stock for all entry signals
    
    Returns list of signals found
    """
    signals = []
    
    try:
        # Fetch daily data from Supabase
        daily_df = fetch_stock_data_from_supabase(supabase, ticker, days=260)  # ~1 year
        
        if daily_df is None or len(daily_df) < 60:
            return signals
        
        current_price = daily_df.iloc[-1]['close']
        
        # Get stock name
        stock_name = get_stock_name(ticker)
        
        # Check Narrow CPR Breakaway
        cpr_signal = check_narrow_cpr_breakaway(daily_df, current_price)
        if cpr_signal:
            cpr_signal['ticker'] = ticker
            cpr_signal['stock_name'] = stock_name
            cpr_signal['price'] = round(current_price, 2)
            signals.append(cpr_signal)
        
        # Fetch weekly data for Blue Zone
        weekly_df = fetch_weekly_data(ticker, weeks=52)
        
        # Check Blue Zone
        blue_zone_signal = check_blue_zone_signal(daily_df, weekly_df, current_price)
        if blue_zone_signal:
            blue_zone_signal['ticker'] = ticker
            blue_zone_signal['stock_name'] = stock_name
            blue_zone_signal['price'] = round(current_price, 2)
            signals.append(blue_zone_signal)
        
        # Check 200 EMA Retest
        ema_signal = check_200_ema_retest(daily_df, current_price)
        if ema_signal:
            ema_signal['ticker'] = ticker
            ema_signal['stock_name'] = stock_name
            ema_signal['price'] = round(current_price, 2)
            signals.append(ema_signal)
        
        return signals
        
    except Exception as e:
        return signals

def save_signals_to_supabase(supabase: Client, signals: List[Dict]) -> bool:
    """Save entry signals to Supabase"""
    try:
        if not signals:
            return True
        
        # Prepare records for insertion
        records = []
        for signal in signals:
            # Extract common fields
            ticker = signal.pop('ticker')
            stock_name = signal.pop('stock_name')
            price = signal.pop('price')
            signal_type = signal.pop('signal_type')
            signal_strength = signal.pop('signal_strength')
            
            # Remaining fields go into details JSON
            # Convert booleans and numpy types to JSON-compatible types
            details = {}
            for key, value in signal.items():
                if isinstance(value, bool):
                    details[key] = value  # Booleans are JSON-compatible
                elif isinstance(value, (np.bool_, np.integer, np.floating)):
                    details[key] = value.item()  # Convert numpy types
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
                'alert_date': str(datetime.now().date()),
                'details': details
            }
            records.append(record)
        
        # Upsert to Supabase (insert or update)
        response = supabase.table('entry_signals').upsert(
            records,
            on_conflict='ticker,signal_type,alert_date'
        ).execute()
        
        return True
        
    except Exception as e:
        print(f"  ❌ Failed to save signals: {e}")
        return False

def scan_all_stocks(supabase: Client, tickers: List[str]) -> Dict:
    """
    Scan all Nifty 500 stocks for entry signals
    """
    stats = {
        'total': len(tickers),
        'scanned': 0,
        'narrow_cpr': 0,
        'blue_zone_strong': 0,
        'blue_zone_buy': 0,
        '200_ema_retest': 0,
        'multiple_signals': 0,
        'failed': 0
    }
    
    all_signals = []
    stocks_with_multiple_signals = []
    
    print(f"\n🔍 Scanning {len(tickers)} stocks for entry signals...")
    print("=" * 80)
    
    for i, ticker in enumerate(tickers, 1):
        try:
            # Progress indicator every 50 stocks
            if i % 50 == 0:
                print(f"\n[{i}/{len(tickers)}] Progress: {(i/len(tickers)*100):.1f}%")
                print(f"  Signals found: CPR={stats['narrow_cpr']}, "
                      f"Blue Zone Strong={stats['blue_zone_strong']}, "
                      f"Blue Zone Buy={stats['blue_zone_buy']}, "
                      f"200 EMA={stats['200_ema_retest']}")
            
            # Scan stock
            signals = scan_stock(supabase, ticker)
            
            if signals:
                all_signals.extend(signals)
                stats['scanned'] += 1
                
                # Count by signal type
                for signal in signals:
                    signal_type = signal['signal_type']
                    if signal_type == 'narrow_cpr_breakaway':
                        stats['narrow_cpr'] += 1
                    elif signal_type == 'blue_zone_strong':
                        stats['blue_zone_strong'] += 1
                    elif signal_type == 'blue_zone_buy':
                        stats['blue_zone_buy'] += 1
                    elif signal_type == '200_ema_retest':
                        stats['200_ema_retest'] += 1
                
                # Track multiple signals
                if len(signals) > 1:
                    stats['multiple_signals'] += 1
                    stocks_with_multiple_signals.append({
                        'ticker': ticker,
                        'count': len(signals)
                    })
                
                print(f"  ✅ {ticker:20} - {len(signals)} signal(s)")
            else:
                stats['scanned'] += 1
            
            # Rate limiting
            time.sleep(0.2)
            
        except Exception as e:
            stats['failed'] += 1
            print(f"  ⚠️  {ticker:20} - Error: {str(e)[:50]}")
    
    # Save all signals to Supabase
    if all_signals:
        print(f"\n💾 Saving {len(all_signals)} signals to database...")
        save_signals_to_supabase(supabase, all_signals)
    
    return stats, stocks_with_multiple_signals

def cleanup_old_signals(supabase: Client) -> int:
    """Delete expired signals (older than 5 days)"""
    try:
        cutoff_date = (datetime.now() - timedelta(days=5)).date()
        
        print(f"\n🗑️  Cleaning up signals older than {cutoff_date}...")
        
        response = supabase.table('entry_signals')\
            .delete()\
            .lt('alert_date', str(cutoff_date))\
            .execute()
        
        deleted = len(response.data) if response.data else 0
        print(f"✅ Deleted {deleted} old signals")
        return deleted
        
    except Exception as e:
        print(f"❌ Cleanup failed: {e}")
        return 0

def main():
    """Main function"""
    print("=" * 80)
    print("🔍 ENTRY SIGNALS SCANNER FOR NIFTY 500")
    print("=" * 80)
    
    # Initialize Supabase
    supabase = init_supabase()
    if not supabase:
        print("\n❌ Cannot proceed without Supabase connection")
        return
    
    # Get list of unique tickers from daily_stock_snapshots
    print("\n📋 Fetching ticker list from database...")
    
    # Get latest date to fetch only recent tickers
    latest_response = supabase.table('daily_stock_snapshots')\
        .select('snapshot_date')\
        .order('snapshot_date', desc=True)\
        .limit(1)\
        .execute()
    
    if not latest_response.data:
        print("❌ No data found in database. Run fetch_ohlc_nifty500.py first!")
        return
    
    latest_date = latest_response.data[0]['snapshot_date']
    print(f"  Using latest date: {latest_date}")
    
    # Fetch all tickers from latest date with proper pagination
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
    
    if not all_tickers:
        print("❌ No tickers found in database. Run fetch_ohlc_nifty500.py first!")
        return
    
    # Remove duplicates and sort
    tickers = sorted(list(set(all_tickers)))
    print(f"✅ Found {len(tickers)} unique stocks in database")
    
    # Scan all stocks
    stats, multiple_signals = scan_all_stocks(supabase, tickers)
    
    # Cleanup old signals
    cleanup_old_signals(supabase)
    
    # Print summary
    print("\n" + "=" * 80)
    print("📈 SUMMARY")
    print("=" * 80)
    print(f"✅ Stocks scanned: {stats['scanned']}/{stats['total']}")
    print(f"⚠️  Failed: {stats['failed']}/{stats['total']}")
    print(f"\n📊 Signals Found:")
    print(f"  📍 Narrow CPR Breakaway: {stats['narrow_cpr']}")
    print(f"  🟢 Blue Zone Strong Buy: {stats['blue_zone_strong']}")
    print(f"  🟢 Blue Zone Buy: {stats['blue_zone_buy']}")
    print(f"  📈 200 EMA Retest: {stats['200_ema_retest']}")
    print(f"  🔥 Stocks with multiple signals: {stats['multiple_signals']}")
    
    if multiple_signals:
        print(f"\n🔥 Stocks with Multiple Signals:")
        for stock in multiple_signals[:10]:  # Show top 10
            print(f"  • {stock['ticker']:20} - {stock['count']} signals")
    
    print("\n" + "=" * 80)
    print("✅ DONE! Check entry_signals table in Supabase!")
    print("=" * 80)

if __name__ == "__main__":
    main()
