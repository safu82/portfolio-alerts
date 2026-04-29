#!/usr/bin/env python3
"""
PRODUCTION: Zerodha OHLC Data Fetcher with RSI Calculations + Alkalyme RS
Fetches OHLC data from Zerodha API and calculates RSI indicators for storage in Supabase

Added in this version:
- EMA(9) of price — for distance from 9 EMA calculation (entry timing signal)
- ATR(14) — Average True Range, Wilder smoothing (for normalising EMA9 distance)

Schedule: Daily at 4:30 PM IST (after market close)
"""

from kiteconnect import KiteConnect
from datetime import datetime, timedelta
from supabase import create_client, Client
import time
import pandas as pd
import numpy as np
import os

SUPABASE_URL    = os.getenv('SUPABASE_URL', 'https://hcgyncghmcvylnrmcivj.supabase.co')
SUPABASE_KEY    = os.getenv('SUPABASE_KEY', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhjZ3luY2dobWN2eWxucm1jaXZqIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTc1MTQwMTEsImV4cCI6MjA3MzA5MDAxMX0.n8vFVCJe1y_3o8fpAY0IgasZ4eKl7DAogEM3OlHB8Ww')
ZERODHA_API_KEY = os.getenv('ZERODHA_API_KEY', 'a0yteflg4n33wu49')

DAYS_HISTORY      = 365
RATE_LIMIT_DELAY  = 1.0

TICKER_MAPPING = {
    'TRIL.NS':              'TARIL',
    'GENUSPOWER.NS':        'GENUSPOWER',
    'DENTAWATER.NS':        'DENTA',
    'NARMP.BO':             'NARMP',
    # Portfolio extras — non-Nifty500 holdings
    'KPEL.NS':              'KPEL',
    'ASHAPURMIN.NS':        'ASHAPURMIN',
    'ELECTCAST.NS':         'ELECTCAST',
    'PGEL.NS':              'PGEL',
}

# Portfolio stocks outside Nifty 500 — fetched after the main scan
PORTFOLIO_EXTRA_TICKERS = [
    'KPEL.NS',
    'ASHAPURMIN.NS',
    'ELECTCAST.NS',
    'PGEL.NS',
]

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
    'ASIANENE.NS', 'BELRISE.NS', 'DENTAWATER.NS', 'GENUSPOWER.NS',
    'GREAVESCOT.NS', 'INDRAMEDCO.NS', 'ITDC.NS', 'MANINFRA.NS',
    'NAVA.NS', 'NFL.NS', 'ORIENTELEC.NS', 'PSUBNKBEES.NS',
    'SOTL.NS', 'TAJGVK.NS',
    'AVANTEL.NS', 'MODEFENCE.NS', 'WOCKPHARMA.NS', 'ZENTEC.NS',
    'GOLDBEES.NS', 'METALIETF.NS', 'SILVERBEES.NS',
    'NARMP.BO',
    'ABFRL.NS', 'ALLCARGO.NS', 'ASHOKA.NS', 'BHARATRAS.NS', 'CENTENKA.NS',
    'CENTRUM.NS', 'CSBBANK.NS', 'DELTACORP.NS', 'DHANI.NS', 'DHANUKA.NS',
    'EDELWEISS.NS', 'EQUITAS.NS', 'ESSELPACK.NS', 'FIEMIND.NS', 'GEPIL.NS',
    'GMMPFAUDLR.NS', 'GREENPANEL.NS', 'GUJALKALI.NS', 'GULFOILLUB.NS',
    'HATHWAY.NS', 'HATSUN.NS', 'HEIDELBERG.NS', 'HEMIPROP.NS', 'IDFC.NS',
    'IFBIND.NS', 'IGARASHI.NS', 'INDOCO.NS', 'INDOSTAR.NS', 'INFIBEAM.NS',
    'JAMNAAUTO.NS', 'JKPAPER.NS', 'KIOCL.NS', 'KPEL.NS', 'KPIGREEN.NS',
    'KRBL.NS', 'KSCL.NS', 'LAXMIMACH.NS', 'LXCHEM.NS', 'MAHINDCIE.NS',
    'MAHLOG.NS', 'MCDOWELL-N.NS', 'MHRIL.NS', 'MIDHANI.NS', 'MINDAIND.NS',
    'MOIL.NS', 'NEULANDLAB.NS', 'NIITLTD.NS', 'NOCIL.NS', 'OMAXE.NS',
    'PACEDIGITK.NS', 'PHILIPCARB.NS', 'POLYPLEX.NS', 'PRSMJOHNSN.NS',
    'QUESS.NS', 'RAIN.NS', 'RALLIS.NS', 'RAYMOND.NS', 'RELAXO.NS',
    'RELIGARE.NS', 'REPCOHOME.NS', 'RPOWER.NS', 'SBB.NS', 'SEQUENT.NS',
    'SFL.NS', 'SHARDACROP.NS', 'SHILPAMED.NS', 'SHOPERSTOP.NS', 'SIS.NS',
    'SKFINDIA.NS', 'SOMANYCERA.NS', 'SPICEJET.NS', 'STARCEMENT.NS',
    'STLTECH.NS', 'SUDARSCHEM.NS', 'SUPRAJIT.NS', 'SYMPHONY.NS',
    'TATAMOTORS.NS', 'TEAMLEASE.NS', 'THOMASCOOK.NS', 'THYROCARE.NS',
    'TTKPRESTIG.NS', 'TV18BRDCST.NS', 'TVSHLTD.NS', 'TVSSRICHAK.NS',
    'UFLEX.NS', 'VAIBHAVGBL.NS', 'VLSFINANCE.NS', 'VRLLOG.NS', 'VSTIND.NS',
    'WABAG.NS', 'WELSPUNIND.NS',
]

