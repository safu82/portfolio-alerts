#!/usr/bin/env python3
"""
Integrated Portfolio Alert Scanner - ZERODHA VERSION
=====================================================
Scans portfolio holdings for technical alerts that power the Holdings tab badges.

Writes to:
  - `alerts` table (status='NEW', portfolio='INDIAN')
  - `earnings_calendar` table (persistent earnings dates + post-declaration results)
  - `stock_fundamentals` table (updated quarterly data post-earnings)

Reads from: `holdings` table + `daily_stock_snapshots` (Zerodha data)

Active scans:
1. EMA Crossover (20/50) — Golden Cross / Death Cross
2. Promoter Buying/Selling — Trendlyne scrape
3. Volume Breakout — 2x average + 3% price move
4. Blue Zone — Daily RSI EMA(9) >= 72 Strong / 65 Buy, Weekly >= 65 / 55
5. Quarterly Results — Trendlyne earnings calendar (30-day window → earnings_calendar)
6. Post-Earnings Detection — Screener fetch + Haiku commentary + PDF analysis

Thresholds synced with PRODUCTION_entry_signals_scanner.py (April 2026)
"""

import os
import re
import json
import base64
import requests
from datetime import datetime, timedelta
from supabase import create_client, Client
import pandas as pd
import numpy as np
from bs4 import BeautifulSoup
import warnings
warnings.filterwarnings('ignore')

# =============================================================================
# CONFIGURATION
# =============================================================================

SUPABASE_URL    = os.getenv('SUPABASE_URL',    'https://hcgyncghmcvylnrmcivj.supabase.co')
SUPABASE_KEY    = os.getenv('SUPABASE_KEY',    'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhjZ3luY2dobWN2eWxucm1jaXZqIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTc1MTQwMTEsImV4cCI6MjA3MzA5MDAxMX0.n8vFVCJe1y_3o8fpAY0IgasZ4eKl7DAogEM3OlHB8Ww')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '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')

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

EMA_CONFIG = {
    'short_ema': 20,
    'long_ema': 50,
    'filter_ema': 200,
    'lookback_days': 5,
    'adx_min_strong': 25,
    'adx_min_buy': 20,
}

VOLUME_CONFIG = {
    'multiplier': 2.0,
    'avg_period': 20,
    'min_price_change': 3.0,
}

BLUE_ZONE_CONFIG = {
    'daily_rsi_strong': 72,
    'weekly_rsi_strong': 65,
    'daily_rsi_buy': 65,
    'weekly_rsi_buy': 55,
    'max_pct_from_high': 10,
}

# =============================================================================
# DATA FETCHING
# =============================================================================

def fetch_ohlc_from_supabase(ticker: str, days: int = 365) -> pd.DataFrame:
    try:
        start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        if not ticker.endswith('.NS') and not ticker.endswith('.BO'):
            ticker = f"{ticker}.NS"
        response = supabase.table('daily_stock_snapshots')\
            .select('*')\
            .eq('ticker', ticker)\
            .gte('snapshot_date', start_date)\
            .order('snapshot_date')\
            .execute()
        if not response.data:
            return pd.DataFrame()
        df = pd.DataFrame(response.data)
        df['snapshot_date'] = pd.to_datetime(df['snapshot_date'])
        df.set_index('snapshot_date', inplace=True)
        df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low',
                            'close': 'Close', 'volume': 'Volume'}, inplace=True)
        return df
    except Exception as e:
        print(f"  ⚠️ Error fetching {ticker}: {e}")
        return pd.DataFrame()


def get_portfolio_stocks():
    try:
        response = supabase.table('holdings')\
            .select('ticker, name, sector')\
            .eq('portfolio', 'INDIAN')\
            .gt('quantity', 0)\
            .execute()
        stocks = []
        for stock in response.data:
            ticker_raw = stock['ticker']
            symbol = ticker_raw.replace('.NS', '').replace('.BO', '')
            stocks.append({
                'ticker': symbol,
                'symbol': symbol,
                'full_ticker': ticker_raw,
                'name': stock['name'],
                'portfolio': 'INDIAN'
            })
        return stocks
    except Exception as e:
        print(f"  ❌ Error fetching portfolio: {e}")
        return []

# =============================================================================
# TECHNICAL INDICATOR HELPERS
# =============================================================================

def calculate_ema(prices, period):
    return prices.ewm(span=period, adjust=False).mean()


def calculate_rsi(prices, period=14):
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    high, low, close = df['High'], df['Low'], df['Close']
    high_low = high - low
    high_close = np.abs(high - close.shift())
    low_close = np.abs(low - close.shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm[plus_dm < 0] = 0
    minus_dm[minus_dm < 0] = 0
    plus_dm[plus_dm < minus_dm] = 0
    minus_dm[minus_dm < plus_dm] = 0
    atr = tr.ewm(alpha=1/period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr)
    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di)
    df['adx'] = dx.ewm(alpha=1/period, adjust=False).mean()
    df['plus_di'] = plus_di
    df['minus_di'] = minus_di
    return df

