"""Global interaction bot for telegram-rag-bot (Pyrogram bot client).

Natural-language gateway over the tg_bot_rag vector database: casual public
answers anywhere, deep OSINT gated behind the private admin challenge/response
(/auth), plus a control panel for the scraper, target manager, and DB metrics.
"""

import asyncio
import re
import time
from typing import Any, Optional

from openai import AsyncOpenAI
from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
)

import config
import profiles
import scraper
import storage as db
from rbac import get_auth
from scraper import TargetManager


def log(message: str) -> None:
    print(f"[bot {time.strftime('%H:%M:%S')}] {message}", flush=True)


ADMIN_USER_ID: int = 0
_username_re = re.compile(r"@([A-Za-z0-9_]{4,32})")

_llm_client: Optional[AsyncOpenAI] = None


def get_llm() -> AsyncOpenAI:
    """Lazily build the DeepSeek AsyncOpenAI client (module singleton)."""
    global _llm_client
    if _llm_client is None:
        _llm_client = AsyncOpenAI(
            api_key=config.DEEPSEEK_API_KEY,
            base_url=config.DEEPSEEK_BASE_URL,
        )
    return _llm_client


# ---------------------------------------------------------------------------
# 1. Shared helpers
# ---------------------------------------------------------------------------

def _is_admin(user_id: int) -> bool:
    return (
        bool(ADMIN_USER_ID)
        and user_id == ADMIN_USER_ID
        and get_auth().is_authenticated(user_id)
    )


def _extract_handles(text: str) -> list[str]:
    """All @handles in text, excluding the bot's own, deduped, order preserved."""
    own = (getattr(config, "BOT_USERNAME", "") or "").lstrip("@").lower()
    seen: set[str] = set()
    out: list[str] = []
    for match in _username_re.findall(text or ""):
        handle = match.lower()
        if handle == own or handle in seen:
            continue
        seen.add(handle)
        out.append(handle)
    return out


def _fmt_dt(value: str) -> str:
    """Best-effort shortening of ISO-ish strings for display."""
    if not value:
        return ""
    return str(value)[:16].replace("T", " ")


async def _deepseek_chat(system: str, user: str, max_tokens: int = None) -> str:
    """Call DeepSeek; never raise — return a graceful fallback on failure."""
    try:
        kwargs: dict[str, Any] = {
            "model": config.DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        resp = await asyncio.wait_for(
            get_llm().chat.completions.create(**kwargs),
            timeout=config.DEEPSEEK_TIMEOUT_SECONDS,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:  # noqa: BLE001
        return f"(LLM unavailable: {e})"


async def _resolve_handle(handle: str) -> Optional[int]:
    """Exact-match username lookup against the profiles collection."""
    from qdrant_client import models as qmodels

    value = (handle or "").lstrip("@").lower()
    if not value:
        return None

    def _do_scroll():
        return db.get_qdrant().scroll(
            collection_name=config.PROFILES_COLLECTION,
            scroll_filter=qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="username",
                        match=qmodels.MatchValue(value=value),
                    )
                ]
            ),
            limit=1,
            with_payload=True,
            with_vectors=False,
        )

    try:
        reader = getattr(db, "_qdrant_read", None)
        if reader is not None:
            points, _ = await reader(([], None), _do_scroll)
        else:
            points, _ = await asyncio.to_thread(_do_scroll)
    except Exception as e:  # noqa: BLE001
        log(f"_resolve_handle failed for @{value}: {e}")
        return None

    if not points:
        return None
    payload = getattr(points[0], "payload", None) or {}
    return payload.get("user_id")


def _profile_line(p: dict) -> str:
    """One-line summary of a profile payload dict (real tg_bot_rag shape)."""
    name = p.get("display_name") or "Unknown"
    username = p.get("username") or ""
    handle = f" (@{username})" if username else ""
    msgs = p.get("message_count", 0) or 0
    groups = len(p.get("common_chat_ids") or [])
    return f"{name}{handle} — {msgs} msgs, {groups} groups"


