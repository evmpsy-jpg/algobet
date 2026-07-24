from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import (
    Base,
    ImportBatch,
    Match,
    MatchAnalysisRequest,
    ScheduledSignal,
    SignalDelivery,
    SignalResult,
    SubscriptionRequest,
    User,
    UserAccess,
)
from app.services.dashboard import collect_dashboard_summary, collect_delivery_list, collect_maintenance_summary, collect_request_detail, collect_request_list, collect_signal_detail, collect_signal_list, collect_user_detail, collect_user_list


@pytest.mark.asyncio
async def test_collect_dashboard_summary_counts_core_entities() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        user = User(
            telegram_id=1001,
            username="paid_user",
            first_name="Paid",
            last_name="User",
            is_active=True,
        )
        inactive_user = User(
            telegram_id=1002,
            username="inactive_user",
            first_name="Inactive",
            last_name="User",
            is_active=False,
        )
        session.add_all([user, inactive_user])
        await session.flush()

        session.add_all(
            [
                UserAccess(user_id=user.id, access_type="paid", status="active", free_signals_remaining=0),
                UserAccess(user_id=inactive_user.id, access_type="trial", status="active", free_signals_remaining=2),
            ]
        )

        batch = ImportBatch(
            file_name="sample.xlsx",
            stored_path="uploads/sample.xlsx",
            file_sha256="abc",
            uploaded_by_telegram_id=315715137,
            status="completed",
            total_rows=10,
            parsed_matches=8,
            inserted_matches=7,
            updated_matches=1,
            missing_matches=2,
            created_at=datetime(2026, 7, 24, 10, 0),
            finished_at=datetime(2026, 7, 24, 10, 1),
        )
        session.add(batch)
        await session.flush()

        match = Match(
            external_match_id=501,
            external_tournament_id=9001,
            source_url="https://example.test/9001/501",
            tournament_date="24.07.2026",
            match_time="12:30",
            match_start_at=datetime(2026, 7, 24, 12, 30),
            player_1="Player 1",
            player_2="Player 2",
            player_1_rating=None,
            player_2_rating=None,
            score=None,
            raw_data={},
            current_import_id=batch.id,
            is_present_in_latest_import=True,
        )
        session.add(match)
        await session.flush()

        signal = ScheduledSignal(
            match_id=match.id,
            status="sent",
            send_at=datetime(2026, 7, 24, 12, 10),
            signal_type="SET_VIP_TOP",
            signal_payload={"signal_group": "vip", "level": "TOP", "side": 1},
            message_text="signal",
            source_import_id=batch.id,
        )
        session.add(signal)
        await session.flush()

        session.add_all(
            [
                SignalDelivery(signal_id=signal.id, user_id=user.id, telegram_id=user.telegram_id, status="sent"),
                SignalDelivery(signal_id=signal.id, user_id=inactive_user.id, telegram_id=inactive_user.telegram_id, status="failed", error_text="telegram unavailable"),
                SignalResult(signal_id=signal.id, status="won", source="auto"),
                SubscriptionRequest(
                    user_id=user.id,
                    telegram_id=user.telegram_id,
                    username=user.username,
                    plan_id="vip_10",
                    plan_group="vip",
                    plan_title="VIP 99%",
                    plan_description="10 signals",
                    price_rub=2500,
                    signals_limit=10,
                    duration_days=None,
                    duration_hours=None,
                    includes_vip=True,
                    includes_all_signals=False,
                    includes_analytics=False,
                    status="new",
                ),
                MatchAnalysisRequest(
                    user_id=user.id,
                    telegram_id=user.telegram_id,
                    username=user.username,
                    match_text="Player 1 vs Player 2",
                    status="new",
                ),
            ]
        )
        await session.commit()

        summary = await collect_dashboard_summary(session)
        signals = await collect_signal_list(session)
        users = await collect_user_list(session)
        requests = await collect_request_list(session)
        deliveries = await collect_delivery_list(session)
        failed_deliveries = await collect_delivery_list(session, status_filter="failed")
        signal_detail = await collect_signal_detail(session, signal.id)
        user_detail = await collect_user_detail(session, user.id)
        subscription_detail = await collect_request_detail(session, "subscription", 1)
        analysis_detail = await collect_request_detail(session, "analysis", 1)

    await engine.dispose()

    assert summary.users_total == 2
    assert summary.users_active == 1
    assert summary.access_paid_active == 1
    assert summary.access_trial_active == 1
    assert summary.matches_total == 1
    assert summary.matches_active == 1
    assert summary.imports_total == 1
    assert summary.signals_total == 1
    assert summary.signals_by_status == {"sent": 1}
    assert summary.deliveries_total == 2
    assert summary.deliveries_by_status == {"failed": 1, "sent": 1}
    assert summary.results_by_status == {"won": 1}
    assert summary.open_subscription_requests == 1
    assert summary.open_analysis_requests == 1
    assert summary.latest_import is not None
    assert summary.latest_import.file_name == "sample.xlsx"
    assert summary.latest_import.parsed_matches == 8
    assert len(summary.recent_signals) == 1
    assert summary.recent_signals[0].signal_group == "vip"
    assert summary.recent_signals[0].result_status == "won"

    assert len(signals) == 1
    assert signals[0].sent_deliveries == 1
    assert signals[0].failed_deliveries == 1
    assert len(deliveries) == 2
    assert deliveries[0].status == "failed"
    assert deliveries[0].error_text == "telegram unavailable"
    assert deliveries[0].match_title == "Player 1 - Player 2"
    assert len(failed_deliveries) == 1
    assert failed_deliveries[0].telegram_id == inactive_user.telegram_id

    assert len(users) == 2
    assert users[0].telegram_id == 1002
    assert users[0].failed_deliveries == 1
    assert users[1].sent_deliveries == 1

    assert [item.kind for item in requests] == ["subscription", "analysis"]
    assert signal_detail is not None
    assert signal_detail.item.id == signal.id
    assert signal_detail.external_match_id == 501
    assert len(signal_detail.deliveries) == 2
    assert user_detail is not None
    assert user_detail.item.telegram_id == 1001
    assert len(user_detail.deliveries) == 1
    assert [item.kind for item in user_detail.requests] == ["subscription", "analysis"]
    assert subscription_detail is not None
    assert subscription_detail.item.kind == "subscription"
    assert analysis_detail is not None
    assert analysis_detail.item.kind == "analysis"


