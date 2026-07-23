from datetime import datetime

from app.database.models import MatchAnalysisRequest, SubscriptionRequest, User, UserAccess
from app.handlers.admin import format_user_detail, user_analysis_requests_keyboard, user_detail_keyboard, user_subscription_requests_keyboard, users_list_keyboard


def test_format_user_detail_shows_recent_requests() -> None:
    user = User(
        id=1,
        telegram_id=315715137,
        username="tester",
        first_name="Евгений",
        last_name="Мельников",
        is_active=True,
        created_at=datetime(2026, 7, 22, 7, 0),
    )
    access = UserAccess(
        user_id=1,
        access_type="paid",
        status="active",
        free_signals_remaining=0,
        signals_remaining=9,
        plan_id="vip_10",
    )
    subscription = SubscriptionRequest(
        id=3,
        user_id=1,
        telegram_id=315715137,
        plan_id="vip_10",
        plan_group="vip",
        plan_title="VIP 99%",
        plan_description="10 сигналов",
        price_rub=2500,
        status="done",
    )
    analysis = MatchAnalysisRequest(
        id=4,
        user_id=1,
        telegram_id=315715137,
        username="tester",
        match_text="Игрок 1 — Игрок 2",
        status="new",
        created_at=datetime(2026, 7, 22, 8, 0),
    )

    text = format_user_detail(user, access, 5, 1, [subscription], [analysis])

    assert "Евгений Мельников" in text
    assert "Тариф: vip_10" in text
    assert "Осталось платных сигналов: 9" in text
    assert "Последние заявки на подписку" in text
    assert "#3" in text
    assert "VIP 99%" in text
    assert "Последние заявки на анализ" in text
    assert "#4" in text


def callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def test_user_detail_keyboard_links_user_requests() -> None:
    markup = user_detail_keyboard(1, 2)

    assert "usr:subreq:1:2" in callbacks(markup)
    assert "usr:anreq:1:2" in callbacks(markup)


def test_user_request_keyboards_link_to_request_details() -> None:
    subscription = SubscriptionRequest(
        id=3,
        user_id=1,
        telegram_id=315715137,
        plan_id="vip_10",
        plan_group="vip",
        plan_title="VIP 99%",
        plan_description="10 сигналов",
        price_rub=2500,
        status="done",
    )
    analysis = MatchAnalysisRequest(
        id=4,
        user_id=1,
        telegram_id=315715137,
        username="tester",
        match_text="Игрок 1 — Игрок 2",
        status="new",
        created_at=datetime(2026, 7, 22, 8, 0),
    )

    sub_markup = user_subscription_requests_keyboard([subscription], user_id=1, page=2)
    an_markup = user_analysis_requests_keyboard([analysis], user_id=1, page=2)

    assert "subadm:view:3:done:0" in callbacks(sub_markup)
    assert "an:view:4:new:0" in callbacks(an_markup)
    assert "usr:view:1:2" in callbacks(sub_markup)
    assert "usr:view:1:2" in callbacks(an_markup)


def test_users_list_keyboard_has_search_button() -> None:
    user = User(id=1, telegram_id=315715137, username="tester", first_name="Евгений", last_name="Мельников")
    markup = users_list_keyboard([(user, None)], page=0, total=1)

    assert "usr:search" in callbacks(markup)
