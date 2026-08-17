# NITRClaw 🦞

A high-performance, asynchronous Telegram bot and automated sync engine for the NIT Rourkela **NITRIS** academic portal.

---

## Features

- **Automated Attendance Monitoring**: Periodically scrapes and parses live attendance records, calculates cryptographic SHA-256 hashes, and alerts students on attendance changes or missed classes.
- **Notice & Message Inbox**: Scrapes portal notices, supports lazy loading of full announcements, and downloads attachments with caching.
- **Previous Year Question Papers**: Resolves Examination modules dynamically, queries question paper archives across academic years, and streams PDFs.
- **Resilient WebForms Engine**: Features self-healing dynamic ASP.NET module launcher URL resolution, date-aware semester autodetection, and automated session recovery.
- **Production Architecture**: Built with `aiogram 3`, `SQLAlchemy 2.0 (asyncio + asyncpg)`, PostgreSQL JSONB, and version-controlled `Alembic` migrations.

---

## Tech Stack

- **Python 3.11+**
- **Aiogram 3.6** (Telegram Bot Framework)
- **HTTPX & BeautifulSoup4** (Asynchronous WebForms Scraper)
- **SQLAlchemy 2.0 & Asyncpg** (Async ORM & Postgres driver)
- **Alembic** (Database schema migrations)
- **Cryptography / Fernet** (AES-128-CBC credential encryption at rest)

---

## Project Structure

```
├── alembic/                  # Database migration scripts
│   └── versions/
│       └── 0001_initial_schema.py
├── app/
│   ├── bot/                  # Aiogram handlers, UI keyboards & routers
│   ├── db/                   # SQLAlchemy models, async session & repositories
│   ├── nitris/               # WebForms client, postback engine & parsers
│   ├── services/             # Attendance, Events, Examination & Lock services
│   ├── workers/              # Background sync & dispatch worker loops
│   ├── config.py             # Environment configuration
│   ├── main.py               # Application entrypoint
│   └── utils.py              # Helper utilities
├── alembic.ini               # Alembic configuration
├── requirements.txt          # Python dependencies
└── README.md
```

---

## Setup & Running

### 1. Clone & Install Dependencies
```bash
git clone https://github.com/VORTEX-7rgb/nitrclaw-.git
cd nitrclaw-
pip install -r requirements.txt
```

### 2. Environment Configuration
Create a `.env` file in the root directory:
```env
BOT_TOKEN=your_telegram_bot_token
NITRIS_BASE_URL=https://eapplication.nitrkl.ac.in
ENCRYPTION_KEY=your_fernet_encryption_key
DATABASE_URL=postgresql+asyncpg://username:password@localhost:5432/collegeclaw
```

### 3. Database Migration
```bash
alembic upgrade head
```

### 4. Start the Application
```bash
python -m app.main
```
