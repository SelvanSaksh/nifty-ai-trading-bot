"""News blackout: RSS dates, whole-word keywords, and the blackout window.

The suite's autouse ``_offline_news`` fixture stubs ``_fetch_news``; these
tests feed RSS straight into ``_parse_rss`` instead, in the format the live
feeds (Economic Times, Mint) use.
"""
from datetime import datetime, timedelta, timezone

import pytest

from config import settings
from features.news import NewsMonitor

IST = timezone(timedelta(hours=5, minutes=30))


def rss(*items: str) -> str:
    return f'<?xml version="1.0"?><rss><channel>{"".join(items)}</channel></rss>'


def item(title: str, pub_date: str = None) -> str:
    date = f"<pubDate>{pub_date}</pubDate>" if pub_date is not None else ""
    return f"<item><title>{title}</title>{date}</item>"


def monitor_with(*items: str) -> NewsMonitor:
    monitor = NewsMonitor()
    monitor._parse_rss(rss(*items))
    return monitor


def test_pub_date_is_read_not_replaced_by_now():
    m = monitor_with(item("RBI keeps repo rate unchanged", "Wed, 28 Oct 2026 12:21:00 +0530"))
    assert len(m._events) == 1
    assert m._events[0].published == datetime(2026, 10, 28, 6, 51, tzinfo=timezone.utc)


@pytest.mark.parametrize("raw, expected", [
    ("Wed, 28 Oct 2026 23:31:23 +0530", datetime(2026, 10, 28, 18, 1, 23, tzinfo=timezone.utc)),
    ("Wed, 28 Oct 2026 18:01:23 GMT", datetime(2026, 10, 28, 18, 1, 23, tzinfo=timezone.utc)),
    ("Wed, 28 Oct 2026 18:01:23 -0000", datetime(2026, 10, 28, 18, 1, 23, tzinfo=timezone.utc)),
    ("2026-10-28T23:31:23+05:30", datetime(2026, 10, 28, 18, 1, 23, tzinfo=timezone.utc)),
    ("2026-10-28T18:01:23Z", datetime(2026, 10, 28, 18, 1, 23, tzinfo=timezone.utc)),
])
def test_dates_parse_to_utc(raw, expected):
    assert NewsMonitor()._parse_date(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "not a date", "2026-10-28T18:01:23"])
def test_unusable_dates_are_none(raw):
    assert NewsMonitor()._parse_date(raw) is None


def test_items_without_a_usable_date_are_skipped():
    m = monitor_with(
        item("RBI hikes repo rate"),                       # no pubDate
        item("RBI hikes repo rate", "yesterday"),          # malformed
        item("RBI hikes repo rate", "Wed, 28 Oct 2026 12:00:00 +0530"),
    )
    assert len(m._events) == 1


@pytest.mark.parametrize("title, keywords", [
    ("RBI keeps repo rate unchanged", ["RBI", "repo rate"]),
    ("US Fed holds rates; FOMC signals cuts", ["fed", "fomc"]),
    ("Israel-Iran war escalates", ["war"]),
    ("CPI inflation eases to 4.2%", ["inflation", "CPI"]),
])
def test_event_headlines_are_high_impact(title, keywords):
    m = NewsMonitor()
    assert m._classify_impact(title) == "high"
    assert sorted(m._extract_keywords(title)) == sorted(keywords)


@pytest.mark.parametrize("title", [
    "Infosys software arm wins award",          # "war" inside words
    "Analysts warn on IT stocks",
    "Toward a greener economy",
    "Fedbank Financial shares rise",            # "fed" inside a word
    "Sensex, Nifty open flat",                  # market words are not events
    "Stock market today: Nifty 50 rallies 0.2%",
    "Bank stocks crash after weak results",
])
def test_ordinary_headlines_are_not_high_impact(title):
    m = NewsMonitor()
    assert m._classify_impact(title) != "high"
    assert m._extract_keywords(title) == []


def test_blackout_window_is_around_the_publish_time():
    m = monitor_with(item("RBI hikes repo rate", "Wed, 28 Oct 2026 12:00:00 +0530"))
    event = m._events[0]
    published = datetime(2026, 10, 28, 12, 0, tzinfo=IST)
    window = timedelta(minutes=settings.NEWS_BLACKOUT_MINUTES)

    assert event.is_blackout(published, settings.NEWS_BLACKOUT_MINUTES)
    assert event.is_blackout(published + window, settings.NEWS_BLACKOUT_MINUTES)
    assert not event.is_blackout(
        published + window + timedelta(seconds=1), settings.NEWS_BLACKOUT_MINUTES
    )
    assert not event.is_blackout(
        published - window - timedelta(seconds=1), settings.NEWS_BLACKOUT_MINUTES
    )


async def test_old_headlines_do_not_block(monkeypatch):
    """The regression: every item used to be stamped "now", so any keyword
    headline in the feed, however old, kept the blackout on."""
    m = monitor_with(
        item("RBI hikes repo rate", "Wed, 05 Oct 2016 14:00:44 +0530"),
        item("US Fed holds rates", "Wed, 28 Oct 2026 09:00:00 +0530"),
    )
    m._last_fetch = datetime.now()  # skip the network refresh
    now = datetime(2026, 10, 28, 11, 0, tzinfo=IST)
    monkeypatch.setattr("features.news.utc_now", lambda: now.astimezone(timezone.utc))

    assert await m.is_blackout_active() is False
    assert await m.get_active_blackouts() == []


async def test_fresh_event_headline_blocks(monkeypatch):
    m = monitor_with(item("RBI hikes repo rate", "Wed, 28 Oct 2026 10:55:00 +0530"))
    m._last_fetch = datetime.now()
    now = datetime(2026, 10, 28, 11, 0, tzinfo=IST)
    monkeypatch.setattr("features.news.utc_now", lambda: now.astimezone(timezone.utc))

    assert await m.is_blackout_active() is True
    active = await m.get_active_blackouts()
    assert [a["title"] for a in active] == ["RBI hikes repo rate"]
    assert active[0]["minutes_remaining"] == settings.NEWS_BLACKOUT_MINUTES - 5
