import asyncio
import httpx

async def test():
    print("Testing connection to Telegram API directly...")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("https://api.telegram.org/")
            print("Telegram API Status:", resp.status_code)
    except Exception as e:
        print("Telegram API Error:", repr(e))

if __name__ == "__main__":
    asyncio.run(test())
