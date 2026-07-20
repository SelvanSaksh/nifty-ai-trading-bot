# test_auth_url.py
from config import settings

app_id = settings.FYERS_APP_ID
app_id_without_suffix = app_id.replace("-100", "")

urls = [
    f"https://api-t1.fyers.in/api/v3/generate-authcode?client_id={app_id}&redirect_uri=https://trade.fyers.in/api-login/redirect-uri/index.html&response_type=code&state=niftybot",
    f"https://api-t1.fyers.in/api/v3/generate-authcode?client_id={app_id_without_suffix}&redirect_uri=https://trade.fyers.in/api-login/redirect-uri/index.html&response_type=code&state=niftybot",
]

for i, url in enumerate(urls, 1):
    print(f"\n--- URL {i}: client_id={app_id if i==1 else app_id_without_suffix} ---")
    print(url)