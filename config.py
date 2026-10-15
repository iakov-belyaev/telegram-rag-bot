"""Central configuration for telegram-rag-bot.

Single source of truth for env-driven settings.
Import-safe: no side effects beyond load_dotenv().
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env_str(key: str, default: str = "") -> str:
    value = os.getenv(key)
    if value is None:
        return default
    return value


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# --- Telegram credentials & sessions ---

TELEGRAM_API_ID: int = _env_int("TELEGRAM_API_ID", 0)
TELEGRAM_API_HASH: str = _env_str("TELEGRAM_API_HASH", "")
BOT_TOKEN: str = _env_str("BOT_TOKEN", "")


def _normalize_bot_username(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    return "@" + value.lstrip("@")


BOT_USERNAME: str = _normalize_bot_username(_env_str("BOT_USERNAME", ""))

SESSION_USERBOT: str = _env_str("USERBOT_SESSION_NAME", "my_userbot")
SESSION_BOT: str = _env_str("BOT_SESSION_NAME", "inline_bot")

# --- Qdrant ---

QDRANT_URL: str = _env_str("QDRANT_URL", "")
QDRANT_API_KEY: str = _env_str("QDRANT_API_KEY", "")
TG_RAG_DB_NAME: str = "tg_bot_rag"
VECTOR_SIZE: int = 384
EMBEDDING_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"

MESSAGES_COLLECTION: str = "telegram_messages"
CHATS_COLLECTION: str = "telegram_chats"
PROFILES_COLLECTION: str = "telegram_profiles"
RELATIONS_COLLECTION: str = "telegram_relations"

QDRANT_UPSERT_BATCH_SIZE: int = _env_int("QDRANT_UPSERT_BATCH_SIZE", 64)
QDRANT_TIMEOUT_SECONDS: int = _env_int("QDRANT_TIMEOUT_SECONDS", 30)

# --- DeepSeek LLM ---

DEEPSEEK_API_KEY: str = _env_str("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL: str = _env_str("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL: str = _env_str("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_MAX_TOKENS: int = _env_int("DEEPSEEK_MAX_TOKENS", 450)
DEEPSEEK_TIMEOUT_SECONDS: int = _env_int("DEEPSEEK_TIMEOUT_SECONDS", 60)

# --- Parallel scraping / ingestion engine ---

SCRAPER_WORKERS: int = _env_int("SCRAPER_WORKERS", 4)
SCRAPER_PAGE_SIZE: int = _env_int("SCRAPER_PAGE_SIZE", 100)
SCRAPER_IDLE_SLEEP_SECONDS: int = _env_int("SCRAPER_IDLE_SLEEP_SECONDS", 300)
SCRAPER_MIN_DELAY_SECONDS: float = _env_float("SCRAPER_MIN_DELAY_SECONDS", 1.0)
EMBED_BATCH_SIZE: int = _env_int("EMBED_BATCH_SIZE", 32)
MAX_VECTORS: int = _env_int("MAX_VECTORS", 100000)

# --- FloodWait resiliency ---

FLOOD_WAIT_MARGIN: float = _env_float("FLOOD_WAIT_MARGIN", 1.15)
FLOOD_WAIT_MAX_SLEEP_SECONDS: int = _env_int("FLOOD_WAIT_MAX_SLEEP_SECONDS", 3600)
FLOOD_MAX_RETRIES: int = _env_int("FLOOD_MAX_RETRIES", 3)
PARALLEL_JOIN_CONCURRENCY: int = _env_int("PARALLEL_JOIN_CONCURRENCY", 2)

# --- Rate limiting (Telegram API pacing) ---
TG_OPS_PER_SECOND: float = _env_float("TG_OPS_PER_SECOND", 5.0)
TG_OPS_BURST: int = _env_int("TG_OPS_BURST", 5)

# --- StorageShield (capacity guard) ---

STORAGE_WARN_RATIO: float = _env_float("STORAGE_WARN_RATIO", 0.80)
STORAGE_PAUSE_RATIO: float = _env_float("STORAGE_PAUSE_RATIO", 0.95)
STORAGE_CHECK_INTERVAL_SECONDS: int = _env_int("STORAGE_CHECK_INTERVAL_SECONDS", 3600)

# --- Chat quality filter (noise filtering) ---

NOISE_TITLE_PATTERNS: list[str] = [
    r"\bnsfw\b",
    r"\bxxx\b",
    r"\bporn",
    r"\bsex\b",
    r"\bhentai\b",
    r"\b18\+",
    r"\badult\b",
    r"\berotic\b",
    r"\bnude",
    r"\bonlyfans\b",
    r"\bmeme",
    r"\bмем",
    r"\bприкол",
    r"\bjoke",
    r"\bfunny\b",
    r"\bhumor\b",
    r"\bgambl",
    r"\bcasino\b",
    r"\bbet\b",
    r"\bbetting\b",
    r"\bказино\b",
    r"\bsticker",
    r"\bстикер",
    r"\bemoji\b",
    r"\bсмайл",
    r"\bairdrop\b",
    r"\bcrypto\s*signal",
    r"\bpump\b",
    r"\bscam\b",
]

MIN_TEXT_RATIO: float = 0.30
FILTER_SAMPLE_SIZE: int = 30
FILTER_MIN_SAMPLES: int = 10
CHAT_FILTER_CACHE_TTL_SECONDS: int = _env_int("CHAT_FILTER_CACHE_TTL_SECONDS", 86400)

# --- Multi-modal extraction ---

MIN_EXTRACTED_TEXT_LENGTH: int = 10
MAX_DOCUMENT_BYTES: int = 20 * 1024 * 1024
SUPPORTED_DOC_EXTENSIONS: set[str] = {".pdf", ".docx", ".txt"}
OCR_LANGUAGES: str = _env_str("OCR_LANGUAGES", "eng+rus")

# --- States / state files ---

STATE_DIR: Path = Path(_env_str("STATE_DIR", "state"))
SCRAPER_STATE_FILE: Path = STATE_DIR / "scraper_state.json"
CHAT_FILTER_CACHE_FILE: Path = STATE_DIR / "chat_filter_cache.json"
TARGET_GROUPS_FILE: Path = STATE_DIR / "target_groups.json"


def ensure_state_dir() -> None:
    """Create STATE_DIR (and parents) if it does not yet exist."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)