def _truncate(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[:n] + "..."


# ---------------------------------------------------------------------------
# 2. RAG answers
# ---------------------------------------------------------------------------

async def rag_answer(query: str, deep: bool) -> str:
    hits = await db.search_messages(query, limit=(15 if deep else 10))

    lines: list[str] = []
    for h in hits or []:
        payload = getattr(h, "payload", None) or {}
        sender = payload.get("sender_name") or "Unknown"
        username = payload.get("sender_username") or ""
        handle = f" (@{username})" if username else ""
        chat_title = payload.get("chat_title") or "?"
        text = _truncate(payload.get("text", ""), 300)
        lines.append(f"- [{sender}{handle} in '{chat_title}': {text}")
    context = "\n".join(lines) if lines else "(no matching messages)"

    if deep:
        prof_hits = await db.search_profiles(query, limit=5)
        prof_lines: list[str] = []
        for p in prof_hits or []:
            payload = getattr(p, "payload", None) or {}
            name = payload.get("display_name") or "Unknown"
            username = payload.get("username") or "?"
            bio = _truncate(payload.get("bio", ""), 200)
            groups = len(payload.get("common_chat_ids") or [])
            prof_lines.append(f"- {name} (@{username}) bio: {bio} groups: {groups}")
        if prof_lines:
            context += "\n\nMATCHING PROFILES\n" + "\n".join(prof_lines)

    system = (
        "You are an advanced Telegram Intelligence Assistant. "
        "Answer STRICTLY from the provided context. Do not fabricate facts. "
        "Respond in the language of the user's query (English or Russian)."
    )
    if deep:
        system += (
            " This is a DEEP analysis request: leverage the matching profiles "
            "block to enrich the answer with identity and network context."
        )
    else:
        system += " Be brief (<=120 words) and casual."

    user = f"CONTEXT:\n{context}\n\nQUESTION: {query}"
    return await _deepseek_chat(system, user)


async def casual_whois(target_label: str, user_id: Optional[int]) -> str:
    if user_id is None:
        return f"No indexed profile for {target_label} yet."

    data = await profiles.whois(user_id)
    if not data:
        return f"No indexed profile for {target_label} yet."

    profile = data.get("profile", data) if isinstance(data, dict) else {}
    name = profile.get("display_name") or target_label
    username = profile.get("username")
    handle = f" (@{username})" if username else ""
    msgs = profile.get("message_count", 0) or 0
    groups = len(profile.get("common_chat_ids") or [])
    last_seen = _fmt_dt(profile.get("last_seen", ""))

    out = [f"👤 {name}{handle}"]
    out.append(f"• Indexed messages: {msgs}")
    out.append(f"• Active in {groups} groups")
    if last_seen:
        out.append(f"• Last seen: {last_seen}")

    if username:
        try:
            hits = await db.search_messages(username, limit=5)
            titles: list[str] = []
            for h in hits or []:
                payload = getattr(h, "payload", None) or {}
                t = payload.get("chat_title")
                if t and t not in titles:
                    titles.append(t)
            if titles:
                out.append("• Seen in: " + ", ".join(titles[:5]))
        except Exception as e:  # noqa: BLE001
            log(f"casual_whois context lookup failed: {e}")

    return "\n".join(out)


async def deep_whois(user_id: int) -> str:
    data = await profiles.whois(user_id)
    if not data:
        return "No indexed profile for this user yet."

    profile = data.get("profile", {}) if isinstance(data, dict) else {}
    name = profile.get("display_name") or profile.get("first_name") or "Unknown"
    username = profile.get("username")
    handle = f" (@{username})" if username else ""

    out: list[str] = [f"🕵️ DEEP DOSSIER — {name}{handle}"]

    out.append("\n**Identity**")
    out.append(f"• ID: {user_id}")
    if profile.get("bio"):
        out.append(f"• Bio: {_truncate(profile.get('bio', ''), 300)}")
    first_seen = _fmt_dt(profile.get("first_seen", ""))
    last_seen = _fmt_dt(profile.get("last_seen", ""))
    if first_seen:
        out.append(f"• First seen: {first_seen}")
    if last_seen:
        out.append(f"• Last seen: {last_seen}")

    pfp_hash = profile.get("pfp_hash") or data.get("pfp_hash")
    if pfp_hash:
        out.append("\n**PFP HASH**")
        out.append(f"• {pfp_hash}")
        pfp_meta = profile.get("pfp_meta") or data.get("pfp_meta")
        if pfp_meta:
            out.append(f"• meta: {pfp_meta}")

    chat_titles = profile.get("common_chat_titles") or []
    if chat_titles:
        out.append("\n**Groups**")
        for title in chat_titles[:15]:
            out.append(f"• {title}")

    interactions = profile.get("interactions") or {}
    if interactions:
        out.append("\n**Top interactions**")
        top = sorted(interactions.items(), key=lambda kv: kv[1], reverse=True)[:8]
        ids: list[int] = []
        for key, _count in top:
            try:
                ids.append(int(key))
            except (TypeError, ValueError):
                continue
        names: dict[int, str] = {}
        try:
            bulk = await db.get_profiles_bulk(ids)
            for uid, p in (bulk or {}).items():
                names[uid] = p.get("display_name") or p.get("username") or "?"
        except Exception as e:  # noqa: BLE001
            log(f"deep_whois bulk lookup failed: {e}")
        for key, count in top:
            try:
                uid = int(key)
            except (TypeError, ValueError):
                out.append(f"• {key} ({count})")
                continue
            label = names.get(uid) or str(uid)
            out.append(f"• {label} ({count})")

    relations = data.get("relations") or []
    if relations:
        out.append("\n**Relations**")
        for r in relations[:10]:
            if not isinstance(r, dict):
                continue
            kind = r.get("edge_kind", "?")
            a = r.get("a_username") or r.get("a_id")
            b = r.get("b_username") or r.get("b_id")
            weight = r.get("weight", "")
            out.append(f"• {kind}: @{a} <-> @{b} ({weight})")

    if getattr(config, "DEEPSEEK_API_KEY", ""):
        summary = await _deepseek_chat(
            "You are an intelligence analyst. Write a concise executive summary "
            "(2-4 sentences) of the following Telegram user dossier. No fabrication.",
            "\n".join(out),
            max_tokens=300,
        )
        out.append("\n**Executive summary**")
        out.append(summary)

    return "\n".join(out)


async def casual_introduce(
    a_id: Optional[int],
    b_id: Optional[int],
    a_label: str,
    b_label: str,
) -> str:
    if a_id is None or b_id is None:
        return (
            f"I couldn't resolve both parties ({a_label}, {b_label}). "
            "Try using their @usernames."
        )

    try:
        intro = await profiles.introduce(a_id, b_id)
    except Exception as e:  # noqa: BLE001
        log(f"casual_introduce failed: {e}")
        intro = None

    common: dict[str, Any] = {}
    if isinstance(intro, dict):
        common = intro.get("common_chats") or {}
    n = len(common.get("chat_ids") or []) if isinstance(common, dict) else len(common)

    if n == 0:
        return f"{a_label} and {b_label} don't appear to share any indexed communities."

    return (
        f"{a_label} and {b_label} are both active in {n} shared communities. "
        "They likely have overlapping interests — worth an introduction."
    )


async def deep_introduce(a_id: int, b_id: int) -> str:
    intro = await profiles.introduce(a_id, b_id)
    if not intro:
        return "No indexed data to introduce these two users."

    out: list[str] = ["🤝 DEEP INTRODUCTION"]

    a_prof = intro.get("a") or intro.get("profile_a") or {}
    b_prof = intro.get("b") or intro.get("profile_b") or {}
    if a_prof:
        out.append(f"• A: {_profile_line(a_prof)}")
    if b_prof:
        out.append(f"• B: {_profile_line(b_prof)}")

    common = intro.get("common_chats") or {}
    titles = common.get("titles") if isinstance(common, dict) else None
    chat_ids = common.get("chat_ids") if isinstance(common, dict) else None
    if titles or chat_ids:
        out.append("\n**Common communities**")
        for title in (titles or [])[:15]:
            out.append(f"• {title}")
        if not titles and chat_ids:
            out.append(f"• {len(chat_ids)} shared communities (titles not indexed)")

    relation = intro.get("relation")
    if isinstance(relation, dict):
        out.append("\n**Relation**")
        out.append(
            f"• {relation.get('edge_kind', '?')} "
            f"(weight {relation.get('weight', '')})"
        )

    if getattr(config, "DEEPSEEK_API_KEY", ""):
        text = await _deepseek_chat(
            "You are a warm, professional networker. Write a 2-3 sentence "
            "introduction connecting the two people below, plus one suggested "
            "opener. Base it strictly on the provided data.",
            "\n".join(out),
            max_tokens=300,
        )
        out.append("\n**Suggested introduction**")
        out.append(text)

    return "\n".join(out)


# ---------------------------------------------------------------------------
# 3. NLP routing
# ---------------------------------------------------------------------------

_WHOIS_PATTERNS = ("who is", "tell me about", "найди", "про кого")
_INTRO_PATTERNS = ("introduce", "познакомь", "connect us", "match")


def _classify(text: str) -> tuple[str, dict]:
    lowered = (text or "").lower()
    handles = _extract_handles(text or "")

    if any(p in lowered for p in _INTRO_PATTERNS):
        return "introduce", {"handles": handles}

    if any(p in lowered for p in _WHOIS_PATTERNS):
        return "whois", {"handles": handles}

    stripped = lowered.strip()
    if handles and len(stripped) <= 40 and stripped.startswith("@"):
        return "whois", {"handles": handles}

    return "search", {"query": text}


async def handle_nlp(
    client: Client,
    message,
    text: str,
    deep_allowed: bool,
) -> None:
    own = (getattr(config, "BOT_USERNAME", "") or "").lstrip("@")
    cleaned = text or ""
    if own:
        cleaned = re.sub(rf"@{re.escape(own)}\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip().lstrip(",.!?:;-— ").strip()

    intent, params = _classify(cleaned)

    try:
        if intent == "whois":
            handles = params.get("handles") or []
            if not handles:
                answer = "Who would you like to know about? Mention an @username."
            else:
                handle = handles[0]
                uid = await _resolve_handle(handle)
                if deep_allowed and uid is not None:
                    if any(w in cleaned.lower() for w in ("everything", "dossier", "всё про", "все про")):
                        import query as _query

                        answer = _query.render_text(await _query.dossier(cleaned))
                    else:
                        answer = await deep_whois(uid)
                else:
                    answer = await casual_whois(f"@{handle}", uid)
                    if uid is not None:
                        answer += (
                            "\n\n🔒 Deep profile available — authenticate with "
                            "/auth in my private chat."
                        )

        elif intent == "introduce":
            handles = params.get("handles") or []
            if len(handles) < 2:
                answer = "Mention two @usernames to introduce them."
            else:
                a_id = await _resolve_handle(handles[0])
                b_id = await _resolve_handle(handles[1])
                if deep_allowed and a_id is not None and b_id is not None:
                    answer = await deep_introduce(a_id, b_id)
                else:
                    answer = await casual_introduce(
                        a_id, b_id, f"@{handles[0]}", f"@{handles[1]}"
                    )
                    if a_id is not None and b_id is not None:
                        answer += (
                            "\n\n🔒 Deep introduction available — authenticate "
                            "with /auth in my private chat."
                        )

        else:
            answer = await rag_answer(params.get("query", cleaned), deep_allowed)

    except Exception as e:  # noqa: BLE001
        log(f"handle_nlp failed: {e}")
        answer = "Sorry, something went wrong while processing your request."

    try:
        await message.reply_text(answer)
    except Exception as e:  # noqa: BLE001
        log(f"reply_text failed: {e}")

# ---------------------------------------------------------------------------
# 4. Panel builders
# ---------------------------------------------------------------------------

_LAST_SWEEP: Optional[str] = None
_SWEEP_TASK: Optional[asyncio.Task] = None


def _panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 Status", callback_data="p:status"),
                InlineKeyboardButton("🗄 DB metrics", callback_data="p:db"),
            ],
            [
                InlineKeyboardButton("▶️ Resume scraper", callback_data="p:resume"),
                InlineKeyboardButton("⏸ Pause scraper", callback_data="p:pause"),
            ],
            [
                InlineKeyboardButton("🎯 Targets", callback_data="p:targets"),
                InlineKeyboardButton("🔄 Refresh", callback_data="p:refresh"),
            ],
        ]
    )


