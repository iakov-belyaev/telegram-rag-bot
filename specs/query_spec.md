# Spec: `query.py` — natural-language entity queries + dossier builder

Replace the stub in `query.py`. Python 3.11, full type hints, no prints at import.
Runtime logging via `log()` wrapping `logging_config.get_logger("query")`.

## Imports

asyncio, argparse, json, re, sys, time, datetime/timezone, typing (Any, Optional),
`import config`, `import storage as db` (the FAILOVER-aware layer — never import database
directly here), `import metrics`, `from logging_config import get_logger`,
`from discovery import normalize_peer` (t.me link canonicalization).

## 1. Entity parsing (pure, unit-testable)

```python
PHONE_RE = re.compile(r"\+?\d[\d\s\-().]{5,}\d")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]{2,}")
ETH_RE = re.compile(r"0x[a-fA-F0-9]{40}")
BTC_RE = re.compile(r"\b(?:bc1[a-z0-9]{25,62}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})\b")
USERNAME_RE = re.compile(r"@([A-Za-z0-9_]{4,32})")
_USER_ID_RE = re.compile(r"^\d{5,15}$")

def parse_entity(raw: str) -> dict:
    """Return {"type": ..., "value": normalized, "raw": raw}."""
```
Detection order (first hit wins):
1. email → {"type":"email","value": lowercased}
2. eth wallet → {"type":"wallet","value": raw, "chain":"ethereum"}
3. btc wallet → {"type":"wallet","value": raw, "chain":"bitcoin"}
4. contains "t.me/" → `normalize_peer(raw or the contained link)`; if peer → {"type":"group","value": peer}
5. `@handle` → {"type":"username","value": handle.lower()}
6. phone (PHONE_RE match with >= 7 digits) → {"type":"phone","value": "+digits" if raw had "+" else digits,
   "digits": digits, "variants": [digits, "+"+digits]}
7. pure digits 5-15 → {"type":"user_id","value": int(raw)}
8. else {"type":"text","value": raw.strip()}

## 2. Search + assembly helpers (all guarded; storage layer only)

- `def _payload(hit) -> dict` — `getattr(hit, "payload", None) or {}` (ScoredPoint OR SimpleNamespace).
- `def _hit_id(hit)` — `str(getattr(hit, "id", "")) or None`.
- `async def _search_messages_multi(terms: list[str], limit: int) -> list[dict]` — for each term:
  `await db.search_messages(term, limit=limit)`; merge preserving order, dedupe by _hit_id (fall back to
  (chat_id,message_id) payload key); return list of hit objects (NOT payloads).
- `async def _profiles_for_terms(terms: list[str], limit: int) -> list[dict]` — merge
  `db.search_profiles(term, limit)` payloads (dedupe by user_id), plus for each term run an additional
  profile-text scan attempt (same call is fine).
- `async def _resolve_username_to_profile(value: str) -> Optional[dict]` — first: `await db.get_profile(int)`?
  No: usernames → search: call `db.search_profiles(value, limit=5)`, return the payload whose
  `username` equals value (case-insensitive); else None.
- `async def _find_chat(chat_ref: str) -> Optional[dict]` — for t.me targets: fetch message hits for the
  bare token (sans @/+https) via db.search_messages; aggregate chat_title/chat_id from payloads whose
  chat_title/username-ish content matches (case-insensitive contains); return
  {"chat_id", "chat_title", "ref", "message_hits": n}; None when nothing.
- `def _date_key(value: str) -> str` — tolerant: return the raw string slice [:19] for sorting
  (lexicographic ISO sorts fine).

## 3. Dossier builder

```python
async def dossier(query: str, limit: Optional[int] = None) -> dict:
```
- `limit = limit or config.QUERY_MAX_RESULTS`; `entity = parse_entity(query)`.
- Fan out per type (all lists capped by `limit`):
  - **username**: `profile = await _resolve_username_to_profile(v)`; if found →
    `relations = await db.get_relations_for(profile["user_id"])`; `messages = await db.search_messages(v, limit=limit, sender_username=v)` else fallback `_search_messages_multi([v], limit)`.
  - **user_id**: `profile = await db.get_profile(v)`; relations via get_relations_for; messages: hits from
    `_search_messages_multi([str(v)] + ([profile["username"]] if profile and profile.get("username") else []), limit)` then post-filter payload sender_id == v (keep unfiltered extras under "context_messages" capped 10).
  - **phone / email / wallet**: terms = variants (phone) or [value]; messages = `_search_messages_multi`;
    profiles = `_profiles_for_terms`; identity from any profile whose bio contains the raw value
    (case-insensitive) → "identity.bio_matches".
  - **group**: `chat = await _find_chat(v)`; if chat and chat.get("chat_id") →
    `messages = await db.search_messages(entity["raw"], limit=limit, chat_id=chat["chat_id"])`;
    participants = Counter of payload sender_id/sender_name over hits → top 10 as
    "participants" (list of {"sender_id","sender_name","sender_username","count"}).
  - **text**: messages = `_search_messages_multi([v], limit)`; profiles = `_profiles_for_terms([v], limit)`.
- Timeline: over message payloads' "date" via `_date_key` → {"first_seen","last_seen"}.
- Assemble result dict:
```python
{
  "query": query, "entity": entity,
  "identity": {"profile": profile_or_None, "bio_matches": [...], "ids": [..], "aliases": [..]},
  "profiles": [payload,...], "relations": [...], "chats": [{"chat_id","chat_title","count"}...],
  "messages": [{"chat_title","chat_id","sender_name","sender_username","date","link","text","source_type"}...],
  "context_messages": [...], "participants": [...],
  "timeline": {...}, "stats": {"message_hits": n, "profile_hits": n, "relation_hits": n, "chat_hits": n},
  "generated_at": iso, "backend": (db.storage_status().get("mode") if callable(getattr(db, "storage_status", None)) else "unknown")
}
```
- Message text truncated to 500 chars; each message includes "link" from payload.
- NEVER raise: wrap everything; on unexpected error return the same shape with
  `"error": str(e)[:300]` and zero stats.

## 4. Rendering + CLI

```python
def render_text(result: dict) -> str
```
Plain-text dossier blocks: header line `📌 DOSSIER for <raw> [type]`, identity block, profiles block
(count + top 5), relations (top 10: edge_kind @a <-> @b w=weight), chats (top 10), messages
(up to 15: date — sender in chat: excerpt), timeline, stats line, error line when present.

```python
def _cli(argv: Optional[list[str]] = None) -> int
if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
```
argparse: positional `query`; `--json` flag; `--limit N`. `--json` → `json.dumps(result, ensure_ascii=False, indent=2)`;
else `render_text`. Exit codes: 0 ok, 2 usage. Also `async def amain`-free — call `asyncio.run(dossier(...))` inside _cli.

## Notes

- storage.py is the ONLY data dependency (failover-safe); pure functions importable without a loop.
- No Telegram client here — this is a read-only analytics layer.
