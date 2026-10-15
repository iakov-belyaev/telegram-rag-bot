"""Unified async orchestrator for telegram-rag-bot v2.

Starts the Pyrogram userbot + global bot, parallel scraper, profile sweeps,
FastAPI control surface, and the StorageShield capacity guard; handles graceful
shutdown on SIGINT/SIGTERM.
"""

import asyncio
import signal
import sys
import time

from pyrogram import Client, errors

import api
import bot as bot_mod
import config
import database as db
import profiles
import scraper


def log(message: str) -> None:
    print(f"[main {time.strftime('%H:%M:%S')}] {message}", flush=True)


def _build_clients() -> tuple[Client, Client]:
    userbot = Client(
        config.SESSION_USERBOT,
        api_id=config.TELEGRAM_API_ID,
        api_hash=config.TELEGRAM_API_HASH,
    )
    bot_client = Client(
        config.SESSION_BOT,
        api_id=config.TELEGRAM_API_ID,
        api_hash=config.TELEGRAM_API_HASH,
        bot_token=config.BOT_TOKEN,
    )
    return userbot, bot_client


async def _notify_admin(bot_client: Client, admin_id: int, text: str) -> None:
    """Best-effort DM to the admin; never raises."""
    try:
        await bot_client.send_message(admin_id, text)
    except errors.PeerIdInvalid:
        log("admin must /start the bot first (PeerIdInvalid)")
    except Exception as exc:  # noqa: BLE001
        log(f"admin notify failed: {exc}")


async def storage_shield(bot_client: Client, admin_id: int, control) -> None:
    """Capacity guard loop; pauses/resumes the scraper based on usage ratio."""
    warned = False
    while not control.stop_requested:
        try:
            stats = await db.collection_stats()
            usage = stats["messages_usage"]["usage_ratio"]

            if usage >= config.STORAGE_PAUSE_RATIO and not control.paused:
                control.pause("storage:high-water")
                await _notify_admin(
                    bot_client,
                    admin_id,
                    f"🛑 Storage high-water: usage={usage:.2%} "
                    f"(>= {config.STORAGE_PAUSE_RATIO:.0%}). Scraper paused.",
                )
            elif usage >= config.STORAGE_WARN_RATIO and not warned:
                warned = True
                await _notify_admin(
                    bot_client,
                    admin_id,
                    f"⚠️ Storage warning: usage={usage:.2%} "
                    f"(>= {config.STORAGE_WARN_RATIO:.0%}).",
                )
            elif usage < config.STORAGE_WARN_RATIO:
                warned = False
                if control.paused and str(control.pause_reason).startswith("storage"):
                    control.resume()
                    await _notify_admin(
                        bot_client,
                        admin_id,
                        f"✅ Storage recovered: usage={usage:.2%}. Scraper resumed.",
                    )
        except Exception as exc:  # noqa: BLE001
            log(f"storage_shield error: {exc}")

        try:
            await asyncio.sleep(config.STORAGE_CHECK_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise


def _make_sweep_fn(userbot: Client):
    async def _sweep() -> dict:
        control = scraper.get_control()
        gate = getattr(control, "gate", None) or scraper.FloodGate()
        rate = scraper.RateGate(config.TG_OPS_PER_SECOND, config.TG_OPS_BURST)
        return await profiles.build_profiles(userbot, gate, rate)

    return _sweep


async def main() -> int:
    """Validate config, start everything, wait for a stop signal. Returns exit code."""
    problems = config.validate_config()
    if problems:
        for problem in problems:
            print(f" - {problem}")
        return 1

    config.ensure_state_dir()

    userbot, bot_client = _build_clients()

    try:
        await userbot.start()
        me = await userbot.get_me()
        admin_id = me.id

        bot_mod.register_handlers(bot_client, admin_id)
        await bot_client.start()
    except Exception as exc:  # noqa: BLE001
        log(f"startup failed: {exc}")
        try:
            await userbot.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            await bot_client.stop()
        except Exception:  # noqa: BLE001
            pass
        return 1

    api._sweep_fn = _make_sweep_fn(userbot)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    control = scraper.get_control()
    tasks = [
        asyncio.create_task(scraper.run_scraper(userbot), name="scraper"),
        asyncio.create_task(profiles.profile_sweep(userbot, control), name="profiles"),
        asyncio.create_task(api.run_api(config.API_HOST, config.API_PORT), name="api"),
        asyncio.create_task(
            storage_shield(bot_client, admin_id, control), name="storage-shield"
        ),
    ]

    await _notify_admin(
        bot_client,
        admin_id,
        f"🚀 telegram-rag-bot v2 online\nBot: {config.BOT_USERNAME}\n"
        f"Admin id: {admin_id}\nWorkers: {config.SCRAPER_WORKERS}\n"
        f"API: http://{config.API_HOST}:{config.API_PORT}/health",
    )

    try:
        await stop.wait()
    except Exception as exc:  # noqa: BLE001
        log(f"wait error: {exc}")

    log("stop signal received — shutting down")
    control.request_stop()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    await _notify_admin(bot_client, admin_id, "🛑 telegram-rag-bot shutting down")

    try:
        await userbot.stop()
    except Exception as exc:  # noqa: BLE001
        log(f"userbot stop error: {exc}")
    try:
        await bot_client.stop()
    except Exception as exc:  # noqa: BLE001
        log(f"bot stop error: {exc}")

    return 0


def _run() -> int:
    try:
        return asyncio.run(main())
    except KeyboardInterrupt:
        log("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(_run())
