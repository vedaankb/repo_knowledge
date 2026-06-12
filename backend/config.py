from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    gemini_api_key: str = ""
    gemini_embed_model: str = "text-embedding-004"
    gemini_chat_model: str = "gemini-2.5-flash"

    github_token: str | None = None

    database_url: str = "postgresql://repoknow:repoknow@localhost:5433/repoknow"

    sync_interval_hours: int = 12
    max_file_bytes: int = 400_000
    embed_batch_size: int = 64
    top_k_code: int = 10
    top_k_skills: int = 5
    min_retrieval_score: float = 0.30

    embedding_dim: int = 768

    # Gemini generation output limits (tokens). Plan mode needs more room for
    # multi-section security / architecture answers.
    max_output_tokens_strict: int = 2048
    max_output_tokens_plan: int = 8192


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
