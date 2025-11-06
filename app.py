import os, time, hmac, hashlib, math, json
from typing import Dict, Any
from fastapi import FastAPI, Request, HTTPException
import httpx

# ====== ENV ======
BINANCE_BASE = os.getenv("BINANCE_BASE", "https://api.binance.us")
API_KEY      = os.getenv("BINANCE_API_KEY", "")
API_SECRET   = os.getenv("BINANCE_API_SECRET", "")
TV_PASSPHRASE= os.getenv("TV_PASSPHRASE", "")
MAX_POSITIONS= int(os.getenv("MAX_CONCURRENT_POSITIONS", "1"))  # we set to 1
TP_PCT       = float(os.getenv("TP_PCT", "15"))  # +15% TP
SL_PCT       = float(os.getenv("SL_PCT", "6"))   # -6%  SL
SYMBOLS      = ["SOLUSD", "JUPUSD", "BONKUSD"]   # managed set

app = FastAPI()

@app.get("/health")
async def health():
    return {"ok": True}

# ====== HTTP helper ======
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
        raise HTTPException(status_code=r.status_code, detail=r.text)
    try:
        return r.json()
    except:
        return {"text": r.text}

# ====== Exchange info / filters ======
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

def _step(n: float, step: float) -> float:
    # floor to step
    precision = max(0, -int(round(math.log10(step)))) if step > 0 else 8
    return float((math.floor(n / step) * step))

def _round_to_tick(p: float, tick: float) -> float:
    precision = max(0, -int(round(math.log10(tick)))) if tick > 0 else 8
    return float(f"{p:.{precision}f}")

# ====== Account / Balances ======
async def get_price(symbol: str) -> float:
    data = await _req("GET", "/api/v3/ticker/price", params={"symbol": symbol})
    return float(data["price"])

async def get_account() -> Dict[str, Any]:
    return await _req("GET", "/api/v3/account", signed=True)

async def has_any_position() -> bool:
    """Return True if we already hold any of SOL/JUP/BONK (free+locked > tiny)."""
    acct = await get_account()
    bal = {b["asset"]: float(b["free"]) + float(b["locked"]) for b in acct["balances"]}
    # map base assets from our symbols
    bases = {"SOLUSD": "SOL", "JUPUSD": "JUP", "BONKUSD": "BONK"}
    for sym, base in bases.items():
        if bal.get(base, 0.0) > 0.000001:
            return True
    return False

# ====== Orders ======
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

# ====== Sizing & placement ======
async def entry_for_symbol(symbol: str, notional_pct: float):
    # 1) Skip if already holding any managed symbol (max positions = 1)
    if MAX_POSITIONS == 1 and await has_any_position():
        return {"ok": False, "note": "Position already open on one of the managed symbols."}

    # 2) Pull filters
    s = await get_symbol_filters(symbol)
    lot_step = None
    tick_size = None
    min_notional = 0.0

    for f in s["filters"]:
        t = f["filterType"]
        if t == "LOT_SIZE":
            lot_step = float(f["stepSize"])
            min_qty  = float(f["minQty"])
        elif t in ("MIN_NOTIONAL", "NOTIONAL"):
            min_notional = float(f.get("minNotional", f.get("notional", 10)))
        elif t == "PRICE_FILTER":
            tick_size = float(f["tickSize"])

    if lot_step is None or tick_size is None:
        raise HTTPException(500, "Could not resolve LOT_SIZE or PRICE_FILTER")

    # 3) Account/equity approximation (use USD balance for sizing)
    acct = await get_account()
    # Try to find USD / USDT cash pool
    usd = 0.0
    for b in acct["balances"]:
        if b["asset"] in ("USD", "USDT", "BUSD", "USDC"):
            usd += float(b["free"])
    # if no stable balances are visible, we size off a fixed $600 baseline (fallback)
    if usd <= 0:
        usd = 600.0

    notional = max(min_notional + 1.0, (usd * (notional_pct / 100.0)))
    price = await get_price(symbol)
    raw_qty = notional / price
    qty = _step(raw_qty, lot_step)
    if qty <= 0:
        return {"ok": False, "note": f"Calculated qty too small. notional={notional} price={price} lot_step={lot_step}"}

    # 4) Entry: market buy
    buy_res = await market_buy(symbol, qty)

    # 5) OCO exit (TP and SL)
    # TP +15%, SL -6% (from env)
    tp_price = _round_to_tick(price * (1 + TP_PCT / 100.0), tick_size)
    sl_stop  = _round_to_tick(price * (1 - SL_PCT / 100.0), tick_size)
    # use stopLimit slightly under stop to ensure fill
    sl_limit = _round_to_tick(sl_stop * 0.999, tick_size)

    oco_res = await place_oco_sell(symbol, qty, tp_price, sl_stop, sl_limit)

    return {"ok": True, "buy": buy_res, "oco": oco_res, "tp": tp_price, "sl": sl_stop}

# ====== API ======
@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/tv")
async def tradingview(req: Request):
    body = await req.json()
    if body.get("passphrase") != TV_PASSPHRASE:
        raise HTTPException(401, "bad passphrase")

    event = body.get("event", "")
    notional_pct = float(body.get("notional_pct", 5.0))  # default 5% per alert

    map_event = {
        "LONG_SOL":  "SOLUSD",
        "LONG_JUP":  "JUPUSD",
        "LONG_BONK": "BONKUSD"
    }

    if event in map_event:
        sym = map_event[event]
        if sym not in SYMBOLS:
            return {"ok": False, "msg": f"Unhandled symbol {sym}"}
        result = await entry_for_symbol(sym, notional_pct)
        return result

    return {"ok": False, "msg": f"Unknown event {event}"}

