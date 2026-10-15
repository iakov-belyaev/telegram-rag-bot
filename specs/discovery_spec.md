# Spec: `discovery.py` — autonomous Cyprus group discovery pipeline

Replace the stub in `discovery.py`. Python 3.11, full type hints, no prints at import.
Runtime logging via `log()` wrapping `logging_config.get_logger("discovery")`.

## Imports

asyncio, json, os, re, time, random, urllib.parse, urllib.robotparser, pathlib.Path,
datetime, typing (Any, Optional), `httpx`, `yaml`,
`import config`, `import database as db`, `import metrics`,
`from logging_config import get_logger`,
`from scraper import FloodGate, RateGate, TargetManager, iterate_with_flood, mute_chat, telegram_call`,
`from quality import QualityCache, classify_chat, label_allows_indexing, GOOD, UNKNOWN`.

## 1. Peer normalization + link extraction (pure, unit-testable)

```python
RESERVED_TME_PATHS = {"joinchat","addstickers","proxy","socks","share","iv","setlanguage",
                      "addtheme","boost","contact","c","k","s","login","confirmphone","socks"}

def normalize_peer(raw: str) -> Optional[str]:
    """Return canonical peer: "@name" | "https://t.me/+hash" | "https://t.me/joinchat/hash" | "-100…int".
    Accepts @name, t.me/name, https://t.me/name, t.me/+hash, t.me/joinchat/hash, numeric ids.
    Returns None for reserved/invalid targets."""
```
Rules: strip whitespace/trailing punctuation; if starts with "http(s)://t.me/" or "t.me/" parse path;
`+hash` → "https://t.me/+hash"; `joinchat/hash` → "https://t.me/joinchat/hash"; plain name (len>=4,
`[A-Za-z0-9_]+`, not in RESERVED_TME_PATHS, not the bot's own username from config.BOT_USERNAME) → "@name";
all-digits (optionally leading -) length>=6 → as-is string. Else None.

```python
def extract_tme_links(text: str) -> list[str]:
    """All canonicalized t.me targets in a text blob (regex n.N. t.me/...), deduped, order kept."""
```

## 2. DiscoveryQueue (persistent, atomic)

File: `config.DISCOVERY_QUEUE_FILE`.
Shape: `{"seen": {peer: iso}, "pending": [{"peer": str, "source": str, "added_at": iso, "attempts": int, "meta": dict}], "enqueued_total": int}`

```python
class DiscoveryQueue:
    def load(self) -> None
    async def enqueue(self, peer: str, source: str, meta: Optional[dict] = None) -> bool  # False if seen
    def pending(self, limit: Optional[int] = None) -> list[dict]
    async def mark_done(self, peer: str) -> None          # drop from pending, keep in seen
    async def note_attempt(self, peer: str) -> int        # attempts += 1, atomic save; returns new count
    async def save(self) -> None                           # tmp + os.replace
    def stats(self) -> dict                                # {"seen": n, "pending": n, "enqueued_total": n}
```
`attempts >= 3` items are dropped by callers (agent), not by the queue.

## 3. Adapters (all exposed for tests; each `discover(ctx) -> int` enqueued)

```python
@dataclass
class DiscoveryContext:
    queue: DiscoveryQueue
    client: Any              # pyrogram userbot (None for web-only tests)
    gate: FloodGate
    rate: RateGate
    seeds: dict              # {"peers": [...], "queries": [...]}
```

### 3a. SeedAdapter
`load_seeds(path=None) -> dict` — reads YAML `config.DISCOVERY_SEEDS_FILE`; missing/invalid →
`{"peers": [], "queries": []}` (log once). `discover` enqueues every seed peer (source "seed").

### 3b. CyprusItAdapter
Scans already-indexed messages for t.me links:
- Uses `db.get_qdrant().scroll` via `db._qdrant_read(([], None), ...)`, limit
  `config.DISCOVERY_SCAN_MESSAGES_LIMIT`, with_payload=True, with_vectors=False.
- Iterates points' `payload["text"]`, extracts links, enqueues (source "cyprus-it-chat").
- If `config.CYPRUS_IT_CHAT` set: numeric → scroll with a FieldCondition chat_id filter;
  otherwise POST-filter points whose `chat_title` contains the value (sans "@") case-insensitively.
- Cap: stop after `DISCOVERY_SCAN_MESSAGES_LIMIT` points or `2 * WEB_MAX_RESULTS_PER_QUERY` enqueues (use 200 cap constant in module).

### 3c. KiprinfobotAdapter
Interactive info-bot querying (guarded, paced):
```python
async def _send_and_collect(client, gate, target: str, text: str, timeout: int) -> list[Any]:
    """Send `text` to target; poll history up to `timeout` s for newer messages from it.
    Returns list of message objects (newest-last). Never raises."""
```
- First a "/start" message (once per cycle), then questions: `seeds["queries"]` plus built-in defaults
  `["кипр группы чаты", "cyprus telegram groups"]`. Max `config.KIPRINFBOT_MAX_QUERIES` per cycle.
- Between sends: `await asyncio.sleep(config.KIPRINFBOT_DELAY_SECONDS)`.
- Extract links from both message `.text` and `reply_markup.inline_keyboard[*][*].url` (guard attribute chains).
- `discover` honors `config.KIPRINFBOT_ENABLED` (skip + log when off; metrics.inc("discovery_kiprinfobot_skipped")).
- Every Telegram call through telegram_call/iterate_with_flood; all wrapped try/except.

### 3d. WebSearchAdapter (pluggable providers, off by default)
```python
class WebProvider:
    name: str = "provider"
    async def search(self, query: str) -> list[str]: ...      # returns URLs

class DuckDuckGoHtmlProvider(WebProvider):  # name="ddg-html"
    # GET https://html.duckduckgo.com/html/?q=<query>, UA rotated from module UA_POOL,
    # timeout 15s; parse result links via regex on href="...uddg=<urlencoded>..." decode.
class SitemapDirectoryProvider(WebProvider):  # name="directory"
    # Configurable directory page list (module DEFAULT_DIRECTORY_URLS = [] placeholder + seeds["directory_urls"]);
    # GET each page, extract t.me links from html.
```
Rules for both:
- `async def _robots_allows(url) -> bool` — httpx GET `scheme://host/robots.txt` (UA pool, timeout 10),
  parse via urllib.robotparser.RobotFileParser().parse(lines); cache per host in module dict.
  robots fetch failure ⇒ disallowed (conservative) + log + metric.
- Pacing: `await asyncio.sleep(config.WEB_FETCH_DELAY_SECONDS)` between every outbound request.
- `WebSearchAdapter.discover` honors `config.WEB_DISCOVERY_ENABLED` (off ⇒ return 0, log once).
- Built-in queries: `["site:t.me cyprus", "site:t.me cyprus group", "cyprus telegram chat join"]` + seeds["queries"][:3].
- Extract + enqueue (source f"web:{provider.name}"); cap `config.WEB_MAX_RESULTS_PER_QUERY` per query.

## 4. GroupDiscoveryAgent

```python
class GroupDiscoveryAgent:
    def __init__(self, client, gate, rate, queue, cache: QualityCache, targets: TargetManager, control): ...
    async def process_candidate(self, item: dict) -> dict:
        """join → mute → triage → record. Returns {"peer","state","label","score"}."""
    async def run_cycle(self, seeds: Optional[dict] = None) -> dict:
        """Adapters → pending triage (max config.DISCOVERY_MAX_JOINS_PER_CYCLE), returns stats."""
    async def discover_sweep(self, control) -> None:
        """Forever-loop honoring DISCOVERY_ENABLED/interval/pause/stop."""
```
process_candidate flow:
1. `peer = item["peer"]`; attempt cap: if `item.get("attempts",0) >= 3` → queue.mark_done + return dropped.
2. Join if `config.DISCOVERY_AUTO_JOIN`:
   - `joined_chat = await telegram_call(gate, client.join_chat, peer)`;
   - except `tg_errors.UserAlreadyParticipant` → proceed; except permanent errors
     (`UsernameNotOccupied, UsernameInvalid, InviteHashExpired, InviteHashInvalid, ChannelPrivate`) →
     mark_done, metrics.inc("groups_join_rejected"), return {"state":"join_error"}; except others →
     note_attempt, metrics.inc("groups_join_errors"), return {"state":"retry"}.
3. Mute IMMEDIATELY (before triage) when `config.DISCOVERY_MUTE_JOINED`:
   `chat_id = getattr(joined_chat, "id", None) or peer`; `await mute_chat(client, chat_id)`; guarded —
   mute failure logs + metrics.inc("mute_failures") but never blocks.
4. `chat = await telegram_call(gate, client.get_chat, peer)` (guarded; fallback: minimal snapshot object).
5. `verdict = await classify_chat(client, gate, chat)`; `await cache.set(chat_id, verdict)`.
6. `await targets.set_state(peer, "joined", title=verdict title, note=f"discovered:{item['source']}:{verdict['label']}")`.
7. If `verdict["label"] != GOOD and config.DISCOVERY_AUTO_LEAVE_BAD`: guarded `leave_chat`;
   metrics.inc("groups_left_bad").
8. metrics: `groups_joined` (when join happened), `groups_triaged`, `groups_rejected` (non-GOOD).
9. `await queue.mark_done(peer)`; return summary dict.
run_cycle: build seeds; run adapters [SeedAdapter, CyprusItAdapter, KiprinfobotAdapter(if client), WebSearchAdapter]
each in try/except (metric discovery_adapter_errors); then process pending items up to
DISCOVERY_MAX_JOINS_PER_CYCLE sequentially (rate gate between); return aggregate stats.
discover_sweep: `while not control.stop_requested:` — honor paused (sleep 5 loop);
`await agent.run_cycle()`, then sleep `config.DISCOVERY_INTERVAL_SECONDS` in 5s steps checking stop/pause.
When `not config.DISCOVERY_ENABLED` → log once + return (task ends quietly).

## 5. Wire-up helper

```python
async def run_discovery(client, gate: FloodGate, rate: RateGate, control) -> None:
    """Top-level for main.py: builds queue/cache/targets/agent and runs discover_sweep."""
```

## Notes

- NEVER raise out of process_candidate / run_cycle / adapter.discover (top-level guards + logs).
- Mute implementation lives in scraper.mute_chat (raw account.UpdateNotifySettings with
  raw.types.InputNotifyPeer(peer=await client.resolve_peer(chat_id)), settings
  InputPeerNotifySettings(show_previews=False, silent=True, mute_until=2147483647)).
  Do NOT reimplement it here — import from scraper.
- No direct `Client` construction anywhere.
