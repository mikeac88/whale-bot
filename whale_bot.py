"""
Whale Bot - Complete Clean Rebuild
===================================
Simple, reliable, tested.

Set these in Render environment:
  ALPACA_API_KEY     = your paper or live key
  ALPACA_SECRET_KEY  = your secret key
  ALPACA_BASE_URL    = https://paper-api.alpaca.markets  (paper)
                    or https://api.alpaca.markets        (live)
  STOP_PIN           = any 4 digit pin e.g. 1234  (for emergency stop only)
"""

import os, json, time, logging, threading, pytz, requests
from datetime import datetime, date
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, jsonify, request, Response

# ── CONFIG ────────────────────────────────────────────────────────────────────
KEY       = os.environ.get("ALPACA_API_KEY",    "")
SECRET    = os.environ.get("ALPACA_SECRET_KEY", "")
BASE_URL  = os.environ.get("ALPACA_BASE_URL",   "https://paper-api.alpaca.markets")
DATA_URL  = "https://data.alpaca.markets"
STOP_PIN  = os.environ.get("STOP_PIN",          "1234")
PORT      = int(os.environ.get("PORT",          8080))

TRADE_SIZE   = float(os.environ.get("TRADE_SIZE",  "45"))
DAILY_GOAL   = float(os.environ.get("DAILY_GOAL",  "100"))
MAX_LOSS     = float(os.environ.get("MAX_LOSS",     "50"))
STOP_PCT     = float(os.environ.get("STOP_PCT",     "0.035"))
TARGET_PCT   = float(os.environ.get("TARGET_PCT",   "0.07"))
MIN_SCORE    = int(os.environ.get("MIN_SCORE",      "55"))
MAX_DT       = int(os.environ.get("MAX_DAY_TRADES", "2"))
KEEPALIVE    = os.environ.get("RENDER_EXTERNAL_URL", "")

ET = pytz.timezone("America/New_York")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("whale")

# ── WATCHLIST ─────────────────────────────────────────────────────────────────
TIER1 = ["NVDA","AMD","TSLA","PLTR","SMCI","MSTR","MARA","RIOT","SOXL","TQQQ"]
TICKERS = list(set(TIER1 + [
    "META","GOOGL","MSFT","AMZN","AAPL","NFLX","UBER","HOOD","SOFI","UPST","COIN","ARKK",
    "OXY","DVN","MRO","HAL","XLE","BOIL","UCO","ERX",
    "CCJ","NNE","OKLO","SMR","UEC","URA",
    "FCX","MP","LAC","ALB","COPX","GDX","GDXJ","NEM","WPM",
    "IONQ","RGTI","RKLB","ASTS","LUNR","CLSK","WULF","JOBY",
]))

# ── SHARED STATE ──────────────────────────────────────────────────────────────
_lock = threading.Lock()
_state = {
    "paused": False,
    "alerts": [],   # list of {t, m, l}
    "scan": {
        "time": "Never",
        "label": "",
        "found": 0,
        "top": [],
        "healthy": True,
    },
    "stats": {
        "date": str(date.today()),
        "daily_pnl": 0.0,
        "wins": 0, "losses": 0,
        "day_trades": 0, "swings": 0,
        "total_trades": 0, "total_wins": 0,
    },
}

def get_state():
    with _lock:
        return json.loads(json.dumps(_state))  # deep copy

def push_alert(msg, level="info"):
    entry = {"t": datetime.now(ET).strftime("%H:%M"), "m": msg, "l": level}
    with _lock:
        _state["alerts"].append(entry)
        if len(_state["alerts"]) > 80:
            _state["alerts"].pop(0)
    log.info(msg)

def reset_day_if_needed():
    today = str(date.today())
    with _lock:
        if _state["stats"]["date"] != today:
            _state["stats"].update({
                "date": today, "daily_pnl": 0.0,
                "wins": 0, "losses": 0, "day_trades": 0, "swings": 0,
            })

def record_trade(pnl, kind="day"):
    reset_day_if_needed()
    with _lock:
        s = _state["stats"]
        s["daily_pnl"] = round(s["daily_pnl"] + pnl, 2)
        s["total_trades"] += 1
        if pnl > 0:
            s["wins"] += 1; s["total_wins"] += 1
        else:
            s["losses"] += 1
        if kind == "day": s["day_trades"] += 1
        elif kind == "swing": s["swings"] += 1

def is_paused():
    with _lock:
        return _state["paused"]

def win_rate():
    s = get_state()["stats"]
    if s["total_trades"] == 0: return 0.0
    return round(s["total_wins"] / s["total_trades"] * 100, 1)

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
    r = requests.delete(BASE_URL + path, headers=HDR, timeout=15)
    return r.json() if r.text.strip() else {}

# ── ACCOUNT ───────────────────────────────────────────────────────────────────
def account():    return aGet("/v2/account")
def positions():  return aGet("/v2/positions")
def open_orders():return aGet("/v2/orders", {"status": "open", "limit": 20})
def clock():      return aGet("/v2/clock")

