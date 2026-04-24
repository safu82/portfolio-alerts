#!/usr/bin/env python3
"""
Weekly Screener.in Fundamentals Fetcher
Scrapes qualitative fundamentals for all ~450 stocks in indian_stock_sectors.

Fetches (Screener.in only — yfinance handled separately by fetch_yfinance_metrics.py):
  - Quarterly P&L (Revenue, Net Profit, EBITDA, EPS — last 8 quarters)
  - Key ratios: PE TTM, ROE, ROCE, Debt/Equity, Net Margin, Book Value
  - Shareholding: Promoter, FII, DII
  - PE 3Y Avg (from chart data if available)
  - Compounded growth rates: Sales, Profit, Stock Price CAGR, ROE (10Y/5Y/3Y/TTM)

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

# ── Growth section header → DB column prefix mapping ─────────────────────────
GROWTH_SECTIONS = {
    'Compounded Sales Growth':  'sales_growth',
    'Compounded Profit Growth': 'profit_growth',
    'Stock Price CAGR':         'price_cagr',
    'Return on Equity':         'roe',
}

PERIOD_MAP = {
    '10 years:': '10y',
    '5 years:':  '5y',
    '3 years:':  '3y',
    'ttm:':      'ttm',
    '1 year:':   '1y',
    'last year:': 'last_year',
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
                all_labels = [li.find('span', class_='name').get_text(strip=True)
                              for li in ratios.find_all('li')
                              if li.find('span', class_='name')]
                if not hasattr(scrape_screener, '_labels_printed'):
                    print(f'    📋 Screener ratio labels: {all_labels}')
                    for li in ratios.find_all('li')[:3]:
                        print(f'    🔍 LI HTML: {str(li)[:200]}')
                    scrape_screener._labels_printed = True
                for li in ratios.find_all('li'):
                    lbl = li.find('span', class_='name')
                    if not lbl:
                        continue
                    label = lbl.get_text(strip=True).lower()
                    val_el = (li.find('span', class_='nowrap') or
                              li.find('span', class_='number') or
                              li.find('span', class_='value'))
                    raw_text = val_el.get_text(strip=True) if val_el else li.get_text(strip=True).replace(lbl.get_text(strip=True), '').strip()
                    v = parse_num(raw_text)
                    if v is None:
                        continue
                    if 'stock p/e' in label or label == 'p/e':
                        result.setdefault('pe_ttm', v)
                    elif 'return on equity' in label or 'roe' in label:
                        result['roe'] = v
                    elif 'roce' in label or 'return on capital' in label:
                        result['roce'] = v
                    elif 'debt / equity' in label or 'debt/equity' in label:
                        result['debt_to_equity'] = v
                    elif 'net profit margin' in label or 'net profit %' in label or 'net margin' in label:
                        result['net_margin'] = v / 100 if v > 1 else v
                    elif 'dividend yield' in label:
                        result['dividend_yield'] = v / 100 if v > 1 else v
                    elif 'book value' in label:
                        result['book_value_per_share'] = v
                    elif label.startswith('eps') and 'eps_ttm' not in result:
                        result['eps_ttm'] = v
                    elif 'opm' in label or 'operating profit margin' in label:
                        result['ebitda_margin'] = v / 100 if v > 1 else v

            # ── Compounded growth rates ───────────────────────────────────────
            for th in soup.find_all('th', colspan='2'):
                section_name = th.get_text(strip=True)
                if section_name not in GROWTH_SECTIONS:
                    continue
                prefix = GROWTH_SECTIONS[section_name]
                table  = th.find_parent('table')
                if not table:
                    continue
                for tr in table.find_all('tr'):
                    cells = tr.find_all('td')
                    if len(cells) < 2:
                        continue
                    period_label = cells[0].get_text(strip=True).lower()
                    value_text   = cells[1].get_text(strip=True)
                    period_key   = PERIOD_MAP.get(period_label)
                    if not period_key:
                        continue
                    val = parse_num(value_text)
                    if val is not None:
                        result[f'{prefix}_{period_key}'] = val

            # ── Shareholding ──────────────────────────────────────────────────
            sh = soup.find('section', id='shareholding')
            if sh:
                for tr in sh.find_all('tr'):
                    cells = tr.find_all('td')
                    if not cells:
                        continue
                    label = cells[0].get_text(strip=True).lower()
                    vals  = [parse_num(c.get_text(strip=True)) for c in cells[1:]]
                    vals  = [v for v in vals if v is not None]
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
                    headers  = [th.get_text(strip=True) for th in table.find_all('th')]
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
                    ebitda_vals = get_row('operating profit')

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

            # ── Compute ROCE and ROE from profit-loss + balance-sheet ─────────
            try:
                def get_table_row(section_id, row_label):
                    sec = soup.find('section', id=section_id)
                    if not sec: return None
                    tbl = sec.find('table')
                    if not tbl: return None
                    for tr in tbl.find_all('tr'):
                        cells = tr.find_all(['td', 'th'])
                        if not cells: continue
                        if row_label.lower() in cells[0].get_text(strip=True).lower():
                            for cell in reversed(cells[1:]):
                                v = parse_num(cell.get_text(strip=True))
                                if v is not None:
                                    return v
                    return None

                op_profit    = get_table_row('profit-loss', 'operating profit')
                other_income = get_table_row('profit-loss', 'other income')
                total_assets = get_table_row('balance-sheet', 'total assets')
                other_liab   = get_table_row('balance-sheet', 'other liabilities')
                if op_profit and total_assets and other_liab:
                    ebit = op_profit + (other_income or 0)
                    capital_employed = total_assets - other_liab
                    if capital_employed > 0:
                        result['roce'] = round(ebit / capital_employed * 100, 1)

                net_profit = get_table_row('profit-loss', 'net profit')
                equity_cap = get_table_row('balance-sheet', 'equity capital')
                reserves   = get_table_row('balance-sheet', 'reserves')
                if net_profit and equity_cap is not None and reserves is not None:
                    equity = equity_cap + reserves
                    if equity > 0:
                        result['roe'] = round(net_profit / equity * 100, 1)

                sales = get_table_row('profit-loss', 'sales')
                if net_profit and sales and sales > 0:
                    result.setdefault('net_margin', round(net_profit / sales * 100, 1) / 100)

            except Exception as e:
                print(f'    ⚠️  ROCE/ROE compute error: {e}')

            result['source_screener'] = True
            return result

        except requests.exceptions.Timeout:
            print(f'    ⏱️  Timeout: {symbol}')
        except Exception as e:
            print(f'    ❌ Error: {e}')

    return result

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

    res     = supabase.table('indian_stock_sectors').select('ticker, company_name').execute()
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
                'ticker':          ticker,
                'stock_name':      name,
                'last_updated':    datetime.utcnow().isoformat(),
                'source_screener': True,
            }
            record.update({k: v for k, v in data.items() if v is not None})
            pending_records.append(record)

        if len(pending_records) >= BATCH_SIZE:
            s, f = save_batch(supabase, pending_records)
            success_total += s
            failed_total  += f
            pending_records = []
            print(f'  💾 Saved batch — total so far: {success_total}')

        time.sleep(SLEEP_BETWEEN)
        if i % 50 == 0:
            print(f'\n  ⏸️  Pause ({SLEEP_EVERY_50}s) after {i} stocks...\n')
            time.sleep(SLEEP_EVERY_50)

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
