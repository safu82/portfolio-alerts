#!/usr/bin/env python3
"""
Stock Fundamentals Fetcher
Fetches comprehensive fundamental data for portfolio holdings from:
  - Yahoo Finance (yfinance): returns, valuation, financials, ATH
  - Screener.in: PE avg, ROCE, shareholding, quarterly data

Runs daily via GitHub Actions after market close.
Only processes stocks currently in the 'holdings' table.

Schedule: 6:00 PM IST (12:30 UTC), Mon-Fri
"""

import yfinance as yf
import pandas as pd
import numpy as np
import requests
from bs4 import BeautifulSoup
from datetime import datetime, date, timedelta
from supabase import create_client
import os
import time
import json
import re
import traceback

# ─── CONFIG ───────────────────────────────────────────────────────────────────

SUPABASE_URL = os.getenv('SUPABASE_URL', 'https://hcgyncghmcvylnrmcivj.supabase.co')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')

SCREENER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-IN,en;q=0.9',
    'Referer': 'https://www.screener.in/',
}

# ─── SUPABASE ─────────────────────────────────────────────────────────────────

def get_portfolio_tickers(supabase):
    """Get all unique tickers from holdings table."""
    result = supabase.table('holdings').select('ticker').execute()
    tickers = list({r['ticker'] for r in result.data if r.get('ticker')})
    # Only NSE stocks (ends in .NS)
    tickers = [t for t in tickers if t.endswith('.NS')]
    print(f"📋 Found {len(tickers)} unique NSE tickers in holdings")
    return tickers

# ─── YAHOO FINANCE ────────────────────────────────────────────────────────────

def safe_float(val, default=None):
    try:
        if val is None or (isinstance(val, float) and np.isnan(val)):
            return default
        return round(float(val), 4)
    except:
        return default

def get_yfinance_data(ticker_ns):
    """Fetch all available data from Yahoo Finance for a ticker."""
    try:
        tk = yf.Ticker(ticker_ns)
        info = tk.info or {}

        # ── Performance returns ───────────────────────────────────────────────
        # Fetch 3 years of history for returns + ATH
        hist = tk.history(period='3y', interval='1d', auto_adjust=True)

        returns = {}
        if not hist.empty:
            today_price = hist['Close'].iloc[-1]

            def pct(n_days):
                if len(hist) > n_days:
                    past = hist['Close'].iloc[-n_days]
                    return round((today_price - past) / past * 100, 2)
                return None

            returns['return_1w']  = pct(5)
            returns['return_1m']  = pct(21)
            returns['return_3m']  = pct(63)
            returns['return_6m']  = pct(126)
            returns['return_1y']  = pct(252)

            # YTD
            year_start_str = f"{date.today().year}-01-01"
            ytd_slice = hist[hist.index >= year_start_str]
            if not ytd_slice.empty:
                ytd_start = ytd_slice['Close'].iloc[0]
                returns['return_ytd'] = round((today_price - ytd_start) / ytd_start * 100, 2)

            # 3Y return
            if len(hist) > 756:
                returns['return_3y'] = pct(756)

            # ATH from full 3yr history (fetch max for true ATH)
            hist_max = tk.history(period='max', interval='1d', auto_adjust=True)
            if not hist_max.empty:
                ath_idx = hist_max['High'].idxmax()
                returns['ath_price'] = round(float(hist_max['High'].max()), 2)
                returns['ath_date']  = ath_idx.date().isoformat()

            # ATR(14)
            if len(hist) >= 14:
                high = hist['High'].tail(14)
                low  = hist['Low'].tail(14)
                close_prev = hist['Close'].tail(15).shift(1).tail(14)
                tr = pd.concat([
                    high - low,
                    (high - close_prev).abs(),
                    (low  - close_prev).abs()
                ], axis=1).max(axis=1)
                atr = round(float(tr.mean()), 2)
                returns['atr_14']     = atr
                returns['atr_14_pct'] = round(atr / float(today_price) * 100, 2)

        # ── Valuation & profitability from info ───────────────────────────────
        mcap_inr = safe_float(info.get('marketCap'))
        mcap_cr  = round(mcap_inr / 1e7, 2) if mcap_inr else None  # convert to crores

        valuation = {
            'market_cap_cr':       mcap_cr,
            'pe_ttm':              safe_float(info.get('trailingPE')),
            'peg_ratio':           safe_float(info.get('pegRatio')),
            'price_to_book':       safe_float(info.get('priceToBook')),
            'price_to_sales':      safe_float(info.get('priceToSalesTrailing12Months')),
            'ev_to_ebitda':        safe_float(info.get('enterpriseToEbitda')),
            'dividend_yield':      safe_float(info.get('dividendYield')),
            'beta':                safe_float(info.get('beta')),
            'net_margin':          safe_float(info.get('profitMargins')),
            'gross_margin':        safe_float(info.get('grossMargins')),
            'ebitda_margin':       safe_float(info.get('ebitdaMargins')),
            'roe':                 safe_float(info.get('returnOnEquity')),
            'debt_to_equity':      safe_float(info.get('debtToEquity')),
            'eps_ttm':             safe_float(info.get('trailingEps')),
            'book_value_per_share':safe_float(info.get('bookValue')),
            'revenue_growth_yoy':  safe_float(info.get('revenueGrowth')),
            'earnings_growth_yoy': safe_float(info.get('earningsGrowth')),
            'sector':              info.get('sector'),
            'industry':            info.get('industry'),
            'stock_name':          info.get('longName') or info.get('shortName'),
        }

        # ── Quarterly financials from yfinance ────────────────────────────────
        quarterly = []
        try:
            qf = tk.quarterly_financials
            qi = tk.quarterly_income_stmt
            if qf is not None and not qf.empty:
                fin = qi if (qi is not None and not qi.empty) else qf
                # Sort columns descending (newest first) then take 8 quarters
                fin = fin.sort_index(axis=1, ascending=False)
                cols = list(fin.columns[:8])
                for col in cols:
                    q_label = col.strftime('%b %Y') if hasattr(col, 'strftime') else str(col)
                    rev = fin.loc['Total Revenue', col] if 'Total Revenue' in fin.index else None
                    ni  = fin.loc['Net Income', col] if 'Net Income' in fin.index else None
                    ebi = fin.loc['EBITDA', col] if 'EBITDA' in fin.index else None

                    def to_cr(val):
                        if val is None or (isinstance(val, float) and np.isnan(val)):
                            return None
                        return round(float(val) / 1e7, 1)  # to crores

                    quarterly.append({
                        'quarter':        q_label,
                        'revenue_cr':     to_cr(rev),
                        'net_income_cr':  to_cr(ni),
                        'ebitda_cr':      to_cr(ebi),
                    })
        except Exception as e:
            print(f"    ⚠️  Quarterly data error: {e}")

        return {**returns, **valuation, 'quarterly_from_yf': quarterly, 'source_yfinance': True}

    except Exception as e:
        print(f"    ❌ yfinance error for {ticker_ns}: {e}")
        return {'source_yfinance': False}

