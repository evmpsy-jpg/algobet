import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base
from app.settings import Settings
from app.services.bot_settings import (
    ANALYSIS_PAYMENT_DETAILS_KEY,
    ANALYSIS_SPECIALIST_CONTACT_KEY,
    SUBSCRIPTION_PAYMENT_DETAILS_KEY,
    SUBSCRIPTION_SPECIALIST_CONTACT_KEY,
    get_analysis_payment_config,
    get_subscription_payment_config,
    get_system_runtime_settings,
    normalize_bot_setting_value,
    set_bot_setting,
    set_system_runtime_settings,
)


async def make_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


def test_normalize_bot_setting_value_rejects_empty_and_long_values() -> None:
    with pytest.raises(ValueError):
        normalize_bot_setting_value("   ")

    with pytest.raises(ValueError):
        normalize_bot_setting_value("abcd", max_length=3)

    assert normalize_bot_setting_value("  карта 0000  ") == "карта 0000"


@pytest.mark.asyncio
async def test_analysis_payment_config_uses_saved_values() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        await set_bot_setting(session, ANALYSIS_PAYMENT_DETAILS_KEY, "Карта 0000")
        await set_bot_setting(session, ANALYSIS_SPECIALIST_CONTACT_KEY, "@spec")
        await session.commit()

        config = await get_analysis_payment_config(session)

        assert config.payment_details == "Карта 0000"
        assert config.specialist_contact == "@spec"
    await engine.dispose()


@pytest.mark.asyncio
async def test_set_bot_setting_updates_existing_value() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        first = await set_bot_setting(session, ANALYSIS_PAYMENT_DETAILS_KEY, "Первое")
        await session.commit()

        second = await set_bot_setting(session, ANALYSIS_PAYMENT_DETAILS_KEY, "Второе")
        await session.commit()

        assert second.id == first.id
        assert second.value == "Второе"
    await engine.dispose()


@pytest.mark.asyncio
async def test_subscription_payment_config_uses_separate_saved_values() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        await set_bot_setting(session, ANALYSIS_PAYMENT_DETAILS_KEY, "Анализ карта")
        await set_bot_setting(session, SUBSCRIPTION_PAYMENT_DETAILS_KEY, "Подписка карта")
        await set_bot_setting(session, SUBSCRIPTION_SPECIALIST_CONTACT_KEY, "@subspec")
        await session.commit()

        analysis_config = await get_analysis_payment_config(session)
        subscription_config = await get_subscription_payment_config(session)

        assert analysis_config.payment_details == "Анализ карта"
        assert subscription_config.payment_details == "Подписка карта"
        assert subscription_config.specialist_contact == "@subspec"
    await engine.dispose()


@pytest.mark.asyncio
async def test_system_runtime_settings_override_env_defaults() -> None:
    engine, factory = await make_session()
    settings = Settings(BOT_TOKEN="token", SQLITE_BACKUP_ENABLED=True, SQLITE_BACKUP_INTERVAL_HOURS=24, SQLITE_BACKUP_KEEP=10)
    async with factory() as session:
        initial = await get_system_runtime_settings(session, settings)
        saved = await set_system_runtime_settings(
            session,
            sqlite_backup_enabled=False,
            sqlite_backup_interval_hours=2,
            sqlite_backup_keep=5,
        )
        await session.commit()
        loaded = await get_system_runtime_settings(session, settings)

    await engine.dispose()

    assert initial.sqlite_backup_enabled is True
    assert initial.sqlite_backup_interval_hours == 24
    assert initial.sqlite_backup_keep == 10
    assert saved.sqlite_backup_enabled is False
    assert loaded.sqlite_backup_enabled is False
    assert loaded.sqlite_backup_interval_hours == 2
    assert loaded.sqlite_backup_keep == 5



def test_settings_parses_multiple_web_admin_credentials() -> None:
    settings = Settings(
        BOT_TOKEN="token",
        WEB_ADMIN_USERS="admin:secret, manager:second, broken, empty:",
    )

    assert settings.web_admin_credentials == {"admin": "secret", "manager": "second"}


def test_settings_web_admin_credentials_falls_back_to_single_admin() -> None:
    settings = Settings(
        BOT_TOKEN="token",
        WEB_ADMIN_USERNAME="admin",
        WEB_ADMIN_PASSWORD="secret",
    )

    assert settings.web_admin_credentials == {"admin": "secret"}


def test_settings_web_admin_superusers_default_to_env_admins() -> None:
    settings = Settings(
        BOT_TOKEN="token",
        WEB_ADMIN_USERS="admin:secret, manager:second",
    )

    assert settings.web_admin_superusers == {"admin", "manager"}


def test_settings_web_admin_superusers_can_be_explicit() -> None:
    settings = Settings(
        BOT_TOKEN="token",
        WEB_ADMIN_USERS="admin:secret, manager:second",
        WEB_ADMIN_SUPERUSERS="admin",
    )

    assert settings.web_admin_superusers == {"admin"}
