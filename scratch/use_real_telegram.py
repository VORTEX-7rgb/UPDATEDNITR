import asyncio
import json
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from app.config import config

async def main():
    print(f"Connecting to database: {config.DATABASE_URL} ...")
    engine = create_async_engine(config.DATABASE_URL)
    try:
        async with engine.begin() as conn:
            # 1. Update user_id=1 to have real telegram_id
            real_telegram_id = 2119633824
            update_user_query = text("""
                UPDATE users
                SET telegram_id = :real_telegram_id
                WHERE id = 1;
            """)
            await conn.execute(update_user_query, {"real_telegram_id": real_telegram_id})
            print(f"Successfully updated User ID 1 telegram_id to {real_telegram_id}!")

            # 2. Insert beautiful test event
            payload = {
                "subject_name": "Database Systems",
                "subject_code": "CS301",
                "changes": {
                    "ua": {
                        "old": "1",
                        "new": "2"
                    },
                    "tc": {
                        "old": "20",
                        "new": "21"
                    }
                }
            }
            # Let's insert a real attendance_updated event or new_absence_detected event!
            # Since the user wants to see the dispatcher work, let's insert a 'new_absence_detected' event!
            absence_payload = {
                "subject_name": "Database Systems",
                "subject_code": "CS301",
                "old_ua": "1",
                "new_ua": "2",
                "total_classes": "21"
            }
            
            insert_query = text("""
                INSERT INTO events (user_id, event_type, payload_json, sent, created_at)
                VALUES (1, 'new_absence_detected', :payload_json, false, NOW())
                RETURNING id;
            """)
            res = await conn.execute(insert_query, {"payload_json": json.dumps(absence_payload)})
            event_id = res.scalar()
            print(f"Successfully inserted unsent new_absence_detected event (ID: {event_id}) for real Telegram ID!")

    except Exception as e:
        print(f"Error: {e}")
    finally:
        await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())
