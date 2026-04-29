"""
twscrape-based X (Twitter) scraper.

A single shared `twscrape.API` instance is created on import and
initialized once at FastAPI startup via `init_scraper()`.
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from typing import Optional

import httpx
from fake_useragent import UserAgent
from loguru import logger
from twscrape import API, Tweet
from twscrape import xclid as _twscrape_xclid


def _patch_xclid_client_with_cookies() -> None:
    """
    Monkey-patch twscrape.xclid._make_client to send our X session cookies.

    Cookies are needed because twscrape anonymously fetches https://x.com/tesla
    to compute the x-client-transaction-id header. From cloud-server IPs
    (Fly.io, AWS, GCP, etc.), Cloudflare blocks anonymous requests to x.com
    and returns a challenge page, which then fails parsing with
    IndexError("list index out of range") and locks the account for 15 min.

    Sending an authenticated session cookie bypasses the challenge.
    """
    # Take only the first whitespace-delimited token. Pasted secrets sometimes
    # carry trailing whitespace + concatenated values from imprecise selection;
    # X rejects those with HTTP 353 (csrf mismatch).
    auth_token = (os.getenv("X_AUTH_TOKEN", "").split() or [""])[0]
    ct0 = (os.getenv("X_CT0", "").split() or [""])[0]
    if not auth_token:
        return

    cookies: dict[str, str] = {"auth_token": auth_token}
    if ct0:
        cookies["ct0"] = ct0

    def _make_client_with_cookies() -> httpx.AsyncClient:
        headers = {"user-agent": UserAgent().chrome}
        return httpx.AsyncClient(
            headers=headers,
            cookies=cookies,
            follow_redirects=True,
        )

    _twscrape_xclid._make_client = _make_client_with_cookies  # type: ignore[attr-defined]
    logger.info("Patched twscrape.xclid._make_client to send X session cookies.")


def _patch_xclid_get_scripts_list() -> None:
    """
    Replace twscrape.xclid.get_scripts_list with a parser that understands
    X.com's current webpack chunk format.

    Old format (what upstream twscrape parses):
        e=>e+"."+{chunk_id: "hash", ...}[e]+"a.js"
    Current format (since late 2025):
        ({chunk_id: "module_name", ...}[e] || e) + "." + ({chunk_id: "hash", ...})[e] + "a.js"

    Without this patch, upstream's `text.split('e=>e+"."+')[1]` raises
    IndexError and twscrape locks the account for 15 minutes (issue #287).
    """
    import re as _re

    def _walk_back_paren(s: str, start: int) -> int:
        depth = 1
        k = start - 1
        while k >= 0 and depth > 0:
            c = s[k]
            if c == ")":
                depth += 1
            elif c == "(":
                depth -= 1
            k -= 1
        return k + 1

    def _walk_back_brace(s: str, start: int) -> int:
        depth = 1
        k = start - 1
        while k >= 0 and depth > 0:
            c = s[k]
            if c == "}":
                depth += 1
            elif c == "{":
                depth -= 1
            k -= 1
        return k + 1

    def _parse_kvs(inner: str) -> dict[int, str]:
        return {int(m.group(1)): m.group(2) for m in _re.finditer(r'(\d+):"([^"]+)"', inner)}

    def patched_get_scripts_list(text: str):
        i = text.find('[e]+"a.js"')
        if i == -1 or text[i - 1] != ")":
            return  # nothing to yield; parse_anim_idx will raise its own error

        hash_close = i - 1
        hash_open = _walk_back_paren(text, hash_close)
        hash_inner = text[hash_open + 1: hash_close]
        hashes = _parse_kvs(hash_inner)

        n = text.rfind('})[e]||e)', 0, hash_open)
        if n == -1:
            return
        name_open = _walk_back_brace(text, n)
        name_inner = text[name_open + 1: n]
        names = _parse_kvs(name_inner)

        for chunk_id, hash_val in hashes.items():
            name = names.get(chunk_id, str(chunk_id))
            yield _twscrape_xclid.script_url(name, f"{hash_val}a")

    _twscrape_xclid.get_scripts_list = patched_get_scripts_list  # type: ignore[attr-defined]
    logger.info("Patched twscrape.xclid.get_scripts_list for current X.com chunk format.")


_patch_xclid_client_with_cookies()
_patch_xclid_get_scripts_list()


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------
class ScraperError(Exception):
    """Base class for scraper-related errors."""


class InvalidTweetURL(ScraperError):
    """Raised when the URL/ID cannot be parsed into a tweet ID."""


class TweetNotFound(ScraperError):
    """Raised when twscrape returns no tweet (deleted, private, suspended)."""


class NoActiveAccounts(ScraperError):
    """Raised when twscrape has no logged-in accounts available."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class TweetData:
    id: int
    url: str
    author: str            # "@handle"
    author_name: str       # display name
    text: str
    created_at: str        # ISO 8601
    thread: list[str] = field(default_factory=list)  # parent-chain (oldest first)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------
_TWEET_URL_RE = re.compile(
    r"""(?ix)
    ^
    (?:https?://)?
    (?:www\.|mobile\.)?
    (?:x\.com|twitter\.com)
    /[^/]+/status(?:es)?/(\d+)
    (?:[/?#].*)?
    $
    """
)
_RAW_ID_RE = re.compile(r"^\d{5,25}$")


def extract_tweet_id(url_or_id: str) -> int:
    """Extract a numeric tweet ID from a URL or raw ID string."""
    if not url_or_id or not isinstance(url_or_id, str):
        raise InvalidTweetURL("Empty or non-string input.")

    s = url_or_id.strip()

    if _RAW_ID_RE.match(s):
        return int(s)

    m = _TWEET_URL_RE.match(s)
    if not m:
        raise InvalidTweetURL(
            "Could not parse a tweet ID. Expected a URL like "
            "https://x.com/<user>/status/<id>"
        )
    return int(m.group(1))


# ---------------------------------------------------------------------------
# Scraper singleton
# ---------------------------------------------------------------------------
_DB_PATH = os.getenv("TWSCRAPE_DB_PATH", "accounts/accounts.db")
os.makedirs(os.path.dirname(_DB_PATH) or ".", exist_ok=True)

api: API = API(_DB_PATH)


async def _maybe_add_account_from_env() -> None:
    """
    Add an X account to the twscrape pool from environment variables.

    Two ways to provide credentials:
    1. Cookies (preferred for cloud deploys — bypasses Cloudflare login wall):
         X_TWSCRAPE_USERNAME, X_AUTH_TOKEN, X_CT0
       (X_TWSCRAPE_PASSWORD/EMAIL/EMAIL_PASSWORD are optional fallback for
       password-based re-login if cookies expire.)

    2. Username/password:
         X_TWSCRAPE_USERNAME, X_TWSCRAPE_PASSWORD,
         X_TWSCRAPE_EMAIL, X_TWSCRAPE_EMAIL_PASSWORD
       (only works from non-blocked IPs — Cloudflare blocks most clouds.)

    If both `X_AUTH_TOKEN` and `X_CT0` are present, the account is added with
    cookies and immediately marked active — no login attempt is made.

    If the account already exists in the pool with no cookies but env now
    provides cookies, the existing account is deleted and re-added so the
    cookies take effect.
    """
    username = os.getenv("X_TWSCRAPE_USERNAME", "").strip()
    if not username:
        return

    password = os.getenv("X_TWSCRAPE_PASSWORD", "").strip() or "x"
    email = os.getenv("X_TWSCRAPE_EMAIL", "").strip() or f"{username}@example.com"
    email_password = os.getenv("X_TWSCRAPE_EMAIL_PASSWORD", "").strip() or "x"

    auth_token = (os.getenv("X_AUTH_TOKEN", "").split() or [""])[0]
    ct0 = (os.getenv("X_CT0", "").split() or [""])[0]
    cookies_str: str | None = None
    if auth_token and ct0:
        cookies_str = f"auth_token={auth_token}; ct0={ct0}"
    elif auth_token:
        # auth_token alone won't auto-activate, but we still try.
        cookies_str = f"auth_token={auth_token}"
        logger.warning(
            "Only X_AUTH_TOKEN provided (no X_CT0). Account will be added but "
            "may not be marked active until ct0 is also supplied."
        )

    accounts = await api.pool.accounts_info()
    existing = next(
        (a for a in accounts if a.get("username", "").lower() == username.lower()),
        None,
    )

    if existing:
        # If we now have cookies but the existing account has no cookies, replace it.
        if cookies_str and not existing.get("active"):
            logger.info("Replacing inactive @{} with cookie-based account.", username)
            try:
                await api.pool.delete_accounts(username)
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed to delete @{}: {}", username, e)
        else:
            logger.info(
                "Account @{} already in pool (active={}) — skipping add.",
                username,
                existing.get("active"),
            )
            return

    try:
        await api.pool.add_account(
            username,
            password,
            email,
            email_password,
            cookies=cookies_str,
        )
        logger.info(
            "Added @{} to twscrape pool from env vars (cookies={}).",
            username,
            "yes" if cookies_str else "no",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to add @{} from env: {}", username, e)


async def init_scraper() -> None:
    """
    Called from FastAPI's startup event.

    - Auto-adds an account from env vars if provided.
    - Logs in any accounts that have not been logged in yet.
    - Verifies at least one active account exists.
    """
    logger.info("Initializing twscrape (db={})...", _DB_PATH)

    await _maybe_add_account_from_env()

    # Clear any stale per-queue locks from previous runs so a single transient
    # twscrape error doesn't leave the pool unusable for 15 minutes.
    try:
        await api.pool.reset_locks()
    except Exception as e:  # noqa: BLE001
        logger.warning("twscrape reset_locks() raised: {}", e)

    try:
        await api.pool.login_all()
    except Exception as e:  # noqa: BLE001
        logger.warning("twscrape login_all() raised: {}", e)

    accounts = await api.pool.accounts_info()
    active = [a for a in accounts if a.get("active")]
    logger.info(
        "twscrape ready — {} total account(s), {} active.",
        len(accounts),
        len(active),
    )

    if not active:
        logger.warning(
            "No active twscrape accounts! Either set "
            "X_TWSCRAPE_USERNAME / PASSWORD / EMAIL / EMAIL_PASSWORD env vars, "
            "or run:\n"
            "  twscrape add_accounts accounts.txt username:password:email:email_password\n"
            "  twscrape login_accounts"
        )


def _format_handle(t: Tweet) -> tuple[str, str]:
    user = getattr(t, "user", None)
    handle = f"@{user.username}" if user and getattr(user, "username", None) else "@unknown"
    name = getattr(user, "displayname", None) or handle
    return handle, name


async def _fetch_thread(tweet: Tweet, max_depth: int = 5) -> list[str]:
    """Walk up the reply chain; return parent texts oldest first."""
    chain: list[str] = []
    parent_id: Optional[int] = getattr(tweet, "inReplyToTweetId", None)
    depth = 0

    while parent_id and depth < max_depth:
        try:
            parent = await api.tweet_details(parent_id)
        except Exception as e:  # noqa: BLE001
            logger.debug("Failed to fetch parent {}: {}", parent_id, e)
            break

        if not parent:
            break

        chain.append(parent.rawContent or "")
        parent_id = getattr(parent, "inReplyToTweetId", None)
        depth += 1

    chain.reverse()
    return chain


async def fetch_tweet_text(url_or_id: str, include_thread: bool = True) -> TweetData:
    """
    Fetch a single tweet (and its parent thread) by URL or ID.

    Raises:
        InvalidTweetURL  — bad URL/ID.
        TweetNotFound    — twscrape returned nothing.
        NoActiveAccounts — pool has zero active accounts.
        ScraperError     — any other scraper failure.
    """
    tweet_id = extract_tweet_id(url_or_id)
    logger.info("Fetching tweet id={}", tweet_id)

    accounts = await api.pool.accounts_info()
    if not any(a.get("active") for a in accounts):
        raise NoActiveAccounts(
            "twscrape has no active accounts. Add and log in at least one account."
        )

    try:
        tweet: Optional[Tweet] = await api.tweet_details(tweet_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("twscrape error for id={}", tweet_id)
        raise ScraperError(f"Scraper failure: {e}") from e

    if not tweet:
        raise TweetNotFound(
            f"Tweet {tweet_id} could not be fetched. "
            "It may be deleted, private, or rate-limited."
        )

    handle, name = _format_handle(tweet)
    thread: list[str] = []
    if include_thread and getattr(tweet, "inReplyToTweetId", None):
        thread = await _fetch_thread(tweet)

    return TweetData(
        id=tweet.id,
        url=getattr(tweet, "url", f"https://x.com/i/web/status/{tweet.id}"),
        author=handle,
        author_name=name,
        text=tweet.rawContent or "",
        created_at=tweet.date.isoformat() if getattr(tweet, "date", None) else "",
        thread=thread,
    )
