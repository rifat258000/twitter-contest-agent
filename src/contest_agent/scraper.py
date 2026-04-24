"""Twitter scraping with twscrape primary and Nitter HTML fallback.

Both backends are fragile. twscrape requires real X accounts; Nitter depends
on community-run instances that are often rate-limited or offline. The agent
tries twscrape first, then falls back to Nitter.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

from .config import Settings
from .models import Tweet

log = logging.getLogger(__name__)


class TwscrapeBackend:
    """Primary backend using twscrape. Requires accounts to be registered.

    If no accounts are registered or the import fails, `available()` returns
    False and the caller should fall back to Nitter.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._api = None

    async def available(self) -> bool:
        try:
            from twscrape import API  # type: ignore
        except ImportError:
            log.warning("twscrape not installed")
            return False

        try:
            self._api = API(self.settings.twscrape_db_path)
            pool_accs = await self._api.pool.accounts_info()
            active = [a for a in pool_accs if a.get("active")]
            if not active:
                log.info("twscrape has no active accounts; will fall back to Nitter")
                return False
            return True
        except Exception as e:
            log.warning("twscrape unavailable: %s", e)
            return False

    async def search(self, query: str, limit: int) -> AsyncIterator[Tweet]:
        if self._api is None:
            return
        try:
            async for t in self._api.search(query, limit=limit):
                yield _twscrape_to_tweet(t)
        except Exception as e:  # pragma: no cover - network
            log.warning("twscrape search failed for %r: %s", query, e)
            return


def _twscrape_to_tweet(t) -> Tweet:  # type: ignore[no-untyped-def]
    return Tweet(
        id=str(t.id),
        url=t.url,
        author=t.user.username if t.user else "",
        author_display=(t.user.displayname if t.user else "") or "",
        content=t.rawContent or "",
        created_at=t.date,
        likes=t.likeCount or 0,
        retweets=t.retweetCount or 0,
        replies=t.replyCount or 0,
        quotes=t.quoteCount or 0,
        views=t.viewCount or 0,
        source="twscrape",
    )


class NitterBackend:
    """Fallback backend that scrapes Nitter HTML pages.

    Engagement metrics from Nitter are less reliable (replies/retweets/likes
    are shown but views are often missing). Still useful as a fallback.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def search(self, query: str, limit: int) -> AsyncIterator[Tweet]:
        encoded = quote_plus(query)
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=20.0,
            headers={"User-Agent": "Mozilla/5.0 (contest-agent)"},
        ) as client:
            for instance in self.settings.nitter_instance_list:
                url = f"{instance.rstrip('/')}/search?f=tweets&q={encoded}"
                try:
                    resp = await client.get(url)
                except Exception as e:
                    log.info("Nitter %s failed: %s", instance, e)
                    continue
                if resp.status_code != 200:
                    log.info("Nitter %s returned %s", instance, resp.status_code)
                    continue
                html = resp.text
                soup = BeautifulSoup(html, "lxml")
                items = soup.select(".timeline-item")
                count = 0
                for item in items:
                    tweet = _parse_nitter_item(item, instance)
                    if tweet is None:
                        continue
                    yield tweet
                    count += 1
                    if count >= limit:
                        break
                if count > 0:
                    # Got results from this instance, stop trying others.
                    return
                log.info("Nitter %s returned 0 parseable items", instance)


def _parse_nitter_item(item, instance: str) -> Tweet | None:  # type: ignore[no-untyped-def]
    link = item.select_one("a.tweet-link")
    if link is None:
        return None
    href = link.get("href", "")
    # href like /user/status/12345#m
    m = re.match(r"^/(?P<user>[^/]+)/status/(?P<id>\d+)", href)
    if not m:
        return None
    tid = m.group("id")
    user = m.group("user")
    content_el = item.select_one(".tweet-content")
    content = content_el.get_text(" ", strip=True) if content_el else ""
    date_el = item.select_one(".tweet-date a")
    created = datetime.now(tz=timezone.utc)
    if date_el is not None:
        raw_ts = date_el.get("title", "") or ""
        # Nitter titles: "Apr 23, 2026 · 7:12 PM UTC"
        for fmt in ("%b %d, %Y · %I:%M %p %Z", "%b %d, %Y · %I:%M %p UTC"):
            try:
                created = datetime.strptime(raw_ts.replace("UTC", "UTC").strip(), fmt)
                created = created.replace(tzinfo=timezone.utc)
                break
            except ValueError:
                pass

    # Nitter renders reply / retweet / like counts in that order inside
    # .tweet-stats. Grab the first integer found in each stat element.
    stats = item.select(".tweet-stats .tweet-stat, .tweet-stat")
    nums: list[int] = []
    for s in stats:
        txt = s.get_text(" ", strip=True).replace(",", "")
        digits = re.findall(r"\d+", txt)
        if digits:
            nums.append(int(digits[0]))

    replies = nums[0] if len(nums) >= 1 else 0
    retweets = nums[1] if len(nums) >= 2 else 0
    likes = nums[2] if len(nums) >= 3 else 0

    public_url = f"https://twitter.com/{user}/status/{tid}"
    return Tweet(
        id=tid,
        url=public_url,
        author=user,
        author_display=user,
        content=content,
        created_at=created,
        likes=likes,
        retweets=retweets,
        replies=replies,
        quotes=0,
        views=0,
        source=f"nitter:{instance}",
    )


class Scraper:
    """Unified scraper that tries twscrape then Nitter."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.tw = TwscrapeBackend(settings)
        self.nitter = NitterBackend(settings)
        self._tw_ready: bool | None = None

    async def search(self, query: str, limit: int | None = None) -> list[Tweet]:
        limit = limit or self.settings.max_tweets_per_query
        if self._tw_ready is None:
            self._tw_ready = await self.tw.available()

        tweets: list[Tweet] = []
        if self._tw_ready:
            async for t in self.tw.search(query, limit):
                tweets.append(t)
            if tweets:
                return tweets
        async for t in self.nitter.search(query, limit):
            tweets.append(t)
        return tweets

    async def search_many(self, queries: list[str]) -> list[Tweet]:
        all_tweets: dict[str, Tweet] = {}
        for q in queries:
            try:
                results = await self.search(q)
            except Exception as e:  # pragma: no cover - network
                log.warning("search failed for %r: %s", q, e)
                continue
            for t in results:
                all_tweets[t.id] = t
            await asyncio.sleep(1.5)  # polite rate-limit between queries
        return list(all_tweets.values())
