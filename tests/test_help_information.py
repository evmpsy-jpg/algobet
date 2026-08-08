from app.handlers.user import WELCOME_MESSAGES, format_help_information


def test_format_help_information_keeps_old_help_and_adds_distance_system() -> None:
    text = format_help_information()

    assert "📚 <b>Полезная информация</b>" in text
    assert "<b>Как работает бот:</b>" in text
    assert "📊 Аналитика турниров" in text
    assert "💳 Подписка" in text
    assert "🏆 Результаты" in text
    assert "🎁 Первые 9 сигналов" in text
    assert "не являются гарантией" in text

    assert "📘 <b>СИСТЕМА НА ДИСТАНЦИЮ</b>" in text
    assert "<b>Стратегия «На сет»</b>" in text
    assert "<b>Только один сет, до первой победы.</b>" in text
    assert "<b>Работа по сигналу (Цикл из 3 шагов)</b>" in text
    assert "<b>Защитный режим</b>" in text
    assert "<b>⛔️ Табу:</b>" in text
    assert len(text) <= 4096

def test_welcome_messages_introduce_product_before_menu() -> None:
    text = "\n".join(WELCOME_MESSAGES)

    assert "Вас приветствует бот Алгобет" in text
    assert "настольному теннису Лиги Про" in text
    assert "аналитику турниров 24/7" in text
    assert "технический этап" not in text