# --- Profiles / OSINT ---

PROFILE_REFRESH_INTERVAL_SECONDS: int = _env_int("PROFILE_REFRESH_INTERVAL_SECONDS", 86400)
PFP_HASH_SIZE: int = _env_int("PFP_HASH_SIZE", 256)
CASUAL_DEEP_LIMIT: int = _env_int("CASUAL_DEEP_LIMIT", 5)
PROFILE_MIN_MESSAGES: int = _env_int("PROFILE_MIN_MESSAGES", 3)
PROFILE_TOP_INTERACTIONS: int = _env_int("PROFILE_TOP_INTERACTIONS", 50)
PROFILE_SCROLL_PAGE: int = _env_int("PROFILE_SCROLL_PAGE", 1000)
RELATION_MAX_USERS_PER_CHAT: int = _env_int("RELATION_MAX_USERS_PER_CHAT", 20)
PROFILE_MAX_USERS_PER_SWEEP: int = _env_int("PROFILE_MAX_USERS_PER_SWEEP", 500)
RELATION_MIN_SHARED_CHATS: int = _env_int("RELATION_MIN_SHARED_CHATS", 1)

# --- Admin auth (RBAC challenge/response) & API ---

ADMIN_SECRET: str = _env_str("ADMIN_SECRET", "")
AUTH_CHALLENGE_TTL_SECONDS: int = _env_int("AUTH_CHALLENGE_TTL_SECONDS", 180)
ADMIN_SESSION_TTL_SECONDS: int = _env_int("ADMIN_SESSION_TTL_SECONDS", 2700)
AUTH_MAX_ATTEMPTS: int = _env_int("AUTH_MAX_ATTEMPTS", 3)
AUTH_LOCKOUT_SECONDS: int = _env_int("AUTH_LOCKOUT_SECONDS", 300)
API_HOST: str = _env_str("API_HOST", "127.0.0.1")
API_PORT: int = _env_int("API_PORT", 8088)
API_TOKEN: str = _env_str("API_TOKEN", "")

