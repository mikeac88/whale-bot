
import os, json, time, logging, threading, traceback
from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytz
import requests
from flask import Flask, jsonify, request

KEY        = os.environ.get("ALPACA_API_KEY", "")
SECRET     = os.environ.get("ALPACA_SECRET_KEY", "")
BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
DATA_URL   = "https://data.alpaca.markets"
DASH_PASS  = os.environ.get("DASHBOARD_PASSWORD", "whale2024")
PORT       = int(os.environ.get("PORT", 8080))
KEEPALIVE  = os.environ.get("RENDER_EXTERNAL_URL", "")

TRADE_SIZE   = float(os.environ.get("TRADE_SIZE",   "25"))
MAX_POSITION = float(os.environ.get("MAX_POSITION", "1000000"))
DAILY_GOAL   = float(os.environ.get("DAILY_GOAL",   "100"))
MAX_LOSS     = float(os.environ.get("MAX_LOSS",     "25"))
STOP_PCT     = float(os.environ.get("STOP_PCT",     "0.05"))
TARGET_PCT   = float(os.environ.get("TARGET_PCT",   "0.07"))
MIN_SCORE    = int(os.environ.get("MIN_SCORE",      "55"))
MIN_PRICE    = float(os.environ.get("MIN_PRICE",    "10.0"))
BEAR_MIN_PRICE = float(os.environ.get("BEAR_MIN_PRICE", "3.0"))

SHORT_CHECK_ENABLED = os.environ.get("SHORT_CHECK_ENABLED", "true").lower() == "true"
MIN_RVOL            = float(os.environ.get("MIN_RVOL", "0.5"))
MAX_SPREAD_PCT      = float(os.environ.get("MAX_SPREAD_PCT", "0.005"))
VOL_FEED            = os.environ.get("VOL_FEED", "delayed_sip")
AGENT_RETRIES       = int(os.environ.get("AGENT_RETRIES", "2"))
AGENT_CACHE_SEC     = int(os.environ.get("AGENT_CACHE_SEC", "300"))
MAX_DT       = int(os.environ.get("MAX_DAY_TRADES", "2"))
MAX_RISK_PCT = float(os.environ.get("MAX_RISK_PCT", "0.01"))

COOLDOWN_MIN       = int(os.environ.get("SYMBOL_COOLDOWN_MIN", "30"))
MAX_TRADES_PER_DAY = int(os.environ.get("MAX_TRADES_PER_DAY", "5"))

MOVERS_ENABLED = os.environ.get("MOVERS_ENABLED", "true").lower() == "true"
MOVERS_COUNT   = int(os.environ.get("MOVERS_COUNT",   "15"))
MOVERS_MIN_CHG = float(os.environ.get("MOVERS_MIN_CHG", "3.0"))

GAP_ENABLED   = os.environ.get("GAP_ENABLED", "false").lower() == "true"
GAP_MIN_PCT   = float(os.environ.get("GAP_MIN_PCT",   "0.04"))
GAP_MIN_PMVOL = int(os.environ.get("GAP_MIN_PMVOL", "50000"))
GAP_BOOST     = int(os.environ.get("GAP_BOOST",        "12"))

DISCOVERED_MAX_CHG  = float(os.environ.get("DISCOVERED_MAX_CHG",  "0.25"))
DISCOVERED_MIN_DVOL = float(os.environ.get("DISCOVERED_MIN_DVOL", "5000000"))

NEWS_LAYER_ENABLED = os.environ.get("NEWS_LAYER_ENABLED", "true").lower() == "true"
NEWS_REQUIRED      = os.environ.get("NEWS_REQUIRED", "false").lower() == "true"
NEWS_BOOST         = int(os.environ.get("NEWS_BOOST",        "10"))
NEWS_MAX_AGE_HRS   = float(os.environ.get("NEWS_MAX_AGE_HRS", "24"))
NEWS_CACHE_MIN     = int(os.environ.get("NEWS_CACHE_MIN",     "10"))

VWAP_FILTER   = os.environ.get("VWAP_FILTER", "true").lower() == "true"
VWAP_MIN_BARS = int(os.environ.get("VWAP_MIN_BARS",     "5"))
ORB_ENABLED   = os.environ.get("ORB_ENABLED", "true").lower() == "true"
ORB_MINUTES   = int(os.environ.get("ORB_MINUTES",      "15"))
ORB_BOOST     = int(os.environ.get("ORB_BOOST",        "12"))

AGENT_URL        = os.environ.get("AGENT_URL", "").rstrip("/")
AGENT_AUTH_TOKEN = os.environ.get("AGENT_AUTH_TOKEN", "")
AGENT_TIMEOUT_S  = float(os.environ.get("AGENT_TIMEOUT_S", "8"))

LIVE_MODE = "paper" not in BASE_URL.lower()
ET = pytz.timezone("America/New_York")
UTC = pytz.UTC

LOG_FILE = "/tmp/whale_bot.log"

try:
    if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 5_000_000:
        with open(LOG_FILE, "r") as f:
            kept = f.readlines()[-1000:]
        with open(LOG_FILE, "w") as f:
            f.writelines(kept)
except Exception:
    pass

_root = logging.getLogger()
_root.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(message)s")

if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
           for h in _root.handlers):
    _sh = logging.StreamHandler()
    _sh.setFormatter(_fmt)
    _root.addHandler(_sh)

try:
    if not any(isinstance(h, logging.FileHandler) for h in _root.handlers):
        _fh = logging.FileHandler(LOG_FILE, mode="a")
        _fh.setFormatter(_fmt)
        _root.addHandler(_fh)
except Exception:
    pass

log = logging.getLogger("whale")
log.setLevel(logging.INFO)

TIER1 = ["NVDA", "AMD", "TSLA", "PLTR", "SMCI", "MSTR", "MARA", "RIOT", "SOXL", "TQQQ"]
BEAR_TICKERS = ["SQQQ", "SPXS", "SDOW", "TZA", "FAZ", "UVXY", "VXX", "SOXS", "TECS", "LABD"]

TICKERS = sorted(set(TIER1 + BEAR_TICKERS + [
    "META", "GOOGL", "MSFT", "AMZN", "AAPL", "NFLX", "UBER", "HOOD", "SOFI",
    "UPST", "COIN", "ARKK", "OXY", "DVN", "MRO", "HAL", "XLE", "BOIL", "UCO",
    "ERX", "CCJ", "NNE", "OKLO", "SMR", "UEC", "URA", "FCX", "MP", "LAC", "ALB",
    "COPX", "GDX", "GDXJ", "NEM", "WPM", "IONQ", "RGTI", "RKLB", "ASTS", "LUNR",
    "CLSK", "WULF", "JOBY", "TXN", "URI", "IBM", "NOW", "AAL", "CMCSA", "BA",
    "MU", "CVNA", "SHOP", "SNAP", "RBLX", "DKNG", "PENN", "CHWY", "LYFT",
]))

_lock = threading.Lock()
_state = {
    "paused": False,
    "e_stop": False,
    "alerts": [],
    "news_tickers": [],
    "mover_tickers": [],
    "gap_candidates": {},
    "traded_today": [],
    "lost_today": [],
    "bot_dt_count": 0,
    "completed_trades_today": 0,
    "sym_cooldown": {},
    "best_score_today": 0,
    "best_setup_today": None,
    "triggered_today": set(),
    "tracker": {
        "date": datetime.now(ET).strftime("%Y-%m-%d"),
        "wins_today": 0,
        "losses_today": 0,
        "day_trades": 0,
        "total_trades": 0,
        "total_wins": 0,
    },
    "health": {
        "last_alpaca_ok": None,
        "last_alpaca_err": None,
        "last_scan_at": None,
        "last_loop_at": None,
        "alpaca_call_count": 0,
        "alpaca_err_count": 0,
    }
}

ALERTS_FILE = "/tmp/whale_alerts.json"

def _now_et_str():
    return datetime.now(ET).strftime("%H:%M:%S")

def push_alert(msg, level="info"):
    entry = {"t": _now_et_str(), "m": msg, "l": level}
    with _lock:
        _state["alerts"].append(entry)
        if len(_state["alerts"]) > 100:
            _state["alerts"].pop(0)
    try:
        saved = []
        if os.path.exists(ALERTS_FILE):
            with open(ALERTS_FILE) as f:
                saved = json.load(f)
        saved.append(entry)
        with open(ALERTS_FILE, "w") as f:
            json.dump(saved[-100:], f)
    except Exception:
        pass
    log.info(msg)

def restore_alerts():
    try:
        if os.path.exists(ALERTS_FILE):
            with open(ALERTS_FILE) as f:
                saved = json.load(f)
            with _lock:
                _state["alerts"] = saved[-100:]
    except Exception:
        pass

def update_health(field, value):
    with _lock:
        _state["health"][field] = value

def bump_health(field):
    with _lock:
        _state["health"][field] = _state["health"].get(field, 0) + 1

HDR = {
    "APCA-API-KEY-ID": KEY,
    "APCA-API-SECRET-KEY": SECRET,
    "Content-Type": "application/json",
}

def aGet(path, params=None, base=None, timeout=10):
    url = (base or BASE_URL) + path
    bump_health("alpaca_call_count")
    try:
        r = requests.get(url, headers=HDR, params=params, timeout=timeout)
        r.raise_for_status()
        update_health("last_alpaca_ok", _now_et_str())
        return r.json()
    except Exception as e:
        bump_health("alpaca_err_count")
        update_health("last_alpaca_err", f"{_now_et_str()} {type(e).__name__}: {str(e)[:100]}")
        raise

def aPost(path, body, timeout=10):
    bump_health("alpaca_call_count")
    try:
        r = requests.post(BASE_URL + path, headers=HDR, json=body, timeout=timeout)
        r.raise_for_status()
        update_health("last_alpaca_ok", _now_et_str())
        return r.json()
    except Exception as e:
        bump_health("alpaca_err_count")
        update_health("last_alpaca_err", f"{_now_et_str()} {type(e).__name__}: {str(e)[:100]}")
        raise

def aDel(path, timeout=10):
    bump_health("alpaca_call_count")
    try:
        r = requests.delete(BASE_URL + path, headers=HDR, timeout=timeout)
        update_health("last_alpaca_ok", _now_et_str())
        return r.json() if r.text.strip() else {}
    except Exception as e:
        bump_health("alpaca_err_count")
        update_health("last_alpaca_err", f"{_now_et_str()} {type(e).__name__}: {str(e)[:100]}")
        raise

def get_account():    return aGet("/v2/account")
def get_positions():  return aGet("/v2/positions")
def get_orders_open(): return aGet("/v2/orders", {"status": "open", "limit": 50})
def get_clock():      return aGet("/v2/clock")

def get_today_fills():
    now_et = datetime.now(ET)
    today_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    after = today_start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return aGet("/v2/account/activities/FILL",
                params={"after": after, "direction": "asc", "page_size": 100}) or []

def get_recent_fills(days=7):
    now_et = datetime.now(ET)
    today_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    after_dt = today_start - timedelta(days=days)
    after = after_dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return aGet("/v2/account/activities/FILL",
                params={"after": after, "direction": "asc", "page_size": 100}) or []

def get_portfolio_history(period="1A", timeframe="1D"):
    return aGet("/v2/account/portfolio/history",
                params={"period": period, "timeframe": timeframe}) or {}

_period_cache = {"data": None, "ts": None}
_period_cache_lock = threading.Lock()

def get_period_pnl(force=False):
    now = datetime.now(ET)
    with _period_cache_lock:
        cached = _period_cache["data"]
        cached_ts = _period_cache["ts"]
    if not force and cached and cached_ts and (now - cached_ts).total_seconds() < 300:
        return cached

    result = {
        "daily": 0.0, "weekly": 0.0, "monthly": 0.0, "yearly": 0.0,
        "daily_pct": 0.0, "weekly_pct": 0.0, "monthly_pct": 0.0, "yearly_pct": 0.0,
        "ok": False,
    }

    try:
        acct = get_account()
        try:
            cur = float(acct.get("equity", 0))
            last_eq = float(acct.get("last_equity", cur))
        except (TypeError, ValueError):
            cur = 0.0
            last_eq = 0.0

        if cur > 0 and last_eq > 0:
            result["daily"] = round(cur - last_eq, 2)
            result["daily_pct"] = round((cur - last_eq) / last_eq * 100, 2)
            result["ok"] = True

        periods = [("weekly", "1W"), ("monthly", "1M"), ("yearly", "1A")]
        for key, period in periods:
            try:
                hist = get_portfolio_history(period=period, timeframe="1D")
                eq_arr = hist.get("equity", []) or []
                valid = [float(e) for e in eq_arr if e is not None and float(e) > 0]
                if valid and cur > 0:
                    base = valid[0]
                    if base > 0:
                        result[key] = round(cur - base, 2)
                        result[key + "_pct"] = round((cur - base) / base * 100, 2)
            except Exception as e:
                log.debug(f"period {key}: {e}")

    except Exception as e:
        log.debug(f"period pnl outer: {e}")

    with _period_cache_lock:
        _period_cache["data"] = result
        _period_cache["ts"] = now
    return result

