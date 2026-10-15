"""OSINT profile builder + relation/network mapping for telegram-rag-bot.

Aggregates per-user stats from indexed messages, enriches them with live Telegram
profile data (bio, profile-photo hash), and maps co-membership/reply/mention
relations into the tg_bot_rag Qdrant collections.
"""

import asyncio
import hashlib
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from pyrogram import errors as tg_errors

import config
import database as db
from scraper import FloodGate, RateGate, telegram_call


def log(message: str) -> None:
    """Timestamped runtime log line (never called at import time)."""
    print(f"[profiles {time.strftime('%H:%M:%S')}] {message}", flush=True)


# ==========================================
# 1. PURE HELPERS (UNIT-TESTABLE, NO NETWORK)
# ==========================================
MENTION_REGEX = re.compile(r"@([A-Za-z0-9_]{4,32})")


def _parse_dt(value: str) -> Optional[datetime]:
    """Tolerant ISO-ish datetime parser; returns None if unparseable."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            continue
    return None


def _pair(a: int, b: int) -> tuple[int, int]:
    """Return a sorted (low, high) tuple for an unordered pair of ids."""
    return (a, b) if a <= b else (b, a)


def compute_common_chats(profile_a: dict, profile_b: dict) -> dict:
    """Return {"chat_ids": sorted intersection, "titles": titles for those ids}."""
    ids_a = list(profile_a.get("common_chat_ids") or [])
    ids_b = list(profile_b.get("common_chat_ids") or [])
    titles_a = list(profile_a.get("common_chat_titles") or [])
    titles_b = list(profile_b.get("common_chat_titles") or [])

    common = sorted(set(ids_a) & set(ids_b))
    if not common:
        return {"chat_ids": [], "titles": []}

    title_map: dict[int, str] = {}
    if len(ids_a) == len(titles_a):
        title_map.update({cid: t for cid, t in zip(ids_a, titles_a)})
    if len(ids_b) == len(titles_b):
        for cid, t in zip(ids_b, titles_b):
            title_map.setdefault(cid, t)

    titles = [title_map.get(cid, "") for cid in common]
    return {"chat_ids": common, "titles": titles}


# ==========================================
# 2. AGGREGATION FROM INDEXED MESSAGES
# ==========================================
async def _scroll_page(offset: Any) -> Any:
    """Scroll one page of the messages collection (payload only)."""
    client = db.get_qdrant()
    return await asyncio.to_thread(
        client.scroll,
        collection_name=config.MESSAGES_COLLECTION,
        limit=config.PROFILE_SCROLL_PAGE,
        offset=offset,
        with_payload=True,
        with_vectors=False,
    )


async def aggregate_message_stats(gate: FloodGate) -> dict:
    """Scroll the whole messages collection and aggregate per-sender stats."""
    users: dict[int, dict] = {}
    chat_members: dict[int, dict[int, int]] = defaultdict(dict)
    username_map: dict[str, int] = {}
    mentions_raw: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    mention_chats: dict[int, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    scanned = 0

    offset: Any = None
    while True:
        await gate.wait()
        try:
            points, next_offset = await _scroll_page(offset)
        except Exception as e:
            log(f"⚠️ scroll failed: {e}")
            break

        for point in points:
            payload = getattr(point, "payload", None) or {}
            sender_id = payload.get("sender_id")
            chat_id = payload.get("chat_id")
            if sender_id is None or chat_id is None:
                continue
            if sender_id == chat_id:
                continue

            scanned += 1
            entry = users.get(sender_id)
            if entry is None:
                entry = {
                    "message_count": 0,
                    "username": "",
                    "display_name": "",
                    "first_seen": "",
                    "last_seen": "",
                    "chats": {},
                    "reply_targets": {},
                    "mentions": {},
                }
                users[sender_id] = entry

            entry["message_count"] += 1

            username = payload.get("sender_username") or ""
            if username:
                entry["username"] = username
                username_map[username.lower()] = sender_id

            display_name = payload.get("sender_name") or ""
            if display_name:
                entry["display_name"] = display_name

            date_str = payload.get("date") or ""
            if date_str:
                if not entry["first_seen"]:
                    entry["first_seen"] = date_str
                    entry["last_seen"] = date_str
                else:
                    first_dt = _parse_dt(entry["first_seen"])
                    last_dt = _parse_dt(entry["last_seen"])
                    cur_dt = _parse_dt(date_str)
                    if first_dt and cur_dt and cur_dt < first_dt:
                        entry["first_seen"] = date_str
                    elif not first_dt and date_str < entry["first_seen"]:
                        entry["first_seen"] = date_str
                    if last_dt and cur_dt and cur_dt > last_dt:
                        entry["last_seen"] = date_str
                    elif not last_dt and date_str > entry["last_seen"]:
                        entry["last_seen"] = date_str

            chat_entry = entry["chats"].get(chat_id)
            if chat_entry is None:
                entry["chats"][chat_id] = {
                    "count": 1,
                    "title": payload.get("chat_title") or "",
                }
            else:
                chat_entry["count"] += 1
                if not chat_entry["title"] and payload.get("chat_title"):
                    chat_entry["title"] = payload["chat_title"]

            chat_members[chat_id][sender_id] = chat_members[chat_id].get(sender_id, 0) + 1

            reply_to = payload.get("reply_to_user_id")
            if reply_to is not None and reply_to != sender_id:
                rt = entry["reply_targets"].get(reply_to)
                if rt is None:
                    entry["reply_targets"][reply_to] = {"count": 1, "chats": {chat_id}}
                else:
                    rt["count"] += 1
                    rt["chats"].add(chat_id)

            text = payload.get("text") or ""
            if text:
                for match in MENTION_REGEX.findall(text):
                    key = match.lower()
                    mentions_raw[sender_id][key] += 1
                    mention_chats[sender_id][key].add(chat_id)

        if next_offset is None:
            break
        offset = next_offset

    # Second pass: resolve raw mentions to user_ids via username_map.
    for sender_id, raw in mentions_raw.items():
        entry = users.get(sender_id)
        if entry is None:
            continue
        for username_lower, count in raw.items():
            target_id = username_map.get(username_lower)
            if target_id is None or target_id == sender_id:
                continue
            m = entry["mentions"].get(username_lower)
            if m is None:
                entry["mentions"][username_lower] = {
                    "count": count,
                    "chats": set(mention_chats[sender_id][username_lower]),
                    "target_id": target_id,
                }
            else:
                m["count"] += count
                m["chats"].update(mention_chats[sender_id][username_lower])

    return {
        "users": users,
        "chat_members": {cid: dict(members) for cid, members in chat_members.items()},
        "username_map": username_map,
        "scanned_points": scanned,
    }


# ==========================================
# 3. PROFILE ENRICHMENT
# ==========================================
async def enrich_user(
    client, gate: FloodGate, user_id: int, existing: Optional[dict]
) -> dict:
    """Fetch fresh Telegram user info + profile-photo hash (or reuse fresh cache)."""
    if existing is not None:
        updated_at = _parse_dt(existing.get("updated_at", ""))
        if updated_at is not None:
            age = (datetime.now(timezone.utc) - updated_at).total_seconds()
            if age < config.PROFILE_REFRESH_INTERVAL_SECONDS:
                return {
                    "bio": existing.get("bio", ""),
                    "first_name": existing.get("first_name", ""),
                    "last_name": existing.get("last_name", ""),
                    "username": existing.get("username", ""),
                    "is_bot": existing.get("is_bot", False),
                    "is_premium": existing.get("is_premium", False),
                    "pfp_hash": existing.get("pfp_hash", ""),
                    "pfp_meta": existing.get("pfp_meta", {}),
                    "refreshed": False,
                }

    try:
        user = await telegram_call(gate, client.get_users, user_id)
    except Exception as e:
        log(f"⚠️ get_users failed for {user_id}: {e}")
        if existing is not None:
            # Transient network failure: keep cached profile data rather than wiping it.
            return {
                "bio": existing.get("bio", ""),
                "first_name": existing.get("first_name", ""),
                "last_name": existing.get("last_name", ""),
                "username": existing.get("username", ""),
                "is_bot": existing.get("is_bot", False),
                "is_premium": existing.get("is_premium", False),
                "pfp_hash": existing.get("pfp_hash", ""),
                "pfp_meta": existing.get("pfp_meta", {}),
                "refreshed": False,
            }
        return {
            "bio": "",
            "first_name": "",
            "last_name": "",
            "username": "",
            "is_bot": False,
            "is_premium": False,
            "pfp_hash": "",
            "pfp_meta": {},
            "refreshed": False,
        }

    bio = getattr(user, "bio", "") or ""
    pfp_hash = ""
    pfp_meta: dict = {}
    try:
        photos = await telegram_call(gate, client.get_profile_photos, user_id, limit=1)
        if photos:
            sizes = list(photos[0])
            if sizes:
                chosen = min(
                    sizes,
                    key=lambda s: (getattr(s, "file_size", None) or (s.width * s.height)),
                )
                blob = await telegram_call(
                    gate, client.download_media, chosen, in_memory=True
                )
                if blob is not None:
                    raw = blob.getvalue() if hasattr(blob, "getvalue") else bytes(blob)
                    pfp_hash = hashlib.sha256(raw).hexdigest()[
                        : config.PFP_HASH_SIZE // 4
                    ]
                    pfp_meta = {
                        "width": getattr(chosen, "width", 0),
                        "height": getattr(chosen, "height", 0),
                        "file_size": getattr(chosen, "file_size", 0),
                    }
    except Exception as e:
        log(f"⚠️ profile photo fetch failed for {user_id}: {e}")
        pfp_hash = ""
        pfp_meta = {}

    return {
        "bio": bio,
        "first_name": getattr(user, "first_name", "") or "",
        "last_name": getattr(user, "last_name", "") or "",
        "username": getattr(user, "username", "") or "",
        "is_bot": bool(getattr(user, "is_bot", False)),
        "is_premium": bool(getattr(user, "is_premium", False)),
        "pfp_hash": pfp_hash,
        "pfp_meta": pfp_meta,
        "refreshed": True,
    }


# ==========================================
# 4. RELATION BUILDING
# ==========================================
def assemble_relation_payloads(stats: dict) -> list[dict]:
    """Build relation payload dicts (no embeddings) from aggregation stats."""
    users = stats.get("users", {})
    chat_members = stats.get("chat_members", {})

    # --- co_member edges ---
    co_edges: dict[tuple[int, int], dict] = {}
    for chat_id, members in chat_members.items():
        qualifying = [
            (uid, count)
            for uid, count in members.items()
            if uid in users
        ]
        qualifying.sort(key=lambda x: x[1], reverse=True)
        qualifying = qualifying[: config.RELATION_MAX_USERS_PER_CHAT]
        ids = [uid for uid, _ in qualifying]
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = _pair(ids[i], ids[j])
                edge = co_edges.get((a, b))
                if edge is None:
                    edge = {"shared_chats": set(), "first_seen": "", "last_seen": ""}
                    co_edges[(a, b)] = edge
                edge["shared_chats"].add(chat_id)

    # --- replies edges ---
    reply_edges: dict[tuple[int, int], dict] = {}
    for uid, u in users.items():
        for target_id, rt in (u.get("reply_targets") or {}).items():
            if target_id not in users:
                continue
            a, b = _pair(uid, target_id)
            edge = reply_edges.get((a, b))
            if edge is None:
                edge = {"weight": 0, "chats": set()}
                reply_edges[(a, b)] = edge
            edge["weight"] += rt.get("count", 0)
            edge["chats"].update(rt.get("chats", set()))

    # --- mentions edges ---
    mention_edges: dict[tuple[int, int], dict] = {}
    for uid, u in users.items():
        for _username_lower, m in (u.get("mentions") or {}).items():
            target_id = m.get("target_id")
            if target_id is None or target_id not in users:
                continue
            a, b = _pair(uid, target_id)
            edge = mention_edges.get((a, b))
            if edge is None:
                edge = {"weight": 0, "chats": set()}
                mention_edges[(a, b)] = edge
            edge["weight"] += m.get("count", 0)
            edge["chats"].update(m.get("chats", set()))

    payloads: list[dict] = []

    for (a, b), edge in co_edges.items():
        shared = edge["shared_chats"]
        if len(shared) < config.RELATION_MIN_SHARED_CHATS:
            continue
        first_seen = ""
        last_seen = ""
        for uid in (a, b):
            u = users.get(uid, {})
            fs = u.get("first_seen", "")
            ls = u.get("last_seen", "")
            if fs and (not first_seen or fs < first_seen):
                first_seen = fs
            if ls and (not last_seen or ls > last_seen):
                last_seen = ls
        payloads.append(
            db.build_relation_payload(
                a,
                (users.get(a) or {}).get("username", ""),
                b,
                (users.get(b) or {}).get("username", ""),
                kind="co_member",
                weight=len(shared),
                evidence_chat_ids=sorted(shared),
                first_seen=first_seen,
                last_seen=last_seen,
            )
        )

    for (a, b), edge in reply_edges.items():
        first_seen = ""
        last_seen = ""
        for uid in (a, b):
            u = users.get(uid, {})
            fs = u.get("first_seen", "")
            ls = u.get("last_seen", "")
            if fs and (not first_seen or fs < first_seen):
                first_seen = fs
            if ls and (not last_seen or ls > last_seen):
                last_seen = ls
        payloads.append(
            db.build_relation_payload(
                a,
                (users.get(a) or {}).get("username", ""),
                b,
                (users.get(b) or {}).get("username", ""),
                kind="replies",
                weight=edge["weight"],
                evidence_chat_ids=sorted(edge["chats"]),
                first_seen=first_seen,
                last_seen=last_seen,
            )
        )

    for (a, b), edge in mention_edges.items():
        first_seen = ""
        last_seen = ""
        for uid in (a, b):
            u = users.get(uid, {})
            fs = u.get("first_seen", "")
            ls = u.get("last_seen", "")
            if fs and (not first_seen or fs < first_seen):
                first_seen = fs
            if ls and (not last_seen or ls > last_seen):
                last_seen = ls
        payloads.append(
            db.build_relation_payload(
                a,
                (users.get(a) or {}).get("username", ""),
                b,
                (users.get(b) or {}).get("username", ""),
                kind="mentions",
                weight=edge["weight"],
                evidence_chat_ids=sorted(edge["chats"]),
                first_seen=first_seen,
                last_seen=last_seen,
            )
        )

    # Deduplicate by (pair, kind), summing weights and merging evidence.
    deduped: dict[tuple[int, int, str], dict] = {}
    for p in payloads:
        key = (p.get("a_id"), p.get("b_id"), p.get("edge_kind"))
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = p
        else:
            existing["weight"] = existing.get("weight", 0) + p.get("weight", 0)
            merged = set(existing.get("evidence_chat_ids") or []) | set(
                p.get("evidence_chat_ids") or []
            )
            existing["evidence_chat_ids"] = sorted(merged)

    result = list(deduped.values())
    result.sort(key=lambda p: p.get("weight", 0), reverse=True)
    return result[:20000]


async def upsert_relations_batch(payloads: list[dict]) -> int:
    """Embed each relation payload's text and upsert via db.upsert_relations."""
    if not payloads:
        return 0
    try:
        texts = [db.build_relation_embed_text(p) for p in payloads]
        vectors = await db.embed_texts(texts)
        pairs = list(zip(payloads, vectors))
        return await db.upsert_relations(pairs)
    except Exception as e:
        log(f"⚠️ upsert_relations_batch failed: {e}")
        return 0


