"""
Zerodha Live Price Updater - Railway Cloud Version
==================================================
Runs 24/7 on Railway.app, automatically fetching prices during market hours.
All configuration from environment variables (no .env file needed).

UPDATED April 2026:
- Added daily NSE market cap fetch at 3:45 PM IST
- Writes market_cap_gdp_ratio to market_ratios table
- No impact on live price fetching
"""

from kiteconnect import KiteConnect
from supabase import create_client
from datetime import date, datetime
import os
import time
import pytz
import requests

# Paper-trading constants + helpers — reused for pending-fill and stop logic.
# paper_trader.py is co-deployed with this worker on Railway.
from paper_trader import (
    SLEEVE as PAPER_SLEEVE,
    SLIPPAGE_BPS as PAPER_SLIPPAGE_BPS,
    STOP_ATR_MULT as PAPER_STOP_ATR_MULT,
    TIER_PARAMS as PAPER_TIER_PARAMS,
    POSITION_FLOOR as PAPER_POSITION_FLOOR,
    size_position as paper_size_position,
    trading_days_between as paper_trading_days_between,
    to_float as paper_to_float,
    to_int as paper_to_int,
)

# Configuration from environment variables
ZERODHA_API_KEY = os.environ.get('ZERODHA_API_KEY')
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')

PENDING_MAX_TRADING_DAYS = 2

# Initialize
supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
ist = pytz.timezone('Asia/Kolkata')

# Ticker mappings
TICKER_MAPPING = {
    'KPEL': 'KPEL.NS',
    'TARIL': 'TRIL.NS',
    'GENUSPOWER': 'GENUSPOWER.NS',
    'DENTA': 'DENTAWATER.NS'
}

# GDP config — update quarterly from MoSPI
GDP_USD_BILLIONS = 3970  # FY 2025-26 First Advance Estimates

# Index instruments fetched by exchange:tradingsymbol (more reliable than raw tokens)
INDEX_INSTRUMENTS = {
    'NSE:NIFTY 50':          'NIFTY50.NS',
    'NSE:NIFTY 500':         'NIFTY500.NS',
    'NSE:NIFTY MIDCAP 100':  'NIFTYMIDCAP100.NS',
    'NSE:NIFTY SMLCAP 100':  'NIFTYSMLCAP100.NS',
}

def get_access_token_from_supabase():
    """Fetch the latest access token from Supabase (updated daily by GitHub Actions)"""
    try:
        result = supabase.table('zerodha_config').select('value').eq('id', 'zerodha_access_token').single().execute()
        if result.data:
            return result.data['value']
    except Exception as e:
        print(f"❌ Error fetching token from Supabase: {e}")
    return None

def get_portfolio_tickers():
    """Get all tickers from portfolio and transactions"""
    tickers = set()
    try:
        holdings = supabase.table('holdings').select('ticker').execute()
        for h in holdings.data:
            if h['ticker'] and (h['ticker'].endswith('.NS') or h['ticker'].endswith('.BO')):
                tickers.add(h['ticker'])

        transactions = supabase.table('transactions').select('ticker').execute()
        for t in transactions.data:
            if t['ticker'] and (t['ticker'].endswith('.NS') or t['ticker'].endswith('.BO')):
                tickers.add(t['ticker'])
    except Exception as e:
        print(f"⚠️  Error getting portfolio tickers: {e}")

    return tickers


def get_paper_active_tickers():
    """Active paper trades — pending (waiting for D1 open fill) + open."""
    tickers = set()
    try:
        resp = (supabase.table('paper_trades').select('ticker, status, mode')
                  .in_('status', ['pending', 'open']).eq('mode', 'paper').execute())
        for r in (resp.data or []):
            t = r.get('ticker')
            if t and (t.endswith('.NS') or t.endswith('.BO')):
                tickers.add(t)
    except Exception as e:
        print(f"⚠️  Error getting active paper tickers: {e}")
    return tickers


def get_all_streaming_tickers():
    """Union of real portfolio tickers + active paper-trade tickers."""
    return get_portfolio_tickers() | get_paper_active_tickers()