# =============================================================================
# SCAN: EMA CROSSOVER
# =============================================================================

def scan_ema_crossover(stock):
    ticker = stock['full_ticker']
    symbol = stock['symbol']
    name   = stock['name']
    try:
        hist = fetch_ohlc_from_supabase(ticker, days=365)
        if hist.empty or len(hist) < 60:
            return None
        hist = calculate_adx(hist)
        if 'ema_20' in hist.columns and 'ema_50' in hist.columns:
            hist['short_ema'] = hist['ema_20']
            hist['long_ema']  = hist['ema_50']
            hist['filter_ema'] = hist.get('ema_200', hist['Close'])
        else:
            hist['short_ema']  = calculate_ema(hist['Close'], 20)
            hist['long_ema']   = calculate_ema(hist['Close'], 50)
            hist['filter_ema'] = calculate_ema(hist['Close'], 200)
        hist['volume_avg'] = hist['Volume'].rolling(window=20).mean()
        crossover = None
        for i in range(1, min(EMA_CONFIG['lookback_days'] + 1, len(hist))):
            curr = hist.iloc[-i]
            prev = hist.iloc[-(i+1)]
            if pd.isna([curr['short_ema'], curr['long_ema'],
                        prev['short_ema'], prev['long_ema']]).any():
                continue
            if prev['short_ema'] <= prev['long_ema'] and curr['short_ema'] > curr['long_ema']:
                crossover = {'type': 'bullish', 'days_ago': i - 1, 'row': curr}
                break
            if prev['short_ema'] >= prev['long_ema'] and curr['short_ema'] < curr['long_ema']:
                crossover = {'type': 'bearish', 'days_ago': i - 1, 'row': curr}
                break
        if not crossover:
            return None
        latest = hist.iloc[-1]
        cmp = latest['Close']
        adx_value  = float(latest['adx'])    if pd.notna(latest['adx'])    else 0
        plus_di    = float(latest['plus_di']) if pd.notna(latest['plus_di']) else 0
        minus_di   = float(latest['minus_di']) if pd.notna(latest['minus_di']) else 0
        avg_volume = latest['volume_avg']
        volume_ratio = float(latest['Volume'] / avg_volume) if avg_volume > 0 else 0
        crossover_type = crossover['type']
        ema_20  = round(float(latest['short_ema']),  2)
        ema_50  = round(float(latest['long_ema']),   2)
        ema_200 = round(float(latest['filter_ema']), 2) if pd.notna(latest['filter_ema']) else None
        return {
            'portfolio':         'INDIAN',
            'ticker':            symbol,
            'stock_name':        name,
            'alert_type':        'ema_golden_cross' if crossover_type == 'bullish' else 'ema_death_cross',
            'alert_category':    'BULLISH' if crossover_type == 'bullish' else 'BEARISH',
            'alert_title':       f"{name} - {'Golden' if crossover_type == 'bullish' else 'Death'} Cross (20/50 EMA)",
            'alert_description': (
                f"20 EMA crossed {'above' if crossover_type == 'bullish' else 'below'} 50 EMA "
                f"{crossover['days_ago']} days ago | ADX: {adx_value:.1f} | Vol: {volume_ratio:.1f}x"
            ),
            'price':      round(float(cmp), 2),
            'alert_date': datetime.now().date().isoformat(),
            'details': {
                'ema_20': ema_20, 'ema_50': ema_50, 'ema_200': ema_200,
                'days_ago': crossover['days_ago'], 'adx': round(adx_value, 2),
                'plus_di': round(plus_di, 2), 'minus_di': round(minus_di, 2),
                'volume_ratio': round(volume_ratio, 2),
                'adx_confirmed': bool(adx_value >= EMA_CONFIG['adx_min_buy'])
            }
        }
    except Exception as e:
        print(f"  ❌ EMA scan error {symbol}: {e}")
        return None

# =============================================================================
# SCAN: PROMOTER BUYING
# =============================================================================

def get_recent_promoter_transactions():
    url = "https://trendlyne.com/equity/group-insider-trading-sast/"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 200:
            return parse_promoter_transactions(response.text)
        print(f"  ⚠️ Trendlyne returned {response.status_code}")
        return []
    except Exception as e:
        print(f"  ❌ Promoter fetch error: {e}")
        return []


