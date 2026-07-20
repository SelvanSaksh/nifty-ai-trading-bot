# test_auth.py
import aiohttp
import asyncio

urls = [
    "https://api-t1.fyers.in/api/v3/generate-authcode?client_id=5LITWFWCEU-100&redirect_uri=https://trade.fyers.in/api-login/redirect-uri/index.html&response_type=code&state=niftybot",
    "https://api-t1.fyers.in/api/v3/generate-authcode?client_id=5LITWFWCEU&redirect_uri=https://trade.fyers.in/api-login/redirect-uri/index.html&response_type=code&state=niftybot",
]

async def test():
    for i, url in enumerate(urls, 1):
        print(f"\n--- Testing URL {i} ---")
        async with aiohttp.ClientSession() as session:
            async with session.get(url, allow_redirects=False) as resp:
                print(f"Status: {resp.status}")
                print(f"Location: {resp.headers.get('Location', 'No redirect')}")
                if resp.status == 200:
                    text = await resp.text()
                    print(f"Response preview: {text[:200]}")

asyncio.run(test())