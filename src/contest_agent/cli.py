"""Command-line entry point."""

from __future__ import annotations

import asyncio
import logging

import typer
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from .agent import Agent
from .config import get_settings

app = typer.Typer(help="Twitter contest & AI event agent")
console = Console()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, markup=False)],
    )


@app.command()
def run(
    dry_run: bool = typer.Option(False, help="Do not actually post to Telegram."),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Run one scrape/extract/rank/post cycle and exit."""
    _setup_logging(verbose)
    agent = Agent()
    stats = asyncio.run(agent.run_once(dry_run=dry_run))
    _print_stats(stats)


@app.command()
def schedule(
    verbose: bool = typer.Option(False, "-v", "--verbose"),
    dry_run: bool = typer.Option(False, help="Do not actually post to Telegram."),
) -> None:
    """Run continuously on an APScheduler interval."""
    _setup_logging(verbose)
    settings = get_settings()
    agent = Agent(settings)

    async def _main() -> None:
        scheduler = AsyncIOScheduler()

        async def _tick() -> None:
            try:
                stats = await agent.run_once(dry_run=dry_run)
                _print_stats(stats)
            except Exception:
                logging.exception("run_once failed")

        scheduler.add_job(
            _tick,
            "interval",
            minutes=settings.poll_interval_minutes,
            next_run_time=None,
        )
        scheduler.start()
        logging.info(
            "Scheduled polling every %d minutes. Running first cycle now...",
            settings.poll_interval_minutes,
        )
        await _tick()
        stop = asyncio.Event()
        try:
            await stop.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            scheduler.shutdown()

    asyncio.run(_main())


@app.command()
def test_telegram() -> None:
    """Send a test message to the configured Telegram chat."""
    _setup_logging(True)
    from .telegram import TelegramPoster

    settings = get_settings()
    poster = TelegramPoster(settings)
    if not poster.configured:
        console.print(
            "[red]Telegram is not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.[/red]"
        )
        raise typer.Exit(1)
    asyncio.run(poster.send("✅ Twitter contest agent — test message"))
    console.print("[green]Test message sent.[/green]")


def _print_stats(stats) -> None:  # type: ignore[no-untyped-def]
    table = Table(title="Run summary")
    table.add_column("metric")
    table.add_column("count", justify="right")
    table.add_row("scraped", str(stats.scraped))
    table.add_row("extracted contests", str(stats.extracted_contests))
    table.add_row("posted", str(stats.posted))
    table.add_row("skipped (duplicate)", str(stats.skipped_dup))
    table.add_row("skipped (low score)", str(stats.skipped_low_score))
    console.print(table)


if __name__ == "__main__":
    app()
