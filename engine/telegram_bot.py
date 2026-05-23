"""
telegram_bot.py
─────────────────────────────────────────────────────────────────────────────
Bidirectional Telegram bot for the AI Software Factory.

Migrated from python-telegram-bot v20 (async) → pyTelegramBotAPI v4 (sync).
This eliminates the `set_wakeup_fd only works in main thread` crash that
occurred when the bot was launched from a background thread.

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

Architecture
─────────────
  • TelegramInterface.start()
      ├── _SenderThread  (daemon) – drains _notify_queue → bot.send_message()
      └── tg-polling     (daemon) – bot.infinity_polling(threaded=True)
                                    fully sync, safe from any thread

  • notify() / notify_raw() – non-blocking push into _notify_queue;
    callable from any thread in the factory.

  • self.bot – exposed as public attribute so main.py can call
    bot.send_chat_action() for live typing indicators.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from pathlib import Path

import telebot                          # pyTelegramBotAPI
from telebot.types import Message

from engine.state_manager import SystemState, ReflectionLog

logger = logging.getLogger("telegram_bot")

TELEGRAM_TOKEN: str = os.getenv("TELEGRAM_TOKEN", "")
ADMIN_CHAT_ID:  int = int(os.getenv("TELEGRAM_ADMIN_CHAT_ID", "0"))

# Markdown parse mode constant
MD = "Markdown"

# Cross-thread notification queue (any thread → _SenderThread → Telegram)
_notify_queue: queue.Queue[str] = queue.Queue(maxsize=500)

# Module-level bot instance (set when TelegramInterface is created)
_bot_instance: telebot.TeleBot | None = None


# ── Low-level notify helpers (public API – unchanged signatures) ──────────────

def notify(message: str, chat_id: int | None = None) -> None:
    """
    Non-blocking push of a notification message.
    Silently drops if queue is full (avoids blocking the factory thread).
    Optional chat_id is ignored here (broadcast goes to ADMIN_CHAT_ID);
    it is accepted for signature compatibility with main.py's _anim_notify().
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


# ── Sender thread (drains _notify_queue → Telegram) ──────────────────────────

class _SenderThread(threading.Thread):
    """
    Daemon thread that sends queued notifications to the admin chat.
    Uses the sync pyTelegramBotAPI – no asyncio needed.
    """

    def __init__(self, bot: telebot.TeleBot, chat_id: int) -> None:
        super().__init__(daemon=True, name="tg-sender")
        self._bot     = bot
        self._chat_id = chat_id
        self._stop    = threading.Event()

    def run(self) -> None:
        if not self._chat_id:
            logger.warning("Telegram sender disabled (TELEGRAM_ADMIN_CHAT_ID not set).")
            return

        while not self._stop.is_set():
            try:
                msg = _notify_queue.get(timeout=2)
                self._send(msg)
            except queue.Empty:
                continue
            except Exception as exc:
                logger.error("Sender thread error: %s", exc)
                time.sleep(5)

    def _send(self, text: str) -> None:
        try:
            self._bot.send_message(
                chat_id    = self._chat_id,
                text       = text[:4096],
                parse_mode = MD,
            )
        except Exception as exc:
            logger.error("Failed to send Telegram message: %s", exc)


# ── Guard helper ──────────────────────────────────────────────────────────────

def _is_admin(message: Message) -> bool:
    """Return True if the message originates from the configured admin chat."""
    if not ADMIN_CHAT_ID:
        return True                     # no restriction configured
    return message.chat.id == ADMIN_CHAT_ID


# ── TelegramInterface ─────────────────────────────────────────────────────────