def _usage_bar(ratio: float, width: int = 10) -> str:
    ratio = max(0.0, min(1.0, float(ratio or 0.0)))
    filled = int(round(ratio * width))
    return "█" * filled + "░" * (width - filled)


async def panel_snapshot() -> str:
    """Compact markdown snapshot of scraper, targets and DB metrics (<=30 lines)."""
    lines: list[str] = ["**🛰 Control panel**"]

    try:
        status = scraper.get_control().status() or {}
    except Exception as e:  # noqa: BLE001
        log(f"panel_snapshot status failed: {e}")
        status = {}

    paused = status.get("paused")
    reason = status.get("pause_reason") or status.get("reason") or ""
    uptime = status.get("uptime") or status.get("uptime_hms") or "-"
    workers = status.get("workers_active", status.get("workers", "-"))
    queue = status.get("queue_depth", status.get("queue", "-"))
    counters = status.get("counters") or {}
    flood = status.get("flood_events", "-")
    gate = status.get("gate_remaining", status.get("gate", "-"))

    lines.append(f"• Scraper: {'⏸ paused' if paused else '▶️ running'}"
                 + (f" ({reason})" if reason else ""))
    lines.append(f"• Uptime: {uptime} | workers: {workers} | queue: {queue}")
    if counters:
        cparts = ", ".join(f"{k}={v}" for k, v in list(counters.items())[:6])
        lines.append(f"• Counters: {cparts}")
    lines.append(f"• Flood events: {flood} | gate remaining: {gate}")

    try:
        tm = TargetManager()
        tm.load()
        summary = tm.summary() or {}
        total = summary.get("total", len(tm.all()))
        joined = summary.get("joined", "-")
        pending = summary.get("pending", "-")
        errors = summary.get("error", summary.get("errors", "-"))
        lines.append(f"• Targets: {total} total | joined {joined} | pending {pending} | err {errors}")
    except Exception as e:  # noqa: BLE001
        log(f"panel_snapshot targets failed: {e}")

    try:
        stats = await db.collection_stats() or {}
        total_points = stats.get("total_points", "-")
        lines.append(f"• DB total points: {total_points}")
        for name, info in stats.items():
            if name in ("messages_usage", "total_points"):
                continue
            if not isinstance(info, dict):
                continue
            pts = info.get("points")
            exists = info.get("exists", True)
            lines.append(f"  - {name}: {pts if exists else 'n/a'}")
        usage = stats.get("messages_usage") or {}
        ratio = usage.get("usage_ratio")
        if ratio is not None:
            lines.append(
                f"• Messages usage: {_usage_bar(ratio)} {float(ratio) * 100:.0f}% "
                f"(max {usage.get('max_vectors', '?')})"
            )
    except Exception as e:  # noqa: BLE001
        log(f"panel_snapshot db failed: {e}")

    return "\n".join(lines[:30])


