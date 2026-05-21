import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from app.config import config

async def main():
    print(f"Connecting to database: {config.DATABASE_URL} ...")
    engine = create_async_engine(config.DATABASE_URL)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT id, telegram_id, roll_number FROM users;"))
            users = result.fetchall()
            print("Users in database:")
            for user in users:
                print(f"ID: {user[0]}, Telegram ID: {user[1]}, Roll: {user[2]}")
    except Exception as e:
        print(f"Error connecting: {e}")
    finally:
        await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())