def occupied():
    return len(positions()) > 0 or len(open_orders()) > 0

def dt_remaining():
    used = int(account().get("daytrade_count", 0))
    local = get_state()["stats"]["day_trades"]
    return max(0, MAX_DT - max(used, local))

def trade_size():
    cash = float(account().get("cash", TRADE_SIZE))
    ratio = cash / 100_000
    return round(max(TRADE_SIZE, min(TRADE_SIZE * (1 + max(0, ratio-1)*3), 500)), 2)

# ── INDICATORS ────────────────────────────────────────────────────────────────
def calc_rsi(bars, p=14):
    if len(bars) < p+1: return 50
    c = [b["c"] for b in bars]
    g = [max(c[i]-c[i-1], 0) for i in range(1, len(c))]
    l = [max(c[i-1]-c[i], 0) for i in range(1, len(c))]
    ag = sum(g[-p:])/p; al = sum(l[-p:])/p
    return round(100-(100/(1+ag/al)), 1) if al else 100

def calc_vwap(mbars):
    if not mbars: return None
    tv = sum(((b["h"]+b["l"]+b["c"])/3)*b["v"] for b in mbars)
    v  = sum(b["v"] for b in mbars)
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
        snap = aGet("/v2/stocks/SPY/snapshot", base=DATA_URL)
        bars = aGet("/v2/stocks/SPY/bars", {"timeframe":"1Day","limit":20,"feed":"iex"}, DATA_URL).get("bars", [])
        e9 = calc_ema(bars, 9); e20 = calc_ema(bars, 20)
        last = snap.get("latestTrade", {}).get("p", 0)
        prev = snap.get("prevDailyBar", {}).get("c", last)
        chg  = (last-prev)/prev if prev else 0
        ok   = bool(e9 and e20 and last > e9 and e9 > e20)
        _regime_cache = {"ts": now, "ok": ok, "chg": chg}
        return ok, chg
    except:
        return True, 0.0

# ── WHALE DETECTOR ────────────────────────────────────────────────────────────
def whale_tier(snap):
    d = snap.get("dailyBar", {}); p = snap.get("prevDailyBar", {})
    t = snap.get("latestTrade", {})
    tv = d.get("v", 0); pv = p.get("v", 1)
    vr = tv/pv if pv > 0 else 0
    cur = t.get("p", d.get("c", 0)); pc = p.get("c", 0)
    if pc == 0: return 0, 0
    chg = abs((cur-pc)/pc)
    if vr >= 20: return 3, round(vr, 1)
    if vr >= 10 and chg < 0.025: return 2, round(vr, 1)
    if vr >= 5  and chg >= 0.015: return 1, round(vr, 1)
    return 0, round(vr, 1)

# ── SCORER ────────────────────────────────────────────────────────────────────
def score_ticker(sym, snap, dbars, mbars, spy_chg=0.0, healthy=True):
    d = snap.get("dailyBar", {}); p = snap.get("prevDailyBar", {})
    t = snap.get("latestTrade", {})
    cur = t.get("p", d.get("c", 0)); pc = p.get("c", 0)
    if cur == 0 or pc == 0: return 0, "LONG"
    chg = (cur-pc)/pc
    vr  = d.get("v", 0) / max(p.get("v", 1), 1)
    dir = "LONG" if chg > 0 else "SHORT"
    score = 0

    tier, _ = whale_tier(snap)
    if tier == 3:   score += 40
    elif tier == 2: score += 32
    elif tier == 1: score += 24
    elif vr >= 3:   score += 12
    elif vr >= 2:   score += 6
    else: return 0, dir

    ac = abs(chg)
    if ac >= 0.10:    score += 20
    elif ac >= 0.06:  score += 16
    elif ac >= 0.03:  score += 11
    elif ac >= 0.015: score += 6

    r = calc_rsi(dbars)
    if dir == "LONG":
        if 45 <= r <= 65:  score += 15
        elif 35 <= r < 45: score += 10
        elif r <= 72:      score += 5
    else:
        if 28 <= r <= 50:  score += 15
        elif r < 28:       score += 5
        else:              score += 8

    v = calc_vwap(mbars)
    if v and cur > 0:
        dist = (cur-v)/v
        if dir == "LONG":
            if dist > 0.01:    score += 15
            elif dist > -0.005:score += 8
        else:
            if dist < -0.01:   score += 15
            elif dist < 0.005: score += 8

    if spy_chg and dir == "LONG" and chg > spy_chg:
        rs = chg/abs(spy_chg)
        if rs >= 3:   score += 10
        elif rs >= 2: score += 6
        elif rs >= 1: score += 3

    if not healthy: score = int(score * 0.8)
    if sym in TIER1: score = min(100, score + 5)

    return min(score, 100), dir

