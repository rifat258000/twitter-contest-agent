from datetime import datetime, timezone

from contest_agent.extractor import regex_extract
from contest_agent.models import Tweet


def _tw(content: str) -> Tweet:
    return Tweet(
        id="1",
        url="https://twitter.com/a/status/1",
        author="a",
        content=content,
        created_at=datetime.now(tz=timezone.utc),
    )


def test_meme_contest_with_prize():
    e = regex_extract(_tw("Join our meme contest! Prize pool of $10,000 USDC. #crypto"))
    assert e.is_contest_or_event is True
    assert e.contest_type == "meme_contest"
    assert e.prize_pool is not None
    assert "crypto" in e.tags


def test_ai_hackathon_with_prize():
    e = regex_extract(_tw("Announcing our AI hackathon with a $50k prize pool for LLM agents"))
    assert e.is_contest_or_event is True
    assert e.contest_type == "ai_event"
    assert e.prize_pool is not None


def test_unrelated_tweet():
    e = regex_extract(_tw("Had a great coffee this morning"))
    assert e.is_contest_or_event is False
    assert e.contest_type == "other"


def test_contest_without_prize_not_classified():
    e = regex_extract(_tw("We just had a fun meme contest last week"))
    assert e.is_contest_or_event is False