# =============================================================================
# CALCULATION FUNCTIONS
# =============================================================================

def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = (delta.where(delta > 0, 0)).rolling(window=period, min_periods=period).mean()
    loss  = (-delta.where(delta < 0, 0)).rolling(window=period, min_periods=period).mean()
    rs    = gain / loss
    return 100 - (100 / (1 + rs))


def calculate_wilder_rsi(series: pd.Series, period: int = 14) -> list:
    values    = series.tolist()
    n         = len(values)
    rsi_values = [None] * n
    if n < period + 1:
        return rsi_values
    gains  = []
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
    for i in range(period + 1, n):
        delta    = values[i] - values[i - 1]
        gain     = max(delta, 0)
        loss     = max(-delta, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            rsi_values[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi_values[i] = 100 - (100 / (1 + rs))
    return rsi_values


def calculate_alkalyme_rs(stock_closes: pd.Series, nifty_closes: pd.Series) -> list:
    combined = pd.DataFrame({'stock': stock_closes, 'nifty': nifty_closes}).dropna()
    if len(combined) < 25:
        return [None] * len(stock_closes)
    rs_ratio   = (combined['stock'] / combined['nifty']) * 1000
    rsi_values = calculate_wilder_rsi(rs_ratio, period=14)
    non_none   = [(i, v) for i, v in enumerate(rsi_values) if v is not None]
    if len(non_none) < 9:
        ema_values = [None] * len(rsi_values)
    else:
        ema_values   = [None] * len(rsi_values)
        seed_indices = [i for i, v in non_none[:9]]
        seed_vals    = [v for i, v in non_none[:9]]
        seed_ema     = sum(seed_vals) / 9
        ema_values[seed_indices[-1]] = seed_ema
        k        = 2 / (9 + 1)
        prev_ema = seed_ema
        for i in range(seed_indices[-1] + 1, len(rsi_values)):
            if rsi_values[i] is not None:
                prev_ema   = rsi_values[i] * k + prev_ema * (1 - k)
                ema_values[i] = prev_ema
    result        = [None] * len(stock_closes)
    combined_dates = list(combined.index)
    stock_dates    = list(stock_closes.index)
    for i, ema_val in enumerate(ema_values):
        if i < len(combined_dates) and ema_val is not None:
            combined_date = combined_dates[i]
            if combined_date in stock_dates:
                stock_idx          = stock_dates.index(combined_date)
                result[stock_idx]  = ema_val
    return result


def calculate_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR using Wilder smoothing (same as TradingView default)"""
    high       = df['High']
    low        = df['Low']
    prev_close = df['Close'].shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


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
        result   = supabase.table('zerodha_config').select('value')\
            .eq('id', 'zerodha_access_token').single().execute()
        if result.data:
            return result.data['value']
    except Exception as e:
        print(f"❌ Error fetching token: {e}")
    return None


def fetch_nifty50_closes(kite: KiteConnect) -> pd.Series:
    try:
        to_date   = datetime.now()
        from_date = to_date - timedelta(days=DAYS_HISTORY)
        data      = kite.historical_data(
            instrument_token=256265,
            from_date=from_date, to_date=to_date, interval='day'
        )
        if not data:
            return pd.Series(dtype=float)
        df = pd.DataFrame(data)
        df['date'] = pd.to_datetime(df['date'])
        df.set_index('date', inplace=True)
        df.index = df.index.normalize()
        print(f"  ✅ NIFTY 50: {len(df)} days fetched")
        return df['close']
    except Exception as e:
        print(f"  ⚠️  Error fetching NIFTY 50: {e}")
        return pd.Series(dtype=float)


def save_nifty50_to_supabase(kite: KiteConnect, supabase: Client):
    try:
        to_date   = datetime.now()
        from_date = to_date - timedelta(days=DAYS_HISTORY)
        data      = kite.historical_data(
            instrument_token=256265,
            from_date=from_date, to_date=to_date, interval='day'
        )
        records = [{
            'ticker': 'NIFTY50.NS',
            'snapshot_date': row['date'].strftime('%Y-%m-%d')
                if hasattr(row['date'], 'strftime') else str(row['date'])[:10],
            'open': float(row['open']), 'high': float(row['high']),
            'low':  float(row['low']),  'close': float(row['close']),
            'volume': int(row.get('volume', 0)),
        } for row in data]
        if records:
            supabase.table('daily_stock_snapshots').upsert(records, on_conflict='ticker,snapshot_date').execute()
            supabase.table('historical_snapshots').upsert(records, on_conflict='ticker,snapshot_date').execute()
            print(f"  ✅ NIFTY50.NS: {len(records)} records saved")
    except Exception as e:
        print(f"  ⚠️  Error saving NIFTY 50: {e}")


def get_instrument_token(kite: KiteConnect, yahoo_ticker: str) -> tuple:
    if yahoo_ticker.endswith('.NS'):
        exchange, symbol = 'NSE', yahoo_ticker.replace('.NS', '')
    elif yahoo_ticker.endswith('.BO'):
        exchange, symbol = 'BSE', yahoo_ticker.replace('.BO', '')
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
        get_instrument_token.instruments = kite.instruments("NSE") + kite.instruments("BSE")
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
    try:
        instrument_token, exchange = get_instrument_token(kite, yahoo_ticker)
        if not instrument_token:
            print(f"  ⚠️  Could not find instrument for {yahoo_ticker}")
            return []

        to_date   = datetime.now()
        from_date = to_date - timedelta(days=DAYS_HISTORY)
        historical_data = kite.historical_data(
            instrument_token=instrument_token,
            from_date=from_date, to_date=to_date, interval='day'
        )
        if not historical_data or len(historical_data) < 20:
            return []

        df = pd.DataFrame(historical_data)
        df['date'] = pd.to_datetime(df['date'])
        df.set_index('date', inplace=True)
        df.index = df.index.normalize()
        df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low',
                            'close': 'Close', 'volume': 'Volume'}, inplace=True)

        # ── Indicators ────────────────────────────────────────────────────────
        df['rsi_14']    = calculate_rsi(df['Close'], 14)
        df['rsi_ema_9'] = calculate_ema(df['rsi_14'], 9)

        # Price EMAs — 9 (NEW), 20, 50, 200
        df['ema_9']   = calculate_ema(df['Close'], 9)
        df['ema_20']  = calculate_ema(df['Close'], 20)
        df['ema_50']  = calculate_ema(df['Close'], 50)
        df['ema_200'] = calculate_ema(df['Close'], 200)

        # ATR(14) — Wilder smoothing (NEW)
        df['atr_14'] = calculate_atr(df, period=14)

        # 52-week high
        df['high_52w'] = df['High'].rolling(window=252, min_periods=1).max()

        # Volume ratio
        df['vol_20_avg'] = df['Volume'].shift(1).rolling(window=20, min_periods=5).mean()
        df['vol_ratio']  = (df['Volume'] / df['vol_20_avg']).round(2)

        # Alkalyme RS
        if len(nifty_closes) >= 25:
            df['alkalyme_rs'] = calculate_alkalyme_rs(df['Close'], nifty_closes)
        else:
            df['alkalyme_rs'] = None

        # Weekly RSI
        weekly_df = resample_to_weekly(df)
        if len(weekly_df) >= 14:
            weekly_df['weekly_rsi_14']    = calculate_rsi(weekly_df['Close'], 14)
            weekly_df['weekly_rsi_ema_9'] = calculate_ema(weekly_df['weekly_rsi_14'], 9)
            df['weekly_rsi_14']    = np.nan
            df['weekly_rsi_ema_9'] = np.nan
            for date, row in weekly_df.iterrows():
                week_mask = (df.index >= date - pd.Timedelta(days=6)) & (df.index <= date)
                df.loc[week_mask, 'weekly_rsi_14']    = row['weekly_rsi_14']
                df.loc[week_mask, 'weekly_rsi_ema_9'] = row['weekly_rsi_ema_9']

        # ── Build records ─────────────────────────────────────────────────────
        def f(v): return float(v) if pd.notna(v) else None

        records = []
        for date, row in df.iterrows():
            records.append({
                'ticker':          yahoo_ticker,
                'snapshot_date':   date.strftime('%Y-%m-%d'),
                'open':            f(row['Open']),
                'high':            f(row['High']),
                'low':             f(row['Low']),
                'close':           f(row['Close']),
                'volume':          int(row['Volume']),
                'rsi_14':          f(row['rsi_14']),
                'rsi_ema_9':       f(row['rsi_ema_9']),
                'ema_9':           f(row['ema_9']),       # NEW
                'ema_20':          f(row['ema_20']),
                'ema_50':          f(row['ema_50']),
                'ema_200':         f(row['ema_200']),
                'atr_14':          f(row['atr_14']),      # NEW
                'weekly_rsi_14':   f(row['weekly_rsi_14']),
                'weekly_rsi_ema_9': f(row['weekly_rsi_ema_9']),
                'high_52w':        f(row['high_52w']),
                'alkalyme_rs':     f(row['alkalyme_rs']),
                'vol_ratio':       f(row['vol_ratio']),
            })
        return records

    except Exception as e:
        print(f"  ⚠️  Error fetching {yahoo_ticker}: {e}")
        return []


def cleanup_old_data(supabase: Client, days_to_keep: int = 60):
    try:
        cutoff = (datetime.now() - timedelta(days=days_to_keep)).strftime('%Y-%m-%d')
        resp   = supabase.table('daily_stock_snapshots').delete().lt('snapshot_date', cutoff).execute()
        print(f"🗑️  Cleaned up {len(resp.data) if resp.data else 0:,} old records (before {cutoff})")
    except Exception as e:
        print(f"⚠️  Cleanup error: {e}")


def main():
    print("=" * 80)
    print("📊 ZERODHA OHLC + RSI + ALKALYME RS DATA FETCHER")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

    access_token = get_access_token_from_supabase()
    if not access_token:
        print("❌ No access token. Exiting.")
        return

    kite = KiteConnect(api_key=ZERODHA_API_KEY)
    kite.set_access_token(access_token)
    supabase = init_supabase()

    print("\n📈 Fetching NIFTY 50...")
    save_nifty50_to_supabase(kite, supabase)
    nifty_closes = fetch_nifty50_closes(kite)
    time.sleep(RATE_LIMIT_DELAY)

    total_records, successful, failed = 0, 0, 0

    for i, ticker in enumerate(NIFTY_500_TICKERS, 1):
        if i % 10 == 0 or i == 1:
            print(f"\n[{i}/{len(NIFTY_500_TICKERS)}] {(i/len(NIFTY_500_TICKERS)*100):.1f}%")

        records = fetch_and_calculate_ohlc(kite, ticker, nifty_closes)
        if records:
            try:
                supabase.table('daily_stock_snapshots').upsert(records, on_conflict='ticker,snapshot_date').execute()
                supabase.table('historical_snapshots').upsert(records, on_conflict='ticker,snapshot_date').execute()
                total_records += len(records)
                successful    += 1
                latest = records[-1]
                print(f"  ✅ {ticker:20} | RSI: {latest['rsi_ema_9'] or 'N/A':.1f} | "
                      f"RS: {latest['alkalyme_rs'] or 'N/A':.1f} | "
                      f"ATR: {latest['atr_14'] or 'N/A':.1f} | "
                      f"EMA9: {latest['ema_9'] or 'N/A':.1f}" if latest['atr_14'] else
                      f"  ✅ {ticker:20} | RSI: N/A")
            except Exception as e:
                failed += 1
                print(f"  ❌ {ticker}: {e}")
        else:
            failed += 1
        time.sleep(RATE_LIMIT_DELAY)

    # ── Portfolio extras (non-Nifty500 holdings) ──────────────────────────────
    print(f"\n📋 Fetching {len(PORTFOLIO_EXTRA_TICKERS)} portfolio extra stocks...")
    for ticker in PORTFOLIO_EXTRA_TICKERS:
        records = fetch_and_calculate_ohlc(kite, ticker, nifty_closes)
        if records:
            try:
                supabase.table('daily_stock_snapshots').upsert(records, on_conflict='ticker,snapshot_date').execute()
                supabase.table('historical_snapshots').upsert(records, on_conflict='ticker,snapshot_date').execute()
                total_records += len(records)
                successful    += 1
                latest = records[-1]
                print(f"  ✅ {ticker:25} | close: {latest['close']} | RSI EMA9: {latest['rsi_ema_9']}")
            except Exception as e:
                failed += 1
                print(f"  ❌ {ticker}: {e}")
        else:
            failed += 1
            print(f"  ⚠️  {ticker}: no data returned")
        time.sleep(RATE_LIMIT_DELAY)

    cleanup_old_data(supabase, days_to_keep=60)
    print(f"\n✅ {successful} ok | ❌ {failed} failed | 💾 {total_records:,} records")
    print(f"Indicators: RSI(14), RSI EMA(9), EMA(9,20,50,200), ATR(14), Weekly RSI, 52W High, Alkalyme RS")
    print(f"Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
