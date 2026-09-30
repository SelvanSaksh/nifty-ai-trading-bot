import asyncio

import aiohttp

from fyers_auth import get_auth_url


async def test():
    url = get_auth_url()
    print(f"\n--- {url}")
    async with aiohttp.ClientSession() as session:
        async with session.get(url, allow_redirects=False) as resp:
            print(f"Status: {resp.status}")
            print(f"Location: {resp.headers.get('Location', 'No redirect')}")
            if resp.status == 200:
                text = await resp.text()
                print(f"Response preview: {text[:200]}")


asyncio.run(test())
