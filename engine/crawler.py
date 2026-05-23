"""
crawler.py
─────────────────────────────────────────────────────────────────────────────
Omni-channel web learning engine.

Architecture
────────────
• DomainCrawler: given a seed list of topic queries / URLs, discovers pages,
  scrapes clean text, asks the Groq pool to summarise each page, and stores
  the condensed knowledge vectors in the appropriate DomainMatrix.

• Four domain crawlers run in rotation:
    android_core  – Android, Kotlin, Jetpack Compose, Flutter
    web_dev       – React, Next.js, FastAPI, TypeScript, cloud
    multimedia    – FFmpeg, MoviePy, Python video automation
    marketing     – SEO, Google Ads, social media, keyword research
    automation    – GitHub Actions, Bash scripting, CI/CD pipelines

• Each crawl cycle:
  1. Pull top-5 DuckDuckGo search results for each seed query.
  2. Filter out already-visited URLs (via SystemState.crawl_index).
  3. Fetch page HTML → extract clean text with BeautifulSoup.
  4. Chunk text into ≤3000-char segments.
  5. Summarise each chunk with Groq (concise bullet-point notes).
  6. Persist to DomainMatrix + mark URL as visited.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Generator

import requests
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS

from engine.api_pool import GroqPool
from engine.state_manager import DomainMatrix, SystemState

logger = logging.getLogger("crawler")

# ── Seed queries per domain ───────────────────────────────────────────────────

DOMAIN_SEEDS: dict[str, list[str]] = {
    "android_core": [
        "Android Jetpack Compose tutorial 2024",
        "Kotlin coroutines advanced patterns",
        "Android multi-module architecture best practices",
        "Flutter state management Riverpod 2024",
        "Android performance optimization techniques",
        "Kotlin multiplatform mobile tutorial",
        "Android Room database advanced usage",
        "Compose navigation with Hilt injection",
    ],
    "web_dev": [
        "Next.js 14 App Router complete guide",
        "React Server Components patterns 2024",
        "FastAPI async Python REST API tutorial",
        "TypeScript advanced types and generics",
        "Tailwind CSS advanced component patterns",
        "Prisma ORM with PostgreSQL tutorial",
        "Docker compose full-stack deployment",
        "WebSockets real-time application Python",
    ],
    "multimedia": [
        "FFmpeg Python automation video processing",
        "MoviePy advanced video editing tutorial",
        "Python automated YouTube video creation",
        "FFmpeg filter complex tutorial examples",
        "PIL Pillow image batch processing Python",
        "Python text to speech TTS automation",
        "OpenCV video manipulation Python",
        "ffmpeg-python library advanced usage",
    ],
    "marketing": [
        "SEO keyword research automation Python 2024",
        "Google Ads API Python automation tutorial",
        "Social media scheduling automation tools",
        "Content marketing AI generation strategy",
        "Python scraping Google SERP results",
        "Facebook Ads API automation Python",
        "Email marketing automation Python",
        "Influencer marketing analytics tools",
    ],
    "automation": [
        "GitHub Actions advanced workflow patterns",
        "Python subprocess automation bash scripts",
        "Selenium web automation advanced tutorial",
        "Playwright Python browser automation",
        "Cron job scheduling Python Linux",
        "Git automation Python GitPython library",
        "AWS Lambda Python automation tutorial",
        "Terraform infrastructure automation Python",
    ],
}

# ── HTTP helpers ──────────────────────────────────────────────────────────────

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
_REQUEST_TIMEOUT = 15
_MAX_PAGE_CHARS = 20_000   # truncate very large pages
_CHUNK_SIZE = 3_000         # chars per summarisation chunk


def _fetch_page(url: str) -> str | None:
    """Fetch a URL and return clean plain text (or None on failure)."""
    try:
        resp = requests.get(
            url,
            headers=_HEADERS,
            timeout=_REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        if "html" not in ct and "text" not in ct:
            return None

        soup = BeautifulSoup(resp.text, "lxml")
        # Remove navigation/script/style clutter
        for tag in soup(["script", "style", "nav", "footer", "header", "aside",
                          "iframe", "noscript", "form"]):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)
        # Collapse excessive whitespace
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r" {2,}", " ", text)
        return text[:_MAX_PAGE_CHARS]

    except Exception as exc:
        logger.debug("Fetch failed for %s: %s", url, exc)
        return None


def _search_ddg(query: str, max_results: int = 5) -> list[dict]:
    """Return list of {title, href, body} dicts from DuckDuckGo."""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        return results
    except Exception as exc:
        logger.warning("DuckDuckGo search failed for '%s': %s", query, exc)
        return []


def _chunk_text(text: str, size: int = _CHUNK_SIZE) -> Generator[str, None, None]:
    """Split text into overlapping chunks of ~size chars."""
    start = 0
    while start < len(text):
        yield text[start: start + size]
        start += size - 200  # 200-char overlap for context continuity
        if start >= len(text):
            break


def _url_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


# ── Domain crawler ────────────────────────────────────────────────────────────


class DomainCrawler:
    """
    Crawls one knowledge domain per cycle.
    """

    def __init__(
        self,
        domain: str,
        groq_pool: GroqPool,
        system_state: SystemState,
    ) -> None:
        if domain not in DOMAIN_SEEDS:
            raise ValueError(f"Unknown crawl domain: {domain}")
        self.domain = domain
        self._pool = groq_pool
        self._state = system_state
        self._matrix = DomainMatrix(domain)

    def run_cycle(self, queries_per_cycle: int = 2) -> list[dict]:
        """
        Run one crawl cycle for this domain.
        Returns list of newly added knowledge entries.
        """
        seeds = DOMAIN_SEEDS[self.domain]
        # Rotate seeds so we cover all of them across cycles
        cycle = self._state.cycle_count
        start_idx = (cycle * queries_per_cycle) % max(len(seeds), 1)
        selected = seeds[start_idx: start_idx + queries_per_cycle]

        new_entries: list[dict] = []
        for query in selected:
            logger.info("[%s] Searching: %s", self.domain, query)
            results = _search_ddg(query, max_results=5)

            for result in results:
                url = result.get("href", "")
                title = result.get("title", "no title")

                if not url or self._state.is_url_visited(self.domain, url):
                    continue

                logger.info("[%s] Fetching: %s", self.domain, url[:80])
                text = _fetch_page(url)
                if not text or len(text) < 200:
                    self._state.mark_url_visited(self.domain, url)
                    continue

                # Summarise the page in chunks
                summaries: list[str] = []
                for chunk in _chunk_text(text):
                    summary = self._summarise_chunk(
                        chunk=chunk,
                        url=url,
                        title=title,
                    )
                    if summary:
                        summaries.append(summary)
                    time.sleep(1)  # rate-limit guard

                if not summaries:
                    self._state.mark_url_visited(self.domain, url)
                    continue

                combined_summary = "\n".join(summaries)
                tags = self._extract_tags(combined_summary)

                entry = self._matrix.add_entry(
                    source_url=url,
                    title=title,
                    summary=combined_summary[:1500],
                    tags=tags,
                )
                self._state.mark_url_visited(self.domain, url)
                new_entries.append(entry)
                logger.info(
                    "[%s] ✓ Ingested: %s (tags: %s)",
                    self.domain,
                    title[:60],
                    tags,
                )
                time.sleep(2)  # polite crawl delay

        return new_entries

    def _summarise_chunk(self, chunk: str, url: str, title: str) -> str:
        """Ask Groq to produce concise bullet-point knowledge notes."""
        system_prompt = (
            "You are a technical knowledge extraction engine. "
            "Extract the most important technical facts, code patterns, "
            "and actionable insights from the provided text. "
            "Output ONLY concise bullet points (max 10). "
            "No preamble, no conclusions. Just the key technical facts."
        )
        messages = [
            {
                "role": "user",
                "content": (
                    f"Source: {url}\nTitle: {title}\n\n"
                    f"Extract key technical knowledge from:\n\n{chunk}"
                ),
            }
        ]
        try:
            return self._pool.chat(
                messages=messages,
                system=system_prompt,
                max_tokens=512,
                temperature=0.2,
            )
        except Exception as exc:
            logger.error("Summarisation failed: %s", exc)
            return ""

    def _extract_tags(self, summary: str) -> list[str]:
        """Ask Groq to extract keyword tags from a summary."""
        try:
            result = self._pool.chat(
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Extract 3-6 relevant single-word or two-word technology tags "
                            f"from this text. Return ONLY a comma-separated list, nothing else.\n\n"
                            f"{summary[:800]}"
                        ),
                    }
                ],
                max_tokens=64,
                temperature=0.1,
            )
            return [t.strip().lower() for t in result.split(",") if t.strip()][:6]
        except Exception:
            return [self.domain]


# ── Master crawl orchestrator ─────────────────────────────────────────────────


class CrawlOrchestrator:
    """
    Cycles through all five domain crawlers in round-robin fashion.
    Each call to run_next() runs one domain's cycle.
    """

    DOMAINS = list(DOMAIN_SEEDS.keys())

    def __init__(self, groq_pool: GroqPool, system_state: SystemState) -> None:
        self._pool = groq_pool
        self._state = system_state
        self._crawlers: dict[str, DomainCrawler] = {
            d: DomainCrawler(d, groq_pool, system_state)
            for d in self.DOMAINS
        }

    def run_next(self) -> tuple[str, list[dict]]:
        """
        Run the crawl cycle for the domain whose turn it is.
        Returns (domain_name, new_entries).
        """
        idx = self._state.cycle_count % len(self.DOMAINS)
        domain = self.DOMAINS[idx]
        logger.info("=== Starting crawl cycle for domain: %s ===", domain)
        entries = self._crawlers[domain].run_cycle()
        return domain, entries

    def run_url(self, url: str) -> dict | None:
        """
        Force-crawl a specific URL and store in the most relevant domain
        (guessed from URL keywords, defaulting to web_dev).
        """
        text = _fetch_page(url)
        if not text:
            return None

        # Heuristic domain detection
        domain = "web_dev"
        url_lower = url.lower()
        if any(k in url_lower for k in ["android", "kotlin", "flutter", "compose"]):
            domain = "android_core"
        elif any(k in url_lower for k in ["ffmpeg", "moviepy", "video", "audio"]):
            domain = "multimedia"
        elif any(k in url_lower for k in ["seo", "marketing", "ads", "keyword"]):
            domain = "marketing"
        elif any(k in url_lower for k in ["github", "ci", "devops", "terraform", "ansible"]):
            domain = "automation"

        crawler = self._crawlers[domain]
        summaries = []
        for chunk in _chunk_text(text):
            s = crawler._summarise_chunk(chunk=chunk, url=url, title=url)
            if s:
                summaries.append(s)
            time.sleep(1)

        if not summaries:
            return None

        combined = "\n".join(summaries)
        tags = crawler._extract_tags(combined)
        entry = DomainMatrix(domain).add_entry(
            source_url=url,
            title=url,
            summary=combined[:1500],
            tags=tags,
        )
        self._state.mark_url_visited(domain, url)
        return entry
