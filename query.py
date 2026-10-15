"""Natural-language OSINT queries: entity parsing + dossier builder.

"give me everything on +35799123456" → parse (phone/username/user_id/email/
wallet/group/text) → fan out across messages, profiles, relations and chats
(via the failover-aware storage layer) → structured dossier. CLI (+ --json),
HTTP endpoint and bot hook live outside this module.
"""

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from typing import Any, Optional

import config
import metrics
import storage as db
from discovery import normalize_peer
from logging_config import get_logger

_logger = get_logger("query")


def log(message: str) -> None:
    _logger.info("%s", message)


# ---------------------------------------------------------------------------
# 1. Entity parsing (pure, unit-testable)
# ---------------------------------------------------------------------------
PHONE_RE = re.compile(r"\+?\d[\d\s\-().]{5,}\d")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]{2,}")
ETH_RE = re.compile(r"0x[a-fA-F0-9]{40}")
BTC_RE = re.compile(r"\b(?:bc1[a-z0-9]{25,62}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})\b")
USERNAME_RE = re.compile(r"@([A-Za-z0-9_]{4,32})")
_USER_ID_RE = re.compile(r"^\d{5,15}$")


def parse_entity(raw: str) -> dict:
    """Return {"type": ..., "value": normalized, "raw": raw}."""
    raw = raw or ""
    stripped = raw.strip()

    # 1. email
    email_match = EMAIL_RE.search(stripped)
    if email_match:
        return {"type": "email", "value": email_match.group(0).lower(), "raw": raw}

    # 2. eth wallet
    eth_match = ETH_RE.search(stripped)
    if eth_match:
        return {
            "type": "wallet",
            "value": eth_match.group(0),
            "chain": "ethereum",
            "raw": raw,
        }

    # 3. btc wallet
    btc_match = BTC_RE.search(stripped)
    if btc_match:
        return {
            "type": "wallet",
            "value": btc_match.group(0),
            "chain": "bitcoin",
            "raw": raw,
        }

    # 4. t.me link
    if "t.me/" in stripped:
        peer = normalize_peer(stripped)
        if peer:
            return {"type": "group", "value": peer, "raw": raw}

    # 5. @handle
    username_match = USERNAME_RE.search(stripped)
    if username_match:
        return {
            "type": "username",
            "value": username_match.group(1).lower(),
            "raw": raw,
        }

    # 6. phone
    phone_match = PHONE_RE.search(stripped)
    if phone_match:
        token = phone_match.group(0)
        digits = re.sub(r"\D", "", token)
        # Bare digit-runs are ambiguous (Telegram user ids are 5-15 digits too):
        # only treat as a phone when there is a '+' or a separator (#, space, dash,
        # parens) — otherwise fall through to the user_id branch below.
        phone_shaped = bool(re.search(r"[+\s\-()]", token))
        if len(digits) >= 7 and phone_shaped:
            value = f"+{digits}" if "+" in token else digits
            return {
                "type": "phone",
                "value": value,
                "digits": digits,
                "variants": [digits, f"+{digits}"],
                "raw": raw,
            }

    # 7. pure digits 5-15
    if _USER_ID_RE.match(stripped):
        return {"type": "user_id", "value": int(stripped), "raw": raw}

    # 8. text
    return {"type": "text", "value": stripped, "raw": raw}


# ---------------------------------------------------------------------------
# 2. Search + assembly helpers (all guarded; storage layer only)
# ---------------------------------------------------------------------------
def _payload(hit: Any) -> dict:
    return getattr(hit, "payload", None) or {}


def _hit_id(hit: Any) -> Optional[str]:
    value = str(getattr(hit, "id", "") or "")
    return value or None


def _hit_dedupe_key(hit: Any) -> str:
    hit_id = _hit_id(hit)
    if hit_id:
        return hit_id
    payload = _payload(hit)
    return f"{payload.get('chat_id')}:{payload.get('message_id')}"


async def _search_messages_multi(terms: list[str], limit: int) -> list[Any]:
    """Search messages for each term, merging hits preserving order."""
    merged: list[Any] = []
    seen: set[str] = set()
    for term in terms:
        if not term:
            continue
        try:
            hits = await db.search_messages(term, limit=limit)
        except Exception as exc:  # noqa: BLE001
            log(f"search_messages failed for {term!r}: {exc}")
            continue
        for hit in hits or []:
            key = _hit_dedupe_key(hit)
            if key in seen:
                continue
            seen.add(key)
            merged.append(hit)
    return merged


