from datetime import datetime, timedelta

import pytest

from decision_engine import BEARISH_PATTERNS, BULLISH_PATTERNS, DecisionEngine
from models.candle import Candle
from utils.patterns import detect_candlestick_patterns, get_pattern_details

START = datetime(2026, 10, 20, 9, 15)


def _bars(*ohlc):
    return [
        Candle(timestamp=START + timedelta(minutes=15 * i), open=o, high=h, low=l, close=c, volume=0)
        for i, (o, h, l, c) in enumerate(ohlc)
    ]


# Bearish bar, then a bar opening below its low and closing above its midpoint (95).
PIERCING = _bars((100, 101, 88, 90), (87, 97, 86, 96))
# Bullish bar, then a bar opening above its high and closing below its midpoint (95).
DARK_CLOUD = _bars((90, 102, 89, 100), (103, 104, 93, 94))


class TestPatternNames:
    def test_every_scored_name_is_one_the_detector_reports(self):
        detector_names = {
            p.name
            for p in get_pattern_details(PIERCING)
            + get_pattern_details(DARK_CLOUD)
        }
        known = {
            "hammer", "shooting_star", "engulfing_bullish", "engulfing_bearish",
            "piercing_pattern", "dark_cloud_cover", "harami_bullish", "harami_bearish",
            "morning_star", "evening_star", "three_white_soldiers", "three_black_crows",
        }
        assert detector_names <= known
        assert set(BULLISH_PATTERNS) <= known
        assert set(BEARISH_PATTERNS) <= known


class TestPatternScoring:
    @pytest.fixture
    def engine(self):
        return DecisionEngine()

    def test_piercing_pattern_adds_to_the_long_score(self, engine):
        patterns = detect_candlestick_patterns(PIERCING)
        assert "piercing_pattern" in patterns
        candle = PIERCING[-1]
        with_pattern = engine._score_long({"candle": candle, "patterns": patterns})
        without = engine._score_long({"candle": candle, "patterns": []})
        assert with_pattern - without == 15

    def test_dark_cloud_cover_adds_to_the_short_score(self, engine):
        patterns = detect_candlestick_patterns(DARK_CLOUD)
        assert "dark_cloud_cover" in patterns
        candle = DARK_CLOUD[-1]
        with_pattern = engine._score_short({"candle": candle, "patterns": patterns})
        without = engine._score_short({"candle": candle, "patterns": []})
        assert with_pattern - without == 15

    def test_bullish_pattern_does_not_score_short(self, engine):
        candle = PIERCING[-1]
        assert engine._score_short(
            {"candle": candle, "patterns": ["piercing_pattern"]}
        ) == engine._score_short({"candle": candle, "patterns": []})

    def test_pattern_bonus_is_counted_once(self, engine):
        candle = PIERCING[-1]
        both = engine._score_long(
            {"candle": candle, "patterns": ["hammer", "piercing_pattern"]}
        )
        one = engine._score_long({"candle": candle, "patterns": ["hammer"]})
        assert both == one