# ==========================================
# 5. FULL SWEEP
# ==========================================
async def build_profiles(client, gate: FloodGate, rate: RateGate) -> dict:
    """One full profile-building sweep. Returns JSON-safe stats."""
    started = time.time()
    errors = 0
    profiles_upserted = 0
    relations_upserted = 0

    try:
        stats = await aggregate_message_stats(gate)
    except Exception as e:
        log(f"⚠️ aggregate_message_stats failed: {e}")
        return {
            "scanned_points": 0,
            "qualifying_users": 0,
            "profiles_upserted": 0,
            "relations_upserted": 0,
            "errors": 1,
            "duration_seconds": time.time() - started,
        }

    users = stats.get("users", {})
    qualifying = [
        (uid, u)
        for uid, u in users.items()
        if u.get("message_count", 0) >= config.PROFILE_MIN_MESSAGES
    ]
    qualifying.sort(key=lambda x: x[1].get("message_count", 0), reverse=True)
    qualifying = qualifying[: config.PROFILE_MAX_USERS_PER_SWEEP]

    pending: list[tuple[int, dict, list[float]]] = []

    async def _flush() -> None:
        nonlocal profiles_upserted, pending
        if not pending:
            return
        try:
            n = await db.upsert_profiles(pending)
            profiles_upserted += n
        except Exception as e:
            log(f"⚠️ upsert_profiles failed: {e}")
        pending = []

    for idx, (uid, u) in enumerate(qualifying, start=1):
        try:
            await rate.acquire()
            existing = await db.get_profile(uid)
            info = await enrich_user(client, gate, uid, existing)

            common_chat_ids = sorted(u.get("chats", {}).keys())
            common_chat_titles = [
                u["chats"][cid].get("title", "") for cid in common_chat_ids
            ]

            reply_targets = u.get("reply_targets", {})
            top_interactions = sorted(
                reply_targets.items(),
                key=lambda kv: kv[1].get("count", 0),
                reverse=True,
            )[: config.PROFILE_TOP_INTERACTIONS]
            interactions = {str(cp): rt.get("count", 0) for cp, rt in top_interactions}

            payload = db.build_profile_payload(
                uid,
                username=info.get("username") or u.get("username", ""),
                first_name=info.get("first_name") or u.get("display_name", ""),
                last_name=info.get("last_name", ""),
                bio=info.get("bio", ""),
                pfp_hash=info.get("pfp_hash", ""),
                pfp_meta=info.get("pfp_meta", {}),
                is_bot=info.get("is_bot", False),
                is_premium=info.get("is_premium", False),
                common_chat_ids=common_chat_ids,
                common_chat_titles=common_chat_titles,
                message_count=u.get("message_count", 0),
                interactions=interactions,
                first_seen=u.get("first_seen", ""),
                last_seen=u.get("last_seen", ""),
            )

            text = db.build_profile_embed_text(payload)
            vectors = await db.embed_texts([text])
            pending.append((uid, payload, vectors[0]))

            if len(pending) >= 16:
                await _flush()
        except Exception as e:
            errors += 1
            log(f"⚠️ profile build failed for {uid}: {e}")

        if idx % 25 == 0:
            log(
                f"👤 profiles: {idx}/{len(qualifying)} "
                f"(upserted={profiles_upserted}, errors={errors})"
            )

    await _flush()

    try:
        payloads = assemble_relation_payloads(stats)
        relations_upserted = await upsert_relations_batch(payloads)
    except Exception as e:
        errors += 1
        log(f"⚠️ relation assembly/upsert failed: {e}")

    return {
        "scanned_points": stats.get("scanned_points", 0),
        "qualifying_users": len(qualifying),
        "profiles_upserted": profiles_upserted,
        "relations_upserted": relations_upserted,
        "errors": errors,
        "duration_seconds": time.time() - started,
    }


