"""
🐋 Whale Bot — Complete (Bot + Dashboard in one file)
=====================================================
Just set your 3 environment variables in Render and deploy.
That's it. No other setup needed.

Required env vars (set in Render dashboard):
  ALPACA_API_KEY      — your Alpaca live key
  ALPACA_SECRET_KEY   — your Alpaca live secret
  DASHBOARD_PASSWORD  — any password you choose e.g. whale2024

Optional:
  ALPACA_BASE_URL     — defaults to live trading (https://api.alpaca.markets)
                        change to https://paper-api.alpaca.markets for paper
"""

import os, json, time, logging, threading, pytz, requests
from datetime import datetime, date, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, Response, jsonify, request

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG — everything controlled by environment variables
# ─────────────────────────────────────────────────────────────────────────────
ALPACA_KEY    = os.environ.get("ALPACA_API_KEY",     "YOUR_KEY")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY",  "YOUR_SECRET")
ALPACA_URL    = os.environ.get("ALPACA_BASE_URL",    "https://api.alpaca.markets")
DATA_URL      = "https://data.alpaca.markets"
DASH_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "whale2024")
PORT          = int(os.environ.get("PORT", 8080))

TRADE_SIZE    = float(os.environ.get("TRADE_SIZE",    "45"))
MAX_TRADE     = float(os.environ.get("MAX_TRADE",     "500"))
DAILY_GOAL    = float(os.environ.get("DAILY_GOAL",    "100"))
MAX_LOSS      = float(os.environ.get("MAX_LOSS",      "50"))
STOP_PCT      = float(os.environ.get("STOP_PCT",      "0.05"))  # 5% stop loss
TARGET_PCT    = float(os.environ.get("TARGET_PCT",    "0.07"))  # 7% take profit
MIN_SCORE     = int(os.environ.get("MIN_SCORE",       "65"))    # raised from 55
MAX_DT        = int(os.environ.get("MAX_DAY_TRADES",  "2"))
MIN_PRICE     = float(os.environ.get("MIN_PRICE",     "10.0"))  # skip penny/cheap stocks

ET = pytz.timezone("America/New_York")

# ─────────────────────────────────────────────────────────────────────────────
# WHALE WATCHLIST — 60 elite tickers
# ─────────────────────────────────────────────────────────────────────────────
TIER1 = ["NVDA","AMD","TSLA","PLTR","SMCI","MSTR","MARA","RIOT","SOXL","TQQQ"]

TICKERS = list(set(TIER1 + [
    # Strong momentum
    "META","GOOGL","MSFT","AMZN","AAPL","NFLX","UBER","HOOD","SOFI","UPST","COIN","ARKK",
    # Energy
    "OXY","DVN","MRO","HAL","XLE","BOIL","UCO","ERX",
    # Nuclear
    "CCJ","NNE","OKLO","SMR","UEC","URA",
    # Minerals
    "FCX","MP","LAC","ALB","COPX","GDX","GDXJ","NEM","WPM",
    # High vol momentum
    "IONQ","RGTI","RKLB","ASTS","LUNR","CLSK","WULF","JOBY",
]))

# ─────────────────────────────────────────────────────────────────────────────
# SHARED STATE — bot and dashboard talk through these
# ─────────────────────────────────────────────────────────────────────────────
state = {
    "paused":       False,
    "e_stop":       False,
    "alerts":       [],       # last 60 alerts
    "news_tickers": [],       # tickers from recent news
    "traded_today": [],       # all tickers traded today
    "lost_today":   [],       # tickers that hit stop loss today — no re-entry on losers
    "tracker": {
        "date": str(date.today()),
        "daily_pnl": 0.0, "trades_today": 0,
        "wins_today": 0,   "losses_today": 0,
        "day_trades_used": 0, "swing_trades": 0,
        "total_trades": 0, "total_wins": 0, "total_pnl": 0.0,
    },
}
state_lock = threading.Lock()

LOG_FILE = "/tmp/whale_bot.log"

# Rotate log file if over 5MB
def rotate_log():
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 5_000_000:
            with open(LOG_FILE, "r") as f:
                lines = f.readlines()
            with open(LOG_FILE, "w") as f:
                f.writelines(lines[-1000:])
    except Exception:
        pass

try:
    rotate_log()
except Exception:
    pass

# Setup logging — add file handler directly (basicConfig is ignored by gunicorn)
_log_format = logging.Formatter("%(asctime)s %(message)s")
_handlers = [logging.StreamHandler()]
try:
    _file_handler = logging.FileHandler(LOG_FILE, mode='a')
    _file_handler.setFormatter(_log_format)
    _handlers.append(_file_handler)
except Exception:
    pass  # If file logging fails, stdout still works

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("whale_bot")
log.setLevel(logging.INFO)
# Add file handler directly — works even when gunicorn pre-configures logging
try:
    _fh = logging.FileHandler(LOG_FILE, mode='a')
    _fh.setFormatter(_log_format)
    if not any(isinstance(h, logging.FileHandler) for h in log.handlers):
        log.addHandler(_fh)
    # Also add to root logger so all logs go to file
    root_log = logging.getLogger()
    if not any(isinstance(h, logging.FileHandler) for h in root_log.handlers):
        root_log.addHandler(_fh)
except Exception:
    pass  # Never crash on logging setup

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type":        "application/json",
}

def aGet(path, params=None, base=None):
    r = requests.get((base or ALPACA_URL)+path, headers=HEADERS, params=params, timeout=15)
    r.raise_for_status(); return r.json()

def aPost(path, body):
    r = requests.post(ALPACA_URL+path, headers=HEADERS, json=body, timeout=15)
    r.raise_for_status(); return r.json()

def aDel(path):
    r = requests.delete(ALPACA_URL+path, headers=HEADERS, timeout=15)
    return r.json() if r.text.strip() else {}

ALERTS_FILE = "/tmp/whale_alerts.json"

def alert(msg, level="info"):
    """Write alert to memory AND file so it survives worker restarts."""
    entry = {"t": datetime.now(ET).strftime("%H:%M"), "m": msg, "l": level}
    with state_lock:
        state["alerts"].append(entry)
        if len(state["alerts"]) > 80:
            state["alerts"].pop(0)
    try:
        existing = []
        if os.path.exists(ALERTS_FILE):
            with open(ALERTS_FILE, "r") as f:
                existing = json.load(f)
        existing.append(entry)
        with open(ALERTS_FILE, "w") as f:
            json.dump(existing[-80:], f)
    except Exception as e:
        log.debug(f"Alert file error: {e}")
    log.info(msg)

def load_alerts_from_file():
    """Restore alerts from file after restart."""
    try:
        if os.path.exists(ALERTS_FILE):
            with open(ALERTS_FILE, "r") as f:
                saved = json.load(f)
            with state_lock:
                state["alerts"] = saved[-80:]
            log.info(f"📋 Restored {len(saved)} alerts from file")
    except Exception as e:
        log.debug(f"Alert restore error: {e}")

def is_paused():
    with state_lock:
        return state["paused"] or state["e_stop"]

