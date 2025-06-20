#!/usr/bin/env python3
"""
Telegram Stock-Alert Bot   |   Python 3.10+
Pushes NSE/BSE corporate filings (+ optional NewsAPI headlines)
to Telegram users who subscribe with /watch <SYMBOL>.
"""

# ───────────────────────────── imports ───────────────────────────── #

import os, html, asyncio, logging, sqlite3
from typing import List, Dict, Any, Optional

import aiohttp
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update, constants
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes
)

# ────────────── ENV / CONFIG (all via Railway Variables) ─────────── #

load_dotenv()                                    # reads .env when running locally
BOT_TOKEN      = os.getenv("7932346974:AAEG4V-RwQVbzXWQIwwyz7S-EedVdSMtNzY")           # Telegram Bot token
NEWSAPI_KEY    = os.getenv("70815b5109c14f1386a363733082b65e")        # optional
POLL_INTERVAL  = int(os.getenv("POLL_INTERVAL", 15))   # ≥5 s recommended

if not BOT_TOKEN:
    raise RuntimeError("⚠️  TG_TOKEN environment variable not set!")

DB = sqlite3.connect("watchlist.db", check_same_thread=False)
DB.execute("CREATE TABLE IF NOT EXISTS watch (user INTEGER, symbol TEXT)")
DB.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_user_sym ON watch(user, symbol)")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s: %(message)s"
)
log = logging.getLogger("alert-bot")

# ─────────────────────────── utilities ──────────────────────────── #

SESSION: Optional[aiohttp.ClientSession] = None
LAST_IDS: Dict[str, Any] = {}     # per-source dedup

async def get_json(url: str, headers: dict | None = None) -> Any:
    """GET URL → JSON with small retry & 10 s timeout."""
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
                log.warning("Fetch %s status %s", url, resp.status)
                return None
    return None

# ─────────────────────────── fetchers ───────────────────────────── #

async def fetch_nse() -> List[dict]:
    url = "https://www.nseindia.com/api/corporate-announcements?index=equities"
    hdr = {"user-agent": "Mozilla/5.0", "referer": "https://www.nseindia.com"}
    data = await get_json(url, hdr)
    if not data:
        return []
    ann = data.get("data", [])
    fresh = []
    for row in ann:
        uid = row["id"]
        if uid == LAST_IDS.get("nse"):
            break
        fresh.append({
            "symbol": row["symbol"],
            "headline": row["headline"],
            "link": row["attachPath"]
        })
    if ann:
        LAST_IDS["nse"] = ann[0]["id"]
    return fresh

async def fetch_bse() -> List[dict]:
    url = "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w?strCat=-1"
    ann = await get_json(url)
    if not isinstance(ann, list):
        return []
    fresh = []
    for row in ann:
        uid = row["SCRIP_CD"] + row["NEWS_DT"]
        if uid == LAST_IDS.get("bse"):
            break
        fresh.append({
            "symbol": row["SCRIP_CD"],
            "headline": row["NEWS_SUB"],
            "link": row["ATTACHMENTNAME"]
        })
    if ann:
        LAST_IDS["bse"] = ann[0]["SCRIP_CD"] + ann[0]["NEWS_DT"]
    return fresh

async def fetch_newsapi() -> List[dict]:
    if not NEWSAPI_KEY:
        return []
    cur = DB.execute("SELECT DISTINCT symbol FROM watch LIMIT 20")
    symbols = [r[0] for r in cur.fetchall()]
    if not symbols:
        return []
    query = " OR ".join(symbols) + " AND India"
    url = (
        "https://newsapi.org/v2/everything"
        f"?q={query}&language=en&sortBy=publishedAt&pageSize=20"
        f"&apiKey={NEWSAPI_KEY}"
    )
    data = await get_json(url)
    if not data or data.get("status") != "ok":
        return []
    fresh = []
    for art in data["articles"]:
        uid = art["url"]
        if uid == LAST_IDS.get("newsapi"):
            break
        fresh.append({
            "symbol": "",           # match later by keyword
            "headline": art["title"],
            "link": art["url"]
        })
    if data["articles"]:
        LAST_IDS["newsapi"] = data["articles"][0]["url"]
    return fresh

