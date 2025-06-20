#!/usr/bin/env python3
"""
Telegram Stock-Alert Bot   |   Python 3.10+
Features:
 1. /watch, /unwatch with inline buttons
 2. /pricealert & /rsialert for threshold triggers
 3. Daily digest at 18:00 summarizing today's alerts
"""

import os, html, logging, sqlite3, asyncio, requests
from datetime import datetime, date, time, timedelta
from typing import List, Dict, Any, Optional

import aiohttp
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes
)

# ────────────────────────────── ENV / CONFIG ────────────────────────────── #

load_dotenv()
BOT_TOKEN       = os.getenv("TG_TOKEN")
NEWSAPI_KEY     = os.getenv("NEWSAPI_KEY")
ALPHAV_KEY      = os.getenv("ALPHAVANTAGE_KEY")
POLL_INTERVAL   = int(os.getenv("POLL_INTERVAL", 15))

if not BOT_TOKEN:
    raise RuntimeError("TG_TOKEN env var missing")

# ──────────── SQLite & Tables ───────────── #

DB = sqlite3.connect("watchlist.db", check_same_thread=False)
c = DB.cursor()
c.executescript("""
CREATE TABLE IF NOT EXISTS watch (
    user   INTEGER,
    symbol TEXT,
    UNIQUE(user, symbol)
);
CREATE TABLE IF NOT EXISTS alerts (
    user      INTEGER,
    symbol    TEXT,
    headline  TEXT,
    link      TEXT,
    ts        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS price_alerts (
    user      INTEGER,
    symbol    TEXT,
    op        TEXT,   -- '>' or '<'
    threshold REAL
);
CREATE TABLE IF NOT EXISTS rsi_alerts (
    user      INTEGER,
    symbol    TEXT,
    op        TEXT,   -- '>' or '<'
    threshold REAL
);
""")
DB.commit()

# ───────────────────────── Logging ────────────────────────── #

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s"
)
log = logging.getLogger("bot")

# ────────────────────────── Utilities ────────────────────────── #

SESSION: Optional[aiohttp.ClientSession] = None
LAST_IDS: Dict[str, Any] = {}

async def get_json(url, headers=None):
    global SESSION
    headers = headers or {}
    if SESSION is None:
        SESSION = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(10))
    for _ in range(3):
        async with SESSION.get(url, headers=headers) as resp:
            if resp.status == 200:
                return await resp.json()
            if resp.status == 429:
                await asyncio.sleep(2)
            else:
                log.warning("Fetch %s -> %s", url, resp.status)
                return None
    return None

def fetch_price(symbol: str) -> Optional[float]:
    """Sync fetch of latest price via AlphaVantage."""
    if not ALPHAV_KEY: return None
    url = (
      f"https://www.alphavantage.co/query?function=GLOBAL_QUOTE"
      f"&symbol={symbol}&apikey={ALPHAV_KEY}"
    )
    r = requests.get(url, timeout=10).json()
    try:
        return float(r["Global Quote"]["05. price"])
    except:
        return None

def fetch_rsi(symbol: str) -> Optional[float]:
    """Sync fetch of daily RSI via AlphaVantage (14-day)."""
    if not ALPHAV_KEY: return None
    url = (
      f"https://www.alphavantage.co/query?function=RSI"
      f"&symbol={symbol}&interval=daily&time_period=14"
      f"&series_type=close&apikey={ALPHAV_KEY}"
    )
    data = requests.get(url, timeout=10).json()
    try:
        # pick most recent day
        key = list(data["Technical Analysis: RSI"].keys())[0]
        return float(data["Technical Analysis: RSI"][key])
    except:
        return None

# ───────────────────────── FETCHERS ────────────────────────── #

