import os, io, csv, logging, re
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from openai import OpenAI, AsyncOpenAI

from telegram import Update, File as TgFile
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import Forbidden, Conflict

from storage import Storage, ensure_db
from summarizer import summarize_window, build_keyword_flags

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("chatgpt-secretary")

# =========================
# ENV / CONFIG
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY     = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL       = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

LOCAL_TZ           = os.getenv("LOCAL_TZ", "Asia/Tashkent")
DB_PATH            = os.getenv("DB_PATH", "data/bot.db")
DEFAULT_DIGEST_TIME = os.getenv("DEFAULT_DIGEST_TIME", "21:00")

# Behavior toggles
KEYWORD_REPLY          = os.getenv("KEYWORD_REPLY", "0") == "1"  # public reply on hit? default OFF
AUTO_REPLY             = os.getenv("AUTO_REPLY", "1") == "1"     # suggest answer for admin DM
DM_ADMIN_ON_KEYWORD    = os.getenv("DM_ADMIN_ON_KEYWORD", "1") == "1"
DM_ADMIN_ON_SEARCH     = os.getenv("DM_ADMIN_ON_SEARCH", "1") == "1"
DM_ADMIN_DIGEST        = os.getenv("DM_ADMIN_DIGEST", "1") == "1"

# Event context (helps suggested answers)
EVENT_CONTEXT      = os.getenv("EVENT_CONTEXT", "")
EVENT_CONTEXT_PATH = os.getenv("EVENT_CONTEXT_PATH")
if EVENT_CONTEXT_PATH and Path(EVENT_CONTEXT_PATH).exists():
    EVENT_CONTEXT = Path(EVENT_CONTEXT_PATH).read_text(encoding="utf-8")

# Inspiration defaults (per-chat overrides via /set_inspire)
DEFAULT_INSPIRE_TIME      = os.getenv("INSPIRE_TIME", "21:00")
DEFAULT_INSPIRE_THRESHOLD = int(os.getenv("INSPIRE_THRESHOLD", "20"))

# Media / transcription
TRANSCRIBE_MEDIA = os.getenv("TRANSCRIBE_MEDIA", "1") == "1"
TRANSCRIBE_MODEL = os.getenv("TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
MAX_MEDIA_MB     = int(os.getenv("MAX_MEDIA_MB", "25"))
MEDIA_DIR        = os.getenv("MEDIA_DIR", "data/media")
KEEP_MEDIA       = os.getenv("KEEP_MEDIA", "0") == "1"
os.makedirs(MEDIA_DIR, exist_ok=True)

# Optional allow-list of chat IDs (comma-separated)
ALLOWED_CHAT_IDS = [int(cid) for cid in os.getenv("ALLOWED_CHAT_IDS", "").replace(" ", "").split(",") if cid]

if not TELEGRAM_BOT_TOKEN or not OPENAI_API_KEY:
    raise SystemExit("TELEGRAM_BOT_TOKEN or OPENAI_API_KEY missing")

client  = OpenAI(api_key=OPENAI_API_KEY)       # sync fallback
aclient = AsyncOpenAI(api_key=OPENAI_API_KEY)  # async for awaited calls

storage   = Storage(DB_PATH)
scheduler = AsyncIOScheduler(timezone=LOCAL_TZ)

INSPIRATIONS = [
    "Bugungi kichik qadamlar ertangi katta g‘alabaga olib boradi. Davom eting! 💪",
    "Har bir savol — o‘sish uchun imkoniyat. Savol bering, sinab ko‘ring, ilgarilang. ✨",
    "Birgalikda kuchlimiz. Bugun qilgan ishingiz ertaga boshqalarga ilhom bo‘ladi. 🌟",
    "Har yutuq — kichik urinishlardan boshlanadi. Siz uddalaysiz! 🚀",
]

# =========================
# Helpers
# =========================
def allow_chat(chat_id: int) -> bool:
    return (chat_id in ALLOWED_CHAT_IDS) if ALLOWED_CHAT_IDS else True

async def dm_admin(chat_id: int, text: str, app: Application, parse_mode=None):
    admin_id = storage.get_admin(chat_id)
    if not admin_id:
        return False
    try:
        await app.bot.send_message(chat_id=admin_id, text=text, parse_mode=parse_mode)
        return True
    except Forbidden:
        log.warning("Cannot DM admin %s. Ask them to /start the bot in DM.", admin_id)
        return False
    except Exception as e:
        log.warning("DM admin failed: %s", e)
        return False

def format_user(u) -> str:
    return f"@{getattr(u, 'username', None)}" if getattr(u, "username", None) else str(getattr(u, "id", ""))

async def suggested_answer(user_msg: str) -> str:
    if not AUTO_REPLY:
        return ""
    sys = (
        "You are a concise assistant for a Telegram event group. "
        "Answer in 2–3 short sentences, helpful and precise. "
        "Use the provided EVENT CONTEXT if relevant. If unsure, suggest what info is needed."
    )
    content = f"EVENT CONTEXT:\n{EVENT_CONTEXT}\n\nQUESTION:\n{user_msg}\n\nProvide a short, direct answer."
    try:
        resp = await aclient.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.2,
            messages=[{"role":"system","content":sys},{"role":"user","content":content}]
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.warning("Auto-reply (async) failed: %s", e)
    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.2,
            messages=[{"role":"system","content":sys},{"role":"user","content":content}]
        )
        return resp.choices[0].message.content.strip()
    except Exception as e2:
        log.warning("Auto-reply (sync fallback) failed: %s", e2)
        return ""