def get_instrument_mappings(kite, tickers):
    """Map Yahoo tickers to Zerodha instrument tokens"""
    print("📥 Downloading instruments from Zerodha...")

    nse_instruments = kite.instruments("NSE")
    bse_instruments = kite.instruments("BSE")
    all_instruments = nse_instruments + bse_instruments

    token_to_ticker = {}
    found = []
    missing = []

    for yahoo_ticker in tickers:
        if yahoo_ticker.endswith('.NS'):
            exchange = 'NSE'
            symbol = yahoo_ticker.replace('.NS', '')
        elif yahoo_ticker.endswith('.BO'):
            exchange = 'BSE'
            symbol = yahoo_ticker.replace('.BO', '')
        else:
            continue

        # Check special mappings
        for zerodha_sym, yahoo_sym in TICKER_MAPPING.items():
            if yahoo_ticker == yahoo_sym:
                symbol = zerodha_sym
                if yahoo_sym in ['GENUSPOWER.NS', 'DENTAWATER.NS']:
                    exchange = 'BSE'
                break

        # Find instrument
        found_inst = False
        for inst in all_instruments:
            if (inst['tradingsymbol'] == symbol and
                inst['exchange'] == exchange and
                inst['instrument_type'] == 'EQ'):
                token = inst['instrument_token']
                token_to_ticker[token] = yahoo_ticker
                found.append(f"{yahoo_ticker}")
                found_inst = True
                break

        if not found_inst:
            missing.append(f"{yahoo_ticker}")

    print(f"✅ Mapped {len(found)} stocks")
    if missing:
        print(f"⚠️  Could not find: {', '.join(missing[:3])}")

    return token_to_ticker

def is_market_open():
    """Check if market is currently open (9:15 AM - 3:30 PM IST, Mon-Fri)"""
    now = datetime.now(ist)
    if now.weekday() >= 5:
        return False
    market_start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_end = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_start <= now <= market_end

def fetch_and_update_prices(kite, token_to_ticker):
    """Fetch quotes from Zerodha and update Supabase"""
    if not token_to_ticker:
        return 0

    try:
        tokens = list(token_to_ticker.keys())
        quotes = kite.quote(tokens)

        if not quotes:
            return 0

        updates = []
        now = datetime.now(ist)

        for token_str, quote_data in quotes.items():
            if ':' in str(token_str):
                for token, ticker in token_to_ticker.items():
                    if ticker in str(token_str):
                        break
            else:
                token = int(token_str)

            if token not in token_to_ticker:
                continue

            yahoo_ticker = token_to_ticker[token]
            last_price = quote_data.get('last_price', 0)
            ohlc = quote_data.get('ohlc', {})
            prev_close = ohlc.get('close', 0)

            if last_price and prev_close:
                day_change = last_price - prev_close
                day_change_pct = (day_change / prev_close * 100) if prev_close > 0 else 0
                updates.append({
                    'ticker': yahoo_ticker,
                    'price': last_price,
                    'day_change': day_change,
                    'day_change_pct': day_change_pct,
                    'prev_close': prev_close,
                    'volume': quote_data.get('volume', 0),
                    'updated_at': now.isoformat()
                })

        if updates:
            supabase.table('live_prices').upsert(updates, on_conflict='ticker').execute()
            return len(updates)

        return 0

    except Exception as e:
        print(f"❌ Error fetching prices: {e}")
        return 0


def fetch_and_update_index_prices(kite):
    """
    Fetch NIFTY 50 and NIFTY 500 from Zerodha using exchange:tradingsymbol format.
    More reliable than raw instrument tokens.
    """
    try:
        symbols = list(INDEX_INSTRUMENTS.keys())
        quotes = kite.quote(symbols)
        if not quotes:
            return 0

        updates = []
        now = datetime.now(ist)

        for symbol, quote_data in quotes.items():
            ticker = INDEX_INSTRUMENTS.get(symbol)
            if not ticker:
                continue

            last_price = quote_data.get('last_price', 0)
            ohlc = quote_data.get('ohlc', {})
            prev_close = ohlc.get('close', 0)

            if last_price and prev_close:
                day_change = last_price - prev_close
                day_change_pct = (day_change / prev_close * 100) if prev_close > 0 else 0
                updates.append({
                    'ticker': ticker,
                    'price': last_price,
                    'day_change': day_change,
                    'day_change_pct': day_change_pct,
                    'prev_close': prev_close,
                    'volume': quote_data.get('volume', 0),
                    'updated_at': now.isoformat()
                })

        if updates:
            supabase.table('live_prices').upsert(updates, on_conflict='ticker').execute()
            return len(updates)

        return 0

    except Exception as e:
        print(f"⚠️  Index price fetch error: {e}")
        return 0

