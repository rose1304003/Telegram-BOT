A Telegram bot that collects group messages, stores them in SQLite, and can:
- send **daily/weekly digests** summarized by OpenAI,
- **track keywords**,
- **search** conversation history,
- show simple **stats** per user.

> Works in groups & supergroups. Make sure **Group Privacy** is **disabled** for your bot in @BotFather.

## Quick start

1. **Create a bot** via [@BotFather](https://t.me/BotFather) → get `TELEGRAM_BOT_TOKEN`.
2. **Create an OpenAI API key** from https://platform.openai.com → get `OPENAI_API_KEY`.
3. **Set env vars** (copy `.env.example` → `.env` and fill in values).
4. Install deps & run:
```bash
python -m venv .venv && . .venv/bin/activate  # (Windows: .venv\Scripts\activate)
pip install -r requirements.txt
python bot.py
```
5. Add the bot to your **group/supergroup**. In @BotFather → **Group privacy = OFF**.
6. (Optional) Make the bot **admin** if you want it to pin/send digests without restrictions.

## Commands (in group)

- `/start` – health check.
- `/search <query>` – find recent messages containing `<query>`.
- `/stats` – top talkers in the last 7 days.
- `/digest_today` – generate a digest for the last 24h and send.
- `/digest_week` – generate a digest for the last 7 days and send.
- `/digest_time 21:30` – set daily digest time (local TZ) for this chat.
- `/keywords` – show current tracked keywords.
- `/set_keywords buy now, финграмотность, deadline` – update tracked keywords (comma-separated).

> The bot **stores only text** by default. You can extend `storage.py` to keep media/captions if needed.

## Scheduling

The bot ships with APScheduler. It keeps a per-chat schedule stored in SQLite and sends a daily digest at the saved local time (default `21:00`). Timezone comes from `LOCAL_TZ` in `.env` (default `Asia/Tashkent`).

## Deploy

- **Railway / Render / Fly.io / Docker**: use `Dockerfile` and set env vars in the dashboard.
- **Heroku**: use `Procfile` and set config vars.

## Env Vars

Copy `.env.example` → `.env`:

- `TELEGRAM_BOT_TOKEN` – token from @BotFather
- `OPENAI_API_KEY` – OpenAI API key
- `OPENAI_MODEL` – e.g. `gpt-4o-mini` (default)
- `LOCAL_TZ` – e.g. `Asia/Tashkent` (default)
- `DB_PATH` – path to SQLite DB (default `data/bot.db`)
- `DEFAULT_DIGEST_TIME` – default HH:MM for daily digest (default `21:00`)
- `TRACKED_KEYWORDS` – comma-separated list for alerts (optional)
- `ALLOWED_CHAT_IDS` – optional comma-separated numeric IDs. If set, bot ignores other chats.

## Notes

- To **see all messages** in groups you must **disable Group Privacy** for your bot. In Telegram terms: bots with privacy disabled (or promoted to admin) receive all messages except messages from other bots.
- The OpenAI model is configurable via `OPENAI_MODEL`. You can change prompts in `summarizer.py`.

## License
MIT
