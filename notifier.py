import aiohttp
from config import settings

class TelegramNotifier:
    def __init__(self):
        self.token = settings.TELEGRAM_BOT_TOKEN
        self.chat_id = settings.TELEGRAM_CHAT_ID
        self.base_url = f"https://api.telegram.org/bot{self.token}"
    
    async def send(self, message: str):
        if not self.token or not self.chat_id:
            print(f"[TELEGRAM SKIP] {message}")
            return
        
        url = f"{self.base_url}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML"
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status != 200:
                        print(f"Telegram error: {await resp.text()}")
        except Exception as e:
            print(f"Telegram send failed: {e}")