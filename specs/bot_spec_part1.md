# Spec PART 1 of 2: `bot.py` — intelligence helpers + NLP routing

Pass 1 of 2. Implement ONLY the sections below, replacing the `# STUB_PART_1:` lines.
LEAVE the `# STUB_PART_2:` lines untouched (pass 2 implements the handlers/panel).

`bot.py` REPLACES the old thin-wrapper bot.py. This is the global interaction bot
(Pyrogram bot client, session from config.SESSION_BOT).

## Preamble (this pass)

```python
import asyncio
import re
import time
from typing import Optional

from pyrogram import Client, filters
from pyrogram.types import InlineQueryResultArticle, InputTextMessageContent

import config
import database as db
import profiles
from openai import AsyncOpenAI          # AsyncOpenAI(config.DEEPSEEK_API_KEY, base_url=config.DEEPSEEK_BASE_URL)
from rbac import get_auth
import scraper                          # for get_control()
from scraper import TargetManager

def log(message: str) -> None:
    print(f"[bot {time.strftime('%H:%M:%S')}] {message}", flush=True)
```

Module-level state (no side effects):
```python
ADMIN_USER_ID: int = 0                       # set by register_handlers()
_username_re = re.compile(r"@([A-Za-z0-9_]{4,32})")
```

DeepSeek client: build LAZILY via `def get_llm() -> AsyncOpenAI` (module singleton
pattern like database.py — no client at import).

## 1. Shared helpers (sync where possible)

- `def _is_admin(user_id: int) -> bool` — `bool(ADMIN_USER_ID) and user_id == ADMIN_USER_ID and get_auth().is_authenticated(user_id)`.
- `def _extract_handles(text: str) -> list[str]` — all @handles (regex above), excluding the bot's own username (config.BOT_USERNAME stripped of @, case-insensitive), deduped, order preserved; lowercase for lookup.
- `def _fmt_dt(value: str) -> str` — best-effort shortening of ISO-ish strings for display (first 16 chars, T→space); "" stays "".
- `async def _deepseek_chat(system: str, user: str, max_tokens: int = None) -> str` —
  via get_llm().chat.completions.create wrapped in `asyncio.wait_for(timeout=config.DEEPSEEK_TIMEOUT_SECONDS)`;
  on any exception return a graceful fallback string `"(LLM unavailable: {e})"` — NEVER raise.
- `async def _resolve_handle(handle: str) -> Optional[int]` — exact-match search:
  use the Qdrant client (`db.get_qdrant().scroll`) with a Filter on PROFILES_COLLECTION,
  `FieldCondition(key="username", match=MatchValue(value=handle_without_@))`,
  limit 1, with_vectors=False, payload included. Wrap in `db._qdrant_read(([], None), ...)`
  when available (import `_qdrant_read` from database via getattr with a try/except fallback
  to plain await asyncio.to_thread). Return payload["user_id"] or None.
- `def _profile_line(p: dict) -> str` — one-line summary: display name, @username, msg count, group count.
- `def _truncate(text: str, n: int) -> str` — simple cut + "...".

## 2. RAG answers

- `async def rag_answer(query: str, deep: bool) -> str`:
  - `hits = await db.search_messages(query, limit=(15 if deep else 10))`
  - Build context lines: `- [{sender_name}{ (@username)} in '{chat_title}': {text}` (truncate each 300).
  - Deep mode additionally: `prof_hits = await db.search_profiles(query, limit=5)`; add a
    "MATCHING PROFILES" block: `- {display_name} (@{username}) bio: {bio} groups: {n}`.
  - system prompt: "You are an advanced Telegram Intelligence Assistant... answer STRICTLY from the provided context..." + deep-mode instruction to leverage profiles; casual-mode instruction to be brief (<=120 words), no fabrication; respond in the query's language (EN/RU).
  - Return `_deepseek_chat(system, user_with_context)`.

- `async def casual_whois(target_label: str, user_id: Optional[int]) -> str`:
  - If no profile found → "No indexed profile for {label} yet."
  - Casual = public-safe: display name, @username, indexed message count, number of groups (COUNT only, no titles), last-seen. Then one contextual line via `db.search_messages` on their username (top title hits, no text dumps) — keep short.

- `async def deep_whois(user_id: int) -> str`:
  - `data = await profiles.whois(user_id)`; None → "No indexed profile...".
  - Build a full dossier: identity block (name/@/id/bio/first-last seen), PFP HASH block (hash + meta),
    groups block (titles, up to 15), interactions top 8 (ids mapped: try db.get_profiles_bulk for
    display names), relations top 10 as `kind: @a <-> @b (weight)`, then a DeepSeek executive summary
    (2-4 sentences) ONLY if an LLM key is set — else skip synthesis.
  - Return the composed markdown.

- `async def casual_introduce(a_id: Optional[int], b_id: Optional[int], a_label: str, b_label: str) -> str`:
  - Both resolvable? → common-chat COUNT + "both active in N shared communities" + one-sentence
    suggestion. Missing → guidance text.
- `async def deep_introduce(a_id: int, b_id: int) -> str`:
  - `intro = await profiles.introduce(a_id, b_id)`; compose: both profiles briefed, common chats
    (titles list), relation payload if any, then a DeepSeek-generated 2-3 sentence warm
    introduction text + 1 suggested opener. Return markdown.

## 3. NLP routing

- `def _classify(text: str) -> tuple[str, dict]` — returns (intent, params):
  - "whois": patterns like (`who is`, `tell me about`, `найди`, `про кого`) OR single standalone @handle with short text.
    params {"handles": [...]}.
  - "introduce": text contains "introduce"/"познакомь"/"connect us"/"match" → params {"handles": [...]}.
  - "search"/default: otherwise → params {"query": text}.
- `async def handle_nlp(client: Client, message, text: str, deep_allowed: bool) -> None`:
  - strip the bot's @handle and leading punctuation from text.
  - classify; for whois/introduce resolve handles via `_resolve_handle` (or use the chat peer
    when exactly one handle equals a mentioned user? keep handles only).
  - deep results ONLY when `deep_allowed` (private admin) — otherwise use casual_* functions
    and append: "🔒 Deep profile available — authenticate with /auth in my private chat."
  - always `await message.reply_text(answer)` with try/except (log on failure).
