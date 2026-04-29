"""
Contest discovery for RIFAT < AI.

Searches X for currently-running contests (AI / video / meme / general giveaways)
via the existing twscrape pool, scores them by weighted engagement, and returns
the top results. Reuses the same auth/cookie path as the reply-generator, so a
single X account powers both features.

Public surface:
    search_contests(mode, custom_queries, min_engagement, limit, recency_hours)
        -> list[ContestResult]
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from loguru import logger

from app.scraper import api as twscrape_api

# ---------------------------------------------------------------------------
# Query banks
# ---------------------------------------------------------------------------
# Each query is a Twitter advanced-search string. They use min_faves to skip
# noise; the per-query limit is small so we can run several in parallel.
QUERIES_AI: list[str] = [
    "AI contest prize min_faves:25 -filter:replies lang:en",
    "AI hackathon prize min_faves:25 -filter:replies lang:en",
    "AI agent hackathon prize min_faves:15 -filter:replies lang:en",
    "AI video contest min_faves:20 -filter:replies lang:en",
    "AI art contest prize min_faves:20 -filter:replies lang:en",
    "AI meme contest min_faves:15 -filter:replies lang:en",
    "LLM hackathon prize min_faves:15 -filter:replies lang:en",
    "generative AI contest min_faves:15 -filter:replies lang:en",
]

QUERIES_GENERAL: list[str] = [
    "video contest prize min_faves:25 -filter:replies lang:en",
    "meme contest prize min_faves:25 -filter:replies lang:en",
    "giveaway RT to win min_faves:50 -filter:replies lang:en",
    "art contest prize min_faves:25 -filter:replies lang:en",
    "challenge prize pool min_faves:25 -filter:replies lang:en",
    '"reply to win" min_faves:30 -filter:replies lang:en',
    '"comment to win" min_faves:30 -filter:replies lang:en',
]

# Keyword sets for quick rule-based classification (also used to validate that
# a hit really does look like a contest — Twitter search is fuzzy).
KW_CONTEST = (
    "contest", "competition", "challenge", "giveaway", "hackathon",
    "tournament", "bounty", "win ", " win!", " winner",
    "rt to win", "retweet to win", "reply to win", "comment to win",
)
KW_PRIZE = (
    "prize", "prize pool", "reward", "bounty", "$", "usd", "usdc",
    "eth", "sol", " btc", "winner gets", "up for grabs",
)
KW_AI = (
    "ai ", "a.i.", "llm", "gpt", "openai", "anthropic", "claude",
    "machine learning", "ml ", "generative", "diffusion", "stable diffusion",
    "midjourney", "ai agent", "agentic", "neural", "transformer",
)
KW_VIDEO = (
    "video", "reel", "short", "tiktok", "youtube", "shorts",
    "vlog", "film", "animation", "cgi",
)
KW_MEME = ("meme", "memes", "meme-off", "shitpost")
KW_ART  = ("art ", " art", "illustrat", "painting", "drawing", "sketch")

# Loose deadline detection. We don't try to parse to a real datetime here — the
# raw phrase is enough for the UI; extraction is best-effort.
DEADLINE_PATTERNS = [
    re.compile(r"\bdeadline[s]?[:\s]+([^.!\n]{3,80})", re.I),
    re.compile(r"\bend[s]?\s+(on\s+|at\s+)?([^.!\n]{3,80})", re.I),
    re.compile(r"\bclos(?:es|ing)\s+(on\s+|at\s+)?([^.!\n]{3,80})", re.I),
    re.compile(r"\b(\d{1,2}\s+(?:hour|hr|day|week)s?\s+left\b[^.!\n]{0,40})", re.I),
    re.compile(r"\buntil\s+([^.!\n]{3,80})", re.I),
]

ContestType = Literal["ai", "video", "meme", "art", "general"]
SortKey = Literal["engagement", "newest", "deadline"]
Mode = Literal["ai", "all", "custom"]


# ---------------------------------------------------------------------------
# Engagement weights
# ---------------------------------------------------------------------------
# Weighted to reward genuine engagement (replies & retweets cost more effort
# than passive likes; views are abundant so weighted very low).
W_LIKES    = 1.0
W_RETWEETS = 2.5
W_REPLIES  = 3.0
W_QUOTES   = 2.0
W_VIEWS    = 0.01


def _engagement_score(likes: int, rts: int, replies: int, quotes: int, views: int) -> float:
    return (
        likes * W_LIKES
        + rts * W_RETWEETS
        + replies * W_REPLIES
        + quotes * W_QUOTES
        + views * W_VIEWS
    )


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------
@dataclass
class ContestResult:
    id: str               # tweet id as string (precise; bigints break in JS)
    url: str
    author: str           # "@handle"
    author_name: str
    content: str
    created_at: str       # ISO 8601
    age_hours: float
    likes: int
    retweets: int
    replies: int
    quotes: int
    views: int
    engagement_score: float
    contest_type: ContestType
    looks_like_contest: bool   # passed our keyword check
    deadline_hint: Optional[str]
    matched_query: str         # which search bucket surfaced this

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def _has_any(text_lower: str, kws) -> bool:
    return any(k in text_lower for k in kws)


def _classify(text: str) -> tuple[ContestType, bool]:
    t = text.lower()
    looks_like = _has_any(t, KW_CONTEST) or (
        # "win $X" / "prize pool $X" pattern even without explicit "contest" word
        _has_any(t, KW_PRIZE) and ("win " in t or "winners" in t)
    )

    # Type priority: AI > video > meme > art > general (most specific first).
    if _has_any(t, KW_AI):
        return ("ai", looks_like)
    if _has_any(t, KW_VIDEO):
        return ("video", looks_like)
    if _has_any(t, KW_MEME):
        return ("meme", looks_like)
    if _has_any(t, KW_ART):
        return ("art", looks_like)
    return ("general", looks_like)


def _extract_deadline(text: str) -> Optional[str]:
    for pat in DEADLINE_PATTERNS:
        m = pat.search(text)
        if m:
            # Last group has the actual phrase (some patterns use group 2).
            phrase = m.group(m.lastindex or 1).strip(" .,;:")
            # Trim to something display-friendly.
            return phrase[:80]
    return None


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
async def _run_query(
    query: str,
    per_query_limit: int,
    recency_hours: int,
) -> list[tuple[str, dict]]:
    """Run one search; return [(query, raw_tweet_dict), ...]."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=recency_hours)
    out: list[tuple[str, dict]] = []
    try:
        async for t in twscrape_api.search(query, limit=per_query_limit):
            try:
                created = getattr(t, "date", None)
                if created and isinstance(created, datetime):
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    if created < cutoff:
                        continue
                out.append((query, _tweet_to_raw(t)))
            except Exception as inner:  # noqa: BLE001
                logger.debug("Skip tweet during search '{}': {}", query, inner)
    except Exception as e:  # noqa: BLE001
        logger.warning("Contest search query failed q='{}': {}", query, e)
    return out


