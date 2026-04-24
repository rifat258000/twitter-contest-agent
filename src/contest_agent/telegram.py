"""Minimal Telegram Bot API client for sendMessage."""

from __future__ import annotations

import logging

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import Settings
from .models import RankedContest

log = logging.getLogger(__name__)


def format_message(r: RankedContest) -> str:
    t = r.tweet
    e = r.extracted

    if e.contest_type == "meme_contest":
        emoji = "🎨"
        label = "Meme contest"
    elif e.contest_type == "ai_event":
        emoji = "🤖"
        label = "AI event"
    else:
        emoji = "📣"
        label = "Contest"

    lines: list[str] = []
    lines.append(f"{emoji} <b>{_esc(label)}</b> — score {r.engagement_score:.0f}")
    if e.summary:
        lines.append(_esc(e.summary))
    meta: list[str] = []
    if e.prize_pool:
        meta.append(f"Prize: <b>{_esc(e.prize_pool)}</b>")
    if e.deadline:
        meta.append(f"Deadline: {_esc(e.deadline)}")
    if e.tags:
        meta.append("Tags: " + ", ".join(_esc(tag) for tag in e.tags[:6]))
    if meta:
        lines.append(" · ".join(meta))
    lines.append(
        f"❤️ {t.likes}  🔁 {t.retweets}  💬 {t.replies}  📝 {t.quotes}"
    )
    lines.append(f"by @{_esc(t.author)} — {t.url}")
    return "\n".join(lines)


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


class TelegramPoster:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(self.settings.telegram_bot_token and self.settings.telegram_chat_id)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def send(self, text: str) -> None:
        if not self.configured:
            log.warning("Telegram not configured; would have posted:\n%s", text)
            return
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(
                url,
                json={
                    "chat_id": self.settings.telegram_chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": False,
                },
            )
            if r.status_code >= 400:
                log.error("Telegram error %s: %s", r.status_code, r.text)
                r.raise_for_status()
