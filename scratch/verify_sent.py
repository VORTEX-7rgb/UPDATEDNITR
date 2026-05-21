import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from app.config import config

async def main():
    print(f"Connecting to database: {config.DATABASE_URL} ...")
    engine = create_async_engine(config.DATABASE_URL)
    try:
        async with engine.connect() as conn:
            query = text("SELECT id, event_type, sent, payload_json FROM events WHERE event_type = 'test_notification' ORDER BY id DESC LIMIT 5;")
            res = await conn.execute(query)
            rows = res.fetchall()
            print("\n=======================================================")
            print("             DATABASE SENT VERIFICATION REPORT         ")
            print("=======================================================")
            for row in rows:
                print(f"Event ID: {row[0]}, Type: {row[1]}, Sent: {row[2]}, Payload: {row[3]}")
            print("=======================================================")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())
