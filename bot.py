"""Standalone Telegram inline bot entrypoint.

Thin CLI wrapper: reuses the shared inline bot client and the universal
natural-language inline query handler defined in `start.py`.
"""

from start import inline_bot, answer_inline_query  # noqa: F401

if __name__ == "__main__":
    print("🤖 Telegram Inline Search Bot is running...")
    inline_bot.run()
