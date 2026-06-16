from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # silently ignore unknown env vars (e.g. REDIS_URL)
    )

    # GitHub
    github_token: str
    github_webhook_secret: str
    github_repo: str  # e.g. "widebirb/BQTR"

    # LLM (Ollama — OpenAI-compatible endpoint)
    ollama_base_url: str = "http://localhost:11434"
    ollama_llm_model: str = "qwen2.5-coder:1.5b"
    llm_api_key: str = "ollama"  # Ollama ignores this; required by openai SDK

    # Tuning
    max_review_comments: int = 10

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    @property
    def llm_base_url(self) -> str:
        """OpenAI-compatible base URL for Ollama."""
        return f"{self.ollama_base_url.rstrip('/')}/v1"

    @property
    def repo_owner(self) -> str:
        return self.github_repo.split("/")[0]

    @property
    def repo_name(self) -> str:
        return self.github_repo.split("/")[1]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()


def load_playbook() -> str:
    """Load the orchestrator persona from playbook.md."""
    playbook_path = Path(__file__).parent / "playbook.md"
    if not playbook_path.exists():
        return "You are a helpful AI orchestrator that manages pull request reviews."
    return playbook_path.read_text(encoding="utf-8")
