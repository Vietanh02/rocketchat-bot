"""Application settings, loaded from environment / .env with the APP_ prefix."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Portal / app
    secret_key: str = "dev-insecure-secret-change-me"
    database_url: str = "sqlite+aiosqlite:///./data/bot.db"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    log_level: str = "INFO"
    jwt_algorithm: str = "HS256"
    jwt_ttl_minutes: int = 12 * 60

    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: str | None = None

    # Rocket.Chat defaults; the portal's bot_config row wins once it is filled in.
    rc_rest_url: str = "http://localhost:3000"
    rc_ws_url: str = "ws://localhost:3000/websocket"
    rc_bot_user_id: str | None = None
    rc_bot_auth_token: str | None = None
    rc_bot_username: str | None = None
    rc_bot_password: str | None = None

    # Integration safety
    allow_private_integration_hosts: bool = False
    integration_timeout_seconds: float = 10.0

    # Script commands: files in this folder can be bound to a command in the
    # portal. The portal can only pick from it, never write to it.
    scripts_dir: str = str(Path(__file__).resolve().parents[2] / "scripts")
    script_max_timeout_seconds: float = 300.0
    # Skills: one folder each, with a SKILL.md and optional scripts/.
    skills_dir: str = str(Path(__file__).resolve().parents[2] / "skills")
    # Scripts running at once, across chat and the portal's tester.
    script_max_concurrency: int = Field(default=20, ge=1, le=200)
    # Comma-separated names of environment variables scripts may see (API keys,
    # say). Nothing else from the bot's environment reaches them.
    script_env_passthrough: str = ""

    # Language model behind an OpenAI-compatible API. Only seeds `llm_config`
    # when the database is created; the portal's Language model page owns it.
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    # "{bot}" is replaced with the bot's username. Empty uses a built-in prompt.
    llm_system_prompt: str = ""
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_max_tokens: int | None = Field(default=None, ge=1)
    llm_temperature: float | None = Field(default=None, ge=0, le=2)
    # Earlier messages (questions and answers) sent along with each question.
    llm_history_messages: int = Field(default=10, ge=0, le=100)

    # Vector database (Qdrant, REST port). Empty: memory and knowledge are
    # found by their words only. The embedding model is set in the portal.
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    # Put before every collection name, so several bots can share a Qdrant.
    qdrant_collection_prefix: str = "rcbot_"
    qdrant_timeout_seconds: float = Field(default=10.0, gt=0)

    # Uploaded knowledge base files are kept here, to be read again later.
    kb_files_dir: str = "./data/kb"
    # Folders copied here (e.g. a Confluence export) can be imported from the portal.
    kb_import_dir: str = str(Path(__file__).resolve().parents[2] / "kb_import")

    # Bot runtime
    bot_enabled: bool = True
    inbound_queue_size: int = 500
    # Messages handled at the same time, each from a different user or room.
    bot_max_concurrency: int = Field(default=20, ge=1, le=200)

    @property
    def script_env_passthrough_list(self) -> list[str]:
        return [n.strip() for n in self.script_env_passthrough.split(",") if n.strip()]

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
