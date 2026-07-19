from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def main_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="📊 Аналитика турниров")],
        [KeyboardButton(text="🎁 Первые 3 сигнала"), KeyboardButton(text="💳 Подписка")],
        [KeyboardButton(text="📚 Полезная информация"), KeyboardButton(text="🏆 Результаты")],
        [KeyboardButton(text="🔎 Анализ матча"), KeyboardButton(text="🧮 Калькулятор")],
    ]
    if is_admin:
        rows.append([KeyboardButton(text="⚙️ Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def admin_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📥 Импорт Excel"), KeyboardButton(text="📊 Сигналы")],
            [KeyboardButton(text="📤 История отправок"), KeyboardButton(text="📈 Статистика")],
            [KeyboardButton(text="👥 Пользователи"), KeyboardButton(text="⚙️ Настройки")],
            [KeyboardButton(text="📋 Последняя загрузка")],
            [KeyboardButton(text="⬅️ Главное меню")],
        ],
        resize_keyboard=True,
    )