def parse_promoter_transactions(html):
    soup = BeautifulSoup(html, 'html.parser')
    transactions = []
    for row in soup.find_all('tr'):
        cols = row.find_all('td')
        if len(cols) < 7:
            continue
        try:
            company_link = cols[0].find('a')
            if not company_link:
                continue
            company_name = company_link.text.strip()
            company_url  = company_link.get('href', '')
            symbol_match = re.search(r'/([A-Z0-9]+)/', company_url)
            symbol       = symbol_match.group(1) if symbol_match else ''
            person_link  = cols[1].find('a')
            person       = person_link.text.strip() if person_link else cols[1].text.strip()
            category         = cols[2].text.strip()
            transaction_type = cols[3].text.strip()
            date             = cols[4].text.strip()
            shares_text      = cols[5].text.strip().replace(',', '')
            try:
                shares = int(shares_text) if shares_text and shares_text != '-' else 0
            except:
                shares = 0
            holding_text = cols[6].text.strip()
            pct_change   = cols[7].text.strip() if len(cols) > 7 else ''
            if 'promoter' not in category.lower():
                continue
            transactions.append({
                'company_name': company_name, 'symbol': symbol,
                'person_entity': person, 'category': category,
                'transaction_type': transaction_type, 'date': date,
                'shares': shares, 'post_holding': holding_text, 'pct_change': pct_change,
            })
        except:
            continue
    return transactions


def scan_promoter_buying(portfolio_stocks):
    all_transactions = get_recent_promoter_transactions()
    if not all_transactions:
        print("  ⚠️ No promoter transactions found")
        return []
    portfolio_symbols    = {stock['symbol'].upper() for stock in portfolio_stocks}
    portfolio_transactions = [t for t in all_transactions if t['symbol'].upper() in portfolio_symbols]
    print(f"  ✅ Found {len(portfolio_transactions)} promoter transactions in portfolio")
    alerts = []
    for txn in portfolio_transactions:
        is_buying  = 'acquisition' in txn['transaction_type'].lower() or 'purchase' in txn['transaction_type'].lower()
        is_selling = 'disposal'    in txn['transaction_type'].lower() or 'sale'     in txn['transaction_type'].lower()
        if not (is_buying or is_selling):
            continue
        stock_name = next(
            (s['name'] for s in portfolio_stocks if s['symbol'].upper() == txn['symbol'].upper()),
            txn['company_name']
        )
        try:
            alert_date = datetime.strptime(txn['date'], "%d %b %Y").date().isoformat()
        except:
            alert_date = datetime.now().date().isoformat()
        alerts.append({
            'portfolio': 'INDIAN', 'ticker': txn['symbol'], 'stock_name': stock_name,
            'alert_type':        'promoter_buying' if is_buying else 'promoter_selling',
            'alert_category':    'BULLISH' if is_buying else 'BEARISH',
            'alert_title':       f"{stock_name} - Promoter {'Buying' if is_buying else 'Selling'}",
            'alert_description': f"{txn['person_entity'][:50]} - {txn['transaction_type']}",
            'price': None, 'alert_date': alert_date,
            'details': {
                'person_entity': txn['person_entity'], 'transaction_type': txn['transaction_type'],
                'shares': txn['shares'], 'post_holding': txn['post_holding'],
                'pct_change': txn['pct_change'], 'category': txn['category']
            }
        })
    return alerts

# =============================================================================
# SCAN: VOLUME BREAKOUT
# =============================================================================

def scan_volume_breakout(stock):
    ticker = stock['full_ticker']
    symbol = stock['symbol']
    name   = stock['name']
    try:
        hist = fetch_ohlc_from_supabase(ticker, days=365)
        if hist.empty or len(hist) < VOLUME_CONFIG['avg_period']:
            return None
        hist['volume_avg']   = hist['Volume'].rolling(window=VOLUME_CONFIG['avg_period']).mean()
        current_volume       = hist['Volume'].iloc[-1]
        avg_volume           = hist['volume_avg'].iloc[-1]
        current_price        = hist['Close'].iloc[-1]
        prev_price           = hist['Close'].iloc[-2]
        if pd.isna(avg_volume) or avg_volume == 0:
            return None
        volume_ratio = current_volume / avg_volume
        if volume_ratio < VOLUME_CONFIG['multiplier']:
            return None
        price_change_pct = ((current_price - prev_price) / prev_price) * 100
        if abs(price_change_pct) < VOLUME_CONFIG['min_price_change']:
            return None
        is_bullish = price_change_pct > 0
        return {
            'portfolio': 'INDIAN', 'ticker': symbol, 'stock_name': name,
            'alert_type':        'volume_breakout_bullish' if is_bullish else 'volume_breakout_bearish',
            'alert_category':    'BULLISH' if is_bullish else 'BEARISH',
            'alert_title':       f"{name} - Volume Breakout ({'Bullish' if is_bullish else 'Bearish'})",
            'alert_description': f"Volume {volume_ratio:.1f}x average with {abs(price_change_pct):.1f}% {'gain' if is_bullish else 'loss'}",
            'price':      round(float(current_price), 2),
            'alert_date': datetime.now().date().isoformat(),
            'details': {
                'current_volume': int(current_volume), 'avg_volume': int(avg_volume),
                'volume_ratio':   round(float(volume_ratio), 2),
                'price_change_pct': round(float(price_change_pct), 2),
                'direction': 'up' if is_bullish else 'down'
            }
        }
    except Exception as e:
        print(f"  ❌ Volume scan error {symbol}: {e}")
        return None

