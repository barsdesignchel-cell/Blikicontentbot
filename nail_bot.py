"""BLIKI Telegram content bot — revised from the user's existing bot.

Environment variables in Railway:
  BOT_TOKEN, OPENAI_KEY, CHANNEL_ID, OWNER_CHAT_ID — required
  OPENAI_MODEL — optional (default gpt-4o-mini)
  DB_PATH — optional (default drafts.sqlite3; use Railway Volume for persistence)
  BOOKING_URL — optional (real booking link; never fabricated)

Requirements: python-telegram-bot>=21,<23 ; openai>=1,<3 ; APScheduler>=3.10,<4
Start command: python bliki_bot_updated.py
"""

import asyncio
import base64
import io
import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from openai import AsyncOpenAI
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters

BOT_TOKEN = os.environ["BOT_TOKEN"]
OPENAI_KEY = os.environ["OPENAI_KEY"]
CHANNEL_ID = os.environ["CHANNEL_ID"]
OWNER_CHAT_ID = int(os.environ["OWNER_CHAT_ID"])
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
DB_PATH = os.getenv("DB_PATH", "drafts.sqlite3")
BOOKING_URL = os.getenv("BOOKING_URL", "").strip()
LOCAL_TZ = ZoneInfo("Asia/Yekaterinburg")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bliki")
ai = AsyncOpenAI(api_key=OPENAI_KEY, timeout=60.0)
publish_lock = asyncio.Lock()

SYSTEM_POST = """Ты — редактор Telegram-канала студии маникюра BLIKI (Челябинск, Академ Riverside).
Цель: показывать реальные работы мастеров, вызывать доверие и интерес к записи у местных клиенток.

Тон: дружелюбно, естественно, конкретно, без пафоса и навязчивых продаж.
- Обычно 200–450 символов, иногда меньше. Длинный текст только при реальной информационной ценности.
- Опирайся на видимые детали фото и проверенные сведения из задания. Не угадывай марку лака,
  технологию покрытия, стойкость, стоимость, свободные даты и адрес.
- Не превращай описание в художественное сочинение. Избегай «космической магии»,
  «невероятной игры света», «этот маникюр — настоящее произведение искусства» и подобных клише.
- Разнообразь начала и композицию; не вставляй вопрос или призыв в каждый пост.
- Для обычной работы используй не более двух релевантных хештегов, включая при необходимости
  #работыBLIKI. Не добавляй общие теги ради количества.
- Смайлики — по желанию, не больше двух.
- Уместно иногда напоминать о записи, но не делай это механически.
- Не утверждай, что видишь содержание видео, если тебе дали только текстовое описание.
- Возвращай только готовый текст поста, без пояснений и кавычек.
"""

SYSTEM_TRENDS = """Ты помощник студии BLIKI. Предложи идеи сезонного контента о маникюре.
ВАЖНО: у тебя нет доступа к интернету и свежей статистике; не называй это проверенными
трендами текущей недели. Ответь JSON с ключами top_trend, trends (3), colors (3), summary."""

SYSTEM_CHAT = """Ты дружелюбный помощник владельца студии маникюра BLIKI.
Отвечай кратко и по существу. Не выдумывай факты о студии."""


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def season() -> str:
    month = now_local().month
    if month in (12, 1, 2):
        return "зима"
    if month in (3, 4, 5):
        return "весна"
    if month in (6, 7, 8):
        return "лето"
    return "осень"


def db_connect():
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with db_connect() as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS drafts (
            id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, text TEXT NOT NULL,
            source_caption TEXT NOT NULL, media_type TEXT, file_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
        )""")


def save_draft(text: str, source_caption: str = "", media_type=None, file_id=None) -> str:
    draft_id = uuid.uuid4().hex[:16]
    with db_connect() as connection:
        connection.execute(
            "INSERT INTO drafts(id,owner_id,text,source_caption,media_type,file_id) VALUES(?,?,?,?,?,?)",
            (draft_id, OWNER_CHAT_ID, text, source_caption, media_type, file_id),
        )
    return draft_id


def get_draft(draft_id: str):
    with db_connect() as connection:
        return connection.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()


def update_draft(draft_id: str, text: str):
    with db_connect() as connection:
        connection.execute("UPDATE drafts SET text=? WHERE id=? AND status='pending'", (text, draft_id))


def mark_published(draft_id: str):
    with db_connect() as connection:
        connection.execute("UPDATE drafts SET status='published' WHERE id=?", (draft_id,))


def buttons(draft_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Опубликовать", callback_data=f"approve:{draft_id}"),
        InlineKeyboardButton("🔄 Переписать", callback_data=f"retry:{draft_id}"),
    ]])
