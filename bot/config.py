from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, load_dotenv


def _bootstrap_env() -> None:
    root_dir = Path(__file__).resolve().parent.parent
    env_path = root_dir / ".env"
    example_path = root_dir / ".env.example"

    load_dotenv(env_path)

    if example_path.exists():
        for key, value in dotenv_values(example_path).items():
            if value and not os.getenv(key):
                os.environ[key] = value


_bootstrap_env()


def _get_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(slots=True)
class Settings:
    api_id: int
    api_hash: str
    bot_token: str
    mongo_uri: str
    mongo_db: str
    session_name: str
    force_sub_channels: list[str]
    force_sub_links: list[str]
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            api_id=int(_get_required("API_ID")),
            api_hash=_get_required("API_HASH"),
            bot_token=_get_required("BOT_TOKEN"),
            mongo_uri=_get_required("MONGO_URI"),
            mongo_db=os.getenv("MONGO_DB", "heroku_helper_bot"),
            session_name=os.getenv("SESSION_NAME", "heroku-helper-bot"),
            force_sub_channels=_split_csv(os.getenv("FORCE_SUB_CHANNELS")),
            force_sub_links=_split_csv(os.getenv("FORCE_SUB_LINKS")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
