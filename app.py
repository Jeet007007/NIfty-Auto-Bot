import os
import time
import json
import datetime as dt
from typing import Any, Dict, List, Optional, Literal, Tuple

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ----------------------------
# Settings (from Railway Variables)
# ----------------------------
UPSTOX_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
TV_WEBHOOK_SECRET = os.getenv("TV_WEBHOOK_SECRET", "").strip()

# Fixed 1-lot quantity (set this in Railway Variables if different)
UPSTOX_NIFTY_QTY = int(os.getenv("UPSTOX_NIFTY_QTY", "65"))

# Risk rules (your numbers)
MAX_RISK_INR = float(os.getenv("MAX_RISK_INR", "500"))          # per trade risk cap
MAX_DAILY_LOSS_INR = float(os.getenv("MAX_DAILY_LOSS_INR", "1300"))  # stop for the day

# Stop loss percent (7%)
FIXED_SL_PCT = float(os.getenv("FIXED_SL_PCT", "7")) / 100.0

# Trade time window (IST)
ALLOW_START_IST = os.getenv("ALLOW_START_IST", "09:30")
ALLOW_END_IST = os.getenv("ALLOW_END_IST", "14:45")

# Cooldown (seconds)
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "900"))

# Store simple state so we don't overtrade
STATE_FILE = os.getenv("STATE_FILE", "state.json")

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# Upstox endpoints
OPTION_CHAIN_URL = "https://api.upstox.com/v2/option/chain"
PLACE_ORDER_URL = "https://api-hft.upstox.com/v2/order/place"
ORDER_DETAILS_URL = "https://api.upstox.com/v2/order/details"
POSITIONS_URL = "https://api.upstox.com/v2/portfolio/short-term-positions"

UNDERLYING_KEY_NIFTY = "NSE_INDEX|Nifty 50"

# ----------------------------
# State
# ----------------------------
DEFAULT_STATE = {
    "day": None,
    "last_trade_epoch": 0.0,
    "last_candle_id": None,
}

def now_ist() -> dt.datetime:
    return dt.datetime.now(tz=IST)

def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return DEFAULT_STATE.copy()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        s = DEFAULT_STATE.copy()
        s.update(d)
        return s
    except Exception:
        return DEFAULT_STATE.copy()

STATE = load_state()

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(STATE, f, indent=2)
    except Exception:
        pass

def reset_daily_state_if_needed():
    today = now_ist().date().isoformat()
    if STATE.get("day") != today:
        STATE["day"] = today
        STATE["last_trade_epoch"] = 0.0
        STATE["last_candle_id"] = None
        save_state()

def parse_hhmm(hhmm: str) -> dt.time:
    hh, mm = hhmm.split(":")
    return dt.time(int(hh), int(mm), tzinfo=IST)

def within_time_window() -> bool:
    t = now_ist().timetz()
    return parse_hhmm(ALLOW_START_IST) <= t <= parse_hhmm(ALLOW_END_IST)

def candle_id_15m(ts_str: str) -> str:
    # TradingView often sends ISO strings; if missing, use current time
    try:
        t = dt.datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone(IST)
    except Exception:
        t = now_ist()
    bucket = (t.minute // 15) * 15
    t = t.replace(minute=bucket, second=0, microsecond=0)
    return t.isoformat()

def round_to_step(x: float, step: float = 0.05) -> float:
    return round(x / step) * step

def round_to_50(x: float) -> int:
    return int(round(x / 50.0) * 50)

def get_next_thursday_ist() -> dt.date:
    d = now_ist().date()
    # Mon=0 ... Thu=3
    days_ahead = (3 - d.weekday()) % 7
    return d + dt.timedelta(days=days_ahead)

# ----------------------------
# Upstox API helpers
# ----------------------------
def upstox_headers() -> Dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}",
    }

