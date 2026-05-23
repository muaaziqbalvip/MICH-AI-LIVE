"""
main.py
─────────────────────────────────────────────────────────────────────────────
AI Software Factory – Master Orchestrator

Spiritual Anchor: رَّبِّ زِدْنِي عِلْمًا
"My Lord, increase me in knowledge." (Quran 20:114)

This prayer is logged and echoed to Telegram at the start of every cycle
as a reminder of the purpose of this system: boundless, ethical learning.

Architecture
────────────
                  ┌─────────────────────────────────────────┐
                  │           GitHub Actions Runner           │
                  │                                          │
                  │  ┌──────────────────────────────────┐   │
                  │  │    main.py  (this file)           │   │
                  │  │                                   │   │
                  │  │  SystemState ◄──► state/*.json    │   │
                  │  │       │                           │   │
                  │  │       ▼                           │   │
                  │  │  TelegramInterface ◄──► Admin     │   │
                  │  │       │                           │   │
                  │  │       ▼                           │   │
                  │  │  CrawlOrchestrator                │   │
                  │  │  SoftwareFactory                  │   │
                  │  │  QaRig                            │   │
                  │  │       │                           │   │
                  │  │       ▼                           │   │
                  │  │  [T-5h mark] commit state         │   │
                  │  │            trigger next run       │   │
                  │  └──────────────────────────────────┘   │
                  └─────────────────────────────────────────┘

Each run lasts up to ~5h 50m, then gracefully serialises state and
triggers the next run before GitHub's 6-hour hard timeout.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import os
import sys
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

SPIRITUAL_ANCHOR = "رَّبِّ زِدْنِي عِلْمًا"           # Rabbi Zidni Ilma
MAX_RUNTIME_SECONDS = 5 * 3600 + 45 * 60              # 5h 45m (15m buffer)
CYCLE_SLEEP_SECONDS = 30                               # pause between cycles
COMMIT_EVERY_N_CYCLES = 5                              # persist state every N cycles


# ── Orchestrator ──────────────────────────────────────────────────────────────

class Orchestrator:
    """
    Master state-machine orchestrator.
    Initialises all subsystems, runs the main event loop,
    handles graceful shutdown and state serialisation.
    """

    def __init__(self) -> None:
        self._start_time = time.time()
        self._cycle_tokens = 0
        self._cycle_errors = 0
        self._cycle_domains: list[str] = []
        self._cycle_tasks: list[str] = []

        logger.info("=" * 70)
        logger.info("  AI SOFTWARE FACTORY – INITIALISING")
        logger.info("  %s", SPIRITUAL_ANCHOR)
        logger.info("=" * 70)

        # State
        self._state = SystemState()
        self._reflection = ReflectionLog()

        # API pool (passes a notify callback so keys can report failures)
        self._pool = GroqPool(
            keys=GROQ_KEYS,
            telegram_notify_fn=notify,
        )

        # Telegram interface
        self._telegram = TelegramInterface(
            system_state=self._state,
            enqueue_fn=self._state.enqueue_task,
        )

        # Subsystems
        self._crawler = CrawlOrchestrator(self._pool, self._state)
        self._factory = SoftwareFactory(self._pool, self._state)
        self._qa = QaRig(self._pool)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Main entry point – start bot and enter event loop."""
        self._telegram.start()
        self._state.set_status("running")

        # Announce startup
        notify_raw(
            f"🌙 *{SPIRITUAL_ANCHOR}*\n\n"
            f"*AI Software Factory* — Cycle {self._state.cycle_count} started.\n"
            f"Run ID: `{self._state.run_id}`\n"
            f"Keys available: `{self._pool.status()['available_keys']}/{self._pool.status()['total_keys']}`"
        )
        logger.info("System online. Run ID: %s", self._state.run_id)

        try:
            self._main_loop()
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt received – shutting down gracefully.")
        except Exception as exc:
            logger.critical("Unhandled exception in main loop: %s", exc, exc_info=True)
            notify(f"💥 CRITICAL: Unhandled exception: {exc}")
        finally:
            self._shutdown()

    def _main_loop(self) -> None:
        """
        Primary event loop.
        Each iteration:
          1. Check runtime – if approaching limit, trigger handoff.
          2. Process next queued task (build / crawl).
          3. If no tasks, run autonomous crawl cycle.
          4. Periodically commit state to git.
          5. Write per-cycle reflection.
          6. Sleep.
        """
        while True:
            # ── Time check ─────────────────────────────────────────────────
            elapsed = time.time() - self._start_time
            if elapsed >= MAX_RUNTIME_SECONDS:
                logger.info("Approaching runtime limit – initiating handoff.")
                self._handoff()
                break

            # ── Pause check ─────────────────────────────────────────────────
            if self._state.status == "paused":
                logger.info("System paused. Waiting …")
                time.sleep(10)
                continue

            cycle = self._state.cycle_count
            logger.info(
                "─── Cycle %d | elapsed %.1fmin | queue=%d ───",
                cycle,
                elapsed / 60,
                len(self._state.task_queue),
            )

            # ── Spiritual anchor log ────────────────────────────────────────
            logger.info("  ✦ %s  ✦", SPIRITUAL_ANCHOR)

            self._cycle_tokens = 0
            self._cycle_errors = 0
            self._cycle_domains = []
            self._cycle_tasks = []

            # ── Task queue ─────────────────────────────────────────────────
            task = self._state.pop_task()
            if task:
                self._process_task(task)
            else:
                # No pending user tasks → run autonomous crawl
                self._run_crawl_cycle()

            # ── State persistence ──────────────────────────────────────────
            self._state.increment_cycle()
            if cycle % COMMIT_EVERY_N_CYCLES == 0:
                self._commit_state()

            # ── Reflection ─────────────────────────────────────────────────
            self._write_reflection()

            time.sleep(CYCLE_SLEEP_SECONDS)

    # ── Task dispatcher ───────────────────────────────────────────────────────

    def _process_task(self, task: dict) -> None:
        task_type = task.get("type", "unknown")
        logger.info("Processing task: type=%s id=%s", task_type, task.get("id"))
        self._cycle_tasks.append(task_type)

        try:
            if task_type == "build":
                self._run_build_task(task)
            elif task_type == "crawl":
                self._run_crawl_url_task(task)
            else:
                logger.warning("Unknown task type: %s", task_type)
        except Exception as exc:
            logger.error("Task processing failed: %s", exc, exc_info=True)
            self._cycle_errors += 1
            self._state.add_error()
            notify(f"❌ Task failed: {task_type} – {exc}")
        finally:
            self._state.complete_active_task()

    # ── Build task ────────────────────────────────────────────────────────────

    def _run_build_task(self, task: dict) -> None:
        spec = task.get("spec", "")
        chat_id = task.get("chat_id")
        job_id = task.get("id", str(uuid.uuid4())[:8])

        logger.info("[Build] Starting job %s: %s", job_id, spec[:100])
        notify(f"🏗️ Starting build | job={job_id}\n`{spec[:200]}`")

        # ── Code generation ────────────────────────────────────────────────
        try:
            zip_path = self._factory.generate(spec=spec, job_id=job_id)
        except Exception as exc:
            logger.error("[Build] Code generation failed: %s", exc, exc_info=True)
            notify(f"❌ Code generation failed for job {job_id}: {exc}")
            return

        notify(f"✅ Code generated | job={job_id} | {zip_path.name}")

        # ── QA / self-healing ──────────────────────────────────────────────
        # Extract the project directory from the zip for compilation
        import tempfile
        workspace = Path(tempfile.mkdtemp(prefix=f"qa_{job_id}_"))
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(workspace)

        # Find the project root (first subdirectory)
        subdirs = [d for d in workspace.iterdir() if d.is_dir()]
        project_dir = subdirs[0] if subdirs else workspace

        qa_result = self._qa.run(project_dir=project_dir, job_id=job_id)

        # ── Upload artifact ────────────────────────────────────────────────
        download_url = None
        tag = f"build-{job_id}"

        if qa_result["success"] and qa_result["artifact_path"]:
            artifact = qa_result["artifact_path"]
            if artifact.is_dir():
                # Re-zip the build output
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
                release_body=f"Auto-generated project.\nSpec: {spec[:300]}",
            )
        else:
            # Upload the source ZIP even if QA didn't fully pass
            download_url = upload_release_asset(
                tag_name=tag,
                asset_path=zip_path,
                release_name=f"Source {job_id} (QA partial)",
                release_body=f"Source code generated (QA not fully passed).\nSpec: {spec[:300]}",
            )

        # ── Telegram delivery ──────────────────────────────────────────────
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

        # Cleanup
        self._factory.cleanup_workspace(zip_path)

    # ── Crawl URL task ────────────────────────────────────────────────────────

    def _run_crawl_url_task(self, task: dict) -> None:
        url = task.get("url", "")
        logger.info("[Crawl] Force-crawling URL: %s", url)
        entry = self._crawler.run_url(url)
        if entry:
            notify(f"📚 Learned from: `{url[:80]}`\nTags: {entry.get('tags', [])}")
        else:
            notify(f"⚠️ Could not crawl: `{url[:80]}`")

    # ── Autonomous crawl cycle ────────────────────────────────────────────────

    def _run_crawl_cycle(self) -> None:
        try:
            domain, entries = self._crawler.run_next()
            self._cycle_domains.append(domain)
            logger.info("[Crawl] Cycle done | domain=%s | new_entries=%d", domain, len(entries))
            if entries:
                notify(
                    f"📖 Crawl cycle | domain=`{domain}` | "
                    f"+{len(entries)} entries"
                )
        except Exception as exc:
            logger.error("[Crawl] Crawl cycle failed: %s", exc, exc_info=True)
            self._cycle_errors += 1
            self._state.add_error()

    # ── Reflection ────────────────────────────────────────────────────────────

    def _write_reflection(self) -> None:
        """
        Self-audit: ask Groq to evaluate this cycle and suggest next priorities.
        Write result to reflection_log.json.
        """
        cycle = self._state.cycle_count

        # Domain stats for context
        domain_stats = []
        for d in ["android_core", "web_dev", "multimedia", "marketing", "automation"]:
            try:
                stats = DomainMatrix(d).stats()
                domain_stats.append(stats)
            except Exception:
                pass

        # Ask Groq for reflection (low temperature, concise)
        try:
            reflection_prompt = (
                f"You are an autonomous AI learning engine performing a self-audit.\n\n"
                f"Cycle number: {cycle}\n"
                f"Domains crawled this cycle: {self._cycle_domains}\n"
                f"Tasks completed: {self._cycle_tasks}\n"
                f"Errors this cycle: {self._cycle_errors}\n"
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
            "cycle": cycle,
            "run_id": self._state.run_id,
            "tokens_this_cycle": self._cycle_tokens,
            "errors_this_cycle": self._cycle_errors,
            "domains_crawled": self._cycle_domains,
            "tasks_completed": self._cycle_tasks,
            "domain_stats": domain_stats,
            "raw_notes": reflection_text,
            "next_priorities": [],
        }
        self._reflection.append(record)
        logger.info("[Reflection] Written for cycle %d.", cycle)

    # ── State commit ──────────────────────────────────────────────────────────

    def _commit_state(self) -> None:
        logger.info("[Git] Committing state …")
        try:
            commit_state(
                message=f"chore: engine state – cycle {self._state.cycle_count}",
            )
        except Exception as exc:
            logger.error("[Git] State commit failed: %s", exc)

    # ── Graceful handoff ──────────────────────────────────────────────────────

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
                "cycle": self._state.cycle_count,
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

    # ── Shutdown ──────────────────────────────────────────────────────────────

    def _shutdown(self) -> None:
        logger.info("Shutting down …")
        self._state.set_status("stopped")
        self._telegram.stop()
        logger.info("Shutdown complete.")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    orchestrator = Orchestrator()
    orchestrator.run()
