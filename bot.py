#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Anonymous Message Bot  -  v6.0

Highlights
  * HTML parse mode with proper escaping (user text can no longer break delivery)
  * Every media type is delivered with copy_message (no "forwarded from", no re-upload)
  * Conversation state, bans and reports are persisted in SQLite (survive restarts)
  * Random link tokens instead of raw Telegram IDs (old numeric links still work
    until the owner regenerates their link)
  * Stable "Anonymous #N" labels per sender, block list with unblock
  * Reply / block / report buttons, all authorised on the server side
  * Pause receiving, regenerate link, delete my data
  * Admin: stats, broadcast (text or copy of any message, confirmed, throttled),
    ban/unban, user info, CSV export, report review
  * Admins still see the sender's identity on messages addressed to them
  * Rate limiting, message-row retention, unreachable-user tracking
"""
import csv
import functools
import html
import io
import json
import logging
import os
import secrets
import sqlite3
import sys
import threading
import time
import unicodedata
import urllib.parse
from collections import defaultdict, deque
from contextlib import contextmanager

from dotenv import load_dotenv
from telebot import TeleBot, types
from telebot.apihelper import ApiTelegramException

load_dotenv()

# ════════════════════════════════════════════════════════════════════════════
# Configuration
# ════════════════════════════════════════════════════════════════════════════


def env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def env_bool(name, default):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    sys.exit("BOT_TOKEN is not set (see .env.example)")

BOT_USERNAME = (os.getenv("BOT_USERNAME") or "").lstrip("@")
ADMINS = {int(x) for x in os.getenv("ADMINS", "").split(",") if x.strip().isdigit()}
DB_PATH = os.getenv("DB_PATH", "anonymous_bot.db")

RATE_MAX = env_int("RATE_MAX_MESSAGES", 5)            # messages ...
RATE_WINDOW = env_int("RATE_WINDOW_SECONDS", 30)      # ... per this many seconds
STATE_TTL = env_int("STATE_TTL_MINUTES", 30) * 60
RETENTION_DAYS = env_int("MESSAGE_RETENTION_DAYS", 60)  # 0 = keep forever
BROADCAST_DELAY = 0.05                                 # ~20 msgs/sec, under Telegram's limit

# The original rule: admins see who sent them a message. Keep it True.
ADMIN_SEES_SENDER = env_bool("ADMIN_SEES_SENDER", True)
# Tell people up front when the receiver is an admin who can see their identity.
ADMIN_DISCLOSURE = env_bool("ADMIN_DISCLOSURE", True)
# Old links looked like ?start=<telegram_id>. Keep them working until regenerated.
LEGACY_NUMERIC_LINKS = env_bool("LEGACY_NUMERIC_LINKS", True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
)
logging.getLogger("TeleBot").setLevel(logging.WARNING)
logger = logging.getLogger("anon_bot")

bot = TeleBot(TOKEN, parse_mode="HTML", num_threads=8)

# ════════════════════════════════════════════════════════════════════════════
# Texts (Persian). Everything user-facing lives here for easy editing.
# NOTE: parse mode is HTML, so never put raw < > & in these strings.
# ════════════════════════════════════════════════════════════════════════════

BTN_LINK = "🔗 دریافت لینک من"
BTN_SETTINGS = "⚙️ تنظیمات"
BTN_STATS = "📊 آمار من"
BTN_HELP = "❓ راهنما"
BTN_ABOUT = "ℹ️ درباره ربات"
BTN_ADMIN = "👑 پنل مدیریت"
BTN_CANCEL = "❌ لغو"


class T:
    BANNED = "⛔️ دسترسی شما به ربات محدود شده است."
    ERROR = "⚠️ خطایی رخ داد. لطفاً دوباره تلاش کنید."
    MENU_HINT = "لطفاً از منوی زیر یک گزینه را انتخاب کنید."
    UNKNOWN_CMD = "❓ دستور ناشناخته است. از منوی زیر استفاده کنید."
    CANCELLED = "عملیات لغو شد 🔙"
    SELF = "❌ شما نمی‌توانید به خودتان پیام ناشناس بدهید."
    NOT_FOUND = "❌ کاربر یافت نشد یا این لینک دیگر معتبر نیست."
    UNAVAILABLE = "❌ در حال حاضر امکان ارسال پیام به این کاربر وجود ندارد."
    UNREACHABLE = "❌ گیرنده ربات را متوقف کرده است و امکان ارسال پیام وجود ندارد."
    SEND_FAILED = "❌ ارسال پیام ناموفق بود. (ممکن است گیرنده دریافت این نوع پیام را محدود کرده باشد.)"
    UNSUPPORTED = "❌ این نوع پیام پشتیبانی نمی‌شود. متن، عکس، ویدیو، ویس، فایل، موسیقی، گیف یا استیکر بفرستید."
    TOO_LONG = "❌ متن یا کپشن بیش از حد طولانی است. لطفاً کوتاه‌ترش کنید."
    SLOW_DOWN = "⚠️ لطفاً آهسته‌تر پیام بفرستید. {sec} ثانیه دیگر دوباره تلاش کنید."
    SENT = "✅ ارسال شد. می‌توانید پیام دیگری بنویسید یا «❌ لغو» را بزنید."
    SENT_REPLY = "✅ پاسخ شما ارسال شد. می‌توانید ادامه دهید یا «❌ لغو» را بزنید."
    WELCOME = (
        "👋 سلام {name} عزیز!\n\n"
        "به ربات پیام ناشناس خوش آمدید. لینک اختصاصی خود را بگیرید و در شبکه‌های "
        "اجتماعی بگذارید تا دیگران بتوانند ناشناس برایتان پیام بفرستند."
    )
    COMPOSING = (
        "📤 در حال نوشتن پیام ناشناس به: 👤 <b>{name}</b>\n\n"
        "💬 متن، عکس، ویدیو، ویس، فایل یا استیکر خود را بفرستید:"
    )
    ADMIN_NOTICE = (
        "\n\n⚠️ <i>توجه: گیرنده این پیام مدیر ربات است و هویت فرستنده برای او قابل مشاهده است.</i>"
    )
    COMPOSING_REPLY = "✍️ در حال نوشتن پاسخ هستید. پیام خود را بفرستید:"
    ASK_NAME = (
        "📝 نام نمایشی جدید خود را ارسال کنید:\n"
        "(این نام به افرادی که روی لینک شما کلیک می‌کنند نشان داده می‌شود)"
    )
    TEXT_ONLY = "❌ لطفاً فقط متن ارسال کنید."
    NAME_INVALID = "❌ نام معتبر نیست. یک نام متنی (حداکثر ۴۰ کاراکتر) بفرستید."
    NAME_SAVED = "✅ نام نمایشی شما به «{name}» تغییر یافت."
    BLOCKED = "✅ فرستنده بلاک شد. از «تنظیمات › لیست بلاک‌ها» می‌توانید رفع بلاک کنید."
    EXPIRED = "❌ این پیام منقضی شده است."
    FORBIDDEN = "⛔️ شما اجازه این کار را ندارید."
    REPORTED = "🚩 گزارش شما برای مدیران ارسال شد."
    ALREADY_REPORTED = "این پیام قبلاً گزارش شده است."
    TOGGLED_ON = "✅ دریافت پیام فعال شد."
    TOGGLED_OFF = "⏸ دریافت پیام متوقف شد."
    UNBLOCKED = "✅ رفع بلاک شد."
    NO_BLOCKS = "هیچ کاربری را بلاک نکرده‌اید."
    HELP = (
        "📖 <b>راهنما</b>\n\n"
        "🔗 لینک اختصاصی خود را از «دریافت لینک من» بگیرید و در بیو یا کانال خود بگذارید.\n"
        "📨 هر کسی که روی لینک بزند می‌تواند متن، عکس، ویدیو، ویس، فایل یا استیکر ناشناس برایتان بفرستد.\n"
        "↩️ می‌توانید بدون فهمیدن هویت فرستنده به او پاسخ دهید.\n"
        "🚫 مزاحم را بلاک کنید؛ در «تنظیمات › لیست بلاک‌ها» هر وقت خواستید رفع بلاک کنید.\n"
        "🚩 پیام‌های توهین‌آمیز را گزارش کنید تا مدیران بررسی کنند.\n"
        "⏸ از تنظیمات می‌توانید دریافت پیام را موقتاً متوقف یا لینک جدید بسازید.\n\n"
        "⚠️ توجه: مدیران ربات هنگام دریافت پیام یا بررسی گزارش تخلف، اطلاعات فرستنده را می‌بینند."
    )
    ABOUT = "🤖 <b>ربات پیام ناشناس</b>\nتوسعه‌یافته با Python و Telebot\nنسخه: 6.0"
    BANNED_NOTICE = "⛔️ دسترسی شما به ربات به دلیل نقض قوانین محدود شد."
    UNBANNED_NOTICE = "✅ محدودیت دسترسی شما برداشته شد."


# ════════════════════════════════════════════════════════════════════════════
# Database
# ════════════════════════════════════════════════════════════════════════════

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    display_name TEXT,
    join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS messages (
    msg_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id INTEGER,
    receiver_id INTEGER,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS blocks (
    blocker_id INTEGER,
    blocked_id INTEGER,
    PRIMARY KEY (blocker_id, blocked_id)
);
CREATE TABLE IF NOT EXISTS aliases (
    receiver_id INTEGER NOT NULL,
    sender_id INTEGER NOT NULL,
    number INTEGER NOT NULL,
    PRIMARY KEY (receiver_id, sender_id)
);
CREATE TABLE IF NOT EXISTS bans (
    user_id INTEGER PRIMARY KEY,
    reason TEXT,
    banned_by INTEGER,
    banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS reports (
    report_id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id INTEGER UNIQUE,
    reporter_id INTEGER,
    sender_id INTEGER,
    status TEXT DEFAULT 'pending',
    handled_by INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS states (
    user_id INTEGER PRIMARY KEY,
    state TEXT,
    data TEXT,
    updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_messages_receiver ON messages(receiver_id);
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_id);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(timestamp);
CREATE UNIQUE INDEX IF NOT EXISTS idx_aliases_number ON aliases(receiver_id, number);
"""