def track_reset_if_new_day():
    today = str(date.today())
    with state_lock:
        if state["tracker"]["date"] != today:
            t = state["tracker"]
            t.update({"date": today, "daily_pnl": 0.0, "trades_today": 0,
                       "wins_today": 0, "losses_today": 0, "day_trades_used": 0})

def record(pnl, kind="day"):
    track_reset_if_new_day()
    with state_lock:
        t = state["tracker"]
        t["daily_pnl"]   = round(t["daily_pnl"] + pnl, 2)
        t["total_pnl"]   = round(t["total_pnl"] + pnl, 2)
        t["trades_today"] += 1; t["total_trades"] += 1
        if pnl > 0: t["wins_today"] += 1; t["total_wins"] += 1
        else: t["losses_today"] += 1
        if kind == "day":   t["day_trades_used"] += 1
        elif kind == "swing": t["swing_trades"] += 1

def tracker():
    track_reset_if_new_day()
    with state_lock:
        return dict(state["tracker"])

def win_rate():
    t = tracker()
    if t["total_trades"] == 0: return 0.0
    return round(t["total_wins"] / t["total_trades"] * 100, 1)

def dynamic_size(cash):
    ratio = cash / 100_000
    return round(max(TRADE_SIZE, min(TRADE_SIZE * (1 + max(0, ratio-1)*3), MAX_TRADE)), 2)

# ─────────────────────────────────────────────────────────────────────────────
# ACCOUNT
# ─────────────────────────────────────────────────────────────────────────────
def get_account(): return aGet("/v2/account")
def get_positions(): return aGet("/v2/positions")
def get_orders(): return aGet("/v2/orders", {"status": "open", "limit": 20})
def is_occupied(): return len(get_positions()) > 0 or len(get_orders()) > 0

def dt_remaining():
    used = int(get_account().get("daytrade_count", 0))
    local = tracker().get("day_trades_used", 0)
    return max(0, MAX_DT - max(used, local))

# ─────────────────────────────────────────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────────────────────────────────────────
def rsi(bars, p=14):
    if len(bars) < p+1: return 50
    c = [b["c"] for b in bars]
    g = [max(c[i]-c[i-1],0) for i in range(1,len(c))]
    l = [max(c[i-1]-c[i],0) for i in range(1,len(c))]
    ag = sum(g[-p:])/p; al = sum(l[-p:])/p
    return round(100-(100/(1+ag/al)),1) if al else 100

def vwap(mbars):
    if not mbars: return None
    tv = sum(((b["h"]+b["l"]+b["c"])/3)*b["v"] for b in mbars)
    v  = sum(b["v"] for b in mbars)
    return round(tv/v,2) if v else None

def ema(bars, p=9):
    if len(bars)<p: return None
    c=[b["c"] for b in bars]; k=2/(p+1)
    e=sum(c[:p])/p
    for x in c[p:]: e=x*k+e*(1-k)
    return round(e,2)

# ─────────────────────────────────────────────────────────────────────────────
# MARKET REGIME
# ─────────────────────────────────────────────────────────────────────────────
_regime = {"ts": None, "ok": True, "chg": 0.0}

def market_regime():
    global _regime
    now = datetime.now(ET)
    if _regime["ts"] and (now-_regime["ts"]).seconds < 900:
        return _regime["ok"], _regime["chg"]
    try:
        snap = aGet("/v2/stocks/SPY/snapshot", base=DATA_URL)
        bars = aGet("/v2/stocks/SPY/bars", {"timeframe":"1Day","limit":20,"feed":"iex"}, DATA_URL).get("bars",[])
        e9=ema(bars,9); e20=ema(bars,20)
        last=snap.get("latestTrade",{}).get("p",0)
        prev=snap.get("prevDailyBar",{}).get("c",last)
        chg=(last-prev)/prev if prev else 0
        ok=bool(e9 and e20 and last>e9 and e9>e20)
        _regime={"ts":now,"ok":ok,"chg":chg}
        log.info(f"Market: {'BULL' if ok else 'BEAR'} | SPY {chg:+.1%}")
        return ok, chg
    except:
        return True, 0.0

# ─────────────────────────────────────────────────────────────────────────────
# WHALE DETECTOR
# ─────────────────────────────────────────────────────────────────────────────
def whale_tier(snap):
    d=snap.get("dailyBar",{}); p=snap.get("prevDailyBar",{}); t=snap.get("latestTrade",{})
    tv=d.get("v",0); pv=p.get("v",1); vr=tv/pv if pv>0 else 0
    cur=t.get("p",d.get("c",0)); pc=p.get("c",0)
    if pc==0: return 0,"none",0
    chg=abs((cur-pc)/pc)
    if vr>=20: return 3,("dark_pool" if chg<0.03 else "extreme"),round(vr,1)
    if vr>=10 and chg<0.025: return 2,"dark_pool",round(vr,1)
    if vr>=5  and chg>=0.015: return 1,"momentum",round(vr,1)
    return 0,"none",round(vr,1)

# ─────────────────────────────────────────────────────────────────────────────
# SCORER
# ─────────────────────────────────────────────────────────────────────────────
def score_ticker(sym, snap, dbars, mbars, spy_chg=0.0, healthy=True):
    d=snap.get("dailyBar",{}); p=snap.get("prevDailyBar",{}); t=snap.get("latestTrade",{})
    cur=t.get("p",d.get("c",0)); pc=p.get("c",0)
    if cur==0 or pc==0: return 0,[],"LONG"
    chg=(cur-pc)/pc; vr=d.get("v",0)/max(p.get("v",1),1)
    dir="LONG" if chg>0 else "SHORT"
    score=0; reasons=[]

    # Whale (0-40)
    tier,wtype,_ = whale_tier(snap)
    if tier==3:   score+=40; reasons.append(f"🚨 EXTREME {vr:.0f}x vol")
    elif tier==2: score+=32; reasons.append(f"🐋 Dark pool {vr:.0f}x vol")
    elif tier==1: score+=24; reasons.append(f"🐋 Whale momentum {vr:.0f}x")
    elif vr>=3:   score+=12; reasons.append(f"📊 Strong vol {vr:.1f}x")
    elif vr>=2:   score+=6;  reasons.append(f"📈 Above avg {vr:.1f}x")
    else: return 0,[],"LONG"

    # Move (0-20)
    ac=abs(chg)
    if ac>=0.10:   score+=20; reasons.append(f"🚀 {chg:+.1%}")
    elif ac>=0.06: score+=16; reasons.append(f"💪 {chg:+.1%}")
    elif ac>=0.03: score+=11; reasons.append(f"📈 {chg:+.1%}")
    elif ac>=0.015:score+=6;  reasons.append(f"👀 {chg:+.1%}")

    # RSI (0-15)
    r=rsi(dbars)
    if dir=="LONG":
        if 45<=r<=65:  score+=15; reasons.append(f"✅ RSI {r}")
        elif 35<=r<45: score+=10; reasons.append(f"📊 RSI {r}")
        elif r<=72:    score+=5;  reasons.append(f"⚠️ RSI {r}")
    else:
        if 28<=r<=50:  score+=15; reasons.append(f"✅ RSI {r} short")
        elif r<28:     score+=5

    # VWAP (0-15)
    v=vwap(mbars)
    if v and cur>0:
        dist=(cur-v)/v
        if dir=="LONG":
            if dist>0.01:    score+=15; reasons.append(f"✅ Above VWAP")
            elif dist>-0.005:score+=8;  reasons.append(f"📊 Near VWAP")
        else:
            if dist<-0.01:   score+=15; reasons.append(f"✅ Below VWAP")

    # RS vs SPY (0-10)
    if spy_chg and dir=="LONG" and chg>spy_chg:
        rs=chg/abs(spy_chg)
        if rs>=3:   score+=10; reasons.append(f"💪 {rs:.1f}x vs SPY")
        elif rs>=2: score+=6
        elif rs>=1: score+=3

    if not healthy: score=int(score*0.8); reasons.append("⚠️ Bear market -20%")
    if sym in TIER1: score=min(100,score+5); reasons.append("⭐ Elite ticker")

    return min(score,100), reasons, dir

