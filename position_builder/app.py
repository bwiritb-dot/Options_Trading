from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import numpy as np
from scipy.stats import norm
from datetime import datetime, timedelta
import json

app = Flask(__name__)
CORS(app)

# Deribit API base URL
DERIBIT_API = "https://www.deribit.com/api/v2"

# Cache for instruments and current price
CACHE = {
    "instruments": [],
    "current_price": None,
    "last_update": None
}

class BlackScholesCalculator:
    """Black-Scholes option pricing model"""

    @staticmethod
    def d1(S, K, T, r, sigma):
        """Calculate d1 in Black-Scholes formula"""
        if T <= 0:
            return float('inf') if S > K else float('-inf')
        return (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))

    @staticmethod
    def d2(S, K, T, r, sigma):
        """Calculate d2 in Black-Scholes formula"""
        if T <= 0:
            return float('inf') if S > K else float('-inf')
        d1 = BlackScholesCalculator.d1(S, K, T, r, sigma)
        return d1 - sigma * np.sqrt(T)

    @staticmethod
    def call_price(S, K, T, r, sigma):
        """European call option price"""
        if T <= 0:
            return max(S - K, 0)
        d1 = BlackScholesCalculator.d1(S, K, T, r, sigma)
        d2 = BlackScholesCalculator.d2(S, K, T, r, sigma)
        return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)

    @staticmethod
    def put_price(S, K, T, r, sigma):
        """European put option price"""
        if T <= 0:
            return max(K - S, 0)
        d1 = BlackScholesCalculator.d1(S, K, T, r, sigma)
        d2 = BlackScholesCalculator.d2(S, K, T, r, sigma)
        return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

    @staticmethod
    def option_price(S, K, T, r, sigma, option_type):
        """Get option price based on type (CALL or PUT)"""
        if option_type == "CALL":
            return BlackScholesCalculator.call_price(S, K, T, r, sigma)
        else:
            return BlackScholesCalculator.put_price(S, K, T, r, sigma)

    @staticmethod
    def delta(S, K, T, r, sigma, option_type):
        """Calculate delta"""
        if T <= 0:
            if option_type == "CALL":
                return 1.0 if S > K else 0.0
            else:
                return -1.0 if S < K else 0.0
        d1 = BlackScholesCalculator.d1(S, K, T, r, sigma)
        if option_type == "CALL":
            return norm.cdf(d1)
        else:
            return norm.cdf(d1) - 1

    @staticmethod
    def gamma(S, K, T, r, sigma):
        """Calculate gamma"""
        if T <= 0:
            return 0
        d1 = BlackScholesCalculator.d1(S, K, T, r, sigma)
        return norm.pdf(d1) / (S * sigma * np.sqrt(T))

    @staticmethod
    def vega(S, K, T, r, sigma):
        """Calculate vega (per 1% change in volatility)"""
        if T <= 0:
            return 0
        d1 = BlackScholesCalculator.d1(S, K, T, r, sigma)
        return S * norm.pdf(d1) * np.sqrt(T) / 100

    @staticmethod
    def theta(S, K, T, r, sigma, option_type):
        """Calculate theta (per day)"""
        if T <= 0:
            return 0
        d1 = BlackScholesCalculator.d1(S, K, T, r, sigma)
        d2 = BlackScholesCalculator.d2(S, K, T, r, sigma)

        if option_type == "CALL":
            theta = (-S * norm.pdf(d1) * sigma / (2 * np.sqrt(T)) -
                    r * K * np.exp(-r * T) * norm.cdf(d2))
        else:
            theta = (-S * norm.pdf(d1) * sigma / (2 * np.sqrt(T)) +
                    r * K * np.exp(-r * T) * norm.cdf(-d2))

        return theta / 365  # Convert to per day


