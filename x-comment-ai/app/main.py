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
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402
from loguru import logger  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from app.groq_client import (  # noqa: E402
    LENGTHS,
    TONES,
    GroqError,
    GroqNotConfigured,
    generate_comments,
)
from app.scraper import (  # noqa: E402
    InvalidTweetURL,
    NoActiveAccounts,
    ScraperError,
    TweetNotFound,
)
from app.scraper import api as twscrape_api  # noqa: E402
from app.scraper import fetch_tweet_text, init_scraper  # noqa: E402


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
    title="RIFAT < AI",
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


class RegenerateResponse(BaseModel):
    variants: list[str]


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
