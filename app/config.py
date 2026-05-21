import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")
    NITRIS_BASE_URL = os.getenv("NITRIS_BASE_URL", "https://eapplication.nitrkl.ac.in")
    DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/collegeclaw")
    ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")
    
config = Config()
