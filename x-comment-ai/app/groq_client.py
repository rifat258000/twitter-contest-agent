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
import json
import os
import re
from typing import AsyncIterator, Iterable, Optional

import httpx
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

GROQ_VISION_MODEL = os.getenv(
    "GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"
).strip()
GROQ_OCR_MAX_TOKENS = int(os.getenv("GROQ_OCR_MAX_TOKENS", "2048"))

# Optional fallback providers — if Groq is rate-limited or out of quota, the
# chat endpoint will transparently retry on the next provider in this order:
# Groq → Groq #2 → Gemini → OpenRouter.
GROQ_API_KEY_2 = os.getenv("GROQ_API_KEY_2", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL", "openai/gpt-oss-120b:free"
).strip()
GROQ_CHAT_MODEL = os.getenv("GROQ_CHAT_MODEL", "").strip() or GROQ_MODEL
GROQ_CHAT_MAX_TOKENS = int(os.getenv("GROQ_CHAT_MAX_TOKENS", "1024"))
GROQ_CHAT_TEMPERATURE = float(os.getenv("GROQ_CHAT_TEMPERATURE", "0.7"))


# ---------------------------------------------------------------------------
# Tones & lengths
# ---------------------------------------------------------------------------
TONES: dict[str, str] = {
    "human":      (
        "Sound like a real person who just tapped out a quick reply on their phone. "
        "Write almost entirely in lowercase. Use contractions ('it's', 'i'm', 'gonna', 'idk', 'ngl', 'tbh'). "
        "Skip ending punctuation often. No emojis. No hashtags. No corporate or marketing words. "
        "It can be a fragment, a half-thought, or a casual aside — not a polished sentence. "
        "Avoid being clever or trying too hard; just react naturally like you're texting a friend."
    ),
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
_RETRY_AFTER_SECS_RE = re.compile(r"try again in\s+([0-9.]+)\s*s", re.IGNORECASE)


def _retry_after_seconds(err: RateLimitError, attempt: int) -> float:
    """
    Decide how long to sleep before retrying a Groq 429.

    Priority:
      1. `Retry-After` HTTP header (seconds, integer).
      2. "Please try again in <X>s" hint inside the error body/message.
      3. Exponential backoff: 1s, 2s, 4s, 8s.

    Capped at 30s so a single 429 can't stall a request indefinitely.
    """
    fallback = min(30.0, 2.0 ** attempt)

    resp = getattr(err, "response", None)
    if resp is not None:
        try:
            ra = resp.headers.get("retry-after")
            if ra:
                return min(30.0, max(0.5, float(ra)))
        except (TypeError, ValueError, AttributeError):
            pass

    # Groq error body usually includes "Please try again in 12.345s"
    text = ""
    body = getattr(err, "body", None)
    if isinstance(body, dict):
        text = str(body.get("error", body)) or ""
    text = text or str(err) or ""
    m = _RETRY_AFTER_SECS_RE.search(text)
    if m:
        try:
            return min(30.0, max(0.5, float(m.group(1))))
        except ValueError:
            pass

    return fallback


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

    # Retry on Groq 429s, honoring Retry-After when the client surfaces it.
    # Free-tier hits ~30 RPM; bursts of bulk requests sometimes overshoot.
    max_attempts = 4
    last_429: Optional[RateLimitError] = None
    resp = None
    for attempt in range(max_attempts):
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
            break
        except RateLimitError as e:
            last_429 = e
            wait = _retry_after_seconds(e, attempt)
            if attempt + 1 >= max_attempts:
                logger.warning("Groq rate-limited (gave up after {} tries): {}", attempt + 1, e)
                raise GroqError(
                    f"Groq rate limit hit and retries exhausted (waited up to {wait:.1f}s). "
                    "Try again in a minute."
                ) from e
            logger.info("Groq 429 (attempt {}/{}); sleeping {:.1f}s", attempt + 1, max_attempts, wait)
            await asyncio.sleep(wait)
        except APIError as e:
            logger.exception("Groq API error")
            raise GroqError(f"Groq API error: {e}") from e
        except Exception as e:  # noqa: BLE001
            logger.exception("Unexpected Groq failure")
            raise GroqError(f"Unexpected Groq failure: {e}") from e

    if resp is None:
        # Defensive — loop should have either broken out or raised.
        raise GroqError(
            "Groq rate limit hit and retries exhausted. Try again in a minute."
        ) from last_429

    if not resp.choices:
        raise GroqError("Groq returned no choices.")

    raw = resp.choices[0].message.content or ""
    cleaned = _clean_reply(raw, max_chars=max_chars)

    if not cleaned:
        raise GroqError("Groq returned an empty reply after cleanup.")

    return cleaned


async def _one_call_stream(
    post_text: str,
    thread: Optional[Iterable[str]],
    lang: str,
    tone: str,
    length: str,
    temperature: float,
):
    """Async generator yielding raw token deltas + a final cleaned string.

    Yields:
        ("delta", str)  — every token chunk as it arrives from Groq
        ("done",  str)  — final cleaned reply (after _clean_reply truncation)
        ("error", str)  — terminal error string; no more deltas

    Mirrors `_one_call` retry logic for rate limits, but only the *initial*
    request creation can be retried — once a stream is open, mid-stream
    failures are fatal for that variant.
    """
    client = _get_client()
    user_prompt = _build_user_prompt(
        post_text, thread=thread, lang=lang, tone=tone, length=length
    )
    max_chars = LENGTHS.get(length, LENGTHS["medium"])[0]

    max_attempts = 4
    last_429: Optional[RateLimitError] = None
    stream = None
    for attempt in range(max_attempts):
        try:
            stream = await client.chat.completions.create(
                model=GROQ_MODEL,
                temperature=temperature,
                max_tokens=GROQ_MAX_TOKENS,
                top_p=0.95,
                stream=True,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            )
            break
        except RateLimitError as e:
            last_429 = e
            wait = _retry_after_seconds(e, attempt)
            if attempt + 1 >= max_attempts:
                yield ("error", f"Groq rate limit hit (waited up to {wait:.1f}s). Try again in a minute.")
                return
            await asyncio.sleep(wait)
        except APIError as e:
            yield ("error", f"Groq API error: {e}")
            return
        except Exception as e:  # noqa: BLE001
            yield ("error", f"Unexpected Groq failure: {e}")
            return

    if stream is None:
        yield ("error", f"Groq rate limit hit and retries exhausted (last error: {last_429}).")
        return

    raw_parts: list[str] = []
    try:
        async for chunk in stream:
            try:
                delta = chunk.choices[0].delta.content
            except Exception:  # noqa: BLE001
                delta = None
            if not delta:
                continue
            raw_parts.append(delta)
            yield ("delta", delta)
    except Exception as e:  # noqa: BLE001
        logger.warning("Groq stream interrupted: {}", e)
        yield ("error", f"Stream interrupted: {e}")
        return

    raw = "".join(raw_parts)
    cleaned = _clean_reply(raw, max_chars=max_chars)
    if not cleaned:
        yield ("error", "Empty reply after cleanup.")
        return
    yield ("done", cleaned)


async def generate_comments_stream(
    post_text: str,
    thread: Optional[Iterable[str]] = None,
    lang: Optional[str] = None,
    tone: str = "witty",
    length: str = "medium",
    n: int = 3,
    base_temperature: Optional[float] = None,
):
    """Run N parallel streaming Groq calls, multiplexing token events.

    Yields tuples:
        ("delta", idx, str)
        ("done",  idx, final_cleaned_text)
        ("error", idx, error_string)
        ("all_done", final_variants_list)
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

    spread = [-0.10, 0.05, 0.20, -0.20, 0.30][:n]
    temps = [max(0.1, min(1.3, base_temp + d)) for d in spread]

    queue: asyncio.Queue = asyncio.Queue()
    finals: list[Optional[str]] = [None] * n
    errors: list[Optional[str]] = [None] * n

    async def runner(idx: int, temp: float) -> None:
        try:
            async for ev in _one_call_stream(post_text, thread, lang, tone, length, temp):
                kind, payload = ev
                await queue.put((kind, idx, payload))
                if kind == "done":
                    finals[idx] = payload
                elif kind == "error":
                    errors[idx] = payload
        except Exception as e:  # noqa: BLE001
            errors[idx] = str(e)
            await queue.put(("error", idx, str(e)))

    tasks = [asyncio.create_task(runner(i, t)) for i, t in enumerate(temps)]

    pending = set(range(n))
    while pending:
        kind, idx, payload = await queue.get()
        yield (kind, idx, payload)
        if kind in ("done", "error"):
            pending.discard(idx)

    await asyncio.gather(*tasks, return_exceptions=True)

    # De-dup successful finals while preserving order.
    out: list[str] = []
    seen: set[str] = set()
    for f in finals:
        if not f:
            continue
        key = f.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(f)

    yield ("all_done", -1, out)


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


# ---------------------------------------------------------------------------
# Vision / OCR
# ---------------------------------------------------------------------------
_OCR_SYSTEM = (
    "You extract text from images. Reproduce the text exactly as it appears, "
    "preserving line breaks, lists, and paragraph structure. Do not translate, "
    "summarize, or add commentary."
)

_OCR_INSTRUCTION = (
    "Extract every word of text visible in this image. Output ONLY the extracted "
    "text, exactly as written, including punctuation and line breaks. If the "
    "image contains no readable text, output the single word: NONE."
)


async def extract_text_from_image(
    image_data_url: str,
    *,
    model: Optional[str] = None,
    timeout: float = 30.0,
) -> str:
    """Run OCR on a base64 data URL via Groq's vision model.

    `image_data_url` must be a 'data:image/<type>;base64,<...>' URL.
    Returns the extracted text (empty string if nothing readable).
    """
    if not image_data_url.startswith("data:image/"):
        raise GroqError("image_data_url must be a base64 data URL")

    client = _get_client()
    use_model = (model or GROQ_VISION_MODEL).strip()

    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=use_model,
                temperature=0.0,
                max_tokens=GROQ_OCR_MAX_TOKENS,
                messages=[
                    {"role": "system", "content": _OCR_SYSTEM},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": _OCR_INSTRUCTION},
                            {"type": "image_url", "image_url": {"url": image_data_url}},
                        ],
                    },
                ],
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError as e:
        raise GroqError(f"OCR timed out after {timeout:.0f}s") from e
    except RateLimitError as e:
        raise GroqError(f"Groq rate-limited: {e}") from e
    except APIError as e:
        raise GroqError(f"Groq API error: {e}") from e

    text = (resp.choices[0].message.content or "").strip()
    if text.upper() == "NONE":
        return ""
    return text


# ---------------------------------------------------------------------------
# Chat — multi-provider streaming with automatic fallback
# ---------------------------------------------------------------------------
CHAT_SYSTEM_DEFAULT = (
    "You are Rifat Ai Model, a helpful AI assistant. "
    "Be concise, friendly, and direct. Skip filler ('Sure!', 'Of course!'). "
    "Use markdown headings/lists only when they meaningfully aid clarity. "
    "If the user asks you to draft a reply for an X/Twitter post, keep it "
    "natural, under 280 characters, no emojis unless requested, and never "
    "use corporate marketing words."
)


def _chat_providers() -> list[tuple[str, str, str]]:
    """Build the ordered list of (name, api_key, model) for chat fallback.

    Order is intentional: Groq is fastest (~700 tok/s), Gemini is most
    generous on free-tier daily quota, OpenRouter is slowest but truly
    independent infra.
    """
    out: list[tuple[str, str, str]] = []
    if GROQ_API_KEY:
        out.append(("groq", GROQ_API_KEY, GROQ_CHAT_MODEL))
    if GROQ_API_KEY_2:
        out.append(("groq2", GROQ_API_KEY_2, GROQ_CHAT_MODEL))
    if GEMINI_API_KEY:
        out.append(("gemini", GEMINI_API_KEY, GEMINI_MODEL))
    if OPENROUTER_API_KEY:
        out.append(("openrouter", OPENROUTER_API_KEY, OPENROUTER_MODEL))
    return out


async def _stream_groq(
    api_key: str, model: str, messages: list[dict],
) -> AsyncIterator[str]:
    client = AsyncGroq(api_key=api_key)
    stream = await client.chat.completions.create(
        model=model,
        messages=messages,
        stream=True,
        max_tokens=GROQ_CHAT_MAX_TOKENS,
        temperature=GROQ_CHAT_TEMPERATURE,
    )
    async for chunk in stream:
        try:
            delta = chunk.choices[0].delta.content
        except (AttributeError, IndexError):
            delta = None
        if delta:
            yield delta


async def _stream_openrouter(
    api_key: str, model: str, messages: list[dict],
) -> AsyncIterator[str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # OpenRouter recommends these for proper attribution / rate-limit tier:
        "HTTP-Referer": os.getenv("OPENROUTER_SITE_URL", "https://rifat-ai.fly.dev"),
        "X-Title": "Rifat Ai Model",
    }
    body = {
        "model": model,
        "messages": messages,
        "stream": True,
        "max_tokens": GROQ_CHAT_MAX_TOKENS,
        "temperature": GROQ_CHAT_TEMPERATURE,
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as http:
        async with http.stream(
            "POST",
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=body,
        ) as resp:
            if resp.status_code != 200:
                err = (await resp.aread()).decode("utf-8", "ignore")[:300]
                raise GroqError(f"OpenRouter HTTP {resp.status_code}: {err}")
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    if payload == "[DONE]":
                        return
                    continue
                try:
                    obj = json.loads(payload)
                    delta = obj["choices"][0].get("delta", {}).get("content")
                except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                    continue
                if delta:
                    yield delta


async def _stream_gemini(
    api_key: str, model: str, messages: list[dict],
) -> AsyncIterator[str]:
    """Stream tokens from Google Gemini's REST API.

    Gemini uses a different shape from OpenAI/Groq:
    - role names are "user" / "model" (not "user" / "assistant")
    - the system prompt goes in `systemInstruction`, not the messages list
    - chunks come back as JSON objects with `candidates[0].content.parts[].text`
    """
    # Split the system message off (if present at messages[0]).
    sys_prompt = ""
    msgs = list(messages)
    if msgs and msgs[0].get("role") == "system":
        sys_prompt = msgs[0].get("content", "") or ""
        msgs = msgs[1:]

    contents = []
    for m in msgs:
        role = "user" if m["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})

    body: dict = {
        "contents": contents,
        "generationConfig": {
            "maxOutputTokens": GROQ_CHAT_MAX_TOKENS,
            "temperature": GROQ_CHAT_TEMPERATURE,
        },
    }
    if sys_prompt:
        body["systemInstruction"] = {"parts": [{"text": sys_prompt}]}

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:streamGenerateContent?alt=sse"
    )
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as http:
        async with http.stream("POST", url, headers=headers, json=body) as resp:
            if resp.status_code != 200:
                err = (await resp.aread()).decode("utf-8", "ignore")[:300]
                raise GroqError(f"Gemini HTTP {resp.status_code}: {err}")
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                # Surface explicit errors that come back as a JSON object.
                if "error" in obj:
                    msg = obj["error"].get("message", str(obj["error"]))
                    raise GroqError(f"Gemini: {msg}")
                try:
                    parts = obj["candidates"][0]["content"]["parts"]
                except (KeyError, IndexError, TypeError):
                    continue
                for p in parts:
                    text = p.get("text") if isinstance(p, dict) else None
                    if text:
                        yield text


async def chat_stream(
    messages: list[dict],
    *,
    system: Optional[str] = None,
) -> AsyncIterator[str]:
    """Stream chat tokens from the first available provider; fall back on errors.

    `messages` is a list of {role: 'user'|'assistant', content: str} (no system
    role — pass `system` separately). Yields plain string deltas.

    Raises GroqNotConfigured if no provider is set up.
    Raises GroqError if all configured providers fail before producing tokens.
    Once tokens start flowing for a provider, errors propagate as GroqError
    rather than falling back (the user has already seen partial output).
    """
    providers = _chat_providers()
    if not providers:
        raise GroqNotConfigured(
            "No AI provider configured. Set GROQ_API_KEY (and optionally "
            "GROQ_API_KEY_2 / GEMINI_API_KEY / OPENROUTER_API_KEY) in your "
            "environment."
        )

    # Build the full message stack with system prompt at index 0.
    full = [{"role": "system", "content": system or CHAT_SYSTEM_DEFAULT}]
    full.extend(messages)

    last_err: Optional[Exception] = None
    for name, key, model in providers:
        if name == "openrouter":
            agen = _stream_openrouter(key, model, full)
        elif name == "gemini":
            agen = _stream_gemini(key, model, full)
        else:
            agen = _stream_groq(key, model, full)
        try:
            first_chunk = await agen.__anext__()
        except StopAsyncIteration:
            # Empty stream — try the next provider.
            last_err = GroqError(f"{name}: empty response")
            logger.warning(f"chat: {name} returned no tokens; falling back")
            continue
        except (RateLimitError, APIError, GroqError, httpx.HTTPError, asyncio.TimeoutError) as e:
            last_err = e
            logger.warning(f"chat: {name} pre-stream error ({type(e).__name__}: {e}); falling back")
            continue
        except Exception as e:  # pragma: no cover — guard against SDK surprises
            last_err = e
            logger.warning(f"chat: {name} unexpected pre-stream error ({type(e).__name__}: {e}); falling back")
            continue

        # First chunk landed — commit to this provider.
        yield first_chunk
        try:
            async for delta in agen:
                yield delta
        except (RateLimitError, APIError, GroqError, httpx.HTTPError, asyncio.TimeoutError) as e:
            # Already streaming — surface as a clean error so the frontend can
            # display "(connection dropped)" rather than silently truncating.
            raise GroqError(f"{name}: stream interrupted ({e})") from e
        return

    raise GroqError(f"All chat providers failed. Last error: {last_err}")


def chat_provider_status() -> dict:
    """Used by /health to report which AI providers are configured."""
    return {
        "groq":       bool(GROQ_API_KEY),
        "groq_2":     bool(GROQ_API_KEY_2),
        "gemini":     bool(GEMINI_API_KEY),
        "openrouter": bool(OPENROUTER_API_KEY),
        "providers":  [p[0] for p in _chat_providers()],
    }
