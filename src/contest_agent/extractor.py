"""Extract structured contest/event info from tweet text.

Primary path uses a local Ollama model; if Ollama is unreachable or returns
unparseable output, falls back to a regex/keyword-based classifier.
"""

from __future__ import annotations

import json
import logging
import re

import httpx

from .config import Settings
from .models import ContestType, ExtractedContest, Tweet
from .queries import AI_EVENT_KEYWORDS, MEME_CONTEST_KEYWORDS, PRIZE_KEYWORDS

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are an information extraction assistant. You are given a tweet.
Decide whether the tweet is announcing a *meme contest* or an *AI event/hackathon*
that participants can enter. Extract structured info.

Return ONLY a single JSON object, no prose, no markdown fences. Schema:
{
  "is_contest_or_event": boolean,
  "contest_type": "meme_contest" | "ai_event" | "other",
  "prize_pool": string or null,        // raw phrase, e.g. "$10,000", "5 ETH", "1000 USDC"
  "prize_pool_usd": number or null,    // best-effort USD estimate; null if unknown
  "deadline": string or null,          // raw phrase or ISO date if clearly stated
  "tags": [string],                    // short lowercase tags, e.g. ["crypto", "solana"]
  "summary": string                    // one-sentence plain-English summary
}
If the tweet is just commentary, news, or unrelated, set is_contest_or_event=false
and contest_type="other"."""


class OllamaExtractor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def available(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{self.settings.ollama_base_url}/api/tags")
                return r.status_code == 200
        except Exception:
            return False

    async def extract(self, tweet: Tweet) -> ExtractedContest | None:
        prompt = (
            f"Tweet by @{tweet.author}:\n\n{tweet.content}\n\n"
            "Return the JSON object now."
        )
        payload = {
            "model": self.settings.ollama_model,
            "system": SYSTEM_PROMPT,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.1},
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.post(
                    f"{self.settings.ollama_base_url}/api/generate", json=payload
                )
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            log.debug("ollama call failed: %s", e)
            return None

        raw = (data.get("response") or "").strip()
        if not raw:
            return None
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            # Sometimes the model wraps output in ```json ... ```. Strip and retry.
            cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
            try:
                obj = json.loads(cleaned)
            except json.JSONDecodeError:
                log.debug("ollama returned non-JSON: %s", raw[:200])
                return None

        try:
            return ExtractedContest(
                is_contest_or_event=bool(obj.get("is_contest_or_event", False)),
                contest_type=_coerce_type(obj.get("contest_type")),
                prize_pool=_str_or_none(obj.get("prize_pool")),
                prize_pool_usd=_float_or_none(obj.get("prize_pool_usd")),
                deadline=_str_or_none(obj.get("deadline")),
                tags=[str(t).lower() for t in obj.get("tags", []) if isinstance(t, (str, int))],
                summary=str(obj.get("summary") or ""),
            )
        except Exception as e:
            log.debug("failed to coerce ollama output: %s", e)
            return None


def _coerce_type(v: object) -> ContestType:
    s = str(v or "").lower().strip()
    if s in ("meme_contest", "meme", "meme-contest"):
        return "meme_contest"
    if s in ("ai_event", "ai-event", "hackathon", "ai_hackathon"):
        return "ai_event"
    return "other"


def _str_or_none(v: object) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _float_or_none(v: object) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


# ---------- regex / keyword fallback ----------

_PRIZE_RE = re.compile(
    r"""
    (?:prize\s*pool|prize|bounty|reward|win)\s*(?:of|:)?\s*
    (?P<amount>
        \$?\s*\d{1,3}(?:[,\.]\d{3})*(?:\.\d+)?\s*(?:k|K|m|M)?\s*
        (?:USD|usd|USDC|usdc|ETH|eth|SOL|sol|BTC|btc|dollars?)?
    )
    """,
    re.VERBOSE,
)

_CURRENCY_SYMBOL_RE = re.compile(
    r"\$\s*\d{1,3}(?:[,\.]\d{3})*(?:\.\d+)?\s*(?:k|K|m|M)?"
)


def _detect_prize(text: str) -> str | None:
    m = _PRIZE_RE.search(text)
    if m:
        return m.group("amount").strip()
    m2 = _CURRENCY_SYMBOL_RE.search(text)
    if m2:
        return m2.group(0).strip()
    return None


def regex_extract(tweet: Tweet) -> ExtractedContest:
    text = tweet.content.lower()
    is_meme = any(kw in text for kw in MEME_CONTEST_KEYWORDS)
    is_ai = any(kw in text for kw in AI_EVENT_KEYWORDS)
    has_prize = any(kw in text for kw in PRIZE_KEYWORDS)

    if is_meme:
        ctype: ContestType = "meme_contest"
    elif is_ai:
        ctype = "ai_event"
    else:
        ctype = "other"

    is_contest = (is_meme or is_ai) and has_prize

    prize = _detect_prize(tweet.content) if has_prize else None

    tags: list[str] = []
    for kw in ("crypto", "nft", "solana", "ethereum", "web3", "airdrop", "llm", "agent"):
        if kw in text:
            tags.append(kw)

    summary = tweet.content[:160].replace("\n", " ")
    return ExtractedContest(
        is_contest_or_event=is_contest,
        contest_type=ctype,
        prize_pool=prize,
        prize_pool_usd=None,
        deadline=None,
        tags=tags,
        summary=summary,
    )


class Extractor:
    """Tries Ollama, falls back to regex."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.ollama = OllamaExtractor(settings)
        self._ollama_ready: bool | None = None

    async def extract(self, tweet: Tweet) -> ExtractedContest:
        if self._ollama_ready is None:
            self._ollama_ready = await self.ollama.available()
            if self._ollama_ready:
                log.info("Ollama reachable at %s", self.settings.ollama_base_url)
            else:
                log.info("Ollama unreachable; using regex extractor")

        if self._ollama_ready:
            result = await self.ollama.extract(tweet)
            if result is not None:
                return result

        return regex_extract(tweet)
