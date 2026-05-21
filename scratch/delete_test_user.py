import asyncio
import logging
from sqlalchemy import select
from app.db.database import get_db_session
from app.db.models import User
from app.db.repositories.user_repository import UserRepository

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def delete_test_user():
    print("--- DB DELETE TEST USER START ---")
    try:
        async with get_db_session() as session:
            stmt = select(User).where(User.roll_number == "987CS1234")
            result = await session.execute(stmt)
            user = result.scalar_one_or_none()
            
            if not user:
                print("Test user '987CS1234' not found.")
            else:
                print(f"Found test user: ID {user.id}, Roll: {user.roll_number}, Telegram: {user.telegram_id}")
                repo = UserRepository(session)
                await repo.delete_user(user.id)
                await session.commit()
                print("Test user deleted successfully and transaction committed.")
    except Exception as e:
        print("Failed to delete test user:", repr(e))
    print("--- DB DELETE TEST USER END ---")

if __name__ == "__main__":
    asyncio.run(delete_test_user())