# ─────────────────────────────────────────────────────────────────────────────
# SCANNER
# ─────────────────────────────────────────────────────────────────────────────
def scan_one(sym, spy_chg, healthy, tsize):
    try:
        snap = aGet(f"/v2/stocks/{sym}/snapshot", base=DATA_URL)
        if not snap: return None
        d=snap.get("dailyBar",{}); p=snap.get("prevDailyBar",{}); t=snap.get("latestTrade",{})
        cur=t.get("p",d.get("c",0)); pc=p.get("c",0)
        if cur < MIN_PRICE or pc == 0: return None
        dbars=aGet(f"/v2/stocks/{sym}/bars",{"timeframe":"1Day","limit":20,"feed":"iex"},DATA_URL).get("bars",[])
        mbars=aGet(f"/v2/stocks/{sym}/bars",{"timeframe":"1Min","limit":60,"feed":"iex"},DATA_URL).get("bars",[])
        sc,rsns,dir=score_ticker(sym,snap,dbars,mbars,spy_chg,healthy)
        if sc<MIN_SCORE: return None
        chg=(cur-pc)/pc; vr=d.get("v",0)/max(p.get("v",1),1)
        tier,_,_=whale_tier(snap)
        shares=max(1,int(tsize/cur)); cost=round(shares*cur,2)
        sl=round(cur*(1-STOP_PCT),2)  if dir=="LONG" else round(cur*(1+STOP_PCT),2)
        tp=round(cur*(1+TARGET_PCT),2) if dir=="LONG" else round(cur*(1-TARGET_PCT),2)
        return {
            "sym":sym,"price":round(cur,2),"chg":round(chg*100,2),
            "vol":round(vr,1),"tier":tier,"dir":dir,"score":sc,
            "rsns":rsns,"rsi":rsi(dbars),"vwap":vwap(mbars),
            "shares":shares,"cost":cost,"sl":sl,"tp":tp,
            "pot":round(shares*cur*TARGET_PCT,2),
            "risk":round(shares*cur*STOP_PCT,2),
            "elite": sym in TIER1,
        }
    except Exception as e:
        log.debug(f"{sym}: {e}"); return None

def full_scan(label="SCAN"):
    # Combine regular tickers + news tickers
    with state_lock:
        news_t = list(state["news_tickers"])
    all_t = list(set(TICKERS + news_t))
    if news_t:
        alert(f"📰 Scanning {len(news_t)} extra news tickers: {', '.join(news_t)}", "info")

    alert(f"🔍 {label}: scanning {len(all_t)} tickers...", "info")
    ok,chg=market_regime()
    acct=get_account(); cash=float(acct.get("cash",TRADE_SIZE))
    tsize=dynamic_size(cash)
    setups=[]
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs={ex.submit(scan_one,s,chg,ok,tsize):s for s in all_t}
        for f in as_completed(futs):
            r=f.result()
            if r: setups.append(r)
    setups.sort(key=lambda x:(x["tier"],x["score"]),reverse=True)
    top=setups[:5]

    # Send results to dashboard
    regime = "✅ BULL" if ok else "⚠️ BEAR"
    if top:
        lines = [f"✅ {label} done | {regime} | {len(setups)} setups found"]
        for i,s in enumerate(top[:3]):
            w = "🐋" if s["tier"]>=2 else ("🐳" if s["tier"]==1 else "📊")
            lines.append(f"  #{i+1} {w} {s['sym']} score:{s['score']} {s['chg']:+.1f}% vol:{s['vol']}x")
        alert("\n".join(lines), "success")
    else:
        alert(f"✅ {label} done | {regime} | No setups above score {MIN_SCORE} — watching", "info")

    return top,ok,cash

# ─────────────────────────────────────────────────────────────────────────────
# ORDERS
# ─────────────────────────────────────────────────────────────────────────────
def place(setup, swing=False):
    side="buy" if setup["dir"]=="LONG" else "sell"
    body={
        "symbol":setup["sym"],"qty":str(setup["shares"]),
        "side":side,"type":"market",
        "time_in_force":"gtc" if swing else "day",
        "order_class":"bracket",
        "stop_loss":  {"stop_price":  str(setup["sl"])},
        "take_profit":{"limit_price": str(setup["tp"])},
    }
    try:
        aPost("/v2/orders",body)
        kind="SWING" if swing else "DAY TRADE"
        alert(f"✅ {kind}: {side.upper()} {setup['shares']}x {setup['sym']} @ ~${setup['price']}  SL:${setup['sl']} TP:${setup['tp']}","success")
        return True, "swing" if swing else "day"
    except Exception as e:
        alert(f"❌ Order failed {setup['sym']}: {e}","error"); return False,None