def _group_fills(fills):
    if not fills:
        return []
    by_order = {}
    for f in fills:
        oid = f.get("order_id", "")
        if not oid:
            continue
        if oid not in by_order:
            by_order[oid] = {
                "order_id": oid,
                "sym":   f.get("symbol", ""),
                "side":  f.get("side", ""),
                "qty":   0.0,
                "value": 0.0,
                "time":  (f.get("transaction_time", "") or "")[:19].replace("T", " "),
            }
        try:
            q = float(f.get("qty", 0))
            p = float(f.get("price", 0))
        except (TypeError, ValueError):
            continue
        by_order[oid]["qty"]   += q
        by_order[oid]["value"] += q * p

    out = []
    for t in by_order.values():
        if t["qty"] > 0:
            out.append({
                "order_id": t["order_id"],
                "sym":   t["sym"],
                "side":  t["side"],
                "qty":   int(t["qty"]) if t["qty"] == int(t["qty"]) else round(t["qty"], 4),
                "price": round(t["value"] / t["qty"], 2),
                "time":  t["time"],
            })
    out.sort(key=lambda x: x["time"])
    return out

def compute_trades_and_pnl(today_fills, lookback_fills=None):
    if lookback_fills is None:
        lookback_fills = today_fills

    if not today_fills and not lookback_fills:
        return [], 0, 0, 0.0

    today_order_ids = {f.get("order_id", "") for f in today_fills if f.get("order_id")}

    today_trades = _group_fills(today_fills)
    all_trades   = _group_fills(lookback_fills)

    display = [{k: v for k, v in t.items() if k != "order_id"} for t in today_trades]

    by_sym = {}
    for t in all_trades:
        sym = t["sym"]
        by_sym.setdefault(sym, {"buys": [], "sells": []})
        bucket = "buys" if t["side"] == "buy" else "sells"
        by_sym[sym][bucket].append({
            "qty":      float(t["qty"]),
            "price":    t["price"],
            "order_id": t["order_id"],
        })

    today_w = 0
    today_l = 0
    today_realized = 0.0
    for sym, data in by_sym.items():
        buys = [{"qty": b["qty"], "price": b["price"]} for b in data["buys"]]
        for sell in data["sells"]:
            qty_left = sell["qty"]
            cost_basis = 0.0
            matched_qty = 0.0
            for buy in buys:
                if qty_left <= 0 or buy["qty"] <= 0:
                    continue
                take = min(qty_left, buy["qty"])
                cost_basis += take * buy["price"]
                matched_qty += take
                buy["qty"] -= take
                qty_left  -= take
            if matched_qty > 0 and sell["order_id"] in today_order_ids:
                pnl = (sell["price"] * matched_qty) - cost_basis
                today_realized += pnl
                if pnl > 0:
                    today_w += 1
                else:
                    today_l += 1

    return display, today_w, today_l, round(today_realized, 2)