# =============================================================================
# SCAN: BLUE ZONE
# =============================================================================

def scan_blue_zone_stocks(stock):
    ticker = stock['full_ticker']
    symbol = stock['symbol']
    name   = stock['name']
    try:
        hist = fetch_ohlc_from_supabase(ticker, days=365)
        if hist.empty or len(hist) < 50:
            return None
        latest           = hist.iloc[-1]
        daily_rsi_ema_9  = latest.get('rsi_ema_9')
        weekly_rsi_ema_9 = latest.get('weekly_rsi_ema_9')
        ema_20           = latest.get('ema_20')
        ema_50           = latest.get('ema_50')
        current_price    = float(latest['Close'])
        if pd.isna(daily_rsi_ema_9) or pd.isna(weekly_rsi_ema_9):
            return None
        high_52w = latest.get('high_52w')
        if pd.isna(high_52w) or high_52w is None:
            high_52w = float(hist['High'].max())
        else:
            high_52w = float(high_52w)
        distance_from_high = ((current_price - high_52w) / high_52w) * 100
        if distance_from_high < -BLUE_ZONE_CONFIG['max_pct_from_high']:
            return None
        avg_volume   = hist['Volume'].tail(20).mean()
        volume_ratio = float(latest['Volume'] / avg_volume) if avg_volume > 0 else 0
        above_ema_50 = current_price > float(ema_50) if pd.notna(ema_50) else False
        above_ema_20 = current_price > float(ema_20) if pd.notna(ema_20) else False
        if (daily_rsi_ema_9  >= BLUE_ZONE_CONFIG['daily_rsi_strong'] and
                weekly_rsi_ema_9 >= BLUE_ZONE_CONFIG['weekly_rsi_strong'] and
                above_ema_50 and volume_ratio > 1.5):
            grade      = 'Strong Buy'
            alert_type = 'blue_zone_stocks'
        elif (daily_rsi_ema_9  >= BLUE_ZONE_CONFIG['daily_rsi_buy'] and
              weekly_rsi_ema_9 >= BLUE_ZONE_CONFIG['weekly_rsi_buy'] and
              above_ema_20):
            grade      = 'Buy'
            alert_type = 'blue_zone_stocks'
        else:
            return None
        days_in_bz = 1
        for i in range(len(hist) - 2, -1, -1):
            row   = hist.iloc[i]
            d_rsi = row.get('rsi_ema_9')
            w_rsi = row.get('weekly_rsi_ema_9')
            if pd.isna(d_rsi) or pd.isna(w_rsi):
                break
            if (d_rsi >= BLUE_ZONE_CONFIG['daily_rsi_buy'] and
                    w_rsi >= BLUE_ZONE_CONFIG['weekly_rsi_buy']):
                days_in_bz += 1
            else:
                break
        return {
            'portfolio': 'INDIAN', 'ticker': symbol, 'stock_name': name,
            'alert_type':     alert_type,
            'alert_category': 'BULLISH',
            'alert_title':    f"{name} - Blue Zone ({grade})",
            'alert_description': (
                f"{grade}: Daily RSI: {daily_rsi_ema_9:.1f}, Weekly RSI: {weekly_rsi_ema_9:.1f}, "
                f"{abs(distance_from_high):.1f}% from 52W high \u2022 {days_in_bz} days in zone"
                + (f" + Vol {volume_ratio:.1f}x \U0001F525" if volume_ratio >= 1.5 else "")
            ),
            'price':      round(current_price, 2),
            'alert_date': datetime.now().date().isoformat(),
            'details': {
                'daily_rsi_ema_9':  round(float(daily_rsi_ema_9), 2),
                'weekly_rsi_ema_9': round(float(weekly_rsi_ema_9), 2),
                'grade': grade,
                'pct_from_52w_high': round(distance_from_high, 2),
                'above_ema_50': above_ema_50, 'above_ema_20': above_ema_20,
                'volume_ratio':  round(volume_ratio, 2),
                'days_in_blue_zone': days_in_bz,
                'daily_threshold':   BLUE_ZONE_CONFIG['daily_rsi_buy'],
                'weekly_threshold':  BLUE_ZONE_CONFIG['weekly_rsi_buy']
            }
        }
    except Exception as e:
        print(f"  ❌ Blue Zone scan error {symbol}: {e}")
        return None

# =============================================================================
# EARNINGS CALENDAR HELPERS
# =============================================================================

def upsert_earnings_calendar(ticker: str, stock_name: str,
                              earnings_date, quarter: str, source: str = 'trendlyne'):
    """Upsert upcoming earnings date to earnings_calendar (persistent, 30-day window)."""
    try:
        supabase.table('earnings_calendar').upsert({
            'ticker':        ticker,
            'stock_name':    stock_name,
            'earnings_date': earnings_date.isoformat() if hasattr(earnings_date, 'isoformat') else str(earnings_date),
            'quarter':       quarter,
            'source':        source,
            'updated_at':    datetime.now().isoformat(),
        }, on_conflict='ticker,earnings_date').execute()
        return True
    except Exception as e:
        print(f"  ⚠️ earnings_calendar upsert error for {ticker}: {e}")
        return False


