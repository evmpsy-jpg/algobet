from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base, PlayerBirthday
from app.services.player_birthdays import PlayerBirthdayRow, parse_player_birthdays_csv, parse_player_birthdays_html, upsert_player_birthdays
from app.web_admin import render_player_birthdays_csv, render_player_birthdays_html


@pytest.mark.asyncio
async def test_upsert_player_birthdays_inserts_and_updates_rows() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        first = await upsert_player_birthdays(session, [
            PlayerBirthdayRow(
                full_name="Иванов Иван Иванович",
                birth_date=date(1990, 2, 1),
                source_url="https://example.test/players/123",
                external_player_id=123,
            )
        ])
        second = await upsert_player_birthdays(session, [
            PlayerBirthdayRow(
                full_name="Иванов Иван Иванович",
                birth_date=date(1991, 3, 2),
                source_url="https://example.test/players/123",
                external_player_id=123,
            )
        ])
        rows = list((await session.scalars(select(PlayerBirthday))).all())

    await engine.dispose()

    assert first.inserted == 1
    assert second.updated == 1
    assert len(rows) == 1
    assert rows[0].birth_date == date(1991, 3, 2)
    assert rows[0].short_name == "Иванов И. И."



def test_parse_player_birthdays_csv_accepts_russian_headers() -> None:
    rows = parse_player_birthdays_csv(
        "Игрок;Дата рождения;Ссылка;ID игрока\n"
        "Петров Петр Петрович;03.04.1992;https://example.test/players/456;456\n"
    )

    assert rows == [
        PlayerBirthdayRow(
            full_name="Петров Петр Петрович",
            birth_date=date(1992, 4, 3),
            source_url="https://example.test/players/456",
            external_player_id=456,
        )
    ]


def test_parse_player_birthdays_html_extracts_rows() -> None:
    html = """
    <table><tr>
      <td><a href="/ru/table-tennis/participants/players/123">Иванов Иван Иванович</a></td>
      <td>01.02.1990</td>
    </tr></table>
    """

    rows = parse_player_birthdays_html(html, base_url="https://www.sport-liga.pro/ru/table-tennis/participants/players")

    assert rows == [
        PlayerBirthdayRow(
            full_name="Иванов Иван Иванович",
            birth_date=date(1990, 2, 1),
            source_url="https://www.sport-liga.pro/ru/table-tennis/participants/players/123",
            external_player_id=123,
        )
    ]


def test_parse_player_birthdays_html_reports_servicepipe_challenge() -> None:
    with pytest.raises(ValueError, match="ServicePipe"):
        parse_player_birthdays_html("<js-challenge-loader></js-challenge-loader><script src='servicepipe.tech/x.js'></script>")


def test_render_player_birthdays_html_and_csv_are_admin_friendly() -> None:
    player = PlayerBirthday(
        id=1,
        external_player_id=123,
        full_name="Иванов Иван Иванович",
        short_name="Иванов И. И.",
        birth_date=date(1990, 2, 1),
        source_url="https://example.test/players/123",
    )

    html = render_player_birthdays_html([player], search="Иванов", message="Обновлено")
    csv_text = render_player_birthdays_csv([player])

    assert "Даты рождения игроков" in html
    assert "Иванов Иван Иванович" in html
    assert "01.02.1990" in html
    assert "Обновить с Sport Liga Pro" in html
    assert "Импортировать список" in html
    assert csv_text.splitlines()[0] == "id,external_player_id,full_name,short_name,birth_date,source_url,last_synced_at"
    assert "1,123,Иванов Иван Иванович,Иванов И. И.,1990-02-01,https://example.test/players/123,-" in csv_text