def new_token():
    # Starts with a letter so it can never be mistaken for a legacy numeric ID.
    return "u" + secrets.token_urlsafe(9)


class Database:
    def __init__(self, path):
        self.path = path
        folder = os.path.dirname(os.path.abspath(path))
        os.makedirs(folder, exist_ok=True)

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def run(self, sql, params=()):
        with self.conn() as c:
            return c.execute(sql, params).rowcount

    def insert(self, sql, params=()):
        with self.conn() as c:
            return c.execute(sql, params).lastrowid

    def one(self, sql, params=()):
        with self.conn() as c:
            return c.execute(sql, params).fetchone()

    def all(self, sql, params=()):
        with self.conn() as c:
            return c.execute(sql, params).fetchall()

    def val(self, sql, params=(), default=0):
        row = self.one(sql, params)
        return row[0] if row is not None and row[0] is not None else default

    def init(self):
        with self.conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
            # Migrations for databases created by the old version
            self._add_column(c, "users", "link_token", "TEXT")
            self._add_column(c, "users", "accepting", "INTEGER DEFAULT 1")
            self._add_column(c, "users", "is_active", "INTEGER DEFAULT 1")
            self._add_column(c, "users", "legacy_ok", "INTEGER DEFAULT 1")
            self._add_column(c, "users", "last_seen", "TIMESTAMP")
            self._add_column(c, "messages", "is_reply", "INTEGER DEFAULT 0")
            self._add_column(c, "messages", "content_type", "TEXT")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_token ON users(link_token)")
            missing = c.execute("SELECT user_id FROM users WHERE link_token IS NULL").fetchall()
            for r in missing:
                c.execute("UPDATE users SET link_token=? WHERE user_id=?", (new_token(), r["user_id"]))

    @staticmethod
    def _add_column(c, table, column, ddl):
        cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


db = Database(DB_PATH)

# ─── users ──────────────────────────────────────────────────────────────────


def esc(value):
    return html.escape(str(value if value is not None else ""))


def clean_name(text):
    text = " ".join((text or "").split())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cc")  # keeps ZWNJ
    return text[:40].strip()


def touch_user(tg_user):
    first = tg_user.first_name or "کاربر"
    exists = db.one("SELECT 1 FROM users WHERE user_id=?", (tg_user.id,))
    if exists:
        db.run(
            "UPDATE users SET username=?, first_name=?, is_active=1, last_seen=CURRENT_TIMESTAMP "
            "WHERE user_id=?",
            (tg_user.username, first, tg_user.id),
        )
    else:
        db.run(
            "INSERT INTO users (user_id, username, first_name, display_name, link_token, last_seen) "
            "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (tg_user.id, tg_user.username, first, first, new_token()),
        )


def get_user(user_id):
    return db.one("SELECT * FROM users WHERE user_id=?", (user_id,))


def find_user_by_payload(payload):
    if payload.startswith("u"):
        return db.one("SELECT * FROM users WHERE link_token=?", (payload,))
    if LEGACY_NUMERIC_LINKS and payload.isdigit():
        return db.one("SELECT * FROM users WHERE user_id=? AND legacy_ok=1", (int(payload),))
    return None


def display_name_of(user_row):
    return (user_row["display_name"] or user_row["first_name"] or "کاربر") if user_row else "کاربر"


def user_line(u):
    uname = f"@{esc(u['username'])}" if u["username"] else "بدون یوزرنیم"
    return (
        f'<a href="tg://user?id={u["user_id"]}">{esc(u["first_name"] or "—")}</a>'
        f" | {uname} | <code>{u['user_id']}</code>"
    )


