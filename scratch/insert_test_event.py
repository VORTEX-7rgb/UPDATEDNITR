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
            # Step 1: Look at existing users
            result = await conn.execute(text("SELECT id, telegram_id, roll_number FROM users;"))
            users = result.fetchall()
            if not users:
                print("No users found in database! Creating a test user...")
                # Insert a test user if none exist (though we found user_id=1 already)
                await conn.execute(text(
                    "INSERT INTO users (telegram_id, roll_number, encrypted_password) "
                    "VALUES (1122334455, '987CS1234', 'gAAAAABmR...') RETURNING id;"
                ))
                # Refetch
                result = await conn.execute(text("SELECT id, telegram_id, roll_number FROM users;"))
                users = result.fetchall()

            user_id = users[0][0]
            print(f"Using user_id = {user_id}")

            # Step 3: Insert fake event
            payload = {
                "subject_name": "Database Systems",
                "subject_code": "CS301",
                "message": "Dispatcher test notification"
            }
            payload_str = json.dumps(payload)

            insert_query = text("""
                INSERT INTO events (user_id, event_type, payload_json, sent, created_at)
                VALUES (:user_id, 'test_notification', :payload_json, false, NOW())
                RETURNING id;
            """)
            
            res = await conn.execute(insert_query, {"user_id": user_id, "payload_json": payload_str})
            event_id = res.scalar()
            print(f"Inserted fake event ID: {event_id}")

            # Step 4: Verify event exists
            verify_query = text("SELECT id, event_type, sent, payload_json FROM events WHERE id = :event_id;")
            verify_res = await conn.execute(verify_query, {"event_id": event_id})
            event_row = verify_res.fetchone()
            print("Verified event in database:")
            print(f"ID: {event_row[0]}, Type: {event_row[1]}, Sent: {event_row[2]}, Payload: {event_row[3]}")

    except Exception as e:
        print(f"Error: {e}")
    finally:
        await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())
