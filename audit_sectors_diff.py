#!/usr/bin/env python3
"""
Stage 2 of the sector audit: take sector_audit_screener.csv produced by
audit_sectors_screener.py and produce sector_audit_diff.csv, which classifies
each ticker into one of:

  match         — our_sector matches what Screener implies under our taxonomy
  mismatch      — our_sector differs from Screener-implied sector (review)
  unmapped      — Screener returned a sector we haven't mapped yet (extend MAPPING)
  no_data       — Screener returned nothing for this ticker

The mapping uses Screener's (broad_sector, sector, broad_industry) tuple, most
specific first. Wildcards are None.

Usage:
    python audit_sectors_diff.py
    python audit_sectors_diff.py --only-mismatch    # filter to actionable rows
"""

import argparse
import csv
from pathlib import Path
from collections import Counter

IN_CSV  = Path(__file__).parent / 'sector_audit_screener.csv'
OUT_CSV = Path(__file__).parent / 'sector_audit_diff.csv'


# (broad_sector, sector, broad_industry) -> our taxonomy.  None = wildcard.
# Order doesn't matter; lookup walks most specific to least specific.
MAPPING = {
    # ── Financial Services ────────────────────────────────────────────
    ('Financial Services', None, 'Banks'):              'BANKING',
    ('Financial Services', None, 'Finance'):            'FINANCIAL SERVICES',
    ('Financial Services', None, 'Insurance'):          'FINANCIAL SERVICES',
    ('Financial Services', None, 'Diversified Financial Services'): 'FINANCIAL SERVICES',
    ('Financial Services', None, 'Financial Technology (Fintech)'): 'FINANCIAL SERVICES',
    ('Financial Services', None, 'Capital Markets'):    'FINANCIAL SERVICES',
    ('Financial Services', None, None):                 'FINANCIAL SERVICES',

    # ── Information Technology ────────────────────────────────────────
    ('Information Technology', None, None):             'IT',

    # ── Energy / Power / Utilities ────────────────────────────────────
    ('Energy', None, None):                             'ENERGY',
    ('Utilities', None, None):                          'ENERGY',
    ('Commodities', 'Oil, Gas & Consumable Fuels', None): 'ENERGY',

    # ── Commodities (split into our buckets by sector) ───────────────
    ('Commodities', 'Chemicals', None):                 'CHEMICALS',
    ('Commodities', 'Metals & Mining', None):           'METALS',
    ('Commodities', 'Construction Materials', None):    'CEMENT',
    ('Commodities', 'Forest Materials', None):          'INDUSTRIAL',
    ('Commodities', 'Fertilizers & Agrochemicals', None): 'CHEMICALS',
    ('Commodities', 'Paper, Forest & Jute Products', None): 'INDUSTRIAL',
    ('Commodities', None, None):                        'CHEMICALS',  # generic fallback

    # ── Consumer Discretionary (most varied) ──────────────────────────
    ('Consumer Discretionary', 'Automobile and Auto Components', 'Automobiles'):
        'AUTOMOBILE',
    ('Consumer Discretionary', 'Automobile and Auto Components', 'Auto Components'):
        'AUTO COMPONENTS',
    ('Consumer Discretionary', 'Automobile and Auto Components', None):
        'AUTO COMPONENTS',
    ('Consumer Discretionary', 'Consumer Services', None):     'CONSUMER SERVICES',
    ('Consumer Discretionary', 'Consumer Durables', None):     'CONSUMER DURABLES',
    ('Consumer Discretionary', 'Textiles', None):              'TEXTILES',
    ('Consumer Discretionary', 'Realty', None):                'REALTY',
    ('Consumer Discretionary', 'Media, Entertainment & Publication', None):
        'MEDIA',

    # ── FMCG ─────────────────────────────────────────────────────────
    ('Fast Moving Consumer Goods', None, None):         'FMCG',
    ('Consumer Staples', None, None):                   'FMCG',  # alt name

    # ── Healthcare ───────────────────────────────────────────────────
    ('Health Care', None, None):                        'HEALTHCARE',
    ('Healthcare', None, None):                         'HEALTHCARE',

    # ── Telecom ──────────────────────────────────────────────────────
    ('Telecommunication', None, None):                  'TELECOM',
    ('Communication Services', None, None):             'TELECOM',

    # ── Industrials / Capital Goods ──────────────────────────────────
    ('Capital Goods', None, None):                      'INDUSTRIAL',
    ('Industrials', None, None):                        'INDUSTRIAL',

    # ── Construction / Infrastructure ────────────────────────────────
    ('Construction', None, None):                       'INFRASTRUCTURE',

    # ── Services (logistics / defence / aviation) ────────────────────
    ('Services', 'Transport Services', None):           'LOGISTICS',
    ('Services', None, 'Logistics'):                    'LOGISTICS',
    ('Services', None, 'Transport Infrastructure'):     'LOGISTICS',
    ('Services', None, 'Defence'):                      'DEFENCE',
    ('Services', None, 'Aerospace & Defense'):          'DEFENCE',
    ('Services', None, 'Airlines'):                     'AVIATION',
    ('Services', None, None):                           'CONSUMER SERVICES',  # fallback

    # ── Diversified / Conglomerate ───────────────────────────────────
    ('Diversified', None, None):                        'CONGLOMERATE',
}


