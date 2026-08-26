from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    database_url: str = "postgresql+psycopg://venture:change_me@db:5432/venture_studio"
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    rss_feed_urls: str = "https://www.producthunt.com/feed"
    hackernews_enabled: bool = True
    rss_enabled: bool = True

    # M3.4: small, explicit, auditable subreddit list -- comma-separated
    # subreddit names (no "r/" prefix, e.g. "smallbusiness,SaaS"). Empty by
    # default: LEAD sets this explicitly before enabling live discovery.
    # Disabled by default so BUILDER's slice ships inert -- LEAD performs
    # the bounded live dogfood after REVIEWER approval, never BUILDER.
    reddit_enabled: bool = False
    reddit_subreddits: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
