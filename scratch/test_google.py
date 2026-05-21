import asyncio
import httpx
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def test_conn():
    url = "https://www.google.com"
    logger.info(f"Connecting to {url}...")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
            logger.info(f"Connected successfully! Status: {resp.status_code}, Bytes: {len(resp.text)}")
    except Exception as e:
        logger.error(f"Failed to connect: {repr(e)}")

if __name__ == "__main__":
    asyncio.run(test_conn())