# --- Failover storage ---

FAILOVER_ENABLED: bool = _env_bool("FAILOVER_ENABLED", True)
FAILOVER_SQLITE_PATH: Path = STATE_DIR / "failover_store.db"
FAILOVER_HEALTH_INTERVAL_SECONDS: int = _env_int("FAILOVER_HEALTH_INTERVAL_SECONDS", 30)
FAILOVER_DEGRADE_ERRORS: int = _env_int("FAILOVER_DEGRADE_ERRORS", 3)
FAILOVER_PROBE_TIMEOUT_SECONDS: int = _env_int("FAILOVER_PROBE_TIMEOUT_SECONDS", 8)
FAILOVER_RESYNC_BATCH: int = _env_int("FAILOVER_RESYNC_BATCH", 200)

# --- Structured logging ---

LOG_LEVEL: str = _env_str("LOG_LEVEL", "INFO")
LOG_JSON: bool = _env_bool("LOG_JSON", True)
LOG_DIR: Path = Path(_env_str("LOG_DIR", "logs"))
LOG_MAX_BYTES: int = _env_int("LOG_MAX_BYTES", 10 * 1024 * 1024)
LOG_BACKUP_COUNT: int = _env_int("LOG_BACKUP_COUNT", 5)

# --- Group discovery ---

DISCOVERY_ENABLED: bool = _env_bool("DISCOVERY_ENABLED", True)
DISCOVERY_INTERVAL_SECONDS: int = _env_int("DISCOVERY_INTERVAL_SECONDS", 1800)
DISCOVERY_AUTO_JOIN: bool = _env_bool("DISCOVERY_AUTO_JOIN", True)
DISCOVERY_MUTE_JOINED: bool = _env_bool("DISCOVERY_MUTE_JOINED", True)
DISCOVERY_AUTO_LEAVE_BAD: bool = _env_bool("DISCOVERY_AUTO_LEAVE_BAD", False)
DISCOVERY_MAX_JOINS_PER_CYCLE: int = _env_int("DISCOVERY_MAX_JOINS_PER_CYCLE", 3)
DISCOVERY_QUEUE_FILE: Path = STATE_DIR / "discovery_queue.json"
DISCOVERY_SEEDS_FILE: Path = Path(_env_str("DISCOVERY_SEEDS_FILE", "seeds.yaml"))
DISCOVERY_TARGET_REGION: str = _env_str("DISCOVERY_TARGET_REGION", "cyprus")
CYPRUS_IT_CHAT: str = _env_str("CYPRUS_IT_CHAT", "")
DISCOVERY_SCAN_MESSAGES_LIMIT: int = _env_int("DISCOVERY_SCAN_MESSAGES_LIMIT", 5000)

# --- @kiprinfobot adapter ---

KIPRINFBOT_ENABLED: bool = _env_bool("KIPRINFBOT_ENABLED", True)
KIPRINFBOT_USERNAME: str = _env_str("KIPRINFBOT_USERNAME", "kiprinfobot")
KIPRINFBOT_DELAY_SECONDS: float = _env_float("KIPRINFBOT_DELAY_SECONDS", 20.0)
KIPRINFBOT_REPLY_TIMEOUT_SECONDS: int = _env_int("KIPRINFBOT_REPLY_TIMEOUT_SECONDS", 25)
KIPRINFBOT_MAX_QUERIES: int = _env_int("KIPRINFBOT_MAX_QUERIES", 6)

