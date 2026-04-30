"""
Whale Bot v5 — Production Build
================================
Clean architecture, debuggable, reliable dashboard.

Design principles:
1. Dashboard reads ONE source of truth: Alpaca API directly (no stale cache lies)
2. All API calls have timeouts and fallbacks
3. /api/debug endpoint exposes everything for diagnosis
4. Health checks visible on dashboard
5. Trades and P&L computed deterministically from fills

Render env vars (required):
  ALPACA_API_KEY
  ALPACA_SECRET_KEY
  ALPACA_BASE_URL    (https://paper-api.alpaca.markets or https://api.alpaca.markets)
  DASHBOARD_PASSWORD
  RENDER_EXTERNAL_URL (your render URL for keepalive)

Optional env vars:
  TRADE_SIZE       (default 25)
  DAILY_GOAL       (default 100)
  MAX_LOSS         (default 25)
  STOP_PCT         (default 0.05)
  TARGET_PCT       (default 0.07)
  MIN_SCORE        (default 55)
  MIN_PRICE        (default 10)
  MAX_DAY_TRADES   (default 2)
"""

import os, json, time, logging, threading, traceback
from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytz
import requests
from flask import Flask, jsonify, request

# ── CONFIG ────────────────────────────────────────────────────────────────────
KEY        = os.environ.get("ALPACA_API_KEY", "")
SECRET     = os.environ.get("ALPACA_SECRET_KEY", "")
BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
DATA_URL   = "https://data.alpaca.markets"
DASH_PASS  = os.environ.get("DASHBOARD_PASSWORD", "whale2024")
PORT       = int(os.environ.get("PORT", 8080))
KEEPALIVE  = os.environ.get("RENDER_EXTERNAL_URL", "")

TRADE_SIZE   = float(os.environ.get("TRADE_SIZE",   "25"))
DAILY_GOAL   = float(os.environ.get("DAILY_GOAL",   "100"))
MAX_LOSS     = float(os.environ.get("MAX_LOSS",     "25"))
STOP_PCT     = float(os.environ.get("STOP_PCT",     "0.05"))
TARGET_PCT   = float(os.environ.get("TARGET_PCT",   "0.07"))
MIN_SCORE    = int(os.environ.get("MIN_SCORE",      "55"))
MIN_PRICE    = float(os.environ.get("MIN_PRICE",    "10.0"))
MAX_DT       = int(os.environ.get("MAX_DAY_TRADES", "2"))
MAX_RISK_PCT = float(os.environ.get("MAX_RISK_PCT", "0.01"))

LIVE_MODE = "paper" not in BASE_URL.lower()
ET = pytz.timezone("America/New_York")
UTC = pytz.UTC

# ── LOGGING ───────────────────────────────────────────────────────────────────
LOG_FILE = "/tmp/whale_bot.log"

# Rotate if huge
try:
    if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 5_000_000:
        with open(LOG_FILE, "r") as f:
            kept = f.readlines()[-1000:]
        with open(LOG_FILE, "w") as f:
            f.writelines(kept)
except Exception:
    pass

# Root logger setup that survives gunicorn
_root = logging.getLogger()
_root.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(message)s")

# Always have stream handler
if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
           for h in _root.handlers):
    _sh = logging.StreamHandler()
    _sh.setFormatter(_fmt)
    _root.addHandler(_sh)

# File handler if writable
try:
    if not any(isinstance(h, logging.FileHandler) for h in _root.handlers):
        _fh = logging.FileHandler(LOG_FILE, mode="a")
        _fh.setFormatter(_fmt)
        _root.addHandler(_fh)
except Exception:
    pass

log = logging.getLogger("whale")
log.setLevel(logging.INFO)

# ── WATCHLIST ─────────────────────────────────────────────────────────────────
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