# ─────────────────────────────────────────────────────────────────────────────
# DECISION ENGINE
# ─────────────────────────────────────────────────────────────────────────────
def execute(setups, healthy, cash, label=""):
    t=tracker()

    # ── Safety checks ─────────────────────────────────────────────────────────
    if t["daily_pnl"] <= -MAX_LOSS:
        alert(f"🛑 Daily loss limit (${t['daily_pnl']:.2f}) — done trading today","error"); return
    if t["daily_pnl"] >= DAILY_GOAL:
        alert(f"🎯 Daily goal hit! +${t['daily_pnl']:.2f} — resting","success"); return
    if is_paused():
        alert("⏸️ Bot paused — skipping","warning"); return
    if is_occupied():
        log.info("📌 Position open — no new entries"); return
    if cash < TRADE_SIZE:
        alert(f"⚠️ Low cash ${cash:.2f}","warning"); return
    if not setups:
        return

    # ── Get actual day trade count from Alpaca ────────────────────────────────
    try:
        acct_dt = int(get_account().get("daytrade_count", 0))
    except:
        acct_dt = t["day_trades_used"]

    # HARD PDT LIMIT — always read from Alpaca directly
    pdt_maxed = acct_dt >= MAX_DT
    if pdt_maxed:
        alert(f"🛑 PDT limit reached ({acct_dt}/{MAX_DT} day trades) — swing trades ONLY today","warning")

    # Filter out tickers that already lost today — no throwing good money after bad
    with state_lock:
        lost = list(state["lost_today"])
        traded = list(state["traded_today"])
    filtered = [s for s in setups if s["sym"] not in lost]
    if not filtered:
        alert("📊 All qualifying setups already lost today — no re-entry on losers","info"); return

    best = filtered[0]
    required = 70 if not healthy else MIN_SCORE

    # Alert best setup
    tags = "🚨EXTREME" if best["tier"]==3 else ("🐋DARK POOL" if best["tier"]==2 else ("🐋WHALE" if best["tier"]==1 else "📊"))
    alert(f"{tags} {best['dir']} {best['sym']} | Score:{best['score']} | {best['chg']:+.1f}% | Vol:{best['vol']}x | ${best['cost']:.2f}","info")

    if best["tier"]==3:
        swing = pdt_maxed
        alert(f"🚨 EXTREME WHALE — {'SWING' if swing else 'DAY TRADE'}: {best['sym']}","warning")
        ok,k = place(best, swing=swing)
        if ok:
            record(0, k or "day")
            with state_lock: state["traded_today"].append(best["sym"]) if best["sym"] not in state["traded_today"] else None

    elif pdt_maxed and best["score"] >= 65:
        alert(f"🔄 PDT maxed ({acct_dt}/{MAX_DT}) — SWING only: {best['sym']}","warning")
        ok,k = place(best, swing=True)
        if ok:
            record(0, "swing")
            with state_lock: state["traded_today"].append(best["sym"]) if best["sym"] not in state["traded_today"] else None

    elif pdt_maxed:
        alert(f"🛑 PDT maxed ({acct_dt}/{MAX_DT}) — skipping {best['sym']} (score too low for swing)","warning")
        return

    elif best["score"] >= 75:
        alert(f"🤖 HIGH SCORE ({best['score']}) — day trading {best['sym']}","info")
        ok,k = place(best, swing=False)
        if ok:
            record(0, "day")
            with state_lock: state["traded_today"].append(best["sym"]) if best["sym"] not in state["traded_today"] else None

    elif best["score"] >= required:
        alert(f"🤖 Auto-trade {best['sym']} score:{best['score']}","info")
        ok,k = place(best, swing=False)
        if ok:
            record(0, "day")
            with state_lock: state["traded_today"].append(best["sym"]) if best["sym"] not in state["traded_today"] else None

    else:
        alert(f"👤 No action: {best['sym']} score:{best['score']} | DT:{acct_dt}/{MAX_DT}","warning")

# ─────────────────────────────────────────────────────────────────────────────
# BOT JOBS
# ─────────────────────────────────────────────────────────────────────────────
def monitor():
    try:
        for p in get_positions():
            pl  = float(p.get("unrealized_pl", 0))
            pct = float(p.get("unrealized_plpc", 0)) * 100
            sym = p.get("symbol","")
            log.info(f"📊 {sym} x{p['qty']} @ ${p['current_price']} | P&L: ${pl:+.2f} ({pct:+.1f}%)")
            alert(f"📊 {sym} | P&L: ${pl:+.2f} ({pct:+.1f}%)", "info")
    except: pass

def check_closed_trades():
    """Check for newly closed trades and track wins/losses."""
    try:
        today = datetime.now(ET).strftime("%Y-%m-%d")
        fills = aGet("/v2/account/activities/FILL",
                    params={"date": today, "direction": "desc", "page_size": 20})
        # Match buy/sell pairs to determine P&L
        buys  = {}
        sells = {}
        for f in fills:
            sym  = f.get("symbol","")
            side = f.get("side","")
            price= float(f.get("price", 0))
            qty  = float(f.get("qty", 0))
            if side == "buy":
                buys[sym]  = buys.get(sym, 0) + price * qty
            elif side == "sell":
                sells[sym] = sells.get(sym, 0) + price * qty

        for sym in sells:
            if sym in buys:
                pnl = sells[sym] - buys[sym]
                if pnl < 0:
                    with state_lock:
                        if sym not in state["lost_today"]:
                            state["lost_today"].append(sym)
                            alert(f"📌 {sym} marked as loser today — no re-entry","warning")
    except Exception as e:
        log.debug(f"Trade check error: {e}")

def job(label):
    if is_paused(): alert(f"⏸️ Paused — skipping {label}","warning"); return
    # No new entries in first 15 min after open (9:30-9:45) — most volatile/fake moves
    now = datetime.now(ET)
    if now.hour == 9 and now.minute < 45 and label not in ("PREMARKET 8AM", "OPENING RANGE", "ORB BREAKOUT"):
        alert("⏳ Waiting for market to settle (9:30-9:45 no-trade window)","info"); return
    s,ok,cash=full_scan(label)
    execute(s,ok,cash,label)

def eod():
    monitor()
    t=tracker(); wr=win_rate()
    pct=min(100,max(0,(t["daily_pnl"]/DAILY_GOAL)*100)) if DAILY_GOAL else 0
    alert(
        f"🔔 END OF DAY | P&L: ${t['daily_pnl']:+.2f} ({pct:.0f}% of ${DAILY_GOAL:.0f} goal) | "
        f"{t['wins_today']}W/{t['losses_today']}L today | "
        f"All-time: {wr}% win rate ({t['total_wins']}/{t['total_trades']} trades)",
        "info"
    )

