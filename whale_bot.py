"""
Whale Bot v4 — Complete Clean Build
=====================================
Set these in Render environment:
  ALPACA_API_KEY     = your paper key
  ALPACA_SECRET_KEY  = your secret
  ALPACA_BASE_URL    = https://paper-api.alpaca.markets
  DASHBOARD_PASSWORD = whale2024
  RENDER_EXTERNAL_URL = https://whale-bot-3ijd.onrender.com
  MIN_PRICE          = 10
  MIN_SCORE          = 55
  STOP_PCT           = 0.05
  MAX_DAY_TRADES     = 2
"""

import os, json, time, logging, threading, pytz, requests
from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, jsonify, request

# ── CONFIG ────────────────────────────────────────────────────────────────────
KEY        = os.environ.get("ALPACA_API_KEY",    "")
SECRET     = os.environ.get("ALPACA_SECRET_KEY", "")
BASE_URL   = os.environ.get("ALPACA_BASE_URL",   "https://paper-api.alpaca.markets")
DATA_URL   = "https://data.alpaca.markets"
DASH_PASS  = os.environ.get("DASHBOARD_PASSWORD","whale2024")
PORT       = int(os.environ.get("PORT",          8080))
KEEPALIVE  = os.environ.get("RENDER_EXTERNAL_URL","")

TRADE_SIZE = float(os.environ.get("TRADE_SIZE",   "25"))   # Safer default for live launch
DAILY_GOAL = float(os.environ.get("DAILY_GOAL",   "100"))
MAX_LOSS   = float(os.environ.get("MAX_LOSS",     "25"))   # Tight circuit breaker first week
STOP_PCT   = float(os.environ.get("STOP_PCT",     "0.05"))
TARGET_PCT = float(os.environ.get("TARGET_PCT",   "0.07"))
MIN_SCORE  = int(os.environ.get("MIN_SCORE",      "55"))
MIN_PRICE  = float(os.environ.get("MIN_PRICE",    "10.0"))
MAX_DT     = int(os.environ.get("MAX_DAY_TRADES", "2"))

# Auto-detect live trading mode from BASE_URL — adds dashboard warning
LIVE_MODE = "paper" not in BASE_URL.lower()

# Max risk per trade: 1% of cash on hand — hard cap regardless of TRADE_SIZE
MAX_RISK_PCT = float(os.environ.get("MAX_RISK_PCT", "0.01"))

ET = pytz.timezone("America/New_York")

# ── LOGGING ───────────────────────────────────────────────────────────────────
LOG_FILE = "/tmp/whale_bot.log"

# Rotate log if over 5MB
try:
    if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 5_000_000:
        with open(LOG_FILE, "r") as f:
            lines = f.readlines()
        with open(LOG_FILE, "w") as f:
            f.writelines(lines[-1000:])
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("whale")

# Add file handler directly to root logger (works with gunicorn)
try:
    _fh = logging.FileHandler(LOG_FILE, mode="a")
    _fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    if not any(isinstance(h, logging.FileHandler) for h in logging.getLogger().handlers):
        logging.getLogger().addHandler(_fh)
except Exception:
    pass

# ── WATCHLIST ─────────────────────────────────────────────────────────────────
TIER1 = ["NVDA","AMD","TSLA","PLTR","SMCI","MSTR","MARA","RIOT","SOXL","TQQQ"]

# Bear market inverse ETFs — go UP when market drops
BEAR_TICKERS = ["SQQQ","SPXS","SDOW","TZA","FAZ","UVXY","VXX","SOXS","TECS","LABD"]

TICKERS = list(set(TIER1 + BEAR_TICKERS + [
    "META","GOOGL","MSFT","AMZN","AAPL","NFLX","UBER","HOOD","SOFI","UPST","COIN","ARKK",
    "OXY","DVN","MRO","HAL","XLE","BOIL","UCO","ERX",
    "CCJ","NNE","OKLO","SMR","UEC","URA",
    "FCX","MP","LAC","ALB","COPX","GDX","GDXJ","NEM","WPM",
    "IONQ","RGTI","RKLB","ASTS","LUNR","CLSK","WULF","JOBY",
    # High liquidity earnings movers
    "TXN","URI","IBM","NOW","TSLA","AAL","CMCSA","BA","MU","AMD",
    # High beta momentum names
    "CVNA","MSTR","SHOP","SNAP","RBLX","DKNG","PENN","CHWY","LYFT",
]))

# ── SHARED STATE ──────────────────────────────────────────────────────────────
_lock = threading.Lock()
_state = {
    "paused":       False,
    "e_stop":       False,
    "alerts":       [],
    "news_tickers": [],
    "traded_today": [],
    "lost_today":   [],
    "tracker": {
        "date":          str(date.today()),
        "wins_today":    0,
        "losses_today":  0,
        "day_trades":    0,
        "total_trades":  0,
        "total_wins":    0,
    },
}

# Alpaca data cache — refreshed by background thread
_cache_lock = threading.Lock()
_cache = {"acct": {}, "pos": [], "ords": [], "clk": {}}

# ── ALERT SYSTEM ──────────────────────────────────────────────────────────────
ALERTS_FILE = "/tmp/whale_alerts.json"

def push_alert(msg, level="info"):
    """Save alert to memory and file — survives restarts."""
    entry = {"t": datetime.now(ET).strftime("%H:%M"), "m": msg, "l": level}
    with _lock:
        _state["alerts"].append(entry)
        if len(_state["alerts"]) > 80:
            _state["alerts"].pop(0)
    try:
        saved = []
        if os.path.exists(ALERTS_FILE):
            with open(ALERTS_FILE) as f:
                saved = json.load(f)
        saved.append(entry)
        with open(ALERTS_FILE, "w") as f:
            json.dump(saved[-80:], f)
    except Exception:
        pass
    log.info(msg)

def restore_alerts():
    """Load saved alerts from file on startup."""
    try:
        if os.path.exists(ALERTS_FILE):
            with open(ALERTS_FILE) as f:
                saved = json.load(f)
            with _lock:
                _state["alerts"] = saved[-80:]
            log.info(f"Restored {len(saved)} alerts from file")
    except Exception:
        pass

