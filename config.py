from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    support_bot_token: str = ""

    # NB: live deployment double-prefixes routes - this must be /api/api, not /api
    # (confirmed empirically: .../api/api/v2/check/ping -> 200, .../api/v2/... -> 404)
    printbox_api_base_url: str = "https://nurtest.space/api/api"
    printbox_admin_login: str = ""
    printbox_admin_password: str = ""
    printbox_admin_token: str = ""

    openai_api_key: str = ""
    openai_model: str = "gpt-4o"

    support_staff_chat_id: int = 0

    # Comma-separated telegram_ids - these users see an admin menu in the bot
    # instead of the regular user one. Plain string (not list[str]) because
    # pydantic-settings expects list-typed env vars to be JSON, not CSV.
    admin_telegram_ids: str = ""

    # Above this amount the staff card gets a "look closer" warning. It no longer
    # gates anything automatic - a human approves every refund either way.
    refund_review_amount_cap: float = 1000
    neighbor_window: int = 5
    print_signal_window_minutes: int = 5
    session_timeout_minutes: int = 10

    support_bot_db_path: str = "./support_bot.sqlite3"


settings = Settings()
