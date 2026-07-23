from datetime import datetime

from app.database.models import SubscriptionRequest, User
from app.handlers.admin import (
    format_subscription_request_detail,
    format_subscription_requests_dashboard_text,
    subscription_request_detail_keyboard,
    subscription_requests_dashboard_keyboard,
    subscription_requests_list_keyboard,
)


def make_request() -> SubscriptionRequest:
    return SubscriptionRequest(
        id=9,
        user_id=1,
        telegram_id=315715137,
        username="tester",
        plan_id="included_10d",
        plan_group="included",
        plan_title="Всё включено",
        plan_description="10 дней",
        price_rub=10000,
        duration_days=10,
        includes_vip=True,
        includes_all_signals=True,
        includes_analytics=True,
        status="new",
        payment_details="Карта 0000",
        specialist_contact="@spec",
        created_at=datetime(2026, 7, 22, 7, 0),
        updated_at=datetime(2026, 7, 22, 7, 5),
    )


def make_user() -> User:
    return User(
        id=1,
        telegram_id=315715137,
        username="tester",
        first_name="Евгений",
        last_name="Мельников",
    )


def callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def test_subscription_requests_dashboard_keyboard_links_statuses() -> None:
    markup = subscription_requests_dashboard_keyboard({"new": 2})

    assert "subadm:list:new:0" in callbacks(markup)
    assert "subadm:list:done:0" in callbacks(markup)
    assert "subadm:dashboard" in callbacks(markup)


def test_subscription_requests_list_keyboard_links_detail() -> None:
    markup = subscription_requests_list_keyboard([(make_request(), make_user())], "new", page=0, total=1)

    assert "subadm:view:9:new:0" in callbacks(markup)
    assert "subadm:dashboard" in callbacks(markup)


def test_subscription_request_detail_keyboard_changes_status() -> None:
    markup = subscription_request_detail_keyboard(9, current_status="new", list_status="new", page=0)

    assert "subadm:activate:9:new:0" in callbacks(markup)
    assert "subadm:status:9:paid:new:0" in callbacks(markup)
    assert "subadm:status:9:done:new:0" in callbacks(markup)
    assert "subadm:status:9:new:new:0" not in callbacks(markup)


def test_format_subscription_request_detail_contains_plan_and_user() -> None:
    text = format_subscription_request_detail(make_request(), make_user())

    assert "Заявка на подписку #9" in text
    assert "Евгений Мельников" in text
    assert "Всё включено" in text
    assert "10 000р" in text
    assert "VIP, все сигналы, аналитика" in text


def test_subscription_request_detail_keyboard_hides_quick_activation_for_done_request() -> None:
    markup = subscription_request_detail_keyboard(9, current_status="done", list_status="done", page=0)

    assert "subadm:activate:9:done:0" not in callbacks(markup)


def test_format_subscription_requests_dashboard_text_shows_amounts() -> None:
    text = format_subscription_requests_dashboard_text(
        {"new": 2, "paid": 1, "done": 1, "cancelled": 1},
        {"new": 4000, "paid": 2500, "done": 10000, "cancelled": 1500},
    )

    assert "Всего: 5" in text
    assert "Новые: 2 · 4 000р" in text
    assert "Оплачено + обработано: 12 500р" in text