def update_closed_trade_state(today_fills, lookback_fills=None):
    if lookback_fills is None:
        lookback_fills = today_fills
    if not today_fills and not lookback_fills:
        return

    today_order_ids = {f.get("order_id", "") for f in today_fills if f.get("order_id")}
    all_trades = _group_fills(lookback_fills)

    by_sym = {}
    for t in all_trades:
        sym = t["sym"]
        by_sym.setdefault(sym, {"buys": [], "sells": []})
        bucket = "buys" if t["side"] == "buy" else "sells"
        by_sym[sym][bucket].append({
            "qty":      float(t["qty"]),
            "price":    t["price"],
            "order_id": t["order_id"],
            "time":     t["time"],
        })

    new_lost = set()
    new_cooldown = {}
    bot_dt = 0
    completed = 0

    for sym, data in by_sym.items():
        buys = [{"qty": b["qty"], "price": b["price"], "order_id": b["order_id"]}
                for b in data["buys"]]
        for sell in sorted(data["sells"], key=lambda x: x["time"]):
            qty_left = sell["qty"]
            cost_basis = 0.0
            matched_qty = 0.0
            buy_was_today = False
            for buy in buys:
                if qty_left <= 0 or buy["qty"] <= 0:
                    continue
                take = min(qty_left, buy["qty"])
                cost_basis += take * buy["price"]
                matched_qty += take
                if buy["order_id"] in today_order_ids:
                    buy_was_today = True
                buy["qty"] -= take
                qty_left -= take

            if matched_qty > 0 and sell["order_id"] in today_order_ids:
                completed += 1
                if buy_was_today:
                    bot_dt += 1
                pnl = (sell["price"] * matched_qty) - cost_basis
                if pnl < 0:
                    new_lost.add(sym)
                prev = new_cooldown.get(sym, "")
                if sell["time"] > prev:
                    new_cooldown[sym] = sell["time"]

    cooldown_until = {}
    now = datetime.now(ET)
    for sym, sell_time_str in new_cooldown.items():
        try:
            sold_utc = datetime.strptime(sell_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            cooldown_end = sold_utc + timedelta(minutes=COOLDOWN_MIN)
            if cooldown_end > now.astimezone(UTC):
                cooldown_until[sym] = cooldown_end.astimezone(ET).isoformat()
        except Exception:
            cooldown_until[sym] = (now + timedelta(minutes=COOLDOWN_MIN)).isoformat()

    with _lock:
        for s in new_lost:
            if s not in _state["lost_today"]:
                _state["lost_today"].append(s)
        _state["bot_dt_count"] = bot_dt
        _state["completed_trades_today"] = completed
        _state["sym_cooldown"] = cooldown_until

class DataSnapshot:
    def __init__(self):
        self.acct = {}
        self.positions = []
        self.orders = []
        self.clock = {}
        self.fills = []
        self.lookback_fills = []
        self.period_pnl = {}
        self.errors = []

    def fetch(self):
        try:
            self.acct = get_account()
        except Exception as e:
            self.errors.append(f"account: {e}")

        try:
            self.positions = get_positions()
        except Exception as e:
            self.errors.append(f"positions: {e}")

        try:
            self.orders = get_orders_open()
        except Exception as e:
            self.errors.append(f"orders: {e}")

        try:
            self.clock = get_clock()
        except Exception as e:
            self.errors.append(f"clock: {e}")

        try:
            self.fills = get_today_fills()
        except Exception as e:
            self.errors.append(f"fills: {e}")

        try:
            self.lookback_fills = get_recent_fills(days=7)
        except Exception as e:
            self.errors.append(f"lookback_fills: {e}")

        try:
            self.period_pnl = get_period_pnl()
        except Exception as e:
            self.errors.append(f"period_pnl: {e}")

        return self

_snap_lock = threading.Lock()
_snap_data = {"snap": None, "ts": None}

_last_good = {"acct": {}, "positions": [], "orders": [], "clock": {}, "fills": [],
              "lookback_fills": [], "period_pnl": {}}
_last_good_lock = threading.Lock()

def get_snapshot(max_age_s=4):
    now = datetime.now(ET)
    with _snap_lock:
        s = _snap_data["snap"]
        ts = _snap_data["ts"]
        fresh = s and ts and (now - ts).total_seconds() < max_age_s
    if fresh:
        return s

    snap = DataSnapshot().fetch()

    with _last_good_lock:
        if snap.acct:      _last_good["acct"]      = snap.acct
        else:              snap.acct      = dict(_last_good["acct"])
        if snap.positions or 'positions' not in [e.split(':')[0] for e in snap.errors]:
            if snap.positions: _last_good["positions"] = snap.positions
        else:
            snap.positions = list(_last_good["positions"])
        if snap.orders is not None and not any('orders' in e for e in snap.errors):
            _last_good["orders"] = snap.orders
        elif any('orders' in e for e in snap.errors):
            snap.orders = list(_last_good["orders"])
        if snap.clock:     _last_good["clock"]     = snap.clock
        else:              snap.clock     = dict(_last_good["clock"])
        if snap.fills or not any('fills' in e for e in snap.errors):
            if snap.fills: _last_good["fills"] = snap.fills
        else:
            snap.fills = list(_last_good["fills"])
        if snap.lookback_fills or not any('lookback_fills' in e for e in snap.errors):
            if snap.lookback_fills: _last_good["lookback_fills"] = snap.lookback_fills
        else:
            snap.lookback_fills = list(_last_good["lookback_fills"])
        if snap.period_pnl and snap.period_pnl.get("ok"):
            _last_good["period_pnl"] = snap.period_pnl
        elif not snap.period_pnl:
            snap.period_pnl = dict(_last_good["period_pnl"])

    with _snap_lock:
        _snap_data["snap"] = snap
        _snap_data["ts"] = now
    return snap

def calc_rsi(bars, p=14):
    if len(bars) < p + 1:
        return 50
    c = [b["c"] for b in bars]
    gains = [max(c[i] - c[i-1], 0) for i in range(1, len(c))]
    losses = [max(c[i-1] - c[i], 0) for i in range(1, len(c))]
    ag = sum(gains[-p:]) / p
    al = sum(losses[-p:]) / p
    return round(100 - (100 / (1 + ag / al)), 1) if al else 100

def calc_vwap(bars):
    if not bars:
        return None
    tv = sum(((b["h"] + b["l"] + b["c"]) / 3) * b["v"] for b in bars)
    v  = sum(b["v"] for b in bars)
    return round(tv / v, 2) if v else None

def calc_ema(bars, p=9):
    if len(bars) < p:
        return None
    c = [b["c"] for b in bars]
    k = 2 / (p + 1)
    e = sum(c[:p]) / p
    for x in c[p:]:
        e = x * k + e * (1 - k)
    return round(e, 2)

def _et_today_at(hour, minute):
    return datetime.now(ET).replace(hour=hour, minute=minute, second=0, microsecond=0)

def _iso_utc(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

def session_fraction():
    now = datetime.now(ET)
    open_et, close_et = _et_today_at(9, 30), _et_today_at(16, 0)
    if now <= open_et:  return 0.0
    if now >= close_et: return 1.0
    return (now - open_et).total_seconds() / (close_et - open_et).total_seconds()

def get_scan_minute_bars(sym, timeout=8):
    now = datetime.now(ET)
    open_et = _et_today_at(9, 30)
    params = {"timeframe": "1Min", "feed": "iex"}
    if now >= open_et:
        params["start"] = _iso_utc(open_et)
        params["limit"] = 500
    else:
        params["limit"] = 60
    return aGet(f"/v2/stocks/{sym}/bars", params, DATA_URL, timeout=timeout).get("bars", [])

def calc_opening_range(bars, minutes=ORB_MINUTES):
    if not bars:
        return None, None, False
    open_et = _et_today_at(9, 30)
    or_end = open_et + timedelta(minutes=minutes)
    hi = lo = None
    for b in bars:
        try:
            bt = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
        except Exception:
            continue
        if open_et <= bt < or_end:
            hi = b["h"] if hi is None else max(hi, b["h"])
            lo = b["l"] if lo is None else min(lo, b["l"])
    complete = datetime.now(ET) >= or_end
    return hi, lo, complete

_regime = {"ts": None, "ok": True, "chg": 0.0}

def market_regime():
    global _regime
    now = datetime.now(ET)
    if _regime["ts"] and (now - _regime["ts"]).seconds < 900:
        return _regime["ok"], _regime["chg"]
    try:
        bars = aGet("/v2/stocks/SPY/bars",
                    {"timeframe": "1Day", "limit": 20, "feed": "iex"},
                    DATA_URL).get("bars", [])
        snap = aGet("/v2/stocks/SPY/snapshot", base=DATA_URL)
        e9  = calc_ema(bars, 9)
        e20 = calc_ema(bars, 20)
        cur = snap.get("latestTrade", {}).get("p", 0)
        prc = snap.get("prevDailyBar", {}).get("c", cur)
        chg = (cur - prc) / prc if prc else 0
        ok = bool(e9 and e20 and cur > e9 and e9 > e20)
        _regime = {"ts": now, "ok": ok, "chg": chg}
        return ok, chg
    except Exception:
        return True, 0.0

def whale_tier(snap):
    d = snap.get("dailyBar", {})
    p = snap.get("prevDailyBar", {})
    tv = d.get("v", 0)
    pv = max(p.get("v", 1), 1)
    vr = tv / pv
    if vr >= 20: return 3, round(vr, 1)
    if vr >= 10: return 2, round(vr, 1)
    if vr >=  5: return 1, round(vr, 1)
    return 0, round(vr, 1)

def score_ticker(sym, snap, dbars, mbars, healthy=True):
    d = snap.get("dailyBar", {})
    p = snap.get("prevDailyBar", {})
    t = snap.get("latestTrade", {})
    cur = t.get("p", d.get("c", 0))
    pc  = p.get("c", 0)
    if not cur or not pc:
        return 0, "LONG"
    chg = (cur - pc) / pc
    vr  = d.get("v", 0) / max(p.get("v", 1), 1)
    direction = "LONG" if chg > 0 else "SHORT"
    score = 0
    ac = abs(chg)

    tier, _ = whale_tier(snap)
    score += {3: 40, 2: 32, 1: 24,
              0: 16 if vr >= 3 else 12 if vr >= 2 else 8 if vr >= 1.5 else 4 if vr >= 1.2 else 0
              }[tier]
    score += (30 if ac >= 0.10 else 24 if ac >= 0.07 else 18 if ac >= 0.04
              else 12 if ac >= 0.025 else 6 if ac >= 0.015 else 0)

    if score < 8:
        return 0, direction

    r = calc_rsi(dbars)
    if direction == "LONG":
        score += 15 if 45 <= r <= 65 else 10 if 35 <= r < 45 else 5 if r <= 72 else 0
    else:
        score += 15 if 28 <= r <= 50 else 10 if r < 28 else 5

    v = calc_vwap(mbars)
    if v and cur:
        dist = (cur - v) / v
        if direction == "LONG":
            score += 15 if dist > 0.01 else 8 if dist > -0.005 else 0
        else:
            score += 15 if dist < -0.01 else 8 if dist < 0.005 else 0
        if VWAP_FILTER and len(mbars) >= VWAP_MIN_BARS:
            if direction == "LONG" and cur < v:
                return 0, direction
            if direction == "SHORT" and cur > v:
                return 0, direction

    if ORB_ENABLED and mbars:
        or_hi, or_lo, complete = calc_opening_range(mbars, ORB_MINUTES)
        if complete and or_hi and or_lo:
            if direction == "LONG" and cur > or_hi:
                score += ORB_BOOST
            elif direction == "SHORT" and cur < or_lo:
                score += ORB_BOOST

    if GAP_ENABLED:
        with _lock:
            gc = _state.get("gap_candidates", {}).get(sym)
        if gc:
            pm_high = gc.get("pm_high")
            gap = gc.get("gap", 0)
            if direction == "LONG" and gap > 0 and pm_high and cur > pm_high:
                score += GAP_BOOST
            elif direction == "SHORT" and gap < 0:
                score += GAP_BOOST

    if sym in TIER1: score = min(100, score + 5)
    if not healthy:  score = int(score * 0.8)
    return min(score, 100), direction

_vol_cache = {}

def accurate_daily_vol(sym):
    if VOL_FEED == "iex":
        return None
    now = time.time()
    hit = _vol_cache.get(sym)
    if hit and now - hit[1] < 60:
        return hit[0]
    try:
        bars = aGet(f"/v2/stocks/{sym}/bars",
                    {"timeframe": "1Day", "limit": 2, "feed": VOL_FEED},
                    DATA_URL, timeout=8).get("bars", [])
        if len(bars) < 2:
            return None
        today_v = float(bars[-1].get("v", 0))
        prev_v  = float(bars[-2].get("v", 0))
        if today_v <= 0 or prev_v <= 0:
            return None
        _vol_cache[sym] = ((today_v, prev_v), now)
        return today_v, prev_v
    except Exception as e:
        log.debug(f"accurate_daily_vol {sym}: {e}")
        return None

def scan_one(sym, healthy, tsize):
    try:
        snap = aGet(f"/v2/stocks/{sym}/snapshot", base=DATA_URL, timeout=8)
        if not snap:
            return None
        d = snap.get("dailyBar", {})
        p = snap.get("prevDailyBar", {})
        t = snap.get("latestTrade", {})
        cur = t.get("p", d.get("c", 0))
        pc  = p.get("c", 0)
        price_floor = BEAR_MIN_PRICE if sym in BEAR_TICKERS else MIN_PRICE
        if cur < price_floor or not pc:
            return None
        if MAX_SPREAD_PCT > 0:
            q = snap.get("latestQuote", {})
            bid, ask = q.get("bp", 0), q.get("ap", 0)
            if bid > 0 and ask > 0:
                mid = (bid + ask) / 2
                spread = (ask - bid) / mid if mid > 0 else 0
                if spread > MAX_SPREAD_PCT:
                    log.debug(f"skip {sym}: spread {spread*100:.2f}% > "
                              f"cap {MAX_SPREAD_PCT*100:.2f}%")
                    return None
        if sym not in TICKERS:
            chg0 = (cur - pc) / pc
            if abs(chg0) > DISCOVERED_MAX_CHG:
                log.debug(f"skip {sym}: discovered move {chg0:+.0%} > cap {DISCOVERED_MAX_CHG:.0%}")
                return None
            if d.get("v", 0) * cur < DISCOVERED_MIN_DVOL:
                log.debug(f"skip {sym}: discovered $vol ${d.get('v',0)*cur:,.0f} < floor")
                return None
        dbars = aGet(f"/v2/stocks/{sym}/bars",
                     {"timeframe": "1Day", "limit": 20, "feed": "iex"},
                     DATA_URL, timeout=8).get("bars", [])
        mbars = get_scan_minute_bars(sym, timeout=8)
        sc, direction = score_ticker(sym, snap, dbars, mbars, healthy)
        if sc < MIN_SCORE:
            return None
        news = news_confirmation(sym)
        if NEWS_REQUIRED and not news["fresh"]:
            log.debug(f"skip {sym}: no fresh catalyst (news layer)")
            return None
        if news["fresh"]:
            sc = min(100, sc + NEWS_BOOST)
        av = accurate_daily_vol(sym)
        if av:
            tv, pv = av
            vr = tv / max(pv, 1)
        else:
            vr = d.get("v", 0) / max(p.get("v", 1), 1)
        chg = (cur - pc) / pc
        if MIN_RVOL > 0:
            rvol = vr / max(session_fraction(), 0.1)
            if rvol < MIN_RVOL:
                log.debug(f"skip {sym}: low RVOL {rvol:.2f} (vr {vr:.2f}, "
                          f"{session_fraction()*100:.0f}% of session elapsed)")
                return None
        tier, _ = whale_tier(snap)
        shares = int(tsize / cur)
        if shares < 1:
            log.debug(f"skip {sym}: ${cur:.2f}/share too expensive for ${tsize:.0f} size")
            return None
        sl = round(cur * (1 - STOP_PCT), 2)   if direction == "LONG" else round(cur * (1 + STOP_PCT), 2)
        tp = round(cur * (1 + TARGET_PCT), 2) if direction == "LONG" else round(cur * (1 - TARGET_PCT), 2)
        return {
            "sym":    sym,
            "price":  round(cur, 2),
            "chg":    round(chg * 100, 2),
            "vol":    round(vr, 1),
            "tier":   tier,
            "dir":    direction,
            "score":  sc,
            "shares": shares,
            "sl":     sl,
            "tp":     tp,
            "news":   news["count"],
            "news_age_min": news["age_min"],
        }
    except Exception as e:
        log.debug(f"scan {sym}: {e}")
        return None

def full_scan(label="SCAN"):
    update_health("last_scan_at", _now_et_str())
    with _lock:
        news = list(_state["news_tickers"])
        movers = list(_state.get("mover_tickers", []))
        gappers = list(_state.get("gap_candidates", {}).keys())
    all_tickers = sorted(set(TICKERS + news + movers + gappers))

    ok, _ = market_regime()
    regime_label = "✅ BULL" if ok else "⚠️ BEAR"
    effective_score = MIN_SCORE if ok else max(MIN_SCORE - 10, 40)

    push_alert(f"🔍 {label}: scanning {len(all_tickers)} tickers ({regime_label}, threshold {effective_score})", "info")

    tsize = min(TRADE_SIZE, MAX_POSITION)

    setups = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(scan_one, s, ok, tsize): s for s in all_tickers}
        for f in as_completed(futs):
            try:
                r = f.result(timeout=15)
                if r and r["score"] >= effective_score:
                    setups.append(r)
            except Exception:
                pass

    if not ok:
        for s in setups:
            if s["sym"] in BEAR_TICKERS:
                s["score"] = min(100, s["score"] + 15)

    setups.sort(key=lambda x: (x["tier"], x["score"]), reverse=True)
    top = setups[:5]

    if top:
        new_best = top[0]
        with _lock:
            current_best_score = _state.get("best_score_today", 0)
            if new_best["score"] > current_best_score:
                _state["best_score_today"] = new_best["score"]
                _state["best_setup_today"] = {
                    "sym":   new_best["sym"],
                    "score": new_best["score"],
                    "tier":  new_best["tier"],
                    "chg":   new_best["chg"],
                    "vol":   new_best["vol"],
                    "time":  _now_et_str(),
                }

    if top:
        lines = [f"✅ {label} | {regime_label} | {len(setups)} setups"]
        for i, s in enumerate(top[:3]):
            w = "🐋" if s["tier"] >= 2 else "🐳" if s["tier"] == 1 else "📊"
            bear = " 🔻" if s["sym"] in BEAR_TICKERS else ""
            lines.append(f"  #{i+1} {w} {s['sym']}{bear} score:{s['score']} {s['chg']:+.1f}% vol:{s['vol']}x")
        push_alert("\n".join(lines), "success")
    else:
        push_alert(f"📊 {label} | {regime_label} | No setups found", "info")

    return top, ok

def fetch_news():
    try:
        since = (datetime.now(ET) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = aGet("/v1beta1/news", params={"start": since, "limit": 50}, base=DATA_URL)
        articles = data.get("news", [])
        counts = {}
        for art in articles:
            for sym in art.get("symbols", []):
                if sym and len(sym) <= 5 and sym.isalpha():
                    counts[sym] = counts.get(sym, 0) + 1
        hot = [s for s, c in counts.items() if c >= 2 and s not in TICKERS][:10]
        if hot:
            push_alert(f"📰 News: {', '.join(hot)}", "info")
            with _lock:
                _state["news_tickers"] = hot
    except Exception as e:
        log.debug(f"news: {e}")

_news_cache = {}

def news_confirmation(sym):
    if not NEWS_LAYER_ENABLED:
        return {"fresh": False, "count": 0, "age_min": None, "headline": None}
    now = time.time()
    with _lock:
        cached = _news_cache.get(sym)
    if cached and now - cached[0] < NEWS_CACHE_MIN * 60:
        return cached[1]
    res = {"fresh": False, "count": 0, "age_min": None, "headline": None}
    try:
        since = (datetime.now(ET) - timedelta(hours=NEWS_MAX_AGE_HRS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = aGet("/v1beta1/news",
                    params={"symbols": sym, "start": since, "limit": 10, "sort": "desc"},
                    base=DATA_URL, timeout=6)
        arts = data.get("news", [])
        if arts:
            latest = arts[0]
            ts = latest.get("updated_at") or latest.get("created_at")
            age = None
            if ts:
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    age = (datetime.now(UTC) - dt).total_seconds() / 60.0
                except Exception:
                    age = None
            res = {"fresh": True, "count": len(arts),
                   "age_min": round(age, 1) if age is not None else None,
                   "headline": (latest.get("headline") or "")[:120]}
    except Exception as e:
        log.debug(f"news_confirmation {sym}: {e}")
    with _lock:
        _news_cache[sym] = (now, res)
    return res

def fetch_movers():
    if not MOVERS_ENABLED:
        return
    try:
        data = aGet("/v1beta1/screener/stocks/movers",
                    params={"top": max(MOVERS_COUNT, 10)}, base=DATA_URL)
        found = []
        for side in ("gainers", "losers"):
            for m in data.get(side, [])[:MOVERS_COUNT]:
                sym = (m.get("symbol") or "").upper()
                pct = abs(float(m.get("percent_change", 0) or 0))
                if (sym and len(sym) <= 5 and sym.isalpha()
                        and pct >= MOVERS_MIN_CHG and sym not in TICKERS):
                    found.append(sym)
        found = sorted(set(found))
        if found:
            shown = ", ".join(found[:12])
            extra = f" +{len(found) - 12} more" if len(found) > 12 else ""
            push_alert(f"📈 Movers: {shown}{extra}", "info")
            with _lock:
                _state["mover_tickers"] = found
    except Exception as e:
        log.debug(f"movers: {e}")

def premarket_scan():
    if not GAP_ENABLED:
        return
    fetch_movers()
    with _lock:
        extra = list(_state.get("mover_tickers", [])) + list(_state.get("news_tickers", []))
    universe = sorted(set(TICKERS + extra))
    pm_start = _iso_utc(_et_today_at(4, 0))

    def _check(sym):
        try:
            snap = aGet(f"/v2/stocks/{sym}/snapshot", base=DATA_URL, timeout=8)
            pc  = snap.get("prevDailyBar", {}).get("c", 0)
            cur = snap.get("latestTrade", {}).get("p", 0)
            if not pc or cur < (BEAR_MIN_PRICE if sym in BEAR_TICKERS else MIN_PRICE):
                return None
            gap = (cur - pc) / pc
            if abs(gap) < GAP_MIN_PCT:
                return None
            pmbars = aGet(f"/v2/stocks/{sym}/bars",
                          {"timeframe": "1Min", "start": pm_start, "limit": 400, "feed": "iex"},
                          DATA_URL, timeout=8).get("bars", [])
            pm_vol = sum(b["v"] for b in pmbars)
            if pm_vol < GAP_MIN_PMVOL:
                return None
            pm_high = max((b["h"] for b in pmbars), default=cur)
            return sym, {"gap": round(gap * 100, 2), "pm_vol": int(pm_vol),
                         "pm_high": round(pm_high, 2), "price": round(cur, 2)}
        except Exception as e:
            log.debug(f"premarket {sym}: {e}")
            return None

    candidates = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(_check, universe):
            if r:
                candidates[r[0]] = r[1]

    with _lock:
        _state["gap_candidates"] = candidates

    if candidates:
        top = sorted(candidates.items(), key=lambda kv: abs(kv[1]["gap"]), reverse=True)[:8]
        lines = ["🌅 Gap-and-go watch:"]
        for sym, c in top:
            arrow = "⬆️" if c["gap"] > 0 else "⬇️"
            lines.append(f"  {arrow} {sym} {c['gap']:+.1f}% pmVol:{c['pm_vol']:,} pmHigh:${c['pm_high']}")
        push_alert("\n".join(lines), "success")
    else:
        push_alert("🌅 Premarket scan — no qualifying gappers", "info")

_agent_cache = {}
def ask_agent(setup, swing=False):
    if not AGENT_URL:
        return {"decision": "APPROVE", "reasoning": "agent not configured",
                "confidence": 1.0, "skipped": True}
    key = (setup["sym"], setup["dir"], bool(swing))
    now = time.time()
    hit = _agent_cache.get(key)
    if hit and now - hit[1] < AGENT_CACHE_SEC:
        cached = dict(hit[0]); cached["cached"] = True
        return cached
    result = _ask_agent_uncached(setup, swing)
    _agent_cache[key] = (result, now)
    return result

def _ask_agent_uncached(setup, swing=False):
    if not AGENT_URL:
        return {"decision": "APPROVE", "reasoning": "agent not configured",
                "confidence": 1.0, "skipped": True}

    try:
        with _lock:
            lost = list(_state.get("lost_today", []))
            equity_str = _state.get("health", {}).get("last_equity", "0")
        try:
            equity = float(get_account().get("equity", 0))
        except Exception:
            equity = 0

        payload = {
            "symbol":      setup["sym"],
            "direction":   setup["dir"],
            "price":       setup["price"],
            "score":       setup["score"],
            "tier":        setup["tier"],
            "vol_ratio":   setup["vol"],
            "change_pct":  setup["chg"],
            "swing":       swing,
            "shares":      setup["shares"],
            "equity":      equity,
            "dt_used":     get_dt_used(),
            "dt_max":      MAX_DT,
            "lost_today":  lost,
        }

        headers = {"Content-Type": "application/json"}
        if AGENT_AUTH_TOKEN:
            headers["X-Auth-Token"] = AGENT_AUTH_TOKEN

        r = None
        for attempt in range(AGENT_RETRIES + 1):
            r = requests.post(f"{AGENT_URL}/evaluate", json=payload,
                              headers=headers, timeout=AGENT_TIMEOUT_S)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504) and attempt < AGENT_RETRIES:
                try:
                    wait = min((float(r.headers.get("Retry-After", 0)) or (0.5 * (2 ** attempt))), 3.0)
                except Exception:
                    wait = 0.5 * (2 ** attempt)
                log.warning(f"Agent {r.status_code} — retry {attempt+1}/{AGENT_RETRIES} in {wait:.1f}s")
                time.sleep(wait)
                continue
            break
        log.warning(f"Agent returned {r.status_code}: {r.text[:200]}")
        return {"decision": "APPROVE", "reasoning": f"agent {r.status_code} — fallback",
                "confidence": 0.5, "agent_failed": True}
    except Exception as e:
        log.warning(f"Agent call failed: {e}")
        return {"decision": "APPROVE", "reasoning": f"agent error: {e} — fallback",
                "confidence": 0.5, "agent_failed": True}

_shortable_cache = {}

def is_shortable(sym):
    if not SHORT_CHECK_ENABLED:
        return True
    hit = _shortable_cache.get(sym)
    if hit and (time.time() - hit[1]) < 600:
        return hit[0]
    try:
        a = aGet(f"/v2/assets/{sym}")
        ok = bool(a.get("tradable", True)) and bool(a.get("shortable")) and bool(a.get("easy_to_borrow"))
    except Exception as e:
        log.debug(f"shortable check {sym}: {e}")
        ok = False
    _shortable_cache[sym] = (ok, time.time())
    return ok

_acct_short = {"can": None, "ts": 0.0, "warned": 0.0}
def account_can_short():
    if not SHORT_CHECK_ENABLED:
        return True
    now = time.time()
    if _acct_short["can"] is not None and now - _acct_short["ts"] < 600:
        return _acct_short["can"]
    can = True
    try:
        a = get_account()
        shorting = bool(a.get("shorting_enabled", True))
        mult = float(a.get("multiplier", "1") or 1)
        blocked = bool(a.get("account_blocked")) or bool(a.get("trading_blocked"))
        can = shorting and mult >= 2 and not blocked
    except Exception as e:
        log.debug(f"account short check: {e}")
        can = True
    _acct_short["can"] = can
    _acct_short["ts"] = now
    return can

def place_order(setup, swing=False):
    side = "buy" if setup["dir"] == "LONG" else "sell"
    body = {
        "symbol": setup["sym"],
        "qty": str(setup["shares"]),
        "side": side,
        "type": "market",
        "time_in_force": "gtc" if swing else "day",
        "order_class": "bracket",
        "stop_loss":   {"stop_price":  str(setup["sl"])},
        "take_profit": {"limit_price": str(setup["tp"])},
    }
    try:
        aPost("/v2/orders", body)
        kind = "SWING" if swing else "DAY TRADE"
        push_alert(f"✅ {kind}: {side.upper()} {setup['shares']}x {setup['sym']} "
                   f"@ ~${setup['price']} | SL:${setup['sl']} TP:${setup['tp']}", "success")
        return True
    except Exception as e:
        push_alert(f"❌ Order failed {setup['sym']}: {e}", "error")
        return False

def get_dt_used():
    try:
        return int(get_account().get("daytrade_count", 0))
    except Exception:
        return 0

def is_paused():
    with _lock:
        return _state["paused"] or _state["e_stop"]

def is_occupied():
    try:
        return bool(get_positions() or get_orders_open())
    except Exception:
        return True

def reset_if_new_day():
    today = datetime.now(ET).strftime("%Y-%m-%d")
    reset_occurred = False
    old_date = None
    with _lock:
        if _state["tracker"]["date"] != today:
            old_date = _state["tracker"]["date"]
            _state["tracker"].update({
                "date": today,
                "wins_today": 0, "losses_today": 0, "day_trades": 0,
            })
            _state["traded_today"] = []
            _state["lost_today"]   = []
            _state["news_tickers"] = []
            _state["mover_tickers"] = []
            _state["gap_candidates"] = {}
            _state["bot_dt_count"] = 0
            _state["completed_trades_today"] = 0
            _state["sym_cooldown"] = {}
            _state["best_score_today"] = 0
            _state["best_setup_today"] = None
            _state["triggered_today"] = set()
            reset_occurred = True
    if reset_occurred:
        push_alert(f"🌅 New trading day ({old_date} → {today}) — daily state reset", "info")
        log.info(f"Day reset: {old_date} → {today}")

def execute(setups, label=""):
    reset_if_new_day()
    if is_paused():
        push_alert("⏸️ Paused — skipping execute", "warning")
        return
    if is_occupied():
        return
    if not setups:
        return

    with _lock:
        completed = _state.get("completed_trades_today", 0)
    if completed >= MAX_TRADES_PER_DAY:
        push_alert(
            f"🛑 Daily trade cap reached ({completed}/{MAX_TRADES_PER_DAY}) — "
            f"no new trades today. Bot will resume tomorrow.", "warning")
        return

    alpaca_dt = get_dt_used()
    with _lock:
        bot_dt = _state.get("bot_dt_count", 0)
    dt_used = max(alpaca_dt, bot_dt)
    pdt_maxed = dt_used >= MAX_DT
    if pdt_maxed:
        push_alert(
            f"⚠️ Day trade limit ({dt_used}/{MAX_DT}) — swing only", "warning")

    with _lock:
        lost = list(_state.get("lost_today", []))
        cooldowns = dict(_state.get("sym_cooldown", {}))
    now = datetime.now(ET)

    def is_blocked(sym):
        if sym in lost:
            return f"already lost on {sym} today"
        cd = cooldowns.get(sym)
        if cd:
            try:
                cd_dt = datetime.fromisoformat(cd)
                if cd_dt > now:
                    mins_left = int((cd_dt - now).total_seconds() / 60) + 1
                    return f"{sym} on cooldown ({mins_left} min left)"
            except Exception:
                pass
        return None

    filtered = []
    for s in setups:
        reason = is_blocked(s["sym"])
        if reason:
            log.debug(f"Skip {s['sym']}: {reason}")
            continue
        if s["dir"] == "SHORT":
            if not account_can_short():
                if time.time() - _acct_short["warned"] > 600:
                    push_alert("⚠️ Account has shorting disabled (cash/no-margin) — "
                               "skipping all SHORT setups. Enable margin on Alpaca, "
                               "or trade inverse ETFs (SQQQ/SPXS) long instead.", "warning")
                    _acct_short["warned"] = time.time()
                continue
            if not is_shortable(s["sym"]):
                push_alert(f"⏭️ Skip short {s['sym']}: not shortable / no borrow available", "info")
                continue
        filtered.append(s)

    if not filtered:
        blocked_msgs = []
        for s in setups[:3]:
            reason = is_blocked(s["sym"])
            if reason:
                blocked_msgs.append(reason)
        if blocked_msgs:
            push_alert(f"📊 All setups blocked: {'; '.join(blocked_msgs[:2])}", "info")
        return

    best = filtered[0]

    try:
        cash = float(get_account().get("cash", 0))
        position_value = best["shares"] * best["price"]
        max_position = cash * (MAX_RISK_PCT * 100)
        if position_value > max_position:
            new_shares = int(max_position / best["price"])
            if new_shares < 1:
                push_alert(f"⚠️ {best['sym']} skipped: even 1 share (${best['price']:.0f}) exceeds risk cap (${max_position:.0f})", "warning")
                return
            if new_shares < best["shares"]:
                push_alert(f"⚠️ Sizing capped {best['sym']}: {best['shares']}→{new_shares}", "warning")
                best["shares"] = new_shares
    except Exception:
        pass

    tag = "🚨EXTREME" if best["tier"] == 3 else "🐋WHALE" if best["tier"] >= 1 else "📊"
    push_alert(f"{tag} {best['dir']} {best['sym']} | Score:{best['score']} | {best['chg']:+.1f}% | Vol:{best['vol']}x", "info")

    try:
        regime_ok, _ = market_regime()
    except Exception:
        regime_ok = True
    effective_threshold = MIN_SCORE if regime_ok else max(MIN_SCORE - 10, 40)

    will_swing = False
    will_trade = False
    if best["tier"] == 3:
        will_swing = pdt_maxed
        will_trade = True
    elif pdt_maxed and best["score"] >= effective_threshold:
        will_swing = True
        will_trade = True
    elif pdt_maxed:
        push_alert(f"🛑 DT maxed + score {best['score']} below threshold {effective_threshold} — skip {best['sym']}", "warning")
        return
    elif best["score"] >= MIN_SCORE:
        will_swing = False
        will_trade = True
    else:
        return

    if not will_trade:
        return

    if AGENT_URL:
        agent_resp = ask_agent(best, swing=will_swing)
        a_decision = agent_resp.get("decision", "APPROVE")
        a_reason   = (agent_resp.get("reasoning") or "")[:120]
        a_conf     = agent_resp.get("confidence", 1.0)
        a_shadow   = agent_resp.get("shadow", False)
        a_shadow_d = agent_resp.get("shadow_decision")

        if a_shadow and a_shadow_d:
            push_alert(f"👻 Agent (shadow): would {a_shadow_d} {best['sym']} | {a_reason}", "info")
        else:
            emoji = "🟢" if a_decision == "APPROVE" else "🔴"
            push_alert(f"{emoji} Agent {a_decision} {best['sym']} (conf {a_conf:.2f}) | {a_reason}", "info")

        if a_decision == "VETO":
            push_alert(f"🛑 Trade vetoed by agent: {best['sym']}", "warning")
            return

    if best["tier"] == 3:
        ok = place_order(best, swing=will_swing)
    elif will_swing:
        push_alert(f"🔄 Day trade limit — SWING: {best['sym']} (score {best['score']} ≥ {effective_threshold})", "warning")
        ok = place_order(best, swing=True)
    else:
        ok = place_order(best, swing=False)

    if ok:
        with _lock:
            if best["sym"] not in _state["traded_today"]:
                _state["traded_today"].append(best["sym"])
            _state["sym_cooldown"][best["sym"]] = (
                datetime.now(ET) + timedelta(minutes=COOLDOWN_MIN)
            ).isoformat()

def monitor():
    try:
        for p in get_positions():
            pl  = float(p.get("unrealized_pl", 0))
            pct = float(p.get("unrealized_plpc", 0)) * 100
            push_alert(f"📊 {p.get('symbol')} x{p.get('qty')} | P&L: ${pl:+.2f} ({pct:+.1f}%)", "info")
    except Exception:
        pass

def protect_positions():
    try:
        positions = get_positions()
        if not positions:
            return

        try:
            open_orders = get_orders_open()
        except Exception as e:
            push_alert(f"⚠️ Protection check: can't fetch orders ({e})", "warning")
            return

        by_sym = {}
        for o in open_orders:
            sym = o.get("symbol", "")
            otype = (o.get("order_type") or o.get("type") or "").lower()
            oclass = (o.get("order_class") or "").lower()
            oside = o.get("side", "")
            oid = o.get("id", "")
            if not sym or not oid:
                continue
            d = by_sym.setdefault(sym, {"stops": [], "limits": [], "all_close": [], "oco": False})
            d["all_close"].append({"id": oid, "side": oside, "type": otype})
            if oclass in ("oco", "bracket", "oto"):
                d["oco"] = True
            if "stop" in otype:
                d["stops"].append(oid)
            elif "limit" in otype:
                d["limits"].append(oid)

        for p in positions:
            sym = p.get("symbol", "")
            try:
                qty = abs(int(float(p.get("qty", 0))))
                entry = float(p.get("avg_entry_price", 0))
                side = p.get("side", "long")
            except (TypeError, ValueError):
                continue
            if qty == 0 or entry <= 0:
                continue

            existing = by_sym.get(sym, {"stops": [], "limits": [], "all_close": [], "oco": False})
            has_stop = len(existing["stops"]) > 0
            has_limit = len(existing["limits"]) > 0
            has_oco  = existing.get("oco", False)

            if has_oco or (has_stop and has_limit):
                continue

            close_side = "sell" if side == "long" else "buy"

            if side == "long":
                sl_price = round(entry * (1 - STOP_PCT), 2)
                tp_price = round(entry * (1 + TARGET_PCT), 2)
            else:
                sl_price = round(entry * (1 + STOP_PCT), 2)
                tp_price = round(entry * (1 - TARGET_PCT), 2)

            cancelled_ids = []
            for o in existing["all_close"]:
                if o["side"] != close_side:
                    continue
                try:
                    aDel(f"/v2/orders/{o['id']}")
                    cancelled_ids.append(o["id"])
                except Exception as e:
                    log.debug(f"cancel {o['id']}: {e}")

            if cancelled_ids:
                push_alert(f"🧹 Cancelled {len(cancelled_ids)} solo close order(s) for {sym} to place OCO", "info")
                for _ in range(10):
                    time.sleep(0.5)
                    try:
                        still_open = {oo.get("id") for oo in get_orders_open()}
                    except Exception:
                        break
                    if not (set(cancelled_ids) & still_open):
                        break

            placed = False
            for attempt in range(3):
                try:
                    aPost("/v2/orders", {
                        "symbol": sym,
                        "qty": str(qty),
                        "side": close_side,
                        "type": "limit",
                        "limit_price": str(tp_price),
                        "time_in_force": "gtc",
                        "order_class": "oco",
                        "stop_loss":   {"stop_price":  str(sl_price)},
                        "take_profit": {"limit_price": str(tp_price)},
                    })
                    push_alert(
                        f"🛡️ OCO protection for {sym}: {close_side.upper()} {qty} | "
                        f"SL ${sl_price} / TP ${tp_price} GTC", "warning")
                    placed = True
                    break
                except Exception as e:
                    if "403" in str(e) and attempt < 2:
                        time.sleep(1.0 * (attempt + 1))
                        continue
                    push_alert(f"🚨 Failed to place OCO for {sym} — position UNPROTECTED, "
                               f"will retry next cycle: {e}", "error")
                    break

    except Exception as e:
        push_alert(f"⚠️ protect_positions error: {e}", "error")

def eod():
    monitor()
    push_alert("🔔 EOD report", "info")

def job(label):
    if is_paused():
        return
    setups, ok = full_scan(label)
    now = datetime.now(ET)
    if now.hour == 9 and now.minute < 45 and label not in ("PREMARKET 8AM", "OPENING RANGE"):
        high = [s for s in setups if s["tier"] >= 2 or s["score"] >= 80]
        if high:
            push_alert(f"🚀 High conviction at open — {high[0]['sym']} score:{high[0]['score']}", "warning")
            execute(high, label)
        else:
            push_alert("⏳ Opening window — waiting score 80+ or whale tier 2+", "info")
        return
    execute(setups, label)

def bot_loop():
    restore_alerts()
    push_alert(f"🐋 Whale Bot v6 online — {'🔴 LIVE' if LIVE_MODE else '📝 PAPER'}", "success")
    push_alert(f"Watching {len(TICKERS)} tickers | ${TRADE_SIZE}/trade | "
               f"min ${MIN_PRICE} | min score {MIN_SCORE}", "info")

    time.sleep(8)

    try:
        if get_positions():
            push_alert("🛡️ Startup: checking position protection…", "info")
            protect_positions()
    except Exception as e:
        push_alert(f"⚠️ Startup protection check err: {e}", "warning")

    try:
        if get_clock().get("is_open"):
            push_alert("🔄 Market open — startup scan", "info")
            job("STARTUP")
        else:
            push_alert("💤 Market closed — waiting", "info")
    except Exception as e:
        push_alert(f"⚠️ Startup err: {e}", "warning")

    while True:
        try:
            update_health("last_loop_at", _now_et_str())
            reset_if_new_day()
            now = datetime.now(ET)
            h, m = now.hour, now.minute
            key = f"{h}:{m:02d}"

            schedule = {
                "7:30":  premarket_scan,
                "8:00":  lambda: (premarket_scan(), job("PREMARKET 8AM")),
                "9:15":  premarket_scan,
                "9:30":  lambda: (protect_positions(), job("OPENING RANGE")),
                "9:45":  lambda: job("ORB BREAKOUT"),
                "10:00": lambda: job("MOMENTUM 10AM"),
                "12:00": lambda: job("MIDDAY"),
                "15:00": lambda: job("POWER HOUR"),
                "15:55": eod,
            }

            with _lock:
                triggered = _state["triggered_today"]

            def mark(k):
                with _lock:
                    _state["triggered_today"].add(k)

            if key in schedule and key not in triggered:
                schedule[key]()
                mark(key)
            elif 8 <= h < 16 and m in (0, 30) and f"news_{key}" not in triggered:
                fetch_news()
                fetch_movers()
                mark(f"news_{key}")
            elif 9 <= h < 16 and m % 10 == 0 and f"protect_{key}" not in triggered:
                try:
                    protect_positions()
                except Exception as e:
                    push_alert(f"⚠️ Protection sweep err: {e}", "warning")
                mark(f"protect_{key}")
            elif 9 <= h < 16 and m % 2 == 0 and key not in triggered:
                try:
                    monitor()
                    if not is_occupied() and not is_paused():
                        setups, ok = full_scan("CONTINUOUS")
                        whales = [s for s in setups if s["tier"] >= 2]
                        if whales:
                            execute(whales, "WHALE ALERT")
                except Exception as e:
                    push_alert(f"⚠️ Scan err: {e}", "warning")
                mark(key)

            if h == 0 and m == 1 and "midnight" not in triggered:
                with _lock:
                    _state["triggered_today"].clear()
                    _state["triggered_today"].add("midnight")

            time.sleep(20)
        except Exception as e:
            push_alert(f"⚠️ Loop err: {e}\n{traceback.format_exc()[:200]}", "error")
            time.sleep(60)

def keepalive_loop():
    time.sleep(120)
    while True:
        try:
            if KEEPALIVE:
                url = KEEPALIVE.rstrip("/") + "/api/health"
                r = requests.get(url, timeout=20)
                push_alert(f"💓 Keepalive ping ({r.status_code})", "info")
            else:
                push_alert("💓 Bot heartbeat (no RENDER_EXTERNAL_URL set)", "info")
        except Exception as e:
            host = (KEEPALIVE or "")[:60]
            push_alert(f"⚠️ Keepalive failed ({type(e).__name__}) to {host} — non-critical", "warning")
        time.sleep(270)

def snapshot_loop():
    time.sleep(2)
    while True:
        try:
            snap = DataSnapshot().fetch()
            with _snap_lock:
                _snap_data["snap"] = snap
                _snap_data["ts"] = datetime.now(ET)
            try:
                update_closed_trade_state(snap.fills or [], snap.lookback_fills or [])
            except Exception as e:
                log.debug(f"snap loop state update: {e}")
        except Exception as e:
            log.debug(f"snap loop: {e}")
        time.sleep(4)

app = Flask(__name__)

def authed():
    tok = request.headers.get("X-Token", "") or request.args.get("token", "")
    return tok == DASH_PASS

def agent_authed():
    if authed():
        return True
    tok = request.headers.get("X-Auth-Token", "")
    return bool(AGENT_AUTH_TOKEN) and tok == AGENT_AUTH_TOKEN

@app.route("/api/data")
def api_data():
    snap = get_snapshot()

    acct = snap.acct or {}
    positions = snap.positions or []
    orders = snap.orders or []
    clock = snap.clock or {}
    fills = snap.fills or []
    lookback_fills = snap.lookback_fills or []

    trades, wins, losses, realized = compute_trades_and_pnl(fills, lookback_fills)

    update_closed_trade_state(fills, lookback_fills)

    try:
        equity = float(acct.get("equity", 0))
    except (ValueError, TypeError):
        equity = 0.0
    try:
        last_eq = float(acct.get("last_equity", equity))
    except (ValueError, TypeError):
        last_eq = equity
    try:
        dt_used = int(acct.get("daytrade_count", 0))
    except (ValueError, TypeError):
        dt_used = 0
    pdt_flag = bool(acct.get("pattern_day_trader", False))

    daily_pnl = round(equity - last_eq, 2)

    unrealized = 0.0
    for p in positions:
        try:
            unrealized += float(p.get("unrealized_pl", 0))
        except (ValueError, TypeError):
            pass
    unrealized = round(unrealized, 2)

    with _lock:
        tw = _state["tracker"].get("total_wins", 0) + wins
        tt = _state["tracker"].get("total_trades", 0) + wins + losses
        alerts = list(_state["alerts"])
        traded = list(_state["traded_today"])
        lost   = list(_state["lost_today"])
        bot_dt_count = _state.get("bot_dt_count", 0)
        completed_today = _state.get("completed_trades_today", 0)
        cooldowns = dict(_state.get("sym_cooldown", {}))
        best_score_today = _state.get("best_score_today", 0)
        best_setup_today = _state.get("best_setup_today")
        health = dict(_state["health"])
    win_rate = round(tw / tt * 100, 1) if tt else 0.0

    pp = dict(snap.period_pnl) if snap.period_pnl else {}
    pp["daily"] = daily_pnl
    pp["daily_pct"] = round(daily_pnl / last_eq * 100, 2) if last_eq else 0.0

    return jsonify({
        "ok":             True,
        "live_mode":      LIVE_MODE,
        "equity":         equity,
        "last_equity":    last_eq,
        "pnl":            daily_pnl,
        "daily_pnl":      daily_pnl,
        "realized_pnl":   realized,
        "unrealized_pnl": unrealized,
        "wins":           wins,
        "losses":         losses,
        "win_rate":       win_rate,
        "dt_used":        dt_used,
        "dt_max":         MAX_DT,
        "pdt_flagged":    pdt_flag,
        "trade_size":     TRADE_SIZE,
        "max_loss":       MAX_LOSS,
        "paused":         _state["paused"],
        "e_stop":         _state["e_stop"],
        "market_open":    clock.get("is_open", False),
        "next_open":      clock.get("next_open", ""),
        "positions":      positions,
        "orders":         orders,
        "alerts":         alerts[-50:],
        "trades":         trades,
        "fills_count":    len(fills),
        "traded_today":   traded,
        "lost_today":     lost,
        "goal":           DAILY_GOAL,
        "period_pnl":     pp,
        "bot_dt_count":   bot_dt_count,
        "completed_today": completed_today,
        "max_trades_per_day": MAX_TRADES_PER_DAY,
        "sym_cooldown":   cooldowns,
        "cooldown_min":   COOLDOWN_MIN,
        "best_score_today": best_score_today,
        "best_setup_today": best_setup_today,
        "snapshot_errors": snap.errors,
        "health":         health,
        "snapshot_age_s": ((datetime.now(ET) - _snap_data["ts"]).total_seconds()
                           if _snap_data["ts"] else None),
    })

@app.route("/api/logs")
def api_logs():
    try:
        if not os.path.exists(LOG_FILE):
            return jsonify({"lines": ["No log file yet."]})
        with open(LOG_FILE) as f:
            lines = [l.rstrip() for l in f.readlines()[-200:] if l.strip()]
        return jsonify({"lines": lines})
    except Exception as e:
        return jsonify({"lines": [f"Log err: {e}"]})

@app.route("/api/debug")
def api_debug():
    try:
        snap = get_snapshot()
        with _lock:
            health = dict(_state["health"])
            movers = list(_state.get("mover_tickers", []))
            gaps = dict(_state.get("gap_candidates", {}))
        return jsonify({
            "live_mode": LIVE_MODE,
            "base_url": BASE_URL,
            "tickers_count": len(TICKERS),
            "alpaca_health": health,
            "discovery": {
                "movers_enabled": MOVERS_ENABLED,
                "movers_count": len(movers),
                "movers": movers,
                "gap_enabled": GAP_ENABLED,
                "gap_candidates": gaps,
            },
            "snapshot_errors": snap.errors,
            "snapshot_age_s": ((datetime.now(ET) - _snap_data["ts"]).total_seconds()
                              if _snap_data["ts"] else None),
            "fills_count": len(snap.fills),
            "positions_count": len(snap.positions),
            "orders_count": len(snap.orders),
            "acct_keys": list(snap.acct.keys()) if snap.acct else [],
            "equity": snap.acct.get("equity") if snap.acct else None,
            "config": {
                "TRADE_SIZE": TRADE_SIZE,
                "MIN_SCORE": MIN_SCORE,
                "MIN_PRICE": MIN_PRICE,
                "BEAR_MIN_PRICE": BEAR_MIN_PRICE,
                "MAX_DT": MAX_DT,
                "STOP_PCT": STOP_PCT,
                "TARGET_PCT": TARGET_PCT,
                "VWAP_FILTER": VWAP_FILTER,
                "ORB_ENABLED": ORB_ENABLED,
                "ORB_MINUTES": ORB_MINUTES,
                "GAP_ENABLED": GAP_ENABLED,
                "GAP_MIN_PCT": GAP_MIN_PCT,
                "MOVERS_MIN_CHG": MOVERS_MIN_CHG,
                "DISCOVERED_MAX_CHG": DISCOVERED_MAX_CHG,
                "DISCOVERED_MIN_DVOL": DISCOVERED_MIN_DVOL,
                "MAX_SPREAD_PCT": MAX_SPREAD_PCT,
                "VOL_FEED": VOL_FEED,
                "NEWS_LAYER_ENABLED": NEWS_LAYER_ENABLED,
                "NEWS_REQUIRED": NEWS_REQUIRED,
                "NEWS_BOOST": NEWS_BOOST,
                "NEWS_MAX_AGE_HRS": NEWS_MAX_AGE_HRS,
            }
        })
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()})

@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "live_mode": LIVE_MODE})

