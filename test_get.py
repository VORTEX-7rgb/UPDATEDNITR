import asyncio
import httpx

async def test():
    print("Testing GET /")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get("https://eapplication.nitrkl.ac.in/")
            print("Status:", resp.status_code)
    except Exception as e:
        print("ERROR:", repr(e))

if __name__ == "__main__":
    asyncio.run(test())
