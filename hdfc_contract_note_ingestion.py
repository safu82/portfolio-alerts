"""
HDFC Securities Contract Note Auto-Ingestion
Runs nightly at 2 AM IST (8:30 PM UTC) via GitHub Actions.
Reads today's contract note PDF from Gmail, extracts trades via Claude API,
inserts into Supabase transactions table, sends Telegram summary.
"""

import os
import json
import base64
import tempfile
import datetime
import anthropic
import pikepdf
import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from supabase import create_client


# ── Config from environment variables (GitHub Secrets) ──────────────────────
GMAIL_CLIENT_ID       = os.environ['GMAIL_CLIENT_ID']
GMAIL_CLIENT_SECRET   = os.environ['GMAIL_CLIENT_SECRET']
GMAIL_REFRESH_TOKEN   = os.environ['GMAIL_REFRESH_TOKEN']
HDFC_PDF_PASSWORD     = os.environ['HDFC_PDF_PASSWORD']          # SAR2001
ANTHROPIC_API_KEY     = os.environ['ANTHROPIC_API_KEY']
SUPABASE_URL          = os.environ['SUPABASE_URL']
SUPABASE_KEY          = os.environ['SUPABASE_SERVICE_KEY']
TELEGRAM_BOT_TOKEN    = os.environ['TELEGRAM_BOT_TOKEN']
TELEGRAM_CHAT_ID      = os.environ['TELEGRAM_CHAT_ID']
PORTFOLIO             = 'INDIAN'


def get_gmail_service():
    """Authenticate with Gmail API using stored refresh token."""
    creds = Credentials(
        token=None,
        refresh_token=GMAIL_REFRESH_TOKEN,
        client_id=GMAIL_CLIENT_ID,
        client_secret=GMAIL_CLIENT_SECRET,
        token_uri='https://oauth2.googleapis.com/token',
        scopes=['https://www.googleapis.com/auth/gmail.readonly']
    )
    creds.refresh(Request())
    return build('gmail', 'v1', credentials=creds)


def find_contract_note_email(service):
    """
    Search Gmail for today's HDFC contract note email.
    Script runs at 2 AM IST — trade date is yesterday IST.
    """
    # Yesterday in IST (trade date)
    ist_offset = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    yesterday = (datetime.datetime.now(ist_offset) - datetime.timedelta(days=1)).date()
    trade_date_str = yesterday.strftime('%d-%b-%Y').upper()  # e.g. 23-MAR-2026

    print(f"🔍 Searching for contract note for trade date: {trade_date_str}")

    # Gmail search: from HDFC, subject contains contract note and trade date
    query = f'from:edocs@hdfcsec.com subject:"Contract Note" subject:"{trade_date_str}"'
    result = service.users().messages().list(userId='me', q=query, maxResults=5).execute()
    messages = result.get('messages', [])

    if not messages:
        print(f"⚠️  No contract note email found for {trade_date_str}")
        return None, None

    # Get the most recent matching email
    msg = service.users().messages().get(userId='me', id=messages[0]['id']).execute()
    return msg, yesterday


def download_pdf_attachment(service, msg):
    """Download the PDF attachment from the email."""
    payload = msg.get('payload', {})
    parts = payload.get('parts', [])

    for part in parts:
        filename = part.get('filename', '')
        if filename.lower().endswith('.pdf'):
            attachment_id = part['body'].get('attachmentId')
            if attachment_id:
                attachment = service.users().messages().attachments().get(
                    userId='me', messageId=msg['id'], id=attachment_id
                ).execute()
                pdf_data = base64.urlsafe_b64decode(attachment['data'])
                print(f"✅ Downloaded PDF: {filename} ({len(pdf_data):,} bytes)")
                return pdf_data, filename

    print("⚠️  No PDF attachment found in email")
    return None, None


def decrypt_pdf(pdf_data, password):
    """Decrypt password-protected PDF using pikepdf."""
    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp_in:
        tmp_in.write(pdf_data)
        tmp_in_path = tmp_in.name

    tmp_out_path = tmp_in_path.replace('.pdf', '_decrypted.pdf')

    with pikepdf.open(tmp_in_path, password=password) as pdf:
        pdf.save(tmp_out_path)

    with open(tmp_out_path, 'rb') as f:
        decrypted_data = f.read()

    os.unlink(tmp_in_path)
    os.unlink(tmp_out_path)
    print(f"✅ PDF decrypted successfully")
    return decrypted_data