@app.route("/api/pause", methods=["POST"])
def api_pause():
    if not authed():
        return jsonify({"error": "Wrong password"}), 401
    with _lock:
        _state["paused"] = True
    push_alert("⏸️ Bot PAUSED", "warning")
    return jsonify({"ok": True})

@app.route("/api/resume", methods=["POST"])
def api_resume():
    if not authed():
        return jsonify({"error": "Wrong password"}), 401
    with _lock:
        _state["paused"] = False
        _state["e_stop"] = False
    push_alert("▶️ Bot RESUMED", "success")
    return jsonify({"ok": True})

@app.route("/api/estop", methods=["POST"])
def api_estop():
    if not authed():
        return jsonify({"error": "Wrong password"}), 401
    with _lock:
        _state["paused"] = True
        _state["e_stop"] = True
    try:
        aDel("/v2/orders")
    except Exception:
        pass
    try:
        requests.delete(BASE_URL + "/v2/positions", headers=HDR, timeout=10)
    except Exception:
        pass
    push_alert("🛑 EMERGENCY STOP", "error")
    return jsonify({"ok": True})

@app.route("/api/protect", methods=["POST"])
def api_protect():
    if not agent_authed():
        return jsonify({"error": "Wrong password"}), 401
    try:
        protect_positions()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/health", methods=["GET"])