# ─────────────────────────────────────────────────────────────────────────────
# BOT THREAD
# ─────────────────────────────────────────────────────────────────────────────
def fetch_news_tickers():
    """
    Pull latest news from Alpaca and extract hot tickers.
    Runs every 30 min — adds new symbols to scan list temporarily.
    """
    try:
        # Get last 30 min of news
        since = (datetime.now(ET) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data  = aGet("/v1beta1/news", params={"start": since, "limit": 50}, base=DATA_URL)
        news  = data.get("news", [])
        if not news:
            return []

        # Extract tickers from news — only ones with multiple mentions (hotter)
        ticker_count = {}
        for article in news:
            syms = article.get("symbols", [])
            for s in syms:
                if s and len(s) <= 5 and s.isalpha():  # Valid ticker format
                    ticker_count[s] = ticker_count.get(s, 0) + 1

        # Only tickers mentioned in 2+ articles = real news flow
        hot = [t for t, c in ticker_count.items() if c >= 2]

        # Filter out already in watchlist (already being scanned)
        new_tickers = [t for t in hot if t not in TICKERS][:10]  # Max 10 new

        if new_tickers:
            alert(f"📰 NEWS: Hot tickers detected — {', '.join(new_tickers)}", "info")
            with state_lock:
                state["news_tickers"] = new_tickers

        return new_tickers
    except Exception as e:
        log.debug(f"News fetch error: {e}")
        return []

def keepalive_loop():
    """Pings own URL every 5 min to prevent Render free tier sleep."""
    time.sleep(60)  # Wait for server to start
    url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if not url:
        log.info("No RENDER_EXTERNAL_URL set — keepalive disabled")
        return
    while True:
        try:
            requests.get(url, timeout=10)
            log.info("💓 Keepalive ping sent")
        except Exception as e:
            log.debug(f"Keepalive error: {e}")
        time.sleep(270)  # Every 4.5 minutes

def bot_loop():
    load_alerts_from_file()  # Restore alerts after restart
    alert("🐋 Whale Bot v3 online — pure equity momentum","success")
    alert(f"Scanning {len(TICKERS)} tickers | ${TRADE_SIZE}/trade | Goal: ${DAILY_GOAL}/day","info")

    # ── STARTUP SCAN ──────────────────────────────────────────────────────────
    # Run immediately on startup if market is open — never miss a session
    try:
        clock = aGet("/v2/clock")
        if clock.get("is_open"):
            alert("🔄 Bot restarted during market hours — running immediate scan","info")
            time.sleep(5)  # Brief pause for server to fully start
            job("STARTUP SCAN")
    except Exception as e:
        log.error(f"Startup scan error: {e}")

    triggered=set()
    while True:
        try:
            now=datetime.now(ET); h,m=now.hour,now.minute; key=f"{h}:{m:02d}"
            schedule={
                "8:00":  lambda: job("PREMARKET 8AM"),
                "9:30":  lambda: job("OPENING RANGE"),
                "9:45":  lambda: job("ORB BREAKOUT"),
                "10:00": lambda: job("MOMENTUM"),
                "12:00": lambda: job("MIDDAY SWING"),
                "15:00": lambda: job("POWER HOUR"),
                "15:55": eod,
            }
            if key in schedule and key not in triggered:
                schedule[key](); triggered.add(key)

            # Fetch news every 30 min during market hours
            elif 8<=h<16 and m in (0,30) and f"news_{key}" not in triggered:
                fetch_news_tickers()
                triggered.add(f"news_{key}")

            elif 9<=h<16 and m%2==0 and key not in triggered:
                monitor()
                check_closed_trades()
                if not is_occupied() and not is_paused():
                    s,ok,cash=full_scan("CONTINUOUS")
                    whales=[x for x in s if x["tier"]>=2]
                    if whales:
                        alert(f"🐋 WHALE DETECTED: {whales[0]['sym']} {whales[0]['vol']}x vol score {whales[0]['score']}","warning")
                        execute(whales,ok,cash,"WHALE ALERT")
                triggered.add(key)
            if h==0 and m==1 and "reset" not in triggered:
                triggered.clear(); triggered.add("reset")
                with state_lock:
                    state["news_tickers"]=[]
                    state["traded_today"]=[]
                    state["lost_today"]=[]
                log.info("🔄 New trading day — reset complete")
            time.sleep(20)
        except Exception as e:
            log.error(f"Bot error: {e}"); time.sleep(60)

# ─────────────────────────────────────────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)

# Start bot threads at module level so gunicorn starts them too
_bot_started = False
def start_bot_threads():
    global _bot_started
    if not _bot_started:
        _bot_started = True
        threading.Thread(target=bot_loop,          daemon=True).start()
        threading.Thread(target=keepalive_loop,     daemon=True).start()
        threading.Thread(target=cache_refresh_loop, daemon=True).start()
        log.info("🤖 Bot threads started")

start_bot_threads()

def authed():
    tok=request.headers.get("X-Token") or request.args.get("token","")
    return tok==DASH_PASSWORD

# Cache for Alpaca data — refreshes in background thread every 10 seconds
_data_cache = {"ts": None, "data": {
    "acct": {}, "pos": [], "ords": [], "clk": {}
}}
_cache_lock = threading.Lock()

def cache_refresh_loop():
    """Background thread that keeps Alpaca data fresh — never blocks Flask."""
    time.sleep(3)  # Brief startup delay
    while True:
        try:
            acct = get_account()
            pos  = get_positions()
            ords = get_orders()
            clk  = aGet("/v2/clock")
            with _cache_lock:
                _data_cache["ts"]   = datetime.now(ET)
                _data_cache["data"] = {"acct": acct, "pos": pos, "ords": ords, "clk": clk}
        except Exception as e:
            log.debug(f"Cache refresh error: {e}")
        time.sleep(10)

def get_cached_alpaca_data():
    """Always returns instantly from cache — never blocks."""
    with _cache_lock:
        return dict(_data_cache["data"])

@app.route("/")
def index(): return DASHBOARD_HTML

@app.route("/api/data")
def api_data():
    try:
        cached = get_cached_alpaca_data()
        acct = cached.get("acct", {})
        pos  = cached.get("pos",  [])
        ords = cached.get("ords", [])
        clk  = cached.get("clk",  {})
        t    = tracker()
        with state_lock:
            alerts  = list(state["alerts"])
            traded  = list(state["traded_today"])
            lost    = list(state["lost_today"])

        # Real P&L from equity vs yesterday close
        equity     = float(acct.get("equity", 0))
        last_eq    = float(acct.get("last_equity", equity))
        real_pnl   = round(equity - last_eq, 2)
        dt_used    = int(acct.get("daytrade_count", 0))
        pdt_flag   = acct.get("pattern_day_trader", False)

        # Get today's trade history from Alpaca directly
        today_trades = []
        try:
            today = datetime.now(ET).strftime("%Y-%m-%d")
            fills = aGet("/v2/account/activities/FILL",
                        params={"date": today, "direction": "desc", "page_size": 20})
            # Build clean trade list — only show buys and take-profit/stop sells
            seen_orders = set()
            for f in fills:
                oid = f.get("order_id","")
                if oid not in seen_orders:
                    seen_orders.add(oid)
                    today_trades.append({
                        "sym":   f.get("symbol",""),
                        "side":  f.get("side",""),
                        "qty":   f.get("cum_qty", f.get("qty","")),
                        "price": float(f.get("price",0)),
                        "time":  f.get("transaction_time","")[:16].replace("T"," "),
                    })
        except Exception as e:
            log.debug(f"Trade history error: {e}")

        return jsonify({
            "equity":      equity,
            "cash":        float(acct.get("cash",0)),
            "pnl":         real_pnl,
            "daily_pnl":   real_pnl,   # Use real Alpaca P&L
            "wins":        t["wins_today"],
            "losses":      t["losses_today"],
            "wr":          win_rate(),
            "dt_used":     dt_used,
            "dt_max":      MAX_DT,
            "pdt_flagged": pdt_flag,
            "paused":      state["paused"],
            "e_stop":      state["e_stop"],
            "market_open": clk.get("is_open", False),
            "next_open":   clk.get("next_open", ""),
            "positions":   pos,
            "orders":      ords,
            "alerts":      alerts[-40:],
            "traded_today":traded,
            "lost_today":  lost,
            "trades":      today_trades,
            "goal":        DAILY_GOAL,
            "trade_size":  TRADE_SIZE,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/logs")
def api_logs():
    """Return last 100 lines of bot log — same as Render sees."""
    try:
        if not os.path.exists(LOG_FILE):
            return jsonify({"lines": ["No logs yet..."]})
        with open(LOG_FILE, "r") as f:
            lines = f.readlines()
        # Return last 100 lines, cleaned up
        recent = [l.strip() for l in lines[-100:] if l.strip()]
        return jsonify({"lines": recent})
    except Exception as e:
        return jsonify({"lines": [f"Log read error: {e}"]})
def api_pause():
    if not authed(): return jsonify({"error":"Wrong password"}),401
    with state_lock: state["paused"]=True; state["e_stop"]=False
    alert("⏸️ Bot PAUSED by you","warning"); return jsonify({"ok":True})

@app.route("/api/resume", methods=["POST"])
def api_resume():
    if not authed(): return jsonify({"error":"Wrong password"}),401
    with state_lock: state["paused"]=False; state["e_stop"]=False
    alert("▶️ Bot RESUMED","success"); return jsonify({"ok":True})

@app.route("/api/estop",  methods=["POST"])
def api_estop():
    if not authed(): return jsonify({"error":"Wrong password"}),401
    with state_lock: state["paused"]=True; state["e_stop"]=True
    try: aDel("/v2/orders")
    except: pass
    try: requests.delete(ALPACA_URL+"/v2/positions",headers=HEADERS,timeout=10)
    except: pass
    alert("🛑 EMERGENCY STOP — all orders cancelled, all positions closed","error")
    return jsonify({"ok":True})

@app.route("/api/close/<sym>", methods=["POST"])
def api_close(sym):
    if not authed(): return jsonify({"error":"Wrong password"}),401
    try:
        requests.delete(f"{ALPACA_URL}/v2/positions/{sym}",headers=HEADERS,timeout=10)
        alert(f"🔴 Closed position: {sym}","warning"); return jsonify({"ok":True})
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route("/api/cancel/<oid>", methods=["POST"])
def api_cancel(oid):
    if not authed(): return jsonify({"error":"Wrong password"}),401
    try:
        aDel(f"/v2/orders/{oid}")
        alert(f"❌ Cancelled order","warning"); return jsonify({"ok":True})
    except Exception as e: return jsonify({"error":str(e)}),500

@app.route("/api/stream")
def api_stream():
    def gen():
        while True:
            try:
                t=tracker()
                with state_lock: als=list(state["alerts"][-5:])
                payload={"daily_pnl":t["daily_pnl"],"wins":t["wins_today"],
                         "losses":t["losses_today"],"paused":state["paused"],
                         "e_stop":state["e_stop"],"new_alerts":als,
                         "time":datetime.now(ET).strftime("%H:%M:%S")}
                yield f"data:{json.dumps(payload)}\n\n"
            except: pass
            time.sleep(6)
    return Response(gen(),mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

# ─────────────────────────────────────────────────────────────────────────────
# DASHBOARD HTML — dark trading terminal, mobile-friendly
# ─────────────────────────────────────────────────────────────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>🐋 Whale Bot</title>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=DM+Sans:wght@300;400;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#07090d;--s:#0d1219;--b:#182030;--a:#00e5b0;--b2:#3d9eff;--r:#ff4060;--w:#ffb020;--t:#b8c4d0;--d:#4a5568;--m:'Share Tech Mono',monospace;--f:'DM Sans',sans-serif}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--t);font-family:var(--f);font-size:14px;min-height:100vh}
body::before{content:'';position:fixed;inset:0;background:radial-gradient(ellipse at 15% 15%,rgba(0,229,176,.05) 0%,transparent 50%),radial-gradient(ellipse at 85% 85%,rgba(61,158,255,.04) 0%,transparent 50%);pointer-events:none}
.hdr{display:flex;align-items:center;justify-content:space-between;padding:12px 18px;background:var(--s);border-bottom:1px solid var(--b);position:sticky;top:0;z-index:100;backdrop-filter:blur(10px)}
.logo{font-family:var(--m);font-size:17px;color:var(--a);letter-spacing:2px}
.logo b{color:var(--d);font-size:11px;margin-left:6px;font-weight:400}
.hdr-r{display:flex;align-items:center;gap:10px}
.clock{font-family:var(--m);font-size:12px;color:var(--d)}
.mbadge{font-family:var(--m);font-size:10px;padding:3px 9px;border-radius:20px;font-weight:700;letter-spacing:1px}
.mo{background:rgba(0,229,176,.12);color:var(--a);border:1px solid rgba(0,229,176,.25)}
.mc{background:rgba(255,255,255,.04);color:var(--d);border:1px solid var(--b)}
.wrap{padding:14px;max-width:1100px;margin:0 auto}
.g4{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:12px}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px}
.g21{display:grid;grid-template-columns:2fr 1fr;gap:10px;margin-bottom:12px}
@media(max-width:700px){.g4{grid-template-columns:1fr 1fr}.g2,.g21{grid-template-columns:1fr}}
.card{background:var(--s);border:1px solid var(--b);border-radius:10px;padding:14px;position:relative;overflow:hidden}
.card::after{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,var(--a),var(--b2));opacity:.3}
.ct{font-size:9px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--d);margin-bottom:8px}
.sv{font-family:var(--m);font-size:24px;color:#fff;line-height:1}
.ss{font-size:11px;color:var(--d);margin-top:4px}
.g{color:var(--a)}.r{color:var(--r)}.w{color:var(--w)}
.gbar{margin-top:8px}
.gtrack{height:3px;background:var(--b);border-radius:2px;overflow:hidden}
.gfill{height:100%;background:linear-gradient(90deg,var(--a),var(--b2));border-radius:2px;transition:width .5s}
.glabel{display:flex;justify-content:space-between;font-size:9px;color:var(--d);margin-top:3px}
.dot{width:9px;height:9px;border-radius:50%;flex-shrink:0}
.dr{background:var(--a);box-shadow:0 0 8px var(--a);animation:pulse 2s infinite}
.dp{background:var(--w)}
.de{background:var(--r);box-shadow:0 0 8px var(--r)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
.srow{display:flex;align-items:center;gap:8px;margin-bottom:12px}
.st{font-family:var(--m);font-size:13px}
.btns{display:flex;flex-direction:column;gap:7px}
.btn{padding:9px 14px;border-radius:6px;border:1px solid;font-family:var(--m);font-size:11px;letter-spacing:1px;cursor:pointer;width:100%;transition:all .15s;font-weight:700}
.btn:hover{filter:brightness(1.2);transform:translateY(-1px)}
.btn:active{transform:none}
.bp{background:rgba(255,176,32,.08);color:var(--w);border-color:rgba(255,176,32,.25)}
.br{background:rgba(0,229,176,.08);color:var(--a);border-color:rgba(0,229,176,.25)}
.bs{background:rgba(255,64,96,.12);color:var(--r);border-color:rgba(255,64,96,.25)}
.btn:disabled{opacity:.35;cursor:not-allowed;transform:none}
.lock-row{display:flex;gap:8px;margin-bottom:12px}
.li{flex:1;background:var(--bg);border:1px solid var(--b);color:var(--t);font-family:var(--m);font-size:12px;padding:7px 10px;border-radius:5px;outline:none}
.li:focus{border-color:var(--a)}
.lb{background:rgba(0,229,176,.08);border:1px solid rgba(0,229,176,.25);color:var(--a);font-family:var(--m);font-size:11px;padding:7px 12px;border-radius:5px;cursor:pointer;white-space:nowrap}
table{width:100%;border-collapse:collapse;font-family:var(--m);font-size:11px}
th{text-align:left;padding:5px 8px;font-size:9px;letter-spacing:1.5px;text-transform:uppercase;color:var(--d);border-bottom:1px solid var(--b)}
td{padding:7px 8px;border-bottom:1px solid rgba(24,32,48,.5);color:var(--t)}
.empty{text-align:center;color:var(--d);padding:16px;font-size:11px}
.log{height:240px;overflow-y:auto;display:flex;flex-direction:column;gap:3px}
.log::-webkit-scrollbar{width:3px}
.log::-webkit-scrollbar-thumb{background:var(--b);border-radius:2px}
.li2{display:flex;gap:7px;padding:5px 7px;border-radius:4px;font-size:10px;font-family:var(--m);border-left:2px solid transparent}
.li2 .ltime{color:var(--d);flex-shrink:0}
.li2 .lmsg{color:var(--t);flex:1}
.ai{border-left-color:var(--b2);background:rgba(61,158,255,.04)}
.as{border-left-color:var(--a);background:rgba(0,229,176,.04)}
.aw{border-left-color:var(--w);background:rgba(255,176,32,.04)}
.ae{border-left-color:var(--r);background:rgba(255,64,96,.05)}
.cbtn{font-family:var(--m);font-size:9px;padding:3px 8px;border-radius:3px;cursor:pointer;border:1px solid rgba(255,64,96,.25);background:rgba(255,64,96,.08);color:var(--r)}
.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:999;align-items:center;justify-content:center}
.modal.show{display:flex}
.mbox{background:var(--s);border:1px solid var(--r);border-radius:12px;padding:26px;max-width:320px;width:90%;text-align:center}
.mbox h3{color:var(--r);font-family:var(--m);margin-bottom:8px;font-size:16px}
.mbox p{color:var(--d);font-size:12px;margin-bottom:18px;line-height:1.6}
.mbts{display:flex;gap:8px}
</style></head><body>
<div class="hdr">
  <div class="logo">🐋 WHALE BOT <b>v3 EQUITY</b></div>
  <div class="hdr-r">
    <span class="clock" id="clk">--:--:--</span>
    <span class="mbadge mc" id="mbadge">MARKET CLOSED</span>
  </div>
</div>
<div class="wrap">
  <div class="lock-row">
    <input class="li" type="password" id="pw" placeholder="Enter your dashboard password to unlock controls...">
    <button class="lb" onclick="unlock()">UNLOCK</button>
  </div>
  <div class="g4">
    <div class="card"><div class="ct">Portfolio Value</div><div class="sv" id="eq">—</div><div class="ss" id="eq-s">—</div></div>
    <div class="card"><div class="ct">Today's P&L</div><div class="sv" id="dpnl">—</div><div class="gbar"><div class="gtrack"><div class="gfill" id="gfill" style="width:0"></div></div><div class="glabel"><span>$0</span><span id="glbl">$100 goal</span></div></div></div>
    <div class="card"><div class="ct">Win / Loss Today</div><div class="sv" id="wl">—</div><div class="ss" id="wr">Win rate: —</div></div>
    <div class="card"><div class="ct">Day Trades Used</div><div class="sv" id="dt">—</div><div class="ss" id="dt-sub">Max 2 (keeping 1 reserve)</div></div>
  </div>
  <div class="g2">
    <div class="card">
      <div class="ct">Open Positions</div>
      <table><thead><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Entry</th><th>Current</th><th>P&L</th><th></th></tr></thead>
      <tbody id="pos-tb"><tr><td colspan="7" class="empty">No open positions</td></tr></tbody></table>
    </div>
    <div class="card">
      <div class="ct">Bot Control</div>
      <div class="srow"><div class="dot dr" id="sdot"></div><div class="st g" id="stxt">RUNNING</div></div>
      <div class="btns">
        <button class="btn bp" id="bpause" onclick="ctrl('pause')">⏸  PAUSE BOT</button>
        <button class="btn br" id="bresume" onclick="ctrl('resume')" disabled>▶  RESUME BOT</button>
        <button class="btn bs" onclick="showModal()">🛑  EMERGENCY STOP</button>
      </div>
      <div style="margin-top:10px;font-size:10px;color:var(--d);line-height:1.7">
        <b style="color:var(--t)">PAUSE</b> stops new trades, holds positions open<br>
        <b style="color:var(--t)">EMERGENCY STOP</b> cancels everything instantly
      </div>
    </div>
  </div>
  <div class="g21">
    <div class="card">
      <div class="ct">Live Bot Activity</div>
      <div class="log" id="log"><div class="li2 ai"><span class="ltime">--:--</span><span class="lmsg">Connecting...</span></div></div>
    </div>
    <div class="card">
      <div class="ct">Today's Trades</div>
      <div id="trades-list" style="font-family:var(--m);font-size:11px;max-height:240px;overflow-y:auto">
        <div style="color:var(--d);padding:10px 0">No trades today</div>
      </div>
    </div>
  </div>
</div>
<div class="modal" id="modal">
  <div class="mbox">
    <h3>🛑 Emergency Stop?</h3>
    <p>This will <strong>cancel all open orders</strong> and <strong>close all positions</strong> immediately at market price.</p>
    <div class="mbts">
      <button class="btn" style="flex:1;background:rgba(255,255,255,.04);color:var(--d);border-color:var(--b)" onclick="closeModal()">CANCEL</button>
      <button class="btn bs" style="flex:1" onclick="doStop()">CONFIRM STOP</button>
    </div>
  </div>
</div>
<script>
// Password persists across reloads via localStorage
let PW=localStorage.getItem('wbpw')||'';
if(PW) document.querySelector('.lock-row').style.display='none';

function unlock(){
  PW=document.getElementById('pw').value.trim();
  localStorage.setItem('wbpw',PW);
  document.querySelector('.lock-row').style.display='none';
  load();
}

// Allow pressing Enter in password box
document.getElementById('pw').addEventListener('keydown',function(e){
  if(e.key==='Enter') unlock();
});

function h(url,method='GET'){
  return fetch(url,{method,headers:{'X-Token':PW}})
    .then(r=>{
      if(r.status===401 && method!=='GET'){
        window.alert('Wrong password — please re-enter');
      }
      return r.json();
    })
    .catch(e=>{
      console.error('Fetch error:',e);
      return {};
    });
}

function showModal(){document.getElementById('modal').classList.add('show')}
function closeModal(){document.getElementById('modal').classList.remove('show')}
function doStop(){closeModal();ctrl('estop')}

async function ctrl(a){
  const map={pause:'/api/pause',resume:'/api/resume',estop:'/api/estop'};
  const res=await h(map[a],'POST');
  if(res.ok) setTimeout(load,400);
}
function fmt(n){const v=parseFloat(n);return(v>=0?'+':'')+`$${Math.abs(v).toFixed(2)}`}
function fv(n){return `$${parseFloat(n||0).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}`}
function pc(v){return parseFloat(v)>=0?'g':'r'}
function tick(){
  const et=new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date());
  document.getElementById('clk').textContent=et+' ET';
}
setInterval(tick,1000);tick();
async function load(){
  try{
    const d=await h('/api/data');
    // Account
    document.getElementById('eq').textContent=fv(d.equity);
    document.getElementById('eq-s').innerHTML=`Today: <span class="${pc(d.pnl)}">${fmt(d.pnl)}</span>`;
    // Daily P&L
    const dp=document.getElementById('dpnl');
    dp.textContent=fmt(d.daily_pnl||0);dp.className='sv '+(parseFloat(d.daily_pnl||0)>=0?'g':'r');
    const gp=Math.min(100,Math.max(0,(d.daily_pnl||0)/(d.goal||100)*100));
    document.getElementById('gfill').style.width=gp+'%';
    document.getElementById('glbl').textContent='$'+(d.goal||100)+' goal';
    // Win/loss
    document.getElementById('wl').textContent=`${d.wins||0}W / ${d.losses||0}L`;
    document.getElementById('wr').textContent=`All-time win rate: ${d.wr||0}%`;
    // Day trades — show real count vs max with PDT warning
    const dtMax = d.dt_max || 2;
    const dtEl = document.getElementById('dt');
    dtEl.textContent = `${d.dt_used||0} / ${dtMax}`;
    dtEl.className = 'sv ' + ((d.dt_used||0) >= dtMax ? 'r' : 'g');
    const dtSub = document.getElementById('dt-sub');
    if(dtSub){
      if(d.pdt_flagged){ dtSub.textContent='⚠️ PDT FLAGGED — swing trades only'; dtSub.style.color='var(--r)'; }
      else if((d.dt_used||0) >= dtMax){ dtSub.textContent='Limit reached — swing trades only'; dtSub.style.color='var(--w)'; }
      else{ dtSub.textContent='Max 2 (keeping 1 reserve)'; dtSub.style.color=''; }
    }
    // Market badge
    const mb=document.getElementById('mbadge');
    mb.className='mbadge '+(d.market_open?'mo':'mc');mb.textContent=d.market_open?'MARKET OPEN':'MARKET CLOSED';
    // Status — always read from server so reload shows correct state
    const dot=document.getElementById('sdot'),st=document.getElementById('stxt');
    const bp=document.getElementById('bpause'),br=document.getElementById('bresume');
    if(d.e_stop){
      dot.className='dot de';st.className='st r';st.textContent='EMERGENCY STOP';
      bp.disabled=true;br.disabled=false;
    } else if(d.paused){
      dot.className='dot dp';st.className='st w';st.textContent='PAUSED';
      bp.disabled=true;br.disabled=false;  // Resume always enabled when paused
    } else {
      dot.className='dot dr';st.className='st g';st.textContent='RUNNING';
      bp.disabled=false;br.disabled=true;  // Pause enabled, Resume disabled when running
    }
    // Positions
    const pt=document.getElementById('pos-tb');
    if(!d.positions||!d.positions.length){pt.innerHTML='<tr><td colspan="7" class="empty">No open positions</td></tr>'}
    else pt.innerHTML=d.positions.map(p=>`<tr><td><b>${p.symbol}</b></td><td class="${p.side==='long'?'g':'r'}">${p.side?.toUpperCase()}</td><td>${p.qty}</td><td>$${parseFloat(p.avg_entry_price||0).toFixed(2)}</td><td>$${parseFloat(p.current_price||0).toFixed(2)}</td><td class="${pc(p.unrealized_pl)}">${fmt(p.unrealized_pl)}</td><td><button class="cbtn" onclick="closePos('${p.symbol}')">CLOSE</button></td></tr>`).join('');
    // Orders
    const ot=document.getElementById('ord-tb');
    if(!d.orders||!d.orders.length){ot.innerHTML='<tr><td colspan="5" class="empty">No open orders</td></tr>'}
    else ot.innerHTML=d.orders.map(o=>`<tr><td><b>${o.symbol}</b></td><td class="${o.side==='buy'?'g':'r'}">${o.side?.toUpperCase()}</td><td>${o.qty}</td><td>${(o.type||'').toUpperCase()}</td><td><button class="cbtn" onclick="cancelOrd('${o.id}')">CANCEL</button></td></tr>`).join('');
    // Trade history
    const tl = document.getElementById('trades-list');
    if(d.trades && d.trades.length > 0){
      tl.innerHTML = d.trades.map(t=>`
        <div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--b)">
          <span class="${t.side==='buy'?'g':'r'}">${t.side?.toUpperCase()} ${t.sym}</span>
          <span>$${t.price?.toFixed(2)}</span>
          <span style="color:var(--d);font-size:10px">${t.time?.slice(11)||''}</span>
        </div>`).join('');
    } else {
      tl.innerHTML = '<div style="color:var(--d);padding:10px 0;font-size:11px">No trades today</div>';
    }

    // Live log — reads actual bot log file (same as Render)
    fetch('/api/logs').then(r=>r.json()).then(d=>{
      if(d.lines && d.lines.length > 0){
        const lg=document.getElementById('log');
        lg.innerHTML = d.lines.slice().reverse().map(line=>{
          // Color code by content
          let cls = 'ai';
          if(line.includes('✅')||line.includes('WIN')||line.includes('profit')) cls='as';
          else if(line.includes('❌')||line.includes('LOSS')||line.includes('failed')) cls='ae';
          else if(line.includes('⚠️')||line.includes('PDT')||line.includes('BLOCKED')||line.includes('PAUSED')) cls='aw';
          else if(line.includes('🐋')||line.includes('WHALE')||line.includes('🚨')) cls='aw';
          // Extract time from log line
          const timeMatch = line.match(/[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}/);
          const t = timeMatch ? timeMatch[0].slice(11) : '';
          const msg = line.replace(/^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} */, '');
          return `<div class="li2 ${cls}"><span class="ltime">${t}</span><span class="lmsg">${msg}</span></div>`;
        }).join('');
      }
    }).catch(e=>console.error('Log fetch error:',e));

    // Alerts fallback
    const lg2=document.getElementById('log');
    if(d.alerts && d.alerts.length > 0 && lg2.children.length <= 1){
      lg2.innerHTML=d.alerts.slice().reverse().map(a=>`<div class="li2 a${a.l||'i'}"><span class="ltime">${a.t||''}</span><span class="lmsg">${a.m||''}</span></div>`).join('');
    } else if(!d.alerts || d.alerts.length === 0){
      const now=new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date());
      if(lg2.children.length <= 1) lg2.innerHTML=`<div class="li2 ai"><span class="ltime">${now}</span><span class="lmsg">Bot running — waiting for activity...</span></div>`;
    }
  }catch(e){
    console.error('Load error:',e);
  }
}
async function closePos(sym){if(!confirm(`Close position: ${sym}?`))return;await h(`/api/close/${sym}`,'POST');setTimeout(load,500)}
async function cancelOrd(id){await h(`/api/cancel/${id}`,'POST');setTimeout(load,500)}

// Load immediately then every 8 seconds — reliable auto-refresh
load();
setInterval(function(){ load(); }, 8000);
</script></body></html>"""

# ─────────────────────────────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Start bot in background thread
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()
    # Start keepalive thread to prevent Render free tier sleep
    k = threading.Thread(target=keepalive_loop, daemon=True)
    k.start()
    log.info(f"🖥️  Dashboard on port {PORT}")
    # Run dashboard (Flask in production mode via gunicorn in Render)
    app.run(host="0.0.0.0", port=PORT, threaded=True)
