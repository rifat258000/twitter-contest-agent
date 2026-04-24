"""Twitter scraping.

Backends, tried in order:
1. RapidAPI (twitter-api45 by alexanderxbx) if RAPIDAPI_KEY is set.
   Fastest and most reliable free/cheap option.
2. twscrape, if accounts are registered. Requires real X accounts.
   Frequently blocked by Cloudflare on datacenter IPs.
3. Nitter HTML. Community-run instances; often rate-limited or offline.
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


class RapidApiBackend:
    """Fetches tweets via the twitter-api45 RapidAPI endpoint.

    Requires `RAPIDAPI_KEY` in the environment. Cheapest/most reliable of the
    three backends for this project. Uses the `/search.php` endpoint.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def available(self) -> bool:
        return bool(self.settings.rapidapi_key)

    async def search(self, query: str, limit: int) -> AsyncIterator[Tweet]:
        if not self.available():
            return
        base = f"https://{self.settings.rapidapi_host}/search.php"
        headers = {
            "x-rapidapi-key": self.settings.rapidapi_key,
            "x-rapidapi-host": self.settings.rapidapi_host,
            "accept": "application/json",
        }
        cursor: str | None = None
        collected = 0
        async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
            while collected < limit:
                params = {"query": query, "search_type": "Top"}
                if cursor:
                    params["cursor"] = cursor
                try:
                    r = await client.get(base, params=params)
                except Exception as e:  # pragma: no cover - network
                    log.warning("rapidapi search %r failed: %s", query, e)
                    return
                if r.status_code != 200:
                    log.warning(
                        "rapidapi %s returned %s: %s",
                        query,
                        r.status_code,
                        r.text[:200],
                    )
                    return
                try:
                    data = r.json()
                except ValueError:
                    log.warning("rapidapi returned non-JSON for %r", query)
                    return
                timeline = data.get("timeline") or []
                if not timeline:
                    return
                for raw in timeline:
                    tweet = _rapidapi_to_tweet(raw)
                    if tweet is None:
                        continue
                    yield tweet
                    collected += 1
                    if collected >= limit:
                        return
                cursor = data.get("next_cursor")
                if not cursor:
                    return


_API45_DATE_FORMATS = (
    "%a %b %d %H:%M:%S %z %Y",  # "Wed Apr 23 12:00:00 +0000 2026"
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%SZ",
)


def _rapidapi_to_tweet(raw: dict) -> Tweet | None:
    tid = raw.get("tweet_id") or raw.get("id_str") or raw.get("id")
    if not tid:
        return None
    # twitter-api45 uses top-level `screen_name` + `user_info`.
    # Older/alternative payloads nest under `author`.
    user_info = raw.get("user_info") or raw.get("author") or {}
    author = raw.get("screen_name") or user_info.get("screen_name") or ""
    display = user_info.get("name") or author
    text = raw.get("text") or raw.get("full_text") or ""
    created_raw = raw.get("created_at") or ""
    created = datetime.now(tz=timezone.utc)
    for fmt in _API45_DATE_FORMATS:
        try:
            created = datetime.strptime(created_raw, fmt)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            break
        except (ValueError, TypeError):
            continue

    def _int(key: str) -> int:
        v = raw.get(key)
        if v is None:
            return 0
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    views = _int("views")
    return Tweet(
        id=str(tid),
        url=f"https://twitter.com/{author}/status/{tid}" if author else f"https://twitter.com/i/status/{tid}",
        author=author,
        author_display=display,
        content=text,
        created_at=created,
        likes=_int("favorites"),
        retweets=_int("retweets"),
        replies=_int("replies"),
        quotes=_int("quotes"),
        views=views,
        source="rapidapi:twitter-api45",
    )


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
    """Unified scraper that tries RapidAPI → twscrape → Nitter, in order."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.rapidapi = RapidApiBackend(settings)
        self.tw = TwscrapeBackend(settings)
        self.nitter = NitterBackend(settings)
        self._tw_ready: bool | None = None

    async def search(self, query: str, limit: int | None = None) -> list[Tweet]:
        limit = limit or self.settings.max_tweets_per_query

        tweets: list[Tweet] = []
        if self.rapidapi.available():
            async for t in self.rapidapi.search(query, limit):
                tweets.append(t)
            if tweets:
                return tweets

        if self._tw_ready is None:
            self._tw_ready = await self.tw.available()
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