async def _profiles_for_terms(terms: list[str], limit: int) -> list[dict]:
    """Merge profile payloads for each term, deduped by user_id."""
    merged: list[dict] = []
    seen: set[Any] = set()
    for term in terms:
        if not term:
            continue
        try:
            hits = await db.search_profiles(term, limit=limit)
        except Exception as exc:  # noqa: BLE001
            log(f"search_profiles failed for {term!r}: {exc}")
            continue
        for hit in hits or []:
            payload = _payload(hit)
            if not payload:
                continue
            uid = payload.get("user_id")
            key = uid if uid is not None else _hit_id(hit)
            if key in seen:
                continue
            seen.add(key)
            merged.append(payload)
    return merged


async def _resolve_username_to_profile(value: str) -> Optional[dict]:
    """Find a profile whose username matches value (case-insensitive)."""
    try:
        hits = await db.search_profiles(value, limit=5)
    except Exception as exc:  # noqa: BLE001
        log(f"resolve_username failed for {value!r}: {exc}")
        return None
    target = value.lower()
    for hit in hits or []:
        payload = _payload(hit)
        if (payload.get("username") or "").lower() == target:
            return payload
    return None


async def _find_chat(chat_ref: str) -> Optional[dict]:
    """Locate a chat by t.me ref via message hits."""
    token = re.sub(r"^https?://", "", chat_ref or "")
    token = token.replace("t.me/", "").lstrip("@+/").strip()
    if not token:
        return None
    try:
        hits = await db.search_messages(token, limit=50)
    except Exception as exc:  # noqa: BLE001
        log(f"find_chat failed for {chat_ref!r}: {exc}")
        return None

    needle = token.lower()
    chat_id: Optional[int] = None
    chat_title = ""
    count = 0
    for hit in hits or []:
        payload = _payload(hit)
        title = (payload.get("chat_title") or "")
        username = (payload.get("chat_username") or payload.get("username") or "")
        if needle in title.lower() or needle in str(username).lower():
            count += 1
            if chat_id is None:
                chat_id = payload.get("chat_id")
                chat_title = title
    if chat_id is None:
        return None
    return {
        "chat_id": chat_id,
        "chat_title": chat_title,
        "ref": chat_ref,
        "message_hits": count,
    }


def _date_key(value: Any) -> str:
    """Tolerant ISO-ish sort key."""
    if value is None:
        return ""
    return str(value)[:19]


# ---------------------------------------------------------------------------
# 3. Dossier builder
# ---------------------------------------------------------------------------
def _message_view(payload: dict) -> dict:
    text = payload.get("text", "") or ""
    if len(text) > 500:
        text = text[:500]
    return {
        "chat_title": payload.get("chat_title", ""),
        "chat_id": payload.get("chat_id"),
        "sender_name": payload.get("sender_name", ""),
        "sender_username": payload.get("sender_username", ""),
        "date": payload.get("date", ""),
        "link": payload.get("link", ""),
        "text": text,
        "source_type": payload.get("source_type", "text"),
    }


