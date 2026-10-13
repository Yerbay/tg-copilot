"""Хранилище: пользователи бота, их белые списки, карточки, подсказки, сообщения WhatsApp и скриншотов.

У каждого пользователя свои контакты (user_id). Один контакт = один человек: у него может быть
чат в Telegram (chat_id), в WhatsApp (wa_jid), скриншоты — или всё сразу.
Историю Telegram каждый раз берём с серверов Telegram. Остальное храним сами в messages:
WhatsApp (он не отдаёт историю по запросу; только белый список) и распознанные скриншоты.
"""
import sqlite3
from datetime import datetime, timezone

import config

# Исходная схема (версия 0). Дальше её меняют миграции.
SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    name_key         TEXT NOT NULL UNIQUE,
    chat_id          INTEGER UNIQUE,
    username         TEXT,
    wa_jid           TEXT UNIQUE,
    facts            TEXT NOT NULL DEFAULT '',
    added_at         TEXT NOT NULL,
    last_reminder_at TEXT
);
CREATE TABLE IF NOT EXISTS suggestions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    incoming   TEXT NOT NULL,
    variants   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wa_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    wa_id      TEXT NOT NULL UNIQUE,
    ts         TEXT NOT NULL,
    out        INTEGER NOT NULL,
    text       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS wa_messages_contact_ts ON wa_messages (contact_id, ts);