def sender_card(tg_user):
    """What admins see when a message is addressed to them."""
    name = " ".join(filter(None, [tg_user.first_name, tg_user.last_name])) or "—"
    uname = f"@{esc(tg_user.username)}" if tg_user.username else "ندارد"
    return (
        "👁 <b>اطلاعات ویژه ادمین</b>\n"
        f'نام: <a href="tg://user?id={tg_user.id}">{esc(name)}</a>\n'
        f"یوزرنیم: {uname}\n"
        f"آی‌دی: <code>{tg_user.id}</code>"
    )


# ─── bans / blocks / aliases / messages ────────────────────────────────────

alias_lock = threading.Lock()


def is_banned(user_id):
    return db.one("SELECT 1 FROM bans WHERE user_id=?", (user_id,)) is not None


def ban_user(user_id, reason, by):
    db.run(
        "INSERT OR REPLACE INTO bans (user_id, reason, banned_by) VALUES (?, ?, ?)",
        (user_id, reason, by),
    )
    clear_state(user_id)


def is_blocked(blocker_id, user_id):
    return db.one(
        "SELECT 1 FROM blocks WHERE blocker_id=? AND blocked_id=?", (blocker_id, user_id)
    ) is not None


def block_user(blocker_id, blocked_id):
    db.run("INSERT OR IGNORE INTO blocks (blocker_id, blocked_id) VALUES (?, ?)", (blocker_id, blocked_id))


def get_alias(receiver_id, sender_id):
    """Stable 'Anonymous #N' number of a sender inside one receiver's inbox."""
    row = db.one(
        "SELECT number FROM aliases WHERE receiver_id=? AND sender_id=?", (receiver_id, sender_id)
    )
    if row:
        return row["number"]
    with alias_lock:
        row = db.one(
            "SELECT number FROM aliases WHERE receiver_id=? AND sender_id=?", (receiver_id, sender_id)
        )
        if row:
            return row["number"]
        number = db.val("SELECT MAX(number) FROM aliases WHERE receiver_id=?", (receiver_id,), 0) + 1
        db.run(
            "INSERT INTO aliases (receiver_id, sender_id, number) VALUES (?, ?, ?)",
            (receiver_id, sender_id, number),
        )
        return number


def save_message(sender_id, receiver_id, is_reply, content_type):
    return db.insert(
        "INSERT INTO messages (sender_id, receiver_id, is_reply, content_type) VALUES (?, ?, ?, ?)",
        (sender_id, receiver_id, int(is_reply), content_type),
    )


def get_message(msg_id):
    return db.one("SELECT * FROM messages WHERE msg_id=?", (msg_id,))


def delete_user_data(user_id):
    """
    Removes the user's profile, inbox metadata and settings. Rows that other people
    rely on for safety (blocks against this user, messages they sent, bans, reports)
    are kept; message rows are purged automatically after MESSAGE_RETENTION_DAYS.
    """
    with db.conn() as c:
        c.execute("DELETE FROM users WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM states WHERE user_id=?", (user_id,))
        c.execute("DELETE FROM blocks WHERE blocker_id=?", (user_id,))
        c.execute("DELETE FROM aliases WHERE receiver_id=?", (user_id,))
        c.execute("DELETE FROM messages WHERE receiver_id=?", (user_id,))


# ─── persisted conversation state ──────────────────────────────────────────


def set_state(user_id, state, data=None):
    db.run(
        "INSERT OR REPLACE INTO states (user_id, state, data, updated_at) VALUES (?, ?, ?, ?)",
        (user_id, state, json.dumps(data or {}), time.time()),
    )


def get_state(user_id):
    row = db.one("SELECT state, data, updated_at FROM states WHERE user_id=?", (user_id,))
    if not row:
        return None
    if time.time() - (row["updated_at"] or 0) > STATE_TTL:
        clear_state(user_id)
        return None
    try:
        data = json.loads(row["data"] or "{}")
    except ValueError:
        data = {}
    return {"state": row["state"], "data": data}


def clear_state(user_id):
    db.run("DELETE FROM states WHERE user_id=?", (user_id,))


# ─── rate limiting ──────────────────────────────────────────────────────────


class RateLimiter:
    def __init__(self, limit, window):
        self.limit, self.window = limit, window
        self.hits = defaultdict(deque)
        self.lock = threading.Lock()

    def check(self, key):
        """Returns 0 if allowed (and records the hit), else seconds to wait."""
        now = time.time()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] >= self.window:
                q.popleft()
            if len(q) >= self.limit:
                return max(1, int(self.window - (now - q[0])))
            q.append(now)
            return 0

    def cleanup(self):
        now = time.time()
        with self.lock:
            for key in [k for k, q in self.hits.items() if not q or now - q[-1] >= self.window]:
                del self.hits[key]


limiter = RateLimiter(RATE_MAX, RATE_WINDOW)

# ════════════════════════════════════════════════════════════════════════════
# Keyboards
# ════════════════════════════════════════════════════════════════════════════


def kb_main(user_id):
    m = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    m.add(BTN_LINK, BTN_SETTINGS)
    m.add(BTN_STATS, BTN_HELP)
    m.add(BTN_ABOUT)
    if user_id in ADMINS:
        m.add(BTN_ADMIN)
    return m


def kb_cancel():
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.add(BTN_CANCEL)
    return m


def actions_markup(msg_id, can_reply=True, can_block=True, can_report=True):
    m = types.InlineKeyboardMarkup()
    row = []
    if can_reply:
        row.append(types.InlineKeyboardButton("↩️ پاسخ دادن", callback_data=f"reply:{msg_id}"))
    if can_block:
        row.append(types.InlineKeyboardButton("🚫 بلاک", callback_data=f"block:{msg_id}"))
    if row:
        m.row(*row)
    if can_report:
        m.row(types.InlineKeyboardButton("🚩 گزارش تخلف", callback_data=f"report:{msg_id}"))
    return m if (row or can_report) else None


def kb_settings(u):
    m = types.InlineKeyboardMarkup(row_width=1)
    m.add(
        types.InlineKeyboardButton("✏️ تغییر نام نمایشی", callback_data="set:name"),
        types.InlineKeyboardButton(
            "⏸ توقف دریافت پیام" if u["accepting"] else "▶️ ادامه دریافت پیام",
            callback_data="set:toggle",
        ),
        types.InlineKeyboardButton("🔄 ساخت لینک جدید", callback_data="set:relink"),
        types.InlineKeyboardButton("🚫 لیست بلاک‌ها", callback_data="set:blocked"),
        types.InlineKeyboardButton("🗑 حذف اطلاعات من", callback_data="set:delete"),
    )
    return m


def kb_confirm(yes_data, label):
    m = types.InlineKeyboardMarkup(row_width=1)
    m.add(
        types.InlineKeyboardButton(label, callback_data=yes_data),
        types.InlineKeyboardButton("🔙 انصراف", callback_data="set:back"),
    )
    return m


