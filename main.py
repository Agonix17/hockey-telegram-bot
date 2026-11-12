import asyncio
import html
import logging
import os
import re
import sqlite3
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("hockey_bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_USER_ID = int(os.environ["ADMIN_USER_ID"])
DRAFT_CHANNEL_ID = os.environ["DRAFT_CHANNEL_ID"]
CHANNEL_ID = os.environ["CHANNEL_ID"]
DB_PATH = Path(os.getenv("DB_PATH", "data/hockey_bot.sqlite3"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
DRAFT_CHAT_ID = None


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                post TEXT NOT NULL,
                source_url TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                importance_rating INTEGER,
                writing_rating INTEGER,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS user_state (
                user_id INTEGER PRIMARY KEY,
                action TEXT NOT NULL,
                draft_id INTEGER NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )


def get_draft(draft_id):
    with db() as con:
        return con.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()


def set_state(action, draft_id):
    with db() as con:
        con.execute(
            """INSERT INTO user_state(user_id,action,draft_id)
               VALUES(?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
               action=excluded.action,draft_id=excluded.draft_id,
               updated_at=CURRENT_TIMESTAMP""",
            (ADMIN_USER_ID, action, draft_id),
        )


def get_state():
    with db() as con:
        return con.execute(
            "SELECT * FROM user_state WHERE user_id=?", (ADMIN_USER_ID,)
        ).fetchone()


def is_draft_chat(chat_id):
    return DRAFT_CHAT_ID is not None and chat_id == DRAFT_CHAT_ID


def clear_state():
    with db() as con:
        con.execute("DELETE FROM user_state WHERE user_id=?", (ADMIN_USER_ID,))


def parse_draft_message(raw):
    text = raw.strip()
    text = re.sub(r"^📝\s*ЧЕРНОВИК.*?\n+", "", text, flags=re.I | re.S)
    match = re.search(r"\n(?:🔗\s*)?Источник:\s*(\S+)\s*$", text, flags=re.I)
    url = match.group(1) if match else ""
    if match:
        text = text[: match.start()].rstrip()
    return text, url


def moderation_keyboard(draft_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Опубликовать", callback_data=f"publish:{draft_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{draft_id}"),
            ],
            [InlineKeyboardButton("✏️ Изменить", callback_data=f"edit:{draft_id}")],
        ]
    )


def rating_keyboard(draft_id, metric):
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(str(n), callback_data=f"rate:{metric}:{draft_id}:{n}")
            for n in range(5, 0, -1)
        ]]
    )


def note_keyboard(draft_id):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Без примечаний", callback_data=f"note_none:{draft_id}")]]
    )


def review_caption(post, source_url=""):
    text = f"📝 ЧЕРНОВИК\n\n{post.strip()}"
    if source_url:
        text += f"\n\n🔗 Источник: {source_url}"
    return text


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    message = query.message if query else None
    if not query or not message or not update.effective_user:
        return
    if update.effective_user.id != ADMIN_USER_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return
    if not is_draft_chat(message.chat.id):
        await query.answer("Это не черновой канал.", show_alert=True)
        return
    await query.answer()
    parts = (query.data or "").split(":")
    action = parts[0]
    try:
        draft_id = int(parts[1])
    except (IndexError, ValueError):
        return
    row = get_draft(draft_id)
    if not row:
        await query.answer("Черновик не найден.", show_alert=True)
        return

    if action == "publish":
        await context.bot.send_message(
            chat_id=CHANNEL_ID, text=row["post"], disable_web_page_preview=True
        )
        with db() as con:
            con.execute(
                "UPDATE drafts SET status='published',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (draft_id,),
            )
        await query.edit_message_text(
            f"✅ <b>ОПУБЛИКОВАНО</b>\n\n{html.escape(row['post'])}\n\nОцени важность новости:",
            parse_mode=ParseMode.HTML,
            reply_markup=rating_keyboard(draft_id, "importance"),
        )
        return

    if action == "reject":
        with db() as con:
            con.execute(
                "UPDATE drafts SET status='rejected',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (draft_id,),
            )
        await query.edit_message_reply_markup(reply_markup=None)
        return

    if action == "edit":
        set_state("await_edit", draft_id)
        await context.bot.send_message(
            chat_id=DRAFT_CHANNEL_ID,
            text="✏️ Пришли следующим сообщением исправленный текст поста.",
        )
        return

    if action == "rate" and len(parts) == 4:
        metric, value = parts[1], int(parts[3])
        column = "importance_rating" if metric == "importance" else "writing_rating"
        with db() as con:
            con.execute(
                f"UPDATE drafts SET {column}=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (value, draft_id),
            )
        if metric == "importance":
            await query.edit_message_text(
                f"Оценка важности: {value}/5.\n\nОцени качество написания:",
                reply_markup=rating_keyboard(draft_id, "writing"),
            )
        else:
            set_state("await_note", draft_id)
            await query.edit_message_text(
                f"Оценка написания: {value}/5.\n\nЕсть примечание?",
                reply_markup=note_keyboard(draft_id),
            )
        return

    if action == "note_none":
        clear_state()
        await query.edit_message_text("✅ Оценки сохранены. Примечаний нет.")


async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.channel_post
    if not message or not is_draft_chat(message.chat.id):
        return
    state = get_state()
    if not state or not message.text:
        return
    draft_id = int(state["draft_id"])
    if state["action"] == "await_edit":
        with db() as con:
            con.execute(
                "UPDATE drafts SET post=?,status='pending',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (message.text.strip(), draft_id),
            )
        clear_state()
        await context.bot.send_message(
            chat_id=DRAFT_CHANNEL_ID,
            text=review_caption(message.text),
            reply_markup=moderation_keyboard(draft_id),
            disable_web_page_preview=True,
        )
    elif state["action"] == "await_note":
        with db() as con:
            con.execute(
                "UPDATE drafts SET note=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (message.text.strip(), draft_id),
            )
        clear_state()


async def button_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create a local draft record for a button message sent by Make."""
    message = update.channel_post
    if not message or not is_draft_chat(message.chat.id):
        return
    raw = message.text or message.caption or ""
    if "ЧЕРНОВИК" not in raw.upper():
        return
    post, source_url = parse_draft_message(raw)
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO drafts(chat_id,message_id,post,source_url) VALUES(?,?,?,?)",
            (str(message.chat.id), message.message_id, post, source_url),
        )
        row = con.execute(
            "SELECT id FROM drafts WHERE chat_id=? AND message_id=?",
            (str(message.chat.id), message.message_id),
        ).fetchone()
    if row:
        await context.bot.edit_message_reply_markup(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=moderation_keyboard(int(row["id"])),
        )


def build_app():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, button_message_handler))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, channel_post_handler))
    return app


async def main():
    global DRAFT_CHAT_ID
    init_db()
    app = build_app()
    log.info("Telegram-бот запущен в режиме polling")
    await app.initialize()
    draft_chat = await app.bot.get_chat(DRAFT_CHANNEL_ID)
    DRAFT_CHAT_ID = draft_chat.id
    log.info("Черновой канал распознан: %s", DRAFT_CHAT_ID)
    await app.start()
    if not app.updater:
        raise RuntimeError("Telegram updater unavailable")
    await app.updater.start_polling(drop_pending_updates=False)
    try:
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
