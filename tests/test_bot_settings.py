import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base
from app.services.bot_settings import (
    ANALYSIS_PAYMENT_DETAILS_KEY,
    ANALYSIS_SPECIALIST_CONTACT_KEY,
    get_analysis_payment_config,
    normalize_bot_setting_value,
    set_bot_setting,
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
