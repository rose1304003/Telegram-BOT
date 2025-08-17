import os
import io
import csv
import logging
import asyncio
import re
from datetime import datetime, timedelta

from dotenv import load_dotenv
from pathlib import Path
load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from openai import OpenAI

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from storage import Storage, ensure_db
from summarizer import summarize_window, build_keyword_flags

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("chatgpt-secretary")

# --- ENV ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
LOCAL_TZ = os.getenv("LOCAL_TZ", "Asia/Tashkent")
DB_PATH = os.getenv("DB_PATH", "data/bot.db")
DEFAULT_DIGEST_TIME = os.getenv("DEFAULT_DIGEST_TIME", "21:00")

# NEW: control public replies on keyword hits (default = silent)
KEYWORD_REPLY = os.getenv("KEYWORD_REPLY", "0") == "1"
log.info(f"KEYWORD_REPLY={KEYWORD_REPLY}")

# optional allow-list of chat ids (comma-separated)
ALLOWED_CHAT_IDS = [int(cid) for cid in os.getenv("ALLOWED_CHAT_IDS", "").replace(" ", "").split(",") if cid]

if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
if not OPENAI_API_KEY:
    raise SystemExit("OPENAI_API_KEY is not set")

client = OpenAI(api_key=OPENAI_API_KEY)
storage = Storage(DB_PATH)

# Single global scheduler in LOCAL_TZ
scheduler = AsyncIOScheduler(timezone=LOCAL_TZ)

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_chat(update.effective_chat.id):
        return
    await update.message.reply_text(
        "Bot ishga tushdi ✅\n"
        "Buyruqlar:\n"
        "/chatid — joriy chat ID\n"
        "/search <so‘rov> — tarix bo‘yicha qidirish\n"
        "/stats — 7 kunlik oddiy statistika\n"
        "/digest_today — bugungi xulosa\n"
        "/digest_week — 7 kunlik xulosa\n"
        "/digest_time HH:MM — kunlik digest vaqti\n"
        "/keywords — kuzatilayotgan so‘zlar\n"
        "/set_keywords a,b,c — ro‘yxatni yangilash\n"
        "/hits_today — bugungi keyword hitlar soni\n"
        "/export_hits [kun] — CSV eksport (default 7)"
    )

async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_chat(update.effective_chat.id):
        return
    await update.message.reply_text(f"Chat ID: {update.effective_chat.id}")

async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_chat(update.effective_chat.id):
        return
    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text("Foydalanish: /search so‘rov")
        return
    results = storage.search(update.effective_chat.id, query, limit=20)
    if not results:
        await update.message.reply_text("Hech narsa topilmadi.")
        return
    lines = []
    for r in results:
        ts = datetime.fromtimestamp(r["date"]).strftime("%Y-%m-%d %H:%M")
        user = f"@{r['username']}" if r["username"] else r["user_id"]
        snippet = (r["text"][:200] + "…") if len(r["text"]) > 200 else r["text"]
        lines.append(f"• {ts} — {user}: {snippet}")
    await update.message.reply_text("\n".join(lines))

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_chat(update.effective_chat.id):
        return
    since = int((datetime.utcnow() - timedelta(days=7)).timestamp())
    top = storage.top_users(update.effective_chat.id, since, limit=10)
    total = storage.count_messages(update.effective_chat.id, since)
    if not total:
        await update.message.reply_text("7 kunlik statistika bo‘sh.")
        return
    lines = [f"Oxirgi 7 kunda jami xabarlar: {total}", "Top ishtirokchilar:"]
    for u in top:
        uname = f"@{u['username']}" if u["username"] else u["user_id"]
        lines.append(f"• {uname} — {u['cnt']} ta")
    await update.message.reply_text("\n".join(lines))

