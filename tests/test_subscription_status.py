from datetime import datetime, timedelta

from app.database.models import User, UserAccess
from app.handlers.user import format_subscription_status


def make_user() -> User:
    return User(telegram_id=111, username=None, first_name="Test", last_name=None)


def test_format_subscription_status_without_access_suggests_trial() -> None:
    text = format_subscription_status(None, None)

    assert "Активного доступа пока нет" in text
    assert "Первые 9 сигналов" in text


def test_format_subscription_status_shows_trial_remaining() -> None:
    user = make_user()
    access = UserAccess(user_id=1, access_type="trial", status="active", free_signals_remaining=2)

    text = format_subscription_status(user, access)

    assert "Тип: пробный доступ" in text
    assert "Осталось бесплатных сигналов: 2" in text
    assert "Статус: активна" in text


def test_format_subscription_status_shows_paid_until_date() -> None:
    user = make_user()
    access = UserAccess(
        user_id=1,
        access_type="paid",
        status="active",
        free_signals_remaining=0,
        active_until=datetime.utcnow() + timedelta(days=30),
    )

    text = format_subscription_status(user, access)

    assert "Тип: платный доступ" in text
    assert "Активен до:" in text


def test_format_subscription_status_for_admin() -> None:
    text = format_subscription_status(make_user(), None, is_admin=True)

    assert "админ-доступ" in text
    assert "без ограничений" in text
