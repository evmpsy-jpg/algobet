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
    SignalDecisionLog,
    SignalResult,
    SubscriptionRequest,
    User,
    UserAccess,
    WebAdminActionLog,
)
from app.services.dashboard import DashboardSummary, ImportListItem, build_system_health_checks, collect_dashboard_summary, collect_delivery_list, collect_import_detail, collect_import_signal_schedule_warnings, collect_maintenance_summary, collect_monitoring_summary, collect_quality_summary, collect_request_detail, collect_request_list, collect_signal_detail, collect_signal_list, collect_user_detail, collect_user_list


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
                WebAdminActionLog(
                    actor_username="admin",
                    action="request_status_update",
                    target_type="subscription_request",
                    target_id="1",
                    details={"old_status": "new", "new_status": "done"},
                ),
            ]
        )
        await session.commit()

        summary = await collect_dashboard_summary(session)
        signals = await collect_signal_list(session)
        won_signals = await collect_signal_list(session, result_filter="won")
        unrated_signals = await collect_signal_list(session, result_filter="unrated")
        users = await collect_user_list(session)
        users_by_id = await collect_user_list(session, search="1001")
        users_by_username = await collect_user_list(session, search="inactive")
        users_by_missing = await collect_user_list(session, search="missing")
        paid_users = await collect_user_list(session, access_type_filter="paid", access_status_filter="active")
        trial_users = await collect_user_list(session, access_type_filter="trial", access_status_filter="active")
        requests = await collect_request_list(session)
        subscription_requests = await collect_request_list(session, kind_filter="subscription")
        analysis_requests = await collect_request_list(session, kind_filter="analysis")
        new_requests = await collect_request_list(session, status_filter="new")
        done_requests = await collect_request_list(session, status_filter="done")
        deliveries = await collect_delivery_list(session)
        failed_deliveries = await collect_delivery_list(session, status_filter="failed")
        searched_deliveries = await collect_delivery_list(session, search="paid_user")
        signal_deliveries = await collect_delivery_list(session, signal_id=signal.id)
        user_deliveries = await collect_delivery_list(session, user_id=user.id)
        signal_detail = await collect_signal_detail(session, signal.id)
        user_detail = await collect_user_detail(session, user.id)
        subscription_detail = await collect_request_detail(session, "subscription", 1)
        analysis_detail = await collect_request_detail(session, "analysis", 1)
        monitoring = await collect_monitoring_summary(session)

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
    assert len(won_signals) == 1
    assert won_signals[0].result_status == "won"
    assert unrated_signals == []
    assert len(deliveries) == 2
    assert deliveries[0].status == "failed"
    assert deliveries[0].error_text == "telegram unavailable"
    assert deliveries[0].match_title == "Player 1 - Player 2"
    assert len(failed_deliveries) == 1
    assert len(searched_deliveries) == 1
    assert len(signal_deliveries) == 2
    assert len(user_deliveries) == 1
    assert failed_deliveries[0].telegram_id == inactive_user.telegram_id

    assert len(users) == 2
    assert users[0].telegram_id == 1002
    assert users[0].failed_deliveries == 1
    assert users[1].sent_deliveries == 1
    assert [item.telegram_id for item in users_by_id] == [1001]
    assert [item.username for item in users_by_username] == ["inactive_user"]
    assert users_by_missing == []
    assert [item.telegram_id for item in paid_users] == [1001]
    assert [item.telegram_id for item in trial_users] == [1002]

    assert [item.kind for item in requests] == ["subscription", "analysis"]
    assert [item.kind for item in subscription_requests] == ["subscription"]
    assert [item.kind for item in analysis_requests] == ["analysis"]
    assert [item.kind for item in new_requests] == ["subscription", "analysis"]
    assert done_requests == []
    assert signal_detail is not None
    assert signal_detail.item.id == signal.id
    assert signal_detail.external_match_id == 501
    assert len(signal_detail.deliveries) == 2
    assert user_detail is not None
    assert user_detail.item.telegram_id == 1001
    assert len(user_detail.deliveries) == 1
    assert user_detail.deliveries[0].signal_status == 'sent'
    assert user_detail.deliveries[0].result_status == 'won'
    assert user_detail.delivery_status_counts == {'sent': 1}
    assert user_detail.signal_group_counts == {'vip': 1}
    assert user_detail.result_status_counts == {'won': 1}
    assert user_detail.request_status_counts == {'new': 2}
    assert user_detail.request_kind_counts == {'subscription': 1, 'analysis': 1}
    assert [item.kind for item in user_detail.requests] == ["subscription", "analysis"]
    assert subscription_detail is not None
    assert subscription_detail.item.kind == "subscription"
    assert analysis_detail is not None
    assert analysis_detail.item.kind == "analysis"
    assert monitoring.dashboard.deliveries_by_status == {"failed": 1, "sent": 1}
    assert len(monitoring.failed_deliveries) == 1
    assert monitoring.failed_deliveries[0].error_text == "telegram unavailable"
    assert [item.file_name for item in monitoring.recent_imports] == ["sample.xlsx"]
    assert len(monitoring.recent_admin_actions) == 1
    assert monitoring.recent_admin_actions[0].actor_username == "admin"