def kb_admin_panel():
    m = types.InlineKeyboardMarkup(row_width=2)
    m.add(
        types.InlineKeyboardButton("🔄 بروزرسانی", callback_data="adm:stats"),
        types.InlineKeyboardButton("📄 خروجی کاربران", callback_data="adm:export"),
    )
    return m


# ════════════════════════════════════════════════════════════════════════════
# Decorators / helpers
# ════════════════════════════════════════════════════════════════════════════

ALL_TYPES = [
    "text", "photo", "voice", "video_note", "sticker", "video", "document", "audio",
    "animation", "location", "contact", "venue", "poll", "dice",
]
SENDABLE = {"text", "photo", "voice", "video_note", "sticker", "video", "document", "audio", "animation"}
CAPTION_TYPES = {"photo", "video", "animation", "audio", "document", "voice"}
MEDIA_LABELS = {"sticker": "😊 استیکر", "video_note": "🎬 ویدیو مسیج"}


def private(**kwargs):
    return bot.message_handler(chat_types=["private"], **kwargs)


def cb(prefix):
    prefix = prefix + ":"
    return lambda c: bool(c.data) and c.data.startswith(prefix)


def guarded(fn):
    """Registers the user, enforces bans and turns exceptions into a friendly reply."""

    @functools.wraps(fn)
    def wrapper(obj, *args, **kwargs):
        is_cb = isinstance(obj, types.CallbackQuery)
        user = obj.from_user
        try:
            touch_user(user)
            if user.id not in ADMINS and is_banned(user.id):
                if is_cb:
                    bot.answer_callback_query(obj.id, T.BANNED, show_alert=True)
                else:
                    bot.send_message(user.id, T.BANNED)
                return
            return fn(obj, *args, **kwargs)
        except Exception:
            logger.exception("Unhandled error in %s", fn.__name__)
            try:
                if is_cb:
                    bot.answer_callback_query(obj.id, T.ERROR, show_alert=True)
                else:
                    bot.send_message(user.id, T.ERROR)
            except Exception:
                pass

    return wrapper


def admin_only(fn):
    @functools.wraps(fn)
    def wrapper(obj, *args, **kwargs):
        if obj.from_user.id not in ADMINS:
            if isinstance(obj, types.CallbackQuery):
                bot.answer_callback_query(obj.id, T.FORBIDDEN, show_alert=True)
            return
        return fn(obj, *args, **kwargs)

    return wrapper


def is_unreachable(exc):
    code = getattr(exc, "error_code", None)
    return code == 403 or (code == 400 and "chat not found" in str(exc).lower())


def mark_inactive(user_id):
    db.run("UPDATE users SET is_active=0 WHERE user_id=?", (user_id,))


def safe_edit(text, call, markup=None):
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup)
    except ApiTelegramException as e:
        if "not modified" not in str(e).lower():
            raise


def safe_edit_markup(call, markup):
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=markup)
    except ApiTelegramException as e:
        if "not modified" not in str(e).lower():
            logger.warning("edit markup failed: %s", e)


# ════════════════════════════════════════════════════════════════════════════
# Delivery of anonymous messages
# ════════════════════════════════════════════════════════════════════════════


class TooLongError(Exception):
    pass


def build_header(is_reply, alias_no, sender, receiver_id):
    if is_reply:
        head = "📩 <b>پاسخ جدید از طرف گیرنده پیام شما:</b>"
    else:
        head = f"📨 <b>پیام ناشناس جدید</b> (ناشناس #{alias_no})"
    # Rule kept from the original bot: admins can see who is writing to them.
    if ADMIN_SEES_SENDER and receiver_id in ADMINS:
        head += "\n\n" + sender_card(sender)
    return head


def deliver(message, target_id, header, markup):
    ct = message.content_type
    if ct == "text":
        body = f"{header}\n\n{html.escape(message.text)}"
        if len(body) > 4096:
            raise TooLongError
        bot.send_message(target_id, body, parse_mode="HTML", reply_markup=markup)
    elif ct in CAPTION_TYPES:
        caption = header
        if message.caption:
            caption += "\n\n" + html.escape(message.caption)
        if len(caption) > 1024:
            raise TooLongError
        bot.copy_message(
            target_id, message.chat.id, message.message_id,
            caption=caption, parse_mode="HTML", reply_markup=markup,
        )
    else:  # stickers / video notes cannot carry a caption: header first, then the content
        label = MEDIA_LABELS.get(ct, "")
        bot.send_message(target_id, header + (f"\n\n{label}" if label else ""), parse_mode="HTML")
        bot.copy_message(target_id, message.chat.id, message.message_id, reply_markup=markup)


def process_outgoing(message, state):
    uid = message.from_user.id
    is_reply = state["state"] == "sending_reply"
    target_id = state["data"].get("target")

    if message.content_type not in SENDABLE:
        bot.send_message(uid, T.UNSUPPORTED)
        return

    wait = limiter.check(uid)
    if wait:
        bot.send_message(uid, T.SLOW_DOWN.format(sec=wait))
        return

    target = get_user(target_id)
    # One generic message for blocked / paused / banned / gone, so nothing is leaked.
    if (
        not target
        or not target["is_active"]
        or is_banned(target_id)
        or is_blocked(target_id, uid)
        or (not is_reply and not target["accepting"])
    ):
        clear_state(uid)
        bot.send_message(uid, T.UNAVAILABLE, reply_markup=kb_main(uid))
        return

    msg_id = save_message(uid, target_id, is_reply, message.content_type)
    alias_no = get_alias(target_id, uid)
    header = build_header(is_reply, alias_no, message.from_user, target_id)

    try:
        deliver(message, target_id, header, actions_markup(msg_id))
    except TooLongError:
        db.run("DELETE FROM messages WHERE msg_id=?", (msg_id,))
        bot.send_message(uid, T.TOO_LONG)
        return
    except ApiTelegramException as e:
        db.run("DELETE FROM messages WHERE msg_id=?", (msg_id,))
        if is_unreachable(e):
            mark_inactive(target_id)
            clear_state(uid)
            bot.send_message(uid, T.UNREACHABLE, reply_markup=kb_main(uid))
        else:
            logger.error("Delivery to %s failed: %s", target_id, e)
            bot.send_message(uid, T.SEND_FAILED)
        return

    set_state(uid, state["state"], state["data"])  # keep the session alive (refresh TTL)
    bot.send_message(uid, T.SENT_REPLY if is_reply else T.SENT)


# ════════════════════════════════════════════════════════════════════════════
# User commands & menu
# ════════════════════════════════════════════════════════════════════════════