def fetch_and_store_market_cap():
    """
    Fetch NSE total market cap once daily at 3:45 PM IST after market close.
    Railway's IP is not blocked by NSE unlike GitHub Actions / Supabase Edge Functions.
    Writes nse_total_mcap_usd_billions and market_cap_gdp_ratio to market_ratios table.
    """
    HOME = "https://www.nseindia.com"
    API  = "https://www.nseindia.com/api/marketStatus"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-IN,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
        "Connection": "keep-alive",
    }
    try:
        s = requests.Session()
        s.headers.update(HEADERS)

        # Step 1: Prime cookies
        h = s.get(HOME, timeout=30)
        if h.status_code != 200:
            print(f"⚠️  NSE home failed: {h.status_code}")
            return
        time.sleep(2)

        # Step 2: Fetch market status
        r = s.get(API, timeout=30)
        if r.status_code != 200:
            print(f"⚠️  NSE API failed: {r.status_code}")
            return

        data = r.json()
        mcap = data.get("marketcap") or (data.get("marketStatus") or {}).get("marketcap")
        if not mcap:
            print("⚠️  marketcap key not found in NSE response")
            return

        inr = float(mcap["marketCapinLACCRRupeesFormatted"])
        usd = float(mcap["marketCapinTRDollars"]) * 1000  # trillion → billion
        ratio = round((usd / GDP_USD_BILLIONS) * 100, 1)
        today = datetime.now(ist).date().isoformat()

        supabase.table('market_ratios').upsert({
            'snapshot_date': today,
            'nse_total_mcap_usd_billions': usd,
            'india_gdp_usd_billions': GDP_USD_BILLIONS,
            'market_cap_gdp_ratio': ratio
        }, on_conflict='snapshot_date').execute()

        print(f"✅ Market cap: ₹{inr} Lakh Cr = ${usd:.0f}B | MC/GDP: {ratio}%")

    except Exception as e:
        print(f"⚠️  Market cap fetch error: {e}")


# ─── Paper-trade fill + intraday stop monitor ────────────────────────────
# On each price-tick cycle: (1) fill any pending paper trades using the
# latest live_prices tick as D1 open; (2) check open paper trades against
# LTP and close at the stop if breached. Mirrors paper_trader._close_trade
# and paper_fill_pending.py so the EOD 22:00 IST run sees coherent state.


def fill_pending_paper_trades():
    """Walk pending paper trades; fill at LTP if live price available.
    Expires rows older than PENDING_MAX_TRADING_DAYS as fill_expired.
    Idempotent — already-filled rows have status='open' and are skipped.
    """
    try:
        resp = (supabase.table('paper_trades').select('*')
                  .eq('status', 'pending').eq('mode', 'paper').execute())
        pending = resp.data or []
        if not pending:
            return 0

        tickers = [r['ticker'] for r in pending]
        prices_resp = (supabase.table('live_prices').select('ticker, price')
                         .in_('ticker', tickers).execute())
        price_map = {r['ticker']: paper_to_float(r.get('price'))
                     for r in (prices_resp.data or [])}

        today = datetime.now(ist).date()
        filled = 0
        for tr in pending:
            ticker = tr['ticker']
            try:
                scan_date = date.fromisoformat(tr['entry_date'])
                age_td = paper_trading_days_between(scan_date, today)

                live_price = price_map.get(ticker)
                if not live_price or live_price <= 0:
                    if age_td > PENDING_MAX_TRADING_DAYS:
                        _expire_pending(tr, today, 'fill_expired')
                        print(f"  ⏳ PAPER EXPIRED: {ticker} (age={age_td}td, no live price)")
                    continue

                tier = tr['strategy_tier']
                entry_atr = paper_to_float(tr.get('entry_atr'))
                if not entry_atr or entry_atr <= 0:
                    _expire_pending(tr, today, 'fill_rejected_missing_atr')
                    continue

                entry_price = live_price * (1 + PAPER_SLIPPAGE_BPS / 10_000)
                qty, _, _ = paper_size_position(tier, entry_price, entry_atr)
                if qty == 0:
                    _expire_pending(tr, today, 'fill_rejected_position_floor')
                    print(f"  ❌ PAPER FILL REJECT: {ticker} qty=0 @ {entry_price:.2f}")
                    continue

                initial_stop = entry_price - PAPER_STOP_ATR_MULT * entry_atr
                initial_risk = qty * (entry_price - initial_stop)

                supabase.table('paper_trades').update({
                    'entry_date': today.isoformat(),
                    'entry_price': round(entry_price, 4),
                    'initial_quantity': qty,
                    'current_quantity': qty,
                    'entry_value': round(qty * entry_price, 2),
                    'initial_risk': round(initial_risk, 2),
                    'initial_stop': round(initial_stop, 4),
                    'current_stop': round(initial_stop, 4),
                    'status': 'open',
                    'updated_at': datetime.utcnow().isoformat(),
                }).eq('id', tr['id']).execute()
                filled += 1
                print(f"  ✅ PAPER FILL: {ticker} @ {entry_price:.2f} "
                      f"qty={qty} stop={initial_stop:.2f} ({tier})")
            except Exception as e:
                print(f"  ⚠️  Fill failed for {ticker}: {e}")
        return filled
    except Exception as e:
        print(f"⚠️  fill_pending_paper_trades error: {e}")
        return 0


