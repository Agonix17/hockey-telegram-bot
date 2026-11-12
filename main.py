import asyncio
import html
import json
import logging
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import uvicorn


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("hockey_bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_USER_ID = int(os.environ["ADMIN_USER_ID"])
CHANNEL_ID = os.environ["CHANNEL_ID"]
DRAFT_CHANNEL_ID = os.environ["DRAFT_CHANNEL_ID"]
MAKE_WEBHOOK_SECRET = os.environ["MAKE_WEBHOOK_SECRET"]
DB_PATH = Path(os.getenv("DB_PATH", "data/hockey_bot.sqlite3"))

DB_PATH.parent.mkdir(parents=True, exist_ok=True)


class DraftPayload(BaseModel):
    post: str = Field(min_length=1, max_length=10000)
    source_url: str = Field(default="", max_length=2000)
    title: str = Field(default="", max_length=2000)
    reason: str = Field(default="", max_length=3000)
    facts: str = Field(default="", max_length=10000)
    sources: list[str] = Field(default_factory=list)
    rss_guid: str = Field(default="", max_length=2000)


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post TEXT NOT NULL,
                original_post TEXT NOT NULL,
                source_url TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                facts TEXT NOT NULL DEFAULT '',
                sources_json TEXT NOT NULL DEFAULT '[]',
                rss_guid TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                importance_rating INTEGER,
                writing_rating INTEGER,
                note TEXT NOT NULL DEFAULT '',
                channel_message_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS drafts_rss_guid_unique
              ON drafts(rss_guid) WHERE rss_guid <> '';
            CREATE TABLE IF NOT EXISTS user_state (
                user_id INTEGER PRIMARY KEY,
                action TEXT NOT NULL,
                draft_id INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_draft(draft_id: int) -> sqlite3.Row | None:
    with db() as con:
        return con.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()


def set_state(user_id: int, action: str, draft_id: int) -> None:
    with db() as con:
        con.execute(
            """INSERT INTO user_state(user_id,action,draft_id,updated_at)
               VALUES(?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
               action=excluded.action,draft_id=excluded.draft_id,
               updated_at=excluded.updated_at""",
            (user_id, action, draft_id, now()),
        )


def get_state(user_id: int) -> sqlite3.Row | None:
    with db() as con:
        return con.execute("SELECT * FROM user_state WHERE user_id=?", (user_id,)).fetchone()


def clear_state(user_id: int) -> None:
    with db() as con:
        con.execute("DELETE FROM user_state WHERE user_id=?", (user_id,))


def action_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Опубликовать", callback_data=f"publish:{draft_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{draft_id}"),
            ],
            [InlineKeyboardButton("✏️ Изменить", callback_data=f"edit:{draft_id}")],
        ]
    )


def rating_keyboard(draft_id: int, metric: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(str(n), callback_data=f"rate:{metric}:{draft_id}:{n}")
                for n in range(5, 0, -1)
            ]
        ]
    )


def note_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Без примечаний", callback_data=f"note_none:{draft_id}")]]
    )


def review_text(row: sqlite3.Row) -> str:
    pieces = ["📝 <b>Черновик «Хоккей по-простому»</b>", "", html.escape(row["post"])]
    if row["reason"]:
        pieces += ["", "ℹ️ <b>Для модератора:</b> " + html.escape(row["reason"])]
    if row["source_url"]:
        pieces += ["", f'🔗 <a href="{html.escape(row["source_url"], quote=True)}">Источник</a>']
    return "\n".join(pieces)


