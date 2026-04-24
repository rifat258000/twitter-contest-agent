from datetime import datetime, timezone

from contest_agent.models import ExtractedContest, RankedContest, Tweet
from contest_agent.telegram import format_message


def test_format_includes_key_fields():
    t = Tweet(
        id="1",
        url="https://twitter.com/a/status/1",
        author="a",
        content="meme contest",
        created_at=datetime.now(tz=timezone.utc),
        likes=5,
        retweets=2,
        replies=1,
    )
    e = ExtractedContest(
        is_contest_or_event=True,
        contest_type="meme_contest",
        prize_pool="$10,000",
        deadline="2026-05-01",
        tags=["crypto", "solana"],
        summary="Meme contest with $10k pool",
    )
    r = RankedContest(tweet=t, extracted=e, engagement_score=42.0)
    msg = format_message(r)
    assert "Meme contest" in msg
    assert "$10,000" in msg
    assert "2026-05-01" in msg
    assert "https://twitter.com/a/status/1" in msg
    assert "@a" in msg


def test_format_escapes_html():
    t = Tweet(
        id="1",
        url="https://twitter.com/a/status/1",
        author="a",
        content="x",
        created_at=datetime.now(tz=timezone.utc),
    )
    e = ExtractedContest(is_contest_or_event=True, summary="<script>alert(1)</script>")
    r = RankedContest(tweet=t, extracted=e, engagement_score=1.0)
    msg = format_message(r)
    assert "<script>" not in msg
    assert "&lt;script&gt;" in msg
