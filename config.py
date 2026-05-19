from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    telegram_bot_token: str
    admin_chat_id: int
    owner_name: str = "Владелец"
    sos_message: str = "🆘 ТРЕВОГА! Мне нужна помощь! Это сообщение отправлено через голосовую команду Алисе."
    host: str = "0.0.0.0"
    port: int = 8000
    alice_secret: str = ""
    contacts_file: str = "contacts.json"

    model_config = {"env_file": ".env"}


settings = Settings()