# ── ALPACA API ────────────────────────────────────────────────────────────────
HDR = {
    "APCA-API-KEY-ID":     KEY,
    "APCA-API-SECRET-KEY": SECRET,
    "Content-Type":        "application/json",
}

def aGet(path, params=None, base=None):
    r = requests.get((base or BASE_URL) + path, headers=HDR, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def aPost(path, body):
    r = requests.post(BASE_URL + path, headers=HDR, json=body, timeout=15)
    r.raise_for_status()
    return r.json()

def aDel(path):
    r = requests.delete(BASE_URL + path, headers=HDR, timeout=10)
    return r.json() if r.text.strip() else {}

def get_account():  return aGet("/v2/account")
def get_positions():return aGet("/v2/positions")
def get_orders():   return aGet("/v2/orders", {"status": "open", "limit": 20})
def get_clock():    return aGet("/v2/clock")

def is_occupied():
    return bool(get_positions() or get_orders())

def get_dt_used():
    try:
        return int(get_account().get("daytrade_count", 0))
    except Exception:
        return 0

# ── STATE HELPERS ─────────────────────────────────────────────────────────────
def is_paused():
    with _lock:
        return _state["paused"] or _state["e_stop"]

def reset_if_new_day():
    today = str(date.today())
    with _lock:
        if _state["tracker"]["date"] != today:
            _state["tracker"].update({
                "date": today, "wins_today": 0, "losses_today": 0,
                "day_trades": 0,
            })
            _state["traded_today"] = []
            _state["lost_today"]   = []
            _state["news_tickers"] = []
            log.info("New trading day — reset complete")

def win_rate():
    with _lock:
        t = _state["tracker"]["total_trades"]
        w = _state["tracker"]["total_wins"]
    return round(w / t * 100, 1) if t else 0.0

def record_win():
    with _lock:
        _state["tracker"]["wins_today"] += 1
        _state["tracker"]["total_trades"] += 1
        _state["tracker"]["total_wins"] += 1

def record_loss():
    with _lock:
        _state["tracker"]["losses_today"] += 1
        _state["tracker"]["total_trades"] += 1

# ── INDICATORS ────────────────────────────────────────────────────────────────
def calc_rsi(bars, p=14):
    if len(bars) < p + 1: return 50
    c = [b["c"] for b in bars]
    g = [max(c[i]-c[i-1], 0) for i in range(1, len(c))]
    l = [max(c[i-1]-c[i], 0) for i in range(1, len(c))]
    ag = sum(g[-p:])/p; al = sum(l[-p:])/p
    return round(100 - (100/(1+ag/al)), 1) if al else 100

def calc_vwap(bars):
    if not bars: return None
    tv = sum(((b["h"]+b["l"]+b["c"])/3)*b["v"] for b in bars)
    v  = sum(b["v"] for b in bars)
    return round(tv/v, 2) if v else None

def calc_ema(bars, p=9):
    if len(bars) < p: return None
    c = [b["c"] for b in bars]; k = 2/(p+1)
    e = sum(c[:p])/p
    for x in c[p:]: e = x*k + e*(1-k)
    return round(e, 2)

# ── MARKET REGIME ─────────────────────────────────────────────────────────────
_regime_cache = {"ts": None, "ok": True, "chg": 0.0}

def market_regime():
    global _regime_cache
    now = datetime.now(ET)
    if _regime_cache["ts"] and (now - _regime_cache["ts"]).seconds < 900:
        return _regime_cache["ok"], _regime_cache["chg"]
    try:
        bars = aGet("/v2/stocks/SPY/bars",
                    {"timeframe":"1Day","limit":20,"feed":"iex"}, DATA_URL).get("bars",[])
        snap = aGet("/v2/stocks/SPY/snapshot", base=DATA_URL)
        e9  = calc_ema(bars, 9); e20 = calc_ema(bars, 20)
        cur = snap.get("latestTrade",{}).get("p", 0)
        prc = snap.get("prevDailyBar",{}).get("c", cur)
        chg = (cur-prc)/prc if prc else 0
        ok  = bool(e9 and e20 and cur > e9 and e9 > e20)
        _regime_cache = {"ts": now, "ok": ok, "chg": chg}
        return ok, chg
    except Exception:
        return True, 0.0

# ── SCANNER ───────────────────────────────────────────────────────────────────
def whale_tier(snap):
    d = snap.get("dailyBar",{}); p = snap.get("prevDailyBar",{})
    tv = d.get("v",0); pv = max(p.get("v",1), 1)
    vr = tv/pv
    if vr >= 20: return 3, round(vr,1)
    if vr >= 10: return 2, round(vr,1)
    if vr >= 5:  return 1, round(vr,1)
    return 0, round(vr,1)

def score_ticker(sym, snap, dbars, mbars, spy_chg=0.0, healthy=True):
    d = snap.get("dailyBar",{}); p = snap.get("prevDailyBar",{})
    t = snap.get("latestTrade",{})
    cur = t.get("p", d.get("c",0)); pc = p.get("c",0)
    if not cur or not pc: return 0, "LONG"
    chg = (cur-pc)/pc
    vr  = d.get("v",0) / max(p.get("v",1),1)
    direction = "LONG" if chg > 0 else "SHORT"
    score = 0
    ac = abs(chg)

    # Volume score — lowered floor to 1.2x so quiet days still score
    tier,_ = whale_tier(snap)
    score += {3:40, 2:32, 1:24, 0:16 if vr>=3 else 12 if vr>=2 else 8 if vr>=1.5 else 4 if vr>=1.2 else 0}[tier]

    # Price move score — strong moves score independently of volume
    # This catches earnings movers and news-driven moves on normal volume
    score += 30 if ac>=0.10 else 24 if ac>=0.07 else 18 if ac>=0.04 else 12 if ac>=0.025 else 6 if ac>=0.015 else 0

    # Need at least some base score to continue
    if score < 8: return 0, direction

    r = calc_rsi(dbars)
    if direction=="LONG":
        score += 15 if 45<=r<=65 else 10 if 35<=r<45 else 5 if r<=72 else 0
    else:
        score += 15 if 28<=r<=50 else 10 if r<28 else 5

    v = calc_vwap(mbars)
    if v and cur:
        dist = (cur-v)/v
        if direction=="LONG":
            score += 15 if dist>0.01 else 8 if dist>-0.005 else 0
        else:
            score += 15 if dist<-0.01 else 8 if dist<0.005 else 0

    if sym in TIER1: score = min(100, score+5)
    if not healthy: score = int(score*0.8)
    return min(score, 100), direction

def scan_one(sym, spy_chg, healthy, tsize):
    try:
        snap = aGet(f"/v2/stocks/{sym}/snapshot", base=DATA_URL)
        if not snap: return None
        d = snap.get("dailyBar",{}); p = snap.get("prevDailyBar",{})
        t = snap.get("latestTrade",{})
        cur = t.get("p", d.get("c",0)); pc = p.get("c",0)
        if cur < MIN_PRICE or not pc: return None
        dbars = aGet(f"/v2/stocks/{sym}/bars",
                     {"timeframe":"1Day","limit":20,"feed":"iex"}, DATA_URL).get("bars",[])
        mbars = aGet(f"/v2/stocks/{sym}/bars",
                     {"timeframe":"1Min","limit":60,"feed":"iex"}, DATA_URL).get("bars",[])
        sc, direction = score_ticker(sym, snap, dbars, mbars, spy_chg, healthy)
        if sc < MIN_SCORE: return None
        vr = d.get("v",0)/max(p.get("v",1),1)
        chg = (cur-pc)/pc
        tier,_ = whale_tier(snap)
        shares = max(1, int(tsize/cur))
        sl = round(cur*(1-STOP_PCT),2)   if direction=="LONG" else round(cur*(1+STOP_PCT),2)
        tp = round(cur*(1+TARGET_PCT),2) if direction=="LONG" else round(cur*(1-TARGET_PCT),2)
        return {
            "sym": sym, "price": round(cur,2), "chg": round(chg*100,2),
            "vol": round(vr,1), "tier": tier, "dir": direction,
            "score": sc, "shares": shares,
            "sl": sl, "tp": tp,
            "pot":  round(shares*cur*TARGET_PCT,2),
            "risk": round(shares*cur*STOP_PCT,2),
        }
    except Exception as e:
        log.debug(f"{sym}: {e}")
        return None

def full_scan(label="SCAN"):
    with _lock:
        news = list(_state["news_tickers"])
    all_tickers = list(set(TICKERS + news))
    if news:
        push_alert(f"📰 Including {len(news)} news tickers: {', '.join(news)}", "info")

    ok, chg = market_regime()
    regime = "✅ BULL" if ok else "⚠️ BEAR"

    # In bear market — lower threshold and prioritize inverse ETFs
    # Bear market = look for SQQQ, SPXS, UVXY etc moving up
    effective_score = MIN_SCORE if ok else max(MIN_SCORE - 10, 40)
    if not ok:
        push_alert(f"🔍 {label}: BEAR market — scanning {len(all_tickers)} tickers (threshold lowered to {effective_score})", "info")
    else:
        push_alert(f"🔍 {label}: scanning {len(all_tickers)} tickers...", "info")

    try:
        cash = float(get_account().get("cash", TRADE_SIZE))
    except Exception:
        cash = TRADE_SIZE
    tsize = min(max(TRADE_SIZE, cash * 0.0004), 500)

    setups = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(scan_one, s, chg, ok, tsize): s for s in all_tickers}
        for f in as_completed(futs):
            r = f.result()
            if r and r["score"] >= effective_score:
                setups.append(r)

    # In bear market — boost inverse ETFs to top of list
    if not ok:
        for s in setups:
            if s["sym"] in BEAR_TICKERS:
                s["score"] = min(100, s["score"] + 15)

    setups.sort(key=lambda x: (x["tier"], x["score"]), reverse=True)
    top = setups[:5]

    if top:
        lines = [f"✅ {label} | {regime} | {len(setups)} setups found"]
        for i, s in enumerate(top[:3]):
            w = "🐋" if s["tier"]>=2 else "🐳" if s["tier"]==1 else "📊"
            bear_tag = " 🔻BEAR" if s["sym"] in BEAR_TICKERS else ""
            lines.append(f"  #{i+1} {w} {s['sym']}{bear_tag} score:{s['score']} {s['chg']:+.1f}% vol:{s['vol']}x")
        push_alert("\n".join(lines), "success")
    else:
        push_alert(f"📊 {label} | {regime} | No setups found — watching", "info")

    return top, ok

