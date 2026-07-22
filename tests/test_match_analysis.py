import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base, MatchAnalysisRequest, User
from app.services.match_analysis import (
    create_match_analysis_request,
    format_analysis_request_admin_text,
    format_analysis_request_user_text,
    validate_match_analysis_text,
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


def test_validate_match_analysis_text_requires_details() -> None:
    with pytest.raises(ValueError):
        validate_match_analysis_text("vs")

    assert validate_match_analysis_text("Игрок 1 vs Игрок 2, 18:00") == "Игрок 1 vs Игрок 2, 18:00"


@pytest.mark.asyncio
async def test_create_match_analysis_request_persists_payload() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        user = make_user()
        session.add(user)
        await session.flush()

        request = await create_match_analysis_request(
            session,
            user,
            "Игрок 1 vs Игрок 2",
            payment_details="Карта 0000",
            specialist_contact="@spec",
        )
        await session.commit()

        saved = await session.scalar(select(MatchAnalysisRequest))
        assert saved is not None
        assert request.id == saved.id
        assert saved.status == "new"
        assert saved.payment_details == "Карта 0000"
        assert saved.specialist_contact == "@spec"
    await engine.dispose()


def test_format_analysis_request_user_text_contains_payment_and_contact() -> None:
    request = MatchAnalysisRequest(
        id=7,
        user_id=1,
        telegram_id=111,
        username="tester",
        match_text="Игрок 1 vs Игрок 2",
        payment_details="Карта 0000",
        specialist_contact="@spec",
    )

    text = format_analysis_request_user_text(request)

    assert "#7" in text
    assert "Карта 0000" in text
    assert "@spec" in text
    assert "После оплаты" in text


def test_format_analysis_request_admin_text_contains_user_and_match() -> None:
    user = make_user()
    request = MatchAnalysisRequest(
        id=8,
        user_id=1,
        telegram_id=111,
        username="tester",
        match_text="Игрок 1 vs Игрок 2",
    )

    text = format_analysis_request_admin_text(request, user)

    assert "Новая заявка" in text
    assert "#8" in text
    assert "@tester" in text
    assert "Игрок 1 vs Игрок 2" in text
