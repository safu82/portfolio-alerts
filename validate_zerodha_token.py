"""
Check that the access token stored in Supabase (the one Railway reads) is
accepted by Zerodha, by calling kite.profile().

Exit codes: 0 = accepted, 1 = rejected by Zerodha, 2 = could not check.
"""

import os
import sys

from kiteconnect import KiteConnect
from kiteconnect.exceptions import TokenException
from supabase import create_client


def main() -> int:
    api_key = os.getenv("ZERODHA_API_KEY")
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")
    if not (api_key and supabase_url and supabase_key):
        print("❌ Missing ZERODHA_API_KEY / SUPABASE_URL / SUPABASE_KEY")
        return 2

    try:
        row = (create_client(supabase_url, supabase_key)
               .table('zerodha_config').select('value, updated_at')
               .eq('id', 'zerodha_access_token').single().execute()).data
    except Exception as e:
        print(f"❌ Could not read token from Supabase: {e}")
        return 2

    token = row['value']
    print(f"🔎 Supabase token {token[:6]}… (updated {row['updated_at']})")

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(token)
    try:
        profile = kite.profile()
    except TokenException as e:
        print(f"❌ Zerodha rejected the token: {e}")
        return 1
    except Exception as e:
        print(f"⚠️  Could not reach Zerodha: {e}")
        return 2

    print(f"✅ Zerodha accepted the token (user {profile.get('user_id')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
