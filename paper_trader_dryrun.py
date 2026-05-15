#!/usr/bin/env python3
"""
Dry-run for paper_trader strategy v3 entry pipeline.
Reads only — no writes to paper_trades, paper_equity, or paper_run_log.
Prints the funnel and the candidates that WOULD be inserted today.
"""

import json
from collections import Counter
from datetime import date

from supabase import create_client

from paper_trader import (
    SUPABASE_URL, SUPABASE_KEY,
    MAX_NEW_PER_DAY, MAX_OPEN_POSITIONS, MAX_DEPLOYED_PCT,
    SLEEVE, SLIPPAGE_BPS,
    get_recent_trading_dates, load_snapshots_for_date, load_holdings,
    load_open_paper_trades, load_pending_paper_trades,
    load_recent_closed_tickers, load_sectors,
    load_sector_quadrants, load_presignals, load_entry_signals,
    load_fundamentals,
    group_signals_by_ticker, classify_entry_bucket, classify_presignal_bucket,
    passes_sector_filter, passes_earnings_filter, size_position,
    to_float,
)


def main():
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    recent_dates = get_recent_trading_dates(sb, days_back=5)
    if len(recent_dates) < 2:
        print('Insufficient trading data; aborting.')
        return
    today_date = date.fromisoformat(recent_dates[0])
    signal_date_from = today_date.isoformat()
    signal_date_to = today_date.isoformat()
    print(f'[DRY] today={today_date}  signal_window={signal_date_from}..{signal_date_to}')

    today_snap = load_snapshots_for_date(sb, today_date.isoformat())
    holdings = load_holdings(sb)
    open_trades = load_open_paper_trades(sb)
    pending_trades = load_pending_paper_trades(sb)
    recent_closed = load_recent_closed_tickers(sb, today_date)
    sector_map = load_sectors(sb)
    sector_quadrants = load_sector_quadrants(sb)
    # Committed = open + pending, matching paper_trader.main().
    committed_trades = open_trades + pending_trades
    open_tickers = {t['ticker'] for t in committed_trades}

    deployed = sum(to_float(t.get('entry_value'), 0) for t in committed_trades)
    cash_cap = SLEEVE * MAX_DEPLOYED_PCT
    headroom = cash_cap - deployed

    print(f'[DRY] open={len(open_trades)} pending={len(pending_trades)} '
          f'holdings={len(holdings)} cooldown={len(recent_closed)} '
          f'sectors_quadranted={len(sector_quadrants)}')
    print(f'[DRY] deployed={deployed:,.0f} ({deployed/SLEEVE*100:.1f}% of sleeve)  '
          f'cash_cap={cash_cap:,.0f} ({MAX_DEPLOYED_PCT*100:.0f}%)  '
          f'headroom={headroom:,.0f}')
    if sector_quadrants:
        qc = Counter(sector_quadrants.values())
        print(f'[DRY] sector quadrants today: {dict(qc)}')
        leading_improving = [s for s, q in sector_quadrants.items()
                             if q in ('leading', 'improving')]
        print(f'[DRY] leading/improving sectors ({len(leading_improving)}): '
              f'{leading_improving}')

    presignals = load_presignals(sb, signal_date_from, signal_date_to)
    entry_signals_data = load_entry_signals(sb, signal_date_from, signal_date_to)
    by_ticker = group_signals_by_ticker(presignals, entry_signals_data)
    print(f'[DRY] signals: presignal={len(presignals)} entry={len(entry_signals_data)} '
          f'tickers={len(by_ticker)}')

    funnel = {
        'signal_rows': len(presignals) + len(entry_signals_data),
        'unique_tickers': len(by_ticker),
        'no_bucket': 0,
        'in_holdings': 0, 'in_cooldown': 0, 'already_open': 0,
        'sector_rejected': 0, 'earnings_rejected': 0,
        'qualified': 0,
        'bucket_counts_raw': {},
        'bucket_counts_qualified': {},
        'sector_reasons': {}, 'earnings_reasons': {},
    }

    raw_candidates = []
    for ticker, bundle in by_ticker.items():
        bucket, families, fam_strength, score = classify_entry_bucket(
            bundle['entry_signal_rows']
        )
        presignal_row = None
        if not bucket:
            bucket, families, fam_strength, score, presignal_row = (
                classify_presignal_bucket(bundle['presignal_rows'])
            )
        if not bucket:
            funnel['no_bucket'] += 1
            continue
        funnel['bucket_counts_raw'][bucket] = (
            funnel['bucket_counts_raw'].get(bucket, 0) + 1
        )
        raw_candidates.append({
            'ticker': ticker, 'tier': bucket, 'families': families,
            'family_strength': fam_strength, 'signal_score': score,
        })

    print(f'[DRY] bucketed (pre-gate): {len(raw_candidates)} '
          f'distribution={funnel["bucket_counts_raw"]}')

    fundamentals = load_fundamentals(sb, [c['ticker'] for c in raw_candidates])
    print(f'[DRY] fundamentals fetched for {len(fundamentals)}/{len(raw_candidates)} '
          f'tickers (quarterly_financials present)')

    candidates = []
    rejected = []
    for c in raw_candidates:
        ticker = c['ticker']
        if ticker in holdings:
            funnel['in_holdings'] += 1
            rejected.append((ticker, c['tier'], 'in_holdings'))
            continue
        if ticker in recent_closed:
            funnel['in_cooldown'] += 1
            rejected.append((ticker, c['tier'], 'in_cooldown'))
            continue
        if ticker in open_tickers:
            funnel['already_open'] += 1
            rejected.append((ticker, c['tier'], 'already_open'))
            continue
        sec_ok, sec_reason = passes_sector_filter(
            ticker, sector_map, sector_quadrants
        )
        if not sec_ok:
            funnel['sector_rejected'] += 1
            funnel['sector_reasons'][sec_reason] = (
                funnel['sector_reasons'].get(sec_reason, 0) + 1
            )
            rejected.append((ticker, c['tier'], f'sector:{sec_reason}'))
            continue
        e_ok, e_reason = passes_earnings_filter(ticker, fundamentals)
        if not e_ok:
            funnel['earnings_rejected'] += 1
            funnel['earnings_reasons'][e_reason] = (
                funnel['earnings_reasons'].get(e_reason, 0) + 1
            )
            rejected.append((ticker, c['tier'], f'earnings:{e_reason}'))
            continue
        funnel['bucket_counts_qualified'][c['tier']] = (
            funnel['bucket_counts_qualified'].get(c['tier'], 0) + 1
        )
        candidates.append(c)
    funnel['qualified'] = len(candidates)

    tier_order = {
        'T1_MULTI_STRONG': 0, 'T2_STRONG_REG': 1,
        'T3_MULTI_REG': 2, 'T4_RS_ACCEL': 3,
    }
    candidates.sort(key=lambda c: (tier_order[c['tier']], -c['signal_score']))
    slots = max(0, min(MAX_NEW_PER_DAY, MAX_OPEN_POSITIONS - len(committed_trades)))

    # Simulate insertion: walk ranked candidates applying the slot count AND
    # the cash gate (notional sized off D0 close, matching process_entries).
    # Sector-concentration is not modelled here — see header note.
    sim_deployed = deployed
    would_insert = set()
    for c in candidates:
        if len(would_insert) >= slots:
            break
        snap = today_snap.get(c['ticker']) or {}
        close_today = to_float(snap.get('close'))
        atr14 = to_float(snap.get('atr_14'))
        if not close_today or not atr14 or atr14 <= 0:
            continue
        entry_px = close_today * (1 + SLIPPAGE_BPS / 10_000)
        qty, notional, _ = size_position(c['tier'], entry_px, atr14)
        if qty == 0:
            continue
        if sim_deployed + notional > cash_cap:
            continue
        sim_deployed += notional
        would_insert.add(c['ticker'])

    print()
    print('=' * 90)
    print('FUNNEL')
    print('=' * 90)
    print(json.dumps(funnel, indent=2))

    print()
    print('=' * 90)
    print(f'QUALIFIED CANDIDATES ({len(would_insert)} would insert — slots={slots}, '
          f'cash gate applied, sector-conc NOT modelled)')
    print('=' * 90)
    if not candidates:
        print('  <none>')
    for c in candidates[:30]:
        snap = today_snap.get(c['ticker']) or {}
        close_today = to_float(snap.get('close'))
        atr14 = to_float(snap.get('atr_14'))
        if close_today and atr14 and atr14 > 0:
            entry_px = close_today * (1 + SLIPPAGE_BPS / 10_000)
            qty, notional, _ = size_position(c['tier'], entry_px, atr14)
            sizing = f'qty={qty:>5d} notl={notional:>10,.0f}'
        else:
            sizing = 'NO_SNAP_OR_ATR'
        sector = sector_map.get(c['ticker'], '?')
        quadrant = sector_quadrants.get(sector, '?')
        flag = ' <-- WOULD INSERT' if c['ticker'] in would_insert else ''
        print(f"  [{c['tier']}] {c['ticker']:18s} score={c['signal_score']:6.1f}  "
              f"fam={','.join(c['families'])[:35]:35s}  "
              f"sec={sector[:18]:18s} q={quadrant:9s} {sizing}{flag}")

    print()
    print('=' * 90)
    print('SAMPLE REJECTIONS (up to 25)')
    print('=' * 90)
    for t, tier, reason in rejected[:25]:
        print(f'  [{tier}] {t:18s} {reason}')


if __name__ == '__main__':
    main()
