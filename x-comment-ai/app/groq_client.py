"""
Groq API wrapper for generating short, natural X (Twitter) replies.

Tier-1 enhancements:
- Tone selector (witty / supportive / sarcastic / insightful / hype)
- Length selector (short / medium / long)
- Multiple variants in one call (`generate_comments`)
- Slight temperature jitter on regenerate
"""

from __future__ import annotations

import asyncio
import os
from typing import Iterable, Optional

from groq import APIError, AsyncGroq, RateLimitError
from loguru import logger


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
GROQ_MAX_TOKENS = int(os.getenv("GROQ_MAX_TOKENS", "200"))
GROQ_TEMPERATURE = float(os.getenv("GROQ_TEMPERATURE", "0.8"))
DEFAULT_REPLY_LANG = os.getenv("DEFAULT_REPLY_LANG", "auto").lower()


# ---------------------------------------------------------------------------
# Tones & lengths
# ---------------------------------------------------------------------------
TONES: dict[str, str] = {
    "witty":      "Be witty and clever. A subtle joke or a sharp observation.",
    "supportive": "Be warm, supportive, and encouraging. Sound like a friend cheering them on.",
    "sarcastic": "Be playfully sarcastic — light teasing, never mean. Match the post's energy.",
    "insightful": "Be thoughtful and add real insight or a fresh angle on what they said.",
    "hype":       "Be enthusiastic and hyped up. Short, punchy, energetic. Like an excited fan.",
}

LENGTHS: dict[str, tuple[int, str]] = {
    # name -> (max_chars, prompt_directive)
    "short":  (120, "Keep it under 120 characters. One short punchy sentence."),
    "medium": (200, "Aim for 1–2 short sentences, under 200 characters."),
    "long":   (280, "Up to 2 sentences, under 280 characters. Don't pad."),
}


class GroqError(Exception):
    """Base class for Groq-related errors."""


class GroqNotConfigured(GroqError):
    """Raised when GROQ_API_KEY is missing."""


# ---------------------------------------------------------------------------
# Client (lazy singleton)
# ---------------------------------------------------------------------------
_client: Optional[AsyncGroq] = None


def _get_client() -> AsyncGroq:
    global _client
    if not GROQ_API_KEY:
        raise GroqNotConfigured(
            "GROQ_API_KEY is not set. Add it to your .env file."
        )
    if _client is None:
        _client = AsyncGroq(api_key=GROQ_API_KEY)
    return _client


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """\
You are "X Comment AI" — an expert at writing short, highly engaging replies
to posts on X (formerly Twitter).

GOAL
Write ONE single reply (a "comment") to the user's post that feels like it
came from a clever, witty human — not from an AI.

LANGUAGE RULES
- If the post is in Bengali (বাংলা), reply in natural conversational Bengali
  (you may mix in common English words the way real Bengali users on X do —
  e.g. "bhai", "bro", "savage", "vibe"). Do NOT translate idioms literally.
- If the post is in English, reply in casual modern English.
- If the post mixes languages (Banglish / code-switch), match that style.
- Never write in a language different from the post unless explicitly told to.

STYLE RULES
- Be funny, witty, relatable, or insightful — never generic.
- Use natural lowercase often, like real X users.
- 0–2 emojis MAX, only if they actually add something. Usually use zero.
- 0–1 hashtag MAX, only if it's genuinely relevant. Usually use zero.
- Do NOT use quotation marks around the reply.
- Do NOT start with "As an AI", "Here's a comment", "Reply:", etc.
- Do NOT repeat or paraphrase the original post.
- Do NOT use corporate/marketing tone or hashtag spam.
- Do NOT @-mention anyone unless the post explicitly asks for it.

CONTENT RULES
- React to the SPECIFIC content of the post — what makes THIS post unique.
- It's okay to gently roast, agree enthusiastically, add a hot take, or make
  a clever observation — pick whichever fits the post best.
- Avoid politics, hate, slurs, or anything that could get the account
  flagged. Keep it safe-for-work.

OUTPUT FORMAT
Return ONLY the reply text. No preamble, no explanation, no markdown,
no quotes. Just the comment, exactly as it should be pasted into X.
"""


def _lang_directive(lang: str) -> str:
    lang = (lang or "auto").lower()
    if lang == "en":
        return "Write the reply in English regardless of the post's language."
    if lang == "bn":
        return "Write the reply in natural conversational Bengali (বাংলা)."
    return "Auto-detect the post's language and reply in the same language."


def _build_user_prompt(
    post_text: str,
    thread: Optional[Iterable[str]] = None,
    lang: str = "auto",
    tone: str = "witty",
    length: str = "medium",
) -> str:
    parts: list[str] = []

    parts.append(_lang_directive(lang))
    parts.append(TONES.get(tone, TONES["witty"]))
    parts.append(LENGTHS.get(length, LENGTHS["medium"])[1])
    parts.append("")

    thread_list = [t for t in (thread or []) if t and t.strip()]
    if thread_list:
        parts.append("CONVERSATION SO FAR (oldest first, context only):")
        for i, t in enumerate(thread_list, 1):
            parts.append(f"{i}. {t.strip()}")
        parts.append("")

    parts.append("POST TO REPLY TO:")
    parts.append(post_text.strip())
    parts.append("")
    parts.append("Now write the reply:")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Post-processing