def _expire_pending(tr, today, reason):
    supabase.table('paper_trades').update({
        'status': 'closed',
        'exit_date': today.isoformat(),
        'exit_reason': reason,
        'exit_price': None,
        'total_pnl': 0,
        'total_pnl_pct': 0,
        'current_quantity': 0,
        'updated_at': datetime.utcnow().isoformat(),
    }).eq('id', tr['id']).execute()


def _close_paper_at_ltp(tr, ltp):
    entry_price = float(tr['entry_price'])
    initial_qty = int(tr['initial_quantity'])
    initial_stop = float(tr['initial_stop'])
    current_qty = int(tr['current_quantity'])
    if current_qty <= 0:
        return
    realised_pnl_before = float(tr.get('realised_pnl') or 0)
    realised_qty_before = int(tr.get('realised_qty') or 0)
    trail_armed = bool(tr.get('trail_armed'))
    breakeven_armed = bool(tr.get('breakeven_armed'))
    partials_taken = int(tr.get('partials_taken') or 0)
    partials = tr.get('partials') or []

    exit_chunk_pnl = (ltp - entry_price) * current_qty
    realised_pnl = realised_pnl_before + exit_chunk_pnl
    realised_qty = realised_qty_before + current_qty

    entry_value = entry_price * initial_qty
    total_pnl_pct = (realised_pnl / entry_value * 100) if entry_value else 0
    risk_per_share = entry_price - initial_stop
    r_mult = ((realised_pnl / initial_qty) / risk_per_share
              if (initial_qty and risk_per_share > 0) else None)

    today_ist = datetime.now(ist).date()
    try:
        entry_date = date.fromisoformat(tr['entry_date'])
        holding_days = paper_trading_days_between(entry_date, today_ist)
    except Exception:
        holding_days = 0

    supabase.table('paper_trades').update({
        'exit_date': today_ist.isoformat(),
        'exit_price': round(ltp, 4),
        'exit_reason': 'trail_stop' if trail_armed else 'stop',
        'total_pnl': round(realised_pnl, 2),
        'total_pnl_pct': round(total_pnl_pct, 3),
        'r_multiple': round(r_mult, 3) if r_mult is not None else None,
        'holding_days': holding_days,
        'current_quantity': 0,
        'realised_pnl': round(realised_pnl, 2),
        'realised_qty': realised_qty,
        'partials': partials,
        'partials_taken': partials_taken,
        'breakeven_armed': breakeven_armed,
        'trail_armed': trail_armed,
        'status': 'closed',
        'updated_at': datetime.utcnow().isoformat(),
    }).eq('id', tr['id']).execute()