async def _reply_chunked(message, text: str) -> None:
    """Reply, splitting on newlines into <=4000 char chunks."""
    text = text or ""
    if len(text) <= 4000:
        await message.reply_text(text)
        return
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 4000:
            await message.reply_text(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await message.reply_text(chunk)


# ---------------------------------------------------------------------------
# 5. Handler registration
# ---------------------------------------------------------------------------

_PRIVATE_COMMANDS = [
    "start", "auth", "logout", "panel", "status", "dbstats",
    "scrape", "join", "targets", "whois", "introduce", "sweep",
    "watch", "dossier",
]


def register_handlers(client: Client, admin_user_id: int) -> None:
    global ADMIN_USER_ID
    ADMIN_USER_ID = int(admin_user_id or 0)

    # -- private: /start ---------------------------------------------------
    @client.on_message(filters.command("start") & filters.private)
    async def _cmd_start(client, message):  # noqa: ARG001
        try:
            uid = message.from_user.id
            if uid == ADMIN_USER_ID:
                if get_auth().is_authenticated(uid):
                    await message.reply_text(
                        await panel_snapshot(), reply_markup=_panel_keyboard()
                    )
                else:
                    await message.reply_text(
                        "🔒 Admin detected but not authenticated.\n"
                        "Send /auth to begin the challenge/response flow."
                    )
            else:
                own = (getattr(config, "BOT_USERNAME", "") or "").lstrip("@")
                await message.reply_text(
                    "I'm an intelligence assistant. Tag me in a group with a "
                    "question, or use inline mode: "
                    f"`@{own} <query>`.\n"
                    "Casual answers are public; deep OSINT requires admin auth."
                )
        except Exception as e:  # noqa: BLE001
            log(f"/start failed: {e}")

    # -- private: /auth ----------------------------------------------------
    @client.on_message(filters.command("auth") & filters.private)
    async def _cmd_auth(client, message):  # noqa: ARG001
        try:
            uid = message.from_user.id
            if uid != ADMIN_USER_ID:
                await message.reply_text("⛔ Not authorized.")
                return
            ch = get_auth().issue_challenge(uid)
            nonce = ch.get("nonce", "?")
            expires = ch.get("expires_in", "?")
            how = ch.get("how", "")
            await message.reply_text(
                "🔐 Challenge issued.\n"
                f"Nonce: `{nonce}`\n"
                f"Expires in: {expires}s\n\n"
                "On the server run:\n"
                f"`python rbac.py respond {nonce}`\n\n"
                "Then send me the response code here."
                + (f"\n\n{how}" if how else "")
            )
        except Exception as e:  # noqa: BLE001
            log(f"/auth failed: {e}")

    # -- private: /logout --------------------------------------------------
    @client.on_message(filters.command("logout") & filters.private)
    async def _cmd_logout(client, message):  # noqa: ARG001
        try:
            uid = message.from_user.id
            if uid != ADMIN_USER_ID:
                await message.reply_text("⛔ Not authorized.")
                return
            revoked = get_auth().revoke(uid)
            await message.reply_text(
                "✅ Session revoked." if revoked else "ℹ️ No active session."
            )
        except Exception as e:  # noqa: BLE001
            log(f"/logout failed: {e}")

    # -- private text router ----------------------------------------------
    @client.on_message(
        filters.private & filters.text & ~filters.command(_PRIVATE_COMMANDS)
    )
    async def _private_text(client, message):
        try:
            uid = message.from_user.id
            text = message.text or ""

            if uid == ADMIN_USER_ID:
                if not get_auth().is_authenticated(uid):
                    ok, reason = get_auth().verify_response(uid, text)
                    if ok:
                        status = get_auth().session_status(uid) or {}
                        ttl = status.get("ttl", status.get("expires_in", 0)) or 0
                        await message.reply_text(
                            f"✅ Authenticated. Session valid {int(ttl) // 60} min. "
                            "Use /panel."
                        )
                    else:
                        await message.reply_text(
                            "🔒 Not authenticated. Send the response code from "
                            f"`python rbac.py respond <nonce>`.\n({reason})"
                        )
                    return
                await handle_nlp(client, message, text, deep_allowed=True)
                return

            await message.reply_text(
                "I answer in groups when tagged, or via inline mode: "
                "`@botusername <query>`."
            )
        except Exception as e:  # noqa: BLE001
            log(f"private text router failed: {e}")

    # -- admin commands ----------------------------------------------------
    def _locked(uid: int) -> bool:
        return not get_auth().is_authenticated(uid)

    @client.on_message(filters.command("panel") & filters.private)
    async def _cmd_panel(client, message):  # noqa: ARG001
        try:
            uid = message.from_user.id
            if uid != ADMIN_USER_ID:
                return
            if _locked(uid):
                await message.reply_text("🔒 Locked: authenticate with /auth first.")
                return
            await message.reply_text(
                await panel_snapshot(), reply_markup=_panel_keyboard()
            )
        except Exception as e:  # noqa: BLE001
            log(f"/panel failed: {e}")

    @client.on_message(filters.command("status") & filters.private)
    async def _cmd_status(client, message):  # noqa: ARG001
        try:
            uid = message.from_user.id
            if uid != ADMIN_USER_ID:
                return
            if _locked(uid):
                await message.reply_text("🔒 Locked: authenticate with /auth first.")
                return
            status = scraper.get_control().status() or {}
            lines = ["**Scraper status**"]
            for k, v in status.items():
                lines.append(f"• {k}: {v}")
            lines.append(f"• last sweep: {_LAST_SWEEP or '-'}")
            await _reply_chunked(message, "\n".join(lines))
        except Exception as e:  # noqa: BLE001
            log(f"/status failed: {e}")

    @client.on_message(filters.command("dbstats") & filters.private)
    async def _cmd_dbstats(client, message):  # noqa: ARG001
        try:
            uid = message.from_user.id
            if uid != ADMIN_USER_ID:
                return
            if _locked(uid):
                await message.reply_text("🔒 Locked: authenticate with /auth first.")
                return
            stats = await db.collection_stats() or {}
            lines = ["**🗄 DB metrics**"]
            for name, info in stats.items():
                if name in ("messages_usage", "total_points"):
                    continue
                if not isinstance(info, dict):
                    continue
                pts = info.get("points")
                exists = info.get("exists", True)
                lines.append(f"• {name}: {pts if exists else 'n/a'}")
            lines.append(f"• total: {stats.get('total_points', '-')}")
            usage = stats.get("messages_usage") or {}
            ratio = usage.get("usage_ratio")
            if ratio is not None:
                lines.append(
                    f"• usage: {_usage_bar(ratio)} {float(ratio) * 100:.0f}% "
                    f"(max {usage.get('max_vectors', '?')})"
                )
            await _reply_chunked(message, "\n".join(lines))
        except Exception as e:  # noqa: BLE001
            log(f"/dbstats failed: {e}")

    @client.on_message(filters.command("scrape") & filters.private)
    async def _cmd_scrape(client, message):  # noqa: ARG001
        try:
            uid = message.from_user.id
            if uid != ADMIN_USER_ID:
                return
            if _locked(uid):
                await message.reply_text("🔒 Locked: authenticate with /auth first.")
                return
            parts = (message.text or "").split()
            arg = parts[1].lower() if len(parts) > 1 else ""
            control = scraper.get_control()
            if arg in ("pause", "stop"):
                control.pause("admin")
                await message.reply_text("⏸ Scraper paused.")
            elif arg in ("resume", "start"):
                control.resume()
                await message.reply_text("▶️ Scraper resumed.")
            else:
                await message.reply_text("Usage: /scrape <start|pause|resume>")
        except Exception as e:  # noqa: BLE001
            log(f"/scrape failed: {e}")

    @client.on_message(filters.command("join") & filters.private)
    async def _cmd_join(client, message):  # noqa: ARG001
        try:
            uid = message.from_user.id
            if uid != ADMIN_USER_ID:
                return
            if _locked(uid):
                await message.reply_text("🔒 Locked: authenticate with /auth first.")
                return
            parts = (message.text or "").split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                await message.reply_text("Usage: /join <peer>")
                return
            peer = parts[1].strip()
            tm = TargetManager()
            tm.load()
            added = await tm.add(peer)
            if added:
                await message.reply_text(
                    "✅ queued — the userbot joins it on the next pass."
                )
            else:
                await message.reply_text("ℹ️ already in target list.")
        except Exception as e:  # noqa: BLE001
            log(f"/join failed: {e}")

    @client.on_message(filters.command("targets") & filters.private)
    async def _cmd_targets(client, message):  # noqa: ARG001
        try:
            uid = message.from_user.id
            if uid != ADMIN_USER_ID:
                return
            if _locked(uid):
                await message.reply_text("🔒 Locked: authenticate with /auth first.")
                return
            tm = TargetManager()
            tm.load()
            rows = tm.all() or []
            if not rows:
                await message.reply_text("No targets configured.")
                return
            lines = ["**🎯 Targets**"]
            for t in rows:
                lines.append(
                    f"• {t.get('state', '?')} | {t.get('peer', '?')} | "
                    f"{t.get('title', '')} | {t.get('note', '')}"
                )
            await _reply_chunked(message, "\n".join(lines))
        except Exception as e:  # noqa: BLE001
            log(f"/targets failed: {e}")

    @client.on_message(filters.command("whois") & filters.private)
    async def _cmd_whois(client, message):  # noqa: ARG001
        try:
            uid = message.from_user.id
            if uid != ADMIN_USER_ID:
                return
            if _locked(uid):
                await message.reply_text("🔒 Locked: authenticate with /auth first.")
                return
            parts = (message.text or "").split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                await message.reply_text("Usage: /whois <@handle|id>")
                return
            target = parts[1].strip()
            if target.lstrip("-").isdigit():
                target_id = int(target)
            else:
                target_id = await _resolve_handle(target)
            if target_id is None:
                await message.reply_text(f"No indexed profile for {target} yet.")
                return
            await _reply_chunked(message, await deep_whois(target_id))
        except Exception as e:  # noqa: BLE001
            log(f"/whois failed: {e}")

    @client.on_message(filters.command("introduce") & filters.private)
    async def _cmd_introduce(client, message):  # noqa: ARG001
        try:
            uid = message.from_user.id
            if uid != ADMIN_USER_ID:
                return
            if _locked(uid):
                await message.reply_text("🔒 Locked: authenticate with /auth first.")
                return
            parts = (message.text or "").split()
            if len(parts) < 3:
                await message.reply_text("Usage: /introduce <a> <b>")
                return

            async def _resolve(token: str) -> Optional[int]:
                if token.lstrip("-").isdigit():
                    return int(token)
                return await _resolve_handle(token)

            a_id = await _resolve(parts[1])
            b_id = await _resolve(parts[2])
            if a_id is None or b_id is None:
                await message.reply_text(
                    f"Couldn't resolve both parties ({parts[1]}, {parts[2]})."
                )
                return
            await _reply_chunked(message, await deep_introduce(a_id, b_id))
        except Exception as e:  # noqa: BLE001
            log(f"/introduce failed: {e}")

    @client.on_message(filters.command("sweep") & filters.private)
    async def _cmd_sweep(client, message):  # noqa: ARG001
        global _SWEEP_TASK, _LAST_SWEEP
        try:
            uid = message.from_user.id
            if uid != ADMIN_USER_ID:
                return
            if _locked(uid):
                await message.reply_text("🔒 Locked: authenticate with /auth first.")
                return
            control = scraper.get_control()
            gate = getattr(control, "gate", None) or scraper.FloodGate()
            rate = scraper.RateGate(config.TG_OPS_PER_SECOND, config.TG_OPS_BURST)
            parts = (message.text or "").split()
            full = len(parts) > 1 and parts[1].lower() in ("full", "--full")
            _SWEEP_TASK = asyncio.create_task(
                profiles.build_profiles(client, gate, rate, force_refresh=full)
            )
            _LAST_SWEEP = time.strftime("%Y-%m-%d %H:%M:%S")
            await message.reply_text(
                "🔄 profile sweep started" + (" (full refresh)" if full else "") + "."
            )
        except Exception as e:  # noqa: BLE001
            log(f"/sweep failed: {e}")

    # -- group mentions ----------------------------------------------------
    @client.on_message(filters.command("watch") & filters.private)
    async def _cmd_watch(client, message):  # noqa: ARG001
        try:
            uid = message.from_user.id
            if uid != ADMIN_USER_ID:
                return
            if _locked(uid):
                await message.reply_text("🔒 Locked: authenticate with /auth first.")
                return
            import watchlist as _watchlist

            wl = _watchlist.get_watchlist()
            parts = (message.text or "").split()
            sub = parts[1].lower() if len(parts) > 1 else "list"
            if sub == "add" and len(parts) > 2:
                added = await wl.add(" ".join(parts[2:]))
                await message.reply_text("✅ watching." if added else "ℹ️ already watched.")
            elif sub == "remove" and len(parts) > 2:
                removed = await wl.remove(" ".join(parts[2:]))
                await message.reply_text("🗑 removed." if removed else "ℹ️ not in watchlist.")
            else:
                rows = wl.all()[:50]
                if not rows:
                    await message.reply_text("Watchlist empty. Usage: /watch add <token>")
                else:
                    lines = ["**👁 Watchlist**"]
                    for item in rows:
                        line = f"• {item.get('token')}"
                        if item.get("note"):
                            line += f" — {item['note']}"
                        lines.append(line)
                    await _reply_chunked(message, "\n".join(lines))
        except Exception as e:  # noqa: BLE001
            log(f"/watch failed: {e}")

    @client.on_message(filters.command("dossier") & filters.private)
    async def _cmd_dossier(client, message):  # noqa: ARG001
        try:
            uid = message.from_user.id
            if uid != ADMIN_USER_ID:
                return
            if _locked(uid):
                await message.reply_text("🔒 Locked: authenticate with /auth first.")
                return
            parts = (message.text or "").split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                await message.reply_text(
                    "Usage: /dossier <entity> — phone | @user | id | email | wallet | t.me link | text"
                )
                return
            import query as _query

            await message.reply_text("🕵️ building dossier…")
            result = await _query.dossier(parts[1].strip())
            await _reply_chunked(message, _query.render_text(result))
        except Exception as e:  # noqa: BLE001
            log(f"/dossier failed: {e}")

    @client.on_message(filters.mentioned & filters.group)
    async def _group_mention(client, message):
        try:
            text = message.text or message.caption or ""
            own = (getattr(config, "BOT_USERNAME", "") or "").lstrip("@")
            if own:
                text = re.sub(
                    rf"@{re.escape(own)}\b", "", text, flags=re.IGNORECASE
                )
            text = text.strip()
            if not text:
                return
            await handle_nlp(client, message, text, deep_allowed=False)
        except Exception as e:  # noqa: BLE001
            log(f"group mention failed: {e}")

    # -- inline mode -------------------------------------------------------
    @client.on_inline_query()
    async def _inline(client, inline_query):
        try:
            query = (inline_query.query or "").strip()
            if not query:
                await inline_query.answer([], cache_time=1, is_personal=True)
                return

            deep = _is_admin(inline_query.from_user.id)
            results: list[InlineQueryResultArticle] = []

            casual = await rag_answer(query, deep=False)
            results.append(
                InlineQueryResultArticle(
                    title="💬 Casual answer",
                    description=_truncate(casual, 120),
                    input_message_content=InputTextMessageContent(
                        _truncate(casual, 3500)
                    ),
                )
            )

            if deep:
                deep_text = await rag_answer(query, deep=True)
                results.append(
                    InlineQueryResultArticle(
                        title="🕵️ Deep answer",
                        description=_truncate(deep_text, 120),
                        input_message_content=InputTextMessageContent(
                            _truncate(deep_text, 3500)
                        ),
                    )
                )
                intent, params = _classify(query)
                handles = params.get("handles") or []
                if intent == "whois" and len(handles) == 1:
                    uid = await _resolve_handle(handles[0])
                    if uid is not None:
                        dossier = await deep_whois(uid)
                        results.append(
                            InlineQueryResultArticle(
                                title=f"🕵️ Dossier @{handles[0]}",
                                description=_truncate(dossier, 120),
                                input_message_content=InputTextMessageContent(
                                    _truncate(dossier, 3500)
                                ),
                            )
                        )

            await inline_query.answer(results, cache_time=1, is_personal=True)
        except Exception as e:  # noqa: BLE001
            log(f"inline query failed: {e}")

    # -- callback queries --------------------------------------------------
    @client.on_callback_query(filters.regex(r"^p:"))
    async def _panel_callback(client, cq):  # noqa: ARG001
        try:
            uid = cq.from_user.id
            if uid != ADMIN_USER_ID or not get_auth().is_authenticated(uid):
                await cq.answer("🔒 Not authenticated.", show_alert=True)
                return

            data = cq.data or ""
            control = scraper.get_control()
            note = "Updated"

            if data == "p:pause":
                control.pause("admin:panel")
                note = "⏸ Paused"
            elif data == "p:resume":
                control.resume()
                note = "▶️ Resumed"

            await cq.message.edit_text(
                await panel_snapshot(), reply_markup=_panel_keyboard()
            )
            await cq.answer(note)
        except Exception as e:  # noqa: BLE001
            log(f"panel callback failed: {e}")
            try:
                await cq.answer("Error", show_alert=True)
            except Exception:  # noqa: BLE001
                pass