# ─────────────────────── alert dispatcher ──────────────────────── #

async def dispatch_announcements(app):
    combined: List[dict] = []
    combined += await fetch_nse()
    combined += await fetch_bse()
    combined += await fetch_newsapi()

    if not combined:
        return

    rows = DB.execute("SELECT user, symbol FROM watch").fetchall()
    watch_map: Dict[str, List[int]] = {}
    for uid, sym in rows:
        watch_map.setdefault(sym.upper(), []).append(uid)

    for item in combined:
        sym = item["symbol"].upper()
        target_uids = watch_map.get(sym, [])
        if not target_uids and sym == "":
            for wsym, uids in watch_map.items():
                if wsym in item["headline"].upper():
                    target_uids.extend(uids)
        if not target_uids:
            continue

        text = (
            f"🔔 <b>{html.escape(sym or 'NEWS')}</b>\n"
            f"{html.escape(item['headline'])}\n"
            f"<a href='{item['link']}'>Open</a>"
        )
        for uid in set(target_uids):
            try:
                await app.bot.send_message(
                    uid, text,
                    parse_mode=constants.ParseMode.HTML,
                    disable_web_page_preview=False
                )
            except Exception as e:
                log.warning("Send failed to %s : %s", uid, e)

# ─────────────────────── command handlers ──────────────────────── #

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "नमस्ते! `/watch TCS` लिखकर स्टॉक ट्रैक करें.\n"
        "`/help` से सभी कमांड देखें.",
        parse_mode=constants.ParseMode.MARKDOWN
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/watch <SYMBOL> – ट्रैक चालू करें\n"
        "/unwatch <SYMBOL> – हटाएँ\n"
        "/list – आपकी वॉचलिस्ट\n"
        "/latency <seconds> – polling interval बदलें",
        parse_mode=constants.ParseMode.MARKDOWN
    )

async def cmd_watch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("उदाहरण: /watch RELIANCE")
        return
    sym = ctx.args[0].upper()
    DB.execute("INSERT OR IGNORE INTO watch VALUES (?,?)",
               (update.effective_user.id, sym))
    DB.commit()
    await update.message.reply_text(f"📈 {sym} added.")

async def cmd_unwatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("उदाहरण: /unwatch RELIANCE")
        return
    sym = ctx.args[0].upper()
    DB.execute("DELETE FROM watch WHERE user=? AND symbol=?",
               (update.effective_user.id, sym))
    DB.commit()
    await update.message.reply_text(f"❌ {sym} removed.")

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = DB.execute("SELECT symbol FROM watch WHERE user=?",
                      (update.effective_user.id,)).fetchall()
    if not rows:
        await update.message.reply_text("आपकी वॉचलिस्ट खाली है.")
    else:
        syms = ", ".join(r[0] for r in rows)
        await update.message.reply_text(f"👀 {syms}")

async def cmd_latency(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global POLL_INTERVAL
    if not ctx.args:
        await update.message.reply_text(f"Current interval: {POLL_INTERVAL}s")
        return
    try:
        new = int(ctx.args[0])
        if new < 5:
            await update.message.reply_text("Minimum 5 seconds allowed.")
            return

        POLL_INTERVAL = new
        scheduler = ctx.application.job_queue.scheduler
        job = scheduler.get_job("poller")
        if job:
            job.reschedule(trigger='interval', seconds=POLL_INTERVAL)

        await update.message.reply_text(
            f"⏱️ Poll interval set to {POLL_INTERVAL}s."
        )
    except ValueError:
        await update.message.reply_text("Usage: /latency 10")

# ─────────────────────────────  main  ───────────────────────────── #

async def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .read_timeout(20)
        .write_timeout(20)
        .build()
    )

    # register commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("unwatch", cmd_unwatch))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("latency", cmd_latency))

    # scheduler job
    sched = AsyncIOScheduler()
    sched.add_job(
        lambda: dispatch_announcements(app),
        "interval", seconds=POLL_INTERVAL, id="poller"
    )
    sched.start()

    log.info("Bot starting …")
    await app.run_polling()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped.")
