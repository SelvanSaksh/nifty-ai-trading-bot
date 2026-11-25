"""The paper capital the bot ships with: ₹18,000 (config default and the
docker-compose fallback agree)."""
from pathlib import Path

from config import Settings


def test_the_default_capital_is_18000():
    assert Settings.model_fields["CAPITAL"].default == 18_000.0


def test_docker_compose_falls_back_to_18000():
    compose = (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text(encoding="utf-8")
    assert "- CAPITAL=${CAPITAL:-18000}" in compose