@pytest.mark.asyncio
async def test_collect_dashboard_summary_handles_empty_database() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        summary = await collect_dashboard_summary(session)

    await engine.dispose()

    assert summary.users_total == 0
    assert summary.signals_total == 0
    assert summary.deliveries_total == 0
    assert summary.latest_import is None
    assert summary.recent_signals == []



def test_collect_maintenance_summary_shows_storage_and_backup_settings(tmp_path) -> None:
    data_dir = tmp_path / "data"
    uploads_dir = tmp_path / "uploads"
    data_dir.mkdir()
    uploads_dir.mkdir()
    db_path = data_dir / "algobet.db"
    db_path.write_bytes(b"database")
    (uploads_dir / "upload.xlsx").write_bytes(b"upload")

    summary = collect_maintenance_summary(
        SimpleNamespace(
            database_url=f"sqlite+aiosqlite:///{db_path}",
            data_dir=data_dir,
            uploads_dir=uploads_dir,
            sqlite_backup_enabled=True,
            sqlite_backup_interval_hours=24,
            sqlite_backup_keep=10,
        )
    )

    assert summary.database_path == str(db_path)
    assert summary.database_size_bytes == len(b"database")
    assert summary.data_size_bytes >= len(b"database")
    assert summary.uploads_size_bytes == len(b"upload")
    assert summary.sqlite_backup_enabled is True
    assert summary.latest_backup_path is None

