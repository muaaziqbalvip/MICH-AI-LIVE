"""
main.py
─────────────────────────────────────────────────────────────────────────────
AI Software Factory – Master Orchestrator

Spiritual Anchor: رَّبِّ زِدْنِي عِلْمًا
"My Lord, increase me in knowledge." (Quran 20:114)

This prayer is logged and echoed to Telegram at the start of every cycle
as a reminder of the purpose of this system: boundless, ethical learning.

Architecture (v2 – Threaded)
────────────────────────────
                  ┌─────────────────────────────────────────┐
                  │           GitHub Actions Runner           │
                  │                                          │
                  │  ┌──────────────────────────────────┐   │
                  │  │    main.py  (this file)           │   │
                  │  │                                   │   │
                  │  │  SystemState ◄──► state/*.json    │   │
                  │  │       │                           │   │
                  │  │       ├──────────────┐            │   │
                  │  │       ▼              ▼            │   │
                  │  │  Thread-A          Thread-B       │   │
                  │  │  TelegramInterface CrawlLoop      │   │
                  │  │  (user tasks)      (autonomous)   │   │
                  │  │       │              │            │   │
                  │  │   is_user_active ◄──┘            │   │
                  │  │  (threading.Event)                │   │
                  │  │       │                           │   │
                  │  │  SoftwareFactory / QaRig          │   │
                  │  │       │                           │   │
                  │  │       ▼                           │   │
                  │  │  [T-5h mark] commit state         │   │
                  │  │            trigger next run       │   │
                  │  └──────────────────────────────────┘   │
                  └─────────────────────────────────────────┘

Threading design
─────────────────
• Thread-A (telegram_thread): runs the Telegram bot polling loop. When
  a user task arrives it sets `_user_active` Event and routes the task
  to `_process_task()`, then clears the Event on completion.

• Thread-B (crawl_thread): runs the autonomous crawl cycle. At the top
  of every iteration it checks `_user_active`; if set it sleeps in
  short bursts until the user task is done, giving 100% CPU priority to
  the user's request.

• Main thread: runtime / handoff watchdog only.

Smart-pause contract
─────────────────────
  User sends prompt → set _user_active → crawl thread sees it → pauses
  → Task completes → clear _user_active → crawl resumes.

Each run lasts up to ~5h 50m, then gracefully serialises state and
triggers the next run before GitHub's 6-hour hard timeout.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

# ── Logging setup (file + console) ───────────────────────────────────────────

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=LOG_DATEFMT,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("factory.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")

# ── Local imports (after logging is configured) ───────────────────────────────

from engine.state_manager import SystemState, ReflectionLog, DomainMatrix
from engine.api_pool import GroqPool, GROQ_KEYS
from engine.telegram_bot import TelegramInterface, notify, notify_raw
from engine.crawler import CrawlOrchestrator
from engine.codegen import SoftwareFactory
from engine.qa_rig import QaRig
from engine.git_ops import commit_state, trigger_next_run, upload_release_asset

# ── Constants ─────────────────────────────────────────────────────────────────

SPIRITUAL_ANCHOR      = "رَّبِّ زِدْنِي عِلْمًا"   # Rabbi Zidni Ilma
MAX_RUNTIME_SECONDS   = 5 * 3600 + 45 * 60          # 5h 45m (15m buffer)
CYCLE_SLEEP_SECONDS   = 30                            # pause between crawl cycles
COMMIT_EVERY_N_CYCLES = 5                             # persist state every N cycles
USER_PAUSE_POLL_SEC   = 2                             # how often crawl checks the flag

# Live animation step sequence shown during user task processing
ANIM_STEPS = ["⏳", "⚙️", "🧠", "🚀"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _send_typing(bot, chat_id: int | str | None) -> None:
    """Fire-and-forget typing indicator; silently ignores errors."""
    if chat_id is None:
        return
    try:
        bot.send_chat_action(chat_id, "typing")
    except Exception:
        pass


def _anim_notify(chat_id: int | str | None, label: str, step_idx: int) -> None:
    """
    Send a short animated step message to the user via Telegram.
    Uses the global notify() so it respects the existing notification pipeline.

    step_idx wraps around ANIM_STEPS length automatically.
    """
    emoji = ANIM_STEPS[step_idx % len(ANIM_STEPS)]
    try:
        notify(f"{emoji} *{label}*", chat_id=chat_id)
    except Exception:
        # notify() may not accept chat_id kwarg in all implementations;
        # fall back to broadcast.
        try:
            notify(f"{emoji} *{label}*")
        except Exception:
            pass


# ── Orchestrator ──────────────────────────────────────────────────────────────

class Orchestrator:
    """
    Master state-machine orchestrator (v2 – threaded).

    Two background threads are started inside run():
      • _telegram_thread  – Telegram bot + user-task dispatcher
      • _crawl_thread     – Autonomous crawl loop

    A threading.Event (_user_active) signals when a user task is running
    so the crawl loop can yield priority.
    """

    def __init__(self) -> None:
        self._start_time   = time.time()
        self._cycle_tokens = 0
        self._cycle_errors = 0
        self._cycle_domains: list[str] = []
        self._cycle_tasks:   list[str] = []

        # ── Thread-safety primitives ──────────────────────────────────────
        # Set while a user task is being processed; crawl thread waits on it.
        self._user_active  = threading.Event()
        # Cleared when the whole system should shut down.
        self._running      = threading.Event()
        self._running.set()
        # Protects shared cycle counters written from both threads.
        self._stats_lock   = threading.Lock()

        logger.info("=" * 70)
        logger.info("  AI SOFTWARE FACTORY – INITIALISING (threaded v2)")
        logger.info("  %s", SPIRITUAL_ANCHOR)
        logger.info("=" * 70)

        # ── State ─────────────────────────────────────────────────────────
        self._state      = SystemState()
        self._reflection = ReflectionLog()

        # ── API pool ──────────────────────────────────────────────────────
        self._pool = GroqPool(
            keys=GROQ_KEYS,
            telegram_notify_fn=notify,
        )

        # ── Telegram interface ────────────────────────────────────────────
        self._telegram = TelegramInterface(
            system_state=self._state,
            enqueue_fn=self._state.enqueue_task,
        )
        # Expose the underlying bot object for chat_action calls if available.
        self._bot = getattr(self._telegram, "bot", None)

        # ── Subsystems ────────────────────────────────────────────────────
        self._crawler = CrawlOrchestrator(self._pool, self._state)
        self._factory = SoftwareFactory(self._pool, self._state)
        self._qa      = QaRig(self._pool)

    # ─────────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Main entry point – start threads and run the watchdog loop."""
        self._telegram.start()
        self._state.set_status("running")

        # Announce startup
        notify_raw(
            f"🌙 *{SPIRITUAL_ANCHOR}*\n\n"
            f"*AI Software Factory* — Cycle {self._state.cycle_count} started.\n"
            f"Run ID: `{self._state.run_id}`\n"
            f"Keys available: `{self._pool.status()['available_keys']}/{self._pool.status()['total_keys']}`\n"
            f"⚙️ Threaded mode active — bot & crawler run concurrently."
        )
        logger.info("System online. Run ID: %s", self._state.run_id)

        # ── Start dedicated threads ───────────────────────────────────────
        telegram_thread = threading.Thread(
            target=self._telegram_worker,
            name="telegram_thread",
            daemon=True,
        )
        crawl_thread = threading.Thread(
            target=self._crawl_worker,
            name="crawl_thread",
            daemon=True,
        )

        telegram_thread.start()
        crawl_thread.start()
        logger.info("[Main] telegram_thread and crawl_thread started.")

        # ── Watchdog loop (main thread – runtime limit only) ──────────────
        try:
            while self._running.is_set():
                elapsed = time.time() - self._start_time
                if elapsed >= MAX_RUNTIME_SECONDS:
                    logger.info("Approaching runtime limit – initiating handoff.")
                    self._running.clear()   # signal threads to stop
                    self._handoff()
                    break
                time.sleep(15)             # watchdog polls every 15 s
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt received – shutting down gracefully.")
            self._running.clear()
        except Exception as exc:
            logger.critical(
                "Unhandled exception in watchdog loop: %s", exc, exc_info=True
            )
            notify(f"💥 CRITICAL: Unhandled exception: {exc}")
            self._running.clear()
        finally:
            self._shutdown()
            telegram_thread.join(timeout=10)
            crawl_thread.join(timeout=10)

    # ─────────────────────────────────────────────────────────────────────────
    # Thread-A  –  Telegram / user-task worker
    # ─────────────────────────────────────────────────────────────────────────

    def _telegram_worker(self) -> None:
        """
        Dedicated thread for processing user-submitted tasks from the queue.

        Polls self._state.task_queue continuously.  When a task arrives:
          1. Sets _user_active  → crawl thread yields.
          2. Processes the task with live animation feedback.
          3. Clears _user_active → crawl thread resumes.
        """
        logger.info("[TelegramWorker] Thread started.")
        while self._running.is_set():
            task = self._state.pop_task()
            if task:
                # ── Signal: user task starting ────────────────────────────
                self._user_active.set()
                logger.info(
                    "[TelegramWorker] User task acquired (type=%s) – crawler paused.",
                    task.get("type"),
                )
                try:
                    self._process_task(task)
                finally:
                    # ── Signal: user task done ────────────────────────────
                    self._user_active.clear()
                    logger.info("[TelegramWorker] User task done – crawler resuming.")
            else:
                # No task – yield briefly so this thread doesn't spin-burn.
                time.sleep(1)

        logger.info("[TelegramWorker] Thread exiting.")

    # ─────────────────────────────────────────────────────────────────────────
    # Thread-B  –  Autonomous crawl worker
    # ─────────────────────────────────────────────────────────────────────────

    def _crawl_worker(self) -> None:
        """
        Dedicated thread for the autonomous crawl loop.

        At the top of every cycle it checks _user_active.  While set,
        it sleeps in short bursts (USER_PAUSE_POLL_SEC) and logs once,
        yielding CPU to the user's build task.

        When _running is cleared (handoff or shutdown) the loop exits.
        """
        logger.info("[CrawlWorker] Thread started.")

        while self._running.is_set():
            elapsed = time.time() - self._start_time

            # ── Smart pause: yield to user task ──────────────────────────
            if self._user_active.is_set():
                logger.info("[CrawlWorker] User task active – pausing crawl cycle …")
                while self._user_active.is_set() and self._running.is_set():
                    time.sleep(USER_PAUSE_POLL_SEC)
                logger.info("[CrawlWorker] User task complete – resuming crawl.")
                continue   # restart loop (re-checks _running, elapsed, etc.)

            # ── Pause command from state ──────────────────────────────────
            if self._state.status == "paused":
                logger.info("[CrawlWorker] System paused. Waiting …")
                time.sleep(10)
                continue

            cycle = self._state.cycle_count
            logger.info(
                "[CrawlWorker] ─── Cycle %d | elapsed %.1fmin ───",
                cycle,
                elapsed / 60,
            )
            logger.info("[CrawlWorker]   ✦ %s  ✦", SPIRITUAL_ANCHOR)

            # Reset per-cycle counters (thread-safe)
            with self._stats_lock:
                self._cycle_tokens  = 0
                self._cycle_errors  = 0
                self._cycle_domains = []
                self._cycle_tasks   = []

            # ── Autonomous crawl ──────────────────────────────────────────
            self._run_crawl_cycle()

            # ── State persistence ─────────────────────────────────────────
            self._state.increment_cycle()
            if cycle % COMMIT_EVERY_N_CYCLES == 0:
                self._commit_state()

            # ── Per-cycle reflection ──────────────────────────────────────
            self._write_reflection()

            # ── Sleep between cycles ──────────────────────────────────────
            # Break the sleep into short chunks so we can react to
            # _user_active or _running being cleared promptly.
            slept = 0
            while slept < CYCLE_SLEEP_SECONDS and self._running.is_set():
                if self._user_active.is_set():
                    break   # user task came in – skip remaining sleep
                time.sleep(min(USER_PAUSE_POLL_SEC, CYCLE_SLEEP_SECONDS - slept))
                slept += USER_PAUSE_POLL_SEC

        logger.info("[CrawlWorker] Thread exiting.")

    # ─────────────────────────────────────────────────────────────────────────
    # Task dispatcher
    # ─────────────────────────────────────────────────────────────────────────

    def _process_task(self, task: dict) -> None:
        task_type = task.get("type", "unknown")
        chat_id   = task.get("chat_id")
        logger.info(
            "Processing task: type=%s id=%s", task_type, task.get("id")
        )

        with self._stats_lock:
            self._cycle_tasks.append(task_type)

        # ── Live typing indicator ─────────────────────────────────────────
        _send_typing(self._bot, chat_id)

        try:
            if task_type == "build":
                self._run_build_task(task)
            elif task_type == "crawl":
                self._run_crawl_url_task(task)
            else:
                logger.warning("Unknown task type: %s", task_type)
        except Exception as exc:
            logger.error(
                "Task processing failed: %s", exc, exc_info=True
            )
            with self._stats_lock:
                self._cycle_errors += 1
            self._state.add_error()
            notify(f"❌ Task failed: {task_type} – {exc}")
        finally:
            self._state.complete_active_task()

    # ─────────────────────────────────────────────────────────────────────────
    # Build task  (with live animation)
    # ─────────────────────────────────────────────────────────────────────────

    def _run_build_task(self, task: dict) -> None:
        spec    = task.get("spec", "")
        chat_id = task.get("chat_id")
        job_id  = task.get("id", str(uuid.uuid4())[:8])

        logger.info("[Build] Starting job %s: %s", job_id, spec[:100])

        # ── Step 0: announce ──────────────────────────────────────────────
        _send_typing(self._bot, chat_id)
        notify(f"⏳ *Starting build* | job=`{job_id}`\n`{spec[:200]}`")

        # ── Step 1: code generation ───────────────────────────────────────
        _send_typing(self._bot, chat_id)
        _anim_notify(chat_id, "Generating code …", 1)

        try:
            zip_path = self._factory.generate(spec=spec, job_id=job_id)
        except Exception as exc:
            logger.error(
                "[Build] Code generation failed: %s", exc, exc_info=True
            )
            notify(f"❌ Code generation failed for job {job_id}: {exc}")
            return

        _anim_notify(chat_id, "Code generated — running QA …", 2)

        # ── Step 2: QA / self-healing ─────────────────────────────────────
        import tempfile
        workspace = Path(tempfile.mkdtemp(prefix=f"qa_{job_id}_"))
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(workspace)

        subdirs     = [d for d in workspace.iterdir() if d.is_dir()]
        project_dir = subdirs[0] if subdirs else workspace

        _send_typing(self._bot, chat_id)
        qa_result = self._qa.run(project_dir=project_dir, job_id=job_id)

        # ── Step 3: upload artifact ───────────────────────────────────────
        _anim_notify(chat_id, "Uploading artifact …", 3)

        download_url = None
        tag = f"build-{job_id}"

        if qa_result["success"] and qa_result["artifact_path"]:
            artifact = qa_result["artifact_path"]
            if artifact.is_dir():
                build_zip = workspace / f"build_{job_id}.zip"
                with zipfile.ZipFile(build_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                    for f in artifact.rglob("*"):
                        if f.is_file():
                            zf.write(f, f.relative_to(workspace))
                artifact = build_zip

            download_url = upload_release_asset(
                tag_name=tag,
                asset_path=artifact,
                release_name=f"Build {job_id}",
                release_body=(
                    f"Auto-generated project.\nSpec: {spec[:300]}"
                ),
            )
        else:
            download_url = upload_release_asset(
                tag_name=tag,
                asset_path=zip_path,
                release_name=f"Source {job_id} (QA partial)",
                release_body=(
                    f"Source code generated (QA not fully passed).\n"
                    f"Spec: {spec[:300]}"
                ),
            )

        # ── Step 4: final delivery message ────────────────────────────────
        status_emoji = "✅" if qa_result["success"] else "⚠️"
        msg = (
            f"{status_emoji} *Build Complete* | job=`{job_id}`\n\n"
            f"*Spec:* {spec[:150]}\n"
            f"*QA:* {'PASSED' if qa_result['success'] else 'PARTIAL'} "
            f"({qa_result['iterations']} iterations, "
            f"{qa_result['errors_fixed']} errors fixed)\n"
        )
        if download_url:
            msg += f"\n📦 *Download:* {download_url}"
        else:
            msg += "\n📦 Artifact not available (upload failed)."

        notify_raw(msg)
        logger.info("[Build] Delivery complete for job %s", job_id)

        self._factory.cleanup_workspace(zip_path)

    # ─────────────────────────────────────────────────────────────────────────
    # Crawl URL task
    # ─────────────────────────────────────────────────────────────────────────

    def _run_crawl_url_task(self, task: dict) -> None:
        url     = task.get("url", "")
        chat_id = task.get("chat_id")

        logger.info("[Crawl] Force-crawling URL: %s", url)
        _send_typing(self._bot, chat_id)
        _anim_notify(chat_id, f"Crawling `{url[:60]}` …", 1)

        entry = self._crawler.run_url(url)
        if entry:
            notify(
                f"📚 Learned from: `{url[:80]}`\n"
                f"Tags: {entry.get('tags', [])}"
            )
        else:
            notify(f"⚠️ Could not crawl: `{url[:80]}`")

    # ─────────────────────────────────────────────────────────────────────────
    # Autonomous crawl cycle  (called from crawl_thread)
    # ─────────────────────────────────────────────────────────────────────────

    def _run_crawl_cycle(self) -> None:
        try:
            domain, entries = self._crawler.run_next()
            with self._stats_lock:
                self._cycle_domains.append(domain)
            logger.info(
                "[Crawl] Cycle done | domain=%s | new_entries=%d",
                domain,
                len(entries),
            )
            if entries:
                notify(
                    f"📖 Crawl cycle | domain=`{domain}` | "
                    f"+{len(entries)} entries"
                )
        except Exception as exc:
            logger.error(
                "[Crawl] Crawl cycle failed: %s", exc, exc_info=True
            )
            with self._stats_lock:
                self._cycle_errors += 1
            self._state.add_error()

    # ─────────────────────────────────────────────────────────────────────────
    # Reflection  (untouched logic, called from crawl_thread)
    # ─────────────────────────────────────────────────────────────────────────

    def _write_reflection(self) -> None:
        """
        Self-audit: ask Groq to evaluate this cycle and suggest next priorities.
        Write result to reflection_log.json.
        """
        cycle = self._state.cycle_count

        domain_stats = []
        for d in [
            "android_core", "web_dev", "multimedia", "marketing", "automation"
        ]:
            try:
                stats = DomainMatrix(d).stats()
                domain_stats.append(stats)
            except Exception:
                pass

        # Snapshot counters under lock for the prompt
        with self._stats_lock:
            snap_domains = list(self._cycle_domains)
            snap_tasks   = list(self._cycle_tasks)
            snap_errors  = self._cycle_errors

        try:
            reflection_prompt = (
                f"You are an autonomous AI learning engine performing a self-audit.\n\n"
                f"Cycle number: {cycle}\n"
                f"Domains crawled this cycle: {snap_domains}\n"
                f"Tasks completed: {snap_tasks}\n"
                f"Errors this cycle: {snap_errors}\n"
                f"Domain knowledge stats: {domain_stats}\n\n"
                f"Perform a concise self-reflection. Identify:\n"
                f"1. What was learned this cycle (bullet points)\n"
                f"2. Any recurring error patterns\n"
                f"3. Top 3 knowledge domains to prioritise next cycle\n"
                f"4. One specific improvement to make\n\n"
                f"Be concise. 150 words maximum."
            )
            reflection_text = self._pool.chat(
                messages=[{"role": "user", "content": reflection_prompt}],
                max_tokens=300,
                temperature=0.3,
            )
        except Exception as exc:
            reflection_text = f"Reflection failed: {exc}"
            logger.warning("[Reflection] Groq call failed: %s", exc)

        record = {
            "cycle":             cycle,
            "run_id":            self._state.run_id,
            "tokens_this_cycle": self._cycle_tokens,
            "errors_this_cycle": snap_errors,
            "domains_crawled":   snap_domains,
            "tasks_completed":   snap_tasks,
            "domain_stats":      domain_stats,
            "raw_notes":         reflection_text,
            "next_priorities":   [],
        }
        self._reflection.append(record)
        logger.info("[Reflection] Written for cycle %d.", cycle)

    # ─────────────────────────────────────────────────────────────────────────
    # State commit  (untouched)
    # ─────────────────────────────────────────────────────────────────────────

    def _commit_state(self) -> None:
        logger.info("[Git] Committing state …")
        try:
            commit_state(
                message=f"chore: engine state – cycle {self._state.cycle_count}",
            )
        except Exception as exc:
            logger.error("[Git] State commit failed: %s", exc)

    # ─────────────────────────────────────────────────────────────────────────
    # Graceful handoff  (untouched logic)
    # ─────────────────────────────────────────────────────────────────────────

    def _handoff(self) -> None:
        """
        Commit all state, then trigger the next GitHub Actions run.
        Called when runtime is approaching the 6-hour GitHub limit.
        """
        logger.info("═" * 60)
        logger.info("  HANDOFF: serialising state and triggering next run")
        logger.info("═" * 60)

        self._state.set_status("handoff")
        notify(
            f"🔄 *Handoff initiated* | cycle={self._state.cycle_count}\n"
            f"Committing state and triggering next run …"
        )

        # Final reflection
        self._write_reflection()

        # Commit everything
        self._commit_state()

        # Trigger the next run
        success = trigger_next_run(
            event_type="engine_loop",
            client_payload={
                "cycle":  self._state.cycle_count,
                "run_id": self._state.run_id,
            },
        )
        if success:
            notify("✅ Next run triggered successfully. Going offline …")
        else:
            notify(
                "⚠️ Repository dispatch failed. "
                "The scheduled CRON trigger will restart the engine."
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Shutdown
    # ─────────────────────────────────────────────────────────────────────────

    def _shutdown(self) -> None:
        logger.info("Shutting down …")
        self._state.set_status("stopped")
        self._telegram.stop()
        logger.info("Shutdown complete.")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    orchestrator = Orchestrator()
    orchestrator.run()