def check_paper_stops():
    """Walk open paper trades, exit at LTP if LTP <= current_stop."""
    try:
        trades_resp = (supabase.table('paper_trades').select('*')
                         .eq('status', 'open').eq('mode', 'paper').execute())
        open_trades = trades_resp.data or []
        if not open_trades:
            return 0

        tickers = [t['ticker'] for t in open_trades]
        prices_resp = (supabase.table('live_prices').select('ticker, price')
                         .in_('ticker', tickers).execute())
        price_map = {r['ticker']: float(r.get('price') or 0)
                     for r in (prices_resp.data or [])}

        closed = 0
        for tr in open_trades:
            ltp = price_map.get(tr['ticker'], 0)
            current_stop = float(tr.get('current_stop') or 0)
            if ltp <= 0 or current_stop <= 0:
                continue
            if ltp > current_stop:
                continue
            try:
                _close_paper_at_ltp(tr, ltp)
                closed += 1
                print(f"  🛑 PAPER STOP: {tr['ticker']} @ {ltp:.2f} "
                      f"(stop={current_stop:.2f}, tier={tr.get('strategy_tier')})")
            except Exception as e:
                print(f"  ⚠️  Failed to close paper trade {tr['ticker']}: {e}")
        return closed
    except Exception as e:
        print(f"⚠️  check_paper_stops error: {e}")
        return 0


def update_paper_excursions():
    """Update max_unrealized_pct / min_unrealized_pct on open paper trades
    whenever the live LTP prints a new extreme. Writes only when a value
    changes, so per-tick cost is minimal (most ticks are no-ops).
    EOD batch (paper_trader.py) will fold today's bar_high/bar_low into the
    same columns, so this stays consistent with the EOD source of truth."""
    try:
        trades_resp = (supabase.table('paper_trades')
                         .select('id, ticker, entry_price, '
                                 'max_unrealized_pct, min_unrealized_pct')
                         .eq('status', 'open').eq('mode', 'paper').execute())
        open_trades = trades_resp.data or []
        if not open_trades:
            return 0

        tickers = [t['ticker'] for t in open_trades]
        prices_resp = (supabase.table('live_prices').select('ticker, price')
                         .in_('ticker', tickers).execute())
        price_map = {r['ticker']: float(r.get('price') or 0)
                     for r in (prices_resp.data or [])}

        updates = 0
        now_iso = datetime.utcnow().isoformat()
        for tr in open_trades:
            ltp = price_map.get(tr['ticker'], 0)
            entry = float(tr.get('entry_price') or 0)
            if ltp <= 0 or entry <= 0:
                continue
            unr_pct = (ltp - entry) / entry * 100

            stored_max = tr.get('max_unrealized_pct')
            stored_min = tr.get('min_unrealized_pct')
            stored_max = float(stored_max) if stored_max is not None else None
            stored_min = float(stored_min) if stored_min is not None else None

            patch = {}
            if stored_max is None or unr_pct > stored_max:
                patch['max_unrealized_pct'] = round(unr_pct, 3)
            if stored_min is None or unr_pct < stored_min:
                patch['min_unrealized_pct'] = round(unr_pct, 3)
            if not patch:
                continue
            patch['updated_at'] = now_iso
            try:
                supabase.table('paper_trades').update(patch).eq('id', tr['id']).execute()
                updates += 1
            except Exception as e:
                print(f"  ⚠️  Excursion update failed for {tr['ticker']}: {e}")
        return updates
    except Exception as e:
        print(f"⚠️  update_paper_excursions error: {e}")
        return 0


