"""Core data models used across the agent."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ContestType = Literal["meme_contest", "ai_event", "other"]


class Tweet(BaseModel):
    """A single tweet normalized from any scraping source."""

    id: str
    url: str
    author: str
    author_display: str = ""
    content: str
    created_at: datetime
    likes: int = 0
    retweets: int = 0
    replies: int = 0
    quotes: int = 0
    views: int = 0
    source: str = "unknown"


class ExtractedContest(BaseModel):
    """Info extracted from a tweet, either via LLM or regex fallback."""

    is_contest_or_event: bool = False
    contest_type: ContestType = "other"
    prize_pool: str | None = None
    prize_pool_usd: float | None = None
    deadline: str | None = None
    tags: list[str] = Field(default_factory=list)
    summary: str = ""


class RankedContest(BaseModel):
    """A tweet + extracted info + engagement score, ready to post."""

    tweet: Tweet
    extracted: ExtractedContest
    engagement_score: float

    @property
    def rank_key(self) -> float:
        return self.engagement_score