def _mb(n_bytes: int) -> float:
    return n_bytes / (1024 * 1024.0)

async def transcribe_file(path: str) -> str:
    if not TRANSCRIBE_MEDIA:
        return ""
    try:
        with open(path, "rb") as f:
            trx = await aclient.audio.transcriptions.create(model=TRANSCRIBE_MODEL, file=f)
        return (getattr(trx, "text", "") or "").strip()
    except Exception as e:
        log.warning("Transcription failed for %s: %s", path, e)
        return ""

# =========================
# Commands
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_chat(update.effective_chat.id):
        return
    await update.message.reply_text(
        "Bot ishga tushdi ✅\n"
        "/chatid — chat ID\n"
        "/search <so‘rov>\n"
        "/stats — 7 kun\n"
        "/digest_today, /digest_week\n"
        "/digest_time HH:MM\n"
        "/keywords, /set_keywords a,b,c\n"
        "/set_admin <user_id>\n"
        "/set_inspire HH:MM [threshold]\n"
        "/hits_today, /export_hits [kun]\n"
        "/whoami — your user id"
    )

async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.message.reply_text(f"Your user id: {u.id}\nUsername: {format_user(u)}")

async def set_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_chat(update.effective_chat.id):
        return
    if not context.args:
        await update.message.reply_text("Foydalanish: /set_admin <user_id>")
        return
    try:
        admin_id = int(context.args[0])
    except:
        await update.message.reply_text("Iltimos, to‘g‘ri user_id kiriting (raqam).")
        return
    storage.set_admin(update.effective_chat.id, admin_id)
    await update.message.reply_text(
        f"Admin DM yo‘naltirish o‘rnatildi: {admin_id}\n"
        f"Admin botga DM orqali /start yuborishi kerak."
    )

