"""BLIKI Telegram content bot. Start on Railway: python3 nail_bot.py

Required environment: BOT_TOKEN, OPENAI_KEY, CHANNEL_ID, OWNER_CHAT_ID.
Optional: OPENAI_MODEL (gpt-4o-mini), DB_PATH (drafts.sqlite3), BOOKING_URL.
Requires: python-telegram-bot>=21,<23; openai>=1,<3; APScheduler>=3.10,<4.
"""
import asyncio
import base64
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
from telegram.ext import (
    Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters
)

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

SYSTEM_POST = """Ты редактор Telegram-канала маникюрной студии BLIKI (Челябинск, район Академ Riverside).
Цель — показывать реальные работы мастеров, формировать доверие и интерес к записи.
Пиши естественно, с характером, без пафоса, чрезмерных метафор, штампов и выдуманных фактов.
Обычно 200–450 символов; можно короче. Конкретные видимые детали важнее пустых эпитетов.
Не придумывай технику, материал, марку, цены, акции, доступное время или адрес.
Не делай обязательный вопрос в конце. Не добавляй призыв записаться в каждый пост.
Максимум два уместных хештега (допустим #работыBLIKI), без общих наборов. Максимум два эмодзи.
Если дано только видео без описания содержания, не заявляй, будто рассмотрел кадры.
Не изображай персонажа Бликса, если владелец об этом не попросил.
Возвращай только готовый текст публикации, без комментариев и кавычек."""

SYSTEM_CHAT = """Ты дружелюбный помощник владельца маникюрной студии BLIKI.
Отвечай кратко и по делу. Не выдумывай факты о студии."""

SYSTEM_IDEAS = """Ты помощник студии маникюра BLIKI в Челябинске.
Предложи идеи сезонного контента на ближайшую неделю. У тебя нет доступа к живому интернету;
не выдавай идеи за проверенные свежие тренды. Верни только JSON с ключами:
 top_trend (строка — идея недели), trends (массив из 3 идей), colors (массив из 3 цветов),
 summary (краткий план на неделю)."""


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def season() -> str:
    m = now_local().month
    return "зима" if m in (12, 1, 2) else "весна" if m in (3, 4, 5) else "лето" if m in (6, 7, 8) else "осень"