def open_anonymous_session(message, payload):
    uid = message.from_user.id
    target = find_user_by_payload(payload)
    if not target:
        bot.send_message(uid, T.NOT_FOUND, reply_markup=kb_main(uid))
        return
    target_id = target["user_id"]
    if target_id == uid:
        bot.send_message(uid, T.SELF, reply_markup=kb_main(uid))
        return
    if (
        not target["accepting"]
        or not target["is_active"]
        or is_banned(target_id)
        or is_blocked(target_id, uid)
    ):
        bot.send_message(uid, T.UNAVAILABLE, reply_markup=kb_main(uid))
        return

    set_state(uid, "sending_anonymous", {"target": target_id})
    text = T.COMPOSING.format(name=esc(display_name_of(target)))
    if ADMIN_DISCLOSURE and ADMIN_SEES_SENDER and target_id in ADMINS:
        text += T.ADMIN_NOTICE
    bot.send_message(uid, text, reply_markup=kb_cancel())


@private(commands=["start"])
@guarded
def cmd_start(message):
    uid = message.from_user.id
    clear_state(uid)
    parts = message.text.split(maxsplit=1)
    if len(parts) == 2:
        open_anonymous_session(message, parts[1].strip())
        return
    bot.send_message(
        uid, T.WELCOME.format(name=esc(display_name_of(get_user(uid)))), reply_markup=kb_main(uid)
    )


@private(commands=["cancel"])
@private(func=lambda m: m.text == BTN_CANCEL)
@guarded
def cmd_cancel(message):
    uid = message.from_user.id
    clear_state(uid)
    bot.send_message(uid, T.CANCELLED, reply_markup=kb_main(uid))


def send_link(uid):
    u = get_user(uid)
    link = f"https://t.me/{BOT_USERNAME}?start={u['link_token']}"
    share = "https://t.me/share/url?" + urllib.parse.urlencode(
        {"url": link, "text": "برای من پیام ناشناس بفرست!"}, quote_via=urllib.parse.quote
    )
    text = (
        "🔗 <b>لینک ناشناس اختصاصی شما</b>\n\n"
        f"نام نمایشی فعلی: <b>{esc(display_name_of(u))}</b>\n"
        f"<code>{link}</code>\n\n"
        "این لینک را در بیو اینستاگرام یا کانال خود قرار دهید."
    )
    if not u["accepting"]:
        text += "\n\n⏸ دریافت پیام فعلاً متوقف است (از تنظیمات می‌توانید فعالش کنید)."
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📤 اشتراک‌گذاری", url=share))
    bot.send_message(uid, text, reply_markup=markup)


@private(commands=["link"])
@private(func=lambda m: m.text == BTN_LINK)
@guarded
def show_link(message):
    clear_state(message.from_user.id)
    send_link(message.from_user.id)


def settings_text(u):
    status = "✅ فعال" if u["accepting"] else "⏸ متوقف"
    return (
        "⚙️ <b>تنظیمات</b>\n\n"
        f"👤 نام نمایشی: <b>{esc(display_name_of(u))}</b>\n"
        f"📥 دریافت پیام ناشناس: {status}"
    )


@private(commands=["settings"])
@private(func=lambda m: m.text == BTN_SETTINGS)
@guarded
def show_settings(message):
    uid = message.from_user.id
    clear_state(uid)
    u = get_user(uid)
    bot.send_message(uid, settings_text(u), reply_markup=kb_settings(u))


@private(commands=["help"])
@private(func=lambda m: m.text == BTN_HELP)
@guarded
def show_help(message):
    clear_state(message.from_user.id)
    bot.send_message(message.from_user.id, T.HELP)


@private(func=lambda m: m.text == BTN_ABOUT)
@guarded
def show_about(message):
    clear_state(message.from_user.id)
    bot.send_message(message.from_user.id, T.ABOUT)


@private(commands=["mystats"])
@private(func=lambda m: m.text == BTN_STATS)
@guarded
def show_my_stats(message):
    uid = message.from_user.id
    clear_state(uid)
    received = db.val("SELECT COUNT(*) FROM messages WHERE receiver_id=? AND is_reply=0", (uid,))
    replies = db.val("SELECT COUNT(*) FROM messages WHERE receiver_id=? AND is_reply=1", (uid,))
    sent = db.val("SELECT COUNT(*) FROM messages WHERE sender_id=? AND is_reply=0", (uid,))
    blocked = db.val("SELECT COUNT(*) FROM blocks WHERE blocker_id=?", (uid,))
    bot.send_message(
        uid,
        "📊 <b>آمار شما</b>\n\n"
        f"📥 پیام‌های ناشناس دریافتی: {received}\n"
        f"📩 پاسخ‌های دریافتی: {replies}\n"
        f"📤 پیام‌های ارسالی: {sent}\n"
        f"🚫 کاربران بلاک‌شده: {blocked}",
    )


# ─── settings callbacks ─────────────────────────────────────────────────────


def blocked_view(uid):
    ids = [r["blocked_id"] for r in db.all("SELECT blocked_id FROM blocks WHERE blocker_id=? LIMIT 30", (uid,))]
    markup = types.InlineKeyboardMarkup(row_width=1)
    for blocked_id in ids:
        n = get_alias(uid, blocked_id)
        markup.add(types.InlineKeyboardButton(f"✅ رفع بلاک ناشناس #{n}", callback_data=f"unb:{n}"))
    markup.add(types.InlineKeyboardButton("🔙 بازگشت", callback_data="set:back"))
    if ids:
        text = "🚫 <b>لیست بلاک‌ها</b>\n\nکاربران بلاک‌شده با شماره ناشناس نمایش داده می‌شوند."
    else:
        text = "🚫 <b>لیست بلاک‌ها</b>\n\n" + T.NO_BLOCKS
    return text, markup


@bot.callback_query_handler(func=cb("set"))
@guarded
def cb_settings(call):
    uid = call.from_user.id
    action = call.data.split(":", 1)[1]
    u = get_user(uid)
    if not u:
        bot.answer_callback_query(call.id)
        return

    if action == "name":
        set_state(uid, "waiting_for_name")
        bot.answer_callback_query(call.id)
        bot.send_message(uid, T.ASK_NAME, reply_markup=kb_cancel())

    elif action == "toggle":
        new_value = 0 if u["accepting"] else 1
        db.run("UPDATE users SET accepting=? WHERE user_id=?", (new_value, uid))
        bot.answer_callback_query(call.id, T.TOGGLED_ON if new_value else T.TOGGLED_OFF)
        u = get_user(uid)
        safe_edit(settings_text(u), call, kb_settings(u))

    elif action == "blocked":
        bot.answer_callback_query(call.id)
        text, markup = blocked_view(uid)
        safe_edit(text, call, markup)

    elif action == "relink":
        bot.answer_callback_query(call.id)
        safe_edit(
            "🔄 با ساخت لینک جدید، لینک قبلی (و لینک‌های قدیمی) از کار می‌افتند.\nادامه می‌دهید؟",
            call,
            kb_confirm("set:relink_yes", "✅ بله، لینک جدید بساز"),
        )

    elif action == "relink_yes":
        db.run("UPDATE users SET link_token=?, legacy_ok=0 WHERE user_id=?", (new_token(), uid))
        bot.answer_callback_query(call.id, "✅ لینک جدید ساخته شد.")
        u = get_user(uid)
        safe_edit(settings_text(u), call, kb_settings(u))
        send_link(uid)

    elif action == "delete":
        bot.answer_callback_query(call.id)
        safe_edit(
            "🗑 پروفایل، تنظیمات و صندوق پیام شما حذف می‌شود. این کار قابل بازگشت نیست.\nادامه می‌دهید؟",
            call,
            kb_confirm("set:delete_yes", "🗑 بله، حذف کن"),
        )

    elif action == "delete_yes":
        delete_user_data(uid)
        bot.answer_callback_query(call.id, "✅ حذف شد.")
        safe_edit("🗑 اطلاعات شما حذف شد. هر زمان خواستید با /start دوباره شروع کنید.", call)
        bot.send_message(uid, "خداحافظ 👋", reply_markup=types.ReplyKeyboardRemove())

    elif action == "back":
        bot.answer_callback_query(call.id)
        safe_edit(settings_text(u), call, kb_settings(u))