def api_admin_health():
    if not agent_authed():
        return jsonify({"error": "unauthorized"}), 401
    try:
        snap = get_snapshot()
        now_et = datetime.now(ET)
        with _lock:
            tracker_date = _state["tracker"]["date"]
            triggered = list(_state.get("triggered_today", set()))
            paused = _state.get("paused", False)
            e_stop = _state.get("e_stop", False)
            health = dict(_state.get("health", {}))
            bot_dt_count = _state.get("bot_dt_count", 0)
            completed_today = _state.get("completed_trades_today", 0)
            best_score = _state.get("best_score_today", 0)
            lost_today = list(_state.get("lost_today", []))

        positions = snap.positions or []
        orders    = snap.orders or []

        orders_by_sym = {}
        for o in orders:
            s = o.get("symbol", "")
            otype = (o.get("order_type") or o.get("type") or "").lower()
            orders_by_sym.setdefault(s, {"stop": False, "limit": False, "ids": []})
            orders_by_sym[s]["ids"].append(o.get("id"))
            if "stop" in otype:  orders_by_sym[s]["stop"] = True
            if "limit" in otype: orders_by_sym[s]["limit"] = True

        position_syms = {p.get("symbol", "") for p in positions}
        unprotected = []
        for p in positions:
            s = p.get("symbol", "")
            prot = orders_by_sym.get(s, {"stop": False, "limit": False})
            if not (prot["stop"] and prot["limit"]):
                unprotected.append(s)

        orphans = []
        for s, info in orders_by_sym.items():
            if s not in position_syms:
                orphans.extend(info["ids"])

        last_loop = health.get("last_loop_at", "")
        last_scan = health.get("last_scan_at", "")
        try:
            last_loop_dt = datetime.fromisoformat(last_loop) if last_loop else None
            loop_age_s = (now_et - last_loop_dt).total_seconds() if last_loop_dt else None
        except Exception:
            loop_age_s = None

        return jsonify({
            "ok":               True,
            "now_et":           now_et.strftime("%Y-%m-%d %H:%M:%S"),
            "today_et":         now_et.strftime("%Y-%m-%d"),
            "tracker_date":     tracker_date,
            "rollover_stuck":   tracker_date != now_et.strftime("%Y-%m-%d"),
            "loop_age_s":       loop_age_s,
            "last_loop_at":     last_loop,
            "last_scan_at":     last_scan,
            "errors":           health.get("alpaca_err_count", 0),
            "calls":            health.get("alpaca_call_count", 0),
            "paused":           paused,
            "e_stop":           e_stop,
            "position_count":   len(positions),
            "order_count":      len(orders),
            "unprotected_syms": unprotected,
            "orphan_order_ids": orphans,
            "triggered_count":  len(triggered),
            "bot_dt_count":     bot_dt_count,
            "completed_today":  completed_today,
            "best_score_today": best_score,
            "lost_today":       lost_today,
            "market_open":      (snap.clock or {}).get("is_open", False),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/force_reset", methods=["POST"])
def api_admin_force_reset():
    if not agent_authed():
        return jsonify({"error": "unauthorized"}), 401
    try:
        with _lock:
            old_date = _state["tracker"]["date"]
            _state["tracker"]["date"] = "1970-01-01"
        reset_if_new_day()
        with _lock:
            new_date = _state["tracker"]["date"]
        push_alert(f"🔧 Admin force-reset (was '{old_date}' → now '{new_date}')", "warning")
        return jsonify({"ok": True, "old_date": old_date, "new_date": new_date})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/cancel_orphans", methods=["POST"])
def api_admin_cancel_orphans():
    if not agent_authed():
        return jsonify({"error": "unauthorized"}), 401
    try:
        positions = get_positions()
        orders = get_orders_open()
        position_syms = {p.get("symbol") for p in positions if p.get("symbol")}
        cancelled = []
        for o in orders:
            sym = o.get("symbol", "")
            oid = o.get("id", "")
            if sym and sym not in position_syms and oid:
                try:
                    aDel(f"/v2/orders/{oid}")
                    cancelled.append({"symbol": sym, "id": oid})
                except Exception as e:
                    log.warning(f"Failed to cancel orphan {oid}: {e}")
        if cancelled:
            push_alert(f"🧹 Cancelled {len(cancelled)} orphan order(s): {[c['symbol'] for c in cancelled]}", "warning")
        return jsonify({"ok": True, "cancelled": cancelled})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/close/<sym>", methods=["POST"])
def api_close(sym):
    if not authed():
        return jsonify({"error": "Wrong password"}), 401
    try:
        requests.delete(f"{BASE_URL}/v2/positions/{sym}", headers=HDR, timeout=10)
        push_alert(f"🔴 Closed: {sym}", "warning")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/")
def index():
    return DASHBOARD_HTML

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Whale Bot</title>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=DM+Sans:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#07090d;--card:#0d1219;--bdr:#182030;--green:#00e5b0;--blue:#3d9eff;
--red:#ff4060;--yellow:#ffb020;--text:#b8c4d0;--dim:#4a5568;
--mono:'Share Tech Mono',monospace;--sans:'DM Sans',sans-serif}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px}
.hdr{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;
background:var(--card);border-bottom:1px solid var(--bdr);position:sticky;top:0;z-index:10;flex-wrap:wrap;gap:8px}
.logo{font-family:var(--mono);font-size:16px;color:var(--green);letter-spacing:2px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.logo .v{color:var(--dim);font-size:11px}
.mode{font-family:var(--mono);font-size:10px;padding:3px 9px;border-radius:12px;font-weight:700;letter-spacing:1px}
.mode.paper{background:rgba(61,158,255,.12);color:var(--blue);border:1px solid var(--blue)}
.mode.live{background:rgba(255,64,96,.18);color:var(--red);border:1px solid var(--red)}
.hdr-r{display:flex;align-items:center;gap:10px}
.clk{font-family:var(--mono);font-size:12px;color:var(--dim)}
.mbadge{font-family:var(--mono);font-size:10px;padding:3px 9px;border-radius:20px;font-weight:700;letter-spacing:1px}
.mbadge.open{background:rgba(0,229,176,.12);color:var(--green);border:1px solid rgba(0,229,176,.3)}
.mbadge.closed{background:rgba(255,255,255,.04);color:var(--dim);border:1px solid var(--bdr)}
.health{padding:6px 16px;background:var(--card);border-bottom:1px solid var(--bdr);
font-family:var(--mono);font-size:10px;color:var(--dim);display:flex;gap:14px;flex-wrap:wrap}
.health span b{color:var(--text)}
.health .ok{color:var(--green)}
.health .err{color:var(--red)}
.wrap{padding:12px;max-width:1100px;margin:0 auto}
.g4{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:12px}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px}
.g21{display:grid;grid-template-columns:2fr 1fr;gap:10px;margin-bottom:12px}
@media(max-width:680px){.g4{grid-template-columns:1fr 1fr}.g2,.g21{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--bdr);border-radius:10px;padding:14px}
.ct{font-size:9px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--dim);margin-bottom:8px}
.sv{font-family:var(--mono);font-size:24px;color:#fff}
.ss{font-size:11px;color:var(--dim);margin-top:4px}
.g{color:var(--green)}.r{color:var(--red)}.w{color:var(--yellow)}
.gbar{margin-top:8px}
.gtrack{height:3px;background:var(--bdr);border-radius:2px;overflow:hidden}
.gfill{height:100%;background:linear-gradient(90deg,var(--green),var(--blue));border-radius:2px;transition:width .5s}
.glbls{display:flex;justify-content:space-between;font-size:9px;color:var(--dim);margin-top:3px}
.dot{width:9px;height:9px;border-radius:50%;flex-shrink:0}
.run{background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 2s infinite}
.psd{background:var(--yellow)}.stp{background:var(--red)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.srow{display:flex;align-items:center;gap:8px;margin-bottom:12px}
.stxt{font-family:var(--mono);font-size:13px}
.pw-row{display:flex;gap:8px;margin-bottom:10px}
.pw-in{flex:1;background:var(--bg);border:1px solid var(--bdr);color:var(--text);
font-family:var(--mono);font-size:14px;padding:7px 10px;border-radius:5px;outline:none;letter-spacing:3px}
.pw-in:focus{border-color:var(--green)}
.btns{display:flex;flex-direction:column;gap:7px}
.btn{padding:9px;border-radius:6px;border:1px solid;font-family:var(--mono);
font-size:11px;letter-spacing:1px;cursor:pointer;width:100%;transition:all .15s;font-weight:700}
.btn:hover{filter:brightness(1.2)}
.btn:disabled{opacity:.3;cursor:not-allowed}
.bp{background:rgba(255,176,32,.08);color:var(--yellow);border-color:rgba(255,176,32,.3)}
.br{background:rgba(0,229,176,.08);color:var(--green);border-color:rgba(0,229,176,.3)}
.bs{background:rgba(255,64,96,.12);color:var(--red);border-color:rgba(255,64,96,.3)}
table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:11px}
th{text-align:left;padding:5px 8px;font-size:9px;letter-spacing:1.5px;text-transform:uppercase;color:var(--dim);border-bottom:1px solid var(--bdr)}
td{padding:6px 8px;border-bottom:1px solid rgba(24,32,48,.5);color:var(--text)}
.empty{text-align:center;color:var(--dim);padding:14px;font-size:11px}
.logbox{height:280px;overflow-y:auto;display:flex;flex-direction:column-reverse;gap:2px}
.logbox::-webkit-scrollbar{width:3px}
.logbox::-webkit-scrollbar-thumb{background:var(--bdr)}
.li{display:flex;gap:7px;padding:4px 6px;border-radius:3px;font-size:10px;font-family:var(--mono);border-left:2px solid transparent}
.li .lt{color:var(--dim);flex-shrink:0;min-width:42px}
.li .lm{color:var(--text);flex:1;word-break:break-word;white-space:pre-wrap}
.li.info{border-left-color:var(--blue);background:rgba(61,158,255,.03)}
.li.success{border-left-color:var(--green);background:rgba(0,229,176,.04)}
.li.warning{border-left-color:var(--yellow);background:rgba(255,176,32,.04)}
.li.error{border-left-color:var(--red);background:rgba(255,64,96,.05)}
.cbtn{font-family:var(--mono);font-size:9px;padding:3px 7px;border-radius:3px;cursor:pointer;
border:1px solid rgba(255,64,96,.3);background:rgba(255,64,96,.08);color:var(--red)}
.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:99;align-items:center;justify-content:center}
.modal.show{display:flex}
.mbox{background:var(--card);border:1px solid var(--red);border-radius:12px;padding:24px;max-width:300px;width:90%;text-align:center}
.mbox h3{color:var(--red);font-family:var(--mono);margin-bottom:8px}
.mbox p{color:var(--dim);font-size:12px;margin-bottom:16px;line-height:1.6}
.mbtns{display:flex;gap:8px}
.note{font-size:10px;color:var(--dim);margin-top:10px;line-height:1.7}
.trade-row{display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px solid var(--bdr);font-family:var(--mono);font-size:11px;gap:6px}
.trade-row:last-child{border-bottom:none}
.bn{display:inline-block;font-size:9px;padding:2px 6px;border-radius:4px;font-weight:700}
.bn.b{background:rgba(0,229,176,.12);color:var(--green)}
.bn.s{background:rgba(255,64,96,.12);color:var(--red)}
.period-card{margin-bottom:12px}
.period-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
@media(max-width:560px){.period-grid{grid-template-columns:repeat(2,1fr);gap:8px}}
.pcell{padding:10px;background:var(--bg);border:1px solid var(--bdr);border-radius:8px;text-align:center}
.plabel{font-family:var(--mono);font-size:9px;letter-spacing:1.5px;color:var(--dim);margin-bottom:4px}
.pval{font-family:var(--mono);font-size:18px;color:#fff;margin-bottom:2px}
.ppct{font-family:var(--mono);font-size:10px;color:var(--dim)}
/* Higher-specificity overrides so .g/.r colors win over .pval/.ppct defaults */
.pval.g,.ppct.g{color:var(--green)}
.pval.r,.ppct.r{color:var(--red)}
</style>
</head>
<body>

<div class="hdr">
  <div class="logo">
    🐋 WHALE BOT <span class="v">v6</span>
    <span id="mode" class="mode paper">PAPER</span>
  </div>
  <div class="hdr-r">
    <span class="clk" id="clk">--:--:--</span>
    <span class="mbadge closed" id="mbadge">MARKET CLOSED</span>
  </div>
</div>

<div class="health" id="health">
  <span>Last Alpaca OK: <b id="h-ok">—</b></span>
  <span>Last scan: <b id="h-scan">—</b></span>
  <span>Calls: <b id="h-calls">0</b></span>
  <span>Errors: <b id="h-errs" class="ok">0</b></span>
  <span>Snapshot: <b id="h-snap">—</b></span>
</div>

<div class="wrap">
  <div class="g4">
    <div class="card">
      <div class="ct">Portfolio Value</div>
      <div class="sv" id="equity">—</div>
      <div class="ss" id="pnl-sub">—</div>
    </div>
    <div class="card">
      <div class="ct">Today's P&amp;L</div>
      <div class="sv" id="dpnl">—</div>
      <div class="gbar">
        <div class="gtrack"><div class="gfill" id="gfill" style="width:0%"></div></div>
        <div class="glbls"><span>$0</span><span id="goal-lbl">$100 goal</span></div>
      </div>
    </div>
    <div class="card">
      <div class="ct">Win / Loss Today</div>
      <div class="sv" id="wl">—</div>
      <div class="ss" id="wr-sub">All-time win rate: —</div>
    </div>
    <div class="card">
      <div class="ct">Day Trades Used</div>
      <div class="sv" id="dt">—</div>
      <div class="ss" id="dt-sub">Max 2 day trades</div>
    </div>
  </div>

  <!-- P&L Periods row — Daily / Weekly / Monthly / Yearly -->
  <div class="card period-card">
    <div class="ct">P&amp;L Periods</div>
    <div class="period-grid">
      <div class="pcell">
        <div class="plabel">TODAY</div>
        <div class="pval" id="pp-day">—</div>
        <div class="ppct" id="pp-day-pct">—</div>
      </div>
      <div class="pcell">
        <div class="plabel">WEEK</div>
        <div class="pval" id="pp-week">—</div>
        <div class="ppct" id="pp-week-pct">—</div>
      </div>
      <div class="pcell">
        <div class="plabel">MONTH</div>
        <div class="pval" id="pp-month">—</div>
        <div class="ppct" id="pp-month-pct">—</div>
      </div>
      <div class="pcell">
        <div class="plabel">YEAR</div>
        <div class="pval" id="pp-year">—</div>
        <div class="ppct" id="pp-year-pct">—</div>
      </div>
    </div>
  </div>

  <div class="g2">
    <div class="card">
      <div class="ct">Open Positions</div>
      <table>
        <thead><tr><th>Sym</th><th>Side</th><th>Qty</th><th>Entry</th><th>Cur</th><th>P&amp;L</th><th></th></tr></thead>
        <tbody id="pos-tb"><tr><td colspan="7" class="empty">No open positions</td></tr></tbody>
      </table>
    </div>
    <div class="card">
      <div class="ct">Bot Control</div>
      <div class="srow">
        <div class="dot run" id="sdot"></div>
        <div class="stxt g" id="stxt">RUNNING</div>
      </div>
      <div class="pw-row">
        <input class="pw-in" type="password" id="pw" maxlength="40" placeholder="Dashboard password">
      </div>
      <div class="btns">
        <button class="btn bp" id="btn-pause"  onclick="ctrl('pause')">⏸  PAUSE BOT</button>
        <button class="btn br" id="btn-resume" onclick="ctrl('resume')" disabled>▶  RESUME BOT</button>
        <button class="btn bs" onclick="showModal()">🛑  EMERGENCY STOP</button>
      </div>
      <div class="note">
        <b style="color:var(--text)">PAUSE</b> stops new trades, holds positions<br>
        <b style="color:var(--text)">EMERGENCY STOP</b> cancels orders + closes positions
      </div>
    </div>
  </div>

  <div class="g21">
    <div class="card">
      <div class="ct">Live Bot Activity</div>
      <div class="logbox" id="logbox">
        <div class="li info"><span class="lt">--:--</span><span class="lm">Connecting…</span></div>
      </div>
    </div>
    <div class="card">
      <div class="ct">Today's Trades</div>
      <div id="trades-panel"><div class="empty">No trades today</div></div>
    </div>
  </div>

  <div class="card" style="margin-bottom:12px">
    <div class="ct">Open Orders</div>
    <table>
      <thead><tr><th>Sym</th><th>Side</th><th>Qty</th><th>Type</th><th>Status</th></tr></thead>
      <tbody id="ord-tb"><tr><td colspan="5" class="empty">No open orders</td></tr></tbody>
    </table>
  </div>
</div>

<div class="modal" id="modal">
  <div class="mbox">
    <h3>🛑 Emergency Stop?</h3>
    <p>This cancels ALL orders and closes ALL positions instantly.</p>
    <div class="mbtns">
      <button class="btn" style="flex:1;background:rgba(255,255,255,.04);color:var(--dim);border-color:var(--bdr)" onclick="closeModal()">CANCEL</button>
      <button class="btn bs" style="flex:1" onclick="doStop()">CONFIRM STOP</button>
    </div>
  </div>
</div>

<script>
// ── helpers ────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const pw = () => $('pw').value.trim();
const fmt = n => { const v = parseFloat(n||0); return (v>=0?'+':'') + '$' + Math.abs(v).toFixed(2); };
const fv = n => '$' + parseFloat(n||0).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
const pc = v => parseFloat(v||0) >= 0 ? 'g' : 'r';

async function api(url, method='GET', body=null) {
  try {
    const r = await fetch(url, {
      method,
      headers: {'Content-Type':'application/json', 'X-Token': pw()},
      body: body ? JSON.stringify(body) : null
    });
    return await r.json();
  } catch(e) {
    console.error('api err', url, e);
    return null;
  }
}

// ── clock ──────────────────────────────────────────────────────
function tickClock() {
  try {
    const t = new Intl.DateTimeFormat('en-US', {
      timeZone:'America/New_York', hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false
    }).format(new Date());
    $('clk').textContent = t + ' ET';
  } catch(e) {}
}
setInterval(tickClock, 1000); tickClock();

// ── modal ──────────────────────────────────────────────────────
function showModal() { $('modal').classList.add('show'); }
function closeModal() { $('modal').classList.remove('show'); }

// ── controls ───────────────────────────────────────────────────
async function ctrl(action) {
  if (!pw()) { alert('Enter dashboard password first'); return; }
  const r = await api('/api/' + action, 'POST');
  if (!r || r.error) { alert('Error: ' + (r && r.error || 'request failed')); return; }
  setTimeout(loadData, 500);
}
async function doStop() {
  closeModal();
  if (!pw()) { alert('Enter password first'); return; }
  const r = await api('/api/estop', 'POST');
  if (!r || r.error) { alert('Error: ' + (r && r.error || 'request failed')); return; }
  setTimeout(loadData, 500);
}
async function closePos(sym) {
  if (!confirm('Close position: ' + sym + '?')) return;
  if (!pw()) { alert('Enter password first'); return; }
  await api('/api/close/' + sym, 'POST');
  setTimeout(loadData, 500);
}

// ── render data ────────────────────────────────────────────────
function renderData(d) {
  if (!d || !d.ok) {
    console.warn('bad data', d);
    return;
  }

  // Mode badge
  const mode = $('mode');
  if (d.live_mode) {
    mode.textContent = '🔴 LIVE';
    mode.className = 'mode live';
  } else {
    mode.textContent = '📝 PAPER';
    mode.className = 'mode paper';
  }

  // Health bar
  const h = d.health || {};
  $('h-ok').textContent   = h.last_alpaca_ok || '—';
  $('h-scan').textContent = h.last_scan_at || '—';
  $('h-calls').textContent = h.alpaca_call_count || 0;
  const errs = h.alpaca_err_count || 0;
  const errEl = $('h-errs');
  errEl.textContent = errs;
  errEl.className = errs > 5 ? 'err' : 'ok';
  $('h-snap').textContent = (d.snapshot_age_s != null) ? d.snapshot_age_s.toFixed(1) + 's ago' : '—';

  // Portfolio
  $('equity').textContent = fv(d.equity);
  const pv = parseFloat(d.pnl || 0);
  $('pnl-sub').innerHTML = 'Today: <span class="' + pc(pv) + '">' + fmt(pv) + '</span>';

  // P&L
  const dpv = parseFloat(d.daily_pnl || 0);
  const dpEl = $('dpnl');
  dpEl.textContent = fmt(dpv);
  dpEl.className = 'sv ' + (dpv >= 0 ? 'g' : 'r');
  const gp = Math.min(100, Math.max(0, dpv / (d.goal || 100) * 100));
  $('gfill').style.width = gp + '%';
  $('goal-lbl').textContent = '$' + (d.goal || 100) + ' goal';

  // P&L Periods (Daily / Weekly / Monthly / Yearly)
  const pp = d.period_pnl || {};
  const renderPeriod = (idVal, idPct, val, pct) => {
    const valEl = $(idVal);
    const pctEl = $(idPct);
    if (val === undefined || val === null) {
      valEl.textContent = '—';
      pctEl.textContent = '—';
      valEl.className = 'pval';
      pctEl.className = 'ppct';
      return;
    }
    const v = parseFloat(val) || 0;
    const p = parseFloat(pct) || 0;
    valEl.textContent = (v >= 0 ? '+' : '') + '$' + Math.abs(v).toFixed(2);
    pctEl.textContent = (p >= 0 ? '+' : '') + p.toFixed(2) + '%';
    const cls = v >= 0 ? 'g' : 'r';
    valEl.className = 'pval ' + cls;
    pctEl.className = 'ppct ' + cls;
  };
  renderPeriod('pp-day',   'pp-day-pct',   pp.daily,   pp.daily_pct);
  renderPeriod('pp-week',  'pp-week-pct',  pp.weekly,  pp.weekly_pct);
  renderPeriod('pp-month', 'pp-month-pct', pp.monthly, pp.monthly_pct);
  renderPeriod('pp-year',  'pp-year-pct',  pp.yearly,  pp.yearly_pct);

  // W/L
  $('wl').textContent = (d.wins || 0) + 'W / ' + (d.losses || 0) + 'L';
  $('wr-sub').textContent = 'All-time win rate: ' + (d.win_rate || 0) + '%';

  // Day trades
  const dtEl = $('dt');
  const dtSub = $('dt-sub');
  dtEl.textContent = (d.dt_used || 0) + ' / ' + (d.dt_max || 2);
  dtEl.className = 'sv ' + ((d.dt_used || 0) >= (d.dt_max || 2) ? 'r' : 'g');
  if (d.pdt_flagged) {
    dtSub.textContent = '⚠️ PDT FLAGGED — swing only at limit';
    dtSub.style.color = 'var(--yellow)';
  } else if ((d.dt_used || 0) >= (d.dt_max || 2)) {
    dtSub.textContent = 'Limit reached — swing only';
    dtSub.style.color = 'var(--yellow)';
  } else {
    dtSub.textContent = 'Max ' + (d.dt_max || 2) + ' day trades';
    dtSub.style.color = '';
  }

  // Market
  const mb = $('mbadge');
  if (d.market_open) {
    mb.className = 'mbadge open';
    mb.textContent = 'MARKET OPEN';
  } else {
    mb.className = 'mbadge closed';
    let txt = 'MARKET CLOSED';
    if (d.next_open) {
      try {
        const no = new Date(d.next_open).toLocaleString('en-US',
          {timeZone:'America/New_York', weekday:'short', hour:'2-digit', minute:'2-digit'});
        txt = 'Opens ' + no;
      } catch(e) {}
    }
    mb.textContent = txt;
  }

  // Bot status
  const dot = $('sdot');
  const st = $('stxt');
  const bp = $('btn-pause');
  const br = $('btn-resume');
  if (d.e_stop) {
    dot.className = 'dot stp'; st.className = 'stxt r'; st.textContent = 'EMERGENCY STOP';
    bp.disabled = true; br.disabled = false;
  } else if (d.paused) {
    dot.className = 'dot psd'; st.className = 'stxt w'; st.textContent = 'PAUSED';
    bp.disabled = true; br.disabled = false;
  } else {
    dot.className = 'dot run'; st.className = 'stxt g'; st.textContent = 'RUNNING';
    bp.disabled = false; br.disabled = true;
  }

  // Positions
  const pt = $('pos-tb');
  const positions = d.positions || [];
  if (positions.length === 0) {
    pt.innerHTML = '<tr><td colspan="7" class="empty">No open positions</td></tr>';
  } else {
    pt.innerHTML = positions.map(p => {
      const ent = parseFloat(p.avg_entry_price || 0).toFixed(2);
      const cur = parseFloat(p.current_price || 0).toFixed(2);
      const upl = parseFloat(p.unrealized_pl || 0);
      const upc = parseFloat(p.unrealized_plpc || 0) * 100;
      return '<tr>' +
        '<td><b>' + (p.symbol || '') + '</b></td>' +
        '<td class="' + (p.side === 'long' ? 'g' : 'r') + '">' + (p.side || '').toUpperCase() + '</td>' +
        '<td>' + (p.qty || '') + '</td>' +
        '<td>$' + ent + '</td>' +
        '<td>$' + cur + '</td>' +
        '<td class="' + pc(upl) + '">' + fmt(upl) + ' (' + upc.toFixed(1) + '%)</td>' +
        '<td><button class="cbtn" onclick="closePos(\'' + p.symbol + '\')">CLOSE</button></td>' +
        '</tr>';
    }).join('');
  }

  // Orders
  const ot = $('ord-tb');
  const orders = d.orders || [];
  if (orders.length === 0) {
    ot.innerHTML = '<tr><td colspan="5" class="empty">No open orders</td></tr>';
  } else {
    ot.innerHTML = orders.map(o =>
      '<tr>' +
      '<td><b>' + (o.symbol || '') + '</b></td>' +
      '<td class="' + (o.side === 'buy' ? 'g' : 'r') + '">' + (o.side || '').toUpperCase() + '</td>' +
      '<td>' + (o.qty || '') + '</td>' +
      '<td>' + (o.type || '').toUpperCase() + '</td>' +
      '<td>' + (o.status || '') + '</td>' +
      '</tr>'
    ).join('');
  }

  // Trades
  const tp = $('trades-panel');
  const trades = d.trades || [];
  if (trades.length === 0) {
    tp.innerHTML = '<div class="empty">No trades today</div>';
  } else {
    tp.innerHTML = trades.map(t => {
      const cls = t.side === 'buy' ? 'b' : 's';
      const tm = (t.time || '').slice(11, 19);
      return '<div class="trade-row">' +
        '<span><span class="bn ' + cls + '">' + (t.side || '').toUpperCase() + '</span> <b>' + t.sym + '</b></span>' +
        '<span>' + t.qty + ' @ $' + parseFloat(t.price || 0).toFixed(2) + '</span>' +
        '<span style="color:var(--dim);font-size:10px">' + tm + '</span>' +
      '</div>';
    }).join('');
  }
}

async function loadData() {
  const d = await api('/api/data');
  renderData(d);
}

async function loadLogs() {
  const r = await api('/api/logs');
  if (!r || !r.lines) return;
  const box = $('logbox');
  box.innerHTML = r.lines.slice().reverse().map(line => {
    let cls = 'info';
    if (/WIN|profit|✅/.test(line)) cls = 'success';
    else if (/LOSS|failed|❌/.test(line)) cls = 'error';
    else if (/⚠️|PDT|BLOCKED|🐋|🚨/.test(line)) cls = 'warning';
    const tm = line.match(/([0-9]{2}:[0-9]{2}):[0-9]{2}/);
    const t = tm ? tm[1] : '';
    const msg = line.replace(/^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2},?[0-9]* /, '');
    return '<div class="li ' + cls + '"><span class="lt">' + t + '</span><span class="lm">' + msg + '</span></div>';
  }).join('');
}

