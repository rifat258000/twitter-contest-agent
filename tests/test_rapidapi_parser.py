from contest_agent.scraper import _rapidapi_to_tweet


def test_parse_typical_tweet():
    # Matches the actual twitter-api45 payload: top-level screen_name, user_info.
    raw = {
        "tweet_id": "1234567890",
        "text": "Meme contest with $5000 USDC prize pool!",
        "created_at": "Wed Apr 23 12:00:00 +0000 2026",
        "favorites": 42,
        "retweets": 10,
        "replies": 3,
        "quotes": 1,
        "views": "12345",
        "screen_name": "alice",
        "user_info": {"screen_name": "alice", "name": "Alice"},
    }
    tweet = _rapidapi_to_tweet(raw)
    assert tweet is not None
    assert tweet.id == "1234567890"
    assert tweet.author == "alice"
    assert tweet.author_display == "Alice"
    assert tweet.content.startswith("Meme contest")
    assert tweet.likes == 42
    assert tweet.retweets == 10
    assert tweet.replies == 3
    assert tweet.quotes == 1
    assert tweet.views == 12345
    assert tweet.url == "https://twitter.com/alice/status/1234567890"
    assert tweet.source == "rapidapi:twitter-api45"


def test_parse_missing_optional_fields():
    raw = {
        "tweet_id": "1",
        "text": "hi",
        "screen_name": "bob",
    }
    tweet = _rapidapi_to_tweet(raw)
    assert tweet is not None
    assert tweet.author == "bob"
    assert tweet.likes == 0
    assert tweet.retweets == 0
    assert tweet.views == 0


def test_parse_legacy_author_schema():
    raw = {
        "tweet_id": "1",
        "text": "hi",
        "author": {"screen_name": "carol", "name": "Carol"},
    }
    tweet = _rapidapi_to_tweet(raw)
    assert tweet is not None
    assert tweet.author == "carol"
    assert tweet.author_display == "Carol"


def test_parse_missing_id_returns_none():
    assert _rapidapi_to_tweet({"text": "no id"}) is None


def test_parse_non_integer_counts_coerced_to_zero():
    raw = {"tweet_id": "1", "text": "x", "favorites": "not-a-number"}
    tweet = _rapidapi_to_tweet(raw)
    assert tweet is not None
    assert tweet.likes == 0