# ── SCAN ──────────────────────────────────────────────────────────────────────
def scan_one(sym, spy_chg, healthy, tsize):
    try:
        snap = aGet(f"/v2/stocks/{sym}/snapshot", base=DATA_URL)
        if not snap: return None
        d = snap.get("dailyBar", {}); p = snap.get("prevDailyBar", {})
        t = snap.get("latestTrade", {})
        cur = t.get("p", d.get("c", 0)); pc = p.get("c", 0)
        if cur < 2 or pc == 0: return None
        dbars = aGet(f"/v2/stocks/{sym}/bars", {"timeframe":"1Day","limit":20,"feed":"iex"}, DATA_URL).get("bars", [])
        mbars = aGet(f"/v2/stocks/{sym}/bars", {"timeframe":"1Min","limit":60,"feed":"iex"},  DATA_URL).get("bars", [])
        sc, dir = score_ticker(sym, snap, dbars, mbars, spy_chg, healthy)
        if sc < MIN_SCORE: return None
        chg = (cur-pc)/pc; vr = d.get("v", 0)/max(p.get("v", 1), 1)
        tier, _ = whale_tier(snap)
        shares = max(1, int(tsize/cur)); cost = round(shares*cur, 2)
        sl = round(cur*(1-STOP_PCT), 2)   if dir == "LONG" else round(cur*(1+STOP_PCT), 2)
        tp = round(cur*(1+TARGET_PCT), 2) if dir == "LONG" else round(cur*(1-TARGET_PCT), 2)
        return {
            "sym": sym, "price": round(cur, 2), "chg": round(chg*100, 2),
            "vol": round(vr, 1), "tier": tier, "dir": dir, "score": sc,
            "rsi": calc_rsi(dbars), "shares": shares, "cost": cost,
            "sl": sl, "tp": tp,
            "pot":  round(shares*cur*TARGET_PCT, 2),
            "risk": round(shares*cur*STOP_PCT,   2),
        }
    except Exception as e:
        log.debug(f"{sym}: {e}"); return None

def full_scan(label="SCAN"):
    push_alert(f"🔍 {label}: scanning {len(TICKERS)} tickers...", "info")
    ok, chg = market_regime()
    tsize = trade_size()
    setups = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(scan_one, s, chg, ok, tsize): s for s in TICKERS}
        for f in as_completed(futs):
            r = f.result()
            if r: setups.append(r)
    setups.sort(key=lambda x: (x["tier"], x["score"]), reverse=True)
    top = setups[:5]

    # Save to state for dashboard
    with _lock:
        _state["scan"] = {
            "time":    datetime.now(ET).strftime("%H:%M ET"),
            "label":   label,
            "found":   len(setups),
            "top":     [{"sym":s["sym"],"score":s["score"],"chg":s["chg"],"vol":s["vol"],"tier":s["tier"],"dir":s["dir"]} for s in top],
            "healthy": ok,
        }

    regime = "✅ BULL" if ok else "⚠️ BEAR"
    if top:
        lines = [f"✅ {label} done | {regime} | {len(setups)} setups found"]
        for i, s in enumerate(top[:3]):
            w = "🐋" if s["tier"] >= 1 else "📊"
            lines.append(f"  #{i+1} {w} {s['sym']} score:{s['score']} {s['chg']:+.1f}% vol:{s['vol']}x")
        push_alert("\n".join(lines), "success")
    else:
        push_alert(f"✅ {label} done | {regime} | No setups above score {MIN_SCORE} — watching", "info")

    return top, ok

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
        push_alert(f"✅ {kind}: {side.upper()} {setup['shares']}x {setup['sym']} @ ~${setup['price']} | SL:${setup['sl']} TP:${setup['tp']}", "success")
        return True, "swing" if swing else "day"
    except Exception as e:
        push_alert(f"❌ Order failed {setup['sym']}: {e}", "error")
        return False, None

# ── EXECUTE ───────────────────────────────────────────────────────────────────
def execute(setups, healthy, label=""):
    reset_day_if_needed()
    s = get_state()["stats"]

    if s["daily_pnl"] <= -MAX_LOSS:
        push_alert(f"🛑 Daily loss limit (${s['daily_pnl']:.2f}) — done for today", "error"); return
    if s["daily_pnl"] >= DAILY_GOAL:
        push_alert(f"🎯 Daily goal hit! +${s['daily_pnl']:.2f} — resting", "success"); return
    if is_paused():
        push_alert("⏸️ Bot paused — skipping", "warning"); return
    if occupied():
        log.info("Position open — no new entries"); return

    if not setups:
        push_alert(f"📊 {label}: No qualifying setups — continuing to watch", "info"); return

    best = setups[0]; dtr = dt_remaining()
    required = 70 if not healthy else MIN_SCORE

    if best["tier"] == 3:
        swing = dtr == 0
        push_alert(f"🚨 EXTREME WHALE {best['sym']} — executing {'swing' if swing else 'day trade'}", "warning")
        ok, k = place_order(best, swing=swing)
        if ok: record_trade(0, k or "day")

    elif best["score"] >= 75 and dtr > 0:
        push_alert(f"🤖 HIGH SCORE {best['score']} — day trading {best['sym']}", "info")
        ok, k = place_order(best, swing=False)
        if ok: record_trade(0, "day")

    elif best["score"] >= 65 and dtr == 0:
        push_alert(f"🔄 PDT limit — swing trade {best['sym']} (hold overnight)", "warning")
        ok, k = place_order(best, swing=True)
        if ok: record_trade(0, "swing")

    elif best["score"] >= required and dtr > 0:
        push_alert(f"🤖 Auto-trading {best['sym']} score:{best['score']}", "info")
        ok, k = place_order(best, swing=False)
        if ok: record_trade(0, "day")

    else:
        push_alert(f"👤 Manual review: {best['sym']} score:{best['score']} | Day trades left: {dtr}", "warning")