// ── start ──────────────────────────────────────────────────────
loadData();
loadLogs();

// Polling intervals
let dataInterval = setInterval(loadData, 2000);
let logsInterval = setInterval(loadLogs, 5000);

// Visibility-aware refresh: when tab becomes visible (user switches back
// to it, unlocks phone, etc), immediately refresh and resume polling.
// Fixes mobile browser timer throttling — without this, you'd see stale
// data after locking your phone for a few minutes.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    // Force immediate refresh when page becomes visible
    loadData();
    loadLogs();
  }
});

// Pulse the LIVE indicator each time fresh data arrives
const origRender = renderData;
renderData = function(d) {
  origRender(d);
  // Briefly flash the snapshot age indicator green to show fresh data
  const snap = $('h-snap');
  if (snap) {
    snap.style.transition = 'color 0.15s';
    snap.style.color = 'var(--green)';
    setTimeout(() => { snap.style.color = ''; }, 200);
  }
};
</script>
</body>
</html>
"""

def start_threads():
    threading.Thread(target=snapshot_loop, daemon=True, name="snapshot").start()
    threading.Thread(target=keepalive_loop, daemon=True, name="keepalive").start()
    threading.Thread(target=bot_loop, daemon=True, name="bot").start()
    log.info("🤖 Threads started")

start_threads()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
