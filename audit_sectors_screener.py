#!/usr/bin/env python3
"""
Sector classification audit — Screener.in backfill.

Fetches the BSE sectoral classification breadcrumb (Broad Sector / Sector /
Broad Industry / Industry) for every ticker in indian_stock_sectors and
writes the result to:
  - sector_audit_screener.csv  (raw scrape, one row per ticker)

A second pass (audit_sectors_diff.py) applies a Screener-to-our-taxonomy
mapping and produces sector_audit_diff.csv with the mismatches highlighted.

Runtime: ~20-25 min for 606 tickers at 2s/page + 10s pause every 50.

Usage:
    python audit_sectors_screener.py
    python audit_sectors_screener.py --resume     # skip tickers already in CSV
    python audit_sectors_screener.py --limit 50   # test run
"""

import argparse
import csv
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from supabase import create_client

# ── Config ────────────────────────────────────────────────────────────────────
SUPABASE_URL = os.getenv('SUPABASE_URL', 'https://hcgyncghmcvylnrmcivj.supabase.co')
SUPABASE_KEY = os.getenv(
    'SUPABASE_KEY',
    'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhjZ3luY2dobWN2eWxucm1jaXZqIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTc1MTQwMTEsImV4cCI6MjA3MzA5MDAxMX0.n8vFVCJe1y_3o8fpAY0IgasZ4eKl7DAogEM3OlHB8Ww',
)

SLEEP_BETWEEN  = 2.0
SLEEP_EVERY_50 = 10
TIMEOUT        = 15

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-IN,en;q=0.9',
    'Referer': 'https://www.screener.in/',
}

# Reuse the override map from fetch_screener_fundamentals.py
SYMBOL_OVERRIDES = {
    'M&MFIN.NS':   'M%26MFIN',
    'M&M.NS':      'M%26M',
    'L&TFH.NS':    'L%26TFH',
    'HDFCAMC.NS':  'HDFCAMC',
}

TITLE_MAP = {
    'Broad Sector':   'screener_broad_sector',
    'Sector':         'screener_sector',
    'Broad Industry': 'screener_broad_industry',
    'Industry':       'screener_industry',
}

OUT_CSV = Path(__file__).parent / 'sector_audit_screener.csv'


def to_screener_symbol(ticker_ns: str) -> str:
    if ticker_ns in SYMBOL_OVERRIDES:
        return SYMBOL_OVERRIDES[ticker_ns]
    return ticker_ns.replace('.NS', '')


def fetch_sector(symbol: str):
    """Return dict with the 4 Screener levels, or None on 404 / no breadcrumb."""
    for path in (f'/company/{symbol}/consolidated/', f'/company/{symbol}/'):
        try:
            r = requests.get('https://www.screener.in' + path, headers=HEADERS, timeout=TIMEOUT)
        except requests.exceptions.Timeout:
            return {'_error': 'timeout'}
        except Exception as e:
            return {'_error': f'request_error: {e}'}
        if r.status_code == 404:
            continue
        if r.status_code != 200:
            return {'_error': f'http_{r.status_code}'}
        soup = BeautifulSoup(r.text, 'html.parser')
        out = {}
        for a in soup.find_all('a', title=True, href=True):
            t = a.get('title', '').strip()
            if t in TITLE_MAP and a['href'].startswith('/market/'):
                out[TITLE_MAP[t]] = a.get_text(strip=True)
        if out:
            return out
    return None


def load_done_tickers(csv_path: Path) -> set:
    """Read tickers already scraped from the CSV — used by --resume."""
    if not csv_path.exists():
        return set()
    with csv_path.open('r', encoding='utf-8', newline='') as f:
        return {row['ticker'] for row in csv.DictReader(f) if row.get('ticker')}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--resume', action='store_true',
                    help='Skip tickers already present in the CSV.')
    ap.add_argument('--limit', type=int, default=None,
                    help='Stop after N new tickers (for testing).')
    args = ap.parse_args()

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    res = sb.table('indian_stock_sectors') \
            .select('ticker, company_name, sector') \
            .order('ticker').execute()
    rows = [r for r in (res.data or []) if r['ticker'].endswith('.NS')]
    print(f'Tickers in DB: {len(rows)}')

    done = load_done_tickers(OUT_CSV) if args.resume else set()
    if done:
        print(f'Resuming — {len(done)} already scraped, skipping those.')

    fieldnames = ['ticker', 'company_name', 'our_sector',
                  'screener_broad_sector', 'screener_sector',
                  'screener_broad_industry', 'screener_industry',
                  'status']

    write_header = not OUT_CSV.exists()
    mode = 'a' if (args.resume and OUT_CSV.exists()) else 'w'
    if mode == 'w':
        write_header = True

    f = OUT_CSV.open(mode, encoding='utf-8', newline='')
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    if write_header:
        writer.writeheader()

    processed = 0
    errors = 0
    try:
        for i, row in enumerate(rows, 1):
            ticker = row['ticker']
            if ticker in done:
                continue
            symbol = to_screener_symbol(ticker)
            result = fetch_sector(symbol)
            if result is None:
                status = 'no_breadcrumb'
                rec = {k: '' for k in fieldnames}
                errors += 1
            elif '_error' in result:
                status = result['_error']
                rec = {k: '' for k in fieldnames}
                errors += 1
            else:
                status = 'ok'
                rec = result
            out = {
                'ticker': ticker,
                'company_name': row.get('company_name') or '',
                'our_sector': row.get('sector') or '',
                'screener_broad_sector':   rec.get('screener_broad_sector', ''),
                'screener_sector':         rec.get('screener_sector', ''),
                'screener_broad_industry': rec.get('screener_broad_industry', ''),
                'screener_industry':       rec.get('screener_industry', ''),
                'status': status,
            }
            writer.writerow(out)
            f.flush()
            processed += 1

            print(f'[{i:3d}/{len(rows)}] {ticker:<20} {symbol:<25} '
                  f'{status:<14} {out["screener_broad_sector"]:<25} '
                  f'{out["screener_sector"]}', flush=True)

            if args.limit and processed >= args.limit:
                print(f'Stopped at --limit {args.limit}.')
                break

            time.sleep(SLEEP_BETWEEN)
            if processed % 50 == 0:
                print(f'  ⏸️  Pause {SLEEP_EVERY_50}s after {processed} new tickers...')
                time.sleep(SLEEP_EVERY_50)
    finally:
        f.close()

    print(f'\nDone. processed={processed} errors={errors} csv={OUT_CSV}')


if __name__ == '__main__':
    main()