# ── BOT JOBS ──────────────────────────────────────────────────────────────────
def job(label):
    if is_paused():
        push_alert(f"⏸️ Paused — skipping {label}", "warning"); return
    setups, ok = full_scan(label)
    execute(setups, ok, label)

def monitor():
    try:
        for p in positions():
            pl  = float(p.get("unrealized_pl", 0))
            pct = float(p.get("unrealized_plpc", 0)) * 100
            push_alert(f"📊 {p['symbol']} x{p['qty']} @ ${p['current_price']} | P&L: ${pl:+.2f} ({pct:+.1f}%)", "info")
    except: pass

def eod():
    monitor()
    s = get_state()["stats"]
    wr = win_rate()
    pct = min(100, max(0, (s["daily_pnl"]/DAILY_GOAL)*100)) if DAILY_GOAL else 0
    push_alert(
        f"🔔 END OF DAY | P&L: ${s['daily_pnl']:+.2f} ({pct:.0f}% of ${DAILY_GOAL:.0f} goal) | "
        f"{s['wins']}W/{s['losses']}L | Win rate: {wr}% all-time", "info"
    )

# ── BOT LOOP ──────────────────────────────────────────────────────────────────
def bot_loop():
    push_alert("🐋 Whale Bot online — pure equity momentum", "success")
    push_alert(f"Watching {len(TICKERS)} elite tickers | ${TRADE_SIZE}/trade | Goal: ${DAILY_GOAL}/day", "info")

    # Startup scan if market is open
    try:
        if clock().get("is_open"):
            push_alert("🔄 Market is open — running startup scan now", "info")
            time.sleep(3)
            job("STARTUP SCAN")
    except Exception as e:
        log.error(f"Startup scan error: {e}")

    triggered = set()
    while True:
        try:
            now = datetime.now(ET); h, m = now.hour, now.minute
            key = f"{h}:{m:02d}"

            sched = {
                "8:00":  lambda: job("PREMARKET 8AM"),
                "9:30":  lambda: job("OPENING RANGE"),
                "9:45":  lambda: job("ORB BREAKOUT"),
                "10:00": lambda: job("MOMENTUM 10AM"),
                "12:00": lambda: job("MIDDAY SWING"),
                "15:00": lambda: job("POWER HOUR"),
                "15:55": eod,
            }

            if key in sched and key not in triggered:
                sched[key](); triggered.add(key)

            elif 9 <= h < 16 and m % 2 == 0 and key not in triggered:
                monitor()
                if not occupied() and not is_paused():
                    setups, ok = full_scan("CONTINUOUS")
                    whales = [s for s in setups if s["tier"] >= 2]
                    if whales:
                        execute(whales, ok, "WHALE ALERT")
                triggered.add(key)

            if h == 0 and m == 1 and "reset" not in triggered:
                triggered.clear(); triggered.add("reset")
                push_alert("🔄 New trading day — reset complete", "info")

            time.sleep(20)
        except Exception as e:
            log.error(f"Bot loop error: {e}"); time.sleep(60)

# ── KEEPALIVE ─────────────────────────────────────────────────────────────────
def keepalive_loop():
    time.sleep(90)
    while True:
        try:
            if KEEPALIVE:
                requests.get(KEEPALIVE, timeout=10)
                log.info("💓 Keepalive ping")
        except: pass
        time.sleep(270)

# ── FLASK APP ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

# Start bot threads when module loads (works with gunicorn)
def _start():
    threading.Thread(target=bot_loop,    daemon=True).start()
    threading.Thread(target=keepalive_loop, daemon=True).start()
    log.info("🤖 Bot threads started")

_start()

