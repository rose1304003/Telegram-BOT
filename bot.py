import os
import logging
import asyncio
import re
from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv()

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
DEFAULT_DIGEST_TIME = os.getenv("DEFAULT_DIGEST_TIME", "21:00")
DB_PATH = os.getenv("DB_PATH", "data/bot.db")
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
    await update.message.reply_text(
        "✅ Bot ishga tushdi. Dayjestlar uchun xabarlarni to'plab boraman.\n"
        "Guruh: /search, /stats, /digest_today, /digest_week, /digest_time HH:MM, /keywords, /set_keywords ..."
    )

def _allowed_chat(update: Update) -> bool:
    if not ALLOWED_CHAT_IDS:
        return True
    chat_id = update.effective_chat.id if update.effective_chat else None
    return chat_id in ALLOWED_CHAT_IDS

async def set_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update):
        return
    chat_id = update.effective_chat.id
    text = " ".join(context.args).strip()

    if not text:
        await update.message.reply_text(
            "❌ Siz kalit so'zlar kiritmadingiz.\n"
            "Foydalanish: /set_keywords so'z1, so'z2, so'z3"
        )
        return

    storage.set_keywords(chat_id, text)
    await update.message.reply_text(f"✅ Kalit so'zlar yangilandi:\n{text}")

async def show_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update):
        return
    chat_id = update.effective_chat.id
    kw = storage.get_keywords(chat_id) or os.getenv("TRACKED_KEYWORDS", "")
    if not kw:
        await update.message.reply_text("🔎 Kuzatilayotgan so'zlar:\n— (hali o'rnatilmagan)")
    else:
        await update.message.reply_text(f"🔎 Kuzatilayotgan so'zlar:\n{kw}")

async def save_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Store messages from groups/supergroups and trigger keyword alerts."""
    message = update.effective_message
    chat = update.effective_chat

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if not _allowed_chat(update):
        return
    if not message or not message.text:
        return
    if message.from_user and message.from_user.is_bot:
        return

    storage.save_message(
        chat_id=chat.id,
        message_id=message.message_id,
        user_id=message.from_user.id if message.from_user else None,
        username=message.from_user.username if message.from_user else None,
        text=message.text,
        date=int(message.date.timestamp())
    )

    # Keyword flags
    kws = storage.get_keywords(chat.id) or os.getenv("TRACKED_KEYWORDS", "")
    flags = build_keyword_flags(message.text, kws)
    if flags:
        await message.reply_text(f"📌 Topilgan kalit so'zlar: {', '.join(flags)}")

async def digest_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update):
        return
    chat_id = update.effective_chat.id
    now = datetime.now()
    start = now - timedelta(days=1)
    msgs = storage.get_messages(chat_id, int(start.timestamp()), int(now.timestamp()))
    if not msgs:
        await update.message.reply_text("Son'ngi 24 soatda xabarlar yo'q.")
        await update.message.reply_text("За последние 24 часа сообщений нет.")
        return
    digest = await summarize_window(client, OPENAI_MODEL, msgs, period_label="So'ngi 24 soat")
    await update.message.reply_text(digest, parse_mode=ParseMode.MARKDOWN)

async def digest_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update):
        return
    chat_id = update.effective_chat.id
    now = datetime.now()
    start = now - timedelta(days=7)
    msgs = storage.get_messages(chat_id, int(start.timestamp()), int(now.timestamp()))
    if not msgs:
        await update.message.reply_text("So'ngi ohirgi haftada xabarlar yo'q.")
        return
    digest = await summarize_window(client, OPENAI_MODEL, msgs, period_label="So'ngi 7 kun")
    if not digest:
        await update.message.reply_text("Daydjest yaratishda xatolik yuz berdi.")
        return
    await update.message.reply_text(digest, parse_mode=ParseMode.MARKDOWN)

time_re = re.compile(r"^(?P<h>\d{1,2}):(?P<m>\d{2})$")

async def digest_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update):
        return
    chat_id = update.effective_chat.id
    if not context.args:
        current = storage.get_digest_time(chat_id) or DEFAULT_DIGEST_TIME
        await update.message.reply_text(f"⏰ Joriy kunlik dayjest vaqti: {current}")
        return
    t = context.args[0]
    m = time_re.match(t)
    if not m or not (0 <= int(m['h']) < 24 and 0 <= int(m['m']) < 60):
        await update.message.reply_text("Vaqtni HH:MM formatida kiriting, masalan: /digest_time 21:30")
        return
    storage.set_digest_time(chat_id, t)
    await update.message.reply_text(f"✅ Kunlik dayjest vaqti yangilandi:{t}\n"
                                    f"Men dayjestni har kuni shu vaqtda yuboraman.")

async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update):
        return
    chat_id = update.effective_chat.id
    q = " ".join(context.args).strip()
    if not q:
        await update.message.reply_text("Foydalanish: /search so‘rov")
        return
    rows = storage.search_messages(chat_id, q, limit=20)
    if not rows:
        await update.message.reply_text("Hech narsa topilmadi.")
        return
    lines = [f"• @{r['username'] or r['user_id']}: {r['text'][:150]}" for r in rows]
    await update.message.reply_text("Topildi:\n" + "\n".join(lines))

async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.message.reply_text(f"Chat ID: {chat.id} (type: {chat.type})")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed_chat(update):
        return
    chat_id = update.effective_chat.id
    now = datetime.now()
    start = now - timedelta(days=7)
    top = storage.top_users(chat_id, since=int(start.timestamp()), limit=10)
    if not top:
        await update.message.reply_text("So'nggi 7 kun bo'yicha statistika yo'q.")
        return
    lines = [f"{i+1}. @{u or uid}: {cnt}" for i, (uid, u, cnt) in enumerate(top)]
    await update.message.reply_text("*So'nggi 7 kun ichidagi eng faol ishtirokchilar:*\n" + "\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def scheduled_digest_job(app: Application):
    chats = storage.all_chats()
    now = datetime.now()
    for chat_id in chats:
        t = storage.get_digest_time(chat_id) or DEFAULT_DIGEST_TIME
        h, m = map(int, t.split(":"))
        if now.hour == h and now.minute == m:
            start = now - timedelta(days=1)
            msgs = storage.get_messages(chat_id, int(start.timestamp()), int(now.timestamp()))
            if not msgs:
                continue
            try:
                digest = await summarize_window(client, OPENAI_MODEL, msgs, period_label="за последние 24 часа")
                await app.bot.send_message(chat_id=chat_id, text=digest, parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                log.exception("Failed to send scheduled digest to %s: %s", chat_id, e)

def setup_scheduler(app: Application):
    async def run_scheduled_job():
        await scheduled_digest_job(app)

    scheduler.add_job(lambda: asyncio.get_event_loop().create_task(run_scheduled_job()),
                      CronTrigger(minute="*"))
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

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, save_message))
    return application

def main():
    ensure_db(DB_PATH)
    app = build_app()
    setup_scheduler(app)

    log.info("Deleting webhook (if any) and starting long-polling worker...")
    # Option A: pure worker process using polling, no public URL needed.
    # PTB will delete webhook for us if we pass drop_pending_updates=...
    app.run_polling(
        stop_signals=None,         # keep alive on PaaS workers
        close_loop=False,          # don't close event loop (friendlier on some hosts)
        drop_pending_updates=False # set True if you want to discard backlog when rebooting
    )

if __name__ == "__main__":
    main()

