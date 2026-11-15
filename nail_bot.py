"""
Nail Studio Bot
===============
Скиньте фото работы → бот напишет пост → нажмите ✅ → опубликуется в канал.
Каждый понедельник в 9:00 — сводка трендов.

Настройка (три переменные окружения):
  BOT_TOKEN   — токен от @BotFather
  OPENAI_KEY  — ключ с platform.openai.com
  CHANNEL_ID  — @username вашего канала или числовой ID

Запуск локально:
  pip install python-telegram-bot openai apscheduler
  BOT_TOKEN=... OPENAI_KEY=... CHANNEL_ID=... python nail_bot.py

Деплой на Railway: просто залейте файл, добавьте переменные в Settings → Variables.
"""

import os, json, logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from openai import OpenAI
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ── Конфиг ────────────────────────────────────────────────────────────────────

BOT_TOKEN  = os.environ["BOT_TOKEN"]
OPENAI_KEY = os.environ["OPENAI_KEY"]
CHANNEL_ID = os.environ["CHANNEL_ID"]   # @yourchannel или -1001234567890

ai = OpenAI(api_key=OPENAI_KEY)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

# Храним черновик в памяти: { chat_id: { "text": ..., "file_id": ... } }
drafts: dict = {}

# ── Промпты ───────────────────────────────────────────────────────────────────

SYSTEM_POST = """Ты — контент-менеджер маникюрной студии. Пишешь посты для Telegram-канала.

Аудитория: женщины 20–45 лет. Стиль: демократично, но с характером — не пафосно, не занудно.
Как умная подруга, которая разбирается в маникюре.

Структура поста:
1. Первая строка — цепляющая фраза (без клише "идеальный маникюр", "нежный образ")
2. 2–3 предложения о работе: что сделано, какое настроение, чем интересно
3. Практичная деталь: покрытие, стойкость или повод — одна конкретная вещь
4. Призыв к действию: короткий, не агрессивный
5. 5–7 хэштегов (микс популярных и нишевых)

Запрещено: слова "нежный", "стильный", "элегантный", "трендовый".
Emoji — не больше 3 штук. Длина: 600–900 символов."""

SYSTEM_TRENDS = """Ты — аналитик beauty-трендов. Дай сводку актуальных трендов маникюра
для небольшой студии. Опирайся на свои знания о сезонных и актуальных тенденциях.

Ответь строго в формате JSON:
{
  "top_trend": "главный тренд одной фразой",
  "trends": ["тренд 1", "тренд 2", "тренд 3"],
  "colors": ["цвет 1", "цвет 2", "цвет 3"],
  "summary": "2–3 предложения для контент-плана"
}"""

# ── Генерация поста ───────────────────────────────────────────────────────────

def get_season() -> str:
    m = datetime.now().month
    return {12: "зима", 1: "зима", 2: "зима",
             3: "весна", 4: "весна", 5: "весна",
             6: "лето", 7: "лето", 8: "лето"}.get(m, "осень")

def generate_post(image_url: str, retrying: bool = False) -> str:
    user_text = (
        "Перепиши пост иначе — другой угол подачи, другое начало." if retrying
        else f"Вот фото работы. Сезон: {get_season()}. Напиши пост."
    )
    resp = ai.chat.completions.create(
        model="gpt-4o",
        max_tokens=600,
        messages=[
            {"role": "system", "content": SYSTEM_POST},
            {"role": "user", "content": [
                {"type": "text",      "text": user_text},
                {"type": "image_url", "image_url": {"url": image_url}},
            ]},
        ],
    )
    return resp.choices[0].message.content.strip()

# ── Обработчики Telegram ──────────────────────────────────────────────────────

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Владелец прислал фото → генерируем черновик."""
    msg = update.message
    chat_id = msg.chat_id

    await msg.reply_text("⏳ Пишу пост…")

    # Берём самое большое фото
    file = await ctx.bot.get_file(msg.photo[-1].file_id)
    image_url = file.file_path          # прямая ссылка на файл

    try:
        post_text = generate_post(image_url)
    except Exception as e:
        log.error("OpenAI error: %s", e)
        await msg.reply_text("❌ Не удалось сгенерировать пост. Попробуйте ещё раз.")
        return

    # Сохраняем черновик
    drafts[chat_id] = {"text": post_text, "file_id": msg.photo[-1].file_id}

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Опубликовать", callback_data="approve"),
        InlineKeyboardButton("🔄 Переписать",   callback_data="retry"),
    ]])
    await msg.reply_text(f"📝 Черновик:\n\n{post_text}", reply_markup=keyboard)


async def handle_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Нажата кнопка Опубликовать / Переписать."""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    draft = drafts.get(chat_id)

    if not draft:
        await query.edit_message_text("⚠️ Черновик не найден. Пришлите фото заново.")
        return

    if query.data == "approve":
        # Публикуем фото с подписью в канал
        await ctx.bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=draft["file_id"],
            caption=draft["text"],
        )
        await query.edit_message_text("✅ Опубликовано в канале!")
        drafts.pop(chat_id, None)

    elif query.data == "retry":
        await query.edit_message_text("⏳ Переписываю…")
        file = await ctx.bot.get_file(draft["file_id"])
        try:
            new_text = generate_post(file.file_path, retrying=True)
        except Exception as e:
            log.error("OpenAI retry error: %s", e)
            await query.edit_message_text("❌ Ошибка. Попробуйте ещё раз.")
            return

        draft["text"] = new_text
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Опубликовать", callback_data="approve"),
            InlineKeyboardButton("🔄 Переписать",   callback_data="retry"),
        ]])
        await query.edit_message_text(f"📝 Новый вариант:\n\n{new_text}", reply_markup=keyboard)

# ── Еженедельный анализ трендов ───────────────────────────────────────────────

async def weekly_trends(app: Application):
    """Запускается каждый понедельник в 9:00."""
    log.info("Running weekly trend analysis…")
    try:
        resp = ai.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=400,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_TRENDS},
                {"role": "user",   "content": f"Дата: {datetime.now().strftime('%d.%m.%Y')}. Сезон: {get_season()}."},
            ],
        )
        data = json.loads(resp.choices[0].message.content)
        text = (
            "📊 Тренды недели\n\n"
            f"🔥 Главный: {data.get('top_trend','—')}\n\n"
            f"Тренды: {', '.join(data.get('trends', []))}\n"
            f"Цвета: {', '.join(data.get('colors', []))}\n\n"
            f"💡 {data.get('summary','')}"
        )
        # Находим chat_id владельца — шлём в первый активный чат из drafts
        # Или замените OWNER_CHAT_ID на ваш числовой ID
        owner_id = int(os.environ.get("OWNER_CHAT_ID", "0"))
        if owner_id:
            await app.bot.send_message(chat_id=owner_id, text=text)
        log.info("Trends sent: %s", data.get("top_trend"))
    except Exception as e:
        log.error("Trends error: %s", e)

# ── Запуск ────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_button))

    # Планировщик — тренды каждый понедельник в 9:00
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        weekly_trends, "cron",
        day_of_week="mon", hour=9, minute=0,
        args=[app],
    )
    scheduler.start()

    log.info("Bot started. Waiting for photos…")
    app.run_polling()

if __name__ == "__main__":
    main()
