"""
api_pool.py
─────────────────────────────────────────────────────────────────────────────
Resilient multi-key Groq inference pool.

• Round-robin key rotation on every call.
• On HTTP 429 / 401 / network error: blacklist key for configurable cooldown,
  notify Telegram admin, immediately retry with next available key.
• Exposes a single synchronous `chat()` method and an async `achat()` method.
• Integrates with ApiPoolState for cross-run key cooldown persistence.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Iterator

import httpx
from groq import Groq, APIStatusError, APIConnectionError, RateLimitError

from engine.state_manager import ApiPoolState

logger = logging.getLogger("api_pool")

# ── Model priority list ───────────────────────────────────────────────────────

GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "mixtral-8x7b-32768",
    "llama-3.1-70b-versatile",
    "llama-3.1-8b-instant",
    "gemma2-9b-it",
]

# ── Load keys from env (populated from GitHub Secrets) ───────────────────────

_ENV_KEYS: list[str] = []
for i in range(1, 10):
    k = os.getenv(f"GROQ_API_KEY_{i}", "").strip()
    if k:
        _ENV_KEYS.append(k)

# Fallback: read from inline list (populated by secrets injection in workflow)
_INLINE_KEYS: list[str] = [
    v.strip()
    for v in os.getenv("GROQ_API_KEYS_CSV", "").split(",")
    if v.strip()
]

GROQ_KEYS: list[str] = _ENV_KEYS or _INLINE_KEYS

if not GROQ_KEYS:
    logger.warning("No Groq API keys found in environment – inference will fail.")


# ── Key rotator ───────────────────────────────────────────────────────────────


class KeyRotator:
    """Yields the next available API key, skipping blacklisted ones."""

    def __init__(self, keys: list[str], pool_state: ApiPoolState) -> None:
        self._keys = list(keys)
        self._pool_state = pool_state
        self._index = 0

    def __iter__(self) -> Iterator[str]:
        return self

    def __next__(self) -> str:
        attempts = 0
        while attempts < len(self._keys):
            key = self._keys[self._index % len(self._keys)]
            self._index += 1
            attempts += 1
            if self._pool_state.is_available(key):
                return key
        raise StopIteration("All API keys are currently blacklisted / cooling down.")

    def available_count(self) -> int:
        return self._pool_state.available_count(self._keys)

    def blacklist(self, key: str, cooldown: int = 300) -> None:
        logger.warning(
            "Blacklisting key %s…%s for %ds", key[:6], key[-4:], cooldown
        )
        self._pool_state.blacklist(key, cooldown)


# ── Main pool class ───────────────────────────────────────────────────────────


class GroqPool:
    """
    Thread-safe Groq inference pool with automatic key rotation,
    model fallback, and Telegram error notification hooks.
    """

    def __init__(
        self,
        keys: list[str] | None = None,
        telegram_notify_fn=None,
    ) -> None:
        self._keys = keys or GROQ_KEYS
        self._pool_state = ApiPoolState()
        self._rotator = KeyRotator(self._keys, self._pool_state)
        self._telegram_notify = telegram_notify_fn  # callable(msg: str)
        self._current_model_idx = 0

    # ── internal helpers ──────────────────────────────────────────────────────

    def _next_model(self) -> str:
        model = GROQ_MODELS[self._current_model_idx % len(GROQ_MODELS)]
        return model

    def _escalate_model(self) -> None:
        self._current_model_idx = (self._current_model_idx + 1) % len(GROQ_MODELS)

    def _notify(self, msg: str) -> None:
        if self._telegram_notify:
            try:
                self._telegram_notify(msg)
            except Exception as exc:
                logger.error("Telegram notify failed: %s", exc)
        logger.warning("[NOTIFY] %s", msg)

    # ── public synchronous API ────────────────────────────────────────────────

    def chat(
        self,
        messages: list[dict],
        max_tokens: int = 4096,
        temperature: float = 0.4,
        system: str | None = None,
        retries: int = 0,
    ) -> str:
        """
        Send a chat completion request.
        Rotates keys and models on transient failures.
        Returns the assistant message text.
        """
        all_messages = []
        if system:
            all_messages.append({"role": "system", "content": system})
        all_messages.extend(messages)

        max_attempts = len(self._keys) * len(GROQ_MODELS)
        last_error: Exception | None = None

        for attempt in range(max_attempts):
            try:
                key = next(self._rotator)
            except StopIteration:
                # All keys cooling down – wait for shortest cooldown to expire
                logger.warning("All keys blacklisted. Sleeping 60s …")
                self._notify("⚠️ All Groq API keys are cooling down. Sleeping 60s.")
                time.sleep(60)
                self._pool_state.clear_expired()
                try:
                    key = next(self._rotator)
                except StopIteration:
                    raise RuntimeError("All Groq API keys exhausted and still cooling.") from last_error

            model = self._next_model()
            client = Groq(api_key=key)

            try:
                logger.debug(
                    "Attempt %d/%d | model=%s | key=%s…%s",
                    attempt + 1,
                    max_attempts,
                    model,
                    key[:6],
                    key[-4:],
                )
                response = client.chat.completions.create(
                    model=model,
                    messages=all_messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                # Success
                content = response.choices[0].message.content or ""
                tokens_used = getattr(response.usage, "total_tokens", 0)
                logger.info(
                    "✓ Inference OK | model=%s | tokens=%d", model, tokens_used
                )
                return content

            except RateLimitError as exc:
                last_error = exc
                logger.warning("429 Rate-limit on key %s…%s", key[:6], key[-4:])
                self._notify(
                    f"🔄 Groq key {key[:6]}…{key[-4:]} hit rate-limit. Rotating…"
                )
                self._rotator.blacklist(key, cooldown=300)

            except APIStatusError as exc:
                last_error = exc
                if exc.status_code == 401:
                    logger.error("401 Unauthorized on key %s…%s", key[:6], key[-4:])
                    self._notify(
                        f"🔑 Groq key {key[:6]}…{key[-4:]} is invalid (401). Blacklisting permanently."
                    )
                    self._rotator.blacklist(key, cooldown=86400)  # 24h
                elif exc.status_code in (500, 503):
                    logger.warning("Groq server error %d – trying next model.", exc.status_code)
                    self._escalate_model()
                    time.sleep(5)
                else:
                    logger.error("Groq API error %d: %s", exc.status_code, exc)
                    self._escalate_model()

            except APIConnectionError as exc:
                last_error = exc
                logger.warning("Network error connecting to Groq: %s", exc)
                self._notify(f"🌐 Network error on Groq inference: {exc}")
                time.sleep(10)

            except Exception as exc:
                last_error = exc
                logger.error("Unexpected error during inference: %s", exc, exc_info=True)
                self._escalate_model()

        raise RuntimeError(
            f"Groq pool exhausted all {max_attempts} attempts."
        ) from last_error

    # ── streaming variant ─────────────────────────────────────────────────────

    def chat_stream(
        self,
        messages: list[dict],
        max_tokens: int = 4096,
        temperature: float = 0.4,
        system: str | None = None,
    ):
        """
        Generator that yields text chunks as they stream in.
        Falls back to non-streaming if the current model doesn't support it.
        """
        all_messages = []
        if system:
            all_messages.append({"role": "system", "content": system})
        all_messages.extend(messages)

        for attempt in range(len(self._keys)):
            try:
                key = next(self._rotator)
            except StopIteration:
                time.sleep(60)
                self._pool_state.clear_expired()
                key = next(self._rotator)

            model = self._next_model()
            client = Groq(api_key=key)

            try:
                with client.chat.completions.stream(
                    model=model,
                    messages=all_messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                ) as stream:
                    for chunk in stream:
                        delta = chunk.choices[0].delta.content
                        if delta:
                            yield delta
                return  # success

            except RateLimitError:
                self._rotator.blacklist(key, 300)
                continue
            except APIStatusError as exc:
                if exc.status_code == 401:
                    self._rotator.blacklist(key, 86400)
                continue
            except Exception as exc:
                logger.error("Stream error: %s", exc)
                continue

        # fallback: non-streaming
        yield self.chat(messages, max_tokens, temperature, system)

    # ── async variant ─────────────────────────────────────────────────────────

    async def achat(
        self,
        messages: list[dict],
        max_tokens: int = 4096,
        temperature: float = 0.4,
        system: str | None = None,
    ) -> str:
        """Async wrapper – runs sync chat() in executor to avoid blocking."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.chat(messages, max_tokens, temperature, system),
        )

    # ── utility ───────────────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "total_keys": len(self._keys),
            "available_keys": self._rotator.available_count(),
            "current_model": self._next_model(),
        }
