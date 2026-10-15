# Spec PART 2 of 2: `storage.py` — resync, health monitor, entrypoints

Pass 2 of 2. Replace the `# STUB_PART_2:` lines ONLY. Reuse everything from pass 1.

## 7. Resync (SQLite → Qdrant when recovered)

```python
async def resync_pending(manager: Optional[FailoverManager] = None) -> dict:
    """Upload dirty SQLite rows back to Qdrant in batches.

    Returns {"messages": n, "chats": n, "profiles": n, "relations": n, "errors": n}.
    - Loop while manager.mode == "qdrant" and dirty rows exist:
      - for each table in (messages, chats, profiles, relations):
        rows = await asyncio.to_thread(manager.sqlite.fetch_dirty, table, config.FAILOVER_RESYNC_BATCH)
        - messages → rebuild PointStruct(id=point_id, vector=await qdb.embed_texts([single text])[0],
          payload=payload) → await qdb.upsert_messages(points)
        - chats → rebuild via qdb.upsert_chat? NO — chat payload already built; upsert point directly:
          qdb.upsert_messages can't (wrong collection). Use qdb._qdrant_call properly:
          point = PointStruct(id=point_id, vector=(await qdb.embed_texts(["Group: " + title + ...]))[0], payload=payload)
          → upsert into qdb.CHATS_COLLECTION via qdb._qdrant_call(..., collection_name=CHATS_COLLECTION...)
          (build the "Group: {title} (@username)" text exactly like qdb.upsert_chat does).
        - profiles → qdb.upsert_profiles([(user_id, payload, vector_from qdb.embed_texts([qdb.build_profile_embed_text(payload)]))])
        - relations → qdb.upsert_relations([(payload, vector_from qdb.embed_texts([qdb.build_relation_embed_text(payload)]))])
        - on success per batch → await asyncio.to_thread(manager.sqlite.mark_clean, table, [point_ids])
          + metrics.inc("failover_resync_uploaded", n)
        - on exception → manager.note_error(e); if now degraded → return stats early (will resume later)
      - stop when all four fetches returned empty.
    - Guard with manager.resyncing flag (skip if already running). Finally: manager.resyncing = False.
    """
```
Embeddings must be batched where cheap: fetch up to batch rows; for messages embed all texts in ONE
`qdb.embed_texts(texts)` call; same for profiles/relations batches.

## 8. Health monitor loop

```python
async def failover_monitor(manager: Optional[FailoverManager] = None) -> None:
    """Forever loop: probe Qdrant every config.FAILOVER_HEALTH_INTERVAL_SECONDS.

    - if manager.enabled is False → log once and return.
    - probe; log transitions only (not every cycle).
    - sleep interval in 1s steps honoring nothing else (task cancelled at shutdown).
    - exceptions inside probe are logged, never propagate.
    """
```

## 9. Entrypoint helpers

```python
async def start_storage() -> None:
    """Called by main.py at startup: get_manager(); if enabled: startup probe (log initial
    mode + counts); schedule failover_monitor() via asyncio.create_task; if mode became
    'sqlite' at startup, also schedule resync attempt every health probe (handled by
    probe_qdrant).  Returns None; never raises."""
async def stop_storage() -> None:
    """Flush: if enabled and mode == 'sqlite', attempt one final resync (best-effort, short);
    close sqlite connection. Never raises."""
def storage_status() -> dict:
    """JSON-safe convenience: get_manager().status() (+ enabled/disabled note)."""
```

## Notes

- `resync_pending` embeds with the SAME fastembed model as qdb (via qdb.embed_texts) so vectors
  are identical to live-written ones.
- Every public function in this file must remain import-safe and callable without a running loop
  except the async ones (which by definition need a loop).
- Whatever counts/types Qdrant-backend returns for reads, the SQLite path returns SHAPE-COMPATIBLE
  objects (payload-carrying SimpleNamespace for search hits; plain dicts for payload fetchers).
