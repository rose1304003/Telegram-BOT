import os
import sqlite3
from typing import List, Tuple, Dict

DB_PATH_DEFAULT = "data/bot.db"

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL,
  message_id INTEGER NOT NULL,
  user_id INTEGER,
  username TEXT,
  text TEXT NOT NULL,
  date INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_date ON messages(chat_id, date);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_messages USING fts5(text, content='messages', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO fts_messages(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO fts_messages(fts_messages, rowid, text) VALUES('delete', old.id, old.text);
END;
CREATE TABLE IF NOT EXISTS chat_settings (
  chat_id INTEGER PRIMARY KEY,
  digest_time TEXT DEFAULT '21:00',
  keywords TEXT DEFAULT ''
);
"""

def ensure_db(path: str = DB_PATH_DEFAULT):
    """Создаёт базу данных и таблицы, если их нет."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with sqlite3.connect(path) as con:
        con.executescript(SCHEMA)

class Storage:
    def __init__(self, path: str = DB_PATH_DEFAULT):
        self.path = path
        ensure_db(self.path)

    def insert_message(self, chat_id: int, message_id: int, user_id: int, username: str, text: str, date: int):
        with sqlite3.connect(self.path) as con:
            con.execute("""
              INSERT INTO messages(chat_id, message_id, user_id, username, text, date)
              VALUES (?, ?, ?, ?, ?, ?)
            """, (chat_id, message_id, user_id, username, text, date))

    def get_messages(self, chat_id: int, since_ts: int) -> List[Dict]:
        with sqlite3.connect(self.path) as con:
            con.row_factory = sqlite3.Row
            cur = con.execute("""
                SELECT m.*
                FROM messages m
                WHERE m.chat_id=? AND m.date>=?
                ORDER BY m.date ASC
            """, (chat_id, since_ts))
            return [dict(r) for r in cur.fetchall()]

    def top_users(self, chat_id: int, since_ts: int, limit: int = 10) -> List[Dict]:
        with sqlite3.connect(self.path) as con:
            con.row_factory = sqlite3.Row
            cur = con.execute("""
                SELECT user_id, username, COUNT(*) as cnt
                FROM messages
                WHERE chat_id=? AND date>=?
                GROUP BY user_id, username
                ORDER BY cnt DESC
                LIMIT ?
            """, (chat_id, since_ts, limit))
            return [dict(r) for r in cur.fetchall()]

    def count_messages(self, chat_id: int, since_ts: int) -> int:
        with sqlite3.connect(self.path) as con:
            cur = con.execute("""
                SELECT COUNT(*) FROM messages WHERE chat_id=? AND date>=?
            """, (chat_id, since_ts))
            row = cur.fetchone()
            return row[0] if row else 0

    def search(self, chat_id: int, query: str, limit: int = 20) -> List[Dict]:
        with sqlite3.connect(self.path) as con:
            con.row_factory = sqlite3.Row
            cur = con.execute("""
                SELECT m.*
                FROM fts_messages f
                JOIN messages m ON m.id = f.rowid
                WHERE m.chat_id=? AND f.text MATCH ?
                ORDER BY m.date DESC
                LIMIT ?
            """, (chat_id, query, limit))
            return [dict(r) for r in cur.fetchall()]

    def set_digest_time(self, chat_id: int, time_str: str):
        with sqlite3.connect(self.path) as con:
            con.execute("""
                INSERT INTO chat_settings(chat_id, digest_time)
                VALUES (?,?)
                ON CONFLICT(chat_id) DO UPDATE SET digest_time=excluded.digest_time
            """, (chat_id, time_str))

    def get_digest_time(self, chat_id: int) -> str:
        with sqlite3.connect(self.path) as con:
            cur = con.execute("SELECT digest_time FROM chat_settings WHERE chat_id=?", (chat_id,))
            row = cur.fetchone()
            return row[0] if row else None

    def set_keywords(self, chat_id: int, kws: str):
        with sqlite3.connect(self.path) as con:
            con.execute("""
                INSERT INTO chat_settings(chat_id, keywords)
                VALUES (?,?)
                ON CONFLICT(chat_id) DO UPDATE SET keywords=excluded.keywords
            """, (chat_id, kws))

    def get_keywords(self, chat_id: int) -> str:
        with sqlite3.connect(self.path) as con:
            cur = con.execute("SELECT keywords FROM chat_settings WHERE chat_id=?", (chat_id,))
            row = cur.fetchone()
            if not row or not row[0]:
                return ""
            return row[0].strip()

    def all_chats(self) -> List[int]:
        """Возвращает все чаты, где бот работает (из сообщений и настроек)."""
        with sqlite3.connect(self.path) as con:
            cur = con.execute("""
                SELECT DISTINCT chat_id FROM messages
                UNION
                SELECT DISTINCT chat_id FROM chat_settings
            """)
            return [r[0] for r in cur.fetchall()]
