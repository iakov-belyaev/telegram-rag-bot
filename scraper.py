"""Standalone historical scraper entrypoint.

Thin CLI wrapper: reuses the shared userbot client and the background
catch-up scraper defined in `start.py`.
"""

from start import userbot, historical_catchup

if __name__ == "__main__":
    print("📚 Starting historical catch-up scraper...")
    userbot.run(historical_catchup())