async def fetch_nse():
    url = "https://www.nseindia.com/api/corporate-announcements?index=equities"
    hdr = {"user-agent":"Mozilla/5.0","referer":"https://www.nseindia.com"}
    data = await get_json(url, hdr)
    fresh=[]
    for row in data.get("data",[]):
        uid=row["id"]
        if uid==LAST_IDS.get("nse"): break
        fresh.append({"symbol":row["symbol"],"headline":row["headline"],"link":row["attachPath"]})
    if fresh: LAST_IDS["nse"]=fresh[0]["symbol"]+str(datetime.now())
    return fresh

async def fetch_bse():
    url="https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w?strCat=-1"
    data=await get_json(url)
    fresh=[]
    if isinstance(data,list):
        for row in data:
            uid=row["SCRIP_CD"]+row["NEWS_DT"]
            if uid==LAST_IDS.get("bse"): break
            fresh.append({"symbol":row["SCRIP_CD"],"headline":row["NEWS_SUB"],"link":row["ATTACHMENTNAME"]})
        if fresh: LAST_IDS["bse"]=fresh[0]["symbol"]+fresh[0]["headline"]
    return fresh

# ───────────────────── Dispatch & Record ────────────────────── #

async def dispatch_announcements(app):
    combined = await fetch_nse()+await fetch_bse()
    # record & send
    rows=DB.execute("SELECT user,symbol FROM watch").fetchall()
    watch_map={}
    for u,s in rows: watch_map.setdefault(s,u if False else []).append(u)
    for item in combined:
        sym=item["symbol"].upper()
        if sym in watch_map:
            for uid in set(watch_map[sym]):
                txt=f"🔔 <b>{sym}</b>\n{html.escape(item['headline'])}\n<a href='{item['link']}'>Open</a>"
                await app.bot.send_message(uid, txt, parse_mode="HTML")
                # record for digest
                DB.execute("INSERT INTO alerts(user,symbol,headline,link) VALUES(?,?,?,?)",
                           (uid,sym,item["headline"],item["link"]))
    DB.commit()

# ───────────────────── Price & RSI Alerts ───────────────────── #

def check_price_alerts(app):
    for (uid,sym,op,thr) in DB.execute("SELECT user,symbol,op,threshold FROM price_alerts"):
        price=fetch_price(sym)
        if price is None: continue
        hit = price>thr if op=='>' else price<thr
        if hit:
            app.bot.send_message(uid,f"💲 Price alert: {sym} is {price:.2f} {op} {thr}")
            DB.execute("DELETE FROM price_alerts WHERE user=? AND symbol=? AND op=?",(uid,sym,op))
    DB.commit()

def check_rsi_alerts(app):
    for (uid,sym,op,thr) in DB.execute("SELECT user,symbol,op,threshold FROM rsi_alerts"):
        rsi=fetch_rsi(sym)
        if rsi is None: continue
        hit = rsi>thr if op=='>' else rsi<thr
        if hit:
            app.bot.send_message(uid,f"📈 RSI alert: {sym} RSI is {rsi:.1f} {op} {thr}")
            DB.execute("DELETE FROM rsi_alerts WHERE user=? AND symbol=? AND op=?",(uid,sym,op))
    DB.commit()

# ───────────────────── Daily Digest ───────────────────── #

def daily_digest(app):
    today=date.today()
    start=f"{today} 00:00:00"
    end=f"{today} 23:59:59"
    users = [r[0] for r in DB.execute("SELECT DISTINCT user FROM watch")]
    for uid in users:
        rows=DB.execute(
            "SELECT symbol,headline FROM alerts WHERE user=? AND ts BETWEEN ? AND ?",
            (uid,start,end)
        ).fetchall()
        if not rows:
            continue
        msg="🗓️ Today's Alerts:\n" + "\n".join(f"{s}: {h}" for s,h in rows)
        app.bot.send_message(uid,msg)

# ───────────────────── Command Handlers ────────────────────── #

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb=[[InlineKeyboardButton("List Subscriptions",callback_data="LIST")]]
    await update.message.reply_text("Welcome! Use /watch to add symbols.",reply_markup=InlineKeyboardMarkup(kb))