def connect_db():
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with connect_db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS drafts (
            id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, text TEXT NOT NULL,
            source_caption TEXT NOT NULL, media_type TEXT, file_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
        )""")


def save_draft(text, source_caption="", media_type=None, file_id=None):
    draft_id = uuid.uuid4().hex[:16]
    with connect_db() as db:
        db.execute("INSERT INTO drafts(id, owner_id, text, source_caption, media_type, file_id) VALUES(?,?,?,?,?,?)",
                   (draft_id, OWNER_CHAT_ID, text, source_caption, media_type, file_id))
    return draft_id


def read_draft(draft_id):
    with connect_db() as db:
        item = db.execute("SELECT * FROM drafts WHERE id=? AND owner_id=?", (draft_id, OWNER_CHAT_ID)).fetchone()
    return dict(item) if item else None


def update_draft(draft_id, text):
    with connect_db() as db:
        db.execute("UPDATE drafts SET text=? WHERE id=? AND status='pending'", (text, draft_id))


def mark_published(draft_id):
    with connect_db() as db:
        db.execute("UPDATE drafts SET status='published' WHERE id=? AND status='pending'", (draft_id,))


def buttons(draft_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Опубликовать", callback_data=f"approve:{draft_id}"),
        InlineKeyboardButton("🔄 Переписать", callback_data=f"retry:{draft_id}"),
    ]])


async def ai_post(caption="", image_bytes=None, mime="image/jpeg", old_text=None, is_video=False):
    instruction = ("Перепиши следующий пост существенно иначе, но без выдумок.\n"
                   f"Предыдущий текст:\n{old_text}\n\n" if old_text else "Напиши пост.\n")
    instruction += f"Пожелание владельца: {caption or 'нет'}. Сезон: {season()}."
    if is_video:
        instruction += " Приложено видео, но ты не видишь его содержимое: опирайся только на описание владельца."
    elif image_bytes is None:
        instruction += " Фотографии нет; не описывай несуществующее изображение."
    content = [{"type": "text", "text": instruction}]
    if image_bytes is not None:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}", "detail": "low"}})
    result = await ai.chat.completions.create(
        model=OPENAI_MODEL, messages=[{"role": "system", "content": SYSTEM_POST},
                                      {"role": "user", "content": content}], max_tokens=600,
    )
    return (result.choices[0].message.content or "").strip()


async def fetch_photo_bytes(ctx, file_id):
    tg_file = await ctx.bot.get_file(file_id)
    return bytes(await tg_file.download_as_bytearray())


async def show_draft(update, text, caption="", media_type=None, file_id=None):
    draft_id = save_draft(text, caption, media_type, file_id)
    await update.effective_message.reply_text(f"📝 Черновик:\n\n{text}", reply_markup=buttons(draft_id))


async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if update.effective_user.id != OWNER_CHAT_ID:
        await msg.reply_text("⛔ Это приватный бот.")
        return
    await msg.reply_text("⏳ Готовлю текст…")
    try:
        file_id = msg.photo[-1].file_id
        photo = await fetch_photo_bytes(ctx, file_id)
        text = await ai_post(caption=msg.caption or "", image_bytes=photo)
        if not text:
            raise ValueError("Пустой ответ модели")
        await show_draft(update, text, msg.caption or "", "photo", file_id)
    except Exception:
        log.exception("Photo post generation failed")
        await msg.reply_text("❌ Не получилось создать пост. Проверь логи и попробуй снова.")


async def handle_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if update.effective_user.id != OWNER_CHAT_ID:
        await msg.reply_text("⛔ Это приватный бот.")
        return
    file_id = msg.video.file_id if msg.video else msg.animation.file_id
    media_type = "video" if msg.video else "animation"
    await msg.reply_text("⏳ Готовлю подпись к видео по твоему описанию…")
    try:
        text = await ai_post(caption=msg.caption or "", is_video=True)
        if not text:
            raise ValueError("Пустой ответ модели")
        await show_draft(update, text, msg.caption or "", media_type, file_id)
    except Exception:
        log.exception("Video post generation failed")
        await msg.reply_text("❌ Не удалось создать подпись.")


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_CHAT_ID:
        return
    msg = update.effective_message
    user_text = (msg.text or "").strip()
    triggers = ("напиши пост", "сделай пост", "пост на тему", "сгенерируй пост")
    try:
        if user_text.lower().startswith(triggers):
            text = await ai_post(caption=user_text)
            if not text:
                raise ValueError("Пустой ответ модели")
            await show_draft(update, text, user_text)
        else:
            result = await ai.chat.completions.create(
                model=OPENAI_MODEL, max_tokens=450,
                messages=[{"role": "system", "content": SYSTEM_CHAT},
                          {"role": "user", "content": user_text}],
            )
            await msg.reply_text((result.choices[0].message.content or "Нет ответа.")[:4096])
    except Exception:
        log.exception("Text handling failed")
        await msg.reply_text("❌ Ошибка обработки сообщения. Загляни в логи Railway.")


async def handle_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != OWNER_CHAT_ID:
        await query.answer("Нет доступа", show_alert=True)
        return
    await query.answer()
    try:
        action, draft_id = query.data.split(":", 1)
    except ValueError:
        return
    if action not in ("approve", "retry"):
        return
    # Serialize mutations and publication to avoid double sends and retry/publish races.
    async with publish_lock:
        draft = read_draft(draft_id)
        if not draft or draft["status"] != "pending":
            await query.edit_message_text("⚠️ Черновик недоступен или уже опубликован.")
            return
        if action == "approve":
            text = draft["text"]
            media_type, file_id = draft["media_type"], draft["file_id"]
            try:
                if media_type in ("photo", "video", "animation") and len(text) > 1024:
                    # Telegram media captions are limited to 1024 characters.
                    raise ValueError("Подпись длиннее 1024 символов. Перепиши пост.")
                if media_type == "photo":
                    await ctx.bot.send_photo(chat_id=CHANNEL_ID, photo=file_id, caption=text)
                elif media_type == "video":
                    await ctx.bot.send_video(chat_id=CHANNEL_ID, video=file_id, caption=text)
                elif media_type == "animation":
                    await ctx.bot.send_animation(chat_id=CHANNEL_ID, animation=file_id, caption=text)
                else:
                    await ctx.bot.send_message(chat_id=CHANNEL_ID, text=text)
                mark_published(draft_id)
                await query.edit_message_text("✅ Опубликовано в канале!")
            except Exception:
                log.exception("Publishing failed for draft %s", draft_id)
                await query.message.reply_text("❌ Публикация не подтверждена. Посмотри логи Railway и канал перед повтором.")
        else:
            try:
                photo = await fetch_photo_bytes(ctx, draft["file_id"]) if draft["media_type"] == "photo" else None
                new_text = await ai_post(caption=draft["source_caption"], image_bytes=photo,
                                         old_text=draft["text"],
                                         is_video=draft["media_type"] in ("video", "animation"))
                if not new_text:
                    raise ValueError("Пустой ответ модели")
                update_draft(draft_id, new_text)
                await query.edit_message_text(f"📝 Новый вариант:\n\n{new_text}", reply_markup=buttons(draft_id))
            except Exception:
                log.exception("Rewrite failed for draft %s", draft_id)
                await query.message.reply_text("❌ Не смогла переписать пост. Попробуй позже.")


async def weekly_ideas(app: Application):
    log.info("Weekly ideas job started at %s", now_local().isoformat())
    try:
        result = await ai.chat.completions.create(
            model=OPENAI_MODEL, max_tokens=450, response_format={"type": "json_object"},
            messages=[{"role": "system", "content": SYSTEM_IDEAS},
                      {"role": "user", "content": f"Дата: {now_local():%d.%m.%Y}. Сезон: {season()}."}],
        )
        data = json.loads(result.choices[0].message.content or "{}")
        message = ("📋 Идеи контента на неделю (не мониторинг интернета)\n\n"
                   f"💡 Тема: {data.get('top_trend', '—')}\n"
                   f"Идеи: {', '.join(data.get('trends', []))}\n"
                   f"Цвета: {', '.join(data.get('colors', []))}\n\n"
                   f"{data.get('summary', '')}")
        await app.bot.send_message(chat_id=OWNER_CHAT_ID, text=message[:4096])
        log.info("Weekly ideas sent")
    except Exception:
        log.exception("Weekly ideas failed")


async def after_start(app: Application):
    # post_init runs inside python-telegram-bot's running asyncio loop.
    scheduler = AsyncIOScheduler(timezone=LOCAL_TZ)
    scheduler.add_job(weekly_ideas, "cron", day_of_week="mon", hour=9, minute=0,
                      args=[app], id="weekly_ideas", replace_existing=True)
    scheduler.start()
    app.bot_data["scheduler"] = scheduler
    log.info("Bot initialized. Scheduler active, Monday 09:00 Asia/Yekaterinburg")


async def before_stop(app: Application):
    scheduler = app.bot_data.get("scheduler")
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)
    await ai.close()


def main():
    init_db()
    application = (Application.builder().token(BOT_TOKEN)
                   .post_init(after_start).post_shutdown(before_stop).build())
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.VIDEO | filters.ANIMATION, handle_video))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(CallbackQueryHandler(handle_button))
    log.info("Starting BLIKI bot via Telegram polling...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
