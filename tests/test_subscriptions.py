import pytest

from app.handlers.user import subscription_plans_keyboard
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base, SubscriptionRequest, User
from app.services.bot_settings import SUBSCRIPTION_PAYMENT_DETAILS_KEY, SUBSCRIPTION_SPECIALIST_CONTACT_KEY, set_bot_setting
from app.services.subscriptions import (
    create_subscription_request,
    format_price,
    format_subscription_activation_user_text,
    format_subscription_plans_text,
    format_subscription_request_admin_text,
    format_subscription_request_user_text,
    get_subscription_plan,
)


async def make_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


def make_user() -> User:
    return User(
        telegram_id=111,
        username="tester",
        first_name="Тест",
        last_name="Игрок",
    )


def test_subscription_plans_include_new_pricing() -> None:
    vip = get_subscription_plan("vip_100")
    included = get_subscription_plan("included_30d")

    assert vip is not None
    assert vip.title == "VIP 99%"
    assert vip.signals_limit == 100
    assert vip.price_rub == 20000
    assert included is not None
    assert included.includes_vip is True
    assert included.includes_all_signals is True
    assert included.includes_analytics is True
    assert included.duration_days == 30
    assert included.price_rub == 25000


def test_format_subscription_plans_text_lists_all_groups() -> None:
    text = format_subscription_plans_text()

    assert "VIP 99%" in text
    assert "Все сигналы 95%" in text
    assert "Всё включено" in text
    assert "25 000р" in text


@pytest.mark.asyncio
async def test_create_subscription_request_persists_selected_plan() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        user = make_user()
        session.add(user)
        await session.flush()
        await set_bot_setting(session, SUBSCRIPTION_PAYMENT_DETAILS_KEY, "Подписка карта")
        await set_bot_setting(session, SUBSCRIPTION_SPECIALIST_CONTACT_KEY, "@subspec")

        request = await create_subscription_request(session, user, "all_50")
        await session.commit()

        saved = await session.scalar(select(SubscriptionRequest))
        assert saved is not None
        assert request.id == saved.id
        assert saved.status == "new"
        assert saved.plan_title == "Все сигналы 95%"
        assert saved.signals_limit == 50
        assert saved.price_rub == 7000
        assert saved.payment_details == "Подписка карта"
        assert saved.specialist_contact == "@subspec"
    await engine.dispose()


def test_format_subscription_request_texts_contain_plan_and_price() -> None:
    user = make_user()
    request = SubscriptionRequest(
        id=5,
        user_id=1,
        telegram_id=111,
        username="tester",
        plan_id="vip_10",
        plan_group="vip",
        plan_title="VIP 99%",
        plan_description="10 сигналов",
        price_rub=2500,
        signals_limit=10,
        payment_details="Карта 0000",
        specialist_contact="@spec",
    )

    assert format_price(2500) == "2 500р"
    user_text = format_subscription_request_user_text(request)
    admin_text = format_subscription_request_admin_text(request, user)

    assert "#5" in user_text
    assert "VIP 99%" in user_text
    assert "2 500р" in user_text
    assert "@spec" in user_text
    assert "Новая заявка" in admin_text
    assert "@tester" in admin_text


def test_format_subscription_activation_user_text_contains_access_terms() -> None:
    request = SubscriptionRequest(
        id=6,
        user_id=1,
        telegram_id=111,
        username="tester",
        plan_id="included_48h",
        plan_group="included",
        plan_title="Всё включено",
        plan_description="48 часов",
        price_rub=4500,
        duration_hours=48,
        includes_vip=True,
        includes_all_signals=True,
        includes_analytics=True,
    )

    text = format_subscription_activation_user_text(request)

    assert "Подписка активирована" in text
    assert "#6" in text
    assert "Всё включено" in text
    assert "48 часов" in text


def test_subscription_plans_keyboard_has_main_menu_button() -> None:
    markup = subscription_plans_keyboard()

    button = markup.inline_keyboard[-1][0]

    assert button.callback_data == "sub:main_menu"
    assert "??????? ????" in button.text
