import os, time, hmac, hashlib, math, json
from typing import Dict, Any
from fastapi import FastAPI, Request, HTTPException
import httpx

# ====== ENV ======
BINANCE_BASE   = os.getenv("BINANCE_BASE", "https://api.binance.us")
API_KEY        = os.getenv("BINANCE_API_KEY", "")
API_SECRET     = os.getenv("BINANCE_API_SECRET", "")
TV_PASSPHRASE  = os.getenv("TV_PASSPHRASE", "")
MAX_POSITIONS  = int(os.getenv("MAX_CONCURRENT_POSITIONS", "1"))   # default: 1
TP_PCT         = float(os.getenv("TP_PCT", "15"))                  # +15% TP
SL_PCT         = float(os.getenv("SL_PCT", "6"))                   # -6%  SL
SYMBOLS        = ["SOLUSDT", "JUPUSDT", "BONKUSDT"]

app = FastAPI()

# ----------------- Helpers -----------------
def _ts() -> str:
    return str(int(time.time() * 1000))

def _sign(params: Dict[str, Any]) -> str:
    q = "&".join([f"{k}={params[k]}" for k in sorted(params.keys())])
    return hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()

async def _req(method: str, path: str, signed=False, params=None, headers=None):
    url = f"{BINANCE_BASE}{path}"
    params = params or {}
    hdrs = headers or {}
    if signed:
        params["timestamp"] = _ts()
        params["recvWindow"] = 5000
        signature = _sign(params)
        params["signature"] = signature
        hdrs["X-MBX-APIKEY"] = API_KEY

    async with httpx.AsyncClient(timeout=20) as c:
        if method == "GET":
            r = await c.get(url, params=params, headers=hdrs)
        elif method == "POST":
            r = await c.post(url, params=params, headers=hdrs)
        elif method == "DELETE":
            r = await c.delete(url, params=params, headers=hdrs)
        else:
            raise ValueError("bad method")

    if r.status_code >= 400:
        # surface exchange/body errors back to you
        raise HTTPException(status_code=r.status_code, detail=r.text)

    try:
        return r.json()
    except Exception:
        return {"text": r.text}

# ----------------- Exchange Info / Filters -----------------
EXINFO_CACHE: Dict[str, Dict[str, Any]] = {}

async def get_symbol_filters(symbol: str) -> Dict[str, Any]:
    if symbol in EXINFO_CACHE:
        return EXINFO_CACHE[symbol]
    info = await _req("GET", "/api/v3/exchangeInfo")
    for s in info["symbols"]:
        if s["symbol"] == symbol:
            EXINFO_CACHE[symbol] = s
            return s
    raise HTTPException(400, f"symbol {symbol} not found in exchangeInfo")

def _step_floor(n: float, step: float) -> float:
    if step <= 0:
        return n
    return math.floor(n / step) * step

def _round_to_tick(p: float, tick: float) -> float:
    if tick <= 0:
        return p
    precision = max(0, -int(round(math.log10(tick))))
    return float(f"{p:.{precision}f}")

# ----------------- Account / Balances -----------------
async def get_price(symbol: str) -> float:
    data = await _req("GET", "/api/v3/ticker/price", params={"symbol": symbol})
    return float(data["price"])

async def get_account() -> Dict[str, Any]:
    return await _req("GET", "/api/v3/account", signed=True)

async def has_any_position() -> bool:
    acct = await get_account()
    bal = {b["asset"]: float(b["free"]) + float(b["locked"]) for b in acct["balances"]}
    bases = {"SOLUSDT": "SOL", "JUPUSDT": "JUP", "BONKUSDT": "BONK"}
    return any(bal.get(base, 0.0) > 0.000001 for base in bases.values())

# ----------------- Orders -----------------
async def market_buy(symbol: str, qty: float):
    params = {"symbol": symbol, "side": "BUY", "type": "MARKET", "quantity": f"{qty}"}
    return await _req("POST", "/api/v3/order", signed=True, params=params)

