"""
telegram_bot.py
─────────────────────────────────────────────────────────────────────────────
Bidirectional Telegram bot for the AI Software Factory.

Commands available to admin:
  /start         – Welcome message + status
  /status        – Show live system state (cycle, queue length, tokens used …)
  /build <spec>  – Enqueue a new code generation job
  /learn <url>   – Force-crawl a specific URL now
  /pause         – Pause the crawler (sets a flag in system state)
  /resume        – Resume crawler
  /queue         – Show pending task queue
  /logs          – Send last 50 lines of the run log
  /reflection    – Send last reflection record as formatted JSON
  /pool          – Show API key pool status
  /help          – Show command list

The bot runs in a background thread using python-telegram-bot v20
(Application + polling).  The factory main loop calls `notify()` to push
status messages into the queue; a dedicated sender thread drains the queue.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
import time
from pathlib import Path

from telegram import Update, Bot
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from engine.state_manager import SystemState, ReflectionLog

logger = logging.getLogger("telegram_bot")

TELEGRAM_TOKEN: str = os.getenv("TELEGRAM_TOKEN", "")
ADMIN_CHAT_ID: int = int(os.getenv("TELEGRAM_ADMIN_CHAT_ID", "0"))

# Cross-thread notification queue (main loop → sender thread → Telegram)
_notify_queue: queue.Queue[str] = queue.Queue(maxsize=500)


# ── Low-level sync notify helper (used by api_pool, main loop, etc.) ─────────


def notify(message: str) -> None:
    """
    Non-blocking push of a notification message.
    Silently drops if queue is full (avoids blocking the factory thread).
    """
    try:
        _notify_queue.put_nowait(f"🤖 *[Factory]* {message}")
    except queue.Full:
        pass


def notify_raw(message: str) -> None:
    """Push a message without the Factory prefix."""
    try:
        _notify_queue.put_nowait(message)
    except queue.Full:
        pass


# ── Sender thread (drains _notify_queue) ─────────────────────────────────────


class _SenderThread(threading.Thread):
    """
    Background thread that sends queued notifications to the admin chat.
    Uses a plain sync Bot.send_message call via asyncio.run().
    """

    def __init__(self, token: str, chat_id: int) -> None:
        super().__init__(daemon=True, name="tg-sender")
        self._token = token
        self._chat_id = chat_id
        self._stop = threading.Event()

    def run(self) -> None:
        if not self._token or not self._chat_id:
            logger.warning("Telegram sender disabled (no token / admin chat id).")
            return
        while not self._stop.is_set():
            try:
                msg = _notify_queue.get(timeout=2)
                asyncio.run(self._send(msg))
            except queue.Empty:
                continue
            except Exception as exc:
                logger.error("Sender thread error: %s", exc)
                time.sleep(5)

    async def _send(self, text: str) -> None:
        bot = Bot(token=self._token)
        try:
            await bot.send_message(
                chat_id=self._chat_id,
                text=text[:4096],
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as exc:
            logger.error("Failed to send Telegram message: %s", exc)


# ── Command handlers ──────────────────────────────────────────────────────────


def _build_command_handler(system_state: SystemState, enqueue_fn):
    """
    Returns async handler functions bound to the current system state.
    enqueue_fn(task: dict) is called when the user issues /build.
    """

    def _is_admin(update: Update) -> bool:
        if not ADMIN_CHAT_ID:
            return True  # no restriction configured
        return update.effective_chat.id == ADMIN_CHAT_ID

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update):
            return
        await update.message.reply_text(
            "🌙 *رَّبِّ زِدْنِي عِلْمًا*\n\n"
            "AI Software Factory is *online*.\n"
            "Use /help to see available commands.",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update):
            return
        help_text = (
            "*Available Commands*\n\n"
            "/start – Welcome\n"
            "/status – System status\n"
            "/build `<spec>` – Enqueue a build job\n"
            "/learn `<url>` – Force-crawl a URL\n"
            "/pause – Pause crawler\n"
            "/resume – Resume crawler\n"
            "/queue – Show task queue\n"
            "/logs – Last 50 log lines\n"
            "/reflection – Last cycle reflection\n"
            "/pool – API key pool status\n"
            "/help – This message"
        )
        await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update):
            return
        snap = system_state.full_snapshot()
        lines = [
            f"*Run ID:* `{snap.get('run_id')}`",
            f"*Cycle:* `{snap.get('cycle_count')}`",
            f"*Status:* `{snap.get('status')}`",
            f"*Queue depth:* `{len(snap.get('task_queue', []))}`",
            f"*Active task:* `{snap.get('active_task')}`",
            f"*Tokens used:* `{snap.get('total_tokens_used')}`",
            f"*Errors total:* `{snap.get('errors_total')}`",
            f"*Last checkpoint:* `{snap.get('last_checkpoint')}`",
        ]
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    async def cmd_build(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update):
            return
        spec = " ".join(context.args).strip()
        if not spec:
            await update.message.reply_text(
                "Usage: /build `<project specification>`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        task = {
            "type": "build",
            "spec": spec,
            "chat_id": update.effective_chat.id,
            "message_id": update.message.message_id,
        }
        enqueue_fn(task)
        await update.message.reply_text(
            f"✅ Build job enqueued!\n`{spec[:200]}`",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def cmd_learn(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update):
            return
        url = " ".join(context.args).strip()
        if not url.startswith("http"):
            await update.message.reply_text("Provide a full URL starting with http(s)://")
            return
        task = {"type": "crawl", "url": url, "chat_id": update.effective_chat.id}
        enqueue_fn(task)
        await update.message.reply_text(f"🔍 Crawl task queued for: `{url}`", parse_mode=ParseMode.MARKDOWN)

    async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update):
            return
        system_state.set_status("paused")
        await update.message.reply_text("⏸️ Crawler paused.")

    async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update):
            return
        system_state.set_status("running")
        await update.message.reply_text("▶️ Crawler resumed.")

    async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update):
            return
        tasks = system_state.task_queue
        if not tasks:
            await update.message.reply_text("📭 Task queue is empty.")
            return
        lines = []
        for i, t in enumerate(tasks[:20], 1):
            lines.append(
                f"{i}. `[{t.get('type')}]` {str(t.get('spec', t.get('url', '')))[:60]}"
            )
        await update.message.reply_text(
            "*Pending tasks:*\n" + "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
        )

    async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update):
            return
        log_path = Path("factory.log")
        if not log_path.exists():
            await update.message.reply_text("No log file found.")
            return
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = "\n".join(lines[-50:])
        await update.message.reply_text(
            f"```\n{tail[-3800:]}\n```",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def cmd_reflection(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update):
            return
        log = ReflectionLog()
        last = log.last()
        if not last:
            await update.message.reply_text("No reflection records yet.")
            return
        text = json.dumps(last, indent=2, ensure_ascii=False)
        await update.message.reply_text(
            f"```json\n{text[:3800]}\n```",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def cmd_pool(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update):
            return
        # Import lazily to avoid circular dependency
        from engine.api_pool import GROQ_KEYS
        from engine.state_manager import ApiPoolState

        pool_state = ApiPoolState()
        available = pool_state.available_count(GROQ_KEYS)
        total = len(GROQ_KEYS)
        lines = [
            f"*API Key Pool*",
            f"Total keys: `{total}`",
            f"Available: `{available}`",
            f"Cooling down: `{total - available}`",
        ]
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    async def fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Ignore any non-command messages."""
        pass

    return {
        "start": cmd_start,
        "help": cmd_help,
        "status": cmd_status,
        "build": cmd_build,
        "learn": cmd_learn,
        "pause": cmd_pause,
        "resume": cmd_resume,
        "queue": cmd_queue,
        "logs": cmd_logs,
        "reflection": cmd_reflection,
        "pool": cmd_pool,
        "fallback": fallback,
    }