async def set_inspire(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_chat(update.effective_chat.id):
        return
    if not context.args:
        t, th = storage.get_inspire(update.effective_chat.id)
        await update.message.reply_text(
            f"Inspire: vaqt={t or DEFAULT_INSPIRE_TIME}, threshold={th or DEFAULT_INSPIRE_THRESHOLD}\n"
            f"Namuna: /set_inspire 21:00 20"
        )
        return
    time_str = context.args[0]
    if not re.match(r"^\d{2}:\d{2}$", time_str):
        await update.message.reply_text("HH:MM formatida kiriting, masalan 21:00")
        return
    threshold = int(context.args[1]) if len(context.args) > 1 else DEFAULT_INSPIRE_THRESHOLD
    storage.set_inspire(update.effective_chat.id, time_str, threshold)
    await update.message.reply_text(f"Inspire sozlandi: vaqt={time_str}, threshold={threshold}")

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
    if DM_ADMIN_ON_SEARCH and storage.get_admin(update.effective_chat.id):
        if not results:
            await dm_admin(update.effective_chat.id, f"[Search] '{query}': hech narsa topilmadi.", context.application)
            await update.message.reply_text("Qidiruv natijalari admin DM'ga yuborildi.")
            return
        lines = [f"[Search] '{query}' — {len(results)} natija:"]
        for r in results:
            ts = datetime.fromtimestamp(r["date"]).strftime("%Y-%m-%d %H:%M")
            user = f"@{r['username']}" if r["username"] else r["user_id"]
            snippet = (r["text"][:300] + "…") if len(r["text"]) > 300 else r["text"]
            lines.append(f"• {ts} — {user}: {snippet}")
        await dm_admin(update.effective_chat.id, "\n".join(lines), context.application)
        await update.message.reply_text("Qidiruv natijalari admin DM'ga yuborildi.")
    else:
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
        await update.message.reply_text(f"Hozirgi kunlik digest vaqti: {cur}\nNamuna: /digest_time 21:30")
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
    await update.message.reply_text("Kuzatilayotgan so‘zlar: " + (kws if kws else "(yo‘q)"))

async def set_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allow_chat(update.effective_chat.id):
        return
    kws = " ".join(context.args) if context.args else ""
    storage.set_keywords(update.effective_chat.id, kws)
    await update.message.reply_text("Kuzatilayotgan so‘zlar yangilandi: " + (kws if kws else "(bo‘sh)"))

# =========================
# Media handler (video / audio / voice / video_notes)
# =========================
async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.effective_message
    chat = update.effective_chat
    if not allow_chat(chat.id):
        return

    file_id, ext, size_bytes, label = None, None, 0, None
    caption = (msg.caption or "").strip()

    if msg.video:
        file_id = msg.video.file_id
        size_bytes = msg.video.file_size or 0
        ext, label = ".mp4", "[video]"
    elif msg.video_note:
        file_id = msg.video_note.file_id
        size_bytes = msg.video_note.file_size or 0
        ext, label = ".mp4", "[video_note]"
    elif msg.voice:
        file_id = msg.voice.file_id
        size_bytes = msg.voice.file_size or 0
        ext, label = ".ogg", "[voice]"
    elif msg.audio:
        file_id = msg.audio.file_id
        size_bytes = msg.audio.file_size or 0
        ext = ".mp3" if (msg.audio.mime_type or "").endswith("mpeg") else ".ogg"
        label = "[audio]"
    else:
        return

    if size_bytes and _mb(size_bytes) > MAX_MEDIA_MB:
        log.warning("Media skipped (%.1f MB > limit %d MB)", _mb(size_bytes), MAX_MEDIA_MB)
        return

    # download
    try:
        tgfile: TgFile = await context.bot.get_file(file_id)
        fname = f"{chat.id}_{msg.message_id}{ext}"
        path  = os.path.join(MEDIA_DIR, fname)
        await tgfile.download_to_drive(custom_path=path)
    except Exception as e:
        log.warning("Download failed: %s", e)
        return

    transcript = await transcribe_file(path)
    parts = [label]
    if caption:
        parts.append(caption)
    if transcript:
        parts.append(f"(transcript) {transcript}")
    text_to_store = " ".join([p for p in parts if p]).strip()

    storage.insert_message(
        chat_id=chat.id,
        message_id=msg.message_id,
        user_id=msg.from_user.id if msg.from_user else None,
        username=msg.from_user.username if msg.from_user and msg.from_user.username else None,
        text=text_to_store if text_to_store else label,
        date=int(msg.date.timestamp()) if msg.date else int(datetime.utcnow().timestamp())
    )

    # keyword detection on caption/transcript
    kws  = storage.get_keywords(chat.id) or ""
    hits = build_keyword_flags(text_to_store, kws)
    if hits:
        try:
            storage.insert_keyword_hit(
                chat_id=chat.id,
                message_id=msg.message_id,
                user_id=msg.from_user.id if msg.from_user else None,
                username=msg.from_user.username if msg.from_user and msg.from_user.username else None,
                matched=",".join(hits),
                text=text_to_store,
                date=int(msg.date.timestamp()) if msg.date else int(datetime.utcnow().timestamp())
            )
        except Exception as e:
            log.debug("insert_keyword_hit failed: %s", e)

        if DM_ADMIN_ON_KEYWORD and storage.get_admin(chat.id):
            ans = await suggested_answer(text_to_store)
            dm_text = (f"[Keyword] {', '.join(hits)}\n"
                       f"Chat: {chat.id}\n"
                       f"User: {format_user(msg.from_user)}\n"
                       f"Msg: {text_to_store}\n\n"
                       f"Suggested answer:\n{ans or '(no suggestion)'}")
            await dm_admin(chat.id, dm_text, context.application)

        if KEYWORD_REPLY:
            await msg.reply_text("Topilgan kalit so‘zlar: " + ", ".join(hits))

    if not KEEP_MEDIA:
        try:
            os.remove(path)
        except Exception:
            pass

# =========================
# Text messages
# =========================
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.effective_message
    chat = update.effective_chat
    if not allow_chat(chat.id) or not msg or not msg.text:
        return

    storage.insert_message(
        chat_id=chat.id,
        message_id=msg.message_id,
        user_id=msg.from_user.id if msg.from_user else None,
        username=msg.from_user.username if msg.from_user and msg.from_user.username else None,
        text=msg.text,
        date=int(msg.date.timestamp()) if msg.date else int(datetime.utcnow().timestamp())
    )

    kws = storage.get_keywords(chat.id) or ""
    hits = build_keyword_flags(msg.text, kws)
    if hits:
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
            log.debug("insert_keyword_hit failed: %s", e)

        if DM_ADMIN_ON_KEYWORD and storage.get_admin(chat.id):
            ans = await suggested_answer(msg.text)
            text = (f"[Keyword] {', '.join(hits)}\n"
                    f"Chat: {chat.id}\n"
                    f"User: {format_user(msg.from_user)}\n"
                    f"Msg: {msg.text}\n\n"
                    f"Suggested answer:\n{ans or '(no suggestion)'}")
            await dm_admin(chat.id, text, context.application)

        if KEYWORD_REPLY:
            await msg.reply_text("Topilgan kalit so‘zlar: " + ", ".join(hits))

# =========================
# Extras: hits stats / export
# =========================
async def hits_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    since = int(datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    n = storage.count_hits(update.effective_chat.id, since)
    await update.message.reply_text(f"Bugun kalit so‘z topilgan xabarlar: {n}")

async def export_hits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        days = int(context.args[0]) if context.args else 7
    except:
        days = 7
    since = int((datetime.utcnow() - timedelta(days=days)).timestamp())
    rows = storage.get_hits(update.effective_chat.id, since)
    if not rows:
        await update.message.reply_text(f"Oxirgi {days} kunda kalit so‘z topilmadi.")
        return
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["datetime_utc","user","matched_keywords","message"])
    for r in rows:
        ts = datetime.utcfromtimestamp(r["date"]).strftime("%Y-%m-%d %H:%M")
        user = ("@" + r["username"]) if r.get("username") else (str(r.get("user_id") or ""))
        w.writerow([ts, user, r["matched"], r["text"].replace("\n"," ")])
    data = buf.getvalue().encode("utf-8-sig"); bio = io.BytesIO(data); bio.name = f"keyword_hits_{days}d.csv"
    await update.message.reply_document(document=bio, filename=bio.name,
        caption=f"Kalit so‘zlar bo‘yicha hitlar — oxirgi {days} kun")

# =========================
# Scheduler (daily digest + inspiration + admin DM)
# =========================
def setup_scheduler(app: Application):
    async def minute_tick():
        now_local = datetime.now().strftime("%H:%M")
        for chat_id in storage.all_chats():
            # Daily digest to group
            desired = storage.get_digest_time(chat_id) or DEFAULT_DIGEST_TIME
            if desired == now_local:
                day_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
                since = int(day_start.timestamp())
                msgs = storage.get_messages(chat_id, since)
                if msgs:
                    digest = await summarize_window(client, OPENAI_MODEL, msgs, period_label="(kunlik)")
                    try:
                        await app.bot.send_message(chat_id=chat_id, text=digest, parse_mode=ParseMode.MARKDOWN)
                    except Exception as e:
                        log.warning("Send digest to %s failed: %s", chat_id, e)
                    if DM_ADMIN_DIGEST and storage.get_admin(chat_id):
                        await dm_admin(chat_id, f"[Daily Digest] Chat {chat_id}\n\n{digest}", app, parse_mode=ParseMode.MARKDOWN)

            # Inspiration
            insp_time, insp_thr = storage.get_inspire(chat_id)
            insp_time = insp_time or DEFAULT_INSPIRE_TIME
            insp_thr  = insp_thr or DEFAULT_INSPIRE_THRESHOLD
            if insp_time == now_local:
                day_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
                msg_count = storage.count_messages(chat_id, int(day_start.timestamp()))
                if msg_count >= insp_thr:
                    try:
                        msg = INSPIRATIONS[msg_count % len(INSPIRATIONS)]
                        await app.bot.send_message(chat_id=chat_id, text=msg)
                    except Exception as e:
                        log.warning("Send inspiration to %s failed: %s", chat_id, e)

    scheduler.add_job(minute_tick, CronTrigger(minute="*"))
    scheduler.start()

# =========================
# Error handler (quiet 409)
# =========================
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, Conflict):
        logging.warning("409 Conflict: another instance is polling. Stop other instances.")
        return
    logging.exception("Unhandled error", exc_info=context.error)

# =========================
# App wiring
# =========================
def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("set_admin", set_admin))
    app.add_handler(CommandHandler("set_inspire", set_inspire))

    app.add_handler(CommandHandler("chatid", chatid))
    app.add_handler(CommandHandler("search", search))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("digest_today", digest_today))
    app.add_handler(CommandHandler("digest_week", digest_week))
    app.add_handler(CommandHandler("digest_time", digest_time))
    app.add_handler(CommandHandler("keywords", show_keywords))
    app.add_handler(CommandHandler("set_keywords", set_keywords))
    app.add_handler(CommandHandler("hits_today", hits_today))
    app.add_handler(CommandHandler("export_hits", export_hits))

    # Media first, then text
    app.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE | filters.VOICE | filters.AUDIO, handle_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    app.add_error_handler(on_error)
    return app

def main():
    ensure_db(DB_PATH)
    app = build_app()
    setup_scheduler(app)
    logging.info("Starting polling…")
    app.run_polling(stop_signals=None, close_loop=False, drop_pending_updates=False)

if __name__ == "__main__":
    main()
