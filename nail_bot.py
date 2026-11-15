"""
Universal Content Bot
===============
Скиньте фото + текст (пожелание) → бот напишет пост → нажмите ✅ → опубликуется в канал.
Можно писать текстом: "Напиши пост про..." — бот сгенерирует пост на тему.
Можно просто общаться с ботом.
Каждый понедельник в 9:00 — сводка трендов.

Настройка (переменные окружения в Railway):
  BOT_TOKEN     — токен от @BotFather
  OPENAI_KEY    — ключ с platform.openai.com
  CHANNEL_ID    — @username вашего канала или числовой ID
  OWNER_CHAT_ID — ваш числовой ID (для доступа и рассылки)
  OPENAI_MODEL  — (необязательно) модель, по умолчанию gpt-4o-mini
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

BOT_TOKEN     = os.environ["BOT_TOKEN"]
OPENAI_KEY    = os.environ["OPENAI_KEY"]
CHANNEL_ID    = os.environ["CHANNEL_ID"]
OWNER_CHAT_ID = int(os.environ.get("OWNER_CHAT_ID", "0"))

OPENAI_MODEL  = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

ai = OpenAI(api_key=OPENAI_KEY)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

drafts: dict = {}

# ── Промпты ───────────────────────────────────────────────────────────────────

# Обновленный системный промпт: теперь он универсальный
SYSTEM_POST = """Ты — креативный контент-мейкер для Telegram-канала. 
Твоя задача — писать вовлекающие посты.

Правила:
1. ВСЕГДА следуй теме и пожеланиям, которые дает владелец канала в тексте.
2. Если к сообщению прикреплено фото, внимательно посмотри на него. Опиши то, что на нем, и свяжи это с темой поста. (Например: если на фото улыбающийся мастер, а тема "хорошей рабочей недели", напиши про позитивный настрой мастера и пожелай хорошей недели).
3. Если на фото маникюр, опиши его. Если на фото что-то другое (интерьер, кофе, человек), используй это как визуальный фон для твоего текста.
4. Структура: цепляющее начало, основная мысль, призыв к действию (или вопрос), 3-5 хэштегов.
5. Длина: 600–900 символов. Emoji — умеренно (до 5 штук).
6. Запрещено использовать клише: "нежный", "стильный", "элегантный", "трендовый".
7. Пиши живым, дружелюбным языком, как будто общаешься с подписчиками."""

SYSTEM_TRENDS = """Ты — аналитик beauty-трендов. Дай сводку актуальных трендов маникюра
для небольшой студии. Опирайся на свои знания о сезонных и актуальных тенденциях.

