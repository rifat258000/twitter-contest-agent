"""Orchestrates scrape → extract → rank → post cycle."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .config import Settings, get_settings
from .dedup import DedupStore
from .extractor import Extractor
from .models import RankedContest
from .queries import DEFAULT_QUERIES
from .ranking import rank
from .scraper import Scraper
from .telegram import TelegramPoster, format_message

log = logging.getLogger(__name__)


@dataclass
class RunStats:
    scraped: int = 0
    extracted_contests: int = 0
    posted: int = 0
    skipped_dup: int = 0
    skipped_low_score: int = 0


class Agent:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.scraper = Scraper(self.settings)
        self.extractor = Extractor(self.settings)
        self.dedup = DedupStore(self.settings.dedup_db_path)
        self.telegram = TelegramPoster(self.settings)

    async def run_once(self, queries: list[str] | None = None, dry_run: bool = False) -> RunStats:
        qs = queries or DEFAULT_QUERIES
        stats = RunStats()

        log.info("Searching %d queries...", len(qs))
        tweets = await self.scraper.search_many(qs)
        stats.scraped = len(tweets)
        log.info("Scraped %d unique tweets", len(tweets))
        if not tweets:
            return stats

        extracted = []
        for t in tweets:
            e = await self.extractor.extract(t)
            if e.is_contest_or_event:
                extracted.append((t, e))
        stats.extracted_contests = len(extracted)
        log.info("%d tweets classified as contests/events", len(extracted))

        ranked: list[RankedContest] = rank(extracted, self.settings)

        to_post: list[RankedContest] = []
        for r in ranked:
            if self.dedup.has_posted(r.tweet.id):
                stats.skipped_dup += 1
                continue
            if r.engagement_score < self.settings.min_engagement_score:
                stats.skipped_low_score += 1
                continue
            to_post.append(r)
            if len(to_post) >= self.settings.top_n_per_run:
                break

        log.info("Posting %d contests to Telegram", len(to_post))
        for r in to_post:
            msg = format_message(r)
            if dry_run:
                log.info("[DRY RUN]\n%s\n", msg)
            else:
                try:
                    await self.telegram.send(msg)
                except Exception as e:
                    log.error("telegram send failed for %s: %s", r.tweet.id, e)
                    continue
            self.dedup.mark_posted(r.tweet.id, r.engagement_score)
            stats.posted += 1
            await asyncio.sleep(1.0)

        return stats