def _empty_result(query: str, entity: dict) -> dict:
    return {
        "query": query,
        "entity": entity,
        "identity": {"profile": None, "bio_matches": [], "ids": [], "aliases": []},
        "profiles": [],
        "relations": [],
        "chats": [],
        "messages": [],
        "context_messages": [],
        "participants": [],
        "timeline": {"first_seen": "", "last_seen": ""},
        "stats": {
            "message_hits": 0,
            "profile_hits": 0,
            "relation_hits": 0,
            "chat_hits": 0,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "backend": "unknown",
    }


def _backend_mode() -> str:
    status_fn = getattr(db, "storage_status", None)
    if callable(status_fn):
        try:
            return status_fn().get("mode", "unknown")
        except Exception:  # noqa: BLE001
            return "unknown"
    return "unknown"


async def dossier(query: str, limit: Optional[int] = None) -> dict:
    limit = limit or config.QUERY_MAX_RESULTS
    entity = parse_entity(query)
    result = _empty_result(query, entity)
    result["backend"] = _backend_mode()

    profile: Optional[dict] = None
    relations: list[dict] = []
    message_hits: list[Any] = []
    profiles: list[dict] = []
    context_hits: list[Any] = []
    participants: list[dict] = []
    chats: list[dict] = []
    bio_matches: list[dict] = []

    try:
        etype = entity["type"]
        value = entity["value"]

        if etype == "username":
            profile = await _resolve_username_to_profile(value)
            if profile and profile.get("user_id") is not None:
                try:
                    relations = await db.get_relations_for(profile["user_id"])
                except Exception as exc:  # noqa: BLE001
                    log(f"get_relations_for failed: {exc}")
                try:
                    message_hits = await db.search_messages(
                        value, limit=limit, sender_username=value
                    )
                except Exception as exc:  # noqa: BLE001
                    log(f"search_messages (sender) failed: {exc}")
                    message_hits = []
            if not message_hits:
                message_hits = await _search_messages_multi([value], limit)
            if profile:
                profiles = [profile]

        elif etype == "user_id":
            uid = value
            try:
                profile = await db.get_profile(uid)
            except Exception as exc:  # noqa: BLE001
                log(f"get_profile failed: {exc}")
            if profile:
                profiles = [profile]
            try:
                relations = await db.get_relations_for(uid)
            except Exception as exc:  # noqa: BLE001
                log(f"get_relations_for failed: {exc}")
            terms = [str(uid)]
            if profile and profile.get("username"):
                terms.append(profile["username"])
            raw_hits = await _search_messages_multi(terms, limit)
            for hit in raw_hits:
                payload = _payload(hit)
                if payload.get("sender_id") == uid:
                    message_hits.append(hit)
                else:
                    context_hits.append(hit)

        elif etype in ("phone", "email", "wallet"):
            terms = entity.get("variants") or [value]
            message_hits = await _search_messages_multi(terms, limit)
            profiles = await _profiles_for_terms(terms, limit)
            needles = {str(value).lower()} | {str(v).lower() for v in terms}
            digits_needles = {
                re.sub(r"\D", "", n) for n in needles
            } - {""}
            digits_needles = {d for d in digits_needles if len(d) >= 6}
            for p in profiles:
                hay = " ".join(
                    str(p.get(k) or "")
                    for k in ("bio", "username", "display_name", "first_name", "last_name")
                ).lower()
                matched = any(n and n in hay for n in needles)
                if not matched and digits_needles:
                    # formatting-insensitive: "+357 99 123456" in bio matches "+35799123456"
                    hay_digits = re.sub(r"\D", "", hay)
                    matched = any(d in hay_digits for d in digits_needles)
                if matched:
                    bio_matches.append(p)

        elif etype == "group":
            chat = await _find_chat(value)
            if chat and chat.get("chat_id") is not None:
                chats = [
                    {
                        "chat_id": chat["chat_id"],
                        "chat_title": chat["chat_title"],
                        "count": chat.get("message_hits", 0),
                    }
                ]
                try:
                    message_hits = await db.search_messages(
                        entity["raw"], limit=limit, chat_id=chat["chat_id"]
                    )
                except Exception as exc:  # noqa: BLE001
                    log(f"search_messages (chat) failed: {exc}")
                    message_hits = []

        else:  # text
            message_hits = await _search_messages_multi([value], limit)
            profiles = await _profiles_for_terms([value], limit)

        # participants (group only, but harmless to compute when hits exist)
        if etype == "group" and message_hits:
            counts: dict[Any, dict] = {}
            for hit in message_hits:
                payload = _payload(hit)
                sid = payload.get("sender_id")
                key = sid if sid is not None else payload.get("sender_name", "")
                entry = counts.setdefault(
                    key,
                    {
                        "sender_id": sid,
                        "sender_name": payload.get("sender_name", ""),
                        "sender_username": payload.get("sender_username", ""),
                        "count": 0,
                    },
                )
                entry["count"] += 1
            participants = sorted(
                counts.values(), key=lambda e: e["count"], reverse=True
            )[:10]

        # chats aggregation from message hits
        if not chats and message_hits:
            chat_counts: dict[Any, dict] = {}
            for hit in message_hits:
                payload = _payload(hit)
                cid = payload.get("chat_id")
                if cid is None:
                    continue
                entry = chat_counts.setdefault(
                    cid,
                    {"chat_id": cid, "chat_title": payload.get("chat_title", ""), "count": 0},
                )
                entry["count"] += 1
            chats = sorted(
                chat_counts.values(), key=lambda c: c["count"], reverse=True
            )[:limit]

        # timeline
        dates = [
            _date_key(_payload(hit).get("date"))
            for hit in message_hits
            if _payload(hit).get("date")
        ]
        dates = [d for d in dates if d]
        timeline = {
            "first_seen": min(dates) if dates else "",
            "last_seen": max(dates) if dates else "",
        }

        # identity
        ids: list[Any] = []
        aliases: list[str] = []
        if profile:
            if profile.get("user_id") is not None:
                ids.append(profile["user_id"])
            if profile.get("username"):
                aliases.append(profile["username"])
        for p in profiles:
            if p.get("user_id") is not None and p["user_id"] not in ids:
                ids.append(p["user_id"])
            uname = p.get("username")
            if uname and uname not in aliases:
                aliases.append(uname)

        result["identity"] = {
            "profile": profile,
            "bio_matches": bio_matches,
            "ids": ids,
            "aliases": aliases,
        }
        result["profiles"] = profiles[:limit]
        result["relations"] = relations[:limit]
        result["chats"] = chats[:limit]
        result["messages"] = [_message_view(_payload(h)) for h in message_hits[:limit]]
        result["context_messages"] = [
            _message_view(_payload(h)) for h in context_hits[:10]
        ]
        result["participants"] = participants
        result["timeline"] = timeline
        result["stats"] = {
            "message_hits": len(message_hits),
            "profile_hits": len(profiles),
            "relation_hits": len(relations),
            "chat_hits": len(chats),
        }
        result["generated_at"] = datetime.now(timezone.utc).isoformat()
        result["backend"] = _backend_mode()

        metrics.inc("query_dossiers")
    except Exception as exc:  # noqa: BLE001
        log(f"dossier error: {exc}")
        result["error"] = str(exc)[:300]
        result["stats"] = {
            "message_hits": 0,
            "profile_hits": 0,
            "relation_hits": 0,
            "chat_hits": 0,
        }

    return result


# ---------------------------------------------------------------------------
# 4. Rendering + CLI
# ---------------------------------------------------------------------------
def render_text(result: dict) -> str:
    entity = result.get("entity", {}) or {}
    lines: list[str] = []
    lines.append(
        f"📌 DOSSIER for {entity.get('raw', result.get('query', ''))} "
        f"[{entity.get('type', '?')}]"
    )

    identity = result.get("identity", {}) or {}
    profile = identity.get("profile")
    lines.append("")
    lines.append("— Identity —")
    if profile:
        lines.append(
            f"  user_id={profile.get('user_id')} "
            f"@{profile.get('username', '')} "
            f"{profile.get('first_name', '')} {profile.get('last_name', '')}".rstrip()
        )
    else:
        lines.append("  (no profile resolved)")
    if identity.get("ids"):
        lines.append(f"  ids: {identity['ids']}")
    if identity.get("aliases"):
        lines.append(f"  aliases: {identity['aliases']}")
    if identity.get("bio_matches"):
        lines.append(f"  bio matches: {len(identity['bio_matches'])}")

    profiles = result.get("profiles", []) or []
    lines.append("")
    lines.append(f"— Profiles ({len(profiles)}) —")
    for p in profiles[:5]:
        lines.append(
            f"  {p.get('user_id')} @{p.get('username', '')} "
            f"{p.get('first_name', '')} {p.get('last_name', '')}".rstrip()
        )

    relations = result.get("relations", []) or []
    lines.append("")
    lines.append(f"— Relations ({len(relations)}) —")
    for rel in relations[:10]:
        lines.append(
            f"  {rel.get('edge_kind', '?')} "
            f"@{rel.get('a_username', rel.get('a_id'))} <-> "
            f"@{rel.get('b_username', rel.get('b_id'))} "
            f"w={rel.get('weight', 0)}"
        )

    chats = result.get("chats", []) or []
    lines.append("")
    lines.append(f"— Chats ({len(chats)}) —")
    for c in chats[:10]:
        lines.append(f"  {c.get('chat_title', '')} ({c.get('chat_id')}) x{c.get('count', 0)}")

    messages = result.get("messages", []) or []
    lines.append("")
    lines.append(f"— Messages ({len(messages)}) —")
    for m in messages[:15]:
        excerpt = (m.get("text", "") or "").replace("\n", " ")
        lines.append(
            f"  {m.get('date', '')} — {m.get('sender_name', '')} "
            f"in {m.get('chat_title', '')}: {excerpt}"
        )

    timeline = result.get("timeline", {}) or {}
    lines.append("")
    lines.append(
        f"— Timeline — first={timeline.get('first_seen', '')} "
        f"last={timeline.get('last_seen', '')}"
    )

    stats = result.get("stats", {}) or {}
    lines.append("")
    lines.append(
        f"— Stats — messages={stats.get('message_hits', 0)} "
        f"profiles={stats.get('profile_hits', 0)} "
        f"relations={stats.get('relation_hits', 0)} "
        f"chats={stats.get('chat_hits', 0)} "
        f"backend={result.get('backend', 'unknown')}"
    )

    if result.get("error"):
        lines.append("")
        lines.append(f"⚠️ error: {result['error']}")

    return "\n".join(lines)


def _cli(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="query", description="Natural-language OSINT dossier builder."
    )
    parser.add_argument("query", help="entity to investigate")
    parser.add_argument("--json", action="store_true", help="emit JSON output")
    parser.add_argument("--limit", type=int, default=None, help="max results per bucket")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 2

    try:
        result = asyncio.run(dossier(args.query, limit=args.limit))
    except Exception as exc:  # noqa: BLE001
        log(f"cli error: {exc}")
        result = _empty_result(args.query, parse_entity(args.query))
        result["error"] = str(exc)[:300]

    if args.json:
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(render_text(result) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
