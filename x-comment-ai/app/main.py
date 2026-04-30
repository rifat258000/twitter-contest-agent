"""
FastAPI entrypoint for RIFAT < AI.
Run with:  uvicorn app.main:app --reload
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv

# Load .env BEFORE importing modules that read env vars at import time.
load_dotenv()

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402
from loguru import logger  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from app.groq_client import (  # noqa: E402
    LENGTHS,
    TONES,
    GroqError,
    GroqNotConfigured,
    extract_text_from_image,
    generate_comments,
    generate_comments_stream,
)
from app.scraper import (  # noqa: E402
    InvalidTweetURL,
    NoActiveAccounts,
    ScraperError,
    ScraperRateLimited,
    TweetNotFound,
)
from app.scraper import api as twscrape_api  # noqa: E402
from app.scraper import fetch_tweet_text, init_scraper  # noqa: E402
from app.contests import search_contests  # noqa: E402


# ---------------------------------------------------------------------------
# App / lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting RIFAT < AI...")
    try:
        await init_scraper()
    except Exception as e:  # noqa: BLE001
        logger.error("Scraper init failed: {}", e)
    yield
    logger.info("Shutting down RIFAT < AI.")


app = FastAPI(
    title="Rifat Ai Model",
    description="Generate engaging X (Twitter) replies with Groq + twscrape.",
    version="1.0.0",
    lifespan=lifespan,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class GenerateRequest(BaseModel):
    url: str = Field(..., min_length=5, description="X post URL or tweet ID")
    lang: Optional[str] = Field(default=None, description='"auto" | "en" | "bn"')
    tone: Optional[str] = Field(default="witty", description="Tone preset")
    length: Optional[str] = Field(default="medium", description="short | medium | long")
    n: int = Field(default=3, ge=1, le=5, description="Number of variants")


class RegenerateRequest(BaseModel):
    """Re-uses already-fetched tweet content; no scrape required."""
    original: str = Field(..., min_length=1)
    thread: list[str] = Field(default_factory=list)
    lang: Optional[str] = Field(default=None)
    tone: Optional[str] = Field(default="witty")
    length: Optional[str] = Field(default="medium")
    n: int = Field(default=3, ge=1, le=5)


class GenerateResponse(BaseModel):
    url: str
    tweet_id: str
    author: str
    author_name: str
    original: str
    thread: list[str]
    variants: list[str]


class ContestSearchRequest(BaseModel):
    mode: str = Field(default="ai", description='"ai" | "all" | "custom"')
    custom_queries: list[str] = Field(default_factory=list)
    min_engagement: float = Field(default=0.0, ge=0.0)
    limit: int = Field(default=30, ge=1, le=100)
    recency_hours: int = Field(default=72, ge=1, le=720)
    require_contest_keywords: bool = Field(default=True)


class ContestSearchResponse(BaseModel):
    count: int
    mode: str
    results: list[dict]


class RegenerateResponse(BaseModel):
    variants: list[str]


class PreviewRequest(BaseModel):
    """Lightweight 'just scrape, don't generate' for the auto-paste preview UI.

    Resolves URLs into author/handle/text in parallel, capped at 10 per call to
    avoid burning the X scraping budget on a paste of hundreds of URLs.
    """
    urls: list[str] = Field(default_factory=list, max_length=10)


class PreviewItem(BaseModel):
    url: str
    ok: bool
    tweet_id: Optional[str] = None
    author: Optional[str] = None
    author_name: Optional[str] = None
    text_preview: Optional[str] = None
    error: Optional[str] = None


class PreviewResponse(BaseModel):
    results: list[PreviewItem]


class OCRRequest(BaseModel):
    """Extract text from a single image (base64 data URL, max ~4 MB).

    Frontend already downscales/encodes; we only validate prefix here.
    """
    image: str = Field(..., min_length=20, description="data:image/...;base64,...")


class OCRResponse(BaseModel):
    text: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
async def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "tones": list(TONES.keys()),
            "lengths": list(LENGTHS.keys()),
        },
    )


@app.get("/manifest.webmanifest", include_in_schema=False)
async def manifest():
    """PWA manifest. Served from /manifest.webmanifest so the URL stays at the
    site root (better install heuristics in some browsers)."""
    return FileResponse(
        os.path.join(BASE_DIR, "static", "manifest.webmanifest"),
        media_type="application/manifest+json",
    )


@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    """Service worker MUST be served from the site root for its scope to cover '/'.
    Adds Service-Worker-Allowed so we can also broaden scope explicitly."""
    return FileResponse(
        os.path.join(BASE_DIR, "static", "sw.js"),
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(
        os.path.join(BASE_DIR, "static", "icons", "icon-32.png"),
        media_type="image/png",
    )


@app.get("/health")
async def health():
    accounts = await twscrape_api.pool.accounts_info()
    active = sum(1 for a in accounts if a.get("active"))
    return {
        "status": "ok",
        "accounts_total": len(accounts),
        "accounts_active": active,
        "groq_configured": bool(os.getenv("GROQ_API_KEY")),
        "model": os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
        "tones": list(TONES.keys()),
        "lengths": list(LENGTHS.keys()),
    }


@app.post("/generate", response_model=GenerateResponse)
async def generate(payload: GenerateRequest):
    # 1) Scrape the tweet
    try:
        tweet = await fetch_tweet_text(payload.url, include_thread=True)
    except InvalidTweetURL as e:
        raise HTTPException(status_code=400, detail=str(e))
    except TweetNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except NoActiveAccounts as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ScraperRateLimited as e:
        raise HTTPException(status_code=429, detail=str(e))
    except ScraperError as e:
        raise HTTPException(status_code=502, detail=f"Scraper failed: {e}")

    # 2) Generate N variants in parallel
    try:
        variants = await generate_comments(
            post_text=tweet.text,
            thread=tweet.thread,
            lang=payload.lang,
            tone=payload.tone or "witty",
            length=payload.length or "medium",
            n=payload.n,
        )
    except GroqNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e))
    except GroqError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return GenerateResponse(
        url=tweet.url,
        tweet_id=str(tweet.id),
        author=tweet.author,
        author_name=tweet.author_name,
        original=tweet.text,
        thread=tweet.thread,
        variants=variants,
    )


@app.post("/contests/search", response_model=ContestSearchResponse)
async def contests_search(payload: ContestSearchRequest):
    """Discover currently-running contests on X, ranked by engagement."""
    mode = payload.mode if payload.mode in ("ai", "all", "custom") else "ai"
    try:
        results = await search_contests(
            mode=mode,
            custom_queries=payload.custom_queries or None,
            min_engagement=payload.min_engagement,
            limit=payload.limit,
            recency_hours=payload.recency_hours,
            require_contest_keywords=payload.require_contest_keywords,
        )
    except NoActiveAccounts as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ScraperRateLimited as e:
        raise HTTPException(status_code=429, detail=str(e))
    except ScraperError as e:
        raise HTTPException(status_code=502, detail=f"Scraper failed: {e}")

    return ContestSearchResponse(
        count=len(results),
        mode=mode,
        results=[r.to_dict() for r in results],
    )


@app.post("/generate/stream")
async def generate_stream(payload: GenerateRequest):
    """SSE: scrape the tweet, then stream Groq variant tokens as they arrive.

    Event types (each line: `data: <json>\\n\\n`):
      meta         { url, tweet_id, author, author_name, original, thread }
      delta        { idx, delta }
      variant_done { idx, text }
      variant_error{ idx, error }
      done         { variants: [...] }
      error        { error }   # terminal — only when scrape fails before stream
    """
    import asyncio as _asyncio
    import json as _json

    async def event_stream():
        def sse(event_type: str, payload: dict) -> bytes:
            return f"event: {event_type}\ndata: {_json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")

        # 1) Scrape — this is fast (twscrape) but failure should terminate stream early.
        try:
            tweet = await fetch_tweet_text(payload.url, include_thread=True)
        except InvalidTweetURL as e:
            yield sse("error", {"error": str(e), "code": 400})
            return
        except TweetNotFound as e:
            yield sse("error", {"error": str(e), "code": 404})
            return
        except NoActiveAccounts as e:
            yield sse("error", {"error": str(e), "code": 503})
            return
        except ScraperRateLimited as e:
            yield sse("error", {"error": str(e), "code": 429})
            return
        except ScraperError as e:
            yield sse("error", {"error": f"Scraper failed: {e}", "code": 502})
            return

        yield sse("meta", {
            "url": tweet.url,
            "tweet_id": str(tweet.id),
            "author": tweet.author,
            "author_name": tweet.author_name,
            "original": tweet.text,
            "thread": list(tweet.thread or []),
        })

        # 2) Stream variants
        try:
            async for ev in generate_comments_stream(
                post_text=tweet.text,
                thread=tweet.thread,
                lang=payload.lang,
                tone=payload.tone or "witty",
                length=payload.length or "medium",
                n=payload.n,
            ):
                kind, idx, payload_v = ev
                if kind == "delta":
                    yield sse("delta", {"idx": idx, "delta": payload_v})
                elif kind == "done":
                    yield sse("variant_done", {"idx": idx, "text": payload_v})
                elif kind == "error":
                    yield sse("variant_error", {"idx": idx, "error": payload_v})
                elif kind == "all_done":
                    yield sse("done", {"variants": payload_v})
                # Cooperatively yield to the event loop so chunks flush promptly
                await _asyncio.sleep(0)
        except GroqNotConfigured as e:
            yield sse("error", {"error": str(e), "code": 503})
        except GroqError as e:
            yield sse("error", {"error": str(e), "code": 502})
        except Exception as e:  # noqa: BLE001
            logger.exception("generate_stream crashed")
            yield sse("error", {"error": f"Streaming failed: {e}", "code": 500})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # disable proxy buffering (nginx, fly edge)
            "Connection": "keep-alive",
        },
    )


@app.post("/preview", response_model=PreviewResponse)
async def preview(payload: PreviewRequest):
    """Resolve URLs to tweet metadata in parallel WITHOUT calling Groq.

    Used by the frontend to render real post cards as soon as a user pastes URLs,
    so the wait between paste and Generate doesn't feel empty. Each URL gets
    short-circuited if it fails — failures don't propagate to other URLs.
    """
    import asyncio as _asyncio

    sem = _asyncio.Semaphore(3)  # match scrape concurrency cap

    async def _one(url: str) -> PreviewItem:
        async with sem:
            try:
                t = await fetch_tweet_text(url, include_thread=False)
                preview_text = (t.text or "")[:240]
                return PreviewItem(
                    url=t.url,
                    ok=True,
                    tweet_id=str(t.id),
                    author=t.author,
                    author_name=t.author_name,
                    text_preview=preview_text,
                )
            except (InvalidTweetURL, TweetNotFound, NoActiveAccounts, ScraperError) as e:
                return PreviewItem(url=url, ok=False, error=str(e))
            except Exception as e:  # noqa: BLE001
                return PreviewItem(url=url, ok=False, error=f"Preview failed: {e}")

    results = await _asyncio.gather(*(_one(u) for u in payload.urls))
    return PreviewResponse(results=list(results))


@app.post("/ocr", response_model=OCRResponse)
async def ocr(payload: OCRRequest):
    """Extract text from an image using Groq's vision model."""
    img = payload.image.strip()
    if not img.startswith("data:image/"):
        raise HTTPException(
            status_code=400,
            detail="image must be a data URL like 'data:image/png;base64,...'",
        )
    # Soft cap at ~6 MB on the wire (base64 of ~4 MB binary).
    if len(img) > 6_500_000:
        raise HTTPException(
            status_code=413,
            detail="Image too large. Please use one under 4 MB.",
        )
    try:
        text = await extract_text_from_image(img)
    except GroqNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e))
    except GroqError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return OCRResponse(text=text)


@app.post("/regenerate", response_model=RegenerateResponse)
async def regenerate(payload: RegenerateRequest):
    """Generate fresh variants without re-scraping the tweet."""
    try:
        variants = await generate_comments(
            post_text=payload.original,
            thread=payload.thread,
            lang=payload.lang,
            tone=payload.tone or "witty",
            length=payload.length or "medium",
            n=payload.n,
            # bump temperature slightly so regenerate feels different
            base_temperature=min(1.2, float(os.getenv("GROQ_TEMPERATURE", "0.8")) + 0.15),
        )
    except GroqNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e))
    except GroqError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return RegenerateResponse(variants=variants)


# ---------------------------------------------------------------------------
# Friendly JSON for unhandled errors
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on {}: {}", request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error. Please try again."},
    )
