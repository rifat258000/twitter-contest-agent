"""Default Twitter search queries for meme contests and AI events."""

from __future__ import annotations

# These are written as simple search strings. The scraper module applies the
# Twitter advanced search operators it supports (e.g. min_faves, -filter:replies).
DEFAULT_QUERIES: list[str] = [
    # Meme contests
    "meme contest prize",
    "meme competition win",
    "meme contest $",
    "meme contest prize pool",
    "memecoin meme contest",
    # Crypto/NFT meme contests
    '"meme contest" crypto',
    '"meme contest" NFT',
    '"meme contest" airdrop',
    # AI events / hackathons
    "AI hackathon prize",
    "AI hackathon register",
    "AI agent hackathon",
    "LLM hackathon prize pool",
    "generative AI hackathon",
    "AI event prize pool",
]

# Keywords used by the regex/keyword fallback classifier.
MEME_CONTEST_KEYWORDS: list[str] = [
    "meme contest",
    "meme competition",
    "meme challenge",
    "meme war",
    "meme-off",
    "meme airdrop",
]

AI_EVENT_KEYWORDS: list[str] = [
    "ai hackathon",
    "llm hackathon",
    "ml hackathon",
    "ai event",
    "ai summit",
    "ai conference",
    "ai agent hackathon",
    "generative ai",
    "ai competition",
]

PRIZE_KEYWORDS: list[str] = [
    "prize",
    "prize pool",
    "reward",
    "bounty",
    "winner",
    "win ",
    "$",
    "usd",
    "eth",
    "sol",
    "usdc",
]