# ---------------------------------------------------------------------------
def _clean_reply(text: str, max_chars: int = 280) -> str:
    """Strip wrapping quotes, common preambles, and trailing whitespace."""
    s = (text or "").strip()

    if len(s) >= 2 and s[0] in {'"', "'", "“", "‘"} and s[-1] in {'"', "'", "”", "’"}:
        s = s[1:-1].strip()

    lowers = s.lower()
    for prefix in (
        "reply:", "comment:", "here's a reply:", "here is a reply:",
        "here's a comment:", "here is a comment:",
    ):
        if lowers.startswith(prefix):
            s = s[len(prefix):].strip()
            break

    if len(s) > max_chars:
        s = s[: max_chars - 1].rstrip() + "…"

    return s


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def _one_call(
    post_text: str,
    thread: Optional[Iterable[str]],
    lang: str,
    tone: str,
    length: str,
    temperature: float,
) -> str:
    client = _get_client()
    user_prompt = _build_user_prompt(
        post_text, thread=thread, lang=lang, tone=tone, length=length
    )
    max_chars = LENGTHS.get(length, LENGTHS["medium"])[0]

    try:
        resp = await client.chat.completions.create(
            model=GROQ_MODEL,
            temperature=temperature,
            max_tokens=GROQ_MAX_TOKENS,
            top_p=0.95,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
    except RateLimitError as e:
        logger.warning("Groq rate-limited: {}", e)
        raise GroqError("Groq is rate-limiting us. Please try again in a moment.") from e
    except APIError as e:
        logger.exception("Groq API error")
        raise GroqError(f"Groq API error: {e}") from e
    except Exception as e:  # noqa: BLE001
        logger.exception("Unexpected Groq failure")
        raise GroqError(f"Unexpected Groq failure: {e}") from e

    if not resp.choices:
        raise GroqError("Groq returned no choices.")

    raw = resp.choices[0].message.content or ""
    cleaned = _clean_reply(raw, max_chars=max_chars)

    if not cleaned:
        raise GroqError("Groq returned an empty reply after cleanup.")

    return cleaned


async def generate_comment(
    post_text: str,
    thread: Optional[Iterable[str]] = None,
    lang: Optional[str] = None,
    tone: str = "witty",
    length: str = "medium",
    temperature: Optional[float] = None,
) -> str:
    """Generate a single ready-to-paste X reply."""
    if not post_text or not post_text.strip():
        raise GroqError("Cannot generate a comment for empty post text.")

    lang = (lang or DEFAULT_REPLY_LANG or "auto").lower()
    tone = (tone or "witty").lower()
    length = (length or "medium").lower()
    temp = GROQ_TEMPERATURE if temperature is None else float(temperature)

    return await _one_call(post_text, thread, lang, tone, length, temp)


async def generate_comments(
    post_text: str,
    thread: Optional[Iterable[str]] = None,
    lang: Optional[str] = None,
    tone: str = "witty",
    length: str = "medium",
    n: int = 3,
    base_temperature: Optional[float] = None,
) -> list[str]:
    """
    Generate `n` reply variants in parallel.

    Each variant uses a slightly different temperature to encourage diversity.
    Failures are logged and dropped — the caller gets whatever succeeded.
    """
    if n < 1:
        n = 1
    if n > 5:
        n = 5

    if not post_text or not post_text.strip():
        raise GroqError("Cannot generate a comment for empty post text.")

    lang = (lang or DEFAULT_REPLY_LANG or "auto").lower()
    tone = (tone or "witty").lower()
    length = (length or "medium").lower()
    base_temp = GROQ_TEMPERATURE if base_temperature is None else float(base_temperature)

    # Spread temperatures around the base to encourage variation.
    # e.g. base=0.8, n=3  ->  [0.7, 0.85, 1.0]
    spread = [-0.10, 0.05, 0.20, -0.20, 0.30][:n]
    temps = [max(0.1, min(1.3, base_temp + d)) for d in spread]

    tasks = [
        _one_call(post_text, thread, lang, tone, length, t)
        for t in temps
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    out: list[str] = []
    seen: set[str] = set()
    for r in results:
        if isinstance(r, Exception):
            logger.warning("variant failed: {}", r)
            continue
        # de-duplicate identical replies
        key = r.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(r)

    if not out:
        # If everything failed, surface the first exception we saw
        for r in results:
            if isinstance(r, Exception):
                raise r if isinstance(r, GroqError) else GroqError(str(r))
        raise GroqError("All variants failed.")

    return out
