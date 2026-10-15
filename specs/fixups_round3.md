# Fixups round 3 — profiles.py only: 5 exact bug fixes

Apply EXACTLY these 5 edits to profiles.py. Standard SEARCH/REPLACE blocks.

## Edit 1 — aggregation reads the wrong reply field name (database.py writes `reply_to_user_id`)

SEARCH:
```python
            reply_to = payload.get("reply_to_sender_id")
```
REPLACE with:
```python
            reply_to = payload.get("reply_to_user_id")
```

## Edit 2 — `build_relation_payload` calls are missing the username arguments

The real signature is:
`build_relation_payload(a_id, a_username, b_id, b_username, kind, weight, evidence_chat_ids, first_seen, last_seen)`.

### 2a — co_member block. SEARCH:
```python
        payloads.append(
            db.build_relation_payload(
                a,
                b,
                kind="co_member",
                weight=len(shared),
                evidence_chat_ids=sorted(shared),
                first_seen=first_seen,
                last_seen=last_seen,
            )
        )
```
REPLACE with:
```python
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
```

### 2b — replies block. SEARCH:
```python
        payloads.append(
            db.build_relation_payload(
                a,
                b,
                kind="replies",
                weight=edge["weight"],
                evidence_chat_ids=sorted(edge["chats"]),
                first_seen=first_seen,
                last_seen=last_seen,
            )
        )
```
REPLACE with:
```python
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
```

### 2c — mentions block. SEARCH:
```python
        payloads.append(
            db.build_relation_payload(
                a,
                b,
                kind="mentions",
                weight=edge["weight"],
                evidence_chat_ids=sorted(edge["chats"]),
                first_seen=first_seen,
                last_seen=last_seen,
            )
        )
```
REPLACE with:
```python
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
```

## Edit 3 — dedup key uses non-existent payload keys (would collapse all relations)

SEARCH:
```python
        key = (p.get("user_a"), p.get("user_b"), p.get("kind"))
```
REPLACE with:
```python
        key = (p.get("a_id"), p.get("b_id"), p.get("edge_kind"))
```

## Edit 4 — `build_profile_payload` has no `display_name` parameter (TypeError on every profile)

SEARCH:
```python
            payload = db.build_profile_payload(
                uid,
                username=info.get("username") or u.get("username", ""),
                first_name=info.get("first_name", ""),
                last_name=info.get("last_name", ""),
                display_name=u.get("display_name", ""),
```
REPLACE with:
```python
            payload = db.build_profile_payload(
                uid,
                username=info.get("username") or u.get("username", ""),
                first_name=info.get("first_name") or u.get("display_name", ""),
                last_name=info.get("last_name", ""),
```

## Edit 5 — `introduce` checks non-existent relation keys

SEARCH:
```python
                if rel.get("user_a") == user_b_id or rel.get("user_b") == user_b_id:
```
REPLACE with:
```python
                if rel.get("a_id") == user_b_id or rel.get("b_id") == user_b_id:
```
