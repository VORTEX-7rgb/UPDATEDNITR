import asyncio
import httpx
from app.config import config

async def main():
    if not config.BOT_TOKEN:
        print("BOT_TOKEN is not set.")
        return
        
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/getUpdates"
    print(f"Fetching updates from Telegram API: {url} ...")
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params={"limit": 10, "timeout": 1})
            data = resp.json()
            if not data.get("ok"):
                print(f"Telegram API returned error: {data}")
                return
                
            results = data.get("result", [])
            if not results:
                print("No recent messages or updates found from Telegram. Please send a message to @nitrclawbot first!")
                return
                
            print("\nFound updates:")
            for update in results:
                msg = update.get("message")
                if msg:
                    chat = msg.get("chat", {})
                    from_user = msg.get("from", {})
                    first_name = str(from_user.get('first_name') or "").encode('ascii', errors='replace').decode('ascii')
                    last_name = str(from_user.get('last_name') or "").encode('ascii', errors='replace').decode('ascii')
                    username = str(from_user.get('username') or "").encode('ascii', errors='replace').decode('ascii')
                    msg_text = str(msg.get('text') or "").encode('ascii', errors='replace').decode('ascii')
                    print(f"--- Update {update.get('update_id')} ---")
                    print(f"User: {first_name} {last_name} (@{username})")
                    print(f"Telegram ID (Chat ID): {chat.get('id')}")
                    print(f"Message Text: {msg_text}")
                    
    except Exception as e:
        print(f"Error fetching updates: {e}")

if __name__ == "__main__":
    asyncio.run(main())
