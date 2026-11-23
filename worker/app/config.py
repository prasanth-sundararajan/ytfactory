import os


def _req(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError("missing required env: " + name)
    return value


class CFG:
    telegram_token: str = _req("TELEGRAM_BOT_TOKEN")
    owner_id: int = int(_req("TELEGRAM_OWNER_ID"))
    database_url: str = os.environ.get(
        "DATABASE_URL", "postgresql://ytf:ytf@postgres:5432/ytfactory"
    )
    redis_url: str = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    gemini_key: str = os.environ.get("GEMINI_API_KEY", "")
    groq_key: str = os.environ.get("GROQ_API_KEY", "")
    serper_key: str = os.environ.get("SERPER_API_KEY", "")
    pexels_key: str = os.environ.get("PEXELS_API_KEY", "")
    state_path: str = os.environ.get("STATE_PATH", "/app/data/state.json")
    config_path: str = os.environ.get("CONFIG_PATH", "/app/config/agents.json")

    yt_client_id: str = os.environ.get("YT_CLIENT_ID", "")
    yt_client_secret: str = os.environ.get("YT_CLIENT_SECRET", "")
    yt_refresh_token: str = os.environ.get("YT_REFRESH_TOKEN", "")


CFG = CFG()
