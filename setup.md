# 1. Navigate to project directory
cd /path/to/nifty-ai-trading-bot

# 2. Create virtual environment
python -m venv venv

# 3. Activate it
# macOS/Linux:
source venv/bin/activate

# Windows:
venv\Scripts\activate

# 4. Install dependencies
pip install -r requirements.txt

# 5. Verify Fyers API installed
pip show fyers-apiv3

# 6. Fyers live data socket (its pinned aiohttp conflicts with requirements.txt)
pip install --no-deps fyers-apiv3==3.1.18