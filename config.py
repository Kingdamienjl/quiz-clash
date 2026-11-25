"""Configuration for the kingdamienjl Twitch quiz bot.

Values come from environment variables, optionally seeded from a `.env`
file in the project directory (never commit `.env` — it holds secrets).
Paths use pathlib so the same code runs on Linux (R810 LXC) and Windows.

Required before the bot can join chat:
  TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, TWITCH_BOT_ID (== owner id here,
  the bot logs in as kingdamienjl himself), TWITCH_ACCESS_TOKEN,
  TWITCH_REFRESH_TOKEN.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = PROJECT_DIR / "data"


def _parse_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            values[key] = val
    return values


def load_env(env_path: Path | None = None) -> dict[str, str]:
    """`.env` values, with real environment variables winning on conflict."""
    merged = _parse_dotenv(env_path or (PROJECT_DIR / ".env"))
    merged.update({k: v for k, v in os.environ.items() if v != ""})
    return merged


@dataclass(frozen=True)
class Config:
    channel_login: str = "kingdamienjl"
    # kingdamienjl's public Twitch user id (decapi.me, 2026-11-19).
    # The bot logs in as Chris himself, so bot id == owner id.
    bot_id: str = "59499420"
    owner_id: str = "59499420"
    client_id: str = ""
    client_secret: str = ""
    access_token: str = ""
    refresh_token: str = ""
    ollama_url: str = "http://100.95.184.104:11434"
    ollama_model: str = "hermes3:8b"
    proactive_interval_minutes: int = 15
    question_timeout_seconds: int = 30
    questions_path: Path = PROJECT_DIR / "questions.json"
    data_dir: Path = DEFAULT_DATA_DIR
    with_adapter: bool = False  # True only for the web-OAuth authorize flow
    # Live overlay for OBS (overlay.py). Served on all interfaces so the
    # streaming PC can open it as a browser source.
    overlay_enabled: bool = True
    overlay_port: int = 8765

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        e = load_env() if env is None else env
        return cls(
            channel_login=e.get("TWITCH_CHANNEL", "kingdamienjl"),
            bot_id=e.get("TWITCH_BOT_ID", "59499420"),
            owner_id=e.get("TWITCH_OWNER_ID", e.get("TWITCH_BOT_ID", "59499420")),
            client_id=e.get("TWITCH_CLIENT_ID", ""),
            client_secret=e.get("TWITCH_CLIENT_SECRET", ""),
            access_token=e.get("TWITCH_ACCESS_TOKEN", ""),
            refresh_token=e.get("TWITCH_REFRESH_TOKEN", ""),
            ollama_url=e.get("OLLAMA_URL", "http://100.95.184.104:11434"),
            ollama_model=e.get("OLLAMA_MODEL", "hermes3:8b"),
            proactive_interval_minutes=int(e.get("PROACTIVE_INTERVAL_MINUTES", "15")),
            question_timeout_seconds=int(e.get("QUESTION_TIMEOUT_SECONDS", "30")),
            questions_path=Path(e.get("QUESTIONS_PATH", str(PROJECT_DIR / "questions.json"))),
            data_dir=Path(e.get("DATA_DIR", str(DEFAULT_DATA_DIR))),
            with_adapter=e.get("TWITCH_WITH_ADAPTER", "false").lower() in ("1", "true", "yes"),
            overlay_enabled=e.get("OVERLAY_ENABLED", "true").lower() in ("1", "true", "yes"),
            overlay_port=int(e.get("OVERLAY_PORT", "8765")),
        )

    def validate(self) -> list[str]:
        """Return a list of missing/invalid settings (empty = ready)."""
        problems: list[str] = []
        if not self.client_id:
            problems.append("TWITCH_CLIENT_ID is missing")
        if not self.client_secret:
            problems.append("TWITCH_CLIENT_SECRET is missing")
        if not self.bot_id:
            problems.append("TWITCH_BOT_ID is missing")
        if not self.access_token:
            problems.append("TWITCH_ACCESS_TOKEN is missing (see README: one-time authorize)")
        if not self.refresh_token:
            problems.append("TWITCH_REFRESH_TOKEN is missing (see README: one-time authorize)")
        if not self.questions_path.exists():
            problems.append(f"questions file not found: {self.questions_path}")
        return problems
