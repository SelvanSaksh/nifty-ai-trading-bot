"""
MCP Server for Trading Bot.
Exposes 10 tools + in-app Claude assistant integration.
"""
from fastapi import FastAPI

from trading_mcp.tools import MCPTools

mcp_app = FastAPI(title="Trading MCP Server")

tools = MCPTools()


@mcp_app.on_event("startup")
async def startup():
    """Link tools to bot instance on startup."""
    # This will be set by main.py when bot starts
    pass


def set_bot_instance(bot):
    """Called from main.py to link the running bot."""
    tools.set_bot(bot)


@mcp_app.get("/tools")
async def list_tools():
    """List all available MCP tools."""
    return {
        "tools": [
            {
                "name": "get_bot_status",
                "description": "Running state, uptime, active trade, paper/live mode",
                "parameters": {},
                "read_only": True
            },
            {
                "name": "get_trading_stats",
                "description": "Win rate, total P&L, today's P&L",
                "parameters": {"timeframe": {"type": "string", "enum": ["today", "week", "month", "year", "all"], "default": "all"}},
                "read_only": True
            },
            {
                "name": "get_recent_trades",
                "description": "Recent closed trades from the journal",
                "parameters": {"limit": {"type": "integer", "default": 10}},
                "read_only": True
            },
            {
                "name": "get_recent_signals",
                "description": "Recent signals with confidence and reasoning",
                "parameters": {"limit": {"type": "integer", "default": 10}},
                "read_only": True
            },
            {
                "name": "get_candles",
                "description": "OHLCV candles for chart analysis. The response states which symbol the candles belong to; pass symbol to view an instrument other than the active one.",
                "parameters": {
                    "timeframe": {"type": "string", "default": "15m"},
                    "limit": {"type": "integer", "default": 50},
                    "symbol": {"type": "string", "default": None,
                               "description": "e.g. NSE:INFY-EQ. Defaults to the active trading symbol."}
                },
                "read_only": True
            },
            {
                "name": "get_market_context",
                "description": "Options PCR, OI shifts, VIX regime",
                "parameters": {},
                "read_only": True
            },
            {
                "name": "get_config_summary",
                "description": "Key configuration values (no secrets)",
                "parameters": {},
                "read_only": True
            },
            {
                "name": "explain_latest_signal",
                "description": "Human-readable rationale for the latest signal",
                "parameters": {},
                "read_only": True
            },
            {
                "name": "start_bot",
                "description": "Start the engine (explicit request only)",
                "parameters": {"confirm": {"type": "boolean", "default": False}},
                "read_only": False
            },
            {
                "name": "stop_bot",
                "description": "Stop the engine (explicit request only)",
                "parameters": {"confirm": {"type": "boolean", "default": False}},
                "read_only": False
            },
        ],
        "version": "1.0.0",
        "read_only_tools": 8,
        "state_changing_tools": 2
    }


@mcp_app.post("/invoke/{tool_name}")
async def invoke_tool(tool_name: str, params: dict = None):
    """Invoke a specific MCP tool."""
    return await tools.invoke(tool_name, params)


@mcp_app.get("/health")
async def health_check():
    """MCP server health check."""
    return {
        "status": "healthy",
        "bot_linked": tools.bot_instance is not None,
        "bot_running": tools.bot_instance.is_running if tools.bot_instance else False
    }