def cleanup_old_earnings():
    """Remove earnings_calendar rows more than 7 days in the past."""
    try:
        cutoff = (datetime.now().date() - timedelta(days=7)).isoformat()
        supabase.table('earnings_calendar').delete().lt('earnings_date', cutoff).execute()
        print("  ✅ Cleaned up old earnings_calendar entries")
    except Exception as e:
        print(f"  ⚠️ earnings_calendar cleanup error: {e}")

# =============================================================================
# SCAN: QUARTERLY RESULTS (30-day window)
# =============================================================================

def scan_upcoming_results(stock):
    """
    Upcoming quarterly results from Trendlyne.
    - 30-day window → always upserts to earnings_calendar
    - 7-day window → also creates alert for Holdings tab badge
    """
    symbol      = stock['symbol']
    name        = stock['name']
    full_ticker = stock['full_ticker']
    try:
        url = f"https://trendlyne.com/equity/{symbol}/results-calendar/"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            return None
        soup = BeautifulSoup(response.content, 'html.parser')
        results_date = None
        for text_elem in soup.find_all(string=True):
            text = str(text_elem).strip().lower()
            if 'expected' in text or 'results' in text:
                parent = text_elem.parent
                if parent:
                    date_text = parent.get_text()
                    pattern = r'(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{4})'
                    match = re.search(pattern, date_text, re.IGNORECASE)
                    if match:
                        try:
                            from dateutil import parser as dateparser
                            results_date = dateparser.parse(date_text, fuzzy=True).date()
                            break
                        except:
                            continue
                if results_date:
                    break
        if not results_date:
            return None
        today     = datetime.now().date()
        days_until = (results_date - today).days
        if days_until < 0 or days_until > 30:
            return None
        current_month = datetime.now().month
        current_year  = datetime.now().year
        if current_month <= 3:
            quarter, fy_year = 'Q4', current_year
        elif current_month <= 6:
            quarter, fy_year = 'Q1', current_year + 1
        elif current_month <= 9:
            quarter, fy_year = 'Q2', current_year + 1
        else:
            quarter, fy_year = 'Q3', current_year + 1
        quarter_str = f"{quarter} FY{fy_year}"
        # Always upsert to earnings_calendar
        upsert_earnings_calendar(full_ticker, name, results_date, quarter_str)
        # Only create alert if within 7 days
        if days_until > 7:
            return None
        return {
            'portfolio': 'INDIAN', 'ticker': symbol, 'stock_name': name,
            'alert_type':     'quarterly_results',
            'alert_category': 'INFO',
            'alert_title':    f"{name} - Quarterly Results Upcoming",
            'alert_description': (
                f"Results expected {results_date.strftime('%b %d, %Y')} "
                f"(in {days_until} {'day' if days_until == 1 else 'days'})"
            ),
            'price': None, 'alert_date': today.isoformat(),
            'details': {
                'results_date': results_date.isoformat(),
                'days_until':   days_until,
                'quarter':      quarter_str,
                'priority':     'URGENT' if days_until <= 3 else 'UPCOMING'
            }
        }
    except Exception as e:
        print(f"  ❌ Results scan error {symbol}: {e}")
        return None

# =============================================================================
# POST-EARNINGS: SCREENER FETCH
# =============================================================================

def fetch_screener_quarterly(symbol: str) -> list:
    """Fetch latest quarterly financials from Screener. Returns newest-first."""
    for url in [
        f"https://www.screener.in/company/{symbol}/consolidated/",
        f"https://www.screener.in/company/{symbol}/"
    ]:
        try:
            resp = requests.get(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }, timeout=15)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, 'html.parser')
            quarterly_section = None
            for section in soup.find_all('section'):
                h2 = section.find('h2')
                if h2 and 'quarterly' in h2.text.lower():
                    quarterly_section = section
                    break
            if not quarterly_section:
                continue
            table = quarterly_section.find('table')
            if not table:
                continue
            thead = table.find('thead')
            if not thead:
                continue
            th_cells = thead.find_all('th')
            periods  = [th.text.strip() for th in th_cells[1:]]
            if not periods:
                continue
            quarters = {p: {'period': p} for p in periods}
            pdf_urls = {}
            tbody = table.find('tbody')
            if not tbody:
                continue
            for row in tbody.find_all('tr'):
                cells = row.find_all('td')
                if not cells:
                    continue
                label = cells[0].text.strip().lower()
                # Extract PDF URLs from Raw PDF row
                if 'raw' in label and 'pdf' in label:
                    for i, period in enumerate(periods):
                        if i + 1 < len(cells):
                            link = cells[i + 1].find('a')
                            if link and link.get('href'):
                                href = link['href']
                                if not href.startswith('http'):
                                    href = 'https://www.screener.in' + href
                                pdf_urls[period] = href
                    continue
                values = [
                    c.text.strip().replace(',', '').replace('%', '').replace('₹', '')
                    for c in cells[1:]
                ]
                for i, period in enumerate(periods):
                    if i >= len(values):
                        continue
                    try:
                        val = float(values[i]) if values[i] and values[i] not in ('-', '', 'N/A') else None
                    except:
                        val = None
                    if 'sales' in label or 'revenue' in label or 'net sales' in label:
                        quarters[period]['revenue'] = val
                    elif 'net profit' in label or 'profit after tax' in label or label.strip() == 'pat':
                        quarters[period]['net_profit'] = val
                    elif 'eps' in label and ('rs' in label or label.strip() == 'eps'):
                        quarters[period]['eps'] = val
            result = [quarters[p] for p in periods if quarters.get(p, {}).get('revenue') is not None]
            if result:
                result.reverse()  # newest first
                for q in result:
                    p = q.get('period')
                    if p and p in pdf_urls:
                        q['pdf_url'] = pdf_urls[p]
                return result
        except Exception as e:
            print(f"  ❌ Screener error at {url}: {e}")
            continue
    return []