@bot.callback_query_handler(func=cb("unb"))
@guarded
def cb_unblock(call):
    uid = call.from_user.id
    number = int(call.data.split(":", 1)[1])
    row = db.one("SELECT sender_id FROM aliases WHERE receiver_id=? AND number=?", (uid, number))
    if row:
        db.run("DELETE FROM blocks WHERE blocker_id=? AND blocked_id=?", (uid, row["sender_id"]))
    bot.answer_callback_query(call.id, T.UNBLOCKED)
    text, markup = blocked_view(uid)
    safe_edit(text, call, markup)


# ════════════════════════════════════════════════════════════════════════════
# Message action buttons: reply / block / report
# ════════════════════════════════════════════════════════════════════════════


def load_owned_message(call):
    """Only the receiver of a message may use its buttons."""
    msg_id = int(call.data.split(":", 1)[1])
    row = get_message(msg_id)
    if not row:
        bot.answer_callback_query(call.id, T.EXPIRED, show_alert=True)
        return None
    if row["receiver_id"] != call.from_user.id:
        bot.answer_callback_query(call.id, T.FORBIDDEN, show_alert=True)
        return None
    return row


def refresh_actions(call, row):
    blocked = is_blocked(call.from_user.id, row["sender_id"])
    reported = db.one("SELECT 1 FROM reports WHERE msg_id=?", (row["msg_id"],)) is not None
    safe_edit_markup(
        call,
        actions_markup(row["msg_id"], can_reply=not blocked, can_block=not blocked, can_report=not reported),
    )


@bot.callback_query_handler(func=cb("reply"))
@guarded
def cb_reply(call):
    row = load_owned_message(call)
    if not row:
        return
    uid = call.from_user.id
    target = get_user(row["sender_id"])
    if not target or not target["is_active"] or is_banned(target["user_id"]) or is_blocked(target["user_id"], uid):
        bot.answer_callback_query(call.id, T.UNAVAILABLE, show_alert=True)
        return
    set_state(uid, "sending_reply", {"target": row["sender_id"]})
    bot.answer_callback_query(call.id)
    bot.send_message(uid, T.COMPOSING_REPLY, reply_markup=kb_cancel())


@bot.callback_query_handler(func=cb("block"))
@guarded
def cb_block(call):
    row = load_owned_message(call)
    if not row:
        return
    uid = call.from_user.id
    block_user(uid, row["sender_id"])
    get_alias(uid, row["sender_id"])  # make sure it shows up in the block list
    bot.answer_callback_query(call.id, T.BLOCKED, show_alert=True)
    refresh_actions(call, row)


@bot.callback_query_handler(func=cb("report"))
@guarded
def cb_report(call):
    row = load_owned_message(call)
    if not row:
        return
    if db.one("SELECT 1 FROM reports WHERE msg_id=?", (row["msg_id"],)):
        bot.answer_callback_query(call.id, T.ALREADY_REPORTED, show_alert=True)
        refresh_actions(call, row)
        return

    rid = db.insert(
        "INSERT INTO reports (msg_id, reporter_id, sender_id) VALUES (?, ?, ?)",
        (row["msg_id"], call.from_user.id, row["sender_id"]),
    )
    prior = db.val("SELECT COUNT(*) FROM reports WHERE sender_id=?", (row["sender_id"],)) - 1
    sender = get_user(row["sender_id"])
    who = user_line(sender) if sender else f"<code>{row['sender_id']}</code>"
    card = (
        f"🚩 <b>گزارش تخلف #{rid}</b>\n"
        f"پیام شماره {row['msg_id']} ({'پاسخ' if row['is_reply'] else 'پیام ناشناس'})\n"
        f"فرستنده: {who}\n"
        f"گزارش‌های قبلی علیه او: {prior}"
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🔨 بن فرستنده", callback_data=f"rpt:ban:{rid}"),
        types.InlineKeyboardButton("✅ رد گزارش", callback_data=f"rpt:ok:{rid}"),
    )
    for admin_id in ADMINS:
        try:
            try:
                bot.copy_message(admin_id, call.message.chat.id, call.message.message_id)
            except ApiTelegramException:
                pass
            bot.send_message(admin_id, card, reply_markup=markup)
        except ApiTelegramException as e:
            logger.warning("Could not notify admin %s: %s", admin_id, e)

    bot.answer_callback_query(call.id, T.REPORTED, show_alert=True)
    refresh_actions(call, row)


@bot.callback_query_handler(func=cb("rpt"))
@guarded
@admin_only
def cb_report_admin(call):
    _, action, rid = call.data.split(":")
    admin_id = call.from_user.id
    rep = db.one("SELECT * FROM reports WHERE report_id=?", (int(rid),))
    if not rep:
        bot.answer_callback_query(call.id, T.EXPIRED, show_alert=True)
        return
    if rep["status"] != "pending":
        bot.answer_callback_query(call.id, f"قبلاً بررسی شده ({rep['status']}).", show_alert=True)
        safe_edit_markup(call, None)
        return

    if action == "ban":
        if rep["sender_id"] in ADMINS:
            bot.answer_callback_query(call.id, "ادمین را نمی‌توان بن کرد.", show_alert=True)
            return
        ban_user(rep["sender_id"], f"report #{rid}", admin_id)
        status, result = "banned", f"🔨 فرستنده گزارش #{rid} بن شد."
        try:
            bot.send_message(rep["sender_id"], T.BANNED_NOTICE)
        except ApiTelegramException:
            pass
    else:
        status, result = "dismissed", f"✅ گزارش #{rid} رد شد."

    db.run("UPDATE reports SET status=?, handled_by=? WHERE report_id=?", (status, admin_id, int(rid)))
    bot.answer_callback_query(call.id, result)
    safe_edit_markup(call, None)
    bot.send_message(admin_id, result)


# ════════════════════════════════════════════════════════════════════════════
# Admin panel & commands
# ════════════════════════════════════════════════════════════════════════════


