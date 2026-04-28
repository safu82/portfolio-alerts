#!/usr/bin/env python3
"""
PRODUCTION: Entry Signals Scanner for Nifty 500
Scans for: Narrow CPR, Blue Zone, Golden Cross (with ADX), MACD (with ADX),
           Darvas Box, Pullback Bounce, Promoter Buying (disabled)

UPDATED (April 2026):
- Narrow CPR: < 0.3% width, within 0.5% of L3, volume > 1.2x
- Blue Zone: RSI EMA(9) >= 65/72 daily, >= 55/65 weekly (lowered from 70/75 and 60/70)
- Golden Cross: 20×50 EMA cross, ADX > 25 + +DI > -DI required for Strong Buy
- MACD: Bullish crossover, ADX > 25 for Strong Buy, ADX > 20 for Buy (raised from no minimum)
- Darvas Box: Tight consolidation near 52W high, volume breakout (NEW - Portfolio A)
- Pullback Bounce: EMA20 touch-and-recover in strong uptrend (NEW - replaces 200 EMA)
- Bull Flag: Pole ≥15% in 5-15d on vol ≥2x, shallow flag with drying volume (NEW)
             Strong: pole ≥20%, vol ≥3x, retrace ≤35%, RS rank ≤75
             Buy:    pole ≥15%, vol ≥2x, retrace ≤50%, RS rank ≤125
- VCP: Minervini Volatility Contraction Pattern, ≥3 progressively tighter contractions (NEW)
       Strong: ≥4 contractions, latest depth <10%, vol <50% avg, RS rank ≤75
       Buy:    ≥3 contractions, latest depth <15%, vol <60% avg, RS rank ≤125
- 200 EMA two-stage: DEPRECATED (removed)
- Promoter Buying: DEPRECATED (Trendlyne scraping unreliable from cloud IPs)

Schedule: Daily at 8:00 AM IST (after OHLC fetch at 7:00 AM)
Runtime: ~15-20 minutes for 500 stocks
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from supabase import create_client, Client
from typing import List, Dict, Tuple, Optional
import time
import os

# ============================================
# CONFIGURATION
# ============================================

SUPABASE_URL = os.getenv('SUPABASE_URL', 'https://hcgyncghmcvylnrmcivj.supabase.co')
SUPABASE_KEY = os.getenv('SUPABASE_KEY', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhjZ3luY2dobWN2eWxucm1jaXZqIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTc1MTQwMTEsImV4cCI6MjA3MzA5MDAxMX0.n8vFVCJe1y_3o8fpAY0IgasZ4eKl7DAogEM3OlHB8Ww')

STOCK_NAMES = {}  # Populated from company info at runtime

# ============================================
# TECHNICAL INDICATOR CALCULATIONS
# ============================================

def calculate_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def calculate_macd(df: pd.DataFrame) -> pd.DataFrame:
    ema_12 = calculate_ema(df['close'], 12)
    ema_26 = calculate_ema(df['close'], 26)
    macd_line = ema_12 - ema_26
    signal_line = calculate_ema(macd_line, 9)
    df['macd_line'] = macd_line
    df['macd_signal'] = signal_line
    df['macd_histogram'] = macd_line - signal_line
    return df


def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    high = df['high']
    low = df['low']
    close = df['close']

    high_low = high - low
    high_close = np.abs(high - close.shift())
    low_close = np.abs(low - close.shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)

    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm < 0] = 0
    plus_dm[(plus_dm < minus_dm)] = 0
    minus_dm[(minus_dm < plus_dm)] = 0

    atr = tr.ewm(alpha=1/period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr)

    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di)
    adx = dx.ewm(alpha=1/period, adjust=False).mean()

    df['plus_di'] = plus_di
    df['minus_di'] = minus_di
    df['adx'] = adx
    return df


def calculate_pivot_points(high: float, low: float, close: float) -> Dict[str, float]:
    pp = (high + low + close) / 3
    r1 = 2 * pp - low
    r2 = pp + (high - low)
    r3 = r1 + (high - low)
    s1 = 2 * pp - high
    s2 = pp - (high - low)
    s3 = s1 - (high - low)
    bc = (high + low) / 2
    tc = (pp - bc) + pp
    h1 = close + (high - low) * 1.1 / 12
    h2 = close + (high - low) * 1.1 / 6
    h3 = close + (high - low) * 1.1 / 4
    h4 = close + (high - low) * 1.1 / 2
    l1 = close - (high - low) * 1.1 / 12
    l2 = close - (high - low) * 1.1 / 6
    l3 = close - (high - low) * 1.1 / 4
    l4 = close - (high - low) * 1.1 / 2
    return {
        'pp': pp, 'r1': r1, 'r2': r2, 'r3': r3,
        's1': s1, 's2': s2, 's3': s3,
        'tc': tc, 'bc': bc,
        'h1': h1, 'h2': h2, 'h3': h3, 'h4': h4,
        'l1': l1, 'l2': l2, 'l3': l3, 'l4': l4
    }


def calculate_cpr_width(tc: float, bc: float, pp: float) -> float:
    return (abs(tc - bc) / pp) * 100

# ============================================
# DATA FETCHING
# ============================================

def init_supabase() -> Client:
    try:
        return create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"❌ Supabase connection failed: {e}")
        return None


def fetch_stock_data_from_supabase(supabase: Client, ticker: str, days: int = 180) -> Optional[pd.DataFrame]:
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

    except Exception:
        return None


def get_stock_name(ticker: str) -> str:
    if ticker in STOCK_NAMES:
        return STOCK_NAMES[ticker]
    name = ticker.replace('.NS', '').replace('.BO', '')
    STOCK_NAMES[ticker] = name
    return name

# ============================================
# SIGNAL DETECTION FUNCTIONS
# ============================================

def check_narrow_cpr_signal(daily_df: pd.DataFrame, current_price: float) -> Optional[Dict]:
    """
    Narrow CPR breakaway signal.
    CPR width < 0.3%, price within 0.5% of L3, volume > 1.2x average.
    """
    if len(daily_df) < 20:
        return None
    try:
        yesterday = daily_df.iloc[-2]
        latest = daily_df.iloc[-1]
        pivots = calculate_pivot_points(yesterday['high'], yesterday['low'], yesterday['close'])
        cpr_width = calculate_cpr_width(pivots['tc'], pivots['bc'], pivots['pp'])
        if cpr_width >= 0.3:
            return None
        distance_to_l3 = abs(current_price - pivots['l3']) / pivots['l3'] * 100
        if distance_to_l3 > 0.5:
            return None
        avg_volume = daily_df['volume'].tail(20).mean()
        volume_ratio = latest['volume'] / avg_volume if avg_volume > 0 else 0
        if volume_ratio < 1.2:
            return None
        return {
            'signal_type': 'narrow_cpr_breakaway',
            'signal_strength': 'strong',
            'cpr_width': round(cpr_width, 3),
            'at_l3': True,
            'distance_to_level': round(distance_to_l3, 2),
            'volume_ratio': round(volume_ratio, 2)
        }
    except Exception:
        return None


def check_blue_zone_signal(daily_df: pd.DataFrame, current_price: float) -> Optional[Dict]:
    """
    Blue Zone momentum signal.

    UPDATED THRESHOLDS (April 2026):
    Strong Buy: Daily RSI EMA(9) >= 72, Weekly RSI EMA(9) >= 65, near 52W high, above EMA50, volume > 1.5x.
    Buy:        Daily RSI EMA(9) >= 65, Weekly RSI EMA(9) >= 55, near 52W high, above EMA20.

    Lowered from 75/70 and 70/60 to account for structurally lower RSI levels
    in correcting/sideways markets while preserving the signal's quality intent.
    """
    if len(daily_df) < 50:
        return None
    try:
        latest = daily_df.iloc[-1]
        daily_rsi_ema_9 = latest.get('rsi_ema_9')
        weekly_rsi_ema_9 = latest.get('weekly_rsi_ema_9')
        ema_20 = latest.get('ema_20')
        ema_50 = latest.get('ema_50')

        if pd.isna(daily_rsi_ema_9) or pd.isna(weekly_rsi_ema_9):
            return None

        # Use stored high_52w if available, fall back to rolling max of available data
        high_52w = latest.get('high_52w')
        if pd.isna(high_52w) or high_52w is None:
            high_52w = daily_df['high'].max()

        distance_from_52w_high = ((current_price - high_52w) / high_52w) * 100
        if distance_from_52w_high < -10:
            return None

        avg_volume = daily_df['volume'].tail(20).mean()
        volume_ratio = latest['volume'] / avg_volume if avg_volume > 0 else 0
        above_ema_50 = current_price > ema_50 if pd.notna(ema_50) else False
        above_ema_20 = current_price > ema_20 if pd.notna(ema_20) else False

        if (daily_rsi_ema_9 >= 72 and weekly_rsi_ema_9 >= 65 and
                above_ema_50 and volume_ratio > 1.5):
            signal_strength = 'strong'
            signal_type = 'blue_zone_strong'
        elif (daily_rsi_ema_9 >= 65 and weekly_rsi_ema_9 >= 55 and above_ema_20):
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
    except Exception:
        return None


def check_golden_cross(daily_df: pd.DataFrame, current_price: float) -> Optional[Dict]:
    """
    Golden Cross: 20 EMA crossing above 50 EMA.
    Strong Buy: crossover 1-3 days, above 200 EMA, volume > 2x, >2% above 50 EMA,
                RSI > 55, both EMAs rising, ADX > 25 + DI alignment.
    Buy: crossover 4-7 days, above 50 EMA, volume > 1.5x, >1% above 50 EMA, RSI > 45, EMA20 rising.
    """
    if len(daily_df) < 50:
        return None
    try:
        daily_df = calculate_adx(daily_df)
        latest = daily_df.iloc[-1]

        ema_20 = latest.get('ema_20')
        ema_50 = latest.get('ema_50')
        ema_200 = latest.get('ema_200')
        rsi_14 = latest.get('rsi_14')
        adx_value = latest['adx']
        plus_di = latest['plus_di']
        minus_di = latest['minus_di']

        if pd.isna(ema_20) or pd.isna(ema_50) or pd.isna(rsi_14):
            return None

        crossover_day = None
        for i in range(1, min(8, len(daily_df))):
            prev_day = daily_df.iloc[-(i+1)]
            curr_day = daily_df.iloc[-i]
            prev_ema_20 = prev_day.get('ema_20')
            prev_ema_50 = prev_day.get('ema_50')
            curr_ema_20 = curr_day.get('ema_20')
            curr_ema_50 = curr_day.get('ema_50')
            if pd.isna(prev_ema_20) or pd.isna(prev_ema_50) or pd.isna(curr_ema_20) or pd.isna(curr_ema_50):
                continue
            if prev_ema_20 <= prev_ema_50 and curr_ema_20 > curr_ema_50:
                crossover_day = i
                break

        if crossover_day is None or current_price <= ema_50:
            return None

        pct_above_50_ema = ((current_price - ema_50) / ema_50) * 100
        avg_volume = daily_df['volume'].tail(20).mean()
        volume_ratio = latest['volume'] / avg_volume if avg_volume > 0 else 0

        ema_20_5d_ago = daily_df.iloc[-6].get('ema_20') if len(daily_df) >= 6 else None
        ema_50_10d_ago = daily_df.iloc[-11].get('ema_50') if len(daily_df) >= 11 else None
        ema_20_rising = ema_20 > ema_20_5d_ago if pd.notna(ema_20_5d_ago) else False
        ema_50_rising = ema_50 > ema_50_10d_ago if pd.notna(ema_50_10d_ago) else False
        above_200_ema = current_price > ema_200 if pd.notna(ema_200) else False

        if (crossover_day <= 3 and above_200_ema and volume_ratio > 2.0 and
                pct_above_50_ema > 2.0 and rsi_14 > 55 and ema_20_rising and
                ema_50_rising and adx_value > 25 and plus_di > minus_di):
            signal_strength = 'strong'
            signal_type = 'golden_cross_strong'
        elif (crossover_day <= 7 and volume_ratio > 1.5 and
              pct_above_50_ema > 1.0 and rsi_14 > 45 and ema_20_rising):
            signal_strength = 'regular'
            signal_type = 'golden_cross_buy'
        else:
            return None

        return {
            'signal_type': signal_type,
            'signal_strength': signal_strength,
            'days_since_crossover': crossover_day,
            'pct_above_50_ema': round(pct_above_50_ema, 2),
            'above_200_ema': above_200_ema,
            'volume_ratio': round(volume_ratio, 2),
            'rsi_14': round(rsi_14, 2),
            'ema_20_rising': ema_20_rising,
            'ema_50_rising': ema_50_rising,
            'ema_20': round(ema_20, 2),
            'ema_50': round(ema_50, 2),
            'ema_200': round(ema_200, 2) if pd.notna(ema_200) else None,
            'adx': round(float(adx_value), 2),
            'plus_di': round(float(plus_di), 2),
            'minus_di': round(float(minus_di), 2),
            'adx_trending': bool(adx_value > 25)
        }
    except Exception:
        return None


def check_macd_signal(daily_df: pd.DataFrame, current_price: float) -> Optional[Dict]:
    """
    MACD bullish crossover.
    Strong Buy: crossover 1-3 days, histogram expanding, both lines > 0,
                above 200 EMA, ADX > 25 + DI alignment.
    Buy: crossover 4-5 days, histogram positive, above 50 EMA, ADX > 20 (raised from no minimum).
    """
    if len(daily_df) < 50:
        return None
    try:
        daily_df = calculate_macd(daily_df)
        daily_df = calculate_adx(daily_df)
        latest = daily_df.iloc[-1]

        ema_50 = latest.get('ema_50')
        ema_200 = latest.get('ema_200')

        crossover_day = None
        for i in range(1, min(6, len(daily_df))):
            prev_day = daily_df.iloc[-(i+1)]
            curr_day = daily_df.iloc[-i]
            if (prev_day['macd_line'] <= prev_day['macd_signal'] and
                    curr_day['macd_line'] > curr_day['macd_signal']):
                crossover_day = i
                break

        if crossover_day is None:
            return None

        macd_line = latest['macd_line']
        signal_line = latest['macd_signal']
        histogram = latest['macd_histogram']
        adx_value = latest['adx']
        plus_di = latest['plus_di']
        minus_di = latest['minus_di']
        histogram_expanding = histogram > daily_df.iloc[-2]['macd_histogram']
        in_bullish_zone = macd_line > 0 and signal_line > 0
        above_200_ema = current_price > ema_200 if pd.notna(ema_200) else False
        above_50_ema = current_price > ema_50 if pd.notna(ema_50) else False

        if (crossover_day <= 3 and histogram_expanding and in_bullish_zone and
                above_200_ema and adx_value > 25 and plus_di > minus_di):
            signal_strength = 'strong'
            signal_type = 'macd_strong'
        elif (crossover_day <= 5 and histogram > 0 and above_50_ema and
              adx_value > 20):  # ← ADX floor raised from no minimum
            signal_strength = 'regular'
            signal_type = 'macd_buy'
        else:
            return None

        return {
            'signal_type': signal_type,
            'signal_strength': signal_strength,
            'days_since_crossover': crossover_day,
            'macd_line': round(float(macd_line), 3),
            'signal_line': round(float(signal_line), 3),
            'histogram': round(float(histogram), 3),
            'histogram_expanding': histogram_expanding,
            'in_bullish_zone': in_bullish_zone,
            'adx': round(float(adx_value), 2),
            'plus_di': round(float(plus_di), 2),
            'minus_di': round(float(minus_di), 2),
            'adx_trending': bool(adx_value > 25),
            'above_200_ema': above_200_ema,
            'above_50_ema': above_50_ema
        }
    except Exception:
        return None


def check_darvas_box(daily_df: pd.DataFrame, current_price: float) -> Optional[Dict]:
    """
    Darvas Box breakout — Portfolio A's best performing pattern (49% win rate).
    Tight consolidation (< 15% depth) within 1% of 52W high, volume breakout >= 1.5x, above EMA50.
    Box duration: 10-60 trading days.
    """
    if len(daily_df) < 60:
        return None
    try:
        latest = daily_df.iloc[-1]
        current_close = latest['close']
        ema_50 = latest.get('ema_50')

        if pd.isna(ema_50) or current_close <= ema_50:
            return None

        # Use stored high_52w if available, fall back to rolling max of available data
        high_52w = latest.get('high_52w')
        if pd.isna(high_52w) or high_52w is None:
            high_52w = daily_df['high'].max()

        box_found = False
        box_top = box_bottom = box_duration = None

        for lookback in range(10, 61):
            if len(daily_df) < lookback + 1:
                break
            window = daily_df.iloc[-(lookback + 1):-1]  # exclude today
            box_top_candidate = window['high'].max()
            box_bottom_candidate = window['low'].min()
            box_depth = (box_top_candidate - box_bottom_candidate) / box_top_candidate * 100
            distance_from_52w = abs(box_top_candidate - high_52w) / high_52w * 100

            if box_depth < 15 and distance_from_52w < 1.0:
                box_found = True
                box_top = box_top_candidate
                box_bottom = box_bottom_candidate
                box_duration = lookback
                break

        if not box_found:
            return None

        # Breakout: today's close above box top + 0.2% margin
        if current_close <= box_top * 1.002:
            return None

        avg_volume = daily_df['volume'].tail(20).mean()
        volume_ratio = latest['volume'] / avg_volume if avg_volume > 0 else 0
        if volume_ratio < 1.5:
            return None

        return {
            'signal_type': 'darvas_box',
            'signal_strength': 'strong',
            'box_top': round(box_top, 2),
            'box_bottom': round(box_bottom, 2),
            'box_depth_pct': round((box_top - box_bottom) / box_top * 100, 2),
            'box_duration_days': box_duration,
            'distance_from_52w_high': round(abs(box_top - high_52w) / high_52w * 100, 2),
            'volume_ratio': round(volume_ratio, 2),
            'ema_50': round(ema_50, 2),
            'breakout_level': round(box_top * 1.002, 2)
        }
    except Exception:
        return None


def check_pullback_bounce(daily_df: pd.DataFrame, current_price: float) -> Optional[Dict]:
    """
    Pullback bounce off EMA20 in a strong uptrend.
    Replaces the deprecated 200 EMA two-stage signal — single pass, no tracking table.

    Strong uptrend: EMA20 > EMA50 * 1.05 > EMA200.
    Touch: intraday low <= EMA20.
    Recovery: close above EMA20, in upper 50% of day's range.
    Volume >= 1.2x average.
    """
    if len(daily_df) < 50:
        return None
    try:
        latest = daily_df.iloc[-1]
        ema_20 = latest.get('ema_20')
        ema_50 = latest.get('ema_50')
        ema_200 = latest.get('ema_200')

        if pd.isna(ema_20) or pd.isna(ema_50) or pd.isna(ema_200):
            return None

        current_close = latest['close']
        current_low = latest['low']
        current_high = latest['high']

        # Strong uptrend: EMA20 > EMA50 * 1.05 > EMA200
        if not (ema_20 > ema_50 * 1.05 and ema_50 > ema_200):
            return None

        # Intraday touch of EMA20
        if current_low > ema_20:
            return None

        # Close recovered above EMA20
        if current_close <= ema_20:
            return None

        # Close in upper 50% of day's range
        day_range = current_high - current_low
        close_position = (current_close - current_low) / day_range if day_range > 0 else 0
        if close_position < 0.5:
            return None

        avg_volume = daily_df['volume'].tail(20).mean()
        volume_ratio = latest['volume'] / avg_volume if avg_volume > 0 else 0
        if volume_ratio < 1.2:
            return None

        rsi_14 = latest.get('rsi_14')
        distance_from_ema20 = ((current_close - ema_20) / ema_20) * 100

        return {
            'signal_type': 'pullback_bounce',
            'signal_strength': 'strong' if volume_ratio >= 1.5 else 'regular',
            'ema_20': round(ema_20, 2),
            'ema_50': round(ema_50, 2),
            'ema_200': round(ema_200, 2),
            'touch_low': round(current_low, 2),
            'recovery_close': round(current_close, 2),
            'distance_from_ema20': round(distance_from_ema20, 2),
            'volume_ratio': round(volume_ratio, 2),
            'close_position_in_range': round(close_position * 100, 1),
            'rsi_14': round(rsi_14, 2) if pd.notna(rsi_14) else None
        }
    except Exception:
        return None

# ============================================
# BULL FLAG & VCP HELPERS
# ============================================

def find_contractions(df: pd.DataFrame, lookback: int = 80) -> list:
    """
    Identify price contractions (local high-to-low swings) in the last `lookback` rows.
    Returns list of dicts: {high, low, depth_pct, avg_volume, start_idx, end_idx}
    """
    window = df.iloc[-lookback:].reset_index(drop=True)
    highs = window['high'].values
    lows  = window['low'].values
    vols  = window['volume'].values
    n     = len(window)

    contractions = []
    i = 0
    while i < n - 5:
        # Find local high
        local_high = highs[i]
        high_idx   = i
        for j in range(i + 1, min(i + 20, n)):
            if highs[j] > local_high:
                local_high = highs[j]
                high_idx   = j
            elif highs[j] < local_high * 0.97:
                break

        # Find subsequent low
        local_low = lows[high_idx]
        low_idx   = high_idx
        for j in range(high_idx + 1, min(high_idx + 30, n)):
            if lows[j] < local_low:
                local_low = lows[j]
                low_idx   = j
            elif lows[j] > local_low * 1.05:
                break

        if low_idx > high_idx and local_high > 0:
            depth_pct = (local_high - local_low) / local_high * 100
            avg_vol   = float(np.mean(vols[high_idx:low_idx + 1]))
            if depth_pct > 2.0:
                contractions.append({
                    'high':       local_high,
                    'low':        local_low,
                    'depth_pct':  round(depth_pct, 1),
                    'avg_volume': avg_vol,
                    'start_idx':  high_idx,
                    'end_idx':    low_idx,
                })
        i = low_idx + 1 if low_idx > i else i + 1

    return contractions


def check_bull_flag(daily_df: pd.DataFrame, current_price: float) -> Optional[Dict]:
    """
    Bull Flag — momentum continuation pattern.

    POLE: stock rose 15%+ in 5-15 days on volume > 2x avg
    FLAG: 3-15 day shallow consolidation (≤ 50% retrace), volume drying to < 70% of pole vol
    EMA stack: EMA20 > EMA50 > EMA200
    52W proximity: within 10% of high
    RS rank ≤ 125

    Strong: pole ≥ 20%, pole vol ≥ 3x, retrace ≤ 35%, RS rank ≤ 75
    Buy:    pole ≥ 15%, pole vol ≥ 2x, retrace ≤ 50%, RS rank ≤ 125
    """
    if len(daily_df) < 40:
        return None
    try:
        latest   = daily_df.iloc[-1]
        e20      = latest.get('ema_20')
        e50      = latest.get('ema_50')
        e200     = latest.get('ema_200')
        high_52w = latest.get('high_52w')
        rs_rank  = latest.get('rs_rank')

        if pd.isna(e20) or pd.isna(e50) or pd.isna(e200):
            return None
        if not (e20 > e50 > e200):
            return None
        if pd.isna(high_52w) or high_52w <= 0:
            return None
        dist_52w = (current_price - high_52w) / high_52w * 100
        if dist_52w < -10.0:
            return None

        close  = daily_df['close'].values
        volume = daily_df['volume'].values
        n      = len(daily_df)

        # Volume baseline: 20-day avg excluding last 10 days (avoid pole/flag contamination)
        vol_baseline = float(np.nanmean(volume[-30:-10])) if len(volume) > 30 else float(np.nanmean(volume))
        if vol_baseline <= 0:
            return None

        # Search flag (3-15 days) then pole (5-15 days) before it
        for flag_days in range(3, min(16, n - 5)):
            flag_s     = daily_df.iloc[-flag_days:]
            flag_high  = flag_s['high'].max()
            flag_low   = flag_s['low'].min()
            flag_close = flag_s.iloc[-1]['close']
            flag_vol   = float(flag_s['volume'].mean())

            # Flag volume must be drying up
            if flag_vol >= 0.70 * vol_baseline:
                continue

            # Price above EMA20 throughout flag (2% tolerance)
            if flag_low < flag_s['ema_20'].min() * 0.98:
                continue

            # Today's close in upper 40% of flag range
            flag_range = flag_high - flag_low
            if flag_range <= 0:
                continue
            close_pos = (flag_close - flag_low) / flag_range
            if close_pos < 0.40:
                continue

            # Search pole
            flag_start = n - flag_days
            for pole_days in range(5, min(16, flag_start)):
                pole_s     = daily_df.iloc[flag_start - pole_days:flag_start]
                pole_open  = float(pole_s.iloc[0]['close'])
                pole_close = float(pole_s.iloc[-1]['close'])
                if pole_open <= 0:
                    continue
                pole_gain = (pole_close - pole_open) / pole_open * 100
                if pole_gain < 15.0:
                    continue

                pole_vol = float(pole_s['volume'].mean())
                if pole_vol < 2.0 * vol_baseline:
                    continue

                pole_move    = pole_close - pole_open
                flag_retrace = (pole_close - flag_low) / pole_move * 100 if pole_move > 0 else 999
                if flag_retrace > 50.0:
                    continue

                # Grade
                in_top_rs = (pd.isna(rs_rank) or int(rs_rank) <= 75)
                if (pole_gain >= 20.0 and pole_vol >= 3.0 * vol_baseline
                        and flag_retrace <= 35.0 and in_top_rs):
                    signal_strength = 'strong'
                    signal_type     = 'bull_flag_strong'
                else:
                    signal_strength = 'regular'
                    signal_type     = 'bull_flag_buy'

                return {
                    'signal_type':       signal_type,
                    'signal_strength':   signal_strength,
                    'pole_days':         pole_days,
                    'pole_gain_pct':     round(pole_gain, 1),
                    'pole_vol_ratio':    round(pole_vol / vol_baseline, 2),
                    'flag_days':         flag_days,
                    'flag_retrace_pct':  round(flag_retrace, 1),
                    'flag_vol_ratio':    round(flag_vol / vol_baseline, 2),
                    'flag_high':         round(flag_high, 2),
                    'flag_low':          round(flag_low, 2),
                    'close_pos_in_flag': round(close_pos * 100, 0),
                    'dist_from_52w':     round(dist_52w, 1),
                    'ema_20':            round(e20, 2),
                    'ema_50':            round(e50, 2),
                }
    except Exception:
        return None
    return None


def check_vcp(daily_df: pd.DataFrame, current_price: float) -> Optional[Dict]:
    """
    VCP (Volatility Contraction Pattern) — Minervini's setup.

    Stage 2 uptrend: above EMA50 > EMA200, EMA20 > EMA50
    EMA50 rising for 4 weeks
    ≥ 3 progressively tighter contractions (each ≥ 25% tighter than prior)
    Latest contraction depth < 15%
    Price within 5% of contraction high (pivot)
    Current volume < 60% of 50-day avg (dry)
    Within 25% of 52W high

    Strong: ≥ 4 contractions, latest depth < 10%, vol < 50% of avg, RS rank ≤ 75
    Buy:    ≥ 3 contractions, latest depth < 15%, vol < 60% of avg, RS rank ≤ 125
    """
    if len(daily_df) < 80:
        return None
    try:
        latest   = daily_df.iloc[-1]
        e20      = latest.get('ema_20')
        e50      = latest.get('ema_50')
        e200     = latest.get('ema_200')
        high_52w = latest.get('high_52w')
        rs_rank  = latest.get('rs_rank')

        if pd.isna(e20) or pd.isna(e50) or pd.isna(e200):
            return None

        # Stage 2: above EMA50>EMA200 with EMA20>EMA50
        if not (current_price > e50 and e50 > e200 and e20 > e50):
            return None

        # EMA50 rising for 4 weeks
        if len(daily_df) >= 21:
            old_e50 = daily_df.iloc[-20].get('ema_50')
            if pd.notna(old_e50) and e50 <= old_e50:
                return None

        # 52W proximity
        if pd.isna(high_52w) or high_52w <= 0:
            return None
        dist_52w = (current_price - high_52w) / high_52w * 100
        if dist_52w < -25.0:
            return None

        # Volume dry-up
        vol_50_avg  = float(daily_df['volume'].tail(50).mean())
        current_vol = float(latest['volume'])
        vol_ratio   = current_vol / vol_50_avg if vol_50_avg > 0 else 1.0
        if vol_ratio > 0.60:
            return None

        # Find contractions
        contractions = find_contractions(daily_df, lookback=80)
        if len(contractions) < 3:
            return None

        # Build valid progressively tighter sequence
        valid_seq = []
        for c in contractions:
            if not valid_seq:
                valid_seq.append(c)
            else:
                prev = valid_seq[-1]
                if (c['depth_pct'] <= prev['depth_pct'] * 0.75 and
                        c['low'] >= prev['low'] * 0.98):
                    valid_seq.append(c)
                else:
                    if len(valid_seq) >= 3:
                        break
                    valid_seq = [c]

        if len(valid_seq) < 3:
            return None

        latest_c = valid_seq[-1]
        pivot    = latest_c['high']
        dist_pivot = (current_price - pivot) / pivot * 100

        # Price within 5% below pivot (or up to 2% above — breakout in progress)
        if dist_pivot < -5.0 or dist_pivot > 2.0:
            return None

        # Grade
        in_top_rs = (pd.isna(rs_rank) or int(rs_rank) <= 75)
        if (len(valid_seq) >= 4 and latest_c['depth_pct'] < 10.0
                and vol_ratio < 0.50 and in_top_rs):
            signal_strength = 'strong'
            signal_type     = 'vcp_strong'
        else:
            signal_strength = 'regular'
            signal_type     = 'vcp_buy'

        vols_in_seq = [c['avg_volume'] for c in valid_seq]
        vol_contracting = all(vols_in_seq[i] >= vols_in_seq[i + 1] * 0.90
                              for i in range(len(vols_in_seq) - 1))

        return {
            'signal_type':          signal_type,
            'signal_strength':      signal_strength,
            'num_contractions':     len(valid_seq),
            'contraction_depths':   [c['depth_pct'] for c in valid_seq],
            'latest_depth_pct':     latest_c['depth_pct'],
            'pivot_level':          round(pivot, 2),
            'dist_from_pivot_pct':  round(dist_pivot, 1),
            'dist_from_52w_pct':    round(dist_52w, 1),
            'current_vol_ratio':    round(vol_ratio, 2),
            'vol_contracting':      vol_contracting,
            'ema_20':               round(e20, 2),
            'ema_50':               round(e50, 2),
            'ema_200':              round(e200, 2),
        }
    except Exception:
        return None


# ============================================
# SCAN SINGLE STOCK
# ============================================

def scan_stock(supabase: Client, ticker: str) -> List[Dict]:
    """
    Scan a single stock for all active signals.
    Applies Alkalyme RS rank filter (top 25% = rank <= 125) per Portfolio A.
    Exception: Darvas Box is NOT exempt (only 3-C is exempt in Portfolio A,
    and we haven't implemented 3-C yet).
    """
    try:
        daily_df = fetch_stock_data_from_supabase(supabase, ticker, days=365)
        if daily_df is None or len(daily_df) < 60:
            return []

        current_price = daily_df.iloc[-1]['close']
        stock_name = get_stock_name(ticker)

        # ── Alkalyme RS rank filter ───────────────────────────────────────
        # Only run signals if stock is in top 25% by RS rank (rank 1-125)
        # rs_rank = None means data not yet available — allow through
        rs_rank = daily_df.iloc[-1].get('rs_rank')
        in_top_25_pct = (rs_rank is None or pd.isna(rs_rank) or int(rs_rank) <= 125)

        signals = []

        checks = [
            check_narrow_cpr_signal,
            check_blue_zone_signal,
            check_golden_cross,
            check_macd_signal,
            check_darvas_box,
            check_pullback_bounce,
            check_bull_flag,
            check_vcp,
        ]

        for check_fn in checks:
            # Skip RS filter for Narrow CPR (intraday, not a breakout pattern)
            # All other patterns require top 25% RS rank
            if not in_top_25_pct and check_fn != check_narrow_cpr_signal:
                continue
            result = check_fn(daily_df, current_price)
            if result:
                result['ticker'] = ticker
                result['stock_name'] = stock_name
                result['price'] = round(current_price, 2)
                result['rs_rank'] = int(rs_rank) if rs_rank and not pd.isna(rs_rank) else None
                signals.append(result)

        return signals

    except Exception:
        return []

# ============================================
# SAVE TO SUPABASE
# ============================================

def save_signals_to_supabase(supabase: Client, signals: List[Dict]) -> bool:
    try:
        if not signals:
            print("No signals to save")
            return True

        today = str(datetime.now().date())
        print(f"🗑️  Deleting existing signals for {today}...")
        supabase.table('entry_signals').delete().eq('alert_date', today).execute()

        records = []
        for signal in signals:
            ticker = signal.pop('ticker')
            stock_name = signal.pop('stock_name')
            price = signal.pop('price')
            signal_type = signal.pop('signal_type')
            signal_strength = signal.pop('signal_strength')

            details = {}
            for key, value in signal.items():
                if isinstance(value, bool):
                    details[key] = value
                elif isinstance(value, (np.bool_, np.integer, np.floating)):
                    details[key] = value.item()
                elif pd.isna(value) if not isinstance(value, (bool, str, list, dict)) else False:
                    details[key] = None
                else:
                    details[key] = value

            records.append({
                'ticker': ticker,
                'stock_name': stock_name,
                'signal_type': signal_type,
                'signal_strength': signal_strength,
                'price': price,
                'alert_date': today,
                'details': details
            })

        print(f"💾 Inserting {len(records)} signals...")
        success, failed = 0, 0
        for record in records:
            try:
                supabase.table('entry_signals').insert(record).execute()
                success += 1
            except Exception as row_err:
                print(f"  ⚠️  Skipped {record.get('ticker')} ({record.get('signal_type')}): {row_err}")
                failed += 1
        print(f"✅ Saved {success} signals" + (f", skipped {failed}" if failed else ""))
        return True

    except Exception as e:
        print(f"❌ Failed to save signals: {e}")
        return False

# ============================================
# MAIN
# ============================================

def main():
    print("=" * 80)
    print("🔍 ENTRY SIGNALS SCANNER")
    print("=" * 80)
    print(f"Signals: Narrow CPR | Blue Zone | Golden Cross | MACD | Darvas Box | Pullback Bounce | Bull Flag | VCP")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    supabase = init_supabase()
    if not supabase:
        print("\n❌ Cannot proceed without Supabase connection")
        return

    # Get latest date and all tickers
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

    # ── Stale data guard ──────────────────────────────────────────────────────
    # If latest snapshot is not today, warn. This happens when the OHLC fetcher
    # runs before Zerodha has finalized today's historical candle.
    today_str = str(datetime.now().date())
    if latest_date != today_str:
        print(f"\n⚠️  WARNING: Latest snapshot date ({latest_date}) is NOT today ({today_str}).")
        print(f"   Zerodha may not have finalized today's historical data yet.")
        print(f"   Scanning with {latest_date} data — signals will use yesterday's prices.")
        print(f"   Consider running this workflow later (5:30 PM IST minimum).\n")
    # ─────────────────────────────────────────────────────────────────────────

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
        print(f"  Batch {offset//batch_size + 1}: {len(batch_tickers)} tickers")
        if len(response.data) < batch_size:
            break
        offset += batch_size

    tickers = sorted(list(set(all_tickers)))
    print(f"✅ Found {len(tickers)} unique stocks")

    # Scan
    print(f"\n🔍 Scanning {len(tickers)} stocks...")
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
        'darvas_box': 0,
        'pullback_bounce': 0,
        'bull_flag_strong': 0,
        'bull_flag_buy': 0,
        'vcp_strong': 0,
        'vcp_buy': 0,
    }

    emoji_map = {
        'narrow_cpr_breakaway': '📍',
        'blue_zone_strong': '🟢🟢',
        'blue_zone_buy': '🟢',
        'golden_cross_strong': '⚡⚡',
        'golden_cross_buy': '⚡',
        'macd_strong': '📊📊',
        'macd_buy': '📊',
        'darvas_box': '📦',
        'pullback_bounce': '🔄',
        'bull_flag_strong': '🚩🚩',
        'bull_flag_buy': '🚩',
        'vcp_strong': '📉📉',
        'vcp_buy': '📉',
    }

    label_map = {
        'narrow_cpr_breakaway': 'CPR',
        'blue_zone_strong': 'BLUE ZONE STRONG',
        'blue_zone_buy': 'BLUE ZONE BUY',
        'golden_cross_strong': 'GC STRONG',
        'golden_cross_buy': 'GC BUY',
        'macd_strong': 'MACD STRONG',
        'macd_buy': 'MACD BUY',
        'darvas_box': 'DARVAS BOX',
        'pullback_bounce': 'PULLBACK BOUNCE',
        'bull_flag_strong': 'BULL FLAG STRONG',
        'bull_flag_buy': 'BULL FLAG BUY',
        'vcp_strong': 'VCP STRONG',
        'vcp_buy': 'VCP BUY',
    }

    for i, ticker in enumerate(tickers, 1):
        if i % 50 == 0:
            print(f"\n[{i}/{len(tickers)}] {(i/len(tickers)*100):.1f}% — "
                  f"CPR:{signal_counts['narrow_cpr_breakaway']} "
                  f"BZ:{signal_counts['blue_zone_strong']+signal_counts['blue_zone_buy']} "
                  f"GC:{signal_counts['golden_cross_strong']+signal_counts['golden_cross_buy']} "
                  f"MACD:{signal_counts['macd_strong']+signal_counts['macd_buy']} "
                  f"Darvas:{signal_counts['darvas_box']} "
                  f"PB:{signal_counts['pullback_bounce']} "
                  f"BF:{signal_counts['bull_flag_strong']+signal_counts['bull_flag_buy']} "
                  f"VCP:{signal_counts['vcp_strong']+signal_counts['vcp_buy']}")

        for signal in scan_stock(supabase, ticker):
            all_signals.append(signal)
            st = signal['signal_type']
            if st in signal_counts:
                signal_counts[st] += 1
            print(f"  {emoji_map.get(st,'📊')} {ticker:20} - {label_map.get(st, st)}")

        time.sleep(0.1)

    # Save
    if all_signals:
        print(f"\n💾 Saving {len(all_signals)} signals...")
        save_signals_to_supabase(supabase, all_signals)

    # Summary
    print("\n" + "=" * 80)
    print("📈 SUMMARY")
    print("=" * 80)
    print(f"✅ Stocks scanned: {len(tickers)}")
    print(f"\n📊 Signals Found:")
    print(f"  📍 Narrow CPR:     {signal_counts['narrow_cpr_breakaway']} (no RS filter)")
    print(f"  🟢 Blue Zone:      {signal_counts['blue_zone_strong']} Strong + {signal_counts['blue_zone_buy']} Buy")
    print(f"  ⚡ Golden Cross:   {signal_counts['golden_cross_strong']} Strong + {signal_counts['golden_cross_buy']} Buy")
    print(f"  📊 MACD:           {signal_counts['macd_strong']} Strong + {signal_counts['macd_buy']} Buy")
    print(f"  📦 Darvas Box:     {signal_counts['darvas_box']}")
    print(f"  🔄 Pullback Bounce:{signal_counts['pullback_bounce']}")
    print(f"  🚩 Bull Flag:      {signal_counts['bull_flag_strong']} Strong + {signal_counts['bull_flag_buy']} Buy")
    print(f"  📉 VCP:            {signal_counts['vcp_strong']} Strong + {signal_counts['vcp_buy']} Buy")
    print(f"  📍 Total:          {len(all_signals)}")
    print(f"  🔍 RS filter:      top 25% only (rank ≤ 125) — signals above are post-filter")
    print(f"\nCompleted: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)


if __name__ == "__main__":
    main()
