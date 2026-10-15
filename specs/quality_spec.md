# Spec: `quality.py` — chat quality classifier (GOOD / SPAM / SCAM / JUNK)

Replace the stub in `quality.py`. Python 3.11, full type hints, no prints at import.
Runtime logging via `log()` wrapping `logging_config.get_logger("quality")`:

```python
def log(message: str) -> None:
    _logger.info("%s", message)
```

## Imports

asyncio, json, os, re, time, pathlib.Path, datetime, typing (Any, Optional),
`import config`, `import metrics`,
`from scraper import FloodGate, RateGate, iterate_with_flood, telegram_call`
(careful: only import names that exist there).

## Constants

```python
GOOD, SPAM, SCAM, JUNK, UNKNOWN = "GOOD", "SPAM", "SCAM", "JUNK", "UNKNOWN"
QUALITY_CACHE_TTL_SECONDS = 7 * 86400

SCAM_PATTERNS = [  # regex fragments, case-insensitive; 2+ hits (or 1 + high links) => SCAM
    r"guaranteed\s+(returns?|profit)", r"double\s+your\s+(money|investment|deposit)",
    r"crypto\s+giveaway", r"dm\s+me\s+(to|for)\s+(invest|profit|earn)",
    r"инвестируй", r"сигналы\s+бесплатно", r"\bх2\b", r"bookmaker", r"no\s+risk\s+profit",
    r"withdraw\s+instantly", r"100%\s+profit", r"пассивный\s+доход",
]
SPAM_PATTERNS = [  # lighter marketing spam
    r"buy\s+followers", r"promo\s+code", r"discount\s+\d+%", r"casino", r"\bbetting\b",
    r"t\.me/[+]", r"подписывайтесь", r"реклама", r"\bsub4sub\b",
]
URL_REGEX = re.compile(r"https?://|t\.me/|www\.", re.IGNORECASE)
```

## Data gathering (Telegram side; never raises)

```python
async def gather_chat_sample(client, gate, chat, limit: int = 0) -> dict:
```
Returns:
```python
{
  "chat_id": int, "title": str, "username": str,
  "member_count": int | None,           # via client.get_chat_members_count (guard exceptions)
  "admin_usernames": list[str],         # via iterate_with_flood(client.get_chat_members, chat.id, filter="administrators"), cap 25
  "messages": [                          # up to (limit or config.QUALITY_SAMPLE_SIZE) messages
     {"text": str, "source": "text"|"caption", "has_url": bool, "is_forward": bool, "date": object},
  ],
  "errors": list[str],
}
```
- Each Telegram call individually try/except'd; failures append to `errors` and continue.
- `limit=0` → use config.QUALITY_SAMPLE_SIZE.

## Pure feature functions (unit-testable, no network)

```python
def text_ratio(messages) -> float            # fraction with len(text.strip()) >= 10
def link_density(messages) -> float          # fraction with has_url True
def forward_ratio(messages) -> float
def duplicate_ratio(messages) -> float       # fraction of normalized texts (lowercased, urls stripped,
                                             # non-alnum collapsed) that repeat within the sample
def velocity_per_hour(messages) -> float     # count / hours between min and max date (>= 1 msg);
                                             # dates are telegram datetime.date; tolerate unparseable by returning float(count)
def pattern_hits(messages, patterns) -> dict  # {pattern: total matches across texts}, only non-zero
```

```python
def score_sample(features: dict) -> tuple[str, float, list[str]]:
```
Rule order (first match wins; reasons list explains):
1. `scam_hits >= 2` OR (`scam_hits >= 1` AND `link_density > 0.30`) → (`SCAM`, score 0.9, reasons)
2. `spam_hits >= 3` OR (`link_density > 0.60` AND `duplicate_ratio > 0.40`) OR
   (`velocity > 120` AND `duplicate_ratio > 0.50`) → (`SPAM`, 0.7, reasons)
3. `duplicate_ratio > 0.70` OR `text_ratio < 0.20` → (`JUNK`, 0.6, reasons)
4. else → (`GOOD`, score = max(0.5, 1.0 - 0.1*spam_hits - 0.5*link_density), reasons)

`features` keys: scam_hits (int), spam_hits (int), link_density, duplicate_ratio,
forward_ratio, text_ratio, velocity, member_count, message_count.

```python
def label_allows_indexing(label: str) -> bool:
    """Only GOOD is indexed; UNKNOWN falls back to legacy heuristics."""
    return label == GOOD
```

## Classifier + optional LLM hook

```python
async def classify_chat(client, gate, chat, llm=None) -> dict:
```
- sample = await gather_chat_sample(...); features computed; `label, score, reasons = score_sample(...)`.
- If `llm` is not None AND config.QUALITY_LLM_ENABLED:
  try `verdict = await llm(prompt)` where prompt is a compact JSON-dumped summary
  (title, description-less features + up to 15 truncated message texts); verdict expected
  `{"label": "...", "confidence": 0-1, "reason": "..."}` (accept str or dict; tolerate
  markdown-fenced JSON by extracting the first {...} block). Apply only when
  confidence >= 0.8 and label in {GOOD, SPAM, SCAM, JUNK}: override rules verdict,
  append "llm:<reason>" to reasons. Any exception → keep rules verdict + append "llm-error".
- Result dict: `{"chat_id": int, "title": str, "label": str, "score": float, "reasons": list[str], "sampled_messages": int, "checked_at": epoch}`.
- `metrics.inc("quality_checks")` + `metrics.inc(f"quality_{label.lower()}")`.

## Cache

```python
class QualityCache:
    """JSON cache at config.QUALITY_CACHE_FILE: {str(chat_id): verdict-dict}."""
    def load(self) -> None
    def get(self, chat_id: int) -> Optional[dict]   # None when missing or older than TTL
    async def set(self, chat_id: int, verdict: dict) -> None   # atomic write (tmp + os.replace)
```

## Notes

- Never raise out of classify_chat: on gather total failure return `{"label": UNKNOWN, "score": 0.0, "reasons": ["classify-error:<e>"], ...}`.
- Do not import profiles/bot (avoid cycles). scraper import is fine.