# ─── SCREENER.IN ──────────────────────────────────────────────────────────────

def get_screener_symbol(ticker_ns):
    """Convert NSE ticker to Screener.in symbol (strip .NS, handle mappings)."""
    symbol = ticker_ns.replace('.NS', '')
    # Common symbol differences
    mappings = {
        'M&M': 'M%26M',
        'L&TFH': 'L%26TFH',
    }
    return mappings.get(symbol, symbol)

def parse_number(text):
    """Parse Indian number format from Screener (handles commas, Cr suffix)."""
    if not text:
        return None
    text = text.strip().replace(',', '').replace('%', '').replace('₹', '')
    # Remove trailing labels
    text = re.sub(r'\s*(Cr|L|K|M|B)\s*$', '', text, flags=re.IGNORECASE)
    try:
        return float(text)
    except:
        return None

def get_screener_data(ticker_ns):
    """Scrape fundamental data from Screener.in."""
    symbol = get_screener_symbol(ticker_ns)
    url = f"https://www.screener.in/company/{symbol}/consolidated/"
    fallback_url = f"https://www.screener.in/company/{symbol}/"

    result = {}

    for attempt_url in [url, fallback_url]:
        try:
            resp = requests.get(attempt_url, headers=SCREENER_HEADERS, timeout=15)
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.text, 'html.parser')

            # ── Key ratios ────────────────────────────────────────────────────
            ratios_section = soup.find('section', id='top-ratios')
            if ratios_section:
                for li in ratios_section.find_all('li'):
                    label_el = li.find('span', class_='name')
                    value_el = li.find('span', class_='nowrap')
                    if not label_el or not value_el:
                        continue
                    label = label_el.get_text(strip=True).lower()
                    value = parse_number(value_el.get_text(strip=True))
                    if value is None:
                        continue

                    if 'stock p/e' in label:
                        result['pe_ttm'] = result.get('pe_ttm') or value
                    elif 'return on equity' in label or 'roe' in label:
                        result['roe'] = value
                    elif 'roce' in label:
                        result['roce'] = value
                    elif 'debt / equity' in label:
                        result['debt_to_equity'] = value
                    elif 'net profit margin' in label:
                        result['net_margin'] = value / 100 if value > 1 else value
                    elif 'dividend yield' in label:
                        result['dividend_yield'] = value / 100 if value > 1 else value
                    elif 'book value' in label:
                        result['book_value_per_share'] = value
                    elif 'eps' in label:
                        result['eps_ttm'] = value

            # ── Shareholding ──────────────────────────────────────────────────
            sh_section = soup.find('section', id='shareholding')
            if sh_section:
                rows = sh_section.find_all('tr')
                for row in rows:
                    cells = row.find_all('td')
                    if not cells:
                        continue
                    label = cells[0].get_text(strip=True).lower()
                    # Get latest value (last column)
                    vals = [parse_number(c.get_text(strip=True)) for c in cells[1:] if parse_number(c.get_text(strip=True)) is not None]
                    if not vals:
                        continue
                    latest = vals[-1]
                    if 'promoters' in label:
                        result['promoter_holding_pct'] = latest
                    elif 'fii' in label or 'foreign' in label:
                        result['fii_holding_pct'] = latest
                    elif 'dii' in label or 'domestic inst' in label:
                        result['dii_holding_pct'] = latest

            # ── Quarterly P&L table ───────────────────────────────────────────
            quarterly = []
            pl_section = soup.find('section', id='quarters')
            if pl_section:
                table = pl_section.find('table')
                if table:
                    headers = [th.get_text(strip=True) for th in table.find_all('th')]
                    # Screener shows oldest on left, newest on right — take rightmost 8, reverse to newest-first
                    quarter_labels = headers[1:]
                    recent_labels = list(reversed(quarter_labels[-8:]))

                    def get_row(row_label):
                        for tr in table.find_all('tr'):
                            cells = tr.find_all('td')
                            if cells and row_label.lower() in cells[0].get_text(strip=True).lower():
                                vals = [parse_number(c.get_text(strip=True)) for c in cells[1:]]
                                # Reverse to match newest-first label order
                                return list(reversed(vals[-8:]))
                        return []

                    revenues  = get_row('sales')
                    net_profs = get_row('net profit')
                    eps_rows  = get_row('eps')

                    for i, q in enumerate(recent_labels):
                        entry = {'quarter': q}
                        if i < len(revenues):  entry['revenue_cr']    = revenues[i]
                        if i < len(net_profs): entry['net_income_cr'] = net_profs[i]
                        if i < len(eps_rows):  entry['eps']           = eps_rows[i]
                        quarterly.append(entry)

            if quarterly:
                result['quarterly_screener'] = quarterly

            # ── 3-year avg PE from historical PE data ─────────────────────────
            # Screener shows historical PE in the chart data — extract if available
            # Look for pe_history in script tags
            for script in soup.find_all('script'):
                text = script.get_text()
                if 'pe_history' in text.lower() or '"pe"' in text.lower():
                    # Try to find PE values
                    pe_matches = re.findall(r'"pe"\s*:\s*([\d.]+)', text)
                    if pe_matches:
                        pe_vals = [float(p) for p in pe_matches if float(p) < 500]
                        if len(pe_vals) >= 4:
                            result['pe_3yr_avg'] = round(sum(pe_vals) / len(pe_vals), 1)

            result['source_screener'] = True
            print(f"    ✅ Screener: {symbol} — {len(result)} fields")
            return result

        except Exception as e:
            print(f"    ⚠️  Screener error ({attempt_url}): {e}")
            continue

    result['source_screener'] = False
    return result

