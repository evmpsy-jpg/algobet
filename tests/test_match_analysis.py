from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base, Match, MatchAnalysisRequest, User
from app.services.match_analysis import (
    build_match_analysis_text,
    create_match_analysis_request,
    format_analysis_request_admin_text,
    format_analysis_request_user_text,
    format_analysis_status_user_text,
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
            Match(
                id=22,
                external_match_id=1001,
                external_tournament_id=2001,
                source_url="https://example.test/t/2001/1001",
                tournament_date="11.08.2026",
                match_time="19:30",
                match_start_at=datetime(2026, 8, 11, 19, 30),
                player_1="Игрок 1",
                player_2="Игрок 2",
                player_1_rating=None,
                player_2_rating=None,
                score=None,
                raw_data={},
            ),
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


def test_format_analysis_status_user_text_contains_status_message() -> None:
    request = MatchAnalysisRequest(
        id=9,
        user_id=1,
        telegram_id=111,
        username="tester",
        match_text="Игрок 1 vs Игрок 2",
        status="in_progress",
        specialist_contact="@spec",
    )

    text = format_analysis_status_user_text(request)

    assert "#9" in text
    assert "готовится" in text


def test_build_match_analysis_text_uses_html_link_and_escapes_favorite() -> None:
    match = Match(
        external_match_id=701431,
        external_tournament_id=76164,
        source_url="https://www.sport-liga.pro/ru/table-tennis/tournaments/76164/701431",
        tournament_date="11.08.2026",
        match_time="19:30",
        match_start_at=datetime(2026, 8, 11, 19, 30),
        player_1="Тяпухин <В. А.>",
        player_2="Шкурко А. О.",
        player_1_rating=623,
        player_2_rating=611,
        score=None,
        raw_data={
            "_tournament_name": "Турнир A5. Лига 600-700",
            "D": 6,
            "CP": 5,
            "Q": 7,
            "X": 7,
            "EJ": 49,
            "EK": 42,
            "CV": 63,
            "CW": 16,
            "DG": 6,
            "DH": 0,
            "AA": 3,
            "AB": 2,
            "AI": 1.2,
            "EF": 2.4,
        },
    )

    text = build_match_analysis_text(match)

    assert '<a href="https://www.sport-liga.pro/ru/table-tennis/tournaments/76164/701431">' in text
    assert '&lt;В. А.&gt;' in text
    assert '<a href=' in text