def map_screener_to_ours(broad_sector: str, sector: str, broad_industry: str):
    """Walk most-specific to least-specific lookup keys."""
    keys = [
        (broad_sector, sector, broad_industry),
        (broad_sector, sector, None),
        (broad_sector, None, broad_industry),
        (broad_sector, None, None),
    ]
    for k in keys:
        if k in MAPPING:
            return MAPPING[k]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--only-mismatch', action='store_true',
                    help='Filter output to mismatches only.')
    args = ap.parse_args()

    if not IN_CSV.exists():
        print(f'Input not found: {IN_CSV}. Run audit_sectors_screener.py first.')
        return 1

    rows = []
    with IN_CSV.open(encoding='utf-8') as f:
        rows = list(csv.DictReader(f))

    out_rows = []
    counts = Counter()
    unmapped_keys = Counter()
    mismatch_pairs = Counter()

    for r in rows:
        bs = r.get('screener_broad_sector', '').strip()
        sec = r.get('screener_sector', '').strip()
        bi = r.get('screener_broad_industry', '').strip()
        ind = r.get('screener_industry', '').strip()
        ours = (r.get('our_sector') or '').strip().upper()
        status = (r.get('status') or '').strip()

        if status != 'ok' or not bs:
            verdict = 'no_data'
            expected = ''
        else:
            expected = map_screener_to_ours(bs, sec, bi) or ''
            if not expected:
                verdict = 'unmapped'
                unmapped_keys[(bs, sec, bi)] += 1
            elif expected.upper() == ours:
                verdict = 'match'
            else:
                verdict = 'mismatch'
                mismatch_pairs[(ours, expected)] += 1

        counts[verdict] += 1
        out_rows.append({
            'ticker':                  r['ticker'],
            'company_name':            r.get('company_name', ''),
            'our_sector':              ours,
            'expected_sector':         expected,
            'verdict':                 verdict,
            'screener_broad_sector':   bs,
            'screener_sector':         sec,
            'screener_broad_industry': bi,
            'screener_industry':       ind,
            'status':                  status,
        })

    # Sort: mismatches first, then unmapped, then no_data, then match
    order = {'mismatch': 0, 'unmapped': 1, 'no_data': 2, 'match': 3}
    out_rows.sort(key=lambda r: (order.get(r['verdict'], 9), r['our_sector'], r['ticker']))

    if args.only_mismatch:
        out_rows = [r for r in out_rows if r['verdict'] == 'mismatch']

    with OUT_CSV.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()) if out_rows else
                                ['ticker', 'verdict'])
        writer.writeheader()
        writer.writerows(out_rows)

    print(f'Wrote {OUT_CSV}  ({len(out_rows)} rows)')
    print('\nSummary:')
    for verdict in ('match', 'mismatch', 'unmapped', 'no_data'):
        print(f'  {verdict:<10} {counts.get(verdict, 0)}')

    if unmapped_keys:
        print('\nUnmapped Screener tuples (add to MAPPING in audit_sectors_diff.py):')
        for k, n in unmapped_keys.most_common():
            print(f'  {n:3d}  {k}')

    if mismatch_pairs:
        print('\nTop (our → expected) mismatch pairs:')
        for (ours, exp), n in mismatch_pairs.most_common(20):
            print(f'  {n:3d}  {ours!r:25s} → {exp!r}')

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