# ── NEWS SCANNER ──────────────────────────────────────────────────────────────
def fetch_news():
    try:
        since = (datetime.now(ET) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data  = aGet("/v1beta1/news", params={"start": since, "limit": 50}, base=DATA_URL)
        news  = data.get("news", [])
        counts = {}
        for article in news:
            for sym in article.get("symbols", []):
                if sym and len(sym) <= 5 and sym.isalpha():
                    counts[sym] = counts.get(sym, 0) + 1
        hot = [s for s, c in counts.items() if c >= 2 and s not in TICKERS][:10]
        if hot:
            push_alert(f"📰 NEWS hot tickers: {', '.join(hot)}", "info")
            with _lock:
                _state["news_tickers"] = hot
    except Exception as e:
        log.debug(f"News fetch: {e}")

# ── ORDERS ────────────────────────────────────────────────────────────────────
def place_order(setup, swing=False):
    side = "buy" if setup["dir"] == "LONG" else "sell"
    body = {
        "symbol": setup["sym"], "qty": str(setup["shares"]),
        "side": side, "type": "market",
        "time_in_force": "gtc" if swing else "day",
        "order_class": "bracket",
        "stop_loss":   {"stop_price":  str(setup["sl"])},
        "take_profit": {"limit_price": str(setup["tp"])},
    }
    try:
        aPost("/v2/orders", body)
        kind = "SWING" if swing else "DAY TRADE"
        push_alert(
            f"✅ {kind}: {side.upper()} {setup['shares']}x {setup['sym']} "
            f"@ ~${setup['price']} | SL:${setup['sl']} TP:${setup['tp']}", "success")
        return True
    except Exception as e:
        push_alert(f"❌ Order failed {setup['sym']}: {e}", "error")
        return False

# ── EXECUTE ───────────────────────────────────────────────────────────────────
def execute(setups, healthy, label=""):
    reset_if_new_day()

    # Safety checks
    with _lock:
        daily_pnl = 0.0  # Will be read from Alpaca in api_data
    if is_paused():
        push_alert("⏸️ Bot paused — skipping", "warning"); return
    if is_occupied():
        log.info("Position open — skipping"); return
    if not setups:
        return

    # Read PDT count live from Alpaca
    dt_used   = get_dt_used()
    pdt_maxed = dt_used >= MAX_DT

    if pdt_maxed:
        push_alert(f"⚠️ PDT limit ({dt_used}/{MAX_DT} day trades) — swing trades only", "warning")

    # Filter losers
    with _lock:
        lost    = list(_state["lost_today"])
        traded  = list(_state["traded_today"])
    filtered = [s for s in setups if s["sym"] not in lost]
    if not filtered:
        push_alert("📊 All setups already lost today — skipping", "info"); return

    best = filtered[0]

    # MAX RISK CAP: never risk more than MAX_RISK_PCT of account on a single trade
    try:
        cash = float(get_account().get("cash", 0))
        position_value = best["shares"] * best["price"]
        max_position = cash * (MAX_RISK_PCT * 100)  # Allow position up to 100x risk pct
        if position_value > max_position:
            # Reduce shares to fit risk cap
            new_shares = max(1, int(max_position / best["price"]))
            if new_shares < best["shares"]:
                push_alert(
                    f"⚠️ Position size capped: {best['sym']} reduced "
                    f"{best['shares']}→{new_shares} shares (1% account risk)", "warning")
                best["shares"] = new_shares
    except Exception as e:
        log.debug(f"Risk cap check error: {e}")

    tags = "🚨EXTREME" if best["tier"]==3 else "🐋WHALE" if best["tier"]>=1 else "📊"
    push_alert(
        f"{tags} {best['dir']} {best['sym']} | Score:{best['score']} | "
        f"{best['chg']:+.1f}% | Vol:{best['vol']}x", "info")

    # Execute with PDT logic
    if best["tier"] == 3:
        ok = place_order(best, swing=pdt_maxed)
        if ok:
            with _lock:
                if best["sym"] not in _state["traded_today"]:
                    _state["traded_today"].append(best["sym"])

    elif pdt_maxed and best["score"] >= 65:
        push_alert(f"🔄 PDT maxed — SWING: {best['sym']}", "warning")
        ok = place_order(best, swing=True)
        if ok:
            with _lock:
                if best["sym"] not in _state["traded_today"]:
                    _state["traded_today"].append(best["sym"])

    elif pdt_maxed:
        push_alert(f"🛑 PDT maxed + score too low — skipping {best['sym']}", "warning")

    elif best["score"] >= MIN_SCORE:
        ok = place_order(best, swing=False)
        if ok:
            with _lock:
                if best["sym"] not in _state["traded_today"]:
                    _state["traded_today"].append(best["sym"])
    else:
        push_alert(f"👤 Score {best['score']} below threshold — skipping", "warning")

# ── MONITOR ───────────────────────────────────────────────────────────────────
def monitor():
    try:
        for p in get_positions():
            pl  = float(p.get("unrealized_pl", 0))
            pct = float(p.get("unrealized_plpc", 0)) * 100
            sym = p.get("symbol","")
            push_alert(f"📊 {sym} x{p['qty']} | P&L: ${pl:+.2f} ({pct:+.1f}%)", "info")
    except Exception:
        pass

def eod():
    monitor()
    with _lock:
        t = dict(_state["tracker"])
    wr = win_rate()
    push_alert(
        f"🔔 EOD | {t['wins_today']}W/{t['losses_today']}L today | "
        f"All-time: {wr}% ({t['total_wins']}/{t['total_trades']} trades)", "info")

# ── BOT JOB ───────────────────────────────────────────────────────────────────
def job(label):
    if is_paused():
        push_alert(f"⏸️ Paused — skipping {label}", "warning"); return
    setups, ok = full_scan(label)
    # Opening window 9:30-9:44 — only skip low conviction trades
    now = datetime.now(ET)
    if now.hour == 9 and now.minute < 45 and label not in ("PREMARKET 8AM", "OPENING RANGE"):
        # Allow tier 2+ whales or score 80+ through — they're real moves
        high_conviction = [s for s in setups if s["tier"] >= 2 or s["score"] >= 80]
        if high_conviction:
            push_alert(f"🚀 High conviction at open — executing {high_conviction[0]['sym']} score:{high_conviction[0]['score']}", "warning")
            execute(high_conviction, ok, label)
        else:
            push_alert("⏳ Opening window (9:30-9:44) — waiting for score 80+ or whale tier 2+", "info")
        return
    execute(setups, ok, label)

# ── BACKGROUND THREADS ────────────────────────────────────────────────────────
def cache_refresh_loop():
    """Keep Alpaca data fresh in background — never blocks Flask."""
    time.sleep(5)
    while True:
        try:
            acct = get_account()
            pos  = get_positions()
            ords = get_orders()
            clk  = get_clock()
            with _cache_lock:
                _cache.update({"acct": acct, "pos": pos, "ords": ords, "clk": clk})
        except Exception as e:
            log.debug(f"Cache error: {e}")
        time.sleep(10)

def keepalive_loop():
    """Ping self to prevent Render free tier sleep."""
    time.sleep(90)
    while True:
        try:
            if KEEPALIVE:
                requests.get(KEEPALIVE, timeout=10)
                log.info("💓 Keepalive ping sent")
        except Exception:
            pass
        time.sleep(270)

def bot_loop():
    """Main bot loop."""
    restore_alerts()
    push_alert("🐋 Whale Bot v4 online", "success")
    push_alert(f"Watching {len(TICKERS)} tickers | ${TRADE_SIZE}/trade | "
               f"MIN_PRICE:${MIN_PRICE} | MIN_SCORE:{MIN_SCORE}", "info")

    # Startup scan if market open
    time.sleep(10)
    try:
        if get_clock().get("is_open"):
            push_alert("🔄 Market open — startup scan running", "info")
            job("STARTUP SCAN")
        else:
            push_alert("💤 Market closed — waiting for open", "info")
    except Exception as e:
        push_alert(f"⚠️ Startup error: {e}", "warning")

    triggered = set()
    while True:
        try:
            reset_if_new_day()
            now = datetime.now(ET); h, m = now.hour, now.minute
            key = f"{h}:{m:02d}"

            schedule = {
                "8:00":  lambda: job("PREMARKET 8AM"),
                "9:30":  lambda: job("OPENING RANGE"),
                "9:45":  lambda: job("ORB BREAKOUT"),
                "10:00": lambda: job("MOMENTUM 10AM"),
                "12:00": lambda: job("MIDDAY SWING"),
                "15:00": lambda: job("POWER HOUR"),
                "15:55": eod,
            }

            if key in schedule and key not in triggered:
                schedule[key](); triggered.add(key)

            elif 8 <= h < 16 and m in (0, 30) and f"news_{key}" not in triggered:
                fetch_news(); triggered.add(f"news_{key}")

            elif 9 <= h < 16 and m % 2 == 0 and key not in triggered:
                try:
                    monitor()
                    if not is_occupied() and not is_paused():
                        setups, ok = full_scan("CONTINUOUS")
                        whales = [s for s in setups if s["tier"] >= 2]
                        if whales:
                            execute(whales, ok, "WHALE ALERT")
                except Exception as scan_err:
                    push_alert(f"⚠️ Scan error: {scan_err}", "warning")
                triggered.add(key)

            if h == 0 and m == 1 and "midnight" not in triggered:
                triggered.clear(); triggered.add("midnight")

            time.sleep(20)
        except Exception as e:
            push_alert(f"⚠️ Bot loop error: {e}", "error")
            time.sleep(60)

# ── FLASK APP ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

# Start all threads — defined AFTER all functions above
def start_threads():
    threading.Thread(target=cache_refresh_loop, daemon=True).start()
    threading.Thread(target=keepalive_loop,     daemon=True).start()
    threading.Thread(target=bot_loop,           daemon=True).start()
    log.info("🤖 All threads started")

start_threads()

# ── API ROUTES ────────────────────────────────────────────────────────────────
def authed():
    tok = request.headers.get("X-Token","") or request.args.get("token","")
    return tok == DASH_PASS

@app.route("/")
def index():
    return DASHBOARD_HTML

def get_today_fills():
    """Fetch today's fills using proper UTC timestamp filter."""
    try:
        now_et = datetime.now(ET)
        today_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
        today_utc = today_start.astimezone(pytz.UTC)
        after = today_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        return aGet("/v2/account/activities/FILL",
                    params={"after": after, "direction":"asc","page_size":100}) or []
    except Exception as e:
        log.debug(f"Fills error: {e}")
        return []

def calc_today_trades_and_pnl():
    """Group fills into trades and calculate realized P&L using FIFO matching."""
    fills = get_today_fills()
    if not fills:
        return [], 0, 0, 0.0

    # Group partial fills by order_id
    by_order = {}
    for f in fills:
        oid = f.get("order_id","")
        if oid not in by_order:
            by_order[oid] = {
                "sym":   f.get("symbol",""),
                "side":  f.get("side",""),
                "qty":   0,
                "value": 0.0,
                "time":  f.get("transaction_time","")[:19].replace("T"," "),
            }
        q = float(f.get("qty", 0))
        p = float(f.get("price", 0))
        by_order[oid]["qty"]   += q
        by_order[oid]["value"] += q * p

    trades = []
    for oid, t in by_order.items():
        if t["qty"] > 0:
            trades.append({
                "sym":   t["sym"],
                "side":  t["side"],
                "qty":   int(t["qty"]),
                "price": round(t["value"] / t["qty"], 2),
                "time":  t["time"],
            })
    trades.sort(key=lambda x: x["time"])

    # FIFO match buys with sells per symbol
    by_sym = {}
    for t in trades:
        sym = t["sym"]
        by_sym.setdefault(sym, {"buys": [], "sells": []})
        by_sym[sym]["buys" if t["side"]=="buy" else "sells"].append({
            "qty": t["qty"], "price": t["price"]
        })

    wins = 0; losses = 0; realized = 0.0
    for sym, data in by_sym.items():
        buys = [b.copy() for b in data["buys"]]
        for sell in data["sells"]:
            qty_left = sell["qty"]
            cost = 0.0
            for buy in buys:
                if qty_left <= 0 or buy["qty"] <= 0: continue
                take = min(qty_left, buy["qty"])
                cost += take * buy["price"]
                buy["qty"] -= take
                qty_left  -= take
            matched = sell["qty"] - qty_left
            if matched > 0:
                pnl = (sell["price"] * matched) - cost
                realized += pnl
                if pnl > 0: wins += 1
                else:       losses += 1

    return trades, wins, losses, round(realized, 2)

@app.route("/api/data")
def api_data():
    try:
        # Direct Alpaca fetch — always accurate. Cache only as fallback.
        try:    acct = get_account()
        except:
            with _cache_lock: acct = dict(_cache["acct"])
        try:    pos  = get_positions()
        except:
            with _cache_lock: pos = list(_cache["pos"])
        try:    ords = get_orders()
        except:
            with _cache_lock: ords = list(_cache["ords"])
        try:    clk  = get_clock()
        except:
            with _cache_lock: clk = dict(_cache["clk"])

        with _lock:
            alerts = list(_state["alerts"])
            traded = list(_state["traded_today"])
            lost   = list(_state["lost_today"])

        equity   = float(acct.get("equity",  0))
        dt_used  = int(acct.get("daytrade_count", 0))
        pdt_flag = bool(acct.get("pattern_day_trader", False))

        # Real trades + realized P&L from Alpaca fills
        trades, wins, losses, realized = calc_today_trades_and_pnl()
        unrealized = sum(float(p.get("unrealized_pl", 0)) for p in pos)
        daily_pnl  = round(realized + unrealized, 2)

        with _lock:
            tw = _state["tracker"].get("total_wins", 0) + wins
            tt = _state["tracker"].get("total_trades", 0) + wins + losses
        wr = round(tw / tt * 100, 1) if tt else 0.0

        return jsonify({
            "equity":         equity,
            "pnl":            daily_pnl,
            "daily_pnl":      daily_pnl,
            "realized_pnl":   realized,
            "unrealized_pnl": round(unrealized, 2),
            "wins":           wins,
            "losses":         losses,
            "win_rate":       wr,
            "dt_used":        dt_used,
            "dt_max":         MAX_DT,
            "pdt_flagged":    pdt_flag,
            "live_mode":      LIVE_MODE,
            "trade_size":     TRADE_SIZE,
            "max_loss":       MAX_LOSS,
            "paused":         _state["paused"],
            "e_stop":         _state["e_stop"],
            "market_open":    clk.get("is_open", False),
            "next_open":      clk.get("next_open",""),
            "positions":      pos,
            "orders":         ords,
            "alerts":         alerts[-40:],
            "trades":         trades,
            "traded_today":   traded,
            "lost_today":     lost,
            "goal":           DAILY_GOAL,
        })
    except Exception as e:
        log.error(f"api_data error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/logs")
def api_logs():
    try:
        if not os.path.exists(LOG_FILE):
            return jsonify({"lines": ["No logs yet..."]})
        with open(LOG_FILE) as f:
            lines = [l.strip() for l in f.readlines()[-150:] if l.strip()]
        return jsonify({"lines": lines})
    except Exception as e:
        return jsonify({"lines": [f"Log error: {e}"]})

@app.route("/api/pause", methods=["POST"])
def api_pause():
    if not authed(): return jsonify({"error":"Wrong password"}), 401
    with _lock: _state["paused"] = True
    push_alert("⏸️ Bot PAUSED", "warning")
    return jsonify({"ok": True})

@app.route("/api/resume", methods=["POST"])
def api_resume():
    if not authed(): return jsonify({"error":"Wrong password"}), 401
    with _lock: _state["paused"] = False; _state["e_stop"] = False
    push_alert("▶️ Bot RESUMED", "success")
    return jsonify({"ok": True})

@app.route("/api/estop", methods=["POST"])
def api_estop():
    if not authed(): return jsonify({"error":"Wrong password"}), 401
    with _lock: _state["paused"] = True; _state["e_stop"] = True
    try: aDel("/v2/orders")
    except Exception: pass
    try: requests.delete(BASE_URL+"/v2/positions", headers=HDR, timeout=10)
    except Exception: pass
    push_alert("🛑 EMERGENCY STOP — all orders cancelled", "error")
    return jsonify({"ok": True})

@app.route("/api/close/<sym>", methods=["POST"])
def api_close(sym):
    if not authed(): return jsonify({"error":"Wrong password"}), 401
    try:
        requests.delete(f"{BASE_URL}/v2/positions/{sym}", headers=HDR, timeout=10)
        push_alert(f"🔴 Closed position: {sym}", "warning")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── DASHBOARD HTML ────────────────────────────────────────────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>🐋 Whale Bot</title>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=DM+Sans:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#07090d;--card:#0d1219;--bdr:#182030;--green:#00e5b0;--blue:#3d9eff;--red:#ff4060;--yellow:#ffb020;--text:#b8c4d0;--dim:#4a5568;--mono:'Share Tech Mono',monospace;--sans:'DM Sans',sans-serif}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px}
.hdr{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;background:var(--card);border-bottom:1px solid var(--bdr);position:sticky;top:0;z-index:10}
.logo{font-family:var(--mono);font-size:16px;color:var(--green);letter-spacing:2px}
.logo span{color:var(--dim);font-size:11px;margin-left:8px}
.hdr-r{display:flex;align-items:center;gap:10px}
.clk{font-family:var(--mono);font-size:12px;color:var(--dim)}
.mbadge{font-family:var(--mono);font-size:10px;padding:3px 9px;border-radius:20px;font-weight:700;letter-spacing:1px}
.mo{background:rgba(0,229,176,.12);color:var(--green);border:1px solid rgba(0,229,176,.3)}
.mc{background:rgba(255,255,255,.04);color:var(--dim);border:1px solid var(--bdr)}
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
.psd{background:var(--yellow)}
.stp{background:var(--red)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.srow{display:flex;align-items:center;gap:8px;margin-bottom:12px}
.stxt{font-family:var(--mono);font-size:13px}
.pw-row{display:flex;gap:8px;margin-bottom:10px}
.pw-in{flex:1;background:var(--bg);border:1px solid var(--bdr);color:var(--text);font-family:var(--mono);font-size:14px;padding:7px 10px;border-radius:5px;outline:none;letter-spacing:3px}
.pw-in:focus{border-color:var(--green)}
.btns{display:flex;flex-direction:column;gap:7px}
.btn{padding:9px;border-radius:6px;border:1px solid;font-family:var(--mono);font-size:11px;letter-spacing:1px;cursor:pointer;width:100%;transition:all .15s;font-weight:700}
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
.li .lm{color:var(--text);flex:1;word-break:break-word}
.li.info{border-left-color:var(--blue);background:rgba(61,158,255,.03)}
.li.success{border-left-color:var(--green);background:rgba(0,229,176,.04)}
.li.warning{border-left-color:var(--yellow);background:rgba(255,176,32,.04)}
.li.error{border-left-color:var(--red);background:rgba(255,64,96,.05)}
.cbtn{font-family:var(--mono);font-size:9px;padding:3px 7px;border-radius:3px;cursor:pointer;border:1px solid rgba(255,64,96,.3);background:rgba(255,64,96,.08);color:var(--red)}
.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:99;align-items:center;justify-content:center}
.modal.show{display:flex}
.mbox{background:var(--card);border:1px solid var(--red);border-radius:12px;padding:24px;max-width:300px;width:90%;text-align:center}
.mbox h3{color:var(--red);font-family:var(--mono);margin-bottom:8px}
.mbox p{color:var(--dim);font-size:12px;margin-bottom:16px;line-height:1.6}
.mbtns{display:flex;gap:8px}
.note{font-size:10px;color:var(--dim);margin-top:10px;line-height:1.7}
</style></head>
<body>
<div class="hdr">
  <div class="logo">🐋 WHALE BOT <span>v4</span> <span id="mode-badge" style="margin-left:6px;padding:2px 7px;border-radius:10px;font-size:9px;font-weight:700"></span></div>
  <div class="hdr-r">
    <span class="clk" id="clk">--:--:--</span>
    <span class="mbadge mc" id="mbadge">MARKET CLOSED</span>
  </div>
</div>
<div class="wrap">
  <div class="g4">
    <div class="card">
      <div class="ct">Portfolio Value</div>
      <div class="sv" id="equity">—</div>
      <div class="ss" id="pnl-sub">—</div>
    </div>
    <div class="card">
      <div class="ct">Today's P&L</div>
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
      <div class="ss" id="dt-sub">Max 2 (keeping 1 reserve)</div>
    </div>
  </div>
  <div class="g2">
    <div class="card">
      <div class="ct">Open Positions</div>
      <table>
        <thead><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Entry</th><th>Current</th><th>P&L</th><th></th></tr></thead>
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
        <input class="pw-in" type="password" id="pw" maxlength="20" placeholder="Dashboard password">
      </div>
      <div class="btns">
        <button class="btn bp" id="btn-pause"  onclick="ctrl('pause')">⏸  PAUSE BOT</button>
        <button class="btn br" id="btn-resume" onclick="ctrl('resume')" disabled>▶  RESUME BOT</button>
        <button class="btn bs" onclick="showModal()">🛑  EMERGENCY STOP</button>
      </div>
      <div class="note">
        <b style="color:var(--text)">PAUSE</b> stops new trades, holds positions<br>
        <b style="color:var(--text)">EMERGENCY STOP</b> closes everything instantly
      </div>
    </div>
  </div>
  <div class="g21">
    <div class="card">
      <div class="ct">Live Bot Activity</div>
      <div class="logbox" id="logbox">
        <div class="li info"><span class="lt">--:--</span><span class="lm">Connecting to bot...</span></div>
      </div>
    </div>
    <div class="card">
      <div class="ct">Today's Trades</div>
      <div id="trades-panel" style="font-family:var(--mono);font-size:11px">
        <div style="color:var(--dim);padding:10px 0">No trades today</div>
      </div>
    </div>
  </div>
  <div class="card" style="margin-bottom:12px">
    <div class="ct">Open Orders</div>
    <table>
      <thead><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Type</th><th>Status</th><th></th></tr></thead>
      <tbody id="ord-tb"><tr><td colspan="6" class="empty">No open orders</td></tr></tbody>
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
// ── CLOCK ──────────────────────────────────────────────────────
function tick(){
  const t=new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date());
  document.getElementById('clk').textContent=t+' ET';
}
setInterval(tick,1000); tick();

