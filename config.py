from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    telegram_bot_token: str
    base_url: str
    host: str = "0.0.0.0"
    port: int = 8000
    db_path: str = "sos.db"
    admin_chat_id: int | None = None
    max_bot_token: str | None = None
    backup_interval_hours: int = 0          # 0 = auto-backup disabled
    backup_passphrase: str | None = None    # if set, backups are encrypted
    # SMTP — enables the e-mail "Телефон" bridge (iPhone email automation)
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_ssl: bool = False                   # True = implicit TLS (port 465)

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