# ── Bot runner ────────────────────────────────────────────────────────────────


class TelegramInterface:
    """
    Encapsulates the full Telegram bot lifecycle.
    Call start() to launch polling in a background thread.
    """

    def __init__(self, system_state: SystemState, enqueue_fn) -> None:
        self._system_state = system_state
        self._enqueue_fn = enqueue_fn
        self._sender = _SenderThread(TELEGRAM_TOKEN, ADMIN_CHAT_ID)
        self._bot_thread: threading.Thread | None = None
        self._app: Application | None = None

    def start(self) -> None:
        """Launch sender thread + polling thread."""
        self._sender.start()
        self._bot_thread = threading.Thread(
            target=self._run_polling,
            daemon=True,
            name="tg-polling",
        )
        self._bot_thread.start()
        logger.info("Telegram interface started.")

    def stop(self) -> None:
        self._sender._stop.set()
        if self._app:
            asyncio.run(self._app.stop())

    def _run_polling(self) -> None:
        if not TELEGRAM_TOKEN:
            logger.warning("TELEGRAM_TOKEN not set – bot polling disabled.")
            return
        try:
            asyncio.run(self._async_polling())
        except Exception as exc:
            logger.error("Telegram polling crashed: %s", exc, exc_info=True)

    async def _async_polling(self) -> None:
        handlers = _build_command_handler(self._system_state, self._enqueue_fn)

        app = (
            Application.builder()
            .token(TELEGRAM_TOKEN)
            .build()
        )
        self._app = app

        for cmd, fn in handlers.items():
            if cmd == "fallback":
                app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fn))
            else:
                app.add_handler(CommandHandler(cmd, fn))

        notify_raw(
            "🌙 *رَّبِّ زِدْنِي عِلْمًا*\n\n"
            f"AI Software Factory is *online*.\n"
            f"Run ID: `{self._system_state.run_id}`"
        )

        await app.run_polling(allowed_updates=Update.ALL_TYPES)
