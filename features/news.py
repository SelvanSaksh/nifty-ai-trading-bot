"""
News feed monitoring and blackout periods.
Prevents trading during high-impact news events.
"""
import aiohttp
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import List, Dict, Optional, Any
from dataclasses import dataclass
import re

from config import settings


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class NewsEvent:
    title: str
    source: str
    published: datetime  # timezone-aware, UTC
    impact: str
    keywords: List[str]

    def is_blackout(self, now: datetime = None, blackout_minutes: int = 15) -> bool:
        """Check if current time falls within blackout window.

        ``now`` must be timezone-aware (default: the current UTC time).
        """
        if now is None:
            now = utc_now()
        window_start = self.published - timedelta(minutes=blackout_minutes)
        window_end = self.published + timedelta(minutes=blackout_minutes)
        return window_start <= now <= window_end


class NewsMonitor:
    """Monitors financial news for trading blackouts."""
    
    # Matched as whole words, ignoring case ("war" is not "software", "fed"
    # is not "Fedbank"). Words that describe the market itself ("nifty",
    # "rally", "crash") are not events: they appear in routine market
    # headlines all day and would block every trade.
    HIGH_IMPACT_KEYWORDS = [
        "RBI", "repo rate", "monetary policy", "GDP", "inflation",
        "CPI", "WPI", "IIP", "trade deficit", "budget", "union budget",
        "election results", "PMI", "crude oil", "geopolitical",
        "war", "sanctions", "fed", "fomc",
    ]
    _KEYWORD_PATTERNS = [
        (kw, re.compile(rf"(?<![A-Za-z0-9]){re.escape(kw)}(?![A-Za-z0-9])", re.IGNORECASE))
        for kw in HIGH_IMPACT_KEYWORDS
    ]
    
    RSS_FEEDS = [
        "https://www.moneycontrol.com/rss/MCtopnews.xml",
        "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
        "https://www.livemint.com/rss/markets",
    ]
    
    def __init__(self):
        self._events: List[NewsEvent] = []
        self._last_fetch: Optional[datetime] = None
    
    async def is_blackout_active(self) -> bool:
        """Check if any high-impact news is within blackout window."""
        # Refresh if needed
        if not self._last_fetch or (datetime.now() - self._last_fetch) > timedelta(minutes=5):
            await self._fetch_news()
        
        now = utc_now()
        for event in self._events:
            if event.impact == "high" and event.is_blackout(now, settings.NEWS_BLACKOUT_MINUTES):
                return True
        return False
    
    async def get_active_blackouts(self) -> List[Dict[str, Any]]:
        """Get list of currently active blackout events."""
        now = utc_now()
        active = []
        for event in self._events:
            if event.impact == "high" and event.is_blackout(now, settings.NEWS_BLACKOUT_MINUTES):
                active.append({
                    "title": event.title,
                    "source": event.source,
                    "published": event.published.isoformat(),
                    "minutes_remaining": self._minutes_remaining(event, now)
                })
        return active
    
    def _minutes_remaining(self, event: NewsEvent, now: datetime) -> int:
        window_end = event.published + timedelta(minutes=settings.NEWS_BLACKOUT_MINUTES)
        remaining = (window_end - now).total_seconds() / 60
        return max(0, int(remaining))
    
    async def _fetch_news(self):
        """Fetch news from RSS feeds."""
        self._events = []
        
        async with aiohttp.ClientSession() as session:
            for feed_url in self.RSS_FEEDS:
                try:
                    async with session.get(feed_url, timeout=10) as resp:
                        if resp.status == 200:
                            content = await resp.text()
                            self._parse_rss(content)
                except Exception as e:
                    print(f"News fetch error for {feed_url}: {e}")
        
        self._last_fetch = datetime.now()
    
    def _parse_rss(self, xml_content: str):
        """Parse RSS XML into NewsEvent objects."""
        try:
            root = ET.fromstring(xml_content)
            # Handle different RSS namespaces
            items = root.findall(".//item")
            
            for item in items:
                title_elem = item.find("title")
                pub_date_elem = item.find("pubDate")
                source_elem = item.find("source")
                
                if title_elem is None:
                    continue
                
                title = title_elem.text or ""
                # `is not None`: an element without children is falsy, so a
                # truthiness test dropped every date.
                pub_date = self._parse_date(
                    pub_date_elem.text if pub_date_elem is not None else None
                )
                if pub_date is None:
                    # No usable time: the item cannot be placed in a window.
                    continue
                source = source_elem.text if source_elem is not None else "Unknown"
                
                # Determine impact
                impact = self._classify_impact(title)
                keywords = self._extract_keywords(title)
                
                self._events.append(NewsEvent(
                    title=title,
                    source=source,
                    published=pub_date,
                    impact=impact,
                    keywords=keywords
                ))
        except ET.ParseError:
            pass
    
    def _parse_date(self, date_str: Optional[str]) -> Optional[datetime]:
        """An RSS date (RFC 2822, or ISO 8601 with an offset) as aware UTC.

        None when it is missing, malformed or has no timezone: guessing a
        time (it used to fall back to "now") puts stale headlines into the
        blackout window.
        """
        text = (date_str or "").strip()
        if not text:
            return None
        parsed = None
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            # RFC 2822 "-0000" means UTC with an unknown local zone; an ISO
            # time without an offset is ambiguous.
            if "-0000" not in text:
                return None
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    
    def _classify_impact(self, title: str) -> str:
        """Classify news impact based on keywords."""
        title_lower = title.lower()

        high_impact = any(pattern.search(title) for _, pattern in self._KEYWORD_PATTERNS)

        if high_impact:
            return "high"
        
        medium_keywords = ["earnings", "quarterly", "results", "dividend", "split"]
        if any(kw in title_lower for kw in medium_keywords):
            return "medium"
        
        return "low"
    
    def _extract_keywords(self, title: str) -> List[str]:
        """Extract relevant keywords from title."""
        return [kw for kw, pattern in self._KEYWORD_PATTERNS if pattern.search(title)]


news_monitor = NewsMonitor()