async def help_cmd(update,ctx):
    await update.message.reply_text(
        "/watch <SYM>\n/unwatch <SYM>\n/pricealert SYM > 1234\n/rsialert SYM < 30\n/testdigest"
    )

async def cmd_watch(update,ctx):
    sym=ctx.args[0].upper()
    DB.execute("INSERT OR IGNORE INTO watch(user,symbol) VALUES(?,?)",(update.effective_user.id,sym))
    DB.commit()
    # send inline remove button
    kb=[[InlineKeyboardButton(f"Remove {sym}",callback_data=f"UNW_{sym}")]]
    await update.message.reply_text(f"Tracking {sym}",reply_markup=InlineKeyboardMarkup(kb))

async def cmd_unwatch(update,ctx):
    sym=ctx.args[0].upper()
    DB.execute("DELETE FROM watch WHERE user=? AND symbol=?",(update.effective_user.id,sym))
    DB.commit()
    await update.message.reply_text(f"Stopped {sym}")

async def list_callback(update,ctx):
    uid=update.effective_user.id
    rows=DB.execute("SELECT symbol FROM watch WHERE user=?", (uid,)).fetchall()
    kb=[[InlineKeyboardButton(f"❌ {r[0]}", callback_data=f"UNW_{r[0]}")] for r in rows]
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("Your Subscriptions:",reply_markup=InlineKeyboardMarkup(kb))

async def unwatch_cb(update,ctx):
    data=update.callback_query.data  # "UNW_SYM"
    sym=data.split("_",1)[1]
    uid=update.effective_user.id
    DB.execute("DELETE FROM watch WHERE user=? AND symbol=?",(uid,sym))
    DB.commit()
    await update.callback_query.answer(f"Removed {sym}")
    await list_callback(update,ctx)

async def cmd_pricealert(update,ctx):
    sym,op,thr = ctx.args[0].upper(), ctx.args[1], float(ctx.args[2])
    DB.execute("INSERT INTO price_alerts(user,symbol,op,threshold) VALUES(?,?,?,?)",
               (update.effective_user.id,sym,op,thr))
    DB.commit()
    await update.message.reply_text(f"Set price alert: {sym} {op} {thr}")

async def cmd_rsialert(update,ctx):
    sym,op,thr = ctx.args[0].upper(), ctx.args[1], float(ctx.args[2])
    DB.execute("INSERT INTO rsi_alerts(user,symbol,op,threshold) VALUES(?,?,?,?)",
               (update.effective_user.id,sym,op,thr))
    DB.commit()
    await update.message.reply_text(f"Set RSI alert: {sym} {op} {thr}")

async def cmd_testdigest(update,ctx):
    await update.message.reply_text("Running digest…")
    daily_digest(ctx.application)
    await update.message.reply_text("Done.")

# ──────────────────────────── MAIN ────────────────────────────── #

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("unwatch", cmd_unwatch))
    app.add_handler(CallbackQueryHandler(list_callback, pattern="^LIST$"))
    app.add_handler(CallbackQueryHandler(unwatch_cb, pattern="^UNW_"))
    app.add_handler(CommandHandler("pricealert", cmd_pricealert))
    app.add_handler(CommandHandler("rsialert", cmd_rsialert))
    app.add_handler(CommandHandler("testdigest", cmd_testdigest))

    # scheduler
    sched = BackgroundScheduler()
    sched.add_job(lambda: asyncio.run(dispatch_announcements(app)),
                  'interval', seconds=POLL_INTERVAL, id="poller",
                  max_instances=1, coalesce=True, next_run_time=datetime.now())
    # price & rsi checks every minute
    sched.add_job(lambda: check_price_alerts(app),
                  'interval', minutes=1, id="price")
    sched.add_job(lambda: check_rsi_alerts(app),
                  'interval', minutes=5, id="rsi")
    # daily digest at 18:00
    sched.add_job(lambda: daily_digest(app),
                  CronTrigger(hour=18, minute=0), id="digest")

    sched.start()
    log.info("Bot starting …")
    app.run_polling()

if __name__=="__main__":
    main()
