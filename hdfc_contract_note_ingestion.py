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
import pdfplumber
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


def extract_text_from_pdf(pdf_data):
    """Extract text from first 3 pages of PDF (equity cash segment)."""
    import re

    def clean_doubled_chars(text):
        """Fix HDFC PDF artifact where page 2 sometimes has doubled characters e.g. VVOODDAAFFOONNEE."""
        if re.search(r'(.)\1{3,}', text):  # 4+ repeated chars = doubled text artifact
            return re.sub(r'(.)\1', r'\1', text)
        return text

    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
        tmp.write(pdf_data)
        tmp_path = tmp.name

    extracted_pages = []
    with pdfplumber.open(tmp_path) as pdf:
        for i, page in enumerate(pdf.pages[:3]):
            text = page.extract_text()
            if text:
                text = clean_doubled_chars(text)
                extracted_pages.append(f"--- PAGE {i+1} ---\n{text}")

    os.unlink(tmp_path)
    full_text = '\n\n'.join(extracted_pages)
    print(f"✅ Extracted {len(full_text):,} characters from PDF")
    return full_text


def parse_trades_with_claude(pdf_text, trade_date):
    """Use Claude API to extract structured trade data from PDF text."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""You are parsing an HDFC Securities contract note PDF for trade date {trade_date}.

Extract all equity cash segment trades from this text. The table has columns:
- ISIN
- Security Name / Symbol (truncated, multi-line, with NSE/BSE symbol appended)
- BUY Quantity (0 = no buy on this day)
- BUY WAP (Weighted Average Price)
- SELL Quantity (0 = no sell on this day)  
- SELL WAP

Rules:
1. Only include rows where BUY Quantity > 0 OR SELL Quantity > 0
2. For the ticker: use the NSE/BSE symbol embedded at end of the security name
   - If exchange is NSE, append .NS (e.g. AVANTI.NS)
   - If exchange is BSE, append .BO
   - The symbol code at end of name (e.g. SOFEQ, COCHINS, DFEREQ) is the NSE/BSE symbol
   - Clean up the symbol: remove EQ suffix, remove Q suffix for NSE cash
   - Common mappings: INDIGOEQ → INDIGO.NS, IDECELEQ → IDEA.NS
3. Use WAP (Weighted Average Price) as the price — not the per-share brokerage
4. Trade date is: {trade_date}

Return ONLY valid JSON, no other text:
{{
  "trade_date": "{trade_date}",
  "trades": [
    {{
      "isin": "INE005B01027",
      "name": "AVANTEL LIMITED",
      "ticker": "AVANTI.NS",
      "type": "BUY" or "SELL",
      "quantity": 5500,
      "price": 120.67,
      "exchange": "NSE" or "BSE"
    }}
  ]
}}

PDF TEXT:
{pdf_text}"""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text.strip()
    # Strip markdown fences if present
    if raw.startswith('```'):
        raw = raw.split('```')[1]
        if raw.startswith('json'):
            raw = raw[4:]
    raw = raw.strip()

    result = json.loads(raw)
    print(f"✅ Claude extracted {len(result.get('trades', []))} trades")
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
    trade_date = None

    try:
        # Step 1: Gmail
        service = get_gmail_service()
        msg, trade_date = find_contract_note_email(service)

        if not msg:
            send_telegram(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, [], [], trade_date or 'unknown')
            return

        # Step 2: Download PDF
        pdf_data, filename = download_pdf_attachment(service, msg)
        if not pdf_data:
            raise Exception("Could not download PDF attachment")

        # Step 3: Decrypt PDF
        decrypted = decrypt_pdf(pdf_data, HDFC_PDF_PASSWORD)

        # Step 4: Extract text
        pdf_text = extract_text_from_pdf(decrypted)

        # Step 5: Parse with Claude
        result = parse_trades_with_claude(pdf_text, trade_date.strftime('%Y-%m-%d'))
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