async def send_review(context: ContextTypes.DEFAULT_TYPE, draft_id: int) -> None:
    row = fetch_draft(draft_id)
    if row is None:
        return
    await context.bot.send_message(
        chat_id=DRAFT_CHANNEL_ID,
        text=review_text(row),
        parse_mode=ParseMode.HTML,
        reply_markup=action_keyboard(draft_id),
        disable_web_page_preview=True,
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user:
        return
    if update.effective_user.id != ADMIN_USER_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return
    await query.answer()
    parts = (query.data or "").split(":")
    action = parts[0]
    try:
        draft_id = int(parts[2] if action == "rate" else parts[1])
    except (IndexError, ValueError):
        return
    row = fetch_draft(draft_id)
    if row is None:
        await query.answer("Черновик не найден.", show_alert=True)
        return

    if action == "publish":
        if row["status"] == "published":
            await query.answer("Уже опубликовано.", show_alert=True)
            return
        sent = await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=html.escape(row["post"]),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        with db() as con:
            con.execute(
                "UPDATE drafts SET status='published',channel_message_id=?,updated_at=? WHERE id=?",
                (sent.message_id, now(), draft_id),
            )
        await query.edit_message_text(
            text=(
                "✅ <b>Опубликовано</b>\n\n"
                f"{html.escape(row['post'])}\n\n"
                "Оцени важность новости по шкале от 5 до 1:"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=rating_keyboard(draft_id, "importance"),
        )
        return

    if action == "reject":
        with db() as con:
            con.execute("UPDATE drafts SET status='rejected',updated_at=? WHERE id=?", (now(), draft_id))
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Отклонено. Черновик и решение сохранены.")
        return

    if action == "edit":
        set_state(ADMIN_USER_ID, "await_edit", draft_id)
        await query.message.reply_text(
            "Пришли исправленный вариант следующим сообщением. Его оформлю в режиме копирования, после чего можно будет снова принять, отклонить или изменить."
        )
        return

    if action == "rate":
        metric, value = parts[1], int(parts[3])
        if metric == "importance":
            with db() as con:
                con.execute(
                    "UPDATE drafts SET importance_rating=?,updated_at=? WHERE id=?",
                    (value, now(), draft_id),
                )
            await query.edit_message_text(
                f"Оценка важности: {value}/5.\nОцени качество написания поста:",
                reply_markup=rating_keyboard(draft_id, "writing"),
            )
        elif metric == "writing":
            with db() as con:
                con.execute(
                    "UPDATE drafts SET writing_rating=?,updated_at=? WHERE id=?",
                    (value, now(), draft_id),
                )
            set_state(ADMIN_USER_ID, "await_note", draft_id)
            await query.edit_message_text(
                f"Оценка написания: {value}/5.\nЕсть примечание? Напиши его сообщением или нажми кнопку.",
                reply_markup=note_keyboard(draft_id),
            )
        return

    if action == "note_none":
        with db() as con:
            con.execute("UPDATE drafts SET note='',updated_at=? WHERE id=?", (now(), draft_id))
        clear_state(ADMIN_USER_ID)
        await query.edit_message_text("Спасибо! Оценки и решение сохранены.")


async def admin_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message or user.id != ADMIN_USER_ID or not message.text:
        return
    state = get_state(user.id)
    if not state:
        return
    draft_id = int(state["draft_id"])
    if state["action"] == "await_edit":
        corrected = message.text.strip()
        if not corrected:
            await message.reply_text("Пустой текст не сохранён. Пришли исправленный пост.")
            return
        with db() as con:
            con.execute(
                "UPDATE drafts SET post=?,status='pending',updated_at=? WHERE id=?",
                (corrected, now(), draft_id),
            )
        clear_state(user.id)
        row = fetch_draft(draft_id)
        await message.reply_text(
            f"<pre>{html.escape(corrected)}</pre>",
            parse_mode=ParseMode.HTML,
        )
        await send_review(context, draft_id)
        return
    if state["action"] == "await_note":
        with db() as con:
            con.execute(
                "UPDATE drafts SET note=?,updated_at=? WHERE id=?",
                (message.text.strip(), now(), draft_id),
            )
        clear_state(user.id)
        await message.reply_text("Примечание сохранено вместе с оценками.")


async def draft_channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Receive an edited draft as a new channel post.

    Telegram channel posts do not expose the human author's user ID to the bot.
    We therefore accept a text post only in the configured draft channel and
    only while the admin has an active edit state.
    """
    message = update.channel_post
    expected_channel_id = context.application.bot_data.get("draft_channel_id")
    if not message or message.chat.id != expected_channel_id or not message.text:
        return
    state = get_state(ADMIN_USER_ID)
    if not state:
        return
    draft_id = int(state["draft_id"])
    if state["action"] == "await_edit":
        corrected = message.text.strip()
        if not corrected:
            return
        with db() as con:
            con.execute(
                "UPDATE drafts SET post=?,status='pending',updated_at=? WHERE id=?",
                (corrected, now(), draft_id),
            )
        clear_state(ADMIN_USER_ID)
        await send_review(context, draft_id)
    elif state["action"] == "await_note":
        with db() as con:
            con.execute(
                "UPDATE drafts SET note=?,updated_at=? WHERE id=?",
                (message.text.strip(), now(), draft_id),
            )
        clear_state(ADMIN_USER_ID)
        await context.bot.send_message(
            chat_id=expected_channel_id,
            text="Примечание сохранено вместе с оценками.",
        )


async def root(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and update.effective_user.id == ADMIN_USER_ID:
        await update.effective_message.reply_text(
            "Бот модерации «Хоккей по-простому» готов принимать черновики из Make."
        )


def build_bot() -> Application:
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_message_handler))
    application.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, draft_channel_post_handler))
    application.add_handler(MessageHandler(filters.COMMAND, root))
    return application


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    bot = build_bot()
    app.state.telegram = bot
    await bot.initialize()
    draft_chat = await bot.bot.get_chat(DRAFT_CHANNEL_ID)
    bot.bot_data["draft_channel_id"] = draft_chat.id
    await bot.start()
    if bot.updater is None:
        raise RuntimeError("Telegram polling updater is unavailable")
    await bot.updater.start_polling(drop_pending_updates=False)
    yield
    await bot.updater.stop()
    await bot.stop()
    await bot.shutdown()


api = FastAPI(title="Hockey Simple Telegram Bot", lifespan=lifespan)


@api.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@api.post("/make/draft")
async def receive_draft(
    payload: DraftPayload,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    expected = f"Bearer {MAKE_WEBHOOK_SECRET}"
    if not authorization or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")

    timestamp = now()
    with db() as con:
        try:
            cursor = con.execute(
                """INSERT INTO drafts
                (post,original_post,source_url,title,reason,facts,sources_json,rss_guid,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    payload.post.strip(),
                    payload.post.strip(),
                    payload.source_url,
                    payload.title,
                    payload.reason,
                    payload.facts,
                    json.dumps(payload.sources, ensure_ascii=False),
                    payload.rss_guid,
                    "pending",
                    timestamp,
                    timestamp,
                ),
            )
            draft_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError:
            return {"ok": True, "duplicate": True}

    await send_review(api.state.telegram, draft_id)
    return {"ok": True, "duplicate": False, "draft_id": draft_id}


async def run() -> None:
    config = uvicorn.Config(
        api,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8080")),
        log_level=os.getenv("UVICORN_LOG_LEVEL", "info"),
    )
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(run())
