"""Shared fixtures: isolated database, offline news source, app client."""
from datetime import datetime

import pytest


@pytest.fixture(autouse=True)
def _offline_news(monkeypatch):
    """Keep the suite off the network (RSS is only used for news blackouts)."""
    async def _noop(self):
        self._events = []
        self._last_fetch = datetime.now()

    monkeypatch.setattr("features.news.NewsMonitor._fetch_news", _noop)
    # Tests must stay deterministic and offline. Production defaults to Fyers.
    from config import settings
    monkeypatch.setattr(settings, "BROKER_MODE", "mock")


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    """Point the app at a throwaway database + engine lock per test."""
    from config import settings

    path = tmp_path / "trades.db"
    monkeypatch.setattr(settings, "DB_PATH", str(path))
    return path


@pytest.fixture()
def client(db_path):
    """FastAPI test client with lifespan (bot engine) running."""
    from fastapi.testclient import TestClient

    import main

    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture()
def fast_client(db_path, monkeypatch):
    """Same as ``client`` but with a fast symbol-reconcile loop."""
    from fastapi.testclient import TestClient

    from bot import NiftyBot

    monkeypatch.setattr(NiftyBot, "RECONCILE_INTERVAL", 0.15)
    import main

    with TestClient(main.app) as test_client:
        yield test_client
