import asyncio
import logging
from sqlalchemy import select
from app.db.database import get_db_session
from app.db.models import User

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def inspect():
    print("--- DB USER AUDIT START ---")
    try:
        async with get_db_session() as session:
            stmt = select(User)
            result = await session.execute(stmt)
            users = result.scalars().all()
            
            if not users:
                print("No users found in database.")
            else:
                print(f"Found {len(users)} user(s):")
                for u in users:
                    print(f"ID: {u.id} | Telegram ID: {u.telegram_id} | Roll: {u.roll_number} | Registered: {u.created_at}")
    except Exception as e:
        print("Failed to inspect database users:", repr(e))
    print("--- DB USER AUDIT END ---")

if __name__ == "__main__":
    asyncio.run(inspect())
