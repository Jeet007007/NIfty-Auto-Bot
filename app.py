import os
import time
import json
import datetime as dt
from typing import Dict, Any

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ======================================================
# 🔐 ENV VARIABLES (Railway Variables Required)
# ======================================================

UPSTOX_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
TV_WEBHOOK_SECRET = os.getenv("TV_WEBHOOK_SECRET", "").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ======================================================
# ⚙️ SETTINGS
# ======================================================

MAX_RISK_INR = float(os.getenv("MAX_RISK_INR", "500"))
MAX_DAILY_LOSS_INR = float(os.getenv("MAX_DAILY_LOSS_INR", "1300"))
FIXED_SL_PCT = float(os.getenv("FIXED_SL_PCT", "7")) / 100

PAPER_MODE = os.getenv("PAPER_MODE", "true").lower() == "true"

# ======================================================
# 📩 TELEGRAM FUNCTION
# ======================================================

def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }

    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print("Telegram error:", e)

# ======================================================
# ❤️ HEALTH CHECK
# ======================================================

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "time_ist": dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30))).isoformat()
    })

# ======================================================
# 📡 WEBHOOK (TradingView → Railway)
# ======================================================

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data: Dict[str, Any] = request.json

        # 🔒 Secret validation
        if data.get("secret") != TV_WEBHOOK_SECRET:
            return jsonify({"error": "Invalid secret"}), 403

        signal = data.get("signal", "UNKNOWN")

        # ==================================================
        # 📝 PAPER TRADE LOGIC
        # ==================================================

        if PAPER_MODE:
            message = f"""
📢 PAPER TRADE SIGNAL
-----------------------
Signal: {signal}
Max Risk: ₹{MAX_RISK_INR}
Max Daily Loss: ₹{MAX_DAILY_LOSS_INR}
SL %: {FIXED_SL_PCT * 100}%
Mode: PAPER
Time: {dt.datetime.now().strftime('%H:%M:%S')}
"""
            print(message)
            send_telegram(message)

            return jsonify({
                "status": "paper_trade_logged",
                "signal": signal
            })

        # ==================================================
        # 🚨 LIVE MODE (Disabled for now)
        # ==================================================

        return jsonify({"status": "live_mode_not_enabled"})

    except Exception as e:
        print("Webhook error:", e)
        return jsonify({"error": str(e)}), 500


# ======================================================
# 🚀 RUN SERVER
# ======================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