@pytest.mark.asyncio
async def test_schedule_problem_filter_and_import_warnings() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        batch = ImportBatch(
            file_name="schedule.xlsx",
            stored_path="uploads/schedule.xlsx",
            file_sha256="abc",
            uploaded_by_telegram_id=315715137,
            status="completed",
        )
        session.add(batch)
        await session.flush()
        ok_match = Match(
            external_match_id=801,
            external_tournament_id=9001,
            source_url="https://example.test/801",
            tournament_date="24.07.2026",
            match_time="12:30",
            match_start_at=datetime(2030, 7, 24, 12, 30),
            player_1="OK Player",
            player_2="OK Opponent",
            raw_data={},
            is_present_in_latest_import=True,
        )
        bad_match = Match(
            external_match_id=802,
            external_tournament_id=9001,
            source_url="https://example.test/802",
            tournament_date="24.07.2026",
            match_time="13:00",
            match_start_at=datetime(2030, 7, 24, 13, 0),
            player_1="Bad Player",
            player_2="Bad Opponent",
            raw_data={},
            is_present_in_latest_import=True,
        )
        session.add_all([ok_match, bad_match])
        await session.flush()
        session.add_all([
            ScheduledSignal(
                match_id=ok_match.id,
                status="scheduled",
                send_at=datetime(2030, 7, 24, 12, 10),
                signal_payload={"signal_group": "vip"},
                source_import_id=batch.id,
            ),
            ScheduledSignal(
                match_id=bad_match.id,
                status="scheduled",
                send_at=datetime(2030, 7, 24, 12, 0),
                signal_payload={"signal_group": "all"},
                source_import_id=batch.id,
            ),
            SignalDecisionLog(
                match_id=ok_match.id,
                import_batch_id=batch.id,
                algorithm_version="v1",
                source="import",
                suitable=True,
                side=1,
                selected_player="OK Player",
                probability=99,
                level="TOP",
                signal_type="SET_VIP_TOP",
                decision_payload={"signal_group": "vip"},
            ),
            SignalDecisionLog(
                match_id=bad_match.id,
                import_batch_id=batch.id,
                algorithm_version="v1",
                source="import",
                suitable=False,
                reason="Недостаточно игр",
                decision_payload={},
            ),
        ])
        await session.commit()

        all_signals = await collect_signal_list(session)
        problem_signals = await collect_signal_list(session, schedule_filter="problem")
        import_warnings = await collect_import_signal_schedule_warnings(session, batch.id)
        import_detail = await collect_import_detail(session, batch.id)
        vip_detail = await collect_import_detail(session, batch.id, group_filter="vip")
        problem_detail = await collect_import_detail(session, batch.id, schedule_filter="problem")

    await engine.dispose()

    assert len(all_signals) == 2
    assert [item.player_1 for item in problem_signals] == ["Bad Player"]
    assert problem_signals[0].schedule_warning == "ожидалось 20 мин"
    assert [item.player_1 for item in import_warnings] == ["Bad Player"]
    assert import_detail is not None
    assert import_detail.batch.file_name == "schedule.xlsx"
    assert len(import_detail.signals) == 2
    assert [item.player_1 for item in import_detail.schedule_warnings] == ["Bad Player"]
    assert import_detail.decision_counts == {"accepted": 1, "rejected": 1}
    assert import_detail.total_signals == 2
    assert import_detail.filtered_signals == 2
    assert [(item.reason, item.count) for item in import_detail.rejection_reasons] == [("Недостаточно игр", 1)]
    assert vip_detail is not None
    assert [item.player_1 for item in vip_detail.signals] == ["OK Player"]
    assert problem_detail is not None
    assert [item.player_1 for item in problem_detail.signals] == ["Bad Player"]


