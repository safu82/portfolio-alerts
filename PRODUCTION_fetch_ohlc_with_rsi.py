#!/usr/bin/env python3
"""
PRODUCTION: Zerodha OHLC Data Fetcher with RSI Calculations + Alkalyme RS
Fetches OHLC data from Zerodha API and calculates RSI indicators for storage in Supabase

Features:
- Fetches daily OHLC data from Zerodha (365 days)
- Fetches NIFTY 50 index data separately (instrument_type = INDICES)
- Calculates Daily RSI(14) and RSI EMA(9)
- Fetches weekly OHLC data (52 weeks)
- Calculates Weekly RSI(14) and RSI EMA(9)
- Calculates EMAs (20, 50, 200)
- Calculates 52-week high (rolling 252-day high)
- Calculates Alkalyme RS score (Portfolio A formula):
    Step 1: RS Ratio = (Stock Close / NIFTY 50 Close) * 1000
    Step 2: Wilder RSI(14) of RS Ratio series (SMA-seeded)
    Step 3: 9-period EMA of the RSI values = alkalyme_rs
- Stores everything in Supabase

Schedule: Daily at 4:30 PM IST (after market close)
Runtime: ~10-15 minutes for 520 stocks (Zerodha rate limit: 60 req/min)
"""

from kiteconnect import KiteConnect
from datetime import datetime, timedelta
from supabase import create_client, Client
import time
import pandas as pd
import numpy as np
import os

# =============================================================================
# CONFIGURATION
# =============================================================================

