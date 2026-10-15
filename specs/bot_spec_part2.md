# Spec PART 2 of 2: `bot.py` — handlers, auth flow, admin panel, callbacks, inline

Pass 2 of 2. Implement ONLY the sections below, replacing the `# STUB_PART_2:` lines.
Reuse everything from pass 1 (log, helpers, rag_answer, casual_whois, deep_whois,
casual_introduce, deep_introduce, handle_nlp, _is_admin, _extract_handles,
get_llm, ADMIN_USER_ID, _classify). Do NOT redefine them.

## 1. Panel builders

- `def _panel_keyboard()` — `InlineKeyboardMarkup` rows (pyrogram types):
  `[("📊 Status","p:status"), ("🗄 DB metrics","p:db")]`,
  `[("▶️ Resume scraper","p:resume"), ("⏸ Pause scraper","p:pause")]`,
  `[("🎯 Targets","p:targets"), ("🔄 Refresh","p:refresh")]`.
- `async def panel_snapshot() -> str` — markdown combining: scraper control status
  (`scraper.get_control().status()` — paused/reason, uptime h:m, workers active, queue depth,
  counters, flood events, gate remaining), targets summary (`TargetManager().load()`+summary()),
  and a compact DB block (`await db.collection_stats()`: per-collection points + messages usage %).
  Keep <= 30 lines.

## 2. Handler registration — `def register_handlers(client: Client) -> None`

Set the module-global `ADMIN_USER_ID` inside register_handlers via
`def register_handlers(client: Client, admin_user_id: int) -> None` (signature includes the id).
All handler bodies wrapped in try/except with log().

### Private chat handlers

- `/start` (`filters.command("start") & filters.private`):
  - If sender id == admin_user_id and already authenticated → send panel (panel_snapshot + keyboard).
  - If sender id == admin_user_id but not authenticated → explain /auth flow.
  - Else → short greeting: "I'm an intelligence assistant. Tag me in a group with a question,
    or use inline mode: `@botusername <query>`." (mention CASUAL behavior).
- `/auth` (private): if sender != admin_user_id → "⛔ Not authorized." (no challenge issued).
  Else → `ch = get_auth().issue_challenge(user_id)`; reply with the nonce + exact instructions
  (mention `python rbac.py respond <nonce>` to run on the server) + expires_in.
- `/logout` (private admin): `get_auth().revoke(uid)` → confirm.
- Private text router (`filters.private & filters.text & ~filters.command([...])`):
  - admin + pending challenge → treat text as the response code: `ok, reason = get_auth().verify_response(uid, text)`;
    on ok → "✅ Authenticated. Session valid {ttl//60} min. Use /panel."; on wrong → usage hint with reason
    (do not echo the code).
  - admin + authenticated → `await handle_nlp(client, message, text, deep_allowed=True)`.
  - non-admin private text → gentle guidance (use inline mode / tag in groups), no auth talk.

### Admin commands (private, admin id, and NOT necessarily authenticated for /auth-logout; require session for the rest)

- `/panel` → panel_snapshot + keyboard.
- `/status` → `scraper.get_control().status()` pretty + profile sweep info (last build via module attr `_LAST_SWEEP` if set, else "-").
- `/dbstats` → `db.collection_stats()` formatted (points per collection, total, usage % with a simple bar `█▓▒░`, e.g. `usage_ratio 0.38 → "████░░░░░░"`).
- `/scrape <start|pause|resume>` → control.pause(reason="admin")/resume(); unknown arg → usage.
- `/join <peer>` → `tm = TargetManager(); tm.load(); added = await tm.add(peer)` then reply "queued — the userbot joins it on the next pass" or "already in target list". (Bots cannot join; the userbot does it.)
- `/targets` → list all targets: `state | peer | title | note`.
- `/whois <@handle|id>` → resolve; deep_whois (admin is in private chat, deep_allowed=True).
- `/introduce <a> <b>` → accept two @handles or ids; deep_introduce.
- `/sweep` → kick a one-off profile sweep: `asyncio.create_task(profiles.build_profiles(client, scraper.get_control().gate or scraper.FloodGate(), scraper.RateGate(config.TG_OPS_PER_SECOND, config.TG_OPS_BURST)))` and reply "profile sweep started". Store task ref in module attr `_SWEEP_TASK`.
- All admin commands except /auth//logout//panel require `get_auth().is_authenticated(uid)`; if not → "🔒 Locked: authenticate with /auth first."

### Group mentions

- `filters.mentioned & filters.group`: extract text (with caption fallback), strip the bot
  mention; `deep_allowed = _is_admin(message.from_user.id)` BUT deep output is only sent in
  private chats in this design — so in groups ALWAYS call `handle_nlp(..., deep_allowed=False)`.

### Inline mode

- `@client.on_inline_query()`:
  - query text empty → answer [] (nothing).
  - `deep = _is_admin(inline_query.from_user.id)`.
  - Build answers: casual: one article with rag_answer(query, deep=False) truncated to ~3500 chars.
    Deep (admin): a second article with the deep RAG and, when the query looks like whois
    (`_classify` → whois and exactly one handle resolves), also a `deep_whois` article.
  - `await inline_query.answer(results, cache_time=1, is_personal=True)`.

### Callback queries (panel buttons)

- `@client.on_callback_query(filters.regex(r"^p:"))`:
  - Only admin id + authenticated may trigger; else `await cq.answer("🔒 Not authenticated.", show_alert=True)`.
  - "p:pause" → control.pause("admin:panel"); "p:resume" → control.resume(); answer callback with ✅/ℹ️.
  - "p:status"/"p:db"/"p:targets"/"p:refresh"/"p:pause"/"p:resume" → after the action,
    `await cq.message.edit_text(await panel_snapshot(), reply_markup=_panel_keyboard())`
    (edit_text supports reply_markup editing) and `await cq.answer("Updated")`.
  - All exceptions caught + `await cq.answer("Error", show_alert=True)` best-effort.

## Notes

- Use pyrogram `Filters` (`filters.command`, `filters.private`, `filters.group`, `filters.mentioned`,
  `filters.text`, `filters.caption`). Command list excludes overlap: private text router must use
  `filters.private & filters.text & ~filters.command(["start","auth","logout","panel","status","dbstats","scrape","join","targets","whois","introduce","sweep"])`.
- Do NOT call `client.start()` here — main.py owns the lifecycle. register_handlers only registers.
- Pagination/message-length: split replies > 4000 chars into chunks on "\n".
- DeepSeek/model calls must never block handler exit for more than config.DEEPSEEK_TIMEOUT_SECONDS+5.
