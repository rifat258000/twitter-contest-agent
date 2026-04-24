from datetime import datetime, timezone

from contest_agent.config import Settings
from contest_agent.models import ExtractedContest, Tweet
from contest_agent.ranking import engagement_score, rank


def _tweet(likes=0, rts=0, replies=0, quotes=0, views=0, tid="1") -> Tweet:
    return Tweet(
        id=tid,
        url=f"https://twitter.com/a/status/{tid}",
        author="a",
        content="x",
        created_at=datetime.now(tz=timezone.utc),
        likes=likes,
        retweets=rts,
        replies=replies,
        quotes=quotes,
        views=views,
    )


def test_engagement_score_weights():
    s = Settings()
    t = _tweet(likes=10, rts=2, replies=3, quotes=1, views=1000)
    expected = 10 * 1.0 + 2 * 2.0 + 3 * 1.5 + 1 * 2.0 + 1000 * 0.001
    assert engagement_score(t, s) == expected


def test_rank_sorts_descending():
    s = Settings()
    a = (_tweet(likes=1, tid="a"), ExtractedContest(is_contest_or_event=True))
    b = (_tweet(likes=100, tid="b"), ExtractedContest(is_contest_or_event=True))
    c = (_tweet(likes=10, tid="c"), ExtractedContest(is_contest_or_event=True))
    ranked = rank([a, b, c], s)
    assert [r.tweet.id for r in ranked] == ["b", "c", "a"]