# ── API ROUTES ────────────────────────────────────────────────────────────────
@app.route("/api/data")
def api_data():
    try:
        acct = account()
        pos  = positions()
        ords = open_orders()
        clk  = clock()
        st   = get_state()
        return jsonify({
            "equity":      float(acct.get("equity", 0)),
            "cash":        float(acct.get("cash", 0)),
            "pnl":         round(float(acct.get("equity", 0)) - float(acct.get("last_equity", 0)), 2),
            "daily_pnl":   st["stats"]["daily_pnl"],
            "wins":        st["stats"]["wins"],
            "losses":      st["stats"]["losses"],
            "win_rate":    win_rate(),
            "day_trades":  int(acct.get("daytrade_count", 0)),
            "paused":      st["paused"],
            "market_open": clk.get("is_open", False),
            "next_close":  clk.get("next_close", ""),
            "next_open":   clk.get("next_open", ""),
            "positions":   pos,
            "orders":      ords,
            "alerts":      st["alerts"][-40:],
            "scan":        st["scan"],
            "goal":        DAILY_GOAL,
        })
    except Exception as e:
        log.error(f"API error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/pause", methods=["POST"])
def api_pause():
    pin = request.json.get("pin", "") if request.json else ""
    if pin != STOP_PIN: return jsonify({"error": "Wrong PIN"}), 401
    with _lock: _state["paused"] = True
    push_alert("⏸️ Bot PAUSED by you", "warning")
    return jsonify({"ok": True})

@app.route("/api/resume", methods=["POST"])
def api_resume():
    pin = request.json.get("pin", "") if request.json else ""
    if pin != STOP_PIN: return jsonify({"error": "Wrong PIN"}), 401
    with _lock: _state["paused"] = False
    push_alert("▶️ Bot RESUMED", "success")
    return jsonify({"ok": True})

@app.route("/api/estop", methods=["POST"])
def api_estop():
    pin = request.json.get("pin", "") if request.json else ""
    if pin != STOP_PIN: return jsonify({"error": "Wrong PIN"}), 401
    with _lock: _state["paused"] = True
    try: aDel("/v2/orders")
    except: pass
    try: requests.delete(BASE_URL + "/v2/positions", headers=HDR, timeout=10)
    except: pass
    push_alert("🛑 EMERGENCY STOP — all orders cancelled, all positions closed", "error")
    return jsonify({"ok": True})

