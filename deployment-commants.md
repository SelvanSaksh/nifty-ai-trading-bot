# 1. Clone repo on server
git clone your-repo.git
cd nifty-ai-trading-bot

# 2. Create .env file
nano .env
# Paste your credentials, save

# 3. Build and start
docker compose up -d --build

# 4. Check logs
docker compose logs -f bot

# 5. Health check
curl http://localhost:8000/health
# -> look for: "engine_leader": true, "bot_running": true, "startup_error": null

# 6. Token refresh (manual trigger)
docker-compose exec bot python fyers_auth_production.py

# 7. Restart after token update
docker-compose restart bot

# 8. Do NOT scale the bot service
#    docker-compose up -d --scale bot=2   <-- never run this
#
#    Why: the trading engine must be a single process. Two engines mean
#    duplicate orders and two different "active symbol" values fighting over
#    the same API (that is what made the chart flip between instruments).
#    The app also runs uvicorn with --workers 1 for the same reason.
#    A file lock (data/engine.lock) still protects you if someone scales it
#    anyway: the extra containers serve the API read-only and report
#    "engine_leader": false in /health.

# 9. Verify which process is trading
curl http://localhost:8000/health
#    engine_leader=true  -> this container runs the engine
#    engine_leader=false -> API-only replica (safe, expected when scaled)

# 10. Inspect active-symbol changes (audit trail)
curl http://localhost:8000/api/symbols/history