def _tweet_to_raw(t) -> dict:
    """Coerce a twscrape Tweet into the fields we need (defensive: any of these
    can be missing/zero on age-gated or restricted posts)."""
    user = getattr(t, "user", None)
    handle = f"@{user.username}" if user and getattr(user, "username", None) else "@unknown"
    name = getattr(user, "displayname", None) or handle
    created = getattr(t, "date", None)
    iso = created.isoformat() if isinstance(created, datetime) else ""
    return {
        "id": str(getattr(t, "id", "")),
        "url": getattr(t, "url", "") or f"https://x.com/i/web/status/{getattr(t, 'id', '')}",
        "author": handle,
        "author_name": name,
        "content": getattr(t, "rawContent", "") or "",
        "created_at": iso,
        "likes":    int(getattr(t, "likeCount", 0) or 0),
        "retweets": int(getattr(t, "retweetCount", 0) or 0),
        "replies":  int(getattr(t, "replyCount", 0) or 0),
        "quotes":   int(getattr(t, "quoteCount", 0) or 0),
        "views":    int(getattr(t, "viewCount", 0) or 0),
    }


async def search_contests(
    mode: Mode = "ai",
    custom_queries: Optional[list[str]] = None,
    min_engagement: float = 0.0,
    limit: int = 30,
    recency_hours: int = 72,
    require_contest_keywords: bool = True,
) -> list[ContestResult]:
    """
    Run multiple search queries in parallel, dedupe, classify, score, sort.

    Args:
        mode: "ai" → only AI queries, "all" → AI + general, "custom" → use
              `custom_queries`.
        custom_queries: extra search strings (used when mode="custom"; appended
                         in other modes).
        min_engagement: drop results whose weighted score is below this.
        limit: max number of results to return after ranking.
        recency_hours: skip tweets older than this many hours.
        require_contest_keywords: if True, drop results that don't visibly look
                                   like contests (helps suppress generic AI
                                   news that gets surfaced by the search).
    """
    if mode == "custom":
        queries = list(custom_queries or [])
    else:
        queries = list(QUERIES_AI)
        if mode == "all":
            queries.extend(QUERIES_GENERAL)
        if custom_queries:
            queries.extend(custom_queries)

    if not queries:
        return []

    # Per-query limit: aim to gather ~3-4× the desired result count across all
    # queries combined, leaving room for dedup + filter shrinkage.
    per_query = max(8, min(30, (limit * 4) // max(1, len(queries))))

    started = time.monotonic()
    logger.info(
        "Contest search starting: mode={} queries={} per_query={} recency={}h",
        mode, len(queries), per_query, recency_hours,
    )

    # Cap concurrency so we don't burn through the X account's per-15-min budget.
    sem = asyncio.Semaphore(3)

    async def _gated(q: str):
        async with sem:
            return await _run_query(q, per_query, recency_hours)

    batches = await asyncio.gather(*(_gated(q) for q in queries), return_exceptions=False)

    seen: dict[str, ContestResult] = {}
    for pairs in batches:
        for q, raw in pairs:
            tid = raw.get("id")
            if not tid:
                continue
            if tid in seen:
                continue

            text = raw.get("content") or ""
            ctype, looks_like = _classify(text)
            if require_contest_keywords and not looks_like:
                continue

            now_utc = datetime.now(timezone.utc)
            try:
                ca = datetime.fromisoformat(raw["created_at"])
                if ca.tzinfo is None:
                    ca = ca.replace(tzinfo=timezone.utc)
                age_h = max(0.0, (now_utc - ca).total_seconds() / 3600.0)
            except Exception:  # noqa: BLE001
                age_h = 0.0

            score = _engagement_score(
                raw["likes"], raw["retweets"], raw["replies"], raw["quotes"], raw["views"]
            )
            if score < min_engagement:
                continue

            seen[tid] = ContestResult(
                id=tid,
                url=raw["url"],
                author=raw["author"],
                author_name=raw["author_name"],
                content=text,
                created_at=raw["created_at"],
                age_hours=round(age_h, 2),
                likes=raw["likes"],
                retweets=raw["retweets"],
                replies=raw["replies"],
                quotes=raw["quotes"],
                views=raw["views"],
                engagement_score=round(score, 2),
                contest_type=ctype,
                looks_like_contest=looks_like,
                deadline_hint=_extract_deadline(text),
                matched_query=q,
            )

    ranked = sorted(seen.values(), key=lambda r: r.engagement_score, reverse=True)
    elapsed = time.monotonic() - started
    logger.info(
        "Contest search done: candidates={} kept={} elapsed={:.1f}s",
        sum(len(b) for b in batches), len(ranked), elapsed,
    )
    return ranked[:limit]
