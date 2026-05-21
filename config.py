from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    telegram_bot_token: str
    base_url: str
    host: str = "0.0.0.0"
    port: int = 8000
    db_path: str = "sos.db"

    model_config = {"env_file": ".env"}


settings = Settings()
