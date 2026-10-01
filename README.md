# Telegram Continuous Indexer & DeepSeek RAG Engine

A decoupled, low-cost Telegram chat indexing and semantic search system. The engine listens to incoming Telegram messages in real time via a Pyrogram Userbot, generates local vector embeddings using FastEmbed, stores them in Qdrant Cloud, and exposes a natural language RAG interface via a Telegram Inline Bot powered by DeepSeek AI.

It also supports **multi-modal ingestion**: images are OCR'd and PDF/DOCX/TXT documents are text-extracted, so their contents become semantically searchable alongside regular messages.

---

## 🤖 How to Interact with the Bot

### 1. Direct Messages (Private Chat with Bot)

* **Initial Setup / Activation (`/start`):**
  Open a private chat with the bot ([@biginformation_bot](https://t.me/biginformation_bot)) and click or send **/start**.
* **System Notifications & Storage Alerts:**
  Telegram restricts bots from initiating contact with users (`PeerIdInvalid` error). Sending `/start` registers your user ID as an authorized peer. The bot uses this direct message channel to send:
  * **Engine Bootup Status:** Confirmation when `start.sh` or `start.py` comes online.
  * **Capacity Warning Alerts:** Automatic threshold notifications when your Qdrant collection approaches capacity limits (e.g., 800,000+ vectors).

---

### 2. In All Other Chats (Groups, Channels & Private Messages)

The bot operates globally using **Telegram Inline Mode**, meaning you can query your vectorized history from **any chat, group, or channel** without needing to add the bot to that group.

#### Step-by-Step Usage:

1. Open **any chat window** in Telegram.
2. In the message input field, type your bot's handle followed by your query:
   ```text
   @biginformation_bot what did we discuss about server setup?

   Wait 1–2 seconds for the interactive inline results popup to appear above the keyboard.

Inline Result Types:
🤖 DeepSeek AI Summary (Top Result):
Synthesizes top semantic matches across all indexed history into a single, concise AI response using DeepSeek API. Selecting this option posts the full AI answer into the chat.

💬 Individual Matched Messages (Below AI Summary):
Displays individual raw chat messages matching your query, complete with sender name, source chat title, date timestamp, and a clickable Jump to message Telegram link (t.me/c/...).

🧠 Multi-Modal Ingestion (OCR & Documents)

Beyond plain text and captions, the engine extracts searchable text from media attachments:

* **Images (OCR):** Photos are downloaded and passed through Tesseract OCR (`pytesseract` + `Pillow`). Extracted text is indexed with `source_type="photo_ocr"`. OCR languages are configurable via the `OCR_LANGUAGES` env var (default `eng+rus`).
* **PDF Documents:** Text is extracted with `pypdf` and indexed with `source_type="document_pdf"`.
* **DOCX Documents:** Text is extracted with `python-docx` and indexed with `source_type="document_docx"`.
* **Plain Text Files:** `.txt` attachments are read directly and indexed with `source_type="document_txt"`.

Extraction rules:

* Only files with extensions in `SUPPORTED_DOC_EXTENSIONS` (`.pdf`, `.docx`, `.txt`) are processed.
* Documents larger than `MAX_DOCUMENT_BYTES` (20 MB) are skipped.
* Extracted text shorter than `MIN_EXTRACTED_TEXT_LENGTH` (10 chars) is discarded.
* Each optional dependency is loaded defensively — if `pytesseract`, `pypdf`, or `python-docx` is missing, that modality is disabled with a warning and the engine keeps running.

The `source_type` field is stored in each message payload and surfaced in inline results, so you can tell whether a match came from a message, an OCR'd image, or a document.

🛡️ StorageShield (Capacity Guard)

`start.py` runs a background `StorageShield` task that periodically checks the size of the `telegram_messages` collection against `MAX_VECTORS` (default `100,000`, configurable via env):

* **≥ 80% capacity (`STORAGE_WARN_RATIO`):** sends a one-time warning DM to the admin via the inline bot.
* **≥ 95% capacity (`STORAGE_PAUSE_RATIO`):** sets `scraping_paused = True`, which halts the background catch-up and deep scrapers to protect the cluster, and sends a critical DM.
* **Below 80%:** resets the alert flag and resumes scraping automatically.

The check interval is controlled by `STORAGE_CHECK_INTERVAL_SECONDS` (default 3600s). Alerts require the admin to have clicked `/start` on the bot first (otherwise Telegram returns `PeerIdInvalid`).

🏗️ Architecture & Tech Stack
   [ Telegram Chats / Groups ]
                │
                ▼ (Pyrogram Userbot Listener)
         [ listener.py / start.py ]
                │
                ├──► FastEmbed (all-MiniLM-L6-v2) ──► Qdrant Cloud (Vectors)
                │                                         ▲
                ▼                                         │
    [ Telegram Inline Bot (@biginformation_bot) ] ────────┘
                │
                ▼
      [ DeepSeek RAG API ]
Language & Runtime: Python 3.11 (macOS / Linux)

Telegram Client: Pyrogram (Dual-client: Userbot + Bot API)

Embedding Model: fastembed (sentence-transformers/all-MiniLM-L6-v2, 384 dimensions)

Vector Database: Qdrant Cloud (telegram_messages and telegram_chats collections)

LLM Synthesis: DeepSeek API (deepseek-chat)

📁 Repository Structure

.
├── core.py           # Shared clients (Qdrant, DeepSeek, FastEmbed) and helpers
├── setup_db.py       # Initializes Qdrant collections and payload indexes
├── scraper.py        # CLI wrapper: runs the historical catch-up scraper
├── listener.py       # CLI wrapper: runs the real-time Pyrogram message listener
├── bot.py            # CLI wrapper: runs the Telegram Inline Bot interface
├── start.py          # Unified continuous engine (Listener + Bot + Scraper + Monitor)
├── start.sh          # Executable shell runner with virtualenv check
├── requirements.txt  # Python package dependencies
└── .env              # Secrets and API credentials (git-ignored)

⚙️ Environment Configuration (.env)
Create a .env file in the root directory with the following configuration:

# Telegram API Credentials (from my.telegram.org)
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=your_telegram_api_hash

# Bot Credentials (from @BotFather)
BOT_TOKEN=1234567890:AAA-BBB-CCC
BOT_USERNAME=@biginformation_bot

# Qdrant Cloud Settings
QDRANT_URL=[https://your-cluster.qdrant.tech](https://your-cluster.qdrant.tech)
QDRANT_API_KEY=your_qdrant_api_key

# DeepSeek API Settings
DEEPSEEK_API_KEY=your_deepseek_api_key

🚀 Quick Start & Installation
1. Environment Setup

# Clone repository and enter directory
git clone [https://github.com/your-username/telegram-rag-bot.git](https://github.com/your-username/telegram-rag-bot.git)
cd telegram-rag-bot

# Create and activate Python 3.11 virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

2. Initialize Qdrant Collections
Run the setup script to generate telegram_messages and telegram_chats collections:

python setup_db.py

3. Run Historical Scraper (Optional Initial Catch-up)
Populate Qdrant with your existing message history:

Bash
python scraper.py
4. Launch Unified Continuous Engine
Make the startup script executable and run the unified server process:

Bash
chmod +x start.sh
./start.sh
To run continuously in the background on local macOS or AWS EC2:

Bash
nohup ./start.sh > bot.log 2>&1 &
📊 Storage Monitoring & Safety

`start.py` continuously monitors your vector collection point count against `MAX_VECTORS` (default `100,000`, configurable via the `MAX_VECTORS` env var). See the **StorageShield** section above for the full warning/pause behavior. If your Qdrant Cloud storage limit approaches maximum capacity, the engine sends an automated administrative notification directly to your Telegram DM and pauses background ingestion at the critical threshold.
