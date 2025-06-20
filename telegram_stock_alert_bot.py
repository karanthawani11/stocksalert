#!/usr/bin/env python3
"""
Telegram Stock Alert Bot
=======================
A single‑file Python bot that watches Indian stock‑market news & regulatory filings
and pushes alerts to users on Telegram.  

Main features
-------------
* /watch <SYMBOL> – add a stock to personal watch‑list.
* /unwatch <SYMBOL> – remove it.
* /list – show current watch‑list.
* /watchall on|off – toggle global feed (every filing for every symbol).
* /latency <seconds> – adjust polling interval (default 15 s).

Data Sources implemented
------------------------
1. **NSE Corporate Filings** (fastest, free)
2. **BSE Corporate Announcements** (free)
3. **NewsAPI** headlines (optional – requires NEWSAPI_KEY)

Dependencies
------------
```
python-telegram-bot>=21.0
apscheduler
requests
python-dotenv
```

Environment variables (via .env or shell)
-----------------------------------------
```
TG_TOKEN="7932346974:AAEG4V-RwQVbzXWQIwwyz7S-EedVdSMtNzY"
NEWSAPI_KEY="70815b5109c14f1386a363733082b65e"
POLL_INTERVAL="15"           # seconds between fetch cycles (int)
```

Run locally
-----------
```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python telegram_stock_alert_bot.py
```

Created for educational use; respect NSE/BSE terms and SEBI PIT regulations.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from typing import Dict, List, Literal, Optional

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          filters)

# ---------------------------------------------------------------------------
# ── Environment & Globals ───────────────────────────────────────────────────
# ---------------------------------------------------------------------------
load_dotenv()
BOT_TOKEN: str = os.environ["TG_TOKEN"]
NEWSAPI_KEY: Optional[str] = os.getenv("NEWSAPI_KEY")
POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL", "15"))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}

DB = sqlite3.connect("watchlist.db")
DB.execute(
    """CREATE TABLE IF NOT EXISTS watch (
    user INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    UNIQUE(user, symbol)
)"""
)
DB.execute(
    """CREATE TABLE IF NOT EXISTS settings (
    user INTEGER PRIMARY KEY,
    watch_all INTEGER DEFAULT 0,
    latency INTEGER DEFAULT 15
)"""
)
DB.commit()

LAST_IDS: Dict[str, str] = {}  # per‑source de‑duplication memory

# ---------------------------------------------------------------------------
# ── Helpers for DB ops ─────────────────────────────────────────────────────-
# ---------------------------------------------------------------------------

def add_symbol(user_id: int, symbol: str):
    with closing(DB.cursor()) as cur:
        cur.execute("INSERT OR IGNORE INTO watch VALUES (?,?)", (user_id, symbol))
        DB.commit()


def remove_symbol(user_id: int, symbol: str):
    with closing(DB.cursor()) as cur:
        cur.execute("DELETE FROM watch WHERE user=? AND symbol=?", (user_id, symbol))
        DB.commit()


def list_symbols(user_id: int) -> List[str]:
    with closing(DB.cursor()) as cur:
        return [row[0] for row in cur.execute("SELECT symbol FROM watch WHERE user=?", (user_id,))]


def set_watch_all(user_id: int, value: bool):
    with closing(DB.cursor()) as cur:
        cur.execute(
            "INSERT INTO settings(user, watch_all) VALUES (?,?) ON CONFLICT(user) DO UPDATE SET watch_all=excluded.watch_all",
            (user_id, int(value)),
        )
        DB.commit()


def get_watch_all(user_id: int) -> bool:
    with closing(DB.cursor()) as cur:
        row = cur.execute("SELECT watch_all FROM settings WHERE user=?", (user_id,)).fetchone()
        return bool(row[0]) if row else False


# ---------------------------------------------------------------------------
# ── Data‑source fetchers ────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

async def fetch_json(session: aiohttp.ClientSession, url: str) -> Optional[dict]:
    try:
        async with session.get(url, headers=HEADERS, timeout=10) as resp:
            if resp.status != 200:
                return None
            return await resp.json()
    except Exception:
        return None


async def nse_announcements(session: aiohttp.ClientSession):
    url = "https://www.nseindia.com/api/corporate-announcements?index=equities"
    data = await fetch_json(session, url)
    if not data or "data" not in data:
        return []
    ann = []
    for row in data["data"]:
        ann.append({
            "id": row["id"],
            "symbol": row["symbol"],
            "headline": row["headline"],
            "link": row.get("attachPath") or row.get("pdfUrl", ""),
            "ts": row.get("time", "")
        })
    return ann


async def bse_announcements(session: aiohttp.ClientSession):
    url = "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w?strCat=-1&strPrevDate=&strScrip=&strSearch=P"
    # undocumented endpoint — may change; fallback to scraping if needed.
    data = await fetch_json(session, url)
    if not data or "Table" not in data:
        return []
    ann = []
    for row in data["Table"]:
        ann.append({
            "id": row["INTIMATION_ID"],
            "symbol": row["SCRIP_CD"],
            "headline": row["SUBJECT"],
            "link": row["ATTACHMENT"],
            "ts": row["DATETIME"],
        })
    return ann


async def newsapi_headlines(session: aiohttp.ClientSession, symbols: List[str]):
    if NEWSAPI_KEY is None:
        return []
    items = []
    # Batch symbols into query to save quota
    query = " OR ".join(symbols[:20])  # NewsAPI query length limit
    url = (
        "https://newsapi.org/v2/everything?" +
        f"q={query}&language=en&sortBy=publishedAt&pageSize=30&apiKey={NEWSAPI_KEY}"
    )
    data = await fetch_json(session, url)
    if not data or data.get("status") != "ok":
        return []
    for art in data.get("articles", []):
        items.append({
            "id": art["url"],
            "symbol": "news",  # generic; we will regex filter later
            "headline": art["title"],
            "link": art["url"],
            "ts": art["publishedAt"],
        })
    return items


# ---------------------------------------------------------------------------
# ── Announcement dispatcher ────────────────────────────────────────────────
# ---------------------------------------------------------------------------

async def poll_and_push(app: Application):
    async with aiohttp.ClientSession() as session:
        nse = await nse_announcements(session)
        bse = await bse_announcements(session)

        merged = nse + bse
        if NEWSAPI_KEY:
            # gather all unique symbols being watched for NewsAPI query optimisation
            cur = DB.execute("SELECT DISTINCT symbol FROM watch")
            symb = [row[0] for row in cur]
            news = await newsapi_headlines(session, symb)
            merged += news

    if not merged:
        return

    for item in merged:
        src_key = f"{item['id']}"  # unique per source
        if src_key == LAST_IDS.get(item["symbol"]):
            break  # older items reached
        LAST_IDS[item["symbol"]] = src_key

        # broadcast logic
        users = []
        if item["symbol"] == "news":
            # simple keyword filter against headline for each user symbol
            headline_low = item["headline"].lower()
            cur = DB.execute("SELECT DISTINCT user, symbol FROM watch")
            for uid, sym in cur:
                if sym.lower() in headline_low:
                    users.append(uid)
        else:
            # exchange filing: precise symbol match + global watchers
            cur = DB.execute("SELECT user FROM watch WHERE symbol=?", (item["symbol"],))
            users += [row[0] for row in cur]
            cur = DB.execute("SELECT user FROM settings WHERE watch_all=1")
            users += [row[0] for row in cur]
        users = set(users)
        if not users:
            continue

        text = (
            f"\u26A0\uFE0F <b>{html.escape(item['symbol'])}</b>\n"
            f"{html.escape(item['headline'])}\n"
            f"<a href='{item['link']}'>Open</a> | <i>{html.escape(item['ts'])}</i>"
        )
        for uid in users:
            try:
                await app.bot.send_message(uid, text=text, parse_mode="HTML", disable_web_page_preview=False)
            except Exception:
                pass  # ignore failures (bot blocked etc.)


# ---------------------------------------------------------------------------
# ── Telegram command handlers ──────────────────────────────────────────────
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "नमस्ते! मैं स्टॉक अलर्ट बोट हूँ. /watch <SYMBOL> से ट्रैकिंग शुरू करें. \n/help देखें."
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Available commands:\n"
        "/watch <SYM> – add stock\n"
        "/unwatch <SYM> – remove\n"
        "/list – show watch‑list\n"
        "/watchall on|off – global feed\n"
        "/latency <sec> – set poll interval"
    )


async def cmd_watch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /watch RELIANCE")
        return
    sym = ctx.args[0].upper()
    add_symbol(update.effective_user.id, sym)
    await update.message.reply_text(f"✅ Now watching {sym}.")


async def cmd_unwatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /unwatch RELIANCE")
        return
    sym = ctx.args[0].upper()
    remove_symbol(update.effective_user.id, sym)
    await update.message.reply_text(f"❌ Stopped watching {sym}.")


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    syms = list_symbols(update.effective_user.id)
    if syms:
        await update.message.reply_text("Your watch‑list:\n" + ", ".join(syms))
    else:
        await update.message.reply_text("No symbols being watched.")


async def cmd_watchall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or ctx.args[0].lower() not in {"on", "off"}:
        await update.message.reply_text("Usage: /watchall on|off")
        return
    val = ctx.args[0].lower() == "on"
    set_watch_all(update.effective_user.id, val)
    await update.message.reply_text("🌐 Global feed " + ("enabled" if val else "disabled") + ".")


async def cmd_latency(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(f"Current latency: {POLL_INTERVAL}s")
        return
    try:
        new_lat = int(ctx.args[0])
        global POLL_INTERVAL
        POLL_INTERVAL = max(5, new_lat)
        await update.message.reply_text(f"⏱️ Poll interval set to {POLL_INTERVAL}s (global setting). Restart bot to persist.")
    except ValueError:
        await update.message.reply_text("Please provide an integer number of seconds.")


# ---------------------------------------------------------------------------
# ── Main application bootstrap ────────────────────────────────────────────
# ---------------------------------------------------------------------------

async def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("unwatch", cmd_unwatch))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("watchall", cmd_watchall))
    app.add_handler(CommandHandler("latency", cmd_latency))

    # scheduler for polling
    scheduler = AsyncIOScheduler()
    scheduler.add_job(lambda: poll_and_push(app), "interval", seconds=POLL_INTERVAL)
    scheduler.start()

    print("Bot is up. Polling ...")
    await app.run_polling()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped.")