def fetch_eth_instruments():
    """Fetch all ETH options from Deribit API"""
    try:
        print("Fetching ETH instruments from Deribit...")
        # Get all ETH instruments
        response = requests.get(f"{DERIBIT_API}/public/get_instruments",
                              params={"currency": "ETH", "kind": "option"},
                              timeout=15)

        if response.status_code == 200:
            instruments = response.json().get("result", [])
            # Filter for active instruments
            active = [i for i in instruments if i.get("is_active")]
            CACHE["instruments"] = sorted(active, key=lambda x: x["instrument_name"])
            CACHE["last_update"] = datetime.now()
            print(f"✓ Loaded {len(active)} ETH options")
            return active
    except requests.exceptions.Timeout:
        print("❌ Deribit API timeout - using cached data")
    except requests.exceptions.ConnectionError:
        print("❌ Connection error - check internet or Deribit API status")
    except Exception as e:
        print(f"❌ Error fetching instruments: {e}")

    return CACHE.get("instruments", [])


def fetch_current_price():
    """Fetch current ETH price"""
    try:
        response = requests.get(f"{DERIBIT_API}/public/ticker",
                              params={"instrument_name": "ETH-PERPETUAL"},
                              timeout=10)

        if response.status_code == 200:
            data = response.json().get("result", {})
            price = data.get("last_price")
            if price:
                CACHE["current_price"] = price
                return price
    except Exception as e:
        print(f"Error fetching price: {e}")

    return CACHE.get("current_price", 2000)


@app.route("/api/instruments", methods=["GET"])
def get_instruments():
    """Get all ETH options instruments"""
    instruments = fetch_eth_instruments()
    return jsonify({"instruments": instruments})


@app.route("/api/current-price", methods=["GET"])
def get_current_price():
    """Get current ETH price"""
    price = fetch_current_price()
    return jsonify({"price": price})


@app.route("/api/ticker", methods=["GET"])
def get_ticker():
    """Get live ticker data for a single instrument"""
    instrument_name = request.args.get("instrument")
    if not instrument_name:
        return jsonify({"error": "instrument param required"}), 400
    try:
        r = requests.get(f"{DERIBIT_API}/public/ticker",
                         params={"instrument_name": instrument_name}, timeout=8)
        if r.status_code == 200:
            result = r.json().get("result", {})
            return jsonify({
                "mark_price": result.get("mark_price", 0),
                "mark_iv":    result.get("mark_iv", 0),
                "best_bid":   result.get("best_bid_price", 0),
                "best_ask":   result.get("best_ask_price", 0),
                "index":      result.get("index_price", 0),
            })
    except Exception as e:
        print(f"Ticker error: {e}")
    return jsonify({"mark_price": 0, "mark_iv": 0})


@app.route("/api/pnl-curve", methods=["POST"])
def calculate_pnl_curve():
    """
    Returns two PNL curves:
    - pnl_expiry : intrinsic-only payoff at expiry (purple line)
    - pnl_theo   : BS value at selected slider date  (green line)
    Prices denominated in USD (mark_price * index).
    """
    data          = request.json
    positions     = data.get("positions", [])
    time_date     = data.get("time_date")
    vol_shift     = data.get("vol_shift", 0) / 100
    rate_shift    = data.get("rate_shift", 0) / 100
    current_price = data.get("current_price", None) or fetch_current_price()
    in_eth        = data.get("currency", "USD") == "ETH"

    time_now      = datetime.now()
    selected_time = datetime.fromisoformat(time_date.replace("Z", "+00:00")).replace(tzinfo=None)
    risk_free     = 0.05 + rate_shift

    spot_min = current_price * 0.60
    spot_max = current_price * 1.40

    # Always include all strikes with margin so expiry line is never flat-only
    for pos in positions:
        parts  = pos["instrument_name"].split("-")
        strike = float(parts[2])
        spot_min = min(spot_min, strike * 0.88)
        spot_max = max(spot_max, strike * 1.12)

    spots = np.linspace(spot_min, spot_max, 200)

    pnl_expiry = np.zeros(len(spots))
    pnl_theo   = np.zeros(len(spots))

    for pos in positions:
        name      = pos["instrument_name"]
        amount    = pos["amount"]
        avg_price = pos.get("avg_price", pos.get("mark_price", 0))
        iv        = max(pos.get("iv", 60), 1) / 100      # from pct → decimal
        adj_iv    = max(iv * (1 + vol_shift), 0.001)

        parts       = name.split("-")
        strike      = float(parts[2])
        option_type = "CALL" if parts[3] == "C" else "PUT"
        expiry_str  = parts[1]   # e.g. "10APR26"
        expiry_dt   = datetime.strptime(expiry_str, "%d%b%y")

        T_expiry  = max((expiry_dt - time_now).total_seconds() / (365.25*24*3600), 0.0)
        T_slider  = max((selected_time - time_now).total_seconds() / (365.25*24*3600), 0.0)
        T_remain  = max(T_expiry - T_slider, 0.0)   # time-to-expiry at the slider date

        for i, S in enumerate(spots):
            # ---- expiry payoff (intrinsic) ----
            if option_type == "CALL":
                intrinsic = max(S - strike, 0.0)
            else:
                intrinsic = max(strike - S, 0.0)

            # Convert entry price to USD
            entry_usd = avg_price * S  # Deribit quotes in ETH of index
            pnl_e     = (intrinsic - entry_usd) * amount
            if in_eth and current_price:
                pnl_e /= current_price
            pnl_expiry[i] += pnl_e

            # ---- theoretical value at slider date ----
            if T_remain <= 0:
                theo_usd = intrinsic
            else:
                theo_usd = BlackScholesCalculator.option_price(
                    S, strike, T_remain, risk_free, adj_iv, option_type
                )
            pnl_t = (theo_usd - entry_usd) * amount
            if in_eth and current_price:
                pnl_t /= current_price
            pnl_theo[i] += pnl_t

    return jsonify({
        "spots":      spots.tolist(),
        "pnl_now":    pnl_expiry.tolist(),   # purple expiry line
        "pnl_theo":   pnl_theo.tolist(),     # green theoretical line
    })


