#!/usr/bin/env python3
"""
Telegram Stock-Alert Bot | Python 3.10+
Features:
  • /watch & /unwatch multiple symbols at once
  • Inline “Remove” buttons
  • /pricealert & /rsialert with background checks
  • Daily digest at 18:00
  • /list, /subscriptionlist, /alertslist
  • /help or /menu shows everything
"""

import os, html, logging, sqlite3, asyncio, requests
from datetime import datetime, date
from typing import List, Dict, Any, Optional

import aiohttp
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
)

# ─────────────── ENV / CONFIG ─────────────── #

load_dotenv()
BOT_TOKEN     = os.getenv("TG_TOKEN")
NEWSAPI_KEY   = os.getenv("NEWSAPI_KEY")
ALPHAV_KEY    = os.getenv("ALPHAVANTAGE_KEY")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 15))

if not BOT_TOKEN:
    raise RuntimeError("TG_TOKEN env var missing")

# ─────────────── Database Setup ─────────────── #

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
  op        TEXT,
  threshold REAL
);
CREATE TABLE IF NOT EXISTS rsi_alerts (
  user      INTEGER,
  symbol    TEXT,
  op        TEXT,
  threshold REAL
);
""")
DB.commit()

# ─────────────── Logging ─────────────── #

logging.basicConfig(
  level=logging.INFO,
  format="%(asctime)s | %(levelname)-7s | %(message)s"
)
log = logging.getLogger("bot")

# ──────────── Utilities & Fetchers ──────────── #

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

def fetch_price(sym):
    if not ALPHAV_KEY: return None
    url = f"https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={sym}&apikey={ALPHAV_KEY}"
    j = requests.get(url, timeout=10).json()
    try: return float(j["Global Quote"]["05. price"])
    except: return None

def fetch_rsi(sym):
    if not ALPHAV_KEY: return None
    url = (f"https://www.alphavantage.co/query?function=RSI"
           f"&symbol={sym}&interval=daily&time_period=14&series_type=close"
           f"&apikey={ALPHAV_KEY}")
    j = requests.get(url, timeout=10).json()
    try:
        k = next(iter(j["Technical Analysis: RSI"]))
        return float(j["Technical Analysis: RSI"][k])
    except: return None

# ─────────────── Dispatch & Alerts ─────────────── #

async def dispatch_filings(app):
    # fetch NSE & BSE filings…
    announcements = []
    for url, hdr_key in [
      ("https://www.nseindia.com/api/corporate-announcements?index=equities",
       {"user-agent":"Mozilla/5.0","referer":"https://www.nseindia.com"}),
      ("https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w?strCat=-1", None)
    ]:
        data = await get_json(url, hdr_key)
        if isinstance(data, dict): data = data.get("data",[])
        if isinstance(data, list):
            for row in data:
                uid = row.get("id") or (row.get("SCRIP_CD","")+row.get("NEWS_DT",""))
                if uid == LAST_IDS.get(url): break
                sym = row.get("symbol") or row.get("SCRIP_CD")
                announcements.append({
                  "symbol": sym.upper(),
                  "headline": row.get("headline") or row.get("NEWS_SUB"),
                  "link": row.get("attachPath") or row.get("ATTACHMENTNAME")
                })
            if announcements: LAST_IDS[url]=uid

    # send & record
    watch_map = {}
    for u,s in DB.execute("SELECT user,symbol FROM watch"):
        watch_map.setdefault(s, []).append(u)

    for ann in announcements:
        sym = ann["symbol"]
        if sym in watch_map:
            for uid in set(watch_map[sym]):
                txt = (f"🔔 <b>{sym}</b>\n"
                       f"{html.escape(ann['headline'])}\n"
                       f"<a href='{ann['link']}'>Open</a>")
                await app.bot.send_message(uid, txt, parse_mode="HTML")
                DB.execute(
                  "INSERT INTO alerts(user,symbol,headline,link) VALUES(?,?,?,?)",
                  (uid, sym, ann["headline"], ann["link"])
                )
    DB.commit()

def check_price_alerts(app):
    for uid,sym,op,thr in DB.execute("SELECT user,symbol,op,threshold FROM price_alerts"):
        price = fetch_price(sym)
        if price is None: continue
        if (op==">" and price>thr) or (op=="<" and price<thr):
            app.bot.send_message(uid, f"💲 Price alert: {sym} is {price:.2f} {op} {thr}")
            DB.execute("DELETE FROM price_alerts WHERE user=? AND symbol=? AND op=?",(uid,sym,op))
    DB.commit()

def check_rsi_alerts(app):
    for uid,sym,op,thr in DB.execute("SELECT user,symbol,op,threshold FROM rsi_alerts"):
        rsi = fetch_rsi(sym)
        if rsi is None: continue
        if (op==">" and rsi>thr) or (op=="<" and rsi<thr):
            app.bot.send_message(uid, f"📈 RSI alert: {sym} RSI is {rsi:.1f} {op} {thr}")
            DB.execute("DELETE FROM rsi_alerts WHERE user=? AND symbol=? AND op=?",(uid,sym,op))
    DB.commit()

def daily_digest(app):
    today = date.today().isoformat()
    rows = DB.execute(
      "SELECT user,symbol,headline FROM alerts WHERE date(ts)=?",
      (today,)
    ).fetchall()
    by_user={}
    for u,s,h in rows:
        by_user.setdefault(u,[]).append(f"{s}: {h}")
    for uid,items in by_user.items():
        app.bot.send_message(uid, "🗓️ Today's Alerts:\n" + "\n".join(items))

# ─────────────── Command Handlers ─────────────── #

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/watch SYM [SYM ...] — start tracking one or more\n"
        "/unwatch SYM [SYM ...] — stop tracking\n"
        "/list or /subscriptionlist — your tracked symbols\n"
        "/pricealert SYM > 123 — price threshold\n"
        "/rsialert SYM < 30 — RSI threshold\n"
        "/alertslist — list today's filing alerts\n"
        "/menu — show this help\n"
        "/testdigest — force today’s digest now"
    )

async def cmd_watch(update,ctx):
    syms = [s.upper() for s in ctx.args]
    added=[]
    for sym in syms:
        DB.execute("INSERT OR IGNORE INTO watch(user,symbol) VALUES(?,?)",
                   (update.effective_user.id,sym))
        added.append(sym)
    DB.commit()
    # show inline remove buttons:
    kb = [[InlineKeyboardButton(f"Remove {sym}",callback_data=f"UNW_{sym}")]
          for sym in added]
    await update.message.reply_text(f"Tracking: {' '.join(added)}",
                                    reply_markup=InlineKeyboardMarkup(kb))

async def cmd_unwatch(update,ctx):
    syms = [s.upper() for s in ctx.args]
    for sym in syms:
        DB.execute("DELETE FROM watch WHERE user=? AND symbol=?",
                   (update.effective_user.id,sym))
    DB.commit()
    await update.message.reply_text(f"Stopped: {' '.join(syms)}")

async def list_subs(update,ctx):
    rows = DB.execute("SELECT symbol FROM watch WHERE user=?",
                      (update.effective_user.id,)).fetchall()
    syms = [r[0] for r in rows]
    await update.message.reply_text("Your subscriptions:\n" + "\n".join(syms or ["(none)"]))

async def list_alerts(update,ctx):
    today = date.today().isoformat()
    rows = DB.execute(
      "SELECT symbol,headline FROM alerts WHERE user=? AND date(ts)=?",
      (update.effective_user.id,today)
    ).fetchall()
    lines = [f"{s}: {h}" for s,h in rows]
    await update.message.reply_text("Today's alerts:\n"+("\n".join(lines) or "(none)"))

async def unwatch_cb(update,ctx):
    data = update.callback_query.data  # e.g. "UNW_TCS"
    sym = data.split("_",1)[1]
    uid = update.effective_user.id
    DB.execute("DELETE FROM watch WHERE user=? AND symbol=?", (uid,sym))
    DB.commit()
    await update.callback_query.answer(f"Removed {sym}")
    await list_subs(update,ctx)

async def cmd_pricealert(update,ctx):
    sym,op,thr = ctx.args[0].upper(), ctx.args[1], float(ctx.args[2])
    DB.execute("INSERT INTO price_alerts(user,symbol,op,threshold) VALUES(?,?,?,?)",
               (update.effective_user.id,sym,op,thr))
    DB.commit()
    await update.message.reply_text(f"Price alert set: {sym} {op} {thr}")

async def cmd_rsialert(update,ctx):
    sym,op,thr = ctx.args[0].upper(), ctx.args[1], float(ctx.args[2])
    DB.execute("INSERT INTO rsi_alerts(user,symbol,op,threshold) VALUES(?,?,?,?)",
               (update.effective_user.id,sym,op,thr))
    DB.commit()
    await update.message.reply_text(f"RSI alert set: {sym} {op} {thr}")

async def cmd_testdigest(update,ctx):
    daily_digest(ctx.application)
    await update.message.reply_text("Digest sent.")

# ─────────────────────────── MAIN ───────────────────────────── #

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # register handlers
    app.add_handler(CommandHandler(["help","menu"], cmd_help))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("unwatch", cmd_unwatch))
    app.add_handler(CommandHandler(["list","subscriptionlist"], list_subs))
    app.add_handler(CommandHandler("alertslist", list_alerts))
    app.add_handler(CallbackQueryHandler(unwatch_cb, pattern="^UNW_"))
    app.add_handler(CommandHandler("pricealert", cmd_pricealert))
    app.add_handler(CommandHandler("rsialert", cmd_rsialert))
    app.add_handler(CommandHandler("testdigest", cmd_testdigest))

    # scheduler
    sched = BackgroundScheduler()
    sched.add_job(lambda: asyncio.run(dispatch_filings(app)),
                  'interval', seconds=POLL_INTERVAL,
                  next_run_time=datetime.now(), id="filings",
                  max_instances=1, coalesce=True)
    sched.add_job(lambda: check_price_alerts(app),
                  'interval', minutes=1, id="price")
    sched.add_job(lambda: check_rsi_alerts(app),
                  'interval', minutes=5, id="rsi")
    sched.add_job(lambda: daily_digest(app),
                  CronTrigger(hour=18, minute=0), id="digest")
    sched.start()

    log.info("Bot starting …")
    app.run_polling()

if __name__=="__main__":
    main()
