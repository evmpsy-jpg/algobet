from app.handlers.user import format_help_information


def test_format_help_information_mentions_core_user_sections_and_risk() -> None:
    text = format_help_information()

    assert "📚 Полезная информация" in text
    assert "📊 Аналитика турниров" in text
    assert "💳 Подписка" in text
    assert "🏆 Результаты" in text
    assert "🎁 Первые 3 сигнала" in text
    assert "не являются гарантией" in text