@app.route("/api/position-greeks", methods=["POST"])
def calculate_greeks():
    """Calculate Greeks for a position"""
    data = request.json

    instrument_name = data.get("instrument_name")
    spot = data.get("spot", fetch_current_price())
    time_date = data.get("time_date")
    vol_shift = data.get("vol_shift", 0) / 100
    rate_shift = data.get("rate_shift", 0) / 100

    # Parse instrument
    parts = instrument_name.split("-")
    strike = float(parts[2])
    option_type = "CALL" if parts[3] == "C" else "PUT"
    expiry_str = parts[1]  # e.g., "10APR26"

    # Parse expiry date
    expiry = datetime.strptime(expiry_str, "%d%b%y")
    time_to_expiry = (expiry - datetime.now()).total_seconds() / (365.25 * 24 * 3600)
    time_to_expiry = max(time_to_expiry, 0.001)  # Minimum 1 day

    # IV is provided in percent (e.g. 63.0 = 63%), convert to decimal
    iv = max(data.get("iv", 60), 1) / 100
    adjusted_iv = max(iv * (1 + vol_shift), 0.001)

    risk_free_rate = 0.05 + rate_shift

    delta = BlackScholesCalculator.delta(spot, strike, time_to_expiry, risk_free_rate, adjusted_iv, option_type)
    gamma = BlackScholesCalculator.gamma(spot, strike, time_to_expiry, risk_free_rate, adjusted_iv)
    vega = BlackScholesCalculator.vega(spot, strike, time_to_expiry, risk_free_rate, adjusted_iv)
    theta = BlackScholesCalculator.theta(spot, strike, time_to_expiry, risk_free_rate, adjusted_iv, option_type)

    return jsonify({
        "delta": delta,
        "gamma": gamma,
        "vega": vega,
        "theta": theta
    })


if __name__ == "__main__":
    print("\n" + "="*50)
    print("🚀 Deribit Position Builder - Backend")
    print("="*50)
    print(f"✓ CORS enabled")
    print(f"✓ Flask running on: http://localhost:5000")
    print(f"✓ Fetching live Deribit data...")
    print("="*50 + "\n")

    # Pre-fetch data on startup
    fetch_eth_instruments()
    fetch_current_price()

    print(f"\n✓ Backend ready!")
    print(f"✓ Frontend: Open index.html or http://localhost:8000\n")

    app.run(debug=False, port=5000, host='127.0.0.1')