# ==========================================
# 6. BACKGROUND LOOP + QUERY HELPERS
# ==========================================
async def profile_sweep(client, control) -> None:
    """Forever: run build_profiles, then sleep, honoring pause/stop."""
    await asyncio.sleep(30)

    gate = getattr(control, "gate", None) or FloodGate()
    rate = RateGate(config.TG_OPS_PER_SECOND, config.TG_OPS_BURST)

    while True:
        if getattr(control, "stop_requested", False):
            return

        while getattr(control, "paused", False):
            await asyncio.sleep(5)
            if getattr(control, "stop_requested", False):
                return

        try:
            result = await build_profiles(client, gate, rate)
            log(f"✅ profile sweep complete: {result}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"⚠️ profile sweep failed: {e}")

        slept = 0.0
        while slept < config.PROFILE_REFRESH_INTERVAL_SECONDS:
            if getattr(control, "stop_requested", False):
                return
            if getattr(control, "paused", False):
                break
            await asyncio.sleep(5)
            slept += 5


async def whois(user_id: int) -> Optional[dict]:
    """Return {"profile": ..., "relations": [...]} or None if no profile."""
    profile = await db.get_profile(user_id)
    if profile is None:
        return None
    relations = await db.get_relations_for(user_id)
    return {"profile": profile, "relations": relations}


async def search_users(query: str, limit: int = 10) -> list[dict]:
    """Search indexed profiles by natural-language query."""
    return await db.search_profiles(query, limit)


async def introduce(user_a_id: int, user_b_id: int) -> dict:
    """Matchmaking payload for bot.py."""
    profile_a = await db.get_profile(user_a_id)
    profile_b = await db.get_profile(user_b_id)

    if profile_a is not None and profile_b is not None:
        common = compute_common_chats(profile_a, profile_b)
    else:
        common = {"chat_ids": [], "titles": []}

    relation = None
    if profile_a is not None:
        try:
            relations = await db.get_relations_for(user_a_id)
            for rel in relations:
                if rel.get("a_id") == user_b_id or rel.get("b_id") == user_b_id:
                    relation = rel
                    break
        except Exception as e:
            log(f"⚠️ introduce relation lookup failed: {e}")

    return {
        "a": profile_a,
        "b": profile_b,
        "common_chats": common,
        "relation": relation,
    }
