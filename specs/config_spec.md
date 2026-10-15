# Spec: `config.py` — central settings module for telegram-rag-bot

Create a NEW file `config.py` implementing EXACTLY this specification.

## Hard rules

- Python 3.11, module-level UPPER_SNAKE constants, full type hints.
- The ONLY side effect allowed at import: `load_dotenv()` from python-dotenv (project-root `.env`).
- Do NOT import fastembed, qdrant_client, pyrogram, openai, fastapi here. Pure stdlib + python-dotenv only.
- Do NOT print anything at import. This module must be import-safe for tests and CLI helpers.
- Env values MUST be type-cast (int/float) with sane defaults shown below.

## Telegram credentials & sessions

- `TELEGRAM_API_ID: int` from `TELEGRAM_API_ID` (default 0)
- `TELEGRAM_API_HASH: str` from `TELEGRAM_API_HASH` (default "")
- `BOT_TOKEN: str` from `BOT_TOKEN` (default "")
- `BOT_USERNAME: str` from `BOT_USERNAME` (default ""), normalized: strip whitespace, ensure a single leading "@" if non-empty
- `SESSION_USERBOT: str` = env `USERBOT_SESSION_NAME`, default `"my_userbot"` (existing authorized session file — do not rename)
- `SESSION_BOT: str` = env `BOT_SESSION_NAME`, default `"inline_bot"` (existing authorized session file — do not rename)

## Qdrant

- `QDRANT_URL: str`, `QDRANT_API_KEY: str` from env
- `TG_RAG_DB_NAME: str` = `"tg_bot_rag"` (documented cluster/database name used by this bot)
- `VECTOR_SIZE: int` = 384
- `EMBEDDING_MODEL: str` = `"sentence-transformers/all-MiniLM-L6-v2"`
- Collections: `MESSAGES_COLLECTION` = `"telegram_messages"`, `CHATS_COLLECTION` = `"telegram_chats"`, `PROFILES_COLLECTION` = `"telegram_profiles"`, `RELATIONS_COLLECTION` = `"telegram_relations"`
- `QDRANT_UPSERT_BATCH_SIZE: int` = env `QDRANT_UPSERT_BATCH_SIZE`, default 64
- `QDRANT_TIMEOUT_SECONDS: int` = env, default 30

## DeepSeek LLM

- `DEEPSEEK_API_KEY: str` from env
- `DEEPSEEK_BASE_URL: str` = env `DEEPSEEK_BASE_URL`, default `"https://api.deepseek.com"`
- `DEEPSEEK_MODEL: str` = env `DEEPSEEK_MODEL`, default `"deepseek-chat"`
- `DEEPSEEK_MAX_TOKENS: int` = env, default 450
- `DEEPSEEK_TIMEOUT_SECONDS: int` = env, default 60

## Parallel scraping / ingestion engine

- `SCRAPER_WORKERS: int` = env `SCRAPER_WORKERS`, default 4 (parallel asyncio worker coroutines)
- `SCRAPER_PAGE_SIZE: int` = env, default 100 (messages per history page)
- `SCRAPER_IDLE_SLEEP_SECONDS: int` = env, default 300 (sleep between full passes)
- `SCRAPER_MIN_DELAY_SECONDS: float` = env, default 1.0 (delay per chat between pages)
- `EMBED_BATCH_SIZE: int` = env, default 32 (texts embedded per batch)
- `MAX_VECTORS: int` = env `MAX_VECTORS`, default 100000 (StorageShield capacity ceiling)

## FloodWait resiliency (Telegram rate limits)

- `FLOOD_WAIT_MARGIN: float` = env, default 1.15 (multiply FloodWait seconds by this + jitter)
- `FLOOD_WAIT_MAX_SLEEP_SECONDS: int` = env, default 3600 (cap any single sleep)
- `FLOOD_MAX_RETRIES: int` = env, default 3 (retries per operation after FloodWait)
- `PARALLEL_JOIN_CONCURRENCY: int` = env, default 2 (group joins are extra flood-sensitive)

## StorageShield (capacity guard)

- `STORAGE_WARN_RATIO: float` = env, default 0.80
- `STORAGE_PAUSE_RATIO: float` = env, default 0.95
- `STORAGE_CHECK_INTERVAL_SECONDS: int` = env, default 3600

## Chat quality filter (noise filtering)

