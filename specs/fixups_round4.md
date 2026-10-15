# Fixups round 4 — bot.py part-1 data-shape corrections

The generated code assumed dicts where the real data layer returns Qdrant
`ScoredPoint` objects, and assumed list shapes where profiles returns dicts.
Apply EXACTLY these 6 edits (SEARCH/REPLACE) to bot.py.

## Edit 1 — `_profile_line` (real payload shape)

SEARCH:
```python
def _profile_line(p: dict) -> str:
    """One-line summary of a profile dict."""
    name = p.get("display_name") or p.get("first_name") or "Unknown"
    username = p.get("username")
    handle = f" (@{username})" if username else ""
    msgs = p.get("message_count", p.get("msg_count", 0))
    groups = p.get("group_count", p.get("chat_count", 0))
    return f"{name}{handle} — {msgs} msgs, {groups} groups"
```
REPLACE:
```python
def _profile_line(p: dict) -> str:
    """One-line summary of a profile payload dict (real tg_bot_rag shape)."""
    name = p.get("display_name") or "Unknown"
    username = p.get("username") or ""
    handle = f" (@{username})" if username else ""
    msgs = p.get("message_count", 0) or 0
    groups = len(p.get("common_chat_ids") or [])
    return f"{name}{handle} — {msgs} msgs, {groups} groups"
```

## Edit 2 — `rag_answer` must read `.payload` off ScoredPoint results

SEARCH:
```python
    lines: list[str] = []
    for h in hits or []:
        sender = h.get("sender_name") or "Unknown"
        username = h.get("username")
        handle = f" (@{username})" if username else ""
        chat_title = h.get("chat_title") or "?"
        text = _truncate(h.get("text", ""), 300)
        lines.append(f"- [{sender}{handle} in '{chat_title}': {text}")
    context = "\n".join(lines) if lines else "(no matching messages)"

    if deep:
        prof_hits = await db.search_profiles(query, limit=5)
        prof_lines: list[str] = []
        for p in prof_hits or []:
            name = p.get("display_name") or p.get("first_name") or "Unknown"
            username = p.get("username") or "?"
            bio = _truncate(p.get("bio", ""), 200)
            groups = p.get("group_count", p.get("chat_count", 0))
            prof_lines.append(f"- {name} (@{username}) bio: {bio} groups: {groups}")
        if prof_lines:
            context += "\n\nMATCHING PROFILES\n" + "\n".join(prof_lines)
```
REPLACE:
```python
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
```

## Edit 3a — `casual_whois` group count from common_chat_ids

SEARCH:
```python
    profile = data.get("profile", data) if isinstance(data, dict) else {}
    name = profile.get("display_name") or profile.get("first_name") or target_label
    username = profile.get("username")
    handle = f" (@{username})" if username else ""
    msgs = profile.get("message_count", profile.get("msg_count", 0))
    groups = profile.get("group_count", profile.get("chat_count", 0))
    last_seen = _fmt_dt(profile.get("last_seen", ""))
```
REPLACE:
```python
    profile = data.get("profile", data) if isinstance(data, dict) else {}
    name = profile.get("display_name") or target_label
    username = profile.get("username")
    handle = f" (@{username})" if username else ""
    msgs = profile.get("message_count", 0) or 0
    groups = len(profile.get("common_chat_ids") or [])
    last_seen = _fmt_dt(profile.get("last_seen", ""))
```

## Edit 3b — `casual_whois` search hits read `.payload`

SEARCH:
```python
            hits = await db.search_messages(username, limit=5)
            titles: list[str] = []
            for h in hits or []:
                t = h.get("chat_title")
                if t and t not in titles:
                    titles.append(t)
```
REPLACE:
```python
            hits = await db.search_messages(username, limit=5)
            titles: list[str] = []
            for h in hits or []:
                payload = getattr(h, "payload", None) or {}
                t = payload.get("chat_title")
                if t and t not in titles:
                    titles.append(t)
```

## Edit 4 — `deep_whois` groups/interactions/relations blocks (real payload keys)

SEARCH:
```python
    chats = data.get("chats") or data.get("groups") or []
    if chats:
        out.append("\n**Groups**")
        for c in chats[:15]:
            title = c.get("title") if isinstance(c, dict) else str(c)
            out.append(f"• {title}")

    interactions = data.get("interactions") or []
    if interactions:
        out.append("\n**Top interactions**")
        ids = [i.get("user_id") for i in interactions[:8] if isinstance(i, dict)]
        names: dict[int, str] = {}
        try:
            bulk = await db.get_profiles_bulk([i for i in ids if i is not None])
            for p in bulk or []:
                names[p.get("user_id")] = p.get("display_name") or p.get("username") or "?"
        except Exception as e:  # noqa: BLE001
            log(f"deep_whois bulk lookup failed: {e}")
        for i in interactions[:8]:
            if not isinstance(i, dict):
                continue
            uid = i.get("user_id")
            label = names.get(uid) or (f"@{i.get('username')}" if i.get("username") else str(uid))
            weight = i.get("weight", i.get("count", ""))
            out.append(f"• {label} ({weight})")

    relations = data.get("relations") or []
    if relations:
        out.append("\n**Relations**")
        for r in relations[:10]:
            if not isinstance(r, dict):
                continue
            kind = r.get("kind", "?")
            a = r.get("a") or r.get("a_username") or r.get("a_id")
            b = r.get("b") or r.get("b_username") or r.get("b_id")
            weight = r.get("weight", "")
            out.append(f"• {kind}: @{a} <-> @{b} ({weight})")
```
REPLACE:
```python
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
```

## Edit 5 — `casual_introduce` counts chat_ids (common_chats is a dict)

SEARCH:
```python
    common = []
    if isinstance(intro, dict):
        common = intro.get("common_chats") or intro.get("common") or []
    n = len(common)
```
REPLACE:
```python
    common = {}
    if isinstance(intro, dict):
        common = intro.get("common_chats") or {}
    n = len(common.get("chat_ids") or []) if isinstance(common, dict) else len(common)
```

## Edit 6 — `deep_introduce` common titles + relation formatting

SEARCH:
```python
    common = intro.get("common_chats") or intro.get("common") or []
    if common:
        out.append("\n**Common communities**")
        for c in common[:15]:
            title = c.get("title") if isinstance(c, dict) else str(c)
            out.append(f"• {title}")

    relation = intro.get("relation")
    if relation:
        out.append("\n**Relation**")
        out.append(f"• {relation}")
```
REPLACE:
```python
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
```
