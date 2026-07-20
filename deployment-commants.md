# 1. Clone repo on server
git clone your-repo.git
cd nifty-ai-trading-bot

# 2. Create .env file
nano .env
# Paste your credentials, save

# 3. Build and start
docker-compose up -d --build

# 4. Check logs
docker-compose logs -f bot

# 5. Health check
curl http://localhost:8000/health

# 6. Token refresh (manual trigger)
docker-compose exec bot python fyers_auth_production.py

# 7. Restart after token update
docker-compose restart bot

# 8. Scale workers (if needed)
docker-compose up -d --scale bot=2