// ── HELPERS ────────────────────────────────────────────────────
const pw=()=>document.getElementById('pw').value.trim();
const fmt=n=>{const v=parseFloat(n||0);return(v>=0?'+':'')+'$'+Math.abs(v).toFixed(2)};
const fv=n=>'$'+parseFloat(n||0).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const pc=v=>parseFloat(v||0)>=0?'g':'r';

async function api(url,method='GET',body=null){
  try{
    const r=await fetch(url,{method,headers:{'Content-Type':'application/json','X-Token':pw()},body:body?JSON.stringify(body):null});
    return await r.json();
  }catch(e){console.error(e);return{};}
}

// ── MODALS ─────────────────────────────────────────────────────
function showModal(){document.getElementById('modal').classList.add('show')}
function closeModal(){document.getElementById('modal').classList.remove('show')}

// ── CONTROLS ───────────────────────────────────────────────────
async function ctrl(action){
  if(!pw()){alert('Enter your dashboard password first');return;}
  const r=await api('/api/'+action,'POST');
  if(r.error){alert('Wrong password');return;}
  setTimeout(load,500);
}
async function doStop(){
  closeModal();
  if(!pw()){alert('Enter your dashboard password first');return;}
  const r=await api('/api/estop','POST');
  if(r.error){alert('Wrong password');return;}
  setTimeout(load,500);
}
async function closePos(sym){
  if(!confirm('Close position: '+sym+'?'))return;
  if(!pw()){alert('Enter password first');return;}
  await api('/api/close/'+sym,'POST');
  setTimeout(load,500);
}

