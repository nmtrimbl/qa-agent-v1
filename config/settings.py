from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Central place for configuration.

    This keeps things beginner-friendly: you only need to edit environment variables.
    """

    openai_api_key: str = Field(alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")

    # FastAPI / Streamlit integration
    fastapi_url: str = Field(default="http://localhost:8000/run-test", alias="FASTAPI_URL")

    # Browser settings
    playwright_headless: bool = Field(default=True, alias="PLAYWRIGHT_HEADLESS")

    # Where we store files (logs/artifacts). These are relative to this repo folder.
    artifacts_dir: str = Field(default="artifacts", alias="ARTIFACTS_DIR")
    logs_dir: str = Field(default="logs", alias="LOGS_DIR")

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )


def get_settings() -> Settings:
    """
    Load settings from environment (.env file supported via pydantic-settings).
    """

    return Settings()