def get_latest_quarter_in_db(ticker: str):
    """Get the most recent quarter label stored in stock_fundamentals."""
    try:
        resp = supabase.table('stock_fundamentals')\
            .select('quarterly_financials')\
            .eq('ticker', ticker)\
            .single()\
            .execute()
        if not resp.data or not resp.data.get('quarterly_financials'):
            return None
        qf = resp.data['quarterly_financials']
        if isinstance(qf, str):
            qf = json.loads(qf)
        return qf[0].get('period') if qf else None
    except:
        return None

# =============================================================================
# POST-EARNINGS: HAIKU COMMENTARY + PDF ANALYSIS
# =============================================================================

def generate_commentary(stock_name: str, quarter: str,
                         latest: dict, prev_quarter: dict, year_ago: dict) -> str:
    """Generate 2-3 sentence financial commentary using Claude Haiku."""
    if not ANTHROPIC_API_KEY:
        return ''
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        def fmt(v):
            return f"₹{v:,.0f} Cr" if v is not None else 'N/A'
        def yoy(cur, old):
            if cur is None or old is None or old == 0: return 'N/A'
            return f"{((cur - old) / abs(old) * 100):+.1f}%"

        prompt = f"""You are a concise financial analyst. Write 2-3 plain sentences (no markdown, no bullets, no headers).
Focus on: revenue trend, profitability, and one forward-looking observation. Be specific with numbers.

Company: {stock_name} | Quarter: {quarter}
Revenue:    {fmt(latest.get('revenue'))} | Prior Q: {fmt(prev_quarter.get('revenue'))} | Year Ago: {fmt(year_ago.get('revenue'))} | YoY: {yoy(latest.get('revenue'), year_ago.get('revenue'))}
Net Profit: {fmt(latest.get('net_profit'))} | Prior Q: {fmt(prev_quarter.get('net_profit'))} | Year Ago: {fmt(year_ago.get('net_profit'))} | YoY: {yoy(latest.get('net_profit'), year_ago.get('net_profit'))}
EPS:        ₹{latest.get('eps', 'N/A')} | Prior Q: ₹{prev_quarter.get('eps', 'N/A')} | Year Ago: ₹{year_ago.get('eps', 'N/A')}"""

        msg = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=150,
            messages=[{'role': 'user', 'content': prompt}]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        print(f"  ⚠️ Haiku commentary error: {e}")
        return ''