def stats_text():
    users = db.val("SELECT COUNT(*) FROM users")
    active24 = db.val("SELECT COUNT(*) FROM users WHERE last_seen >= datetime('now','-1 day')")
    new24 = db.val("SELECT COUNT(*) FROM users WHERE join_date >= datetime('now','-1 day')")
    unreachable = db.val("SELECT COUNT(*) FROM users WHERE is_active=0")
    total_msgs = db.val("SELECT seq FROM sqlite_sequence WHERE name='messages'")
    msgs24 = db.val("SELECT COUNT(*) FROM messages WHERE timestamp >= datetime('now','-1 day')")
    blocks = db.val("SELECT COUNT(*) FROM blocks")
    bans = db.val("SELECT COUNT(*) FROM bans")
    pending = db.val("SELECT COUNT(*) FROM reports WHERE status='pending'")
    return (
        "📊 <b>آمار ربات</b>\n\n"
        f"👥 کل کاربران: {users}\n"
        f"🟢 فعال در ۲۴ ساعت اخیر: {active24}\n"
        f"🆕 عضو جدید در ۲۴ ساعت اخیر: {new24}\n"
        f"📴 غیرقابل دسترس (ربات را متوقف کرده‌اند): {unreachable}\n\n"
        f"✉️ کل پیام‌های ارسالی: {total_msgs}\n"
        f"✉️ پیام‌های ۲۴ ساعت اخیر: {msgs24}\n"
        f"🚫 بلاک‌ها: {blocks}   🔨 بن‌ها: {bans}\n"
        f"🚩 گزارش‌های در انتظار: {pending}"
    )


ADMIN_HELP = (
    "\n\n<b>دستورات مدیریت</b>\n"
    "/stats ← آمار\n"
    "/broadcast متن ← پیام همگانی (یا ریپلای روی یک پیام و ارسال /broadcast)\n"
    "/ban آیدی [دلیل]\n"
    "/unban آیدی\n"
    "/userinfo آیدی"
)


@private(commands=["admin", "stats"])
@private(func=lambda m: m.text == BTN_ADMIN)
@guarded
@admin_only
def admin_panel(message):
    clear_state(message.from_user.id)
    bot.send_message(message.from_user.id, stats_text() + ADMIN_HELP, reply_markup=kb_admin_panel())


@bot.callback_query_handler(func=cb("adm"))
@guarded
@admin_only
def cb_admin(call):
    action = call.data.split(":", 1)[1]
    if action == "stats":
        bot.answer_callback_query(call.id, "بروزرسانی شد.")
        safe_edit(stats_text() + ADMIN_HELP, call, kb_admin_panel())
    elif action == "export":
        rows = db.all(
            "SELECT user_id, username, first_name, display_name, join_date, last_seen, is_active, accepting "
            "FROM users ORDER BY join_date"
        )
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["user_id", "username", "first_name", "display_name", "join_date", "last_seen", "is_active", "accepting"])
        for r in rows:
            writer.writerow(list(r))
        file = io.BytesIO(buf.getvalue().encode("utf-8-sig"))  # BOM so Excel shows Persian correctly
        file.name = "users.csv"
        bot.answer_callback_query(call.id)
        bot.send_document(call.from_user.id, file, caption=f"👥 {len(rows)} کاربر")


def parse_user_id(message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit():
        return None, ""
    return int(parts[1]), (parts[2].strip() if len(parts) > 2 else "")


@private(commands=["ban"])
@guarded
@admin_only
def cmd_ban(message):
    uid = message.from_user.id
    target, reason = parse_user_id(message)
    if target is None:
        bot.send_message(uid, "استفاده: /ban آیدی [دلیل]")
        return
    if target in ADMINS:
        bot.send_message(uid, "❌ ادمین را نمی‌توان بن کرد.")
        return
    ban_user(target, reason or "manual", uid)
    bot.send_message(uid, f"🔨 کاربر <code>{target}</code> بن شد.")
    try:
        bot.send_message(target, T.BANNED_NOTICE)
    except ApiTelegramException:
        pass


@private(commands=["unban"])
@guarded
@admin_only
def cmd_unban(message):
    uid = message.from_user.id
    target, _ = parse_user_id(message)
    if target is None:
        bot.send_message(uid, "استفاده: /unban آیدی")
        return
    removed = db.run("DELETE FROM bans WHERE user_id=?", (target,))
    bot.send_message(uid, f"✅ کاربر <code>{target}</code> آزاد شد." if removed else "این کاربر بن نبود.")
    if removed:
        try:
            bot.send_message(target, T.UNBANNED_NOTICE)
        except ApiTelegramException:
            pass


@private(commands=["userinfo"])
@guarded
@admin_only
def cmd_userinfo(message):
    uid = message.from_user.id
    target, _ = parse_user_id(message)
    if target is None:
        bot.send_message(uid, "استفاده: /userinfo آیدی")
        return
    u = get_user(target)
    ban = db.one("SELECT reason, banned_at FROM bans WHERE user_id=?", (target,))
    if not u and not ban:
        bot.send_message(uid, "کاربری با این آیدی پیدا نشد.")
        return
    lines = ["👤 <b>اطلاعات کاربر</b>\n"]
    if u:
        lines += [
            user_line(u),
            f"نام نمایشی: {esc(u['display_name'])}",
            f"عضویت: {esc(u['join_date'])}",
            f"آخرین فعالیت: {esc(u['last_seen'] or '—')}",
            f"وضعیت: {'🟢 قابل دسترس' if u['is_active'] else '📴 ربات را متوقف کرده'}"
            f" | دریافت پیام: {'✅' if u['accepting'] else '⏸'}",
        ]
    else:
        lines.append(f"<code>{target}</code> (پروفایل حذف شده)")
    lines += [
        f"ارسالی: {db.val('SELECT COUNT(*) FROM messages WHERE sender_id=?', (target,))}"
        f" | دریافتی: {db.val('SELECT COUNT(*) FROM messages WHERE receiver_id=?', (target,))}",
        f"بلاک‌شده توسط: {db.val('SELECT COUNT(*) FROM blocks WHERE blocked_id=?', (target,))} نفر",
        f"گزارش علیه او: {db.val('SELECT COUNT(*) FROM reports WHERE sender_id=?', (target,))}",
    ]
    if ban:
        lines.append(f"🔨 بن‌شده ({esc(ban['banned_at'])}): {esc(ban['reason'] or '—')}")
    bot.send_message(uid, "\n".join(lines))


# ─── broadcast ──────────────────────────────────────────────────────────────


def broadcast_audience():
    return db.all("SELECT user_id FROM users WHERE is_active=1 AND user_id NOT IN (SELECT user_id FROM bans)")


@private(commands=["broadcast"])
@guarded
@admin_only
def cmd_broadcast(message):
    uid = message.from_user.id
    parts = message.text.split(maxsplit=1)
    text = parts[1].strip() if len(parts) > 1 else ""
    replied = message.reply_to_message

    if replied:
        data = {"mode": "copy", "chat_id": replied.chat.id, "message_id": replied.message_id}
        preview = "🔁 پیامی که روی آن ریپلای کردید (با هر نوع محتوا) برای همه ارسال می‌شود."
    elif text:
        data = {"mode": "text", "text": text}
        preview = f"📢 <b>پیش‌نمایش:</b>\n\n{esc(text)}"
    else:
        bot.send_message(uid, "استفاده: /broadcast متن پیام\nیا روی یک پیام ریپلای کنید و /broadcast بفرستید.")
        return

    set_state(uid, "confirm_broadcast", data)
    count = len(broadcast_audience())
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton(f"✅ ارسال به {count} نفر", callback_data="bc:yes"),
        types.InlineKeyboardButton("❌ انصراف", callback_data="bc:no"),
    )
    bot.send_message(uid, preview, reply_markup=markup)