def main():
    print("=" * 60)
    print("ZERODHA LIVE PRICE UPDATER - RAILWAY CLOUD VERSION")
    print("=" * 60)
    print()

    update_count = 0
    last_ticker_check = time.time()
    last_market_session = None
    market_cap_fetched_today = None  # Track which day we fetched market cap

    # Initialize as None — loaded when market opens
    access_token = None
    kite = None
    tickers = None
    token_to_ticker = None

    while True:
        try:
            now = datetime.now(ist)
            current_date = now.date()

            # ── Daily market cap fetch at 3:45–4:00 PM IST ───────────────
            # Runs after market close, once per day, regardless of price loop state
            now_time = now.time()
            market_cap_window_start = now.replace(hour=15, minute=45, second=0, microsecond=0).time()
            market_cap_window_end   = now.replace(hour=16, minute=0,  second=0, microsecond=0).time()

            if (now.weekday() < 5 and
                    market_cap_window_start <= now_time <= market_cap_window_end and
                    market_cap_fetched_today != current_date):
                print(f"\n📊 Fetching NSE market cap for {current_date}...")
                fetch_and_store_market_cap()
                market_cap_fetched_today = current_date

            # ── Market hours check ────────────────────────────────────────
            if not is_market_open():
                print(f"⏸️  Market closed - {now.strftime('%I:%M %p')} (waiting...)")
                time.sleep(60)
                continue

            # ── New session initialisation ────────────────────────────────
            if last_market_session != current_date:
                print(f"\n{'='*60}")
                print(f"🌅 NEW MARKET SESSION: {current_date}")
                print(f"{'='*60}")

                print("🔑 Fetching latest access token from Supabase...")
                access_token = get_access_token_from_supabase()

                if not access_token:
                    print("❌ No access token found in Supabase!")
                    print("Waiting 5 minutes before retry...")
                    time.sleep(300)
                    continue

                print(f"✅ Loaded access token: {access_token[:20]}...")

                kite = KiteConnect(api_key=ZERODHA_API_KEY)
                kite.set_access_token(access_token)
                print("✅ Connected to Zerodha API with fresh token\n")

                print("📊 Getting your portfolio + active paper-trade stocks...")
                tickers = get_all_streaming_tickers()
                print(f"✅ Found {len(tickers)} stocks "
                      f"(incl. active paper trades)\n")

                if not tickers:
                    print("⚠️  No stocks in portfolio! Waiting...")
                    time.sleep(300)
                    continue

                token_to_ticker = get_instrument_mappings(kite, tickers)

                if not token_to_ticker:
                    print("❌ No instruments mapped! Retrying in 5 minutes...")
                    time.sleep(300)
                    continue

                print()
                print("🔴 STARTING LIVE PRICE UPDATES")
                print("=" * 60)
                print("Polling interval: 5 seconds")
                print("Market hours: 9:15 AM - 3:30 PM IST (Mon-Fri)")
                print("Market cap fetch: 3:45 PM IST daily")
                print("Running 24/7 on Railway.app ☁️")
                print()

                last_market_session = current_date
                update_count = 0

            # ── Live price update ─────────────────────────────────────────
            if kite and token_to_ticker:
                updated = fetch_and_update_prices(kite, token_to_ticker)
                fetch_and_update_index_prices(kite)  # NIFTY 50, NIFTY 500, USDINR
                # Fill must run before stops on a fresh fill: pending → open
                # with the actual D1 entry price + ATR-derived stop.
                filled = fill_pending_paper_trades() if updated > 0 else 0
                stopped = check_paper_stops()
                excursions = update_paper_excursions() if updated > 0 else 0
                update_count += 1

                now = datetime.now(ist)
                suffix_parts = []
                if filled:
                    suffix_parts.append(f"filled_paper={filled}")
                if stopped:
                    suffix_parts.append(f"stopped_paper={stopped}")
                if excursions:
                    suffix_parts.append(f"excursions={excursions}")
                suffix = (", " + ", ".join(suffix_parts)) if suffix_parts else ""
                if updated > 0:
                    print(f"📈 {now.strftime('%H:%M:%S')} - Updated {updated}/{len(token_to_ticker)} prices (#{update_count}){suffix}")
                else:
                    print(f"⚠️  {now.strftime('%H:%M:%S')} - No updates (#{update_count}){suffix}")

                # Check for new stocks every 5 minutes (incl. fresh paper trades)
                if time.time() - last_ticker_check > 300:
                    print("\n🔄 Checking for new stocks...")
                    new_tickers = get_all_streaming_tickers()
                    if new_tickers != tickers:
                        added = new_tickers - tickers
                        dropped = tickers - new_tickers
                        if added:
                            print(f"📢 Added {len(added)} ticker(s): "
                                  f"{', '.join(sorted(added)[:5])}"
                                  f"{'...' if len(added) > 5 else ''}")
                        if dropped:
                            print(f"➖ Dropped {len(dropped)} ticker(s): "
                                  f"{', '.join(sorted(dropped)[:5])}"
                                  f"{'...' if len(dropped) > 5 else ''}")
                        tickers = new_tickers
                        token_to_ticker = get_instrument_mappings(kite, tickers)
                        print()
                    else:
                        print("✅ No changes\n")
                    last_ticker_check = time.time()

            time.sleep(5)

        except KeyboardInterrupt:
            print("\n\n👋 Shutting down...")
            break
        except Exception as e:
            print(f"\n❌ Error in main loop: {e}")
            print("Retrying in 30 seconds...")
            time.sleep(30)

if __name__ == '__main__':
    main()
