from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import Base, Match, ScheduledSignal, SignalResult
from app.services.signal_results import auto_set_signal_result, auto_update_signal_results, format_winrate, infer_signal_result_status, result_full_label, result_source_label, set_signal_result, summarize_results


async def make_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


def make_match() -> Match:
    return Match(
        external_match_id=501,
        external_tournament_id=9001,
        source_url="https://example.test/tournaments/9001/501",
        tournament_date="22.07.2026",
        match_time="15:00",
        match_start_at=datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc),
        player_1="Player 1",
        player_2="Player 2",
        player_1_rating=None,
        player_2_rating=None,
        score=None,
        raw_data={},
    )


@pytest.mark.asyncio
async def test_set_signal_result_creates_and_updates_result() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        match = make_match()
        session.add(match)
        await session.flush()
        signal = ScheduledSignal(
            match_id=match.id,
            status="sent",
            send_at=datetime(2026, 7, 22, 11, 45, tzinfo=timezone.utc),
            signal_payload={"level": "TOP"},
            message_text="signal",
        )
        session.add(signal)
        await session.commit()

        result = await set_signal_result(session, signal, "won", fixed_by_telegram_id=315715137)
        assert result.status == "won"
        assert result.fixed_by_telegram_id == 315715137
        assert result.fixed_at is not None

        result = await set_signal_result(session, signal, "lost", fixed_by_telegram_id=315715137)
        rows = list((await session.execute(select(SignalResult))).scalars())
        assert len(rows) == 1
        assert result.id == rows[0].id
        assert rows[0].status == "lost"
    await engine.dispose()


def test_summarize_results_counts_overall_by_group_and_by_level() -> None:
    summary = summarize_results(
        [
            ({"signal_group": "vip", "level": "TOP", "probability": 99}, "won"),
            ({"signal_group": "vip", "level": "TOP", "probability": 98}, "lost"),
            ({"signal_group": "all", "level": "STRONG", "probability": 95}, "won"),
            ({"signal_group": "all", "level": "STANDARD", "probability": 80}, "void"),
            ({"level": "STANDARD"}, None),
        ],
        total_sent=5,
    )

    assert summary.total_sent == 5
    assert summary.evaluated == 5
    assert summary.unrated_sent == 0
    assert summary.overall.won == 2
    assert summary.overall.lost == 1
    assert summary.overall.void == 1
    assert summary.overall.unknown == 1
    assert format_winrate(summary.overall.winrate) == "66.7%"
    assert summary.by_group["vip"].won == 1
    assert summary.by_group["vip"].lost == 1
    assert summary.by_group["all"].won == 1
    assert summary.by_group["all"].void == 1
    assert summary.by_group["unknown"].unknown == 1
    assert summary.by_tariff["vip_99"].won == 1
    assert summary.by_tariff["vip_99"].lost == 1
    assert summary.by_tariff["all_95"].won == 1
    assert summary.by_tariff["all_95"].void == 1
    assert summary.by_tariff["unknown"].unknown == 1
    assert summary.by_level["TOP"].won == 1
    assert summary.by_level["TOP"].lost == 1
    assert format_winrate(summary.by_level["TOP"].winrate) == "50.0%"

def test_summarize_results_reports_unrated_sent_signals() -> None:
    summary = summarize_results(
        [
            ({"level": "TOP"}, "won"),
            ({"level": "STRONG"}, "lost"),
        ],
        total_sent=5,
    )

    assert summary.evaluated == 2
    assert summary.unrated_sent == 3

def test_infer_signal_result_status_from_match_score() -> None:
    assert infer_signal_result_status("3:1", 1) == "won"
    assert infer_signal_result_status("3:1", 2) == "won"
    assert infer_signal_result_status("8:11 11:8 9:11 11:7 8:11", 1) == "won"
    assert infer_signal_result_status("8:11 11:8 9:11 11:7 8:11", 2) == "won"
    assert infer_signal_result_status("11:8 11:9 11:7", 2) == "lost"
    assert infer_signal_result_status("-:-", 1) is None
    assert infer_signal_result_status(None, 1) is None
    assert infer_signal_result_status("11:11", 1) == "void"


@pytest.mark.asyncio
async def test_auto_set_signal_result_uses_score_without_overwriting_manual_result() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        match = make_match()
        match.score = "3:1"
        session.add(match)
        await session.flush()
        signal = ScheduledSignal(
            match_id=match.id,
            status="sent",
            send_at=datetime(2026, 7, 22, 11, 45, tzinfo=timezone.utc),
            signal_payload={"level": "TOP", "side": 1},
            message_text="signal",
        )
        session.add(signal)
        await session.commit()

        result = await auto_set_signal_result(session, signal, match)
        await session.commit()
        assert result is not None
        assert result.status == "won"
        assert result.source == "auto"

        result = await set_signal_result(session, signal, "lost", fixed_by_telegram_id=315715137)
        match.score = "3:0"
        ignored = await auto_set_signal_result(session, signal, match)
        await session.commit()

        assert ignored is None
        assert result.status == "lost"
        assert result.source == "manual"
    await engine.dispose()

def test_result_labels_include_source() -> None:
    assert result_source_label("auto") == "авто"
    assert result_source_label("manual") == "вручную"
    assert result_full_label(SignalResult(signal_id=1, status="won", source="auto")) == "✅ Зашёл · авто"
    assert result_full_label(None) == "❔ Неизвестно"

@pytest.mark.asyncio
async def test_auto_update_signal_results_summarizes_bulk_run() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        won_match = make_match()
        won_match.external_match_id = 601
        won_match.score = "3:1"
        manual_match = make_match()
        manual_match.external_match_id = 602
        manual_match.score = "0:3"
        no_score_match = make_match()
        no_score_match.external_match_id = 603
        no_score_match.score = "-:-"
        session.add_all([won_match, manual_match, no_score_match])
        await session.flush()

        won_signal = ScheduledSignal(
            match_id=won_match.id,
            status="sent",
            send_at=datetime(2026, 7, 22, 11, 45, tzinfo=timezone.utc),
            signal_payload={"side": 1},
            message_text="won",
        )
        manual_signal = ScheduledSignal(
            match_id=manual_match.id,
            status="sent",
            send_at=datetime(2026, 7, 22, 11, 46, tzinfo=timezone.utc),
            signal_payload={"side": 2},
            message_text="manual",
        )
        no_score_signal = ScheduledSignal(
            match_id=no_score_match.id,
            status="sent",
            send_at=datetime(2026, 7, 22, 11, 47, tzinfo=timezone.utc),
            signal_payload={"side": 1},
            message_text="no score",
        )
        session.add_all([won_signal, manual_signal, no_score_signal])
        await session.flush()
        session.add(SignalResult(signal_id=manual_signal.id, status="lost", source="manual"))
        await session.commit()

        summary = await auto_update_signal_results(session)
        await session.commit()

        assert summary.scanned == 3
        assert summary.updated == 1
        assert summary.skipped_manual == 1
        assert summary.no_score == 1
        result = await session.scalar(select(SignalResult).where(SignalResult.signal_id == won_signal.id))
        assert result is not None
        assert result.status == "won"
        assert result.source == "auto"
    await engine.dispose()
