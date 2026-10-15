# Spec PART 2 of 2: `scraper.py` — indexing pipeline, workers, supervisor, listener, runner

This is pass 2 of 2 for `scraper.py`. Implement ONLY sections 7-11 below, replacing the
`# STUB_PART_2:` line. Section 6 (multimodal helpers) already exists — REUSE
`extract_text_from_media`, `log`, `telegram_call`, `FloodGate`, `RateGate`,
`ScraperControl`, `CursorStore`, `ChatFilterCache`, `TargetManager`, `join_converge`,
`is_high_value_chat`, `get_control`, `_CONTROL` from pass 1.

## 7. Batch indexing

```python
async def index_messages_batch(client, gate: FloodGate, chat, messages: list) -> int:
    """Extract text, dedup against the cluster, embed, and upsert messages.

    1. candidates = []
       for m in messages:
           text = (m.text or m.caption or "").strip()
           source_type = "text"
           if len(text) < config.MIN_EXTRACTED_TEXT_LENGTH:
               text, source_type = await extract_text_from_media(client, gate, m)
               if not text:
                   continue
           candidates.append((m, text, source_type))
    2. DEDUP GUARD: existing = await db.filter_existing_message_ids(chat.id, [m.id for m, _, _ in candidates]);
       candidates = [(m, t, s) for (m, t, s) in candidates if m.id not in existing]
    3. if not candidates: return 0
    4. vectors = await db.embed_texts([t for _, t, _ in candidates])
    5. points = [PointStruct(id=db.message_point_id(chat.id, m.id), vector=v,
                  payload=db.build_message_payload(chat, m, t, source_type=s))
                 for (m, t, s), v in zip(candidates, vectors)]
    6. await db.upsert_chat(chat)
    7. n = await db.upsert_messages(points); control = get_control();
       control.note_indexed(n); return n
    Never raise for individual-message problems — wrap the whole body in try/except and
    return 0 on unexpected errors after log() + control.last_error=str(e)[:200].
    """
```

## 8. Per-chat pagination worker

```python
async def sync_chat_history(client, gate: FloodGate, rate: RateGate, cursors: CursorStore, chat, control: ScraperControl) -> int:
    """Paginate one chat's full history until done/paused/stopped. Returns indexed count.

    - entry = cursors.get(chat.id) or {}; if entry.get("completed"): return 0
    - offset_id = entry.get("offset_id", 0); total = 0
    - loop:
        control/gate checks: if control.stop_requested → break; while control.paused → await asyncio.sleep(5)
        await rate.acquire()
        page = []
        async for m in telegram_call(gate, client.get_chat_history, chat.id,
                                     limit=config.SCRAPER_PAGE_SIZE, offset_id=offset_id):
            page.append(m); offset_id = m.id
        if not page: mark completed=True (cursors.update) ; control.counters["chats_completed"] += 1; break
        n = await index_messages_batch(client, gate, chat, page); total += n
        await cursors.update(chat.id, offset_id=offset_id, messages_indexed=<existing+total>, last_error=None)
        if len(page) < config.SCRAPER_PAGE_SIZE: mark completed=True; break
        await asyncio.sleep(config.SCRAPER_MIN_DELAY_SECONDS)
    - on generic Exception: cursors.update(last_error=str(e)[:200]); log; return total
    - return total
    """
```

## 9. Supervisor (parallel workers over a work queue)

```python
async def scraper_supervisor(client, gate: FloodGate, rate: RateGate, cursors: CursorStore,
                             cache: ChatFilterCache, targets: TargetManager, control: ScraperControl) -> None:
    """Forever: build a pass of chats to sync, run SCRAPER_WORKERS consumers, sleep, repeat.

    Each pass:
      1. control.last_pass_started = iso now; join_converge(client, gate, rate, targets) (best-effort, log result).
      2. dialogs = [] ; async for d in telegram_call(gate, client.get_dialogs): dialogs.append(d)
      3. For each dialog: chat = dialog.chat; skip if cursors entry completed; verdict, reason =
         await is_high_value_chat(client, gate, cache, chat); skip if not verdict (log skipped).
      4. queue = asyncio.Queue(); put all work chats (queue items = chat objects).
      5. Spawn config.SCRAPER_WORKERS consumer tasks; each:
         while not control.stop_requested:
             try: chat = queue.get_nowait()
             except asyncio.QueueEmpty: break
             control.set_current(worker, chat.title or str(chat.id))
             try: await sync_chat_history(...)
             except Exception as e: log; control.last_error
             finally: control.set_current(worker, None); queue.task_done()
      6. control.queue_depth = queue.qsize() (update periodically or at pass end)
      7. await queue.join(); cancel remaining consumers; control.queue_depth = 0
      8. control.last_pass_finished = iso now; log pass summary; await asyncio.sleep(config.SCRAPER_IDLE_SLEEP_SECONDS)
    While control.paused: sleep 5 and continue (do not start a new pass).
    """
```

## 10. Live listener (real-time ingestion)

```python
def register_live_listener(client, gate: FloodGate, rate: RateGate, control: ScraperControl) -> None:
    """Register the real-time handler on the userbot (text/caption/photo/document)."""
```
Handler behavior: if control.stop_requested → return; if control.paused → return;
`await rate.acquire()`; then `await index_messages_batch(client, gate, message.chat, [message])`;
catch ALL exceptions (log, control.last_error). Decorator:
`@client.on_message(filters.text | filters.caption | filters.photo | filters.document)`.

## 11. Top-level runner + exports

```python
async def run_scraper(client) -> None:
    """Entry point used by main.py.

    - config.ensure_state_dir()
    - gate = FloodGate(); rate = RateGate(config.TG_OPS_PER_SECOND, config.TG_OPS_BURST)
    - control = get_control(); control.gate = gate
    - cursors = CursorStore(); cursors.load()
    - cache = ChatFilterCache(); cache.load()
    - targets = TargetManager(); targets.load()
    - register_live_listener(client, gate, rate, control)
    - log startup line (targets summary, worker count)
    - try: await scraper_supervisor(client, gate, rate, cursors, cache, targets, control)
      finally (incl. asyncio.CancelledError → save then re-raise): persist cursors/cache/targets (await save())
    - log shutdown
    """
```
Everything above already exists — do NOT redefine classes/functions from pass 1.