- `NOISE_TITLE_PATTERNS: list[str]` — keep EXACTLY this list (regex fragments, case-insensitive, joined with "|" by consumers):
  `[r"\bnsfw\b", r"\bxxx\b", r"\bporn", r"\bsex\b", r"\bhentai\b", r"\b18\+", r"\badult\b", r"\berotic\b", r"\bnude", r"\bonlyfans\b", r"\bmeme", r"\bмем", r"\bприкол", r"\bjoke", r"\bfunny\b", r"\bhumor\b", r"\bgambl", r"\bcasino\b", r"\bbet\b", r"\bbetting\b", r"\bказино\b", r"\bsticker", r"\bстикер", r"\bemoji\b", r"\bсмайл", r"\bairdrop\b", r"\bcrypto\s*signal", r"\bpump\b", r"\bscam\b"]`
- `MIN_TEXT_RATIO: float` = 0.30
- `FILTER_SAMPLE_SIZE: int` = 30
- `FILTER_MIN_SAMPLES: int` = 10
- `CHAT_FILTER_CACHE_TTL_SECONDS: int` = env, default 86400 (verdict cache TTL)

## Multi-modal extraction

- `MIN_EXTRACTED_TEXT_LENGTH: int` = 10
- `MAX_DOCUMENT_BYTES: int` = 20 * 1024 * 1024
- `SUPPORTED_DOC_EXTENSIONS: set[str]` = `{".pdf", ".docx", ".txt"}`
- `OCR_LANGUAGES: str` = env `OCR_LANGUAGES`, default `"eng+rus"`

## States / state files (resume + persistence)

- `STATE_DIR: pathlib.Path` = env `STATE_DIR`, default `Path("state")` (relative to project root)
- `SCRAPER_STATE_FILE: Path` = `STATE_DIR / "scraper_state.json"` (per-chat pagination cursors)
- `CHAT_FILTER_CACHE_FILE: Path` = `STATE_DIR / "chat_filter_cache.json"`
- `TARGET_GROUPS_FILE: Path` = `STATE_DIR / "target_groups.json"` (managed list of groups the userbot should join / watch)
- Provide a helper `ensure_state_dir() -> None` that creates STATE_DIR with `mkdir(parents=True, exist_ok=True)` (called explicitly by other modules, NOT at import).

## Profiles / OSINT

- `PROFILE_REFRESH_INTERVAL_SECONDS: int` = env, default 86400 (re-fetch bios only after this age)
- `PFP_HASH_SIZE: int` = env, default 256 (store a 256-bit perceptual/digest hash of profile photos, not images)
- `CASUAL_DEEP_LIMIT: int` = env, default 5 (max results shown to non-admin casual query)

## Admin auth (RBAC challenge/response) & API

- `ADMIN_SECRET: str` from env (HMAC key used to answer auth challenges)
- `AUTH_CHALLENGE_TTL_SECONDS: int` = env, default 180 (challenge nonce validity)
- `ADMIN_SESSION_TTL_SECONDS: int` = env, default 2700 (admin session validity after successful auth)
- `AUTH_MAX_ATTEMPTS: int` = env, default 3 (failed responses before lockout)
- `AUTH_LOCKOUT_SECONDS: int` = env, default 300
- `API_HOST: str` = env `API_HOST`, default `"127.0.0.1"`
- `API_PORT: int` = env `API_PORT`, default 8088
- `API_TOKEN: str` from env (bearer token for control endpoints; empty = control routes disabled)

## Validation & summary helpers

- `CRITICAL_ENV_KEYS: tuple[str, ...]` = `("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "BOT_TOKEN", "QDRANT_URL", "QDRANT_API_KEY", "DEEPSEEK_API_KEY", "ADMIN_SECRET")`
- `def validate_config() -> list[str]:` returns a list of human-readable problems: each critical env var that is missing/empty, plus warning strings for empty `API_TOKEN` (control API disabled). Empty list = fully valid.
- `def config_summary() -> dict:` returns a JSON-safe dict of non-secret operational settings (worker count, batch sizes, thresholds, collection names, API host/port, session names) — NEVER include secret values (keys/tokens/hashes), only booleans like `"admin_secret_set": bool(ADMIN_SECRET)`.

## Module docstring

Start the file with a docstring: "Central configuration for telegram-rag-bot. Single source of truth for env-driven settings. Import-safe: no side effects beyond load_dotenv()."
