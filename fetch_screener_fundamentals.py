#!/usr/bin/env python3
"""
Weekly Screener.in Fundamentals Fetcher
Scrapes qualitative fundamentals for all ~450 stocks in indian_stock_sectors.

Fetches (Screener.in only — yfinance handled separately by fetch_yfinance_metrics.py):
  - Quarterly P&L (Revenue, Net Profit, EBITDA, EPS — last 8 quarters)
  - Key ratios: PE TTM, ROE, ROCE, Debt/Equity, Net Margin, Book Value
  - Shareholding: Promoter, FII, DII
  - PE 3Y Avg (from chart data if available)

Schedule: Saturday 7:00 AM IST (01:30 UTC)
Runtime: ~25-35 min for 450 stocks at ~3-4s per stock
"""

import requests
from bs4 import BeautifulSoup
from datetime import datetime
from supabase import create_client
import os, time, re, json, traceback

# ── Config ────────────────────────────────────────────────────────────────────
SUPABASE_URL = os.getenv('SUPABASE_URL', 'https://hcgyncghmcvylnrmcivj.supabase.co')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')

SLEEP_BETWEEN  = 3.0   # seconds between stocks (polite crawling)
SLEEP_EVERY_50 = 15    # extra pause every 50 stocks
TIMEOUT        = 15    # request timeout seconds
BATCH_SIZE     = 50    # upsert batch size

SCREENER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-IN,en;q=0.9',
    'Referer': 'https://www.screener.in/',
}

# ── Ticker → Screener symbol ──────────────────────────────────────────────────
SYMBOL_OVERRIDES = {
    'M&MFIN.NS':   'M%26MFIN',
    'M&M.NS':      'M%26M',
    'L&TFH.NS':    'L%26TFH',
    'HDFCAMC.NS':  'HDFCAMC',
}

def to_screener_symbol(ticker_ns):
    if ticker_ns in SYMBOL_OVERRIDES:
        return SYMBOL_OVERRIDES[ticker_ns]
    return ticker_ns.replace('.NS', '')

# ── Number parser ─────────────────────────────────────────────────────────────
def parse_num(text):
    if not text:
        return None
    text = text.strip().replace(',', '').replace('%', '').replace('₹', '').replace('\xa0', '')
    text = re.sub(r'\s*(Cr|L|K|M|B)\s*$', '', text, flags=re.IGNORECASE)
    try:
        return float(text)
    except:
        return None

# ── Scrape a single stock ─────────────────────────────────────────────────────
def scrape_screener(ticker_ns):
    symbol = to_screener_symbol(ticker_ns)
    urls = [
        f'https://www.screener.in/company/{symbol}/consolidated/',
        f'https://www.screener.in/company/{symbol}/',
    ]
    result = {}

    for url in urls:
        try:
            resp = requests.get(url, headers=SCREENER_HEADERS, timeout=TIMEOUT)
            if resp.status_code == 404:
                continue
            if resp.status_code != 200:
                print(f'    HTTP {resp.status_code}')
                continue

            soup = BeautifulSoup(resp.text, 'html.parser')

            # ── Key ratios ────────────────────────────────────────────────────
            ratios = soup.find('section', id='top-ratios')
            if ratios:
                for li in ratios.find_all('li'):
                    lbl = li.find('span', class_='name')
                    val = li.find('span', class_='nowrap') or li.find('span', class_='number')
                    if not lbl or not val:
                        continue
                    label = lbl.get_text(strip=True).lower()
                    v = parse_num(val.get_text(strip=True))
                    if v is None:
                        continue
                    if 'stock p/e' in label:
                        result.setdefault('pe_ttm', v)
                    elif 'return on equity' in label or label == 'roe':
                        result['roe'] = v
                    elif 'roce' in label:
                        result['roce'] = v
                    elif 'debt / equity' in label:
                        result['debt_to_equity'] = v
                    elif 'net profit margin' in label or 'net profit %' in label:
                        result['net_margin'] = v / 100 if v > 1 else v
                    elif 'dividend yield' in label:
                        result['dividend_yield'] = v / 100 if v > 1 else v
                    elif 'book value' in label:
                        result['book_value_per_share'] = v
                    elif label.startswith('eps') and 'eps_ttm' not in result:
                        result['eps_ttm'] = v

            # ── Shareholding ──────────────────────────────────────────────────
            sh = soup.find('section', id='shareholding')
            if sh:
                for tr in sh.find_all('tr'):
                    cells = tr.find_all('td')
                    if not cells:
                        continue
                    label = cells[0].get_text(strip=True).lower()
                    vals = [parse_num(c.get_text(strip=True)) for c in cells[1:]]
                    vals = [v for v in vals if v is not None]
                    if not vals:
                        continue
                    latest = vals[-1]
                    if 'promoter' in label:
                        result['promoter_holding_pct'] = latest
                    elif 'fii' in label or 'foreign inst' in label:
                        result['fii_holding_pct'] = latest
                    elif 'dii' in label or 'domestic inst' in label:
                        result['dii_holding_pct'] = latest

            # ── Quarterly P&L ─────────────────────────────────────────────────
            pl = soup.find('section', id='quarters')
            if pl:
                table = pl.find('table')
                if table:
                    headers = [th.get_text(strip=True) for th in table.find_all('th')]
                    q_labels = list(reversed(headers[1:][-8:]))  # newest first

                    def get_row(keyword):
                        for tr in table.find_all('tr'):
                            cells = tr.find_all('td')
                            if not cells:
                                continue
                            if keyword.lower() in cells[0].get_text(strip=True).lower():
                                vals = [parse_num(c.get_text(strip=True)) for c in cells[1:]]
                                return list(reversed(vals[-8:]))
                        return []

                    revenues    = get_row('sales')
                    net_profs   = get_row('net profit')
                    eps_vals    = get_row('eps')
                    ebitda_vals = get_row('operating profit')  # Screener calls it "Operating Profit"

                    quarterly = []
                    for i, q in enumerate(q_labels):
                        entry = {'quarter': q}
                        if i < len(revenues)    and revenues[i]    is not None: entry['revenue_cr']    = revenues[i]
                        if i < len(net_profs)   and net_profs[i]   is not None: entry['net_income_cr'] = net_profs[i]
                        if i < len(eps_vals)    and eps_vals[i]    is not None: entry['eps']           = eps_vals[i]
                        if i < len(ebitda_vals) and ebitda_vals[i] is not None: entry['ebitda_cr']     = ebitda_vals[i]
                        quarterly.append(entry)

                    if quarterly:
                        result['quarterly_financials'] = json.dumps(quarterly)

            # ── PE 3Y avg from chart script ───────────────────────────────────
            for script in soup.find_all('script'):
                text = script.get_text()
                if '"pe"' in text or 'pe_history' in text.lower():
                    pe_matches = re.findall(r'"pe"\s*:\s*([\d.]+)', text)
                    if pe_matches:
                        pe_vals = [float(p) for p in pe_matches if float(p) < 500]
                        if len(pe_vals) >= 4:
                            result['pe_3yr_avg'] = round(sum(pe_vals) / len(pe_vals), 1)
                    break

            result['source_screener'] = True
            return result

        except requests.exceptions.Timeout:
            print(f'    ⏱️  Timeout: {symbol}')
        except Exception as e:
            print(f'    ❌ Error: {e}')

    return result  # empty if both URLs failed

