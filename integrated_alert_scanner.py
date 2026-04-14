"""
Integrated Portfolio Alert Scanner - ZERODHA VERSION
=====================================================
Scans portfolio holdings for technical alerts that power the Holdings tab badges.

Writes to: `alerts` table (status='NEW', portfolio='INDIAN')
Reads from: `holdings` table + `daily_stock_snapshots` (Zerodha data)

Active scans:
1. EMA Crossover (20/50) — Golden Cross / Death Cross
2. Promoter Buying/Selling — Trendlyne scrape
3. Volume Breakout — 2x average + 3% price move
4. Blue Zone — Daily RSI EMA(9) >= 72 Strong / 65 Buy, Weekly >= 65 / 55
5. Quarterly Results — Trendlyne earnings calendar (next 7 days)

Removed: 200 EMA Breakout, 200 EMA Retest (deprecated)

Thresholds synced with PRODUCTION_entry_signals_scanner.py (April 2026)
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

# =============================================================================
# CONFIGURATION
# =============================================================================

SUPABASE_URL = os.getenv('SUPABASE_URL', 'https://hcgyncghmcvylnrmcivj.supabase.co')
SUPABASE_KEY = os.getenv('SUPABASE_KEY', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhjZ3luY2dobWN2eWxucm1jaXZqIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTc1MTQwMTEsImV4cCI6MjA3MzA5MDAxMX0.n8vFVCJe1y_3o8fpAY0IgasZ4eKl7DAogEM3OlHB8Ww')
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

EMA_CONFIG = {
    'short_ema': 20,
    'long_ema': 50,
    'filter_ema': 200,
    'lookback_days': 5,   # Check last 5 days for crossover
    'adx_min_strong': 25, # ADX threshold for Strong Buy grade
    'adx_min_buy': 20,    # ADX threshold for Buy grade
}

VOLUME_CONFIG = {
    'multiplier': 2.0,    # Volume must be 2x average
    'avg_period': 20,     # 20-day average volume
    'min_price_change': 3.0,  # Price must move 3%
}

# UPDATED April 2026 — synced with PRODUCTION_entry_signals_scanner.py
BLUE_ZONE_CONFIG = {
    'daily_rsi_strong': 72,    # Strong Buy: Daily RSI EMA(9) >= 72
    'weekly_rsi_strong': 65,   # Strong Buy: Weekly RSI EMA(9) >= 65
    'daily_rsi_buy': 65,       # Buy: Daily RSI EMA(9) >= 65
    'weekly_rsi_buy': 55,      # Buy: Weekly RSI EMA(9) >= 55
    'max_pct_from_high': 10,   # Must be within 10% of 52W high
}

# =============================================================================
# DATA FETCHING
# =============================================================================

def fetch_ohlc_from_supabase(ticker: str, days: int = 365) -> pd.DataFrame:
    """Fetch OHLC + pre-calculated indicators from daily_stock_snapshots."""
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
        df.rename(columns={
            'open': 'Open', 'high': 'High', 'low': 'Low',
            'close': 'Close', 'volume': 'Volume'
        }, inplace=True)
        return df

    except Exception as e:
        print(f"  ⚠️ Error fetching {ticker}: {e}")
        return pd.DataFrame()


def get_portfolio_stocks():
    """Fetch current portfolio stocks from holdings table."""
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
    """
    20/50 EMA crossover with ADX confirmation.
    Synced with PRODUCTION_entry_signals_scanner golden_cross logic.
    """
    ticker = stock['full_ticker']
    symbol = stock['symbol']
    name = stock['name']

    try:
        hist = fetch_ohlc_from_supabase(ticker, days=365)
        if hist.empty or len(hist) < 60:
            return None

        hist = calculate_adx(hist)

        # Use pre-calculated EMAs from Zerodha OHLC fetcher
        if 'ema_20' in hist.columns and 'ema_50' in hist.columns:
            hist['short_ema'] = hist['ema_20']
            hist['long_ema'] = hist['ema_50']
            hist['filter_ema'] = hist.get('ema_200', hist['Close'])
        else:
            hist['short_ema'] = calculate_ema(hist['Close'], 20)
            hist['long_ema'] = calculate_ema(hist['Close'], 50)
            hist['filter_ema'] = calculate_ema(hist['Close'], 200)

        hist['volume_avg'] = hist['Volume'].rolling(window=20).mean()

        # Check for crossover in lookback window
        crossover = None
        for i in range(1, min(EMA_CONFIG['lookback_days'] + 1, len(hist))):
            curr = hist.iloc[-i]
            prev = hist.iloc[-(i+1)]
            if pd.isna([curr['short_ema'], curr['long_ema'],
                        prev['short_ema'], prev['long_ema']]).any():
                continue

            if prev['short_ema'] <= prev['long_ema'] and curr['short_ema'] > curr['long_ema']:
                crossover = {'type': 'bullish', 'days_ago': i - 1, 'row': curr, 'prev': prev}
                break
            if prev['short_ema'] >= prev['long_ema'] and curr['short_ema'] < curr['long_ema']:
                crossover = {'type': 'bearish', 'days_ago': i - 1, 'row': curr, 'prev': prev}
                break

        if not crossover:
            return None

        latest = hist.iloc[-1]
        cmp = latest['Close']
        adx_value = float(latest['adx']) if pd.notna(latest['adx']) else 0
        plus_di = float(latest['plus_di']) if pd.notna(latest['plus_di']) else 0
        minus_di = float(latest['minus_di']) if pd.notna(latest['minus_di']) else 0
        avg_volume = latest['volume_avg']
        volume_ratio = float(latest['Volume'] / avg_volume) if avg_volume > 0 else 0

        crossover_type = crossover['type']
        alert_type = 'ema_golden_cross' if crossover_type == 'bullish' else 'ema_death_cross'
        alert_category = 'BULLISH' if crossover_type == 'bullish' else 'BEARISH'

        ema_20 = round(float(latest['short_ema']), 2)
        ema_50 = round(float(latest['long_ema']), 2)
        ema_200 = round(float(latest['filter_ema']), 2) if pd.notna(latest['filter_ema']) else None

        return {
            'portfolio': 'INDIAN',
            'ticker': symbol,
            'stock_name': name,
            'alert_type': alert_type,
            'alert_category': alert_category,
            'alert_title': f"{name} - {'Golden' if crossover_type == 'bullish' else 'Death'} Cross (20/50 EMA)",
            'alert_description': (
                f"20 EMA crossed {'above' if crossover_type == 'bullish' else 'below'} 50 EMA "
                f"{crossover['days_ago']} days ago | ADX: {adx_value:.1f} | Vol: {volume_ratio:.1f}x"
            ),
            'price': round(float(cmp), 2),
            'alert_date': datetime.now().date().isoformat(),
            'details': {
                'ema_20': ema_20,
                'ema_50': ema_50,
                'ema_200': ema_200,
                'days_ago': crossover['days_ago'],
                'adx': round(adx_value, 2),
                'plus_di': round(plus_di, 2),
                'minus_di': round(minus_di, 2),
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
    """Scrape recent promoter transactions from Trendlyne."""
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

            if 'promoter' not in category.lower():
                continue

            transactions.append({
                'company_name': company_name,
                'symbol': symbol,
                'person_entity': person,
                'category': category,
                'transaction_type': transaction_type,
                'date': date,
                'shares': shares,
                'post_holding': holding_text,
                'pct_change': pct_change,
            })
        except:
            continue
    return transactions


def scan_promoter_buying(portfolio_stocks):
    """Filter Trendlyne promoter transactions for portfolio stocks only."""
    all_transactions = get_recent_promoter_transactions()
    if not all_transactions:
        print("  ⚠️ No promoter transactions found")
        return []

    portfolio_symbols = {stock['symbol'].upper() for stock in portfolio_stocks}
    portfolio_transactions = [
        t for t in all_transactions
        if t['symbol'].upper() in portfolio_symbols
    ]
    print(f"  ✅ Found {len(portfolio_transactions)} promoter transactions in portfolio")

    alerts = []
    for txn in portfolio_transactions:
        is_buying = ('acquisition' in txn['transaction_type'].lower() or
                     'purchase' in txn['transaction_type'].lower())
        is_selling = ('disposal' in txn['transaction_type'].lower() or
                      'sale' in txn['transaction_type'].lower())
        if not (is_buying or is_selling):
            continue

        stock_name = next(
            (s['name'] for s in portfolio_stocks
             if s['symbol'].upper() == txn['symbol'].upper()),
            txn['company_name']
        )

        try:
            alert_date = datetime.strptime(txn['date'], "%d %b %Y").date().isoformat()
        except:
            alert_date = datetime.now().date().isoformat()

        alerts.append({
            'portfolio': 'INDIAN',
            'ticker': txn['symbol'],
            'stock_name': stock_name,
            'alert_type': 'promoter_buying' if is_buying else 'promoter_selling',
            'alert_category': 'BULLISH' if is_buying else 'BEARISH',
            'alert_title': f"{stock_name} - Promoter {'Buying' if is_buying else 'Selling'}",
            'alert_description': f"{txn['person_entity'][:50]} - {txn['transaction_type']}",
            'price': None,
            'alert_date': alert_date,
            'details': {
                'person_entity': txn['person_entity'],
                'transaction_type': txn['transaction_type'],
                'shares': txn['shares'],
                'post_holding': txn['post_holding'],
                'pct_change': txn['pct_change'],
                'category': txn['category']
            }
        })
    return alerts

# =============================================================================
# SCAN: VOLUME BREAKOUT
# =============================================================================

def scan_volume_breakout(stock):
    """Volume > 2x average with >= 3% price move."""
    ticker = stock['full_ticker']
    symbol = stock['symbol']
    name = stock['name']

    try:
        hist = fetch_ohlc_from_supabase(ticker, days=365)
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

        return {
            'portfolio': 'INDIAN',
            'ticker': symbol,
            'stock_name': name,
            'alert_type': alert_type,
            'alert_category': 'BULLISH' if is_bullish else 'BEARISH',
            'alert_title': f"{name} - Volume Breakout ({'Bullish' if is_bullish else 'Bearish'})",
            'alert_description': (
                f"Volume {volume_ratio:.1f}x average with "
                f"{abs(price_change_pct):.1f}% {'gain' if is_bullish else 'loss'}"
            ),
            'price': round(float(current_price), 2),
            'alert_date': datetime.now().date().isoformat(),
            'details': {
                'current_volume': int(current_volume),
                'avg_volume': int(avg_volume),
                'volume_ratio': round(float(volume_ratio), 2),
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
    """
    Blue Zone momentum signal — UPDATED thresholds (April 2026):
    Strong: Daily RSI EMA(9) >= 72, Weekly RSI EMA(9) >= 65, above EMA50, volume > 1.5x
    Buy:    Daily RSI EMA(9) >= 65, Weekly RSI EMA(9) >= 55, above EMA20
    Within 10% of 52W high.
    Uses pre-calculated RSI values from daily_stock_snapshots.
    """
    ticker = stock['full_ticker']
    symbol = stock['symbol']
    name = stock['name']

    try:
        hist = fetch_ohlc_from_supabase(ticker, days=365)
        if hist.empty or len(hist) < 50:
            return None

        latest = hist.iloc[-1]

        # Use pre-calculated values from OHLC fetcher
        daily_rsi_ema_9 = latest.get('rsi_ema_9')
        weekly_rsi_ema_9 = latest.get('weekly_rsi_ema_9')
        ema_20 = latest.get('ema_20')
        ema_50 = latest.get('ema_50')
        current_price = float(latest['Close'])

        if pd.isna(daily_rsi_ema_9) or pd.isna(weekly_rsi_ema_9):
            return None

        # 52W high — use stored column if available, else rolling max
        high_52w = latest.get('high_52w')
        if pd.isna(high_52w) or high_52w is None:
            high_52w = float(hist['High'].max())
        else:
            high_52w = float(high_52w)

        distance_from_high = ((current_price - high_52w) / high_52w) * 100
        if distance_from_high < -BLUE_ZONE_CONFIG['max_pct_from_high']:
            return None

        avg_volume = hist['Volume'].tail(20).mean()
        volume_ratio = float(latest['Volume'] / avg_volume) if avg_volume > 0 else 0
        above_ema_50 = current_price > float(ema_50) if pd.notna(ema_50) else False
        above_ema_20 = current_price > float(ema_20) if pd.notna(ema_20) else False

        # Determine grade
        if (daily_rsi_ema_9 >= BLUE_ZONE_CONFIG['daily_rsi_strong'] and
                weekly_rsi_ema_9 >= BLUE_ZONE_CONFIG['weekly_rsi_strong'] and
                above_ema_50 and volume_ratio > 1.5):
            grade = 'Strong Buy'
            alert_type = 'blue_zone_stocks'
        elif (daily_rsi_ema_9 >= BLUE_ZONE_CONFIG['daily_rsi_buy'] and
              weekly_rsi_ema_9 >= BLUE_ZONE_CONFIG['weekly_rsi_buy'] and
              above_ema_20):
            grade = 'Buy'
            alert_type = 'blue_zone_stocks'
        else:
            return None

        # Count consecutive days in Blue Zone
        days_in_bz = 1
        for i in range(len(hist) - 2, -1, -1):
            row = hist.iloc[i]
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
            'portfolio': 'INDIAN',
            'ticker': symbol,
            'stock_name': name,
            'alert_type': alert_type,
            'alert_category': 'BULLISH',
            'alert_title': f"{name} - Blue Zone ({grade})",
            'alert_description': (
                f"{grade}: Daily RSI: {daily_rsi_ema_9:.1f}, Weekly RSI: {weekly_rsi_ema_9:.1f}, "
                f"{abs(distance_from_high):.1f}% from 52W high • {days_in_bz} days in zone"
                + (f" + Vol {volume_ratio:.1f}x 🔥" if volume_ratio >= 1.5 else "")
            ),
            'price': round(current_price, 2),
            'alert_date': datetime.now().date().isoformat(),
            'details': {
                'daily_rsi_ema_9': round(float(daily_rsi_ema_9), 2),
                'weekly_rsi_ema_9': round(float(weekly_rsi_ema_9), 2),
                'grade': grade,
                'pct_from_52w_high': round(distance_from_high, 2),
                'above_ema_50': above_ema_50,
                'above_ema_20': above_ema_20,
                'volume_ratio': round(volume_ratio, 2),
                'days_in_blue_zone': days_in_bz,
                'daily_threshold': BLUE_ZONE_CONFIG['daily_rsi_buy'],
                'weekly_threshold': BLUE_ZONE_CONFIG['weekly_rsi_buy']
            }
        }

    except Exception as e:
        print(f"  ❌ Blue Zone scan error {symbol}: {e}")
        return None

# =============================================================================
# SCAN: QUARTERLY RESULTS
# =============================================================================

def scan_upcoming_results(stock):
    """Upcoming quarterly results in next 7 days from Trendlyne."""
    symbol = stock['symbol']
    name = stock['name']

    try:
        url = f"https://trendlyne.com/equity/{symbol}/results-calendar/"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(url, headers=headers, timeout=10)

        if response.status_code != 200:
            return None

        soup = BeautifulSoup(response.content, 'html.parser')
        results_date = None

        import re
        for text_elem in soup.find_all(string=True):
            text = str(text_elem).strip().lower()
            if 'expected' in text or 'results' in text:
                parent = text_elem.parent
                if parent:
                    date_text = parent.get_text()
                    date_patterns = [
                        r'(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{4})',
                    ]
                    for pattern in date_patterns:
                        match = re.search(pattern, date_text, re.IGNORECASE)
                        if match:
                            try:
                                from dateutil import parser
                                results_date = parser.parse(date_text, fuzzy=True).date()
                                break
                            except:
                                continue
                if results_date:
                    break

        if not results_date:
            return None

        today = datetime.now().date()
        days_until = (results_date - today).days

        if days_until < 0 or days_until > 7:
            return None

        current_month = datetime.now().month
        current_year = datetime.now().year
        if current_month <= 3:
            quarter, fy_year = 'Q4', current_year
        elif current_month <= 6:
            quarter, fy_year = 'Q1', current_year + 1
        elif current_month <= 9:
            quarter, fy_year = 'Q2', current_year + 1
        else:
            quarter, fy_year = 'Q3', current_year + 1

        return {
            'portfolio': 'INDIAN',
            'ticker': symbol,
            'stock_name': name,
            'alert_type': 'quarterly_results',
            'alert_category': 'INFO',
            'alert_title': f"{name} - Quarterly Results Upcoming",
            'alert_description': (
                f"Results expected {results_date.strftime('%b %d, %Y')} "
                f"(in {days_until} {'day' if days_until == 1 else 'days'})"
            ),
            'price': None,
            'alert_date': today.isoformat(),
            'details': {
                'results_date': results_date.isoformat(),
                'days_until': days_until,
                'quarter': f"{quarter} FY{fy_year}",
                'priority': 'URGENT' if days_until <= 3 else 'UPCOMING'
            }
        }

    except Exception as e:
        print(f"  ❌ Results scan error {symbol}: {e}")
        return None

# =============================================================================
# ALERT MANAGEMENT
# =============================================================================

def insert_alert(alert_data):
    """Insert alert — archive old, skip if today's already exists."""
    try:
        import json
        if 'details' in alert_data and alert_data['details']:
            alert_data['details'] = json.loads(
                json.dumps(alert_data['details'],
                           default=lambda x: bool(x) if isinstance(x, (bool, np.bool_)) else str(x))
            )

        today = datetime.now().date().isoformat()

        # Archive old alerts for same ticker+type
        old = supabase.table('alerts').select('id')\
            .eq('ticker', alert_data['ticker'])\
            .eq('alert_type', alert_data['alert_type'])\
            .lt('alert_date', today)\
            .eq('status', 'NEW')\
            .execute()

        if old.data:
            for a in old.data:
                supabase.table('alerts').update({'status': 'ARCHIVED'})\
                    .eq('id', a['id']).execute()
            print(f"  📦 Archived {len(old.data)} old alert(s) for {alert_data['ticker']}")

        # Skip if today's already exists
        existing = supabase.table('alerts').select('id')\
            .eq('ticker', alert_data['ticker'])\
            .eq('alert_type', alert_data['alert_type'])\
            .eq('alert_date', today)\
            .eq('status', 'NEW')\
            .execute()

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

    # Step 5: Quarterly Results
    print("\n📅 Upcoming Quarterly Results (next 7 days)...")
    for stock in portfolio_stocks:
        print(f"  {stock['name']}...")
        alert = scan_upcoming_results(stock)
        if alert and insert_alert(alert):
            total_alerts += 1

    print("\n" + "=" * 70)
    print(f"✅ SCAN COMPLETE — {total_alerts} new alerts inserted")
    print("=" * 70)


if __name__ == '__main__':
    main()