@pytest.mark.asyncio
async def test_collect_quality_summary_groups_sent_signal_results() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        matches = []
        for offset in range(4):
            match = Match(
                external_match_id=601 + offset,
                external_tournament_id=9001,
                source_url=f"https://example.test/9001/{601 + offset}",
                tournament_date="24.07.2026",
                match_time="12:30",
                match_start_at=datetime(2026, 7, 24, 12, 30),
                player_1=f"Player {offset + 1}",
                player_2=f"Opponent {offset + 1}",
                raw_data={},
                is_present_in_latest_import=True,
            )
            matches.append(match)
        session.add_all(matches)
        await session.flush()

        vip_signal = ScheduledSignal(
            match_id=matches[0].id,
            status="sent",
            send_at=datetime(2026, 7, 24, 12, 10),
            signal_type="SET_VIP_TOP",
            signal_payload={"signal_group": "vip", "level": "TOP", "side": 1},
            message_text="vip signal",
        )
        all_lost_signal = ScheduledSignal(
            match_id=matches[1].id,
            status="sent",
            send_at=datetime(2026, 7, 24, 12, 11),
            signal_type="SET_ALL_STANDARD",
            signal_payload={"signal_group": "all", "level": "STANDARD", "side": 2},
            message_text="all signal",
        )
        all_unrated_signal = ScheduledSignal(
            match_id=matches[2].id,
            status="sent",
            send_at=datetime(2026, 7, 24, 12, 12),
            signal_type="SET_ALL_TOP",
            signal_payload={"signal_group": "all", "level": "TOP", "side": 1},
            message_text="all unrated",
        )
        scheduled_signal = ScheduledSignal(
            match_id=matches[3].id,
            status="ready",
            send_at=datetime(2026, 7, 24, 12, 13),
            signal_type="SET_VIP_TOP",
            signal_payload={"signal_group": "vip", "level": "TOP", "side": 1},
            message_text="not sent",
        )
        session.add_all([vip_signal, all_lost_signal, all_unrated_signal, scheduled_signal])
        await session.flush()
        session.add_all([
            SignalResult(signal_id=vip_signal.id, status="won", source="auto"),
            SignalResult(signal_id=all_lost_signal.id, status="lost", source="manual"),
            SignalResult(signal_id=scheduled_signal.id, status="won", source="auto"),
        ])
        await session.commit()

        summary = await collect_quality_summary(session)

    await engine.dispose()

    assert summary.sent_total == 3
    assert summary.overall.evaluated == 2
    assert summary.overall.unrated_sent == 1
    assert summary.overall.counter.winrate == 50.0
    assert [(item.key, item.sent_total, item.evaluated) for item in summary.by_group] == [("vip", 1, 1), ("all", 2, 1)]
    assert summary.by_group[0].counter.won == 1
    assert summary.by_group[1].counter.lost == 1
    assert [(item.key, item.sent_total, item.evaluated) for item in summary.by_level] == [("TOP", 2, 1), ("STANDARD", 1, 1)]


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
    assert summary.backups == ()



def test_build_system_health_checks_flags_operational_problems(tmp_path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    dashboard = DashboardSummary(
        deliveries_by_status={"failed": 2},
        subscription_requests_by_status={"new": 1},
        analysis_requests_by_status={"new": 1},
    )
    imports = [
        ImportListItem(
            id=1,
            file_name="bad.xlsx",
            status="failed",
            total_rows=10,
            parsed_matches=0,
            inserted_matches=0,
            updated_matches=0,
            missing_matches=0,
            error_text="Excel error",
            created_at=datetime.utcnow(),
            finished_at=datetime.utcnow(),
        )
    ]

    checks = build_system_health_checks(
        dashboard,
        imports,
        failed_delivery_count=2,
        overdue_signals=3,
        settings=SimpleNamespace(
            data_dir=data_dir,
            sqlite_backup_enabled=True,
            sqlite_backup_interval_hours=24,
        ),
    )

    by_title = {item.title: item for item in checks}
    assert by_title["Доставки"].status == "problem"
    assert by_title["Очередь сигналов"].status == "problem"
    assert by_title["Импорт"].status == "problem"
    assert by_title["Заявки"].status == "warning"
    assert by_title["Backup"].status == "warning"

