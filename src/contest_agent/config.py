"""Runtime configuration loaded from environment / .env file."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")

    ollama_base_url: str = Field(default="http://localhost:11434")
    ollama_model: str = Field(default="llama3.1:8b")

    twscrape_db_path: str = Field(default="./data/twscrape.db")
    nitter_instances: str = Field(
        default="https://nitter.net,https://nitter.privacydev.net,https://nitter.poast.org"
    )

    weight_likes: float = 1.0
    weight_retweets: float = 2.0
    weight_replies: float = 1.5
    weight_quotes: float = 2.0
    weight_views: float = 0.001

    poll_interval_minutes: int = 30
    max_tweets_per_query: int = 50
    min_engagement_score: float = 10.0
    top_n_per_run: int = 5

    dedup_db_path: str = Field(default="./data/posted.db")

    @property
    def nitter_instance_list(self) -> list[str]:
        return [s.strip() for s in self.nitter_instances.split(",") if s.strip()]


def get_settings() -> Settings:
    return Settings()
