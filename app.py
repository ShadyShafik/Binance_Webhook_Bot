import os, time, hmac, hashlib, math
from typing import Dict, Any
from fastapi import FastAPI, Request, HTTPException
import httpx

# ===== ENV =====
BINANCE_BASE = os.getenv("BINANCE_BASE", "https://api.binance.us")
API_KEY      = os.getenv("BINANCE_API_KEY", "")
API_SECRET   = os.getenv("BINANCE_API_SECRET", "")
TV_PASSPHRASE= os.getenv("TV_PASSPHRASE", "")

MAX_POSITIONS = int(os.getenv("MAX_CONCURRENT_POSITIONS", "1"))
TP_PCT = float(os.getenv("TP_PCT", "15"))
SL_PCT = float(os.getenv("SL_PCT", "6"))

SYMBOLS = ["SOLUSDT", "JUPUSDT", "BONKUSDT"]

app = FastAPI()

@app.get("/health")
async def health():
    return {"ok": True}

# ===== Helpers =====

def _ts() -> str:
    return str(int(time.time() * 1000))

def _sign(params: Dict[str, Any]) -> str:
    query = "&".join([f"{k}={params[k]}" for k in sorted(params.keys())])
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()

async def _req(method: str, path: str, signed=False, params=None):
    url = f"{BINANCE_BASE}{path}"
    params = params or {}
    headers = {}

    if signed:
        params["recvWindow"] = 5000
        params["timestamp"] = _ts()
        params["signature"] = _sign(params)
        headers["X-MBX-APIKEY"] = API_KEY

    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.request(method, url, params=params, headers=headers)

    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text)

    return r.json()

# ===== Account =====

async def get_account():
    return await _req("GET", "/api/v3/account", signed=True)

async def has_any_position():
    acct = await get_account()
    bal = {b["asset"]: float(b["free"]) + float(b["locked"]) for b in acct["balances"]}
    bases = {"SOLUSDT": "SOL", "JUPUSDT": "JUP", "BONKUSDT": "BONK"}
    return any(bal.get(base, 0) > 0.0001 for base in bases.values())

async def get_price(symbol):
    r = await _req("GET", "/api/v3/ticker/price", params={"symbol": symbol})
    return float(r["price"])

# ===== Orders =====

async def market_buy(symbol, qty):
    return await _req("POST", "/api/v3/order", signed=True, params={
        "symbol": symbol,
        "side": "BUY",
        "type": "MARKET",
        "quantity": qty
    })

async def place_oco(symbol, qty, tp, sl_stop, sl_limit):
    return await _req("POST", "/api/v3/order/oco", signed=True, params={
        "symbol": symbol,
        "side": "SELL",
        "type": "OCO",
        "quantity": qty,
        "price": tp,
        "stopPrice": sl_stop,
        "stopLimitPrice": sl_limit,
        "stopLimitTimeInForce": "GTC"
    })

# ===== Main Logic =====

async def enter_trade(symbol, pct):
    if MAX_POSITIONS == 1 and await has_any_position():
        return {"ok": False, "msg": "Position already open. Skipping."}

    price = await get_price(symbol)

    acct = await get_account()
    usd = sum(float(b["free"]) for b in acct["balances"] if b["asset"] in ("USDT", "USD", "BUSD"))
    if usd <= 0: usd = 600

    notional = usd * (pct / 100.0)
    qty = round(notional / price, 4)
    if qty <= 0:
        return {"ok": False, "msg": "Calculated qty too small."}

    buy = await market_buy(symbol, qty)

    tp = round(price * (1 + TP_PCT/100), 4)
    sl = round(price * (1 - SL_PCT/100), 4)
    sl_lim = round(sl * 0.999, 4)

    oco = await place_oco(symbol, qty, tp, sl, sl_lim)

    return {"ok": True, "symbol": symbol, "qty": qty, "tp": tp, "sl": sl}

# ===== Webhook =====

@app.post("/tv")
async def tradingview(req: Request):
    body = await req.json()

    if body.get("passphrase") != TV_PASSPHRASE:
        raise HTTPException(401, "bad passphrase")

    event = body.get("event")
    pct = float(body.get("notional_pct", 5))

    mapping = {
        "LONG_SOL": "SOLUSDT",
        "LONG_JUP": "JUPUSDT",
        "LONG_BONK": "BONKUSDT"
    }

    if event not in mapping:
        return {"ok": False, "msg": f"Unknown event {event}"}

    return await enter_trade(mapping[event], pct)