SUPABASE_URL = os.getenv('SUPABASE_URL', 'https://hcgyncghmcvylnrmcivj.supabase.co')
SUPABASE_KEY = os.getenv('SUPABASE_KEY', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhjZ3luY2dobWN2eWxucm1jaXZqIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTc1MTQwMTEsImV4cCI6MjA3MzA5MDAxMX0.n8vFVCJe1y_3o8fpAY0IgasZ4eKl7DAogEM3OlHB8Ww')
ZERODHA_API_KEY = os.getenv('ZERODHA_API_KEY', 'a0yteflg4n33wu49')

DAYS_HISTORY = 365
RATE_LIMIT_DELAY = 1.0

TICKER_MAPPING = {
    'KPENERGY.NS': 'KPEL',
    'TRIL.NS': 'TARIL',
    'GENUSPOWER.NS': 'GENUSPOWER',
    'DENTAWATER.NS': 'DENTA',
    'NARMP.BO': 'NARMP'
}

NIFTY_500_TICKERS = [
    'DELHIVERY.NS', 'CASTROLIND.NS', 'SARDAEN.NS', 'GODIGIT.NS', 'PNCINFRA.NS',
    'AEGISLOG.NS', 'WELCORP.NS', 'IDEA.NS', '360ONE.NS', 'SAPPHIRE.NS', 'ASTRAL.NS',
    'REDINGTON.NS', 'WESTLIFE.NS', 'HOMEFIRST.NS', 'CRAFTSMAN.NS', 'APARINDS.NS',
    'EMAMILTD.NS', 'TRITURBINE.NS', 'VTL.NS', 'STARHEALTH.NS', 'INDIACEM.NS',
    'ROUTE.NS', 'GESHIP.NS', 'USHAMART.NS', 'APLLTD.NS', 'NAVINFLUOR.NS',
    'HINDALCO.NS', 'TRENT.NS', 'MAHLIFE.NS', 'HAPPSTMNDS.NS', 'KIRLOSBROS.NS',
    'VEDL.NS', 'ABSLAMC.NS', 'BALAMINES.NS', 'BOSCHLTD.NS', 'SIEMENS.NS',
    'ECLERX.NS', 'ASAHIINDIA.NS', 'DBREALTY.NS', 'MANKIND.NS', 'TORNTPHARM.NS',
    'TORNTPOWER.NS', 'ZENSARTECH.NS', 'BALRAMCHIN.NS', 'BHARATFORG.NS', 'TIINDIA.NS',
    'VIPIND.NS', 'AIAENG.NS', 'SCHNEIDER.NS', 'SAIL.NS', 'POWERGRID.NS',
    'JSWINFRA.NS', 'DOMS.NS', 'MOTHERSON.NS', 'JSL.NS', 'HAVELLS.NS', 'DEEPAKNTR.NS',
    'CARBORUNIV.NS', 'GRINDWELL.NS', 'CENTRALBK.NS', 'THERMAX.NS', 'JWL.NS',
    'NAUKRI.NS', 'RHIM.NS', 'KNRCON.NS', 'CELLO.NS', 'MAPMYINDIA.NS',
    'HEROMOTOCO.NS', 'SBILIFE.NS', 'RBLBANK.NS', 'KIRLOSENG.NS', 'JUSTDIAL.NS',
    'POWERINDIA.NS', 'SYNGENE.NS', 'FINPIPE.NS', 'INDIANB.NS', 'SONACOMS.NS',
    'JINDALSTEL.NS', 'FSL.NS', 'HAL.NS', 'GLENMARK.NS', 'HSCL.NS', 'SCHAEFFLER.NS',
    'HDFCBANK.NS', 'CENTURYPLY.NS', 'SUNDRMFAST.NS', 'TATAELXSI.NS',
    'INDUSINDBK.NS', 'HBLENGINE.NS', 'NHPC.NS', 'FLUOROCHEM.NS', 'PHOENIXLTD.NS',
    'METROBRAND.NS', 'UPL.NS', 'LINDEINDIA.NS', 'KPIL.NS', 'KALYANKJIL.NS',
    'ATUL.NS', 'TATASTEEL.NS', 'AUBANK.NS', 'MANAPPURAM.NS', 'NMDC.NS',
    'TATACONSUM.NS', 'M&M.NS', 'MARUTI.NS', 'PFC.NS', 'PRAJIND.NS', 'TECHNOE.NS',
    'J&KBANK.NS', 'CHEMPLASTS.NS', 'LTTS.NS', 'JBCHEPHARM.NS', 'AFFLE.NS',
    'KAJARIACER.NS', 'UCOBANK.NS', 'ELGIEQUIP.NS', 'TATACOMM.NS', 'IIFL.NS',
    'HINDZINC.NS', 'MUTHOOTFIN.NS', 'AWL.NS', 'UTIAMC.NS', 'ELECON.NS',
    'NATIONALUM.NS', 'ICICIBANK.NS', 'SHYAMMETL.NS', 'MANYAVAR.NS', 'NLCINDIA.NS',
    'MFSL.NS', 'CHALET.NS', 'COALINDIA.NS', 'ENDURANCE.NS', 'PETRONET.NS',
    'BLUESTARCO.NS', 'AARTIIND.NS', 'CIEINDIA.NS', 'CLEAN.NS', 'IGL.NS',
    'PERSISTENT.NS', 'APOLLOTYRE.NS', 'KOTAKBANK.NS', 'GUJGASLTD.NS',
    'ULTRACEMCO.NS', 'ABBOTINDIA.NS', 'VARROC.NS', 'GMRAIRPORT.NS', 'NESTLEIND.NS',
    'PEL.NS', 'ADANIPOWER.NS', 'JSWENERGY.NS', 'GRANULES.NS', 'ADANIPORTS.NS',
    'MINDACORP.NS', 'NETWORK18.NS', 'FEDERALBNK.NS', 'HONASA.NS', 'AUROPHARMA.NS',
    'UNIONBANK.NS', 'CERA.NS', 'MGL.NS', 'ACE.NS', 'CUB.NS', 'BIKAJI.NS',
    'JUBLPHARMA.NS', 'SOLARINDS.NS', 'BBTC.NS', 'TATAPOWER.NS',
    'POLYCAB.NS', 'TIMKEN.NS', 'TCS.NS', 'SPARC.NS', 'ZFCVINDIA.NS', 'FINEORG.NS',
    'RENUKA.NS', 'CANBK.NS', 'ADANIGREEN.NS', 'GRINFRA.NS', 'MSUMI.NS',
    'TATACHEM.NS', 'RRKABEL.NS', 'RELIANCE.NS', 'HEG.NS', 'SUPREMEIND.NS',
    'WELSPUNLIV.NS', 'PNB.NS', 'PAGEIND.NS', 'ICICIGI.NS', 'NUVOCO.NS', 'NTPC.NS',
    'CESC.NS', 'RATNAMANI.NS', 'DATAPATTNS.NS', 'GMDCLTD.NS', 'CRISIL.NS',
    'BSOFT.NS', 'TATATECH.NS', 'RITES.NS', 'GRSE.NS', 'APOLLOHOSP.NS', 'TRIDENT.NS',
    'APLAPOLLO.NS', 'JKLAKSHMI.NS', 'SAREGAMA.NS', 'ESCORTS.NS', 'TVSMOTOR.NS',
    'SBFC.NS', 'ALOKINDS.NS', 'HDFCLIFE.NS', 'BHEL.NS', 'JUBLINGREA.NS',
    'CGPOWER.NS', 'CAMPUS.NS', 'TITAGARH.NS', 'VOLTAS.NS', 'BIRLACORPN.NS',
    'JKCEMENT.NS', 'NATCOPHARM.NS', 'DLF.NS', 'CAMS.NS', 'MARICO.NS', 'POLYMED.NS',
    'MAXHEALTH.NS', 'BASF.NS', 'PGHH.NS', 'JBMA.NS', 'JSWSTEEL.NS', 'INOXINDIA.NS',
    'GNFC.NS', 'BAJAJHLDNG.NS', 'DMART.NS', 'ITC.NS', 'INDGN.NS', 'PIIND.NS',
    'OFSS.NS', 'IDFCFIRSTB.NS', 'EXIDEIND.NS', 'IFCI.NS', 'PFIZER.NS', 'KEC.NS',
    'SIGNATURE.NS', 'JYOTICNC.NS', 'ISEC.NS', 'CROMPTON.NS', 'ASTERDM.NS',
    'NIACL.NS', 'GICRE.NS', 'BRITANNIA.NS', 'MRPL.NS', 'HONAUT.NS', 'MAHABANK.NS',
    '3MINDIA.NS', 'DRREDDY.NS', 'CHAMBLFERT.NS', 'INDIAMART.NS', 'SRF.NS',
    'SONATSOFTW.NS', 'PATANJALI.NS', 'ABREL.NS', 'RAJESHEXPO.NS', 'CEATLTD.NS',
    'COCHINSHIP.NS', 'FINCABLES.NS', 'GPPL.NS', 'HFCL.NS', 'ALKYLAMINE.NS',
    'VINATIORGA.NS', 'ANANDRATHI.NS', 'ABB.NS', 'INOXWIND.NS', 'IRB.NS', 'SUZLON.NS',
    'ASTRAZEN.NS', 'SUNPHARMA.NS', 'ITI.NS', 'GSPL.NS', 'TATAMOTORS.NS',
    'CHENNPETRO.NS', 'POONAWALLA.NS', 'AJANTPHARM.NS', 'CAPLIPOINT.NS', 'PVRINOX.NS',
    'JUBLFOOD.NS', 'AMBUJACEM.NS', 'MAZDOCK.NS', 'KSB.NS', 'ARE&M.NS', 'IOB.NS',
    'IOC.NS', 'EQUITASBNK.NS', 'LICI.NS', 'TBOTEK.NS', 'GODREJIND.NS',
    'BHARTIHEXA.NS', 'ADANIENT.NS', 'RAMCOCEM.NS', 'IRFC.NS', 'EMCURE.NS',
    'GODREJPROP.NS', 'YESBANK.NS', 'WIPRO.NS', 'NBCC.NS', 'BLUEDART.NS', 'MRF.NS',
    'GODREJAGRO.NS', 'OLECTRA.NS', 'INTELLECT.NS', 'ASHOKLEY.NS', 'CONCORDBIO.NS',
    'GLAND.NS', 'OIL.NS', 'SJVN.NS', 'COLPAL.NS', 'RKFORGE.NS', 'MASTEK.NS',
    'INDIGO.NS', 'DALBHARAT.NS', 'ACC.NS', 'BALKRISIND.NS', 'NSLNISP.NS', 'SUNTV.NS',
    'JKTYRE.NS', 'HINDUNILVR.NS', 'BAJAJ-AUTO.NS', 'ZOMATO.NS', 'BEL.NS',
    'UNITDSPR.NS', 'TATAINVEST.NS', 'LATENTVIEW.NS', 'SHREECEM.NS',
    'TRIVENI.NS', 'BAJAJFINSV.NS', 'ACI.NS', 'BDL.NS', 'ZYDUSLIFE.NS', 'HCLTECH.NS',
    'NETWEB.NS', 'ERIS.NS', 'ANANTRAJ.NS', 'NEWGEN.NS', 'GILLETTE.NS', 'TTML.NS',
    'IRCON.NS', 'SOBHA.NS', 'GRASIM.NS', 'RAILTEL.NS', 'BATAINDIA.NS',
    'HINDCOPPER.NS', 'GPIL.NS', 'LT.NS', 'ONGC.NS', 'BHARTIARTL.NS', 'JYOTHYLAB.NS',
    'RVNL.NS', 'GAIL.NS', 'CONCOR.NS', 'ENGINERSIN.NS', 'ALKEM.NS', 'KFINTECH.NS',
    'MOTILALOFS.NS', 'BANKBARODA.NS', 'CIPLA.NS', 'IEX.NS', 'LEMONTREE.NS',
    'INDHOTEL.NS', 'MAHSEAMLES.NS', 'NUVAMA.NS', 'SBIN.NS', 'ATGL.NS',
    'KANSAINER.NS', 'UBL.NS', 'JIOFIN.NS', 'APTUS.NS', 'KIMS.NS', 'PPLPHARMA.NS',
    'TANLA.NS', 'INDUSTOWER.NS', 'SWSOLAR.NS', 'PIDILITIND.NS', 'TECHM.NS',
    'PCBL.NS', 'SAMMAANCAP.NS', 'BAJFINANCE.NS', 'ABCAPITAL.NS', 'PAYTM.NS',
    'CUMMINSIND.NS', 'GRAPHITE.NS', 'DABUR.NS', 'RCF.NS', 'PRESTIGE.NS',
    'LAURUSLABS.NS', 'LODHA.NS', 'LUPIN.NS', 'CDSL.NS', 'IDBI.NS', 'RTNINDIA.NS',
    'JINDALSAW.NS', 'BPCL.NS', 'IRCTC.NS', 'BEML.NS', 'ASIANPAINT.NS',
    'METROPOLIS.NS', 'EASEMYTRIP.NS', 'SANOFI.NS', 'NH.NS', 'TEJASNET.NS',
    'BAYERCROP.NS', 'CANFINHOME.NS', 'AXISBANK.NS', 'GSFC.NS', 'COFORGE.NS',
    'CCL.NS', 'SUVENPHAR.NS', 'PTCIL.NS', 'ICICIPRULI.NS', 'WHIRLPOOL.NS',
    'BRIGADE.NS', 'LALPATHLAB.NS', 'LTIM.NS', 'HINDPETRO.NS', 'KPRMILL.NS',
    'SUNDARMFIN.NS', 'CYIENT.NS', 'FACT.NS', 'RECLTD.NS', 'EIHOTEL.NS', 'HDFCAMC.NS',
    'IREDA.NS', 'MMTC.NS', 'SUMICHEM.NS', 'EIDPARRY.NS', 'M&MFIN.NS', 'VBL.NS',
    'SWANENERGY.NS', 'TVSSCS.NS', 'OBEROIRLTY.NS', 'EICHERMOT.NS', 'KARURVYSYA.NS',
    'ZEEL.NS', 'BANDHANBNK.NS', 'BANKINDIA.NS', 'CREDITACC.NS', 'NCC.NS',
    'JPPOWER.NS', 'DIXON.NS', 'VGUARD.NS', 'RADICO.NS', 'NYKAA.NS', 'DEEPAKFERT.NS',
    'INFY.NS', 'AAVAS.NS', 'MEDANTA.NS', 'LICHSGFIN.NS', 'BERGEPAINT.NS',
    'ADANIENSOL.NS', 'KEI.NS', 'BIOCON.NS', 'SYRMA.NS', 'GLAXO.NS', 'BLS.NS',
    'UNOMINDA.NS', 'GODREJCP.NS', 'CHOLAFIN.NS', 'FORTIS.NS', 'AADHARHFC.NS',
    'SBICARD.NS', 'MPHASIS.NS', 'NAM-INDIA.NS', 'LTF.NS', 'IPCALAB.NS', 'SCI.NS',
    'JMFINANCIL.NS', 'DIVISLAB.NS', 'KPITTECH.NS', 'DEVYANI.NS', 'SHRIRAMFIN.NS',
    'UJJIVANSFB.NS', 'TITAN.NS', 'HUDCO.NS', 'ANGELONE.NS', 'PNBHOUSING.NS',
    'GAEL.NS', 'POLICYBZR.NS', 'BSE.NS', 'MCX.NS', 'AVANTIFEED.NS', 'GODFRYPHLP.NS',
    'COROMANDEL.NS', 'AKUMS.NS', 'AMBER.NS', 'LLOYDSME.NS', 'CGCL.NS', 'KAYNES.NS',
    'RAINBOW.NS', 'CHOLAHLDNG.NS', 'VIJAYA.NS', 'FIVESTAR.NS',
    # Portfolio stocks NOT in Nifty 500
    'ASIANENE.NS', 'BELRISE.NS', 'DENTAWATER.NS', 'GENUSPOWER.NS',
    'GREAVESCOT.NS', 'INDRAMEDCO.NS', 'ITDC.NS', 'MANINFRA.NS',
    'NAVA.NS', 'NFL.NS', 'ORIENTELEC.NS', 'PSUBNKBEES.NS',
    'SOTL.NS', 'TAJGVK.NS',
    # Additional portfolio stocks (Feb 2026)
    'AVANTEL.NS', 'MODEFENCE.NS', 'WOCKPHARMA.NS', 'ZENTEC.NS',
    # ETFs in portfolio (Feb 2026)
    'GOLDBEES.NS', 'METALIETF.NS', 'SILVERBEES.NS',
    # BSE stocks in portfolio (Feb 2026)
    'NARMP.BO'
]

# =============================================================================
# CALCULATION FUNCTIONS
# =============================================================================

def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Standard RSI using rolling mean (for stock indicators)"""
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period, min_periods=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period, min_periods=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def calculate_wilder_rsi(series: pd.Series, period: int = 14) -> list:
    """
    Wilder RSI with proper SMA seeding — used for Alkalyme RS formula.
    Matches Portfolio A audit document Section 5.2 exactly.
    Returns list of floats (None for warmup period).
    """
    values = series.tolist()
    n = len(values)
    rsi_values = [None] * n

    if n < period + 1:
        return rsi_values

    # Seed: SMA of first 14 gains and losses
    gains = []
    losses = []
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        rsi_values[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi_values[period] = 100 - (100 / (1 + rs))

    # Wilder smoothing for subsequent values
    for i in range(period + 1, n):
        delta = values[i] - values[i - 1]
        gain = max(delta, 0)
        loss = max(-delta, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            rsi_values[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi_values[i] = 100 - (100 / (1 + rs))

    return rsi_values


def calculate_alkalyme_rs(stock_closes: pd.Series,
                          nifty_closes: pd.Series) -> list:
    """
    Alkalyme RS score per Portfolio A audit document Section 5.
    Step 1: RS Ratio = (Stock Close / NIFTY 50 Close) * 1000
    Step 2: Wilder RSI(14) of RS Ratio series (SMA-seeded)
    Step 3: 9-period EMA of RSI values (SMA-seeded) = alkalyme_rs

    Returns list of floats aligned to stock_closes index (None for warmup).
    """
    # Align on common dates
    combined = pd.DataFrame({
        'stock': stock_closes,
        'nifty': nifty_closes
    }).dropna()

    if len(combined) < 25:  # Need at least 14 + 9 + 2 periods
        return [None] * len(stock_closes)

    # Step 1: RS Ratio
    rs_ratio = (combined['stock'] / combined['nifty']) * 1000

    # Step 2: Wilder RSI(14) of RS Ratio
    rsi_values = calculate_wilder_rsi(rs_ratio, period=14)

    # Step 3: 9-period EMA of RSI values (SMA-seeded per audit doc Section 5.3)
    non_none = [(i, v) for i, v in enumerate(rsi_values) if v is not None]
    if len(non_none) < 9:
        ema_values = [None] * len(rsi_values)
    else:
        ema_values = [None] * len(rsi_values)
        # Find first 9 non-None values to seed EMA
        seed_indices = [i for i, v in non_none[:9]]
        seed_vals = [v for i, v in non_none[:9]]
        seed_ema = sum(seed_vals) / 9
        ema_values[seed_indices[-1]] = seed_ema

        k = 2 / (9 + 1)  # = 0.2
        prev_ema = seed_ema
        for i in range(seed_indices[-1] + 1, len(rsi_values)):
            if rsi_values[i] is not None:
                prev_ema = rsi_values[i] * k + prev_ema * (1 - k)
                ema_values[i] = prev_ema

    # Map back to original stock_closes index using combined's dates
    result = [None] * len(stock_closes)
    combined_dates = list(combined.index)
    stock_dates = list(stock_closes.index)

    for i, ema_val in enumerate(ema_values):
        if i < len(combined_dates) and ema_val is not None:
            combined_date = combined_dates[i]
            if combined_date in stock_dates:
                stock_idx = stock_dates.index(combined_date)
                result[stock_idx] = ema_val

    return result


def calculate_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def resample_to_weekly(df: pd.DataFrame) -> pd.DataFrame:
    weekly = df.resample('W-FRI').agg({
        'Open': 'first', 'High': 'max', 'Low': 'min',
        'Close': 'last', 'Volume': 'sum'
    })
    return weekly.dropna()

# =============================================================================
# ZERODHA FUNCTIONS
# =============================================================================

def get_access_token_from_supabase() -> str:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        result = supabase.table('zerodha_config').select('value')\
            .eq('id', 'zerodha_access_token').single().execute()
        if result.data:
            return result.data['value']
    except Exception as e:
        print(f"❌ Error fetching token: {e}")
    return None


def get_nifty50_instrument_token(kite: KiteConnect) -> int:
    """
    Return NIFTY 50 index instrument token.
    Token 256265 is the permanent Zerodha token for NSE:NIFTY 50.
    Confirmed working via kite.quote('NSE:NIFTY 50').
    """
    return 256265


def fetch_nifty50_closes(kite: KiteConnect) -> pd.Series:
    """
    Fetch NIFTY 50 index OHLC and return a date-indexed Close series.
    Also stores NIFTY 50 in daily_stock_snapshots as NIFTY50.NS.
    """
    token = get_nifty50_instrument_token(kite)
    if not token:
        return pd.Series(dtype=float)

    try:
        to_date = datetime.now()
        from_date = to_date - timedelta(days=DAYS_HISTORY)

        data = kite.historical_data(
            instrument_token=token,
            from_date=from_date,
            to_date=to_date,
            interval='day'
        )

        if not data:
            return pd.Series(dtype=float)

        df = pd.DataFrame(data)
        df['date'] = pd.to_datetime(df['date'])
        df.set_index('date', inplace=True)
        df.index = df.index.normalize()  # strip time component

        print(f"  ✅ NIFTY 50: {len(df)} days fetched")
        return df['close']

    except Exception as e:
        print(f"  ⚠️  Error fetching NIFTY 50: {e}")
        return pd.Series(dtype=float)


def save_nifty50_to_supabase(kite: KiteConnect, supabase: Client):
    """Store NIFTY 50 OHLC in daily_stock_snapshots as NIFTY50.NS"""
    token = get_nifty50_instrument_token(kite)
    if not token:
        return

    try:
        to_date = datetime.now()
        from_date = to_date - timedelta(days=DAYS_HISTORY)
        data = kite.historical_data(
            instrument_token=token,
            from_date=from_date,
            to_date=to_date,
            interval='day'
        )

        records = []
        for row in data:
            records.append({
                'ticker': 'NIFTY50.NS',
                'snapshot_date': row['date'].strftime('%Y-%m-%d')
                    if hasattr(row['date'], 'strftime') else str(row['date'])[:10],
                'open': float(row['open']),
                'high': float(row['high']),
                'low': float(row['low']),
                'close': float(row['close']),
                'volume': int(row.get('volume', 0)),
            })

        if records:
            supabase.table('daily_stock_snapshots').upsert(
                records, on_conflict='ticker,snapshot_date'
            ).execute()
            print(f"  ✅ NIFTY50.NS: {len(records)} records saved")

    except Exception as e:
        print(f"  ⚠️  Error saving NIFTY 50: {e}")


def get_instrument_token(kite: KiteConnect, yahoo_ticker: str) -> tuple:
    if yahoo_ticker.endswith('.NS'):
        exchange = 'NSE'
        symbol = yahoo_ticker.replace('.NS', '')
    elif yahoo_ticker.endswith('.BO'):
        exchange = 'BSE'
        symbol = yahoo_ticker.replace('.BO', '')
    else:
        return None, None

    for zerodha_sym, yahoo_sym in TICKER_MAPPING.items():
        if yahoo_ticker == yahoo_sym:
            symbol = zerodha_sym.split('.')[0]
            if yahoo_sym in ['GENUSPOWER.NS', 'DENTAWATER.NS']:
                exchange = 'BSE'
            break

    if not hasattr(get_instrument_token, 'instruments'):
        print("📥 Downloading instruments from Zerodha...")
        nse_instruments = kite.instruments("NSE")
        bse_instruments = kite.instruments("BSE")
        get_instrument_token.instruments = nse_instruments + bse_instruments
        print(f"✅ Loaded {len(get_instrument_token.instruments)} instruments")

    for inst in get_instrument_token.instruments:
        if (inst['tradingsymbol'] == symbol and
                inst['exchange'] == exchange and
                inst['instrument_type'] == 'EQ'):
            return inst['instrument_token'], exchange

    return None, None


def init_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def fetch_and_calculate_ohlc(kite: KiteConnect, yahoo_ticker: str,
                              nifty_closes: pd.Series) -> list:
    """
    Fetch OHLC + calculate all indicators including Alkalyme RS.
    nifty_closes: date-indexed Series of NIFTY 50 closes for RS computation.
    """
    try:
        instrument_token, exchange = get_instrument_token(kite, yahoo_ticker)
        if not instrument_token:
            print(f"  ⚠️  Could not find instrument for {yahoo_ticker}")
            return []

        to_date = datetime.now()
        from_date = to_date - timedelta(days=DAYS_HISTORY)

        historical_data = kite.historical_data(
            instrument_token=instrument_token,
            from_date=from_date,
            to_date=to_date,
            interval='day'
        )

        if not historical_data or len(historical_data) < 20:
            return []

        df = pd.DataFrame(historical_data)
        df['date'] = pd.to_datetime(df['date'])
        df.set_index('date', inplace=True)
        df.index = df.index.normalize()
        df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low',
                            'close': 'Close', 'volume': 'Volume'}, inplace=True)

        # Daily RSI
        df['rsi_14'] = calculate_rsi(df['Close'], 14)
        df['rsi_ema_9'] = calculate_ema(df['rsi_14'], 9)

        # EMAs
        df['ema_20'] = calculate_ema(df['Close'], 20)
        df['ema_50'] = calculate_ema(df['Close'], 50)
        df['ema_200'] = calculate_ema(df['Close'], 200)

        # 52-week high
        df['high_52w'] = df['High'].rolling(window=252, min_periods=1).max()

        # Volume ratio — today's volume vs 20-day average
        df['vol_20_avg'] = df['Volume'].shift(1).rolling(window=20, min_periods=5).mean()
        df['vol_ratio'] = (df['Volume'] / df['vol_20_avg']).round(2)

        # Alkalyme RS — computed against NIFTY 50
        if len(nifty_closes) >= 25:
            rs_list = calculate_alkalyme_rs(df['Close'], nifty_closes)
            df['alkalyme_rs'] = rs_list
        else:
            df['alkalyme_rs'] = None

        # Weekly RSI
        weekly_df = resample_to_weekly(df)
        if len(weekly_df) >= 14:
            weekly_df['weekly_rsi_14'] = calculate_rsi(weekly_df['Close'], 14)
            weekly_df['weekly_rsi_ema_9'] = calculate_ema(weekly_df['weekly_rsi_14'], 9)
            df['weekly_rsi_14'] = np.nan
            df['weekly_rsi_ema_9'] = np.nan
            for date, row in weekly_df.iterrows():
                week_start = date - pd.Timedelta(days=6)
                week_mask = (df.index >= week_start) & (df.index <= date)
                df.loc[week_mask, 'weekly_rsi_14'] = row['weekly_rsi_14']
                df.loc[week_mask, 'weekly_rsi_ema_9'] = row['weekly_rsi_ema_9']

        # Build records
        records = []
        for date, row in df.iterrows():
            records.append({
                'ticker': yahoo_ticker,
                'snapshot_date': date.strftime('%Y-%m-%d'),
                'open': float(row['Open']),
                'high': float(row['High']),
                'low': float(row['Low']),
                'close': float(row['Close']),
                'volume': int(row['Volume']),
                'rsi_14': float(row['rsi_14']) if pd.notna(row['rsi_14']) else None,
                'rsi_ema_9': float(row['rsi_ema_9']) if pd.notna(row['rsi_ema_9']) else None,
                'ema_20': float(row['ema_20']) if pd.notna(row['ema_20']) else None,
                'ema_50': float(row['ema_50']) if pd.notna(row['ema_50']) else None,
                'ema_200': float(row['ema_200']) if pd.notna(row['ema_200']) else None,
                'weekly_rsi_14': float(row['weekly_rsi_14']) if pd.notna(row['weekly_rsi_14']) else None,
                'weekly_rsi_ema_9': float(row['weekly_rsi_ema_9']) if pd.notna(row['weekly_rsi_ema_9']) else None,
                'high_52w': float(row['high_52w']) if pd.notna(row['high_52w']) else None,
                'alkalyme_rs': float(row['alkalyme_rs']) if pd.notna(row['alkalyme_rs']) else None,
                'vol_ratio': float(row['vol_ratio']) if pd.notna(row['vol_ratio']) else None,
            })

        return records

    except Exception as e:
        print(f"  ⚠️  Error fetching {yahoo_ticker}: {e}")
        return []


def cleanup_old_data(supabase: Client, days_to_keep: int = 60):
    try:
        cutoff_date = (datetime.now() - timedelta(days=days_to_keep)).strftime('%Y-%m-%d')
        response = supabase.table('daily_stock_snapshots')\
            .delete().lt('snapshot_date', cutoff_date).execute()
        deleted = len(response.data) if response.data else 0
        print(f"🗑️  Cleaned up {deleted:,} old records (before {cutoff_date})")
    except Exception as e:
        print(f"⚠️  Cleanup error: {e}")


def main():
    print("=" * 80)
    print("📊 ZERODHA OHLC + RSI + ALKALYME RS DATA FETCHER")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

    print("\n🔑 Fetching Zerodha access token...")
    access_token = get_access_token_from_supabase()
    if not access_token:
        print("❌ No access token found! Exiting.")
        return
    print(f"✅ Token loaded: {access_token[:20]}...")

    kite = KiteConnect(api_key=ZERODHA_API_KEY)
    kite.set_access_token(access_token)
    print("✅ Connected to Zerodha API\n")

    supabase = init_supabase()

    # ── Fetch NIFTY 50 first — needed for Alkalyme RS ──────────────────────
    print("📈 Fetching NIFTY 50 index data...")
    save_nifty50_to_supabase(kite, supabase)
    nifty_closes = fetch_nifty50_closes(kite)
    time.sleep(RATE_LIMIT_DELAY)

    if nifty_closes.empty:
        print("⚠️  NIFTY 50 data unavailable — Alkalyme RS will be skipped")
    else:
        print(f"✅ NIFTY 50 loaded: {len(nifty_closes)} days\n")

    # ── Main fetch loop ─────────────────────────────────────────────────────
    total_records = 0
    successful = 0
    failed = 0

    for i, ticker in enumerate(NIFTY_500_TICKERS, 1):
        if i % 10 == 0 or i == 1:
            print(f"\n[{i}/{len(NIFTY_500_TICKERS)}] Progress: {(i/len(NIFTY_500_TICKERS)*100):.1f}%")

        records = fetch_and_calculate_ohlc(kite, ticker, nifty_closes)

        if records:
            try:
                supabase.table('daily_stock_snapshots').upsert(
                    records, on_conflict='ticker,snapshot_date'
                ).execute()

                total_records += len(records)
                successful += 1

                latest = records[-1]
                rsi_info = f"RSI: {latest['rsi_ema_9']:.1f}" if latest['rsi_ema_9'] else "RSI: N/A"
                rs_info = f"RS: {latest['alkalyme_rs']:.1f}" if latest['alkalyme_rs'] else "RS: N/A"
                print(f"  ✅ {ticker:20} - {len(records)} records | {rsi_info} | {rs_info}")

            except Exception as e:
                failed += 1
                print(f"  ❌ {ticker:20} - DB error: {e}")
        else:
            failed += 1

        time.sleep(RATE_LIMIT_DELAY)

    print("\n" + "=" * 80)
    cleanup_old_data(supabase, days_to_keep=60)

    print("\n" + "=" * 80)
    print("📈 SUMMARY")
    print("=" * 80)
    print(f"✅ Successful: {successful}/{len(NIFTY_500_TICKERS)}")
    print(f"❌ Failed:     {failed}/{len(NIFTY_500_TICKERS)}")
    print(f"💾 Total records: {total_records:,}")
    print(f"📊 Indicators: RSI(14), RSI EMA(9), EMAs(20,50,200), Weekly RSI, 52W High, Alkalyme RS")
    print(f"Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)


if __name__ == "__main__":
    main()