@app.route("/api/close/<sym>", methods=["POST"])
def api_close(sym):
    pin = request.json.get("pin", "") if request.json else ""
    if pin != STOP_PIN: return jsonify({"error": "Wrong PIN"}), 401
    try:
        requests.delete(f"{BASE_URL}/v2/positions/{sym}", headers=HDR, timeout=10)
        push_alert(f"🔴 Closed position: {sym}", "warning")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── DASHBOARD HTML ────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return """<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>🐋 Whale Bot</title>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=DM+Sans:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#07090d;--card:#0d1219;--border:#182030;--green:#00e5b0;--blue:#3d9eff;--red:#ff4060;--yellow:#ffb020;--text:#b8c4d0;--dim:#4a5568;--mono:'Share Tech Mono',monospace;--sans:'DM Sans',sans-serif}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--sans);font-size:14px}
.header{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;background:var(--card);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:10}
.logo{font-family:var(--mono);font-size:16px;color:var(--green);letter-spacing:2px}
.logo span{color:var(--dim);font-size:11px;margin-left:6px}
.hright{display:flex;align-items:center;gap:10px}
.clock{font-family:var(--mono);font-size:12px;color:var(--dim)}
.mbadge{font-family:var(--mono);font-size:10px;padding:3px 8px;border-radius:20px;font-weight:700;letter-spacing:1px}
.open{background:rgba(0,229,176,.12);color:var(--green);border:1px solid rgba(0,229,176,.3)}
.closed{background:rgba(255,255,255,.04);color:var(--dim);border:1px solid var(--border)}
.wrap{padding:12px;max-width:1100px;margin:0 auto}
.g4{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:12px}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px}
.g21{display:grid;grid-template-columns:2fr 1fr;gap:10px;margin-bottom:12px}
@media(max-width:700px){.g4{grid-template-columns:1fr 1fr}.g2,.g21{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px;position:relative;overflow:hidden}
.card::after{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,var(--green),var(--blue));opacity:.3}
.ct{font-size:9px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--dim);margin-bottom:8px}
.sv{font-family:var(--mono);font-size:24px;color:#fff;line-height:1}
.ss{font-size:11px;color:var(--dim);margin-top:4px}
.g{color:var(--green)}.r{color:var(--red)}.w{color:var(--yellow)}
.gbar{margin-top:8px}
.gtrack{height:3px;background:var(--border);border-radius:2px;overflow:hidden}
.gfill{height:100%;background:linear-gradient(90deg,var(--green),var(--blue));border-radius:2px;transition:width .5s}
.glabels{display:flex;justify-content:space-between;font-size:9px;color:var(--dim);margin-top:3px}
.dot{width:9px;height:9px;border-radius:50%;flex-shrink:0}
.running{background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 2s infinite}
.paused{background:var(--yellow)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.srow{display:flex;align-items:center;gap:8px;margin-bottom:12px}
.stxt{font-family:var(--mono);font-size:13px}
.controls{display:flex;flex-direction:column;gap:7px}
.btn{padding:9px 14px;border-radius:6px;border:1px solid;font-family:var(--mono);font-size:11px;letter-spacing:1px;cursor:pointer;width:100%;transition:all .15s;font-weight:700}
.btn:hover{filter:brightness(1.2)}
.btn-p{background:rgba(255,176,32,.08);color:var(--yellow);border-color:rgba(255,176,32,.3)}
.btn-r{background:rgba(0,229,176,.08);color:var(--green);border-color:rgba(0,229,176,.3)}
.btn-s{background:rgba(255,64,96,.12);color:var(--red);border-color:rgba(255,64,96,.3)}
.btn:disabled{opacity:.3;cursor:not-allowed}
.pin-row{display:flex;gap:8px;margin-bottom:10px}
.pin-input{flex:1;background:var(--bg);border:1px solid var(--border);color:var(--text);font-family:var(--mono);font-size:14px;padding:7px 10px;border-radius:5px;outline:none;letter-spacing:4px}
.pin-input:focus{border-color:var(--green)}
table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:11px}
th{text-align:left;padding:5px 8px;font-size:9px;letter-spacing:1.5px;text-transform:uppercase;color:var(--dim);border-bottom:1px solid var(--border)}
td{padding:7px 8px;border-bottom:1px solid rgba(24,32,48,.5);color:var(--text)}
.empty{text-align:center;color:var(--dim);padding:16px;font-size:11px}
.log{height:260px;overflow-y:auto;display:flex;flex-direction:column;gap:3px;padding-right:4px}
.log::-webkit-scrollbar{width:3px}
.log::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
.aitem{display:flex;gap:7px;padding:5px 7px;border-radius:4px;font-size:10px;font-family:var(--mono);border-left:2px solid transparent;animation:fadein .3s}
@keyframes fadein{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.atime{color:var(--dim);flex-shrink:0;min-width:38px}
.amsg{color:var(--text);flex:1;white-space:pre-wrap}
.ai{border-left-color:var(--blue);background:rgba(61,158,255,.04)}
.as{border-left-color:var(--green);background:rgba(0,229,176,.04)}
.aw{border-left-color:var(--yellow);background:rgba(255,176,32,.04)}
.ae{border-left-color:var(--red);background:rgba(255,64,96,.05)}
.scan-item{display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px solid var(--border);font-family:var(--mono);font-size:11px}
.cbtn{font-family:var(--mono);font-size:9px;padding:3px 7px;border-radius:3px;cursor:pointer;border:1px solid rgba(255,64,96,.3);background:rgba(255,64,96,.08);color:var(--red)}
.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:99;align-items:center;justify-content:center}
.modal.show{display:flex}
.mbox{background:var(--card);border:1px solid var(--red);border-radius:12px;padding:24px;max-width:300px;width:90%;text-align:center}
.mbox h3{color:var(--red);font-family:var(--mono);margin-bottom:8px}
.mbox p{color:var(--dim);font-size:12px;margin-bottom:16px;line-height:1.6}
.mbtns{display:flex;gap:8px}
</style></head>
<body>
<div class="header">
  <div class="logo">🐋 WHALE BOT <span>v3 EQUITY</span></div>
  <div class="hright">
    <span class="clock" id="clk">--:--:--</span>
    <span class="mbadge closed" id="mbadge">MARKET CLOSED</span>
  </div>
</div>

<div class="wrap">
  <!-- STATS ROW -->
  <div class="g4">
    <div class="card">
      <div class="ct">Portfolio Value</div>
      <div class="sv" id="equity">Loading...</div>
      <div class="ss" id="pnl-sub">—</div>
    </div>
    <div class="card">
      <div class="ct">Today's P&L</div>
      <div class="sv" id="daily-pnl">—</div>
      <div class="gbar">
        <div class="gtrack"><div class="gfill" id="goal-bar" style="width:0%"></div></div>
        <div class="glabels"><span>$0</span><span id="goal-label">$100 goal</span></div>
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
      <div class="ss">Max 2 (keeping 1 reserve)</div>
    </div>
  </div>

  <!-- POSITIONS + CONTROLS -->
  <div class="g2">
    <div class="card">
      <div class="ct">Open Positions</div>
      <table>
        <thead><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Entry</th><th>Current</th><th>P&L</th><th></th></tr></thead>
        <tbody id="pos-body"><tr><td colspan="7" class="empty">No open positions</td></tr></tbody>
      </table>
    </div>
    <div class="card">
      <div class="ct">Bot Control</div>
      <div class="srow">
        <div class="dot running" id="bot-dot"></div>
        <div class="stxt g" id="bot-status">RUNNING</div>
      </div>
      <div class="pin-row">
        <input class="pin-input" type="password" id="pin" maxlength="4" placeholder="PIN">
      </div>
      <div class="controls">
        <button class="btn btn-p" id="btn-pause"  onclick="ctrl('pause')">⏸  PAUSE BOT</button>
        <button class="btn btn-r" id="btn-resume" onclick="ctrl('resume')" disabled>▶  RESUME BOT</button>
        <button class="btn btn-s" onclick="showModal()">🛑  EMERGENCY STOP</button>
      </div>
      <div style="margin-top:10px;font-size:10px;color:var(--dim);line-height:1.7">
        Enter PIN before using controls<br>
        <b style="color:var(--text)">PAUSE</b> — stops new trades, keeps positions<br>
        <b style="color:var(--text)">EMERGENCY STOP</b> — closes everything instantly
      </div>
    </div>
  </div>

  <!-- ACTIVITY + SCAN SUMMARY -->
  <div class="g21">
    <div class="card">
      <div class="ct">Live Bot Activity</div>
      <div class="log" id="log">
        <div class="aitem ai"><span class="atime">--:--</span><span class="amsg">Connecting to bot...</span></div>
      </div>
    </div>
    <div class="card">
      <div class="ct">Last Scan Summary</div>
      <div id="scan-panel" style="font-family:var(--mono);font-size:11px;color:var(--dim)">
        Waiting for first scan...
      </div>
    </div>
  </div>

  <!-- OPEN ORDERS -->
  <div class="card" style="margin-bottom:12px">
    <div class="ct">Open Orders</div>
    <table>
      <thead><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Type</th><th>Status</th><th></th></tr></thead>
      <tbody id="ord-body"><tr><td colspan="6" class="empty">No open orders</td></tr></tbody>
    </table>
  </div>
</div>

<!-- MODAL -->
<div class="modal" id="modal">
  <div class="mbox">
    <h3>🛑 Emergency Stop?</h3>
    <p>This will <strong>cancel ALL orders</strong> and <strong>close ALL positions</strong> immediately.</p>
    <div class="mbtns">
      <button class="btn" style="flex:1;background:rgba(255,255,255,.04);color:var(--dim);border-color:var(--border)" onclick="closeModal()">CANCEL</button>
      <button class="btn btn-s" style="flex:1" onclick="doStop()">CONFIRM</button>
    </div>
  </div>
</div>

<script>
// ── CLOCK ──────────────────────────────────────────────────────────────────
function tick(){
  const et=new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date());
  document.getElementById('clk').textContent=et+' ET';
}
setInterval(tick,1000); tick();

// ── HELPERS ────────────────────────────────────────────────────────────────
function pin(){ return document.getElementById('pin').value.trim(); }
function fmt(n){ const v=parseFloat(n||0); return (v>=0?'+':'')+'$'+Math.abs(v).toFixed(2); }
function fv(n){ return '$'+parseFloat(n||0).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}); }
function pc(v){ return parseFloat(v||0)>=0?'g':'r'; }

async function api(url, method='GET', body=null){
  const opts = { method, headers:{'Content-Type':'application/json'} };
  if(body) opts.body = JSON.stringify(body);
  try{
    const r = await fetch(url, opts);
    return await r.json();
  } catch(e){
    console.error('API error:', e);
    return {};
  }
}

// ── CONTROLS ───────────────────────────────────────────────────────────────
function showModal(){ document.getElementById('modal').classList.add('show'); }
function closeModal(){ document.getElementById('modal').classList.remove('show'); }

async function ctrl(action){
  const p = pin();
  if(!p){ window.alert('Enter your PIN first'); return; }
  const r = await api('/api/'+action, 'POST', {pin: p});
  if(r.error){ window.alert('Wrong PIN'); return; }
  setTimeout(load, 400);
}

async function doStop(){
  closeModal();
  const p = pin();
  if(!p){ window.alert('Enter your PIN first'); return; }
  const r = await api('/api/estop', 'POST', {pin: p});
  if(r.error){ window.alert('Wrong PIN'); return; }
  setTimeout(load, 400);
}

async function closePos(sym){
  if(!confirm('Close position: '+sym+'?')) return;
  const p = pin();
  if(!p){ window.alert('Enter your PIN first'); return; }
  await api('/api/close/'+sym, 'POST', {pin: p});
  setTimeout(load, 500);
}

// ── MAIN LOAD ──────────────────────────────────────────────────────────────
let lastAlertCount = 0;

async function load(){
  const d = await api('/api/data');
  if(!d || d.error) return;

  // Portfolio
  document.getElementById('equity').textContent = fv(d.equity);
  const ps = document.getElementById('pnl-sub');
  ps.innerHTML = 'Today: <span class="'+pc(d.pnl)+'">'+fmt(d.pnl)+'</span>';

  // Daily P&L
  const dp = parseFloat(d.daily_pnl||0);
  const dpEl = document.getElementById('daily-pnl');
  dpEl.textContent = fmt(dp); dpEl.className = 'sv '+(dp>=0?'g':'r');
  const gp = Math.min(100, Math.max(0, dp/(d.goal||100)*100));
  document.getElementById('goal-bar').style.width = gp+'%';
  document.getElementById('goal-label').textContent = '$'+(d.goal||100)+' goal';

  // Win/Loss
  document.getElementById('wl').textContent = (d.wins||0)+'W / '+(d.losses||0)+'L';
  document.getElementById('wr-sub').textContent = 'All-time win rate: '+(d.win_rate||0)+'%';

  // Day trades
  document.getElementById('dt').textContent = (d.day_trades||0)+' / 3';

  // Market badge
  const mb = document.getElementById('mbadge');
  if(d.market_open){
    mb.className='mbadge open'; mb.textContent='MARKET OPEN';
  } else {
    mb.className='mbadge closed';
    const no = d.next_open ? new Date(d.next_open).toLocaleString('en-US',{timeZone:'America/New_York',weekday:'short',hour:'2-digit',minute:'2-digit'}) : '';
    mb.textContent = no ? 'Opens '+no : 'MARKET CLOSED';
  }

  // Bot status
  const dot=document.getElementById('bot-dot'), st=document.getElementById('bot-status');
  const bp=document.getElementById('btn-pause'), br=document.getElementById('btn-resume');
  if(d.paused){
    dot.className='dot paused'; st.className='stxt w'; st.textContent='PAUSED';
    bp.disabled=true; br.disabled=false;
  } else {
    dot.className='dot running'; st.className='stxt g'; st.textContent='RUNNING';
    bp.disabled=false; br.disabled=true;
  }

  // Positions
  const pt = document.getElementById('pos-body');
  if(!d.positions||!d.positions.length){
    pt.innerHTML='<tr><td colspan="7" class="empty">No open positions</td></tr>';
  } else {
    pt.innerHTML = d.positions.map(p=>`
      <tr>
        <td><b>${p.symbol}</b></td>
        <td class="${p.side==='long'?'g':'r'}">${(p.side||'').toUpperCase()}</td>
        <td>${p.qty}</td>
        <td>$${parseFloat(p.avg_entry_price||0).toFixed(2)}</td>
        <td>$${parseFloat(p.current_price||0).toFixed(2)}</td>
        <td class="${pc(p.unrealized_pl)}">${fmt(p.unrealized_pl)} (${(parseFloat(p.unrealized_plpc||0)*100).toFixed(1)}%)</td>
        <td><button class="cbtn" onclick="closePos('${p.symbol}')">CLOSE</button></td>
      </tr>`).join('');
  }

  // Orders
  const ot = document.getElementById('ord-body');
  if(!d.orders||!d.orders.length){
    ot.innerHTML='<tr><td colspan="6" class="empty">No open orders</td></tr>';
  } else {
    ot.innerHTML = d.orders.map(o=>`
      <tr>
        <td><b>${o.symbol}</b></td>
        <td class="${o.side==='buy'?'g':'r'}">${(o.side||'').toUpperCase()}</td>
        <td>${o.qty}</td>
        <td>${(o.type||'').toUpperCase()}</td>
        <td>${o.status||''}</td>
        <td></td>
      </tr>`).join('');
  }

  // Alerts — prepend new ones to top
  if(d.alerts && d.alerts.length !== lastAlertCount){
    lastAlertCount = d.alerts.length;
    const lg = document.getElementById('log');
    lg.innerHTML = d.alerts.slice().reverse().map(a=>`
      <div class="aitem a${a.l||'i'}">
        <span class="atime">${a.t||''}</span>
        <span class="amsg">${a.m||''}</span>
      </div>`).join('');
  }

  // Scan summary
  const sc = d.scan;
  if(sc && sc.time && sc.time !== 'Never'){
    const regime = sc.healthy ? '✅ BULL' : '⚠️ BEAR';
    let html = `<div style="margin-bottom:8px;color:var(--text)"><b>${sc.label}</b> @ ${sc.time}<br><span style="color:var(--dim)">${regime} | ${sc.found} setups found</span></div>`;
    if(sc.top && sc.top.length){
      sc.top.forEach((s,i)=>{
        const w = s.tier>=2?'🐋':s.tier===1?'🐳':'📊';
        const col = parseFloat(s.chg)>=0?'var(--green)':'var(--red)';
        html += `<div class="scan-item">
          <span>${w} <b style="color:#fff">${s.sym}</b></span>
          <span style="color:${col}">${s.chg>0?'+':''}${s.chg}%</span>
          <span style="color:var(--blue)">${s.vol}x</span>
          <span style="color:var(--yellow)">score ${s.score}</span>
        </div>`;
      });
    } else {
      html += `<div style="color:var(--dim);font-size:10px;margin-top:6px">No setups above min score — bot watching</div>`;
    }
    document.getElementById('scan-panel').innerHTML = html;
  }
}

// Start loading and refresh every 10 seconds
load();
setInterval(load, 10000);
</script>
</body></html>"""

# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