async def place_oco_sell(symbol: str, quantity: float, tp_price: float, sl_stop: float, sl_limit: float):
    params = {
        "symbol": symbol,
        "side": "SELL",
        "type": "OCO",
        "quantity": f"{quantity}",
        "price": f"{tp_price}",
        "stopPrice": f"{sl_stop}",
        "stopLimitPrice": f"{sl_limit}",
        "stopLimitTimeInForce": "GTC",
    }
    return await _req("POST", "/api/v3/order/oco", signed=True, params=params)

async def open_orders(symbol: str):
    return await _req("GET", "/api/v3/openOrders", signed=True, params={"symbol": symbol})

# ----------------- Sizing & Placement -----------------
async def entry_for_symbol(symbol: str, notional_pct: float):
    if MAX_POSITIONS == 1 and await has_any_position():
        return {"ok": False, "note": "Position already open on one of the managed symbols."}

    s = await get_symbol_filters(symbol)
    lot_step = None
    tick_size = None
    min_notional = 0.0

    for f in s["filters"]:
        t = f["filterType"]
        if t == "LOT_SIZE":
            lot_step = float(f["stepSize"])
        elif t in ("MIN_NOTIONAL", "NOTIONAL"):
            min_notional = float(f.get("minNotional", f.get("notional", 10)))
        elif t == "PRICE_FILTER":
            tick_size = float(f["tickSize"])

    if lot_step is None or tick_size is None:
        raise HTTPException(500, "Could not resolve LOT_SIZE or PRICE_FILTER")

    # USD/USDT/BUSD/USDC balance pool
    acct = await get_account()
    usd = 0.0
    for b in acct["balances"]:
        if b["asset"] in ("USD", "USDT", "BUSD", "USDC"):
            usd += float(b["free"])
    if usd <= 0:
        usd = 600.0  # fallback

    notional = max(min_notional + 1.0, usd * (notional_pct / 100.0))
    price = await get_price(symbol)
    qty = _step_floor(notional / price, lot_step)
    if qty <= 0:
        return {"ok": False, "note": f"Calculated qty too small. notional={notional} price={price} lot_step={lot_step}"}

    buy_res = await market_buy(symbol, qty)

    tp_price = _round_to_tick(price * (1 + TP_PCT / 100.0), tick_size)
    sl_stop  = _round_to_tick(price * (1 - SL_PCT / 100.0), tick_size)
    sl_limit = _round_to_tick(sl_stop * 0.999, tick_size)

    oco_res = await place_oco_sell(symbol, qty, tp_price, sl_stop, sl_limit)
    return {"ok": True, "buy": buy_res, "oco": oco_res, "tp": tp_price, "sl": sl_stop}

# ----------------- API -----------------
@app.get("/health")
async def health():
    return {"ok": True, "tv_passphrase_len": len(TV_PASSPHRASE)}

@app.post("/tv")
async def tradingview(req: Request):
    # Try JSON; if that fails, try to parse text as JSON.
    try:
        body = await req.json()
    except Exception:
        raw = (await req.body()).decode("utf-8", errors="ignore").strip()
        try:
            body = json.loads(raw)
        except Exception:
            raise HTTPException(400, f"Invalid payload: expected JSON, got: '{raw[:80]}'")

    p = body.get("passphrase", "")
    if p != TV_PASSPHRASE:
        raise HTTPException(401, f"bad passphrase (len {len(p)})")

    event = body.get("event", "")
    notional_pct = float(body.get("notional_pct", 5.0))

    map_event = {
        "LONG_SOL":  "SOLUSDT",
        "LONG_JUP":  "JUPUSDT",
        "LONG_BONK": "BONKUSDT",
    }

    if event not in map_event:
        return {"ok": False, "msg": f"Unknown event '{event}'"}

    sym = map_event[event]
    if sym not in SYMBOLS:
        return {"ok": False, "msg": f"Unhandled symbol {sym}"}

    result = await entry_for_symbol(sym, notional_pct)
    return result