# ─── MERGE & SAVE ─────────────────────────────────────────────────────────────

def merge_quarterly(yf_data, screener_data):
    """Prefer Screener quarterly data (more reliable for Indian stocks), fallback to yfinance."""
    screener_q = screener_data.get('quarterly_screener', [])
    yf_q       = yf_data.get('quarterly_from_yf', [])

    if screener_q:
        # Merge EPS from yfinance into screener quarters if available
        for i, q in enumerate(screener_q):
            if i < len(yf_q) and yf_q[i].get('ebitda_cr'):
                q['ebitda_cr'] = q.get('ebitda_cr') or yf_q[i].get('ebitda_cr')
        return screener_q
    return yf_q

def build_record(ticker, yf_data, screener_data):
    """Merge yfinance and Screener data into a single record."""
    q = merge_quarterly(yf_data, screener_data)

    # Screener overrides yfinance for Indian-specific metrics
    record = {
        'ticker':                ticker,
        'last_updated':          datetime.utcnow().isoformat(),

        # Metadata
        'stock_name':     yf_data.get('stock_name') or screener_data.get('stock_name'),
        'sector':         yf_data.get('sector')     or screener_data.get('sector'),
        'industry':       yf_data.get('industry')   or screener_data.get('industry'),

        # Price
        'market_cap_cr':  yf_data.get('market_cap_cr'),
        'ath_price':      yf_data.get('ath_price'),
        'ath_date':       yf_data.get('ath_date'),
        'beta':           yf_data.get('beta'),
        'atr_14':         yf_data.get('atr_14'),
        'atr_14_pct':     yf_data.get('atr_14_pct'),

        # Returns
        'return_1w':  yf_data.get('return_1w'),
        'return_1m':  yf_data.get('return_1m'),
        'return_3m':  yf_data.get('return_3m'),
        'return_6m':  yf_data.get('return_6m'),
        'return_ytd': yf_data.get('return_ytd'),
        'return_1y':  yf_data.get('return_1y'),
        'return_3y':  yf_data.get('return_3y'),

        # Valuation — prefer Screener PE, fallback to yfinance
        'pe_ttm':          screener_data.get('pe_ttm')      or yf_data.get('pe_ttm'),
        'pe_3yr_avg':      screener_data.get('pe_3yr_avg'),
        'peg_ratio':       yf_data.get('peg_ratio'),
        'price_to_book':   yf_data.get('price_to_book'),
        'price_to_sales':  yf_data.get('price_to_sales'),
        'ev_to_ebitda':    yf_data.get('ev_to_ebitda'),
        'dividend_yield':  screener_data.get('dividend_yield') or yf_data.get('dividend_yield'),

        # Profitability — prefer Screener
        'net_margin':   screener_data.get('net_margin')    or yf_data.get('net_margin'),
        'gross_margin': yf_data.get('gross_margin'),
        'ebitda_margin':yf_data.get('ebitda_margin'),
        'roe':          screener_data.get('roe')           or yf_data.get('roe'),
        'roce':         screener_data.get('roce'),
        'debt_to_equity':screener_data.get('debt_to_equity') or yf_data.get('debt_to_equity'),

        # Growth
        'revenue_growth_yoy':  yf_data.get('revenue_growth_yoy'),
        'earnings_growth_yoy': yf_data.get('earnings_growth_yoy'),
        'eps_cagr_3yr':        screener_data.get('eps_cagr_3yr'),
        'revenue_cagr_3yr':    screener_data.get('revenue_cagr_3yr'),

        # Per-share
        'eps_ttm':              screener_data.get('eps_ttm')            or yf_data.get('eps_ttm'),
        'book_value_per_share': screener_data.get('book_value_per_share') or yf_data.get('book_value_per_share'),

        # Shareholding
        'promoter_holding_pct': screener_data.get('promoter_holding_pct'),
        'fii_holding_pct':      screener_data.get('fii_holding_pct'),
        'dii_holding_pct':      screener_data.get('dii_holding_pct'),

        # Quarterly (JSON)
        'quarterly_financials': json.dumps(q) if q else None,

        # Source flags
        'source_yfinance': yf_data.get('source_yfinance', False),
        'source_screener': screener_data.get('source_screener', False),
    }

    # Clean out None values only for numeric fields to avoid overwriting good data
    return {k: v for k, v in record.items() if v is not None}

