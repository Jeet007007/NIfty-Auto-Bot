import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# Environment variables
UPSTOX_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN")
TV_WEBHOOK_SECRET = os.getenv("TV_WEBHOOK_SECRET")

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    # Security check
    secret = request.args.get("secret")
    if secret != TV_WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    signal = data.get("signal")

    if signal not in ["BUY_CE", "BUY_PE"]:
        return jsonify({"error": "Invalid signal"}), 400

    # Example order payload (simple market order)
    order_payload = {
        "quantity": 1,
        "product": "I",
        "validity": "DAY",
        "price": 0,
        "tag": "niftybot",
        "instrument_token": "NSE_FO|REPLACE_WITH_REAL_TOKEN",
        "order_type": "MARKET",
        "transaction_type": "BUY",
        "disclosed_quantity": 0,
        "trigger_price": 0,
        "is_amo": False
    }

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}",
    }

    try:
        response = requests.post(
            "https://api-hft.upstox.com/v2/order/place",
            json=order_payload,
            headers=headers,
        )
        return jsonify(response.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