// ── MAIN LOAD ──────────────────────────────────────────────────
async function load(){
  const d=await api('/api/data');
  if(!d||d.error)return;

  // Live/Paper mode badge — critical safety indicator
  const mb=document.getElementById('mode-badge');
  if(d.live_mode){
    mb.textContent='🔴 LIVE';
    mb.style.background='rgba(255,64,96,.15)';
    mb.style.color='var(--red)';
    mb.style.border='1px solid var(--red)';
  } else {
    mb.textContent='📝 PAPER';
    mb.style.background='rgba(61,158,255,.1)';
    mb.style.color='var(--blue)';
    mb.style.border='1px solid var(--blue)';
  }

  // Portfolio
  document.getElementById('equity').textContent=fv(d.equity);
  const ps=document.getElementById('pnl-sub');
  const pv=parseFloat(d.pnl||0);
  ps.innerHTML='Today: <span class="'+pc(pv)+'">'+fmt(pv)+'</span>';

  // P&L bar
  const dp=document.getElementById('dpnl');
  const dpv=parseFloat(d.daily_pnl||0);
  dp.textContent=fmt(dpv); dp.className='sv '+(dpv>=0?'g':'r');
  const gp=Math.min(100,Math.max(0,dpv/(d.goal||100)*100));
  document.getElementById('gfill').style.width=gp+'%';
  document.getElementById('goal-lbl').textContent='$'+(d.goal||100)+' goal';

  // Win/Loss
  document.getElementById('wl').textContent=(d.wins||0)+'W / '+(d.losses||0)+'L';
  document.getElementById('wr-sub').textContent='All-time win rate: '+(d.win_rate||0)+'%';

  // Day trades
  const dtEl=document.getElementById('dt');
  const dtSub=document.getElementById('dt-sub');
  dtEl.textContent=(d.dt_used||0)+' / '+(d.dt_max||2);
  dtEl.className='sv '+((d.dt_used||0)>=(d.dt_max||2)?'r':'g');
  if(d.pdt_flagged){dtSub.textContent='⚠️ PDT FLAGGED — swing trades only';dtSub.style.color='var(--red)';}
  else if((d.dt_used||0)>=(d.dt_max||2)){dtSub.textContent='Limit reached — swing only';dtSub.style.color='var(--yellow)';}
  else{dtSub.textContent='Max '+(d.dt_max||2)+' day trades';dtSub.style.color='';}

  // Market
  const mb=document.getElementById('mbadge');
  if(d.market_open){mb.className='mbadge mo';mb.textContent='MARKET OPEN';}
  else{mb.className='mbadge mc';const no=d.next_open?new Date(d.next_open).toLocaleString('en-US',{timeZone:'America/New_York',weekday:'short',hour:'2-digit',minute:'2-digit'}):'';mb.textContent=no?'Opens '+no:'MARKET CLOSED';}

  // Bot status
  const dot=document.getElementById('sdot'),st=document.getElementById('stxt');
  const bp=document.getElementById('btn-pause'),br=document.getElementById('btn-resume');
  if(d.e_stop){dot.className='dot stp';st.className='stxt r';st.textContent='EMERGENCY STOP';bp.disabled=true;br.disabled=false;}
  else if(d.paused){dot.className='dot psd';st.className='stxt w';st.textContent='PAUSED';bp.disabled=true;br.disabled=false;}
  else{dot.className='dot run';st.className='stxt g';st.textContent='RUNNING';bp.disabled=false;br.disabled=true;}

  // Positions
  const pt=document.getElementById('pos-tb');
  if(!d.positions||!d.positions.length){pt.innerHTML='<tr><td colspan="7" class="empty">No open positions</td></tr>';}
  else pt.innerHTML=d.positions.map(p=>`<tr>
    <td><b>${p.symbol}</b></td>
    <td class="${p.side==='long'?'g':'r'}">${(p.side||'').toUpperCase()}</td>
    <td>${p.qty}</td>
    <td>$${parseFloat(p.avg_entry_price||0).toFixed(2)}</td>
    <td>$${parseFloat(p.current_price||0).toFixed(2)}</td>
    <td class="${pc(p.unrealized_pl)}">${fmt(p.unrealized_pl)} (${(parseFloat(p.unrealized_plpc||0)*100).toFixed(1)}%)</td>
    <td><button class="cbtn" onclick="closePos('${p.symbol}')">CLOSE</button></td>
  </tr>`).join('');

  // Orders
  const ot=document.getElementById('ord-tb');
  if(!d.orders||!d.orders.length){ot.innerHTML='<tr><td colspan="6" class="empty">No open orders</td></tr>';}
  else ot.innerHTML=d.orders.map(o=>`<tr>
    <td><b>${o.symbol}</b></td>
    <td class="${o.side==='buy'?'g':'r'}">${(o.side||'').toUpperCase()}</td>
    <td>${o.qty}</td>
    <td>${(o.type||'').toUpperCase()}</td>
    <td>${o.status||''}</td>
    <td></td>
  </tr>`).join('');

  // Today's trades
  const tp=document.getElementById('trades-panel');
  if(d.trades&&d.trades.length){
    tp.innerHTML=d.trades.map(t=>`
      <div style="display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px solid var(--bdr)">
        <span class="${t.side==='buy'?'g':'r'}">${(t.side||'').toUpperCase()} <b>${t.sym}</b></span>
        <span>$${parseFloat(t.price||0).toFixed(2)}</span>
        <span style="color:var(--dim);font-size:10px">${(t.time||'').slice(11)}</span>
      </div>`).join('');
  } else {
    tp.innerHTML='<div style="color:var(--dim);padding:10px 0;font-size:11px">No trades today</div>';
  }
}

