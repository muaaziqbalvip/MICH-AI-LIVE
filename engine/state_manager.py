"""
state_manager.py
─────────────────────────────────────────────────────────────────────────────
Persistent state layer for the AI Software Factory.
All state is serialised to JSON files that are committed back to the repo
so the next GitHub-Actions run can resume exactly where this one left off.

Domains tracked:
  android_core.json    – Android / Kotlin / Flutter knowledge vectors
  web_dev.json         – Full-stack web knowledge vectors
  multimedia.json      – FFmpeg / MoviePy automation vectors
  marketing.json       – SEO / ad-copy knowledge vectors
  automation.json      – Scripting / infra automation vectors
  system_state.json    – Master orchestrator state (queue, cycle counters …)
  reflection_log.json  – Per-cycle self-audit entries
  api_pool_state.json  – Key cool-down tracking
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── paths ────────────────────────────────────────────────────────────────────

STATE_DIR = Path(os.getenv("STATE_DIR", "state"))
STATE_DIR.mkdir(parents=True, exist_ok=True)

DOMAIN_FILES = {
    "android_core": STATE_DIR / "android_core.json",
    "web_dev":      STATE_DIR / "web_dev.json",
    "multimedia":   STATE_DIR / "multimedia.json",
    "marketing":    STATE_DIR / "marketing.json",
    "automation":   STATE_DIR / "automation.json",
    "system":       STATE_DIR / "system_state.json",
    "reflection":   STATE_DIR / "reflection_log.json",
    "api_pool":     STATE_DIR / "api_pool_state.json",
}

# ── helpers ───────────────────────────────────────────────────────────────────


def _load(path: Path, default: Any = None) -> Any:
    """Load JSON file; return *default* on any read / parse error."""
    if default is None:
        default = {}
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
    except (json.JSONDecodeError, OSError):
        pass
    return default


def _save(path: Path, data: Any) -> None:
    """Atomically write JSON to *path*."""
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False, default=str)
    tmp.replace(path)


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── System state ──────────────────────────────────────────────────────────────

_SYSTEM_DEFAULTS: dict[str, Any] = {
    "run_id": "",
    "cycle_count": 0,
    "started_at": "",
    "last_checkpoint": "",
    "task_queue": [],          # list[dict]  pending user / crawl tasks
    "active_task": None,
    "crawl_index": {},         # domain -> list of already-visited URLs
    "software_factory_jobs": [],
    "total_tokens_used": 0,
    "errors_total": 0,
    "status": "idle",
}


class SystemState:
    """Singleton wrapper around system_state.json."""

    def __init__(self) -> None:
        raw = _load(DOMAIN_FILES["system"])
        self._data: dict[str, Any] = {**_SYSTEM_DEFAULTS, **raw}

        # Always start a new run_id for this process
        if not self._data["run_id"]:
            self._data["run_id"] = str(uuid.uuid4())[:8]
        if not self._data["started_at"]:
            self._data["started_at"] = _ts()

    # ── accessors ────────────────────────────────────────────────────────────

    @property
    def cycle_count(self) -> int:
        return self._data["cycle_count"]

    @property
    def run_id(self) -> str:
        return self._data["run_id"]

    @property
    def task_queue(self) -> list:
        return self._data["task_queue"]

    @property
    def active_task(self) -> dict | None:
        return self._data["active_task"]

    @property
    def status(self) -> str:
        return self._data["status"]

    @property
    def total_tokens(self) -> int:
        return self._data["total_tokens_used"]

    # ── mutators ─────────────────────────────────────────────────────────────

    def increment_cycle(self) -> None:
        self._data["cycle_count"] += 1
        self._data["last_checkpoint"] = _ts()
        self.persist()

    def set_status(self, status: str) -> None:
        self._data["status"] = status
        self.persist()

    def enqueue_task(self, task: dict) -> None:
        task.setdefault("id", str(uuid.uuid4())[:8])
        task.setdefault("enqueued_at", _ts())
        self._data["task_queue"].append(task)
        self.persist()

    def pop_task(self) -> dict | None:
        if not self._data["task_queue"]:
            return None
        task = self._data["task_queue"].pop(0)
        self._data["active_task"] = task
        self.persist()
        return task

    def complete_active_task(self) -> None:
        self._data["active_task"] = None
        self.persist()

    def mark_url_visited(self, domain: str, url: str) -> None:
        self._data["crawl_index"].setdefault(domain, [])
        if url not in self._data["crawl_index"][domain]:
            self._data["crawl_index"][domain].append(url)
        self.persist()

    def is_url_visited(self, domain: str, url: str) -> bool:
        return url in self._data["crawl_index"].get(domain, [])

    def add_tokens(self, n: int) -> None:
        self._data["total_tokens_used"] += n

    def add_error(self) -> None:
        self._data["errors_total"] += 1

    def add_factory_job(self, job: dict) -> None:
        self._data["software_factory_jobs"].append(job)
        self.persist()

    def persist(self) -> None:
        _save(DOMAIN_FILES["system"], self._data)

    def full_snapshot(self) -> dict:
        return dict(self._data)


# ── Domain knowledge matrices ─────────────────────────────────────────────────


class DomainMatrix:
    """
    Manages a single domain knowledge file.
    Structure:
    {
      "domain": "<name>",
      "last_updated": "<iso>",
      "entries": [
        { "id": "…", "source_url": "…", "title": "…",
          "summary": "…", "tags": [], "added_at": "…" }
      ]
    }
    """

    def __init__(self, domain: str) -> None:
        if domain not in DOMAIN_FILES:
            raise ValueError(f"Unknown domain: {domain}")
        self.domain = domain
        self._path = DOMAIN_FILES[domain]
        raw = _load(self._path, {"domain": domain, "last_updated": _ts(), "entries": []})
        self._data: dict[str, Any] = raw
        self._data.setdefault("entries", [])

    @property
    def entries(self) -> list[dict]:
        return self._data["entries"]

    def add_entry(
        self,
        source_url: str,
        title: str,
        summary: str,
        tags: list[str] | None = None,
    ) -> dict:
        entry = {
            "id": str(uuid.uuid4())[:8],
            "source_url": source_url,
            "title": title,
            "summary": summary,
            "tags": tags or [],
            "added_at": _ts(),
        }
        self._data["entries"].append(entry)
        self._data["last_updated"] = _ts()
        self.persist()
        return entry

    def search(self, keyword: str) -> list[dict]:
        kw = keyword.lower()
        return [
            e for e in self._data["entries"]
            if kw in e.get("title", "").lower()
            or kw in e.get("summary", "").lower()
            or any(kw in t.lower() for t in e.get("tags", []))
        ]

    def persist(self) -> None:
        _save(self._path, self._data)

    def stats(self) -> dict:
        return {
            "domain": self.domain,
            "entry_count": len(self._data["entries"]),
            "last_updated": self._data.get("last_updated"),
        }


# ── Reflection log ────────────────────────────────────────────────────────────


class ReflectionLog:
    """
    Appends per-cycle self-audit records to reflection_log.json.
    Each record:
    {
      "cycle": <int>,
      "run_id": "<str>",
      "timestamp": "<iso>",
      "tokens_this_cycle": <int>,
      "errors_this_cycle": <int>,
      "domains_crawled": [],
      "tasks_completed": [],
      "learned_summaries": [],
      "next_priorities": [],
      "raw_notes": "<str>"
    }
    """

    def __init__(self) -> None:
        self._path = DOMAIN_FILES["reflection"]
        raw = _load(self._path, {"records": []})
        self._data: dict = raw
        self._data.setdefault("records", [])

    def append(self, record: dict) -> None:
        record.setdefault("timestamp", _ts())
        self._data["records"].append(record)
        _save(self._path, self._data)

    def last(self) -> dict | None:
        if self._data["records"]:
            return self._data["records"][-1]
        return None

    def all_records(self) -> list[dict]:
        return self._data["records"]


# ── API pool state ────────────────────────────────────────────────────────────


class ApiPoolState:
    """
    Tracks per-key cooldown periods so the rotator knows which keys are live.
    Structure: { "<key_prefix>": { "blacklisted_until": <epoch_float> | 0 } }
    """

    def __init__(self) -> None:
        self._path = DOMAIN_FILES["api_pool"]
        raw = _load(self._path, {})
        self._data: dict[str, dict] = raw

    def _key_id(self, key: str) -> str:
        """Use first+last 4 chars as a non-sensitive identifier."""
        return f"{key[:4]}…{key[-4:]}"

    def blacklist(self, key: str, cooldown_seconds: int = 300) -> None:
        kid = self._key_id(key)
        self._data[kid] = {"blacklisted_until": time.time() + cooldown_seconds}
        _save(self._path, self._data)

    def is_available(self, key: str) -> bool:
        kid = self._key_id(key)
        if kid not in self._data:
            return True
        return time.time() > self._data[kid].get("blacklisted_until", 0)

    def clear_expired(self) -> None:
        now = time.time()
        self._data = {
            k: v for k, v in self._data.items()
            if v.get("blacklisted_until", 0) > now
        }
        _save(self._path, self._data)

    def available_count(self, keys: list[str]) -> int:
        return sum(1 for k in keys if self.is_available(k))