# --- Web discovery adapter ---

WEB_DISCOVERY_ENABLED: bool = _env_bool("WEB_DISCOVERY_ENABLED", False)
WEB_FETCH_DELAY_SECONDS: float = _env_float("WEB_FETCH_DELAY_SECONDS", 5.0)
WEB_MAX_RESULTS_PER_QUERY: int = _env_int("WEB_MAX_RESULTS_PER_QUERY", 20)

# --- Group quality classifier ---

QUALITY_CACHE_FILE: Path = STATE_DIR / "quality_cache.json"
QUALITY_SAMPLE_SIZE: int = _env_int("QUALITY_SAMPLE_SIZE", 50)
QUALITY_LLM_ENABLED: bool = _env_bool("QUALITY_LLM_ENABLED", False)
QUALITY_LLM_MODEL: str = _env_str("QUALITY_LLM_MODEL", "deepseek-chat")

# --- Query / dossier / export ---

QUERY_MAX_RESULTS: int = _env_int("QUERY_MAX_RESULTS", 50)
EXPORT_DIR: Path = STATE_DIR / "exports"

# --- Watchlist ---

WATCHLIST_FILE: Path = STATE_DIR / "watchlist.json"
WATCHLIST_HITS_FILE: Path = STATE_DIR / "watchlist_hits.jsonl"
WATCHLIST_FLUSH_SECONDS: int = _env_int("WATCHLIST_FLUSH_SECONDS", 60)

# --- Control flags (kill switch / remote pause) ---

CONTROL_FLAGS_FILE: Path = STATE_DIR / "control_flags.json"
CONTROL_FLAG_POLL_SECONDS: int = _env_int("CONTROL_FLAG_POLL_SECONDS", 5)

# --- Message dedup ---

DEDUP_ENABLED: bool = _env_bool("DEDUP_ENABLED", True)

# --- Validation & summary helpers ---

CRITICAL_ENV_KEYS: tuple[str, ...] = (
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "BOT_TOKEN",
    "QDRANT_URL",
    "QDRANT_API_KEY",
    "DEEPSEEK_API_KEY",
    "ADMIN_SECRET",
)


def validate_config() -> list[str]:
    """Return a list of human-readable configuration problems.

    An empty list means the configuration is fully valid.
    """
    problems: list[str] = []

    for key in CRITICAL_ENV_KEYS:
        raw = os.getenv(key)
        if raw is None or raw.strip() == "":
            problems.append(f"Missing or empty required env var: {key}")

    if not API_TOKEN:
        problems.append(
            "API_TOKEN is empty: control API endpoints are disabled."
        )

    return problems