# ── SHARED STATE ──────────────────────────────────────────────────────────────
_lock = threading.Lock()
_state = {
    "paused": False,
    "e_stop": False,
    "alerts": [],
    "news_tickers": [],
    "traded_today": [],
    "lost_today": [],
    "tracker": {
        "date": str(date.today()),
        "wins_today": 0,
        "losses_today": 0,
        "day_trades": 0,
        "total_trades": 0,
        "total_wins": 0,
    },
    # Health metrics surfaced on dashboard
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
    """Add alert to memory + persist to file. Always log."""
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

# ── ALPACA API CLIENT ─────────────────────────────────────────────────────────
HDR = {
    "APCA-API-KEY-ID": KEY,
    "APCA-API-SECRET-KEY": SECRET,
    "Content-Type": "application/json",
}

def aGet(path, params=None, base=None, timeout=10):
    """Single Alpaca GET. Tracks health. Returns parsed JSON or raises."""
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

# Convenience wrappers — all may raise
def get_account():    return aGet("/v2/account")
def get_positions():  return aGet("/v2/positions")
def get_orders_open(): return aGet("/v2/orders", {"status": "open", "limit": 50})
def get_clock():      return aGet("/v2/clock")

def get_today_fills():
    """Get today's fills using ET-midnight converted to UTC."""
    now_et = datetime.now(ET)
    today_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    after = today_start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return aGet("/v2/account/activities/FILL",
                params={"after": after, "direction": "asc", "page_size": 100}) or []

def get_recent_fills(days=7):
    """Get fills from the past N days — used for cross-day FIFO matching
    so swing trades that close today get correctly counted as wins/losses
    even though their original buy was on a prior day.

    Note: Alpaca's max page_size for activities is 100. For accounts with
    more than 100 fills in the lookback window, this would need pagination,
    but for a bot doing 1-3 trades/day with $25 sizes, 100 covers ~1 month."""
    now_et = datetime.now(ET)
    today_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    after_dt = today_start - timedelta(days=days)
    after = after_dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return aGet("/v2/account/activities/FILL",
                params={"after": after, "direction": "asc", "page_size": 100}) or []

def get_portfolio_history(period="1A", timeframe="1D"):
    """Fetch Alpaca's portfolio history for a period.
    Returns dict with 'equity' (array), 'timestamp' (array), 'base_value' (float).
    period: 1D, 1W, 1M, 3M, 6M, 1A, all
    timeframe: 1Min, 5Min, 15Min, 1H, 1D"""
    return aGet("/v2/account/portfolio/history",
                params={"period": period, "timeframe": timeframe}) or {}

# Period P&L cache — refreshed every 5 minutes (equity moves slowly)
_period_cache = {"data": None, "ts": None}
_period_cache_lock = threading.Lock()

def get_period_pnl(force=False):
    """
    Return P&L over rolling Daily / Weekly / Monthly / Yearly periods.

    - Daily uses last_equity from /v2/account (= prior session close).
    - Week/Month/Year use Alpaca's portfolio history endpoint.
      Each period's base = first equity value in that period's series.

    Cached 5 minutes — these values move slowly and we don't want to hammer
    Alpaca with 3 history calls every 8 seconds.

    Note on accuracy: rolling periods use Alpaca's defined windows
    (1W = ~5 trading days, 1M = ~21 days, 1A = ~252 days). They are NOT
    calendar-aligned (Mon-to-now / 1st-to-now / Jan1-to-now). For most
    users this distinction is unimportant — both are reasonable measures
    of recent performance.

    Returns dict with: daily, weekly, monthly, yearly + corresponding
    _pct fields. Returns zeros if calls fail (dashboard shows '—').
    """
    now = datetime.now(ET)
    with _period_cache_lock:
        cached = _period_cache["data"]
        cached_ts = _period_cache["ts"]
    if not force and cached and cached_ts and (now - cached_ts).total_seconds() < 300:
        return cached

    result = {
        "daily": 0.0, "weekly": 0.0, "monthly": 0.0, "yearly": 0.0,
        "daily_pct": 0.0, "weekly_pct": 0.0, "monthly_pct": 0.0, "yearly_pct": 0.0,
        "ok": False,  # True only if at least daily computed
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

        # Fetch each period's history. If a call fails the others still work.
        periods = [("weekly", "1W"), ("monthly", "1M"), ("yearly", "1A")]
        for key, period in periods:
            try:
                hist = get_portfolio_history(period=period, timeframe="1D")
                eq_arr = hist.get("equity", []) or []
                # Filter out null/zero entries (account had no value yet)
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

# ── TRADE PAIRING & P&L ──────────────────────────────────────────────────────
def _group_fills(fills):
    """Group partial fills by order_id → list of trades, sorted by time."""
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
    """
    Compute trades for display + W/L + realized P&L.

    today_fills:    fills from today (used for display + identifying today's sells)
    lookback_fills: optional wider window of fills used for FIFO matching
                    so swing trades closing today get counted correctly
                    (their original buy was on a prior day).
                    If None, defaults to today_fills (no cross-day matching).

    Returns (display_trades, today_wins, today_losses, today_realized_pnl).
    """
    if lookback_fills is None:
        lookback_fills = today_fills

    if not today_fills and not lookback_fills:
        return [], 0, 0, 0.0

    # Order IDs from today — used to decide which matched sells "count" for today
    today_order_ids = {f.get("order_id", "") for f in today_fills if f.get("order_id")}

    today_trades = _group_fills(today_fills)
    all_trades   = _group_fills(lookback_fills)

    # Display trades (today only) — strip internal order_id
    display = [{k: v for k, v in t.items() if k != "order_id"} for t in today_trades]

    # FIFO match per symbol across the full lookback window
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
            # Only count toward TODAY when this sell happened today
            if matched_qty > 0 and sell["order_id"] in today_order_ids:
                pnl = (sell["price"] * matched_qty) - cost_basis
                today_realized += pnl
                if pnl > 0:
                    today_w += 1
                else:
                    today_l += 1

    return display, today_w, today_l, round(today_realized, 2)

# ── DASHBOARD DATA — single source of truth ──────────────────────────────────
class DataSnapshot:
    """
    One snapshot of everything the dashboard needs.
    Fetched fresh every refresh. Cached briefly to absorb mobile refresh bursts.
    """
    def __init__(self):
        self.acct = {}
        self.positions = []
        self.orders = []
        self.clock = {}
        self.fills = []           # today's fills (for display)
        self.lookback_fills = []  # past 7 days (for cross-day FIFO matching)
        self.period_pnl = {}      # rolling D/W/M/Y P&L
        self.errors = []  # what failed during fetch

    def fetch(self):
        """Fetch all dashboard data. Each call wrapped — partial data is OK."""
        # Account
        try:
            self.acct = get_account()
        except Exception as e:
            self.errors.append(f"account: {e}")

        # Positions
        try:
            self.positions = get_positions()
        except Exception as e:
            self.errors.append(f"positions: {e}")

        # Orders
        try:
            self.orders = get_orders_open()
        except Exception as e:
            self.errors.append(f"orders: {e}")

        # Clock
        try:
            self.clock = get_clock()
        except Exception as e:
            self.errors.append(f"clock: {e}")

        # Fills (today only — for display)
        try:
            self.fills = get_today_fills()
        except Exception as e:
            self.errors.append(f"fills: {e}")

        # Recent fills (past 7 days — for cross-day FIFO matching of swing closes)
        try:
            self.lookback_fills = get_recent_fills(days=7)
        except Exception as e:
            self.errors.append(f"lookback_fills: {e}")

        # Period P&L (cached internally — only hits Alpaca every 5 min)
        try:
            self.period_pnl = get_period_pnl()
        except Exception as e:
            self.errors.append(f"period_pnl: {e}")

        return self


# Snapshot cache — refreshed every 8 seconds
_snap_lock = threading.Lock()
_snap_data = {"snap": None, "ts": None}

# Last-known-good values per piece — used when a single fetch fails
_last_good = {"acct": {}, "positions": [], "orders": [], "clock": {}, "fills": [],
              "lookback_fills": [], "period_pnl": {}}
_last_good_lock = threading.Lock()

def get_snapshot(max_age_s=8):
    """Get cached snapshot if fresh, else refetch."""
    now = datetime.now(ET)
    with _snap_lock:
        s = _snap_data["snap"]
        ts = _snap_data["ts"]
        fresh = s and ts and (now - ts).total_seconds() < max_age_s
    if fresh:
        return s

    snap = DataSnapshot().fetch()

    # Merge with last-known-good — if a piece failed this time, use last good value
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

# ── INDICATORS ────────────────────────────────────────────────────────────────
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

# ── MARKET REGIME ─────────────────────────────────────────────────────────────
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

# ── SCANNER ───────────────────────────────────────────────────────────────────
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

    if sym in TIER1: score = min(100, score + 5)
    if not healthy:  score = int(score * 0.8)
    return min(score, 100), direction

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
        if cur < MIN_PRICE or not pc:
            return None
        dbars = aGet(f"/v2/stocks/{sym}/bars",
                     {"timeframe": "1Day", "limit": 20, "feed": "iex"},
                     DATA_URL, timeout=8).get("bars", [])
        mbars = aGet(f"/v2/stocks/{sym}/bars",
                     {"timeframe": "1Min", "limit": 60, "feed": "iex"},
                     DATA_URL, timeout=8).get("bars", [])
        sc, direction = score_ticker(sym, snap, dbars, mbars, healthy)
        if sc < MIN_SCORE:
            return None
        vr  = d.get("v", 0) / max(p.get("v", 1), 1)
        chg = (cur - pc) / pc
        tier, _ = whale_tier(snap)
        shares = max(1, int(tsize / cur))
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
        }
    except Exception as e:
        log.debug(f"scan {sym}: {e}")
        return None

def full_scan(label="SCAN"):
    update_health("last_scan_at", _now_et_str())
    with _lock:
        news = list(_state["news_tickers"])
    all_tickers = sorted(set(TICKERS + news))

    ok, _ = market_regime()
    regime_label = "✅ BULL" if ok else "⚠️ BEAR"
    effective_score = MIN_SCORE if ok else max(MIN_SCORE - 10, 40)

    push_alert(f"🔍 {label}: scanning {len(all_tickers)} tickers ({regime_label}, threshold {effective_score})", "info")

    try:
        cash = float(get_account().get("cash", TRADE_SIZE))
    except Exception:
        cash = TRADE_SIZE
    tsize = min(max(TRADE_SIZE, cash * 0.0004), 500)

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

    # Bear boost
    if not ok:
        for s in setups:
            if s["sym"] in BEAR_TICKERS:
                s["score"] = min(100, s["score"] + 15)

    setups.sort(key=lambda x: (x["tier"], x["score"]), reverse=True)
    top = setups[:5]

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

# ── NEWS ──────────────────────────────────────────────────────────────────────
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

# ── ORDERS ────────────────────────────────────────────────────────────────────
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
    """True if any open positions or orders."""
    try:
        return bool(get_positions() or get_orders_open())
    except Exception:
        return True  # safer to assume occupied on error

def reset_if_new_day():
    today = str(date.today())
    with _lock:
        if _state["tracker"]["date"] != today:
            _state["tracker"].update({
                "date": today,
                "wins_today": 0, "losses_today": 0, "day_trades": 0,
            })
            _state["traded_today"] = []
            _state["lost_today"]   = []
            _state["news_tickers"] = []

def execute(setups, label=""):
    reset_if_new_day()
    if is_paused():
        push_alert("⏸️ Paused — skipping execute", "warning")
        return
    if is_occupied():
        return
    if not setups:
        return

    dt_used = get_dt_used()
    pdt_maxed = dt_used >= MAX_DT
    if pdt_maxed:
        push_alert(f"⚠️ PDT {dt_used}/{MAX_DT} — swing only", "warning")

    with _lock:
        lost = list(_state["lost_today"])
    filtered = [s for s in setups if s["sym"] not in lost]
    if not filtered:
        return

    best = filtered[0]

    # Risk cap — never bet more than MAX_RISK_PCT * 100 of cash on one position
    try:
        cash = float(get_account().get("cash", 0))
        position_value = best["shares"] * best["price"]
        max_position = cash * (MAX_RISK_PCT * 100)
        if position_value > max_position:
            new_shares = max(1, int(max_position / best["price"]))
            if new_shares < best["shares"]:
                push_alert(f"⚠️ Sizing capped {best['sym']}: {best['shares']}→{new_shares}", "warning")
                best["shares"] = new_shares
    except Exception:
        pass

    tag = "🚨EXTREME" if best["tier"] == 3 else "🐋WHALE" if best["tier"] >= 1 else "📊"
    push_alert(f"{tag} {best['dir']} {best['sym']} | Score:{best['score']} | {best['chg']:+.1f}% | Vol:{best['vol']}x", "info")

    if best["tier"] == 3:
        ok = place_order(best, swing=pdt_maxed)
    elif pdt_maxed and best["score"] >= 65:
        push_alert(f"🔄 PDT maxed — SWING: {best['sym']}", "warning")
        ok = place_order(best, swing=True)
    elif pdt_maxed:
        push_alert(f"🛑 PDT maxed + low score — skip {best['sym']}", "warning")
        return
    elif best["score"] >= MIN_SCORE:
        ok = place_order(best, swing=False)
    else:
        return

    if ok:
        with _lock:
            if best["sym"] not in _state["traded_today"]:
                _state["traded_today"].append(best["sym"])

def monitor():
    try:
        for p in get_positions():
            pl  = float(p.get("unrealized_pl", 0))
            pct = float(p.get("unrealized_plpc", 0)) * 100
            push_alert(f"📊 {p.get('symbol')} x{p.get('qty')} | P&L: ${pl:+.2f} ({pct:+.1f}%)", "info")
    except Exception:
        pass

def eod():
    monitor()
    push_alert("🔔 EOD report", "info")

# ── BOT JOB & LOOP ────────────────────────────────────────────────────────────
def job(label):
    if is_paused():
        return
    setups, ok = full_scan(label)
    now = datetime.now(ET)
    if now.hour == 9 and now.minute < 45 and label not in ("PREMARKET 8AM", "OPENING RANGE"):
        # Opening 9:30-9:44 — only high conviction
        high = [s for s in setups if s["tier"] >= 2 or s["score"] >= 80]
        if high:
            push_alert(f"🚀 High conviction at open — {high[0]['sym']} score:{high[0]['score']}", "warning")
            execute(high, label)
        else:
            push_alert("⏳ Opening window — waiting score 80+ or whale tier 2+", "info")
        return
    execute(setups, label)

def bot_loop():
    """Main scheduled loop."""
    restore_alerts()
    push_alert(f"🐋 Whale Bot v5 online — {'🔴 LIVE' if LIVE_MODE else '📝 PAPER'}", "success")
    push_alert(f"Watching {len(TICKERS)} tickers | ${TRADE_SIZE}/trade | "
               f"min ${MIN_PRICE} | min score {MIN_SCORE}", "info")

    time.sleep(8)

    # Startup scan if open
    try:
        if get_clock().get("is_open"):
            push_alert("🔄 Market open — startup scan", "info")
            job("STARTUP")
        else:
            push_alert("💤 Market closed — waiting", "info")
    except Exception as e:
        push_alert(f"⚠️ Startup err: {e}", "warning")

    triggered = set()
    while True:
        try:
            update_health("last_loop_at", _now_et_str())
            reset_if_new_day()
            now = datetime.now(ET)
            h, m = now.hour, now.minute
            key = f"{h}:{m:02d}"

            schedule = {
                "8:00":  lambda: job("PREMARKET 8AM"),
                "9:30":  lambda: job("OPENING RANGE"),
                "9:45":  lambda: job("ORB BREAKOUT"),
                "10:00": lambda: job("MOMENTUM 10AM"),
                "12:00": lambda: job("MIDDAY"),
                "15:00": lambda: job("POWER HOUR"),
                "15:55": eod,
            }

            if key in schedule and key not in triggered:
                schedule[key]()
                triggered.add(key)
            elif 8 <= h < 16 and m in (0, 30) and f"news_{key}" not in triggered:
                fetch_news()
                triggered.add(f"news_{key}")
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
                triggered.add(key)

            if h == 0 and m == 1 and "midnight" not in triggered:
                triggered.clear()
                triggered.add("midnight")

            time.sleep(20)
        except Exception as e:
            push_alert(f"⚠️ Loop err: {e}\n{traceback.format_exc()[:200]}", "error")
            time.sleep(60)

def keepalive_loop():
    """Ping self every 4.5min to prevent Render sleep + give visible proof of life."""
    time.sleep(120)
    while True:
        try:
            if KEEPALIVE:
                requests.get(KEEPALIVE, timeout=10)
                push_alert("💓 Keepalive ping sent", "info")
            else:
                # No KEEPALIVE URL set — at least log that we're alive
                push_alert("💓 Bot heartbeat (no keepalive URL configured)", "info")
        except Exception as e:
            push_alert(f"⚠️ Keepalive failed: {e}", "warning")
        time.sleep(270)

def snapshot_loop():
    """Refresh dashboard snapshot every 8s in background — keeps mobile fast."""
    time.sleep(2)
    while True:
        try:
            snap = DataSnapshot().fetch()
            with _snap_lock:
                _snap_data["snap"] = snap
                _snap_data["ts"] = datetime.now(ET)
        except Exception as e:
            log.debug(f"snap loop: {e}")
        time.sleep(8)

# ── FLASK APP ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

def authed():
    tok = request.headers.get("X-Token", "") or request.args.get("token", "")
    return tok == DASH_PASS

# ── SINGLE SOURCE OF TRUTH: /api/data ────────────────────────────────────────
@app.route("/api/data")
def api_data():
    """
    Build full dashboard payload from one snapshot.
    Always returns 200 with what we have — never lies about trades that exist.
    """
    snap = get_snapshot()

    acct = snap.acct or {}
    positions = snap.positions or []
    orders = snap.orders or []
    clock = snap.clock or {}
    fills = snap.fills or []
    lookback_fills = snap.lookback_fills or []

    # Compute trades + realized P&L from fills (cross-day matching for swing closes)
    trades, wins, losses, realized = compute_trades_and_pnl(fills, lookback_fills)

    # Account values
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

    # Today P&L = (equity now - equity at start of day) — Alpaca's last_equity
    # is the equity at previous market close, which is accurate.
    daily_pnl = round(equity - last_eq, 2)

    # Unrealized from open positions for display detail
    unrealized = 0.0
    for p in positions:
        try:
            unrealized += float(p.get("unrealized_pl", 0))
        except (ValueError, TypeError):
            pass
    unrealized = round(unrealized, 2)

    # Win rate (today + history)
    with _lock:
        tw = _state["tracker"].get("total_wins", 0) + wins
        tt = _state["tracker"].get("total_trades", 0) + wins + losses
        alerts = list(_state["alerts"])
        traded = list(_state["traded_today"])
        lost   = list(_state["lost_today"])
        health = dict(_state["health"])
    win_rate = round(tw / tt * 100, 1) if tt else 0.0

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
        "period_pnl":     snap.period_pnl or {},
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
    """Diagnostic endpoint — exposes everything for troubleshooting."""
    try:
        snap = get_snapshot()
        with _lock:
            health = dict(_state["health"])
        return jsonify({
            "live_mode": LIVE_MODE,
            "base_url": BASE_URL,
            "tickers_count": len(TICKERS),
            "alpaca_health": health,
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
                "MAX_DT": MAX_DT,
                "STOP_PCT": STOP_PCT,
                "TARGET_PCT": TARGET_PCT,
            }
        })
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()})

@app.route("/api/health")
def api_health():
    """Simple health check for Render."""
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


# ── DASHBOARD HTML — clean rebuild ──────────────────────────────────────────
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
</style>
</head>
<body>

<div class="hdr">
  <div class="logo">
    🐋 WHALE BOT <span class="v">v5</span>
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
setInterval(loadData, 5000);
setInterval(loadLogs, 10000);
</script>
</body>
</html>
"""

# ── STARTUP ───────────────────────────────────────────────────────────────────
def start_threads():
    threading.Thread(target=snapshot_loop, daemon=True, name="snapshot").start()
    threading.Thread(target=keepalive_loop, daemon=True, name="keepalive").start()
    threading.Thread(target=bot_loop, daemon=True, name="bot").start()
    log.info("🤖 Threads started")

# Start threads at module import (so gunicorn picks them up)
start_threads()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
