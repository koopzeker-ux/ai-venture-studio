from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    database_url: str = "postgresql+psycopg://venture:change_me@db:5432/venture_studio"
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    rss_feed_urls: str = "https://www.producthunt.com/feed"
    hackernews_enabled: bool = True
    rss_enabled: bool = True

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