// ── LOG FEED — reads actual bot log file ───────────────────────
async function loadLogs(){
  try{
    const r=await fetch('/api/logs');
    const d=await r.json();
    if(!d.lines||!d.lines.length)return;
    const box=document.getElementById('logbox');
    box.innerHTML=d.lines.slice().reverse().map(line=>{
      let cls='info';
      if(line.includes('WIN')||line.includes('profit')||line.includes('✅'))cls='success';
      else if(line.includes('LOSS')||line.includes('failed')||line.includes('❌'))cls='error';
      else if(line.includes('⚠️')||line.includes('PDT')||line.includes('BLOCKED')||line.includes('🐋')||line.includes('🚨'))cls='warning';
      // Extract timestamp (HH:MM from log line)
      const tm=line.match(/([0-9]{2}:[0-9]{2}):[0-9]{2}/);
      const t=tm?tm[1]:'';
      const msg=line.replace(/^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} /,'');
      return `<div class="li ${cls}"><span class="lt">${t}</span><span class="lm">${msg}</span></div>`;
    }).join('');
  }catch(e){console.error('Log error:',e);}
}

// ── START ──────────────────────────────────────────────────────
load();
loadLogs();
setInterval(load,    10000);
setInterval(loadLogs, 8000);
</script>
</body></html>"""

# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
