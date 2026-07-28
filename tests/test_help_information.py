from app.handlers.user import WELCOME_MESSAGES, format_help_information


def test_format_help_information_mentions_core_user_sections_and_risk() -> None:
    text = format_help_information()

    assert "📚 Полезная информация" in text
    assert "📊 Аналитика турниров" in text
    assert "💳 Подписка" in text
    assert "🏆 Результаты" in text
    assert "🎁 Первые 9 сигналов" in text
    assert "не являются гарантией" in text


def test_welcome_messages_introduce_product_before_menu() -> None:
    text = "\n".join(WELCOME_MESSAGES)

    assert "Вас приветствует бот Алгобет" in text
    assert "настольному теннису Лиги Про" in text
    assert "аналитику турниров 24/7" in text
    assert "технический этап" not in text
