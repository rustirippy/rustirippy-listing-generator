"""Runtime settings, read from environment variables (see .env.example)."""

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader so the app runs without extra dependencies."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    etsy_api_key: str = field(default_factory=lambda: os.getenv("ETSY_API_KEY", ""))
    etsy_shared_secret: str = field(default_factory=lambda: os.getenv("ETSY_SHARED_SECRET", ""))
    etsy_redirect_uri: str = field(
        default_factory=lambda: os.getenv("ETSY_REDIRECT_URI", "http://localhost:8000/oauth/callback")
    )
    etsy_api_base: str = field(default_factory=lambda: os.getenv("ETSY_API_BASE", "https://api.etsy.com/v3"))
    etsy_scopes: str = field(
        default_factory=lambda: os.getenv("ETSY_SCOPES", "listings_r listings_w shops_r")
    )
    claude_model: str = field(default_factory=lambda: os.getenv("CLAUDE_MODEL", "claude-opus-5-5"))
    claude_effort: str = field(default_factory=lambda: os.getenv("CLAUDE_EFFORT", "medium"))
    database_path: str = field(default_factory=lambda: os.getenv("DATABASE_PATH", "data/app.db"))
    upload_dir: str = field(default_factory=lambda: os.getenv("UPLOAD_DIR", "data/uploads"))
    sync_interval_minutes: int = field(default_factory=lambda: int(os.getenv("SYNC_INTERVAL_MINUTES", "15")))
    auto_optimize: bool = field(default_factory=lambda: _bool("AUTO_OPTIMIZE", False))
    auto_optimize_below_score: int = field(
        default_factory=lambda: int(os.getenv("AUTO_OPTIMIZE_BELOW_SCORE", "70"))
    )
    app_password: str = field(default_factory=lambda: os.getenv("APP_PASSWORD", ""))
    secret_key: str = field(default_factory=lambda: os.getenv("SECRET_KEY", "change-me"))
    background_sync: bool = field(default_factory=lambda: _bool("BACKGROUND_SYNC", True))

    @property
    def etsy_configured(self) -> bool:
        return bool(self.etsy_api_key)

    @property
    def claude_configured(self) -> bool:
        return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))

    @property
    def api_key_header(self) -> str:
        # Etsy expects "keystring:shared_secret" in x-api-key; older apps used the keystring alone.
        if self.etsy_shared_secret:
            return f"{self.etsy_api_key}:{self.etsy_shared_secret}"
        return self.etsy_api_key


def load_settings() -> Settings:
    _load_dotenv()
    return Settings()