"""

# Миграции по порядку: номер уже применённой хранится в PRAGMA user_version.
# {admin} и {now} подставляются при применении.
MIGRATIONS = [
    # 1: wa_messages -> messages с источником (WA — WhatsApp, SS — скриншот); кэш расшифровок голосовых
    """
    ALTER TABLE wa_messages RENAME TO messages;
    ALTER TABLE messages RENAME COLUMN wa_id TO ext_id;
    ALTER TABLE messages ADD COLUMN source TEXT NOT NULL DEFAULT 'WA';
    DROP INDEX IF EXISTS wa_messages_contact_ts;
    CREATE INDEX IF NOT EXISTS messages_contact_ts ON messages (contact_id, ts);
    CREATE TABLE IF NOT EXISTS transcripts (
        key  TEXT PRIMARY KEY,  -- tg:<user>:<chat>:<msg> | wa:<user>:<id>
        text TEXT NOT NULL
    );
    """,
    # 2: несколько пользователей. Контакты получают владельца; имена и чаты уникальны в пределах владельца.
    """
    CREATE TABLE users (
        id         INTEGER PRIMARY KEY,               -- Telegram ID пользователя бота
        name       TEXT NOT NULL,
        role       TEXT NOT NULL DEFAULT 'user',      -- admin | user
        status     TEXT NOT NULL DEFAULT 'active',    -- active | blocked
        partner    TEXT NOT NULL DEFAULT 'f',         -- f — переписывается с девушками, m — с парнями
        created_at TEXT NOT NULL
    );
    INSERT INTO users (id, name, role, created_at) VALUES ({admin}, 'Администратор', 'admin', '{now}');
    CREATE TABLE usage (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        day     TEXT NOT NULL,
        calls   INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, day)
    );
    CREATE TABLE contacts_new (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name             TEXT NOT NULL,
        name_key         TEXT NOT NULL,
        chat_id          INTEGER,
        username         TEXT,
        wa_jid           TEXT,
        facts            TEXT NOT NULL DEFAULT '',
        added_at         TEXT NOT NULL,
        last_reminder_at TEXT,
        UNIQUE (user_id, name_key),
        UNIQUE (user_id, chat_id),
        UNIQUE (user_id, wa_jid)
    );
    INSERT INTO contacts_new (id, user_id, name, name_key, chat_id, username, wa_jid, facts, added_at, last_reminder_at)
        SELECT id, {admin}, name, name_key, chat_id, username, wa_jid, facts, added_at, last_reminder_at FROM contacts;
    DROP TABLE contacts;
    ALTER TABLE contacts_new RENAME TO contacts;
    """,
    # 3: карточка самого пользователя и уровень грамотности предлагаемых сообщений
    """
    ALTER TABLE users ADD COLUMN about TEXT NOT NULL DEFAULT '';
    ALTER TABLE users ADD COLUMN literacy TEXT NOT NULL DEFAULT '95';  -- 100 | 95 | 85 | 70 | me
    """,
    # 4: режим помощника — знакомства или отношения с одним человеком
    """
    ALTER TABLE users ADD COLUMN mode TEXT NOT NULL DEFAULT 'dating';  -- dating | relationship
    """,
    # 5: режим для отдельного человека; NULL — как у пользователя по умолчанию
    """
    ALTER TABLE contacts ADD COLUMN mode TEXT;  -- dating | relationship | NULL
    """,
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _key(name: str) -> str:
    return " ".join(name.split()).casefold()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init() -> None:
    with _conn() as c:
        version = c.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            c.executescript(SCHEMA)
        # пересоздание таблиц в миграциях — с выключенными внешними ключами, иначе DROP удалит связанные строки
        c.execute("PRAGMA foreign_keys = OFF")
        for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
            script = script.replace("{admin}", str(int(config.ADMIN_ID))).replace("{now}", now_iso())
            c.executescript(f"BEGIN; {script} PRAGMA user_version = {number}; COMMIT;")
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("INSERT OR IGNORE INTO users (id, name, role, created_at) VALUES (?, 'Администратор', 'admin', ?)",
                  (config.ADMIN_ID, now_iso()))


# --- пользователи ---

def add_user(user_id: int, name: str) -> None:
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO users (id, name, created_at) VALUES (?, ?, ?)", (user_id, name, now_iso()))


def get_user(user_id: int) -> sqlite3.Row | None:
    with _conn() as c:
        return c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def list_users() -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute("SELECT * FROM users ORDER BY role, created_at").fetchall()


def set_user(user_id: int, **fields) -> None:
    """Обновить поля пользователя: name, status, partner, about, literacy, mode."""
    allowed = {k: v for k, v in fields.items() if k in ("name", "status", "partner", "about", "literacy", "mode")}
    if allowed:
        with _conn() as c:
            sets = ", ".join(f"{k} = ?" for k in allowed)
            c.execute(f"UPDATE users SET {sets} WHERE id = ?", (*allowed.values(), user_id))


def delete_user(user_id: int) -> None:
    """Все данные пользователя: контакты (с перепиской и подсказками), расход, кэш расшифровок."""
    with _conn() as c:
        c.execute("DELETE FROM contacts WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM transcripts WHERE key LIKE ? OR key LIKE ?", (f"tg:{user_id}:%", f"wa:{user_id}:%"))
        c.execute("DELETE FROM users WHERE id = ?", (user_id,))


def add_usage(user_id: int) -> int:
    """+1 запрос к ИИ за сегодня. Возвращает, сколько стало."""
    day = datetime.now(timezone.utc).date().isoformat()
    with _conn() as c:
        c.execute("INSERT INTO usage (user_id, day, calls) VALUES (?, ?, 1) "
                  "ON CONFLICT (user_id, day) DO UPDATE SET calls = calls + 1", (user_id, day))
        return c.execute("SELECT calls FROM usage WHERE user_id = ? AND day = ?", (user_id, day)).fetchone()[0]


def usage_today(user_id: int) -> int:
    day = datetime.now(timezone.utc).date().isoformat()
    with _conn() as c:
        row = c.execute("SELECT calls FROM usage WHERE user_id = ? AND day = ?", (user_id, day)).fetchone()
    return row[0] if row else 0


# --- контакты ---

def add_contact(user_id: int, name: str, chat_id: int | None = None, username: str | None = None,
                wa_jid: str | None = None) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO contacts (user_id, name, name_key, chat_id, username, wa_jid, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, name, _key(name), chat_id, username, wa_jid, now_iso()),
        )


def set_telegram(contact_id: int, chat_id: int, username: str | None) -> None:
    with _conn() as c:
        c.execute("UPDATE contacts SET chat_id = ?, username = ? WHERE id = ?", (chat_id, username, contact_id))


def set_whatsapp(contact_id: int, wa_jid: str) -> None:
    with _conn() as c:
        c.execute("UPDATE contacts SET wa_jid = ? WHERE id = ?", (wa_jid, contact_id))


def remove_contact(contact_id: int) -> bool:
    with _conn() as c:
        return c.execute("DELETE FROM contacts WHERE id = ?", (contact_id,)).rowcount > 0


def get_by_id(contact_id: int) -> sqlite3.Row | None:
    with _conn() as c:
        return c.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()


def get_by_name(user_id: int, name: str) -> sqlite3.Row | None:
    with _conn() as c:
        return c.execute("SELECT * FROM contacts WHERE user_id = ? AND name_key = ?",
                         (user_id, _key(name))).fetchone()


def get_by_chat(user_id: int, chat_id: int) -> sqlite3.Row | None:
    with _conn() as c:
        return c.execute("SELECT * FROM contacts WHERE user_id = ? AND chat_id = ?", (user_id, chat_id)).fetchone()


def get_by_wa(user_id: int, wa_jid: str) -> sqlite3.Row | None:
    with _conn() as c:
        return c.execute("SELECT * FROM contacts WHERE user_id = ? AND wa_jid = ?", (user_id, wa_jid)).fetchone()


def list_contacts(user_id: int) -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute("SELECT * FROM contacts WHERE user_id = ? ORDER BY name_key", (user_id,)).fetchall()


def set_contact_mode(contact_id: int, mode: str | None) -> None:
    with _conn() as c:
        c.execute("UPDATE contacts SET mode = ? WHERE id = ?", (mode, contact_id))


def update_facts(contact_id: int, facts: str) -> None:
    with _conn() as c:
        c.execute("UPDATE contacts SET facts = ? WHERE id = ?", (facts, contact_id))


def set_reminded(contact_id: int) -> None:
    with _conn() as c:
        c.execute("UPDATE contacts SET last_reminder_at = ? WHERE id = ?", (now_iso(), contact_id))


# --- сообщения, которые храним сами (WhatsApp, скриншоты) ---

def save_message(contact_id: int, source: str, ext_id: str, ts: str, out: bool, text: str) -> bool:
    """False — такое сообщение уже есть (повтор)."""
    with _conn() as c:
        return c.execute(
            "INSERT OR IGNORE INTO messages (contact_id, source, ext_id, ts, out, text) VALUES (?, ?, ?, ?, ?, ?)",
            (contact_id, source, ext_id, ts, int(out), text),
        ).rowcount > 0


def set_message_text(ext_id: str, text: str) -> None:
    with _conn() as c:
        c.execute("UPDATE messages SET text = ? WHERE ext_id = ?", (text, ext_id))


def messages(contact_id: int, limit: int) -> list[sqlite3.Row]:
    """Последние limit сообщений, старые сверху."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM messages WHERE contact_id = ? ORDER BY ts DESC, id DESC LIMIT ?",
            (contact_id, limit),
        ).fetchall()
    return list(reversed(rows))


def has_messages(contact_id: int, source: str) -> bool:
    with _conn() as c:
        return c.execute("SELECT 1 FROM messages WHERE contact_id = ? AND source = ? LIMIT 1",
                         (contact_id, source)).fetchone() is not None


# --- кэш расшифровок голосовых ---

def get_transcript(key: str) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT text FROM transcripts WHERE key = ?", (key,)).fetchone()
    return row["text"] if row else None


def save_transcript(key: str, text: str) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO transcripts (key, text) VALUES (?, ?)", (key, text))


# --- подсказки ---

def save_suggestion(contact_id: int, incoming: str, variants: str) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO suggestions (contact_id, created_at, incoming, variants) VALUES (?, ?, ?, ?)",
            (contact_id, now_iso(), incoming, variants),
        )
        return cur.lastrowid


def get_suggestion(suggestion_id: int) -> sqlite3.Row | None:
    with _conn() as c:
        return c.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()


def suggestions_since(contact_id: int, since_iso: str) -> list[sqlite3.Row]:
    with _conn() as c:
        return c.execute(
            "SELECT * FROM suggestions WHERE contact_id = ? AND created_at >= ? ORDER BY created_at",
            (contact_id, since_iso),
        ).fetchall()
