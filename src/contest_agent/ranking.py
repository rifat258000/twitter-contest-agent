"""Engagement-based ranking of contest tweets."""

from __future__ import annotations

from .config import Settings
from .models import ExtractedContest, RankedContest, Tweet


def engagement_score(tweet: Tweet, settings: Settings) -> float:
    return (
        tweet.likes * settings.weight_likes
        + tweet.retweets * settings.weight_retweets
        + tweet.replies * settings.weight_replies
        + tweet.quotes * settings.weight_quotes
        + tweet.views * settings.weight_views
    )


def rank(
    items: list[tuple[Tweet, ExtractedContest]], settings: Settings
) -> list[RankedContest]:
    ranked = [
        RankedContest(
            tweet=t,
            extracted=e,
            engagement_score=engagement_score(t, settings),
        )
        for t, e in items
    ]
    ranked.sort(key=lambda r: r.engagement_score, reverse=True)
    return ranked