async def digest_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_chat(update.effective_chat.id):
        return
    day_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    since = int(day_start.timestamp())
    msgs = storage.get_messages(update.effective_chat.id, since)
    if not msgs:
        await update.message.reply_text("Bugun uchun xabarlar yo‘q.")
        return
    digest = await summarize_window(client, OPENAI_MODEL, msgs, period_label="(bugun)")
    await update.message.reply_text(digest, parse_mode=ParseMode.MARKDOWN)

async def digest_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_chat(update.effective_chat.id):
        return
    since = int((datetime.utcnow() - timedelta(days=7)).timestamp())
    msgs = storage.get_messages(update.effective_chat.id, since)
    if not msgs:
        await update.message.reply_text("7 kunlik xabarlar yo‘q.")
        return
    digest = await summarize_window(client, OPENAI_MODEL, msgs, period_label="(7 kun)")
    await update.message.reply_text(digest, parse_mode=ParseMode.MARKDOWN)

async def digest_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_chat(update.effective_chat.id):
        return
    if not context.args:
        cur = storage.get_digest_time(update.effective_chat.id) or DEFAULT_DIGEST_TIME
        await update.message.reply_text(f"Hozirgi kunlik digest vaqti: {cur}\n"
                                        f"Namuna: /digest_time 21:30")
        return
    time_str = context.args[0]
    if not re.match(r"^\d{2}:\d{2}$", time_str):
        await update.message.reply_text("Iltimos HH:MM formatida kiriting, masalan: 21:30")
        return
    storage.set_digest_time(update.effective_chat.id, time_str)
    await update.message.reply_text(f"Kunlik digest vaqti yangilandi: {time_str}")

async def show_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_chat(update.effective_chat.id):
        return
    kws = storage.get_keywords(update.effective_chat.id)
    # Use simple concat to avoid quote issues in f-strings
    msg = "Kuzatilayotgan so‘zlar: " + (kws if kws else "(yo‘q)")
    await update.message.reply_text(msg)

async def set_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_chat(update.effective_chat.id):
        return
    kws = " ".join(context.args) if context.args else ""
    storage.set_keywords(update.effective_chat.id, kws)
    await update.message.reply_text("Kuzatilayotgan so‘zlar yangilandi: " + (kws if kws else "(bo‘sh)"))

# --- Extra: simple counters/exports for keyword hits (works if storage has keyword_hits methods)
async def hits_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_chat(update.effective_chat.id):
        return
    since = int(datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    if hasattr(storage, "count_hits"):
        n = storage.count_hits(update.effective_chat.id, since)
        await update.message.reply_text(f"Bugun kalit so‘z topilgan xabarlar: {n}")
    else:
        await update.message.reply_text("Keyword hits hisoboti yoqilmagan (storage.py yangilash kerak).")

async def export_hits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_chat(update.effective_chat.id):
        return
    if not hasattr(storage, "get_hits"):
        await update.message.reply_text("Keyword hits eksporti yoqilmagan (storage.py yangilash kerak).")
        return
    try:
        days = int(context.args[0]) if context.args else 7
    except Exception:
        days = 7
    since = int((datetime.utcnow() - timedelta(days=days)).timestamp())
    rows = storage.get_hits(update.effective_chat.id, since)
    if not rows:
        await update.message.reply_text(f"Oxirgi {days} kunda kalit so‘z topilmadi.")
        return
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["datetime_utc", "user", "matched_keywords", "message"])
    for r in rows:
        ts = datetime.utcfromtimestamp(r["date"]).strftime("%Y-%m-%d %H:%M")
        user = ("@" + r["username"]) if r.get("username") else (str(r.get("user_id") or ""))
        w.writerow([ts, user, r["matched"], r["text"].replace("\n", " ")])
    data = buf.getvalue().encode("utf-8-sig")
    bio = io.BytesIO(data); bio.name = f"keyword_hits_{days}d.csv"
    await update.message.reply_document(document=bio, filename=bio.name,
                                        caption=f"Kalit so‘zlar bo‘yicha hitlar — oxirgi {days} kun")

