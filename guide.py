"""Shared, platform-neutral instruction texts.

Plain text only (no HTML/Markdown) so the same string renders correctly in
both Telegram and MAX messages.
"""


def alice_setup_instructions() -> str:
    """Short step-by-step guide for connecting the Alice skill.

    Assumes the owner's personal Webhook URL is shown right above this text
    in the same message.
    """
    return (
        "📋 Как подключить навык Алисы (Webhook URL — выше):\n\n"
        "1) Откройте dialogs.yandex.ru/developer и войдите своим Яндекс-аккаунтом.\n"
        "2) «Создать диалог» → «Навык в Алисе».\n"
        "3) Активационное имя — 2–3 узнаваемых слова, например «сос помощник» "
        "(Алиса плохо распознаёт буквы С-О-С по отдельности).\n"
        "4) В блоке Backend выберите «Свой навык / Webhook URL» и вставьте ваш "
        "Webhook URL целиком (вместе с токеном в конце).\n"
        "5) Сохраните → вкладка «Тестирование» → скажите «открой» и ваше "
        "активационное имя. Навык спросит, кому отправить SOS.\n"
        "6) Чтобы навыком могли пользоваться другие — отправьте его на модерацию "
        "(1–3 рабочих дня). Для себя достаточно режима тестирования.\n\n"
        "Требования: Webhook URL должен быть на HTTPS и доступен из интернета. "
        "Подробный гайд — в README проекта, раздел «Настройка навыка Алисы по шагам»."
    )