# ── Save batch to Supabase ────────────────────────────────────────────────────
def save_batch(supabase, records):
    success, failed = 0, 0
    for rec in records:
        try:
            supabase.table('stock_fundamentals').upsert(
                rec, on_conflict='ticker'
            ).execute()
            success += 1
        except Exception as e:
            print(f'    ⚠️  Save error {rec.get("ticker")}: {e}')
            failed += 1
    return success, failed

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print('=' * 70)
    print('📋 WEEKLY SCREENER.IN FUNDAMENTALS FETCHER')
    print(f'Started: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print('=' * 70)

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    # Get all tickers from indian_stock_sectors
    res = supabase.table('indian_stock_sectors').select('ticker, company_name').execute()
    tickers = [(r['ticker'], r['company_name']) for r in res.data if r.get('ticker', '').endswith('.NS')]
    print(f'📋 {len(tickers)} NSE tickers to process\n')

    success_total, failed_total, skip_total = 0, 0, 0
    pending_records = []

    for i, (ticker, name) in enumerate(tickers, 1):
        symbol = to_screener_symbol(ticker)
        print(f'[{i:3d}/{len(tickers)}] {ticker:<20} ({symbol})', end=' ', flush=True)

        data = scrape_screener(ticker)

        if not data or not data.get('source_screener'):
            print('⚠️  No data')
            skip_total += 1
        else:
            fields = [k for k in data if k not in ('source_screener',)]
            print(f'✓ {len(fields)} fields')
            record = {
                'ticker':         ticker,
                'stock_name':     name,
                'last_updated':   datetime.utcnow().isoformat(),
                'source_screener': True,
            }
            record.update({k: v for k, v in data.items() if v is not None})
            pending_records.append(record)

        # Save in batches
        if len(pending_records) >= BATCH_SIZE:
            s, f = save_batch(supabase, pending_records)
            success_total += s
            failed_total  += f
            pending_records = []
            print(f'  💾 Saved batch — total so far: {success_total}')

        # Polite delays
        time.sleep(SLEEP_BETWEEN)
        if i % 50 == 0:
            print(f'\n  ⏸️  Pause ({SLEEP_EVERY_50}s) after {i} stocks...\n')
            time.sleep(SLEEP_EVERY_50)

    # Save remaining
    if pending_records:
        s, f = save_batch(supabase, pending_records)
        success_total += s
        failed_total  += f

    print('\n' + '=' * 70)
    print(f'✅ Saved:   {success_total}')
    print(f'❌ Failed:  {failed_total}')
    print(f'⚠️  Skipped: {skip_total} (no Screener data)')
    print(f'Completed: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print('=' * 70)

if __name__ == '__main__':
    main()