def normalize_company_name(name):
    """Strip common suffixes and whitespace for fuzzy name matching."""
    import re
    name = name.upper().strip()
    for suffix in [' LIMITED', ' LTD.', ' LTD', ' INDIA']:
        if name.endswith(suffix):
            name = name[:-len(suffix)].strip()
    return re.sub(r' +', ' ', name).strip()


def load_zerodha_name_map():
    """
    Download Zerodha NSE instruments CSV (no auth, confirmed accessible from GitHub Actions).
    Columns: instrument_token, exchange_token, tradingsymbol, name, instrument_type, ...
    Builds normalized_company_name → ticker map for EQ instruments only.
    """
    import csv, io
    url = "https://api.kite.trade/instruments/NSE"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    name_map = {}
    for row in reader:
        if row.get('instrument_type') == 'EQ':
            raw_name = row.get('name', '').strip()
            symbol   = row.get('tradingsymbol', '').strip()
            if raw_name and symbol:
                name_map[normalize_company_name(raw_name)] = symbol + '.NS'
    print(f"✅ Loaded {len(name_map):,} NSE EQ instruments from Zerodha")
    return name_map


def resolve_ticker_from_supabase(name, supabase):
    """
    Look up ticker from our own transactions table by stock name.
    Reliable for any stock we have previously traded (all SELLs, repeat BUYs).
    """
    result = supabase.table('transactions').select('ticker').eq(
        'portfolio', PORTFOLIO
    ).eq('stock', name).limit(1).execute()
    if result.data:
        ticker = result.data[0]['ticker']
        print(f"  🔍 '{name}' → {ticker} (from Supabase)")
        return ticker
    return None


def resolve_ticker_from_isin(isin, name, name_map, supabase):
    """
    Resolve NSE ticker with two-stage fallback:
    1. Supabase transactions table (covers all SELLs + previously bought stocks)
    2. Zerodha NSE instruments name map (new BUY positions only)
    """
    # Stage 1: check our own database first
    ticker = resolve_ticker_from_supabase(name, supabase)
    if ticker:
        return ticker

    # Stage 2: Zerodha name map for genuinely new stocks
    norm = normalize_company_name(name)
    ticker = name_map.get(norm)
    if ticker:
        print(f"  🔍 '{name}' → {ticker} (from Zerodha)")
        return ticker

    raise ValueError(
        f"Could not resolve ticker for '{name}' (normalized: '{norm}', ISIN: {isin})"
        " — add manually or check Zerodha instruments file."
    )


def parse_trades_with_claude(pdf_data, trade_date, supabase):
    """
    Pass the decrypted PDF directly to Claude as a document — avoids pdfplumber
    mangling the dense multi-column table. Claude reads the actual layout.
    Extracts ISIN + quantities + WAP prices only; ticker resolved separately via ISIN.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    pdf_b64 = base64.standard_b64encode(pdf_data).decode('utf-8')

    prompt = f"""This is an HDFC Securities contract note PDF for trade date {trade_date}.

Look at the Equity (Cash) Segment table. It has these columns (left to right):
ISIN | Security Name | Total BUY qty | BUY WAP | BUY Brokerage/share | Total BUY Value | Total SELL qty | SELL WAP | SELL Brokerage/share | Total SELL Value | Net Qty | Net Obligation

Extract every row where Total BUY qty > 0 OR Total SELL qty > 0.

CRITICAL:
1. Quantity = "Total BUY qty traded across exchanges" for buys, "Total SELL qty traded across exchanges" for sells. NOT Net Qty.
2. Price = WAP (Weighted Average Price), the column immediately after the quantity. NOT the brokerage/share (tiny number like 0.09 or 0.44).
3. Name = full company name only, strip any trailing exchange suffix (EEQ, STEEQ, EQ, etc.).
4. Leave ticker as empty string "" — do not guess it.
5. If a row has both BUY qty > 0 and SELL qty > 0, emit two separate trade objects.

Return ONLY valid JSON, no markdown fences, no explanation:
{{
  "trade_date": "{trade_date}",
  "trades": [
    {{
      "isin": "INE263A01024",
      "name": "BHARAT ELECTRONICS LTD",
      "ticker": "",
      "type": "SELL",
      "quantity": 3000,
      "price": 442.15,
      "exchange": "NSE"
    }}
  ]
}}"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": pdf_b64
                    }
                },
                {"type": "text", "text": prompt}
            ]
        }]
    )

    raw = response.content[0].text.strip()
    if raw.startswith('```'):
        raw = raw.split('```')[1]
        if raw.startswith('json'):
            raw = raw[4:]
    raw = raw.strip()

    result = json.loads(raw)
    trades = result.get('trades', [])
    print(f"✅ Claude extracted {len(trades)} trades")

    # Stage 1: Supabase lookup (SELLs + repeat BUYs). Stage 2: Zerodha name map (new BUYs).
    name_map = load_zerodha_name_map()
    for trade in trades:
        trade['ticker'] = resolve_ticker_from_isin(trade['isin'], trade['name'], name_map, supabase)

    return result


