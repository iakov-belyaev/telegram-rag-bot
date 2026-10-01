"""Standalone real-time listener entrypoint.

Thin CLI wrapper: reuses the shared userbot client and the live-message
handler defined in `start.py`, so ingestion behavior stays identical to the
unified engine.
"""

from start import userbot, process_incoming_message  # noqa: F401

if __name__ == "__main__":
    print("🚀 Starting Real-Time Telegram Message Listener...")
    userbot.run()