Ответь строго в формате JSON:
{
  "top_trend": "главный тренд одной фразой",
  "trends": ["тренд 1", "тренд 2", "тренд 3"],
  "colors": ["цвет 1", "цвет 2", "цвет 3"],
  "summary": "2–3 предложения для контент-плана"
}"""

SYSTEM_CHAT = """Ты — умный и дружелюбный помощник владельца маникюрной студии. 
Помогаешь с идеями, советуешь по работе, можешь просто поболтать. 
Отвечай кратко и по делу."""

# ── Генерация поста ───────────────────────────────────────────────────────────

def get_season() -> str:
    m = datetime.now().month
    return {12: "зима", 1: "зима", 2: "зима",
             3: "весна", 4: "весна", 5: "весна",
             6: "лето", 7: "лето", 8: "лето"}.get(m, "осень")

def generate_post(image_url: str = None, user_caption: str = "", retrying: bool = False) -> str:
    if retrying:
        user_text = f"Перепиши пост иначе, сохранив смысл. Тема/пожелание: {user_caption}"
    else:
        if image_url and user_caption:
            user_text = (
                f"Вот фото. Владелец просит написать пост на тему: '{user_caption}'.\n"
                f"Посмотри на фото, но напиши именно то, что просит владелец. "
                f"Сезон: {get_season()}."
            )
        elif image_url:
            user_text = f"Вот фото работы. Сезон: {get_season()}. Напиши пост, описывая то, что видишь."
        else:
            user_text = f"Напиши пост на тему: '{user_caption}'. Сезон: {get_season()}."

    content = [{"type": "text", "text": user_text}]
    if image_url:
        content.append({"type": "image_url", "image_url": {"url": image_url}})

    resp = ai.chat.completions.create(
        model=OPENAI_MODEL,
        max_tokens=700, # Чуть увеличили, чтобы модель успевала описать и фото, и тему
        messages=[
            {"role": "system", "content": SYSTEM_POST},
            {"role": "user", "content": content},
        ],
    )
    return resp.choices[0].message.content.strip()

# ── Обработчики Telegram ──────────────────────────────────────────────────────

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    
    if msg.from_user.id != OWNER_CHAT_ID:
        await msg.reply_text("⛔ Извините, это приватный бот.")
        return

    chat_id = msg.chat_id
    user_caption = msg.caption if msg.caption else ""
    
    await msg.reply_text("⏳ Пишу пост…")

    file = await ctx.bot.get_file(msg.photo[-1].file_id)
    image_url = file.file_path

    try:
        post_text = generate_post(image_url=image_url, user_caption=user_caption)
    except Exception as e:
        log.error("OpenAI error: %s", e)
        await msg.reply_text("❌ Не удалось сгенерировать пост. Попробуйте ещё раз.")
        return

    drafts[chat_id] = {"text": post_text, "file_id": msg.photo[-1].file_id, "caption": user_caption}

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Опубликовать", callback_data="approve"),
        InlineKeyboardButton("🔄 Переписать",   callback_data="retry"),
    ]])
    await msg.reply_text(f"📝 Черновик:\n\n{post_text}", reply_markup=keyboard)


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    
    if msg.from_user.id != OWNER_CHAT_ID:
        return

    user_text = msg.text.strip()
    
    triggers = ["напиши пост", "сделай пост", "пост на тему", "сгенерируй пост"]
    is_post_request = any(user_text.lower().startswith(trigger) for trigger in triggers)

    await ctx.bot.send_chat_action(chat_id=msg.chat_id, action="typing")

    try:
        if is_post_request:
            post_text = generate_post(user_caption=user_text)
            chat_id = msg.chat_id
            drafts[chat_id] = {"text": post_text, "file_id": None, "caption": user_text}

            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Опубликовать", callback_data="approve"),
                InlineKeyboardButton("🔄 Переписать",   callback_data="retry"),
            ]])
            await msg.reply_text(f"📝 Черновик:\n\n{post_text}", reply_markup=keyboard)
            
        else:
            resp = ai.chat.completions.create(
                model=OPENAI_MODEL,
                max_tokens=500,
                messages=[
                    {"role": "system", "content": SYSTEM_CHAT},
                    {"role": "user", "content": user_text}
                ],
            )
            answer = resp.choices[0].message.content.strip()
            await msg.reply_text(answer)

    except Exception as e:
        log.error("OpenAI text error: %s", e)
        await msg.reply_text("❌ Произошла ошибка. Попробуйте позже.")


async def handle_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != OWNER_CHAT_ID:
        await query.edit_message_text("⛔ У вас нет прав на это действие.")
        return

    chat_id = query.message.chat_id
    draft = drafts.get(chat_id)

    if not draft:
        await query.edit_message_text("⚠️ Черновик не найден. Пришлите фото заново.")
        return

    if query.data == "approve":
        if draft["file_id"]:
            await ctx.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=draft["file_id"],
                caption=draft["text"],
            )
        else:
            await ctx.bot.send_message(
                chat_id=CHANNEL_ID,
                text=draft["text"],
            )
        await query.edit_message_text("✅ Опубликовано в канале!")
        drafts.pop(chat_id, None)

    elif query.data == "retry":
        await query.edit_message_text("⏳ Переписываю…")
        file_path = None
        if draft["file_id"]:
            file = await ctx.bot.get_file(draft["file_id"])
            file_path = file.file_path
            
        try:
            new_text = generate_post(image_url=file_path, user_caption=draft.get("caption", ""), retrying=True)
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
    log.info("Running weekly trend analysis…")
    try:
        resp = ai.chat.completions.create(
            model=OPENAI_MODEL,
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
        
        if OWNER_CHAT_ID:
            await app.bot.send_message(chat_id=OWNER_CHAT_ID, text=text)
        log.info("Trends sent: %s", data.get("top_trend"))
    except Exception as e:
        log.error("Trends error: %s", e)

# ── Запуск ────────────────────────────────────────────────────────────────────

def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .build()
    )

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_button))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        weekly_trends, "cron",
        day_of_week="mon", hour=9, minute=0,
        args=[app],
    )
    scheduler.start()

    log.info("Bot started. Waiting for photos and messages…")
    
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
