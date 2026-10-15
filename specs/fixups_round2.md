# Fixups round 2 — scraper.py only: async-generator FloodWait iteration

BUG: `telegram_call` returns a coroutine, so `async for x in telegram_call(...)` raises
`TypeError: 'async for' requires an object with __aiter__ method, got coroutine`.
Three call sites use this broken pattern: `is_high_value_chat` (sampling), `sync_chat_history`
(pagination), `scraper_supervisor` (get_dialogs).

FIX: add an async-generator helper next to `telegram_call`, then swap the three call sites.

## Edit 1 — add helper immediately AFTER the `telegram_call` function body (before section 2):

```python
async def iterate_with_flood(gate: FloodGate, fn: Callable[..., Any], /, *args: Any, **kwargs: Any):
    """Async-iterate an async-generator function with gate + FloodWait retry.

    On FloodWait mid-iteration the underlying iterator is RESTARTED from scratch;
    this is safe because callers pass an explicit offset (resumable pagination)
    and re-processed items are filtered by the message-id dedup guard.
    """
    attempt = 0
    while True:
        await gate.wait()
        try:
            async for item in fn(*args, **kwargs):
                yield item
            return
        except tg_errors.FloodWait as e:
            if attempt >= config.FLOOD_MAX_RETRIES:
                raise
            attempt += 1
            await gate.trip(float(e.value))
            await gate.wait()
```

## Edit 2 — in `is_high_value_chat`, replace:

```python
        async for message in telegram_call(
            gate, client.get_chat_history, chat.id, limit=config.FILTER_SAMPLE_SIZE
        ):
```
with:
```python
        async for message in iterate_with_flood(
            gate, client.get_chat_history, chat.id, limit=config.FILTER_SAMPLE_SIZE
        ):
```

## Edit 3 — in `sync_chat_history`, replace:

```python
            async for m in telegram_call(
                gate,
                client.get_chat_history,
                chat.id,
                limit=config.SCRAPER_PAGE_SIZE,
                offset_id=offset_id,
            ):
```
with:
```python
            async for m in iterate_with_flood(
                gate,
                client.get_chat_history,
                chat.id,
                limit=config.SCRAPER_PAGE_SIZE,
                offset_id=offset_id,
            ):
```

## Edit 4 — in `scraper_supervisor`, replace:

```python
            async for d in telegram_call(gate, client.get_dialogs):
```
with:
```python
            async for d in iterate_with_flood(gate, client.get_dialogs):
```

Do NOT change anything else.