def api_get(url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    r = requests.get(url, headers=upstox_headers(), params=params, timeout=12)
    r.raise_for_status()
    return r.json()

def api_post(url: str, payload: Dict[str, Any], params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    r = requests.post(url, headers=upstox_headers(), json=payload, params=params, timeout=12)
    r.raise_for_status()
    return r.json()

def fetch_option_chain(expiry_date: str) -> List[Dict[str, Any]]:
    payload = api_get(OPTION_CHAIN_URL, params={"instrument_key": UNDERLYING_KEY_NIFTY, "expiry_date": expiry_date})
    if payload.get("status") != "success":
        raise RuntimeError(f"Option chain failed: {payload}")
    return payload.get("data") or []

def place_order(payload: Dict[str, Any]) -> str:
    resp = api_post(PLACE_ORDER_URL, payload=payload)
    if resp.get("status") != "success":
        raise RuntimeError(f"Order place failed: {resp}")
    order_id = ((resp.get("data") or {}).get("order_id")) or ""
    if not order_id:
        raise RuntimeError(f"No order_id: {resp}")
    return order_id

def get_order_details(order_id: str) -> Dict[str, Any]:
    return api_get(ORDER_DETAILS_URL, params={"order_id": order_id})

def wait_for_avg_price(order_id: str, timeout_sec: int = 12) -> Optional[float]:
    end = time.time() + timeout_sec
    while time.time() < end:
        try:
            d = get_order_details(order_id)
            if d.get("status") == "success":
                data = d.get("data") or {}
                for k in ("average_price", "avg_price", "traded_price"):
                    v = data.get(k)
                    if v is not None:
                        pv = float(v)
                        if pv > 0:
                            return pv
        except Exception:
            pass
        time.sleep(0.7)
    return None

def get_positions() -> List[Dict[str, Any]]:
    payload = api_get(POSITIONS_URL)
    if payload.get("status") != "success":
        raise RuntimeError(f"Positions API failed: {payload}")
    return payload.get("data") or []

def current_day_pnl() -> float:
    """
    Conservative: sums MTM / PnL fields if available.
    If API schema differs, returns 0 (fail-open to not block incorrectly).
    """
    try:
        positions = get_positions()
    except Exception:
        return 0.0

    total = 0.0
    for p in positions:
        # common keys across brokers: pnl, mtm, unrealized, realized
        for key in ("pnl", "mtm", "unrealised", "unrealized", "realised", "realized"):
            if key in p and p[key] is not None:
                try:
                    total += float(p[key])
                    break
                except Exception:
                    pass
    return float(total)

def has_open_nifty_option_position() -> bool:
    """
    Blocks new entries if any NSE_FO position has non-zero net qty.
    """
    try:
        positions = get_positions()
    except Exception:
        # fail-safe: if cannot read positions, don't trade
        return True

    for p in positions:
        ik = str(p.get("instrument_key") or p.get("instrument_token") or "")
        if "NSE_FO|" not in ik:
            continue
        net = p.get("net_quantity")
        if net is not None:
            try:
                if float(net) != 0:
                    return True
            except Exception:
                pass
        # fallback
        qty = p.get("quantity")
        if qty is not None:
            try:
                if float(qty) != 0:
                    return True
            except Exception:
                pass
    return False

# ----------------------------
# Option selection (1 ITM)
# ----------------------------
def choose_1itm(chain: List[Dict[str, Any]], side: Literal["CE", "PE"]) -> Dict[str, Any]:
    if not chain:
        raise RuntimeError("Empty option chain")

    spot = float(chain[0].get("underlying_spot_price", 0))
    if spot <= 0:
        raise RuntimeError("Invalid underlying spot price")

    atm = round_to_50(spot)
    desired_strike = atm - 50 if side == "CE" else atm + 50

    best_row = None
    best_dist = 10**9
    for row in chain:
        strike = int(row.get("strike_price"))
        dist = abs(strike - desired_strike)
        if dist < best_dist:
            best_row = row
            best_dist = dist

    if not best_row:
        raise RuntimeError("No strike found")

    leg_key = "call_options" if side == "CE" else "put_options"
    leg = best_row.get(leg_key) or {}
    instrument_key = leg.get("instrument_key")
    ltp = float((leg.get("market_data") or {}).get("ltp") or 0)

    if not instrument_key:
        raise RuntimeError("Missing instrument_key from option chain")

    return {
        "instrument_key": instrument_key,
        "strike": int(best_row["strike_price"]),
        "spot": spot,
        "ltp": ltp,
        "expiry": best_row.get("expiry"),
    }

def risk_check(option_ltp: float, qty: int) -> Tuple[bool, str]:
    """
    Expected SL loss = ltp * qty * 7%
    Must be <= MAX_RISK_INR (₹500)
    """
    if option_ltp <= 0:
        return False, "Invalid option LTP"
    expected_loss = option_ltp * qty * FIXED_SL_PCT
    if expected_loss > MAX_RISK_INR:
        return False, f"Risk too high: expected SL loss ₹{expected_loss:.0f} > ₹{MAX_RISK_INR:.0f}"
    return True, "ok"

def cooldown_ok() -> Tuple[bool, str]:
    reset_daily_state_if_needed()
    if (time.time() - float(STATE.get("last_trade_epoch", 0))) < COOLDOWN_SECONDS:
        return False, "Cooldown active"
    return True, "ok"

# ----------------------------
# Core execution
# ----------------------------
def execute(signal: str, ts: str) -> Dict[str, Any]:
    reset_daily_state_if_needed()

    if not within_time_window():
        return {"ok": True, "ignored": True, "reason": "Outside time window (IST)"}

    ok, reason = cooldown_ok()
    if not ok:
        return {"ok": True, "ignored": True, "reason": reason}

    # avoid duplicate same candle
    cid = candle_id_15m(ts)
    if STATE.get("last_candle_id") == cid:
        return {"ok": True, "ignored": True, "reason": "Already handled this candle", "candle_id": cid}

    # daily loss stop
    pnl = current_day_pnl()
    if pnl <= -MAX_DAILY_LOSS_INR:
        return {"ok": True, "ignored": True, "reason": f"Daily loss stop hit (PnL {pnl:.0f})"}

    # one position at a time
    if has_open_nifty_option_position():
        return {"ok": True, "ignored": True, "reason": "Open position exists"}

    side = "CE" if signal == "BUY_CE" else "PE"

    expiry = get_next_thursday_ist().isoformat()  # weekly expiry (next Thursday)
    chain = fetch_option_chain(expiry)
    sel = choose_1itm(chain, side=side)

    # risk check using your ₹500 limit and SL 7%
    ok, rreason = risk_check(sel["ltp"], UPSTOX_NIFTY_QTY)
    if not ok:
        return {"ok": True, "ignored": True, "reason": rreason, "selected": sel}

    # Place market BUY
    tag = f"nifty_safe_{signal.lower()}_{now_ist().strftime('%Y%m%d_%H%M')}"
    entry_payload = {
        "quantity": UPSTOX_NIFTY_QTY,
        "product": "I",
        "validity": "DAY",
        "price": 0,
        "tag": tag,
        "instrument_token": sel["instrument_key"],
        "order_type": "MARKET",
        "transaction_type": "BUY",
        "disclosed_quantity": 0,
        "trigger_price": 0,
        "is_amo": False
    }
    entry_order_id = place_order(entry_payload)

    fill = wait_for_avg_price(entry_order_id) or max(sel["ltp"], 0.05)

    # Place SL-M SELL at 7% below entry
    sl_trigger = round_to_step(fill * (1.0 - FIXED_SL_PCT), 0.05)
    sl_payload = {
        "quantity": UPSTOX_NIFTY_QTY,
        "product": "I",
        "validity": "DAY",
        "price": 0,
        "tag": f"{tag}_sl",
        "instrument_token": sel["instrument_key"],
        "order_type": "SL-M",
        "transaction_type": "SELL",
        "disclosed_quantity": 0,
        "trigger_price": sl_trigger,
        "is_amo": False
    }
    sl_order_id = place_order(sl_payload)

    # update state
    STATE["last_trade_epoch"] = time.time()
    STATE["last_candle_id"] = cid
    save_state()

    return {
        "ok": True,
        "executed": True,
        "signal": signal,
        "side": side,
        "selected": sel,
        "entry_order_id": entry_order_id,
        "sl_order_id": sl_order_id,
        "fill_price": fill,
        "sl_trigger": sl_trigger,
        "pnl_snapshot": pnl,
        "candle_id": cid
    }

# ----------------------------
# Routes
# ----------------------------
@app.get("/health")
def health():
    reset_daily_state_if_needed()
    return jsonify({
        "ok": True,
        "time_ist": now_ist().isoformat(),
        "day": STATE.get("day"),
        "cooldown_remaining_sec": max(0, int(COOLDOWN_SECONDS - (time.time() - float(STATE.get("last_trade_epoch", 0))))),
    })

@app.post("/webhook")
def webhook():
    # Security: secret in query ?secret=
    secret = request.args.get("secret", "")
    if not TV_WEBHOOK_SECRET or secret != TV_WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"ok": False, "error": "Invalid JSON"}), 400

    signal = str(data.get("signal", "")).upper().strip()
    ts = str(data.get("ts", ""))

    if signal not in ("BUY_CE", "BUY_PE"):
        return jsonify({"ok": False, "error": "Invalid signal"}), 400

    if not UPSTOX_ACCESS_TOKEN:
        return jsonify({"ok": False, "error": "Missing UPSTOX_ACCESS_TOKEN"}), 500

    try:
        result = execute(signal, ts)
        return jsonify(result), 200
    except requests.HTTPError as e:
        body = ""
        try:
            body = e.response.text[:1200]
        except Exception:
            pass
        return jsonify({"ok": False, "error": str(e), "body": body}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == "__main__":
    # Railway sets PORT automatically
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