def save_to_supabase(supabase, record):
    ticker = record['ticker']
    try:
        supabase.table('stock_fundamentals').upsert(
            record, on_conflict='ticker'
        ).execute()
        print(f"    💾 Saved: {ticker}")
    except Exception as e:
        print(f"    ❌ Save error for {ticker}: {e}")

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("📊 STOCK FUNDAMENTALS FETCHER")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    tickers  = get_portfolio_tickers(supabase)

    success, failed = 0, 0

    for i, ticker in enumerate(tickers, 1):
        print(f"\n[{i}/{len(tickers)}] {ticker}")
        try:
            # yfinance
            print("  📈 Yahoo Finance...")
            yf_data = get_yfinance_data(ticker)
            time.sleep(1)

            # Screener.in
            print("  📋 Screener.in...")
            screener_data = get_screener_data(ticker)
            time.sleep(2)  # polite delay

            # Merge & save
            record = build_record(ticker, yf_data, screener_data)
            save_to_supabase(supabase, record)

            src = []
            if yf_data.get('source_yfinance'): src.append('YF')
            if screener_data.get('source_screener'): src.append('Screener')
            print(f"  ✅ Done [{', '.join(src)}]")
            success += 1

        except Exception as e:
            print(f"  ❌ Error: {e}")
            traceback.print_exc()
            failed += 1

        # Rate limit protection
        if i % 10 == 0:
            print("\n  ⏸️  Rate limit pause (10s)...")
            time.sleep(10)

    print("\n" + "=" * 70)
    print(f"✅ Done: {success} succeeded, {failed} failed")
    print(f"Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

if __name__ == "__main__":
    main()