# --- Message capture ---
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        # You may choose to store DMs too; we skip here
        pass

    if not allow_chat(chat.id):
        return

    # Only store text messages
    if not msg or not msg.text:
        return

    storage.insert_message(
        chat_id=chat.id,
        message_id=msg.message_id,
        user_id=msg.from_user.id if msg.from_user else None,
        username=msg.from_user.username if msg.from_user and msg.from_user.username else None,
        text=msg.text,
        date=int(msg.date.timestamp()) if msg.date else int(datetime.utcnow().timestamp())
    )

    # Keyword alert (now silent; optional DB logging)
    kws = storage.get_keywords(chat.id) or ""
    hits = build_keyword_flags(msg.text, kws)
    if hits:
        # Persist hit if storage supports it
        if hasattr(storage, "insert_keyword_hit"):
            try:
                storage.insert_keyword_hit(
                    chat_id=chat.id,
                    message_id=msg.message_id,
                    user_id=msg.from_user.id if msg.from_user else None,
                    username=msg.from_user.username if msg.from_user and msg.from_user.username else None,
                    matched=",".join(hits),
                    text=msg.text,
                    date=int(msg.date.timestamp()) if msg.date else int(datetime.utcnow().timestamp())
                )
            except Exception as e:
                log.warning("Failed to insert keyword hit: %s", e)

        # Always log to server logs for admins
        log.info("Keyword hit in chat %s: %s | %s",
                 chat.id, ", ".join(hits), msg.text[:200].replace("\n", " "))

        # Only reply publicly if KEYWORD_REPLY=1
        if KEYWORD_REPLY:
            await msg.reply_text("Topilgan kalit so‘zlar: " + ", ".join(hits))

# --- Helpers ---
def allow_chat(chat_id: int) -> bool:
    if not ALLOWED_CHAT_IDS:
        return True
    return chat_id in ALLOWED_CHAT_IDS

def setup_scheduler(app: Application):
    async def daily_digest_job():
        # For every chat with activity/settings, check whether we should send digest now
        now_local = datetime.now().strftime("%H:%M")
        for chat_id in storage.all_chats():
            desired = storage.get_digest_time(chat_id) or DEFAULT_DIGEST_TIME
            if desired == now_local:
                # pull messages since local midnight
                day_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
                since = int(day_start.timestamp())
                msgs = storage.get_messages(chat_id, since)
                if not msgs:
                    continue
                digest = await summarize_window(client, OPENAI_MODEL, msgs, period_label="(kunlik)")
                try:
                    await app.bot.send_message(chat_id=chat_id, text=digest, parse_mode=ParseMode.MARKDOWN)
                except Exception as e:
                    log.warning("Failed to send digest to %s: %s", chat_id, e)

    scheduler.add_job(daily_digest_job, CronTrigger(minute="*"))
    scheduler.start()

def build_app() -> Application:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("chatid", chatid))
    application.add_handler(CommandHandler("search", search))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("digest_today", digest_today))
    application.add_handler(CommandHandler("digest_week", digest_week))
    application.add_handler(CommandHandler("digest_time", digest_time))
    application.add_handler(CommandHandler("keywords", show_keywords))
    application.add_handler(CommandHandler("set_keywords", set_keywords))

    # Extra (only useful if storage has keyword_hits):
    application.add_handler(CommandHandler("hits_today", hits_today))
    application.add_handler(CommandHandler("export_hits", export_hits))

    # capture every text message
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    return application

def main():
    ensure_db(DB_PATH)

    app = build_app()
    setup_scheduler(app)

    log.info("Deleting webhook (if any) and starting long-polling worker...")
    app.run_polling(
        stop_signals=None,         # keep alive on PaaS workers
        close_loop=False,          # don't close event loop (friendlier on some hosts)
        drop_pending_updates=False # set True if you want to discard backlog when rebooting
    )

if __name__ == "__main__":
    main()