def config_summary() -> dict:
    """Return a JSON-safe dict of non-secret operational settings.

    Never includes secret values (keys, tokens, hashes) — only booleans
    indicating whether a secret is set.
    """
    return {
        "session_userbot": SESSION_USERBOT,
        "session_bot": SESSION_BOT,
        "bot_username": BOT_USERNAME,
        "qdrant_db_name": TG_RAG_DB_NAME,
        "vector_size": VECTOR_SIZE,
        "embedding_model": EMBEDDING_MODEL,
        "collections": {
            "messages": MESSAGES_COLLECTION,
            "chats": CHATS_COLLECTION,
            "profiles": PROFILES_COLLECTION,
            "relations": RELATIONS_COLLECTION,
        },
        "qdrant_upsert_batch_size": QDRANT_UPSERT_BATCH_SIZE,
        "qdrant_timeout_seconds": QDRANT_TIMEOUT_SECONDS,
        "deepseek_base_url": DEEPSEEK_BASE_URL,
        "deepseek_model": DEEPSEEK_MODEL,
        "deepseek_max_tokens": DEEPSEEK_MAX_TOKENS,
        "deepseek_timeout_seconds": DEEPSEEK_TIMEOUT_SECONDS,
        "scraper_workers": SCRAPER_WORKERS,
        "scraper_page_size": SCRAPER_PAGE_SIZE,
        "scraper_idle_sleep_seconds": SCRAPER_IDLE_SLEEP_SECONDS,
        "scraper_min_delay_seconds": SCRAPER_MIN_DELAY_SECONDS,
        "embed_batch_size": EMBED_BATCH_SIZE,
        "max_vectors": MAX_VECTORS,
        "flood_wait_margin": FLOOD_WAIT_MARGIN,
        "flood_wait_max_sleep_seconds": FLOOD_WAIT_MAX_SLEEP_SECONDS,
        "flood_max_retries": FLOOD_MAX_RETRIES,
        "tg_ops_per_second": TG_OPS_PER_SECOND,
        "tg_ops_burst": TG_OPS_BURST,
        "parallel_join_concurrency": PARALLEL_JOIN_CONCURRENCY,
        "storage_warn_ratio": STORAGE_WARN_RATIO,
        "storage_pause_ratio": STORAGE_PAUSE_RATIO,
        "storage_check_interval_seconds": STORAGE_CHECK_INTERVAL_SECONDS,
        "min_text_ratio": MIN_TEXT_RATIO,
        "filter_sample_size": FILTER_SAMPLE_SIZE,
        "filter_min_samples": FILTER_MIN_SAMPLES,
        "chat_filter_cache_ttl_seconds": CHAT_FILTER_CACHE_TTL_SECONDS,
        "min_extracted_text_length": MIN_EXTRACTED_TEXT_LENGTH,
        "max_document_bytes": MAX_DOCUMENT_BYTES,
        "supported_doc_extensions": sorted(SUPPORTED_DOC_EXTENSIONS),
        "ocr_languages": OCR_LANGUAGES,
        "state_dir": str(STATE_DIR),
        "profile_refresh_interval_seconds": PROFILE_REFRESH_INTERVAL_SECONDS,
        "profile_min_messages": PROFILE_MIN_MESSAGES,
        "profile_top_interactions": PROFILE_TOP_INTERACTIONS,
        "profile_scroll_page": PROFILE_SCROLL_PAGE,
        "relation_max_users_per_chat": RELATION_MAX_USERS_PER_CHAT,
        "profile_max_users_per_sweep": PROFILE_MAX_USERS_PER_SWEEP,
        "relation_min_shared_chats": RELATION_MIN_SHARED_CHATS,
        "pfp_hash_size": PFP_HASH_SIZE,
        "casual_deep_limit": CASUAL_DEEP_LIMIT,
        "auth_challenge_ttl_seconds": AUTH_CHALLENGE_TTL_SECONDS,
        "admin_session_ttl_seconds": ADMIN_SESSION_TTL_SECONDS,
        "auth_max_attempts": AUTH_MAX_ATTEMPTS,
        "auth_lockout_seconds": AUTH_LOCKOUT_SECONDS,
        "api_host": API_HOST,
        "api_port": API_PORT,
        "failover_enabled": FAILOVER_ENABLED,
        "discovery_enabled": DISCOVERY_ENABLED,
        "quality_llm_enabled": QUALITY_LLM_ENABLED,
        "watchlist_flush_seconds": WATCHLIST_FLUSH_SECONDS,
        "log_level": LOG_LEVEL,
        "dedup_enabled": DEDUP_ENABLED,
        "admin_secret_set": bool(ADMIN_SECRET),
        "api_token_set": bool(API_TOKEN),
        "telegram_api_id_set": bool(TELEGRAM_API_ID),
        "telegram_api_hash_set": bool(TELEGRAM_API_HASH),
        "bot_token_set": bool(BOT_TOKEN),
        "qdrant_url_set": bool(QDRANT_URL),
        "qdrant_api_key_set": bool(QDRANT_API_KEY),
        "deepseek_api_key_set": bool(DEEPSEEK_API_KEY),
    }
