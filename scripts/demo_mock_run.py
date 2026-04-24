"""One-off mock demo: feeds two fabricated contest tweets through the
extractor → ranker → Telegram poster, so you can see the end-to-end
pipeline without relying on Twitter scraping.

Usage: python scripts/demo_mock_run.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from contest_agent.agent import Agent
from contest_agent.extractor import regex_extract
from contest_agent.models import Tweet
from contest_agent.ranking import rank
from contest_agent.telegram import format_message


MOCK_TWEETS: list[Tweet] = [
    Tweet(
        id="mock-meme-1",
        url="https://twitter.com/example/status/1",
        author="memecoin_launches",
        author_display="Memecoin Launches",
        content=(
            "🎨 MEME CONTEST 🎨 Post your best meme for $SOLCAT — prize pool of "
            "$5,000 USDC split across top 5 winners. Must include the cashtag and "
            "tag @solcat_official. Deadline: 2026-05-01. #solana #memecoin"
        ),
        created_at=datetime.now(tz=timezone.utc),
        likes=842,
        retweets=210,
        replies=95,
        quotes=30,
        views=42000,
        source="mock",
    ),
    Tweet(
        id="mock-ai-1",
        url="https://twitter.com/example/status/2",
        author="hackathon_hq",
        author_display="Hackathon HQ",
        content=(
            "🚨 AI AGENT HACKATHON 🚨 Build an autonomous agent in 48h. "
            "$25,000 prize pool across 3 tracks (Agents, RAG, Multimodal). "
            "Register by May 10. Sponsors: OpenAI, Anthropic, Modal. #AI #LLM"
        ),
        created_at=datetime.now(tz=timezone.utc),
        likes=1503,
        retweets=412,
        replies=188,
        quotes=77,
        views=120_000,
        source="mock",
    ),
]


async def main() -> None:
    agent = Agent()
    extracted = [(t, regex_extract(t)) for t in MOCK_TWEETS]
    ranked = rank(extracted, agent.settings)

    for r in ranked:
        msg = format_message(r)
        print("=" * 60)
        print(msg)
        print()
        await agent.telegram.send(msg)
        agent.dedup.mark_posted(r.tweet.id, r.engagement_score)
        await asyncio.sleep(1.0)


if __name__ == "__main__":
    asyncio.run(main())
