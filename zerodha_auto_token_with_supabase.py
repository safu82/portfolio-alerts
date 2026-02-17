"""
Enhanced Zerodha Token Automation with Supabase Integration
Stores the access token in Supabase for use by Edge Functions
"""

import os
import time
import re
from datetime import datetime, timedelta

import pyotp
from dotenv import load_dotenv
from kiteconnect import KiteConnect
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options as ChromeOptions
from webdriver_manager.chrome import ChromeDriverManager

# Optional: Supabase integration
try:
    from supabase import create_client, Client
    SUPABASE_AVAILABLE = True
except ImportError:
    SUPABASE_AVAILABLE = False
    print("⚠️  Supabase client not installed. Token will only be saved to file.")
    print("   Install with: pip install supabase")


def get_browser(headless: bool = False):
    """Initialize Chrome browser with webdriver."""
    options = ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    
    service = ChromeService(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    print("✅ Chrome browser initialized")
    return driver


def fetch_request_token(api_key: str, userid: str, password: str, secret_key: str, headless: bool = False) -> str:
    """Automate Zerodha login to get request token."""
    browser = get_browser(headless=headless)
    
    try:
        print(f"🌐 Navigating to Kite Connect login page...")
        login_url = f"https://kite.zerodha.com/connect/login?v=3&api_key={api_key}"
        browser.get(login_url)
        time.sleep(3)
        
        print(f"📝 Entering credentials for user: {userid}")
        username_input = browser.find_element(By.ID, "userid")
        username_input.send_keys(userid)
        
        password_input = browser.find_element(By.ID, "password")
        password_input.send_keys(password)
        
        login_button = browser.find_element(
            By.XPATH,
            "/html/body/div[1]/div/div[2]/div[1]/div/div/div[2]/form/div[4]/button"
        )
        login_button.click()
        
        print("⏳ Waiting for 2FA page...")
        time.sleep(3)
        
        print("🔐 Generating TOTP code...")
        totp = pyotp.TOTP(secret_key)
        otp_code = totp.now()
        print(f"   Generated code: {otp_code}")
        
        totp_input = browser.find_element(By.ID, "userid")
        totp_input.send_keys(otp_code)
        
        otp_submit_button = browser.find_element(
            By.XPATH,
            "/html/body/div[1]/div/div[2]/div[1]/div[2]/div/div[2]/form/div[2]/button"
        )
        otp_submit_button.click()
        
        print("⏳ Waiting for callback redirect...")
        time.sleep(5)  # Increased wait time
        
        callback_url = browser.current_url
        print(f"📍 Callback URL: {callback_url}")
        
        pattern = r"request_token=([^&]+)"
        match = re.search(pattern, callback_url)
        
        if match is None:
            raise ValueError(f"❌ Could not extract request token from URL: {callback_url}")
        
        request_token = match.group(1)
        print(f"✅ Request token obtained: {request_token[:10]}...")
        return request_token
    
    finally:
        browser.quit()


def store_token_in_supabase(access_token: str, supabase_url: str, supabase_key: str):
    """Store access token in Supabase."""
    if not SUPABASE_AVAILABLE:
        return
    
    try:
        supabase: Client = create_client(supabase_url, supabase_key)
        
        # Calculate expiry (6 AM IST next day)
        now = datetime.now()
        tomorrow_6am = (now + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)
        
        # Upsert token
        data = {
            'id': 'zerodha_access_token',
            'value': access_token,
            'expires_at': tomorrow_6am.isoformat(),
            'updated_at': now.isoformat()
        }
        
        result = supabase.table('zerodha_config').upsert(data).execute()
        print(f"✅ Token stored in Supabase (expires: {tomorrow_6am.strftime('%Y-%m-%d %I:%M %p')})")
        
    except Exception as e:
        print(f"⚠️  Failed to store in Supabase: {e}")


def main():
    """Main function."""
    load_dotenv()
    
    # Zerodha credentials
    api_key = os.getenv("ZERODHA_API_KEY")
    api_secret = os.getenv("ZERODHA_API_SECRET")
    secret_key = os.getenv("ZERODHA_TOTP_SECRET")
    userid = os.getenv("ZERODHA_USER_ID")
    password = os.getenv("ZERODHA_PASSWORD")
    headless = os.getenv("HEADLESS", "false").lower() in ("true", "1", "yes")
    
    # Supabase credentials (optional)
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")
    
    # Validate required variables
    required_vars = {
        "ZERODHA_API_KEY": api_key,
        "ZERODHA_API_SECRET": api_secret,
        "ZERODHA_TOTP_SECRET": secret_key,
        "ZERODHA_USER_ID": userid,
        "ZERODHA_PASSWORD": password,
    }
    
    missing_vars = [name for name, value in required_vars.items() if not value]
    if missing_vars:
        raise EnvironmentError(f"❌ Missing: {', '.join(missing_vars)}")
    
    print("=" * 60)
    print("ZERODHA ACCESS TOKEN AUTOMATION")
    print("=" * 60)
    print()
    
    # Fetch request token
    request_token = fetch_request_token(api_key, userid, password, secret_key, headless)
    
    # Generate session and access token
    print("🔑 Generating access token...")
    kite = KiteConnect(api_key=api_key)
    data = kite.generate_session(request_token, api_secret=api_secret)
    access_token = data["access_token"]
    
    # Save to file
    access_token_file = os.getenv("ZERODHA_ACCESS_TOKEN_FILE", "zerodha_access_token.txt")
    with open(access_token_file, "w") as f:
        f.write(access_token)
    print(f"✅ Token saved to: {access_token_file}")
    
    # Store in Supabase if credentials provided
    if supabase_url and supabase_key:
        store_token_in_supabase(access_token, supabase_url, supabase_key)
    else:
        print("ℹ️  Supabase credentials not provided (skipping cloud storage)")
    
    print()
    print("=" * 60)
    print("✅ AUTOMATION COMPLETE!")
    print("=" * 60)
    print(f"Access token: {access_token[:20]}...")
    print(f"Valid until: Tomorrow 6:00 AM IST")


if __name__ == "__main__":
    main()