class TelegramInterface:
    """
    Encapsulates the full Telegram bot lifecycle.

    Call start() to launch the sender + polling threads.
    self.bot is exposed so main.py can call bot.send_chat_action().
    """

    def __init__(self, system_state: SystemState, enqueue_fn) -> None:
        self._system_state = system_state
        self._enqueue_fn   = enqueue_fn

        # Build the sync bot (no asyncio involved)
        self.bot = telebot.TeleBot(
            token         = TELEGRAM_TOKEN,
            parse_mode    = MD,
            threaded      = True,       # handler calls run in their own threads
        ) if TELEGRAM_TOKEN else None

        # Expose globally for notify helpers (send_chat_action usage)
        global _bot_instance
        _bot_instance = self.bot

        self._sender     = _SenderThread(self.bot, ADMIN_CHAT_ID) if self.bot else None
        self._poll_thread: threading.Thread | None = None

        # Register all command handlers
        if self.bot:
            self._register_handlers()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Launch sender thread + polling thread."""
        if self._sender:
            self._sender.start()

        self._poll_thread = threading.Thread(
            target = self._run_polling,
            daemon = True,
            name   = "tg-polling",
        )
        self._poll_thread.start()
        logger.info("Telegram interface started (pyTelegramBotAPI, sync).")

    def stop(self) -> None:
        if self._sender:
            self._sender._stop.set()
        if self.bot:
            try:
                self.bot.stop_polling()
            except Exception:
                pass

    def _run_polling(self) -> None:
        if not self.bot:
            logger.warning("TELEGRAM_TOKEN not set – bot polling disabled.")
            return
        try:
            logger.info("Bot polling started.")
            # infinity_polling is sync and thread-safe – no set_wakeup_fd issue.
            self.bot.infinity_polling(
                timeout          = 20,
                long_polling_timeout = 20,
                logger_level     = logging.WARNING,
                allowed_updates  = ["message"],
            )
        except Exception as exc:
            logger.error("Telegram polling crashed: %s", exc, exc_info=True)

    # ── Command registration ───────────────────────────────────────────────────

    def _register_handlers(self) -> None:
        bot = self.bot

        # /start ───────────────────────────────────────────────────────────────
        @bot.message_handler(commands=["start"])
        def cmd_start(msg: Message) -> None:
            if not _is_admin(msg):
                return
            bot.reply_to(
                msg,
                "🌙 *رَّبِّ زِدْنِي عِلْمًا*\n\n"
                "AI Software Factory is *online*.\n"
                "Use /help to see available commands.",
            )

        # /help ────────────────────────────────────────────────────────────────
        @bot.message_handler(commands=["help"])
        def cmd_help(msg: Message) -> None:
            if not _is_admin(msg):
                return
            bot.reply_to(
                msg,
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
                "/help – This message",
            )

        # /status ──────────────────────────────────────────────────────────────
        @bot.message_handler(commands=["status"])
        def cmd_status(msg: Message) -> None:
            if not _is_admin(msg):
                return
            snap = self._system_state.full_snapshot()
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
            bot.reply_to(msg, "\n".join(lines))

        # /build ───────────────────────────────────────────────────────────────
        @bot.message_handler(commands=["build"])
        def cmd_build(msg: Message) -> None:
            if not _is_admin(msg):
                return
            # Everything after "/build "
            parts = msg.text.split(maxsplit=1)
            spec  = parts[1].strip() if len(parts) > 1 else ""
            if not spec:
                bot.reply_to(
                    msg,
                    "Usage: /build `<project specification>`",
                )
                return
            task = {
                "type":       "build",
                "spec":       spec,
                "chat_id":    msg.chat.id,
                "message_id": msg.message_id,
            }
            self._enqueue_fn(task)
            bot.reply_to(
                msg,
                f"✅ Build job enqueued!\n`{spec[:200]}`",
            )

        # /learn ───────────────────────────────────────────────────────────────
        @bot.message_handler(commands=["learn"])
        def cmd_learn(msg: Message) -> None:
            if not _is_admin(msg):
                return
            parts = msg.text.split(maxsplit=1)
            url   = parts[1].strip() if len(parts) > 1 else ""
            if not url.startswith("http"):
                bot.reply_to(msg, "Provide a full URL starting with http(s)://")
                return
            task = {
                "type":    "crawl",
                "url":     url,
                "chat_id": msg.chat.id,
            }
            self._enqueue_fn(task)
            bot.reply_to(msg, f"🔍 Crawl task queued for: `{url}`")

        # /pause ───────────────────────────────────────────────────────────────
        @bot.message_handler(commands=["pause"])
        def cmd_pause(msg: Message) -> None:
            if not _is_admin(msg):
                return
            self._system_state.set_status("paused")
            bot.reply_to(msg, "⏸️ Crawler paused.")

        # /resume ──────────────────────────────────────────────────────────────
        @bot.message_handler(commands=["resume"])
        def cmd_resume(msg: Message) -> None:
            if not _is_admin(msg):
                return
            self._system_state.set_status("running")
            bot.reply_to(msg, "▶️ Crawler resumed.")

        # /queue ───────────────────────────────────────────────────────────────
        @bot.message_handler(commands=["queue"])
        def cmd_queue(msg: Message) -> None:
            if not _is_admin(msg):
                return
            tasks = self._system_state.task_queue
            if not tasks:
                bot.reply_to(msg, "📭 Task queue is empty.")
                return
            lines = []
            for i, t in enumerate(tasks[:20], 1):
                preview = str(t.get("spec", t.get("url", "")))[:60]
                lines.append(f"{i}. `[{t.get('type')}]` {preview}")
            bot.reply_to(msg, "*Pending tasks:*\n" + "\n".join(lines))

        # /logs ────────────────────────────────────────────────────────────────
        @bot.message_handler(commands=["logs"])
        def cmd_logs(msg: Message) -> None:
            if not _is_admin(msg):
                return
            log_path = Path("factory.log")
            if not log_path.exists():
                bot.reply_to(msg, "No log file found.")
                return
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            tail  = "\n".join(lines[-50:])
            bot.reply_to(msg, f"```\n{tail[-3800:]}\n```")

        # /reflection ──────────────────────────────────────────────────────────
        @bot.message_handler(commands=["reflection"])
        def cmd_reflection(msg: Message) -> None:
            if not _is_admin(msg):
                return
            log  = ReflectionLog()
            last = log.last()
            if not last:
                bot.reply_to(msg, "No reflection records yet.")
                return
            text = json.dumps(last, indent=2, ensure_ascii=False)
            bot.reply_to(msg, f"```json\n{text[:3800]}\n```")

        # /pool ────────────────────────────────────────────────────────────────
        @bot.message_handler(commands=["pool"])
        def cmd_pool(msg: Message) -> None:
            if not _is_admin(msg):
                return
            from engine.api_pool import GROQ_KEYS
            from engine.state_manager import ApiPoolState

            pool_state = ApiPoolState()
            total      = len(GROQ_KEYS)
            available  = pool_state.available_count(GROQ_KEYS)
            bot.reply_to(
                msg,
                f"*API Key Pool*\n"
                f"Total keys: `{total}`\n"
                f"Available: `{available}`\n"
                f"Cooling down: `{total - available}`",
            )

        # Fallback – ignore plain text messages silently
        @bot.message_handler(func=lambda m: True, content_types=["text"])
        def fallback(msg: Message) -> None:
            pass