def fetch_pdf_insights(stock_name: str, quarter: str, pdf_url: str) -> str:
    """Download quarterly results PDF and extract key insights using Claude Haiku."""
    if not ANTHROPIC_API_KEY or not pdf_url:
        return ''
    try:
        import anthropic
        resp = requests.get(pdf_url, timeout=30, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        if resp.status_code != 200:
            print(f"  ⚠️ PDF download failed: {resp.status_code}")
            return ''
        pdf_b64 = base64.standard_b64encode(resp.content).decode('utf-8')
        print(f"  ✅ PDF downloaded ({len(resp.content)//1024} KB)")
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=350,
            messages=[{
                'role': 'user',
                'content': [
                    {
                        'type': 'document',
                        'source': {'type': 'base64', 'media_type': 'application/pdf', 'data': pdf_b64}
                    },
                    {
                        'type': 'text',
                        'text': f"""This is the quarterly results filing for {stock_name} ({quarter}).
In 2-3 plain sentences only. Absolutely no markdown, no headers, no bullet points, no asterisks, no # symbols.
Extract the 2-3 most material insights NOT visible in standard revenue/profit/EPS tables.
This could be anything significant — dividends, guidance, order book, capacity, regulatory updates, asset quality, exceptional items, debt changes, or any sector-specific metric.
Be specific with numbers. Skip generic statements."""
                    }
                ]
            }]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        print(f"  ⚠️ PDF analysis error: {e}")
        return ''

# =============================================================================
# POST-EARNINGS: MAIN DETECTION LOOP
# =============================================================================

def check_post_earnings_declarations():
    """
    For each portfolio stock where earnings_date <= today and declared = false:
    1. Fetch fresh Screener data
    2. If new quarter detected → update stock_fundamentals, generate commentary + PDF insights
    3. Mark declared in earnings_calendar
    """
    today = datetime.now().date()
    try:
        resp = supabase.table('earnings_calendar')\
            .select('id, ticker, stock_name, earnings_date, quarter')\
            .lte('earnings_date', today.isoformat())\
            .eq('declared', False)\
            .execute()
        pending = resp.data or []
    except Exception as e:
        print(f"  ❌ earnings_calendar fetch error: {e}")
        return

    if not pending:
        print("  ✅ No pending earnings declarations to check")
        return

    print(f"  📋 Checking {len(pending)} pending earnings dates...")

    for entry in pending:
        ticker     = entry['ticker']
        stock_name = entry['stock_name']
        symbol     = ticker.replace('.NS', '').replace('.BO', '')
        print(f"  🔍 {symbol} (earnings: {entry['earnings_date']})...")

        db_latest_period = get_latest_quarter_in_db(ticker)
        quarters         = fetch_screener_quarterly(symbol)

        if not quarters:
            print(f"    ⚠️ No Screener data found")
            continue

        latest_screener_period = quarters[0].get('period')

        if db_latest_period is None:
            # No prior quarterly data in DB. Declare directly using Screener data.
            # Do NOT store to stock_fundamentals here — the declaration block below handles that.
            print(f"    ✅ No prior DB data — treating {latest_screener_period} as new results")
            # Skip the "no new quarter" check by jumping straight to declaration
            # We do this by pretending db_latest_period is a dummy value
            db_latest_period = '__NONE__'

        if latest_screener_period == db_latest_period and db_latest_period != '__NONE__':
            print(f"    ⏳ No new quarter yet (still showing {db_latest_period})")
            continue

        print(f"    ✅ New quarter: {latest_screener_period} (was: {db_latest_period})")

        # Update stock_fundamentals
        try:
            existing = supabase.table('stock_fundamentals')\
                .select('id').eq('ticker', ticker).single().execute()
            if existing.data:
                supabase.table('stock_fundamentals')\
                    .update({'quarterly_financials': quarters}).eq('ticker', ticker).execute()
            else:
                supabase.table('stock_fundamentals')\
                    .insert({'ticker': ticker, 'quarterly_financials': quarters}).execute()
            print(f"    💾 stock_fundamentals updated")
        except Exception as e:
            print(f"    ⚠️ stock_fundamentals update error: {e}")

        # Generate Haiku commentary
        latest       = quarters[0] if len(quarters) > 0 else {}
        prev_quarter = quarters[1] if len(quarters) > 1 else {}
        year_ago     = quarters[4] if len(quarters) > 4 else {}
        commentary   = generate_commentary(stock_name, latest_screener_period or entry['quarter'],
                                           latest, prev_quarter, year_ago)
        if commentary:
            print(f"    💬 Commentary generated")

        # Fetch PDF insights
        pdf_url    = latest.get('pdf_url', '')
        pdf_insights = ''
        if pdf_url:
            print(f"    📄 Fetching PDF insights...")
            pdf_insights = fetch_pdf_insights(stock_name, latest_screener_period or entry['quarter'], pdf_url)
            if pdf_insights:
                print(f"    ✅ PDF insights generated")

        # Combine commentary + PDF insights
        full_commentary = commentary
        if pdf_insights:
            full_commentary = (commentary + '\n\n' if commentary else '') + pdf_insights

        # Mark as declared
        try:
            supabase.table('earnings_calendar').update({
                'declared':      True,
                'declared_date': today.isoformat(),
                'commentary':    full_commentary,
                'pdf_url':       pdf_url or None,
            }).eq('id', entry['id']).execute()
            print(f"    ✅ Marked as declared: {stock_name}")
        except Exception as e:
            print(f"    ❌ earnings_calendar update error: {e}")

# =============================================================================
# ALERT MANAGEMENT
# =============================================================================

def insert_alert(alert_data):
    try:
        if 'details' in alert_data and alert_data['details']:
            alert_data['details'] = json.loads(
                json.dumps(alert_data['details'],
                           default=lambda x: bool(x) if isinstance(x, (bool, np.bool_)) else str(x))
            )
        today = datetime.now().date().isoformat()
        old = supabase.table('alerts').select('id')\
            .eq('ticker', alert_data['ticker'])\
            .eq('alert_type', alert_data['alert_type'])\
            .lt('alert_date', today)\
            .eq('status', 'NEW').execute()
        if old.data:
            for a in old.data:
                supabase.table('alerts').update({'status': 'ARCHIVED'}).eq('id', a['id']).execute()
            print(f"  📦 Archived {len(old.data)} old alert(s) for {alert_data['ticker']}")
        existing = supabase.table('alerts').select('id')\
            .eq('ticker', alert_data['ticker'])\
            .eq('alert_type', alert_data['alert_type'])\
            .eq('alert_date', today)\
            .eq('status', 'NEW').execute()
        if existing.data:
            return False
        supabase.table('alerts').insert(alert_data).execute()
        print(f"  ✅ {alert_data['ticker']} — {alert_data['alert_type']}")
        return True
    except Exception as e:
        print(f"  ❌ Insert error: {e}")
        return False


def auto_archive_old_alerts(days=1):
    try:
        cutoff = (datetime.now() - timedelta(days=days)).date().isoformat()
        result = supabase.table('alerts').update({'status': 'ARCHIVED'})\
            .eq('status', 'NEW').lt('alert_date', cutoff).execute()
        count = len(result.data) if result.data else 0
        if count > 0:
            print(f"  ✅ Auto-archived {count} alerts older than {days} days")
    except Exception as e:
        print(f"  ❌ Archive error: {e}")


def archive_past_results_alerts():
    try:
        today = datetime.now().date().isoformat()
        alerts_resp = supabase.table('alerts').select('id, details')\
            .eq('alert_type', 'quarterly_results').eq('status', 'NEW').execute()
        archived = 0
        for alert in alerts_resp.data:
            if alert.get('details') and alert['details'].get('results_date'):
                if alert['details']['results_date'] < today:
                    supabase.table('alerts').update({'status': 'ARCHIVED'})\
                        .eq('id', alert['id']).execute()
                    archived += 1
        if archived > 0:
            print(f"  ✅ Archived {archived} past results alerts")
    except Exception as e:
        print(f"  ❌ Results archive error: {e}")

# =============================================================================
# MAIN
# =============================================================================

def main():
    if datetime.now().weekday() >= 5:
        print("📅 Weekend — scanner skipped")
        return

    print("=" * 70)
    print("📈 INTEGRATED PORTFOLIO ALERT SCANNER")
    print(f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    print("\n📊 Fetching portfolio from holdings table...")
    portfolio_stocks = get_portfolio_stocks()
    print(f"  ✅ Found {len(portfolio_stocks)} stocks")
    if not portfolio_stocks:
        print("  ❌ No stocks found. Exiting.")
        return

    print("\n📦 Archiving old alerts...")
    auto_archive_old_alerts(days=1)
    archive_past_results_alerts()

    print("\n🗑️  Cleaning up old earnings calendar entries...")
    cleanup_old_earnings()

    total_alerts = 0

    # Step 1: EMA Crossover
    print(f"\n🔍 EMA Crossovers (20/50, lookback {EMA_CONFIG['lookback_days']} days)...")
    for stock in portfolio_stocks:
        print(f"  {stock['name']} ({stock['symbol']})...")
        alert = scan_ema_crossover(stock)
        if alert and insert_alert(alert):
            total_alerts += 1

    # Step 2: Promoter Buying
    print("\n💼 Promoter Transactions (Trendlyne)...")
    for alert in scan_promoter_buying(portfolio_stocks):
        if insert_alert(alert):
            total_alerts += 1

    # Step 3: Volume Breakout
    print(f"\n📈 Volume Breakouts (>{VOLUME_CONFIG['multiplier']}x avg, >{VOLUME_CONFIG['min_price_change']}% move)...")
    for stock in portfolio_stocks:
        print(f"  {stock['name']}...")
        alert = scan_volume_breakout(stock)
        if alert and insert_alert(alert):
            total_alerts += 1

    # Step 4: Blue Zone
    print(f"\n🔵 Blue Zone (Daily RSI EMA9 >= {BLUE_ZONE_CONFIG['daily_rsi_buy']}, "
          f"Weekly >= {BLUE_ZONE_CONFIG['weekly_rsi_buy']})...")
    for stock in portfolio_stocks:
        print(f"  {stock['name']}...")
        alert = scan_blue_zone_stocks(stock)
        if alert and insert_alert(alert):
            total_alerts += 1

    # Step 5: Upcoming Quarterly Results
    print("\n📅 Upcoming Quarterly Results (30-day → earnings_calendar, 7-day → alert)...")
    for stock in portfolio_stocks:
        print(f"  {stock['name']}...")
        alert = scan_upcoming_results(stock)
        if alert and insert_alert(alert):
            total_alerts += 1

    # Step 6: Post-Earnings Declaration Check
    print("\n📊 Checking for post-earnings declarations...")
    check_post_earnings_declarations()

    print("\n" + "=" * 70)
    print(f"✅ SCAN COMPLETE — {total_alerts} new alerts inserted")
    print("=" * 70)


if __name__ == '__main__':
    main()