def check_duplicate(supabase, trade_date, ticker, txn_type, quantity):
    """Check if this transaction already exists in Supabase."""
    result = supabase.table('transactions').select('id').eq(
        'portfolio', PORTFOLIO
    ).eq('date', str(trade_date)).eq('ticker', ticker).eq(
        'type', txn_type
    ).eq('quantity', quantity).execute()
    return len(result.data) > 0


def insert_transactions(supabase, trades, trade_date):
    """Insert new transactions into Supabase, skipping duplicates."""
    inserted = []
    skipped = []

    for trade in trades:
        ticker  = trade['ticker']
        txn_type = trade['type']
        quantity = trade['quantity']
        price    = trade['price']
        name     = trade['name']

        if check_duplicate(supabase, trade_date, ticker, txn_type, quantity):
            print(f"  ⏭️  Skipping duplicate: {txn_type} {quantity} {ticker}")
            skipped.append(trade)
            continue

        row = {
            'portfolio': PORTFOLIO,
            'date':      str(trade_date),
            'type':      txn_type,
            'stock':     name,
            'ticker':    ticker,
            'quantity':  quantity,
            'price':     price,
            'sector':    None,
            'strategy':  'Auto-imported'
        }
        supabase.table('transactions').insert(row).execute()
        print(f"  ✅ Inserted: {txn_type} {quantity} {ticker} @ ₹{price}")
        inserted.append(trade)

    return inserted, skipped


def send_telegram(bot_token, chat_id, inserted, skipped, trade_date, error=None):
    """Send Telegram summary notification."""
    if error:
        msg = f"❌ *Contract Note Ingestion Failed*\nDate: {trade_date}\nError: {error}"
    elif not inserted and not skipped:
        msg = f"📋 *Contract Note*: No trades found for {trade_date}"
    else:
        lines = [f"📋 *Contract Note Ingested* — {trade_date}"]
        lines.append(f"✅ Inserted: {len(inserted)} | ⏭️ Skipped: {len(skipped)}")
        lines.append("")
        for t in inserted:
            sign = "🟢 BUY" if t['type'] == 'BUY' else "🔴 SELL"
            lines.append(f"{sign} {t['quantity']:,} {t['ticker']} @ ₹{t['price']:,.2f}")
        msg = '\n'.join(lines)

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    requests.post(url, json={
        'chat_id': chat_id,
        'text': msg,
        'parse_mode': 'Markdown'
    })
    print(f"✅ Telegram notification sent")


def main():
    print("=" * 60)
    print("HDFC Contract Note Ingestion — Starting")
    print("=" * 60)

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    # Always compute trade_date upfront (yesterday in IST)
    ist_offset = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    trade_date = (datetime.datetime.now(ist_offset) - datetime.timedelta(days=1)).date()
    print(f"📅 Trade date: {trade_date}")

    try:
        # Step 1: Gmail
        service = get_gmail_service()
        msg, trade_date = find_contract_note_email(service)

        if not msg:
            send_telegram(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, [], [], trade_date)
            return

        # Step 2: Download PDF
        pdf_data, filename = download_pdf_attachment(service, msg)
        if not pdf_data:
            raise Exception("Could not download PDF attachment")

        # Step 3: Decrypt PDF
        decrypted = decrypt_pdf(pdf_data, HDFC_PDF_PASSWORD)

        # Step 4: Parse with Claude (PDF passed directly — no pdfplumber)
        result = parse_trades_with_claude(decrypted, trade_date.strftime('%Y-%m-%d'), supabase)
        trades = result.get('trades', [])

        if not trades:
            print("⚠️  No trades extracted from PDF")
            send_telegram(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, [], [], trade_date)
            return

        # Step 6: Insert into Supabase
        inserted, skipped = insert_transactions(supabase, trades, trade_date)

        # Step 7: Telegram notification
        send_telegram(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, inserted, skipped, trade_date)

        print(f"\n✅ Done — {len(inserted)} inserted, {len(skipped)} skipped")

    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        send_telegram(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, [], [], trade_date, error=str(e))
        raise


if __name__ == '__main__':
    main()
