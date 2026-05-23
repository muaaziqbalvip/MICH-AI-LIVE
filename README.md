# AI Software Factory

> رَّبِّ زِدْنِي عِلْمًا — *My Lord, increase me in knowledge.* (Quran 20:114)

An autonomous, 24/7 self-evolving AI engineering system that runs on GitHub
Actions, learns from the web, and builds complete software projects on demand
via Telegram.

---

## Architecture

```
GitHub Actions (6h loop)
  └── engine/main.py          Master orchestrator (state machine)
        ├── state_manager.py  JSON persistence (state/*.json)
        ├── api_pool.py       Groq multi-key rotating inference pool
        ├── telegram_bot.py   Bidirectional Telegram control interface
        ├── crawler.py        DuckDuckGo + BeautifulSoup knowledge crawler
        ├── codegen.py        AI software factory (full project generation)
        ├── qa_rig.py         Self-healing compilation + test loop
        └── git_ops.py        State commit + next-run dispatch
```

---

## Setup Instructions

### 1. Fork / create your repository

Create a new GitHub repository and push all these files into it.

### 2. Add GitHub Secrets

Go to **Settings → Secrets and variables → Actions → New repository secret**
and add every secret below. **Never put real keys in code files.**

| Secret Name | Description |
|---|---|
| `TELEGRAM_TOKEN` | Your Telegram bot token (from @BotFather) |
| `TELEGRAM_ADMIN_CHAT_ID` | Your Telegram user/chat ID (get from @userinfobot) |
| `GROQ_API_KEY_1` | First Groq API key |
| `GROQ_API_KEY_2` | Second Groq API key |
| `GROQ_API_KEY_3` | Third Groq API key |
| `GROQ_API_KEY_4` | Fourth Groq API key |
| `GROQ_API_KEY_5` | Fifth Groq API key |
| `GROQ_API_KEY_6` | Sixth Groq API key |

> ⚠️ **IMPORTANT**: Rotate all API keys that were previously exposed in
> plaintext. Never commit secrets to the repository.

### 3. Enable GitHub Actions write permissions

Go to **Settings → Actions → General → Workflow permissions**
and select **Read and write permissions**.

### 4. Start the engine

Go to **Actions → AI Software Factory Engine Loop → Run workflow**.

The engine will:
1. Start and send a startup message to your Telegram.
2. Begin crawling programming documentation autonomously.
3. At ~5h 45m, commit state and trigger the next run automatically.
4. Repeat indefinitely.

---

## Telegram Commands

| Command | Description |
|---|---|
| `/start` | Welcome message |
| `/status` | Live system status |
| `/build <spec>` | Generate a complete software project |
| `/learn <url>` | Force-crawl a specific URL |
| `/pause` | Pause the crawler |
| `/resume` | Resume the crawler |
| `/queue` | Show pending task queue |
| `/logs` | Last 50 lines of the run log |
| `/reflection` | Last cycle self-audit |
| `/pool` | API key pool status |

### Example Build Commands

```
/build Android news reader app with Kotlin, Jetpack Compose, Retrofit2 for REST API, Room for offline cache, dark mode, and bottom navigation

/build Next.js 14 e-commerce site with TypeScript, Tailwind CSS, Stripe payment integration, PostgreSQL via Prisma, and admin dashboard

/build FastAPI REST backend for a task management system with JWT auth, PostgreSQL, Redis caching, and complete test suite

/build Flutter cross-platform chat app with Firebase Realtime Database, push notifications, image sharing, and read receipts
```

---

## State Files (state/)

| File | Contents |
|---|---|
| `system_state.json` | Master orchestrator state, task queue, cycle counter |
| `android_core.json` | Android/Kotlin/Flutter knowledge vectors |
| `web_dev.json` | Web development knowledge vectors |
| `multimedia.json` | FFmpeg/video automation knowledge |
| `marketing.json` | SEO/ads knowledge vectors |
| `automation.json` | DevOps/scripting knowledge vectors |
| `reflection_log.json` | Per-cycle AI self-audit records |
| `api_pool_state.json` | Key cooldown tracking |

All state files are committed back to the repo after every 5 cycles so
the next run resumes from the exact point this run ended.

---

## How the 24/7 Loop Works

```
Run N starts (GitHub Actions)
  │
  ├── [0h–5h45m]  Main event loop
  │     ├── Process Telegram tasks (/build, /learn)
  │     ├── Autonomous web crawl cycles
  │     ├── Commit state every 5 cycles
  │     └── Write reflection after every cycle
  │
  └── [5h45m]  Handoff
        ├── Write final reflection
        ├── git add state/ && git commit && git push
        └── POST /repos/{owner}/{repo}/dispatches
              └── event_type: "engine_loop"
                    └── Triggers Run N+1 immediately

If dispatch fails → CRON trigger fires at next 6h mark (safety net)
```

---

## Project Types (Auto-detected for /build)

| Type | Toolchain | Build Command |
|---|---|---|
| `android` | Kotlin + Compose | `./gradlew assembleDebug` |
| `flutter` | Dart + Flutter | `flutter build apk --debug` |
| `web_react` | React + Vite + TypeScript | `npm run build` |
| `web_next` | Next.js 14 App Router | `npm run build` |
| `fastapi` | Python + FastAPI | `pytest` |
| `script` | Python | `py_compile` |

The QA rig runs up to **5 self-healing iterations** per build:
parse error → ask Groq to fix → rewrite file → rebuild → repeat.
