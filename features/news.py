"""
News feed monitoring and blackout periods.
Prevents trading during high-impact news events.
"""
import aiohttp
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any
from dataclasses import dataclass
import re

from config import settings


@dataclass
class NewsEvent:
    title: str
    source: str
    published: datetime
    impact: str        
    keywords: List[str]
    
    def is_blackout(self, now: datetime = None, blackout_minutes: int = 15) -> bool:
        """Check if current time falls within blackout window."""
        if now is None:
            now = datetime.now()
        window_start = self.published - timedelta(minutes=blackout_minutes)
        window_end = self.published + timedelta(minutes=blackout_minutes)
        return window_start <= now <= window_end


class NewsMonitor:
    """Monitors financial news for trading blackouts."""
    
    HIGH_IMPACT_KEYWORDS = [
        "RBI", "repo rate", "monetary policy", "GDP", "inflation",
        "CPI", "WPI", "IIP", "trade deficit", "budget", "union budget",
        "election results", "PMI", "crude oil", "geopolitical",
        "war", "sanctions", "fed", "fomc", "nifty", "crash", "rally"
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
        
        now = datetime.now()
        for event in self._events:
            if event.impact == "high" and event.is_blackout(now, settings.NEWS_BLACKOUT_MINUTES):
                return True
        return False
    
    async def get_active_blackouts(self) -> List[Dict[str, Any]]:
        """Get list of currently active blackout events."""
        now = datetime.now()
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
                pub_date = self._parse_date(pub_date_elem.text if pub_date_elem else "")
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
    
    def _parse_date(self, date_str: str) -> datetime:
        """Parse various RSS date formats."""
        formats = [
            "%a, %d %b %Y %H:%M:%S %z",
            "%a, %d %b %Y %H:%M:%S GMT",
            "%Y-%m-%dT%H:%M:%S%z",
        ]
        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt)
            except (ValueError, TypeError):
                continue
        return datetime.now()
    
    def _classify_impact(self, title: str) -> str:
        """Classify news impact based on keywords."""
        title_lower = title.lower()
        
        high_impact = any(kw.lower() in title_lower for kw in self.HIGH_IMPACT_KEYWORDS)
        
        if high_impact:
            return "high"
        
        medium_keywords = ["earnings", "quarterly", "results", "dividend", "split"]
        if any(kw in title_lower for kw in medium_keywords):
            return "medium"
        
        return "low"
    
    def _extract_keywords(self, title: str) -> List[str]:
        """Extract relevant keywords from title."""
        found = []
        title_lower = title.lower()
        for kw in self.HIGH_IMPACT_KEYWORDS:
            if kw.lower() in title_lower:
                found.append(kw)
        return found


news_monitor = NewsMonitor()