@bot.callback_query_handler(func=cb("bc"))
@guarded
@admin_only
def cb_broadcast(call):
    uid = call.from_user.id
    action = call.data.split(":", 1)[1]
    state = get_state(uid)
    safe_edit_markup(call, None)
    if action == "no" or not state or state["state"] != "confirm_broadcast":
        clear_state(uid)
        bot.answer_callback_query(call.id, "لغو شد.")
        return
    clear_state(uid)
    bot.answer_callback_query(call.id, "⏳ ارسال شروع شد.")
    threading.Thread(target=run_broadcast, args=(uid, state["data"]), daemon=True).start()


def run_broadcast(admin_id, data):
    audience = broadcast_audience()
    ok = failed = 0
    bot.send_message(admin_id, f"⏳ ارسال پیام همگانی به {len(audience)} نفر شروع شد...")
    for row in audience:
        target = row["user_id"]
        for _ in range(3):
            try:
                if data["mode"] == "text":
                    bot.send_message(target, f"📢 <b>پیام مدیریت:</b>\n\n{html.escape(data['text'])}")
                else:
                    bot.copy_message(target, data["chat_id"], data["message_id"])
                ok += 1
                break
            except ApiTelegramException as e:
                if e.error_code == 429:
                    params = (getattr(e, "result_json", None) or {}).get("parameters") or {}
                    time.sleep(int(params.get("retry_after", 5)) + 1)
                    continue
                if is_unreachable(e):
                    mark_inactive(target)
                failed += 1
                break
            except Exception:
                logger.exception("Broadcast error for %s", target)
                failed += 1
                break
        else:  # three 429s in a row
            failed += 1
        time.sleep(BROADCAST_DELAY)
    bot.send_message(admin_id, f"✅ پایان ارسال همگانی.\nموفق: {ok}\nناموفق: {failed}")


# ════════════════════════════════════════════════════════════════════════════
# Catch-all: conversation states (name change, composing a message)
# ════════════════════════════════════════════════════════════════════════════


@private(content_types=ALL_TYPES)
@guarded
def on_any_message(message):
    uid = message.from_user.id

    # Unknown /commands must never be sent to someone as an "anonymous message".
    if message.content_type == "text" and message.text.startswith("/"):
        bot.send_message(uid, T.UNKNOWN_CMD, reply_markup=kb_main(uid))
        return

    state = get_state(uid)
    if not state:
        bot.send_message(uid, T.MENU_HINT, reply_markup=kb_main(uid))
        return

    name = state["state"]

    if name == "waiting_for_name":
        if message.content_type != "text":
            bot.send_message(uid, T.TEXT_ONLY)
            return
        new_name = clean_name(message.text)
        if not new_name:
            bot.send_message(uid, T.NAME_INVALID)
            return
        db.run("UPDATE users SET display_name=? WHERE user_id=?", (new_name, uid))
        clear_state(uid)
        bot.send_message(uid, T.NAME_SAVED.format(name=esc(new_name)), reply_markup=kb_main(uid))

    elif name in ("sending_anonymous", "sending_reply"):
        process_outgoing(message, state)

    else:  # e.g. a stale confirm_broadcast
        clear_state(uid)
        bot.send_message(uid, T.MENU_HINT, reply_markup=kb_main(uid))


@bot.my_chat_member_handler()
def on_my_chat_member(update):
    """Tracks users who block / unblock the bot so we stop messaging dead chats."""
    try:
        if update.chat.type != "private":
            return
        status = update.new_chat_member.status
        db.run(
            "UPDATE users SET is_active=? WHERE user_id=?",
            (0 if status in ("kicked", "left") else 1, update.chat.id),
        )
    except Exception:
        logger.exception("my_chat_member handler failed")


# ════════════════════════════════════════════════════════════════════════════
# Startup
# ════════════════════════════════════════════════════════════════════════════


def setup_commands():
    user_cmds = [
        types.BotCommand("start", "شروع / منوی اصلی"),
        types.BotCommand("link", "دریافت لینک من"),
        types.BotCommand("settings", "تنظیمات"),
        types.BotCommand("mystats", "آمار من"),
        types.BotCommand("cancel", "لغو عملیات"),
        types.BotCommand("help", "راهنما"),
    ]
    admin_cmds = user_cmds + [
        types.BotCommand("admin", "پنل مدیریت"),
        types.BotCommand("broadcast", "پیام همگانی"),
        types.BotCommand("ban", "بن کاربر"),
        types.BotCommand("unban", "آزاد کردن کاربر"),
        types.BotCommand("userinfo", "اطلاعات کاربر"),
    ]
    try:
        bot.set_my_commands(user_cmds)
        for admin_id in ADMINS:
            try:
                bot.set_my_commands(admin_cmds, scope=types.BotCommandScopeChat(admin_id))
            except ApiTelegramException:
                pass  # admin has not started the bot yet
    except Exception:
        logger.exception("Could not set bot commands")


def maintenance_loop():
    while True:
        try:
            db.run("DELETE FROM states WHERE updated_at < ?", (time.time() - STATE_TTL,))
            if RETENTION_DAYS > 0:
                db.run("DELETE FROM messages WHERE timestamp < datetime('now', ?)", (f"-{RETENTION_DAYS} days",))
            limiter.cleanup()
        except Exception:
            logger.exception("Maintenance failed")
        time.sleep(3600)


def main():
    global BOT_USERNAME
    logger.info("Initializing database at %s ...", DB_PATH)
    db.init()

    logger.info("Removing any existing webhook ...")
    bot.remove_webhook()

    if not BOT_USERNAME:
        BOT_USERNAME = bot.get_me().username
    if not ADMINS:
        logger.warning("ADMINS is empty: admin features are disabled.")

    setup_commands()
    threading.Thread(target=maintenance_loop, daemon=True).start()

    logger.info("Starting @%s (polling) ...", BOT_USERNAME)
    bot.infinity_polling(
        timeout=60,
        long_polling_timeout=60,
        allowed_updates=["message", "callback_query", "my_chat_member"],
    )


if __name__ == "__main__":
    main()
