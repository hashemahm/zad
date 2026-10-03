"""Application settings, loaded from environment variables and the project's .env file."""

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

ALL_SOURCES = ("youtube", "reddit", "hackernews", "devto", "medium")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "Tech Microlearning"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    log_level: str = "INFO"

    database_url: str = "sqlite:///./data/microlearning.db"

    enabled_sources: str = ",".join(ALL_SOURCES)

    youtube_api_key: str = ""
    youtube_relevance_language: str = "en"
    youtube_region_code: str = ""

    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "tech-microlearning/0.1"
    reddit_min_score: int = 10

    hackernews_min_points: int = 20

    crawl_results_per_source: int = 15
    crawl_interval_minutes: int = 360
    crawl_on_startup: bool = False
    http_timeout_seconds: float = 15.0
    http_user_agent: str = "Mozilla/5.0 (compatible; TechMicrolearning/0.1)"

    estimate_reading_time: bool = True
    reading_words_per_minute: int = 230
    max_concurrent_page_fetches: int = 5

    priority_weights: str = "16,8,4,2,1"
    feed_default_size: int = 12

    default_max_video_minutes: int = 10
    default_max_reading_minutes: int = 8

    @field_validator("database_url")
    @classmethod
    def _resolve_sqlite_path(cls, value: str) -> str:
        # Make relative SQLite paths independent of the current working directory.
        prefix = "sqlite:///"
        if value.startswith(prefix) and not value.startswith(prefix + "/"):
            relative = value[len(prefix):]
            if relative != ":memory:":
                return prefix + str((BASE_DIR / relative).resolve())
        return value

    @field_validator("priority_weights")
    @classmethod
    def _validate_weights(cls, value: str) -> str:
        parts = [p.strip() for p in value.split(",") if p.strip()]
        if len(parts) != 5 or any(float(p) <= 0 for p in parts):
            raise ValueError("PRIORITY_WEIGHTS must be 5 positive numbers for P1..P5")
        return value

    @property
    def sources(self) -> list[str]:
        names = [s.strip().lower() for s in self.enabled_sources.split(",") if s.strip()]
        unknown = set(names) - set(ALL_SOURCES)
        if unknown:
            raise ValueError(f"Unknown sources in ENABLED_SOURCES: {', '.join(sorted(unknown))}")
        return names

    @property
    def weights(self) -> dict[int, float]:
        """Map priority (1..5) to its feed weight."""
        values = [float(p) for p in self.priority_weights.split(",") if p.strip()]
        return {priority: weight for priority, weight in enumerate(values, start=1)}


@lru_cache
def get_settings() -> Settings:
    return Settings()
