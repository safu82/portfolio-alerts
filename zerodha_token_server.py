"""
Zerodha Token Refresh Web Server
=================================
Exposes a /refresh-token endpoint that can be called by cron-job.org
to trigger the Selenium token refresh at exactly 7:00 AM IST daily.
"""

from flask import Flask, jsonify
import subprocess
import os
from datetime import datetime
import pytz

app = Flask(__name__)

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'ok',
        'timestamp': datetime.now(pytz.timezone('Asia/Kolkata')).isoformat()
    })

@app.route('/refresh-token', methods=['POST', 'GET'])
def refresh_token():
    """
    Trigger Zerodha token refresh
    This endpoint should be called by cron-job.org at 7:00 AM IST daily
    """
    try:
        ist = pytz.timezone('Asia/Kolkata')
        now = datetime.now(ist)
        
        print(f"\n{'='*60}")
        print(f"TOKEN REFRESH TRIGGERED at {now.strftime('%Y-%m-%d %H:%M:%S IST')}")
        print(f"{'='*60}\n")
        
        # Run the token automation script
        result = subprocess.run(
            ['python', 'zerodha_auto_token_with_supabase.py'],
            capture_output=True,
            text=True,
            timeout=120  # 2 minute timeout
        )
        
        if result.returncode == 0:
            print("✅ Token refresh successful!")
            return jsonify({
                'success': True,
                'message': 'Token refreshed successfully',
                'timestamp': now.isoformat(),
                'output': result.stdout[-500:]  # Last 500 chars of output
            }), 200
        else:
            print(f"❌ Token refresh failed: {result.stderr}")
            return jsonify({
                'success': False,
                'message': 'Token refresh failed',
                'error': result.stderr[-500:],
                'timestamp': now.isoformat()
            }), 500
            
    except subprocess.TimeoutExpired:
        return jsonify({
            'success': False,
            'message': 'Token refresh timed out after 2 minutes',
            'timestamp': now.isoformat()
        }), 500
    except Exception as e:
        print(f"❌ Error: {e}")
        return jsonify({
            'success': False,
            'message': str(e),
            'timestamp': now.isoformat()
        }), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
