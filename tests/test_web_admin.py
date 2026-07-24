from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials

from app.database.models import Base, Match, MatchAnalysisRequest, ScheduledSignal, SignalResult, SubscriptionRequest, User, UserAccess
from app.services.dashboard import (
    DashboardSummary,
    MaintenanceSummary,
    QualityStatsItem,
    QualitySummary,
    LatestImportSummary,
    RecentSignalSummary,
    DeliveryListItem,
    RequestDetail,
    RequestListItem,
    SignalDetail,
    SignalListItem,
    UserDetail,
    UserListItem,
)
from app.services.bot_settings import PaymentConfig, get_analysis_payment_config, get_subscription_payment_config
from app.services.signal_results import AutoResultSummary, ResultCounter
from app.web_admin import (
    render_dashboard_html,
    render_deliveries_csv,
    render_deliveries_html,
    render_maintenance_html,
    render_quality_html,
    render_request_detail_html,
    render_requests_csv,
    render_requests_html,
    render_settings_html,
    render_signal_detail_html,
    render_subscriptions_csv,
    render_subscriptions_html,
    render_signals_csv,
    render_signals_html,
    render_user_detail_html,
    render_users_html,
    require_web_admin,
    update_web_payment_settings,
    update_web_request_status,
    update_web_signal_result,
    update_web_user_access,
)


def make_summary() -> DashboardSummary:
    return DashboardSummary(
        users_total=10,
        users_active=8,
        access_trial_active=2,
        access_paid_active=4,
        matches_total=20,
        matches_active=18,
        imports_total=3,
        signals_by_status={"ready": 5, "sent": 7},
        deliveries_by_status={"sent": 12, "failed": 1},
        results_by_status={"won": 3},
        subscription_requests_by_status={"new": 2},
        analysis_requests_by_status={"new": 1},
        latest_import=LatestImportSummary(
            id=3,
            file_name="sample.xlsx",
            status="completed",
            parsed_matches=168,
            inserted_matches=100,
            updated_matches=68,
            missing_matches=4,
            created_at=datetime(2026, 7, 24, 10, 0),
            finished_at=datetime(2026, 7, 24, 10, 1),
        ),
        recent_signals=[
            RecentSignalSummary(
                id=11,
                status="sent",
                send_at=datetime(2026, 7, 24, 11, 40),
                signal_group="vip",
                level="TOP",
                side=1,
                player_1="Player <One>",
                player_2="Player Two",
                result_status="won",
            )
        ],
    )


def make_signal() -> SignalListItem:
    return SignalListItem(
        id=11,
        status="sent",
        send_at=datetime(2026, 7, 24, 11, 40),
        signal_group="vip",
        level="TOP",
        side=1,
        player_1="Player <One>",
        player_2="Player Two",
        result_status="won",
        sent_deliveries=5,
        failed_deliveries=1,
    )


def test_render_dashboard_html_shows_core_metrics_and_navigation() -> None:
    html = render_dashboard_html(make_summary(), token="secret")

    assert "Админка Algobet" in html
    assert "sample.xlsx" in html
    assert "<strong>10</strong>" in html
    assert "Готов" in html
    assert "Отправлен" in html
    assert "Player &lt;One&gt;" in html
    assert "/signals" in html
    assert "/users" in html
    assert "/requests" in html


def test_render_signals_html_shows_filters_and_delivery_counts() -> None:
    html = render_signals_html([make_signal()], token="secret", status_filter="sent", result_filter="won")

    assert "Сигналы: Отправлен" in html
    assert "Зашли" in html
    assert "/signals?status=ready&result=won" in html
    assert "/signals?status=sent&result=lost" in html
    assert "/signals?status=sent&result=unrated" in html
    assert "/signals/export.csv?status=sent&result=won" in html
    assert "Player &lt;One&gt;" in html
    assert "5 / 1" in html
    assert "/signals/11" in html


def test_render_signals_csv_exports_rows() -> None:
    csv_text = render_signals_csv([make_signal()])

    assert csv_text.splitlines()[0] == "id,status,send_at,signal_group,level,side,match,result,sent_deliveries,failed_deliveries"
    assert "11,sent,24.07.2026 11:40,vip,TOP,1,Player <One> - Player Two,won,5,1" in csv_text


def test_render_deliveries_html_shows_filters_and_errors() -> None:
    html = render_deliveries_html(
        [
            DeliveryListItem(
                id=5,
                signal_id=11,
                status="failed",
                telegram_id=315715137,
                username="admin",
                match_title="Player <One> - Player Two",
                signal_group="vip",
                sent_at=None,
                created_at=datetime(2026, 7, 24, 11, 45),
                error_text="telegram <unavailable>",
            )
        ],
        token="secret",
        status_filter="failed",
    )

    assert "Доставки: Ошибка" in html
    assert "/deliveries?status=sent" in html
    assert "/deliveries/export.csv?status=failed" in html
    assert "/signals/11" in html
    assert "Player &lt;One&gt; - Player Two" in html
    assert "telegram &lt;unavailable&gt;" in html
    assert 'action="/deliveries/5/retry?status=failed"' in html
    assert "Повторить" in html


def test_render_deliveries_csv_exports_rows() -> None:
    csv_text = render_deliveries_csv([
        DeliveryListItem(
            id=5,
            signal_id=11,
            status="failed",
            telegram_id=315715137,
            username="admin",
            match_title="Player One - Player Two",
            signal_group="vip",
            sent_at=None,
            created_at=datetime(2026, 7, 24, 11, 45),
            error_text="telegram unavailable",
        )
    ])

    assert csv_text.splitlines()[0] == "id,signal_id,status,telegram_id,username,signal_group,match,time,error"
    assert "5,11,failed,315715137,admin,vip,Player One - Player Two,24.07.2026 11:45,telegram unavailable" in csv_text


def test_render_quality_html_shows_signal_result_statistics() -> None:
    summary = QualitySummary(
        sent_total=3,
        overall=QualityStatsItem(
            key="overall",
            title="Всего",
            sent_total=3,
            counter=ResultCounter(won=1, lost=1),
        ),
        by_group=[
            QualityStatsItem(
                key="vip",
                title="VIP",
                sent_total=1,
                counter=ResultCounter(won=1),
            ),
            QualityStatsItem(
                key="all",
                title="ALL",
                sent_total=2,
                counter=ResultCounter(lost=1),
            ),
        ],
        by_level=[
            QualityStatsItem(
                key="TOP",
                title="TOP",
                sent_total=2,
                counter=ResultCounter(won=1),
            )
        ],
    )

    html = render_quality_html(
        summary,
        token="secret",
        auto_result=AutoResultSummary(scanned=5, updated=2, unchanged=1, skipped_manual=1, no_score=1),
    )

    assert "Статистика качества" in html
    assert "Отправлено" in html
    assert "Без результата" in html
    assert 'action="/quality/auto-update"' in html
    assert "Обновить результаты по счету" in html
    assert "Автообновление" in html
    assert "проверено 5" in html
    assert "обновлено 2" in html
    assert "50.0%" in html
    assert "VIP" in html
    assert "ALL" in html
    assert "TOP" in html
    assert "/quality" in html


def test_render_users_html_shows_access_and_delivery_counts() -> None:
    html = render_users_html(
        [
            UserListItem(
                id=1,
                telegram_id=315715137,
                username="admin",
                first_name="Admin",
                last_name="User",
                is_active=True,
                access_type="paid",
                access_status="active",
                free_signals_remaining=0,
                signals_remaining=9,
                active_until=None,
                sent_deliveries=3,
                failed_deliveries=1,
            )
        ],
        token="secret",
        search="315715137",
    )

    assert 'name="search"' in html
    assert 'value="315715137"' in html
    assert "ID Telegram, имя пользователя или имя" in html
    assert "@admin" in html
    assert "Платный / Активен" in html
    assert "3 / 1" in html
    assert "/users/1" in html


def test_render_subscriptions_html_shows_filters_and_user_rows() -> None:
    html = render_subscriptions_html(
        [
            UserListItem(
                id=1,
                telegram_id=315715137,
                username="admin",
                first_name="Admin",
                last_name="User",
                is_active=True,
                access_type="paid",
                access_status="active",
                free_signals_remaining=0,
                signals_remaining=9,
                active_until=datetime(2026, 7, 30, 10, 0),
                sent_deliveries=3,
                failed_deliveries=1,
            )
        ],
        token="secret",
        access_type_filter="paid",
        access_status_filter="active",
    )

    assert "Подписки: Платные" in html
    assert "/subscriptions?type=trial&status=active" in html
    assert "/subscriptions?type=paid&status=disabled" in html
    assert "/subscriptions/export.csv?type=paid&status=active" in html
    assert "@admin" in html
    assert "Платный" in html
    assert "Активен" in html
    assert "/users/1" in html


def test_render_subscriptions_csv_exports_rows() -> None:
    csv_text = render_subscriptions_csv(
        [
            UserListItem(
                id=1,
                telegram_id=315715137,
                username="admin",
                first_name="Admin",
                last_name="User",
                is_active=True,
                access_type="paid",
                access_status="active",
                free_signals_remaining=0,
                signals_remaining=9,
                active_until=datetime(2026, 7, 30, 10, 0),
                sent_deliveries=3,
                failed_deliveries=1,
            )
        ]
    )

    assert csv_text.splitlines()[0] == "id,telegram_id,username,name,access_type,access_status,signals_remaining,free_signals_remaining,active_until,sent_deliveries,failed_deliveries"
    assert "1,315715137,admin,Admin User,paid,active,9,0,30.07.2026 10:00,3,1" in csv_text


def test_render_requests_html_shows_subscription_and_analysis_items() -> None:
    html = render_requests_html(
        [
            RequestListItem(
                kind="subscription",
                id=7,
                status="new",
                telegram_id=315715137,
                username="admin",
                title="VIP <99%>",
                created_at=datetime(2026, 7, 24, 11, 0),
            )
        ],
        token="secret",
        kind_filter="subscription",
        status_filter="new",
    )

    assert "Заявки: Подписки" in html
    assert "Новые" in html
    assert "/requests?kind=analysis&status=new" in html
    assert "/requests?kind=subscription&status=done" in html
    assert "/requests/export.csv?kind=subscription&status=new" in html
    assert "Подписка" in html
    assert "VIP &lt;99%&gt;" in html
    assert "@admin" in html
    assert "/requests/subscription/7" in html


def test_render_requests_csv_exports_rows() -> None:
    csv_text = render_requests_csv(
        [
            RequestListItem(
                kind="subscription",
                id=7,
                status="new",
                telegram_id=315715137,
                username="admin",
                title="VIP <99%>",
                created_at=datetime(2026, 7, 24, 11, 0),
            )
        ]
    )

    assert csv_text.splitlines()[0] == "kind,id,status,telegram_id,username,title,created_at"
    assert "subscription,7,new,315715137,admin,VIP <99%>,24.07.2026 11:00" in csv_text

def test_render_signal_detail_html_shows_message_deliveries_and_trace() -> None:
    signal = make_signal()
    detail = SignalDetail(
        item=signal,
        external_match_id=501,
        external_tournament_id=9001,
        source_url="https://example.test/9001/501",
        match_start_at=datetime(2026, 7, 24, 12, 0),
        match_time="12:00",
        tournament_date="24.07.2026",
        message_text="Signal <message>",
        cancel_reason=None,
        decision_reason="Accepted <rule>",
        decision_trace=[{"code": "P1_BASE", "label": "Base", "passed": True, "actual": "EG=5"}],
        deliveries=[],
    )

    html = render_signal_detail_html(detail, token="secret")

    assert "Сигнал #11" in html
    assert "Signal &lt;message&gt;" in html
    assert "Accepted &lt;rule&gt;" in html
    assert "P1_BASE" in html
    assert 'action="/signals/11/result/won"' in html
    assert 'action="/signals/11/result/lost"' in html
    assert 'action="/signals/11/result/void"' in html
    assert 'action="/signals/11/result/unknown"' in html
    assert "current" in html


def test_render_user_detail_html_shows_related_deliveries_and_requests() -> None:
    user = UserListItem(
        id=1,
        telegram_id=315715137,
        username="admin",
        first_name="Admin",
        last_name="User",
        is_active=True,
        access_type="paid",
        access_status="active",
        free_signals_remaining=0,
        signals_remaining=9,
        active_until=None,
        sent_deliveries=3,
        failed_deliveries=1,
    )
    detail = UserDetail(item=user, deliveries=[], requests=[RequestListItem("analysis", 4, "new", 315715137, "admin", "Match <A>", datetime(2026, 7, 24, 11, 0))])

    html = render_user_detail_html(detail, token="secret")

    assert "Пользователь #1" in html
    assert "Управление доступом" in html
    assert "/users/1/access/trial" in html
    assert "/users/1/access/disable" in html
    assert "/users/1/access/plan/vip_10" in html
    assert "VIP 99% · 10 сигналов · 2 500р" in html
    assert "Match &lt;A&gt;" in html
    assert "/requests/analysis/4" in html


def test_render_request_detail_html_shows_payment_and_contact() -> None:
    detail = RequestDetail(
        item=RequestListItem("subscription", 7, "new", 315715137, "admin", "VIP", datetime(2026, 7, 24, 11, 0)),
        payment_details="Card <123>",
        specialist_contact="@spec",
        description="vip_10",
        updated_at=datetime(2026, 7, 24, 11, 5),
    )

    html = render_request_detail_html(detail, token="secret")

    assert "Заявка: Подписка #7" in html
    assert "Card &lt;123&gt;" in html
    assert "@spec" in html
    assert 'action="/requests/subscription/7/status/done"' in html
    assert "Выполнена" in html


def test_render_settings_html_shows_payment_forms() -> None:
    html = render_settings_html(
        PaymentConfig(payment_details="Карта <111>", specialist_contact="@analysis"),
        PaymentConfig(payment_details="СБП <222>", specialist_contact="@sub"),
        message="Сохранено",
    )

    assert "Настройки" in html
    assert "/settings/analysis" in html
    assert "/settings/subscription" in html
    assert "Карта &lt;111&gt;" in html
    assert "СБП &lt;222&gt;" in html
    assert "Сохранено" in html


@pytest.mark.asyncio
async def test_update_web_payment_settings_saves_subscription_values() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        await update_web_payment_settings(session, "subscription", "Карта 5555", "@manager")

        config = await get_subscription_payment_config(session)

    await engine.dispose()

    assert config.payment_details == "Карта 5555"
    assert config.specialist_contact == "@manager"


@pytest.mark.asyncio
async def test_update_web_payment_settings_saves_analysis_values() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        await update_web_payment_settings(session, "analysis", "Карта анализа", "@spec")

        config = await get_analysis_payment_config(session)

    await engine.dispose()

    assert config.payment_details == "Карта анализа"
    assert config.specialist_contact == "@spec"


def test_render_maintenance_html_shows_storage_and_backup_settings() -> None:
    html = render_maintenance_html(
        MaintenanceSummary(
            database_path="data/algobet.db",
            database_size_bytes=2048,
            data_size_bytes=4096,
            uploads_size_bytes=512,
            latest_backup_path=None,
            latest_backup_size_bytes=None,
            latest_backup_created_at=None,
            sqlite_backup_enabled=True,
            sqlite_backup_interval_hours=24,
            sqlite_backup_keep=10,
        )
    )

    assert "Обслуживание" in html
    assert "data/algobet.db" in html
    assert "2.0 KB" in html
    assert "Включен" in html
    assert "/maintenance" in html


def test_require_web_admin_accepts_basic_credentials_for_multiple_admins(monkeypatch) -> None:
    monkeypatch.setattr("app.web_admin.get_settings", lambda: SimpleNamespace(web_admin_credentials={"admin": "secret", "manager": "second"}))

    require_web_admin(HTTPBasicCredentials(username="admin", password="secret"))
    require_web_admin(HTTPBasicCredentials(username="manager", password="second"))


def test_require_web_admin_rejects_missing_or_wrong_credentials(monkeypatch) -> None:
    monkeypatch.setattr("app.web_admin.get_settings", lambda: SimpleNamespace(web_admin_credentials={"admin": "secret", "manager": "second"}))

    with pytest.raises(HTTPException) as missing_exc:
        require_web_admin(None)
    with pytest.raises(HTTPException) as wrong_exc:
        require_web_admin(HTTPBasicCredentials(username="admin", password="bad"))

    assert missing_exc.value.status_code == 401
    assert missing_exc.value.headers == {"WWW-Authenticate": "Basic"}
    assert wrong_exc.value.status_code == 401


def test_require_web_admin_requires_configured_credentials(monkeypatch) -> None:
    monkeypatch.setattr("app.web_admin.get_settings", lambda: SimpleNamespace(web_admin_credentials={}))

    with pytest.raises(HTTPException) as exc:
        require_web_admin(HTTPBasicCredentials(username="admin", password="secret"))

    assert exc.value.status_code == 503


async def make_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


@pytest.mark.asyncio
async def test_update_web_subscription_status_grants_access() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        user = User(telegram_id=777, username="buyer", first_name="Buyer", last_name=None)
        session.add(user)
        await session.flush()
        request = SubscriptionRequest(
            user_id=user.id,
            telegram_id=user.telegram_id,
            username=user.username,
            plan_id="vip_10",
            plan_group="vip",
            plan_title="VIP 99%",
            plan_description="10 signals",
            price_rub=2500,
            signals_limit=10,
            includes_vip=True,
            status="new",
        )
        session.add(request)
        await session.commit()

        updated = await update_web_request_status(session, "subscription", request.id, "done")

        saved_request = await session.get(SubscriptionRequest, request.id)
        access = await session.scalar(select(UserAccess).where(UserAccess.user_id == user.id))

    await engine.dispose()

    assert updated is True
    assert saved_request is not None
    assert saved_request.status == "done"
    assert access is not None
    assert access.access_type == "paid"
    assert access.status == "active"
    assert access.plan_id == "vip_10"
    assert access.signals_remaining == 10
    assert access.includes_vip is True


@pytest.mark.asyncio
async def test_update_web_user_access_grants_selected_plan() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        user = User(telegram_id=779, username="vip", first_name="Vip", last_name=None)
        session.add(user)
        await session.commit()

        updated = await update_web_user_access(session, user.id, "plan", plan_id="included_48h")

        access = await session.scalar(select(UserAccess).where(UserAccess.user_id == user.id))

    await engine.dispose()

    assert updated is True
    assert access is not None
    assert access.access_type == "paid"
    assert access.status == "active"
    assert access.plan_id == "included_48h"
    assert access.plan_group == "included"
    assert access.signals_remaining is None
    assert access.includes_vip is True
    assert access.includes_all_signals is True
    assert access.includes_analytics is True
    assert access.active_until is not None


@pytest.mark.asyncio
async def test_update_web_user_access_can_disable_access() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        user = User(telegram_id=780, username="disabled", first_name="Disabled", last_name=None)
        session.add(user)
        await session.flush()
        session.add(UserAccess(user_id=user.id, access_type="paid", status="active", signals_remaining=10, includes_vip=True))
        await session.commit()

        updated = await update_web_user_access(session, user.id, "disable")

        access = await session.scalar(select(UserAccess).where(UserAccess.user_id == user.id))

    await engine.dispose()

    assert updated is True
    assert access is not None
    assert access.status == "disabled"
    assert access.signals_remaining is None
    assert access.free_signals_remaining == 0


@pytest.mark.asyncio
async def test_update_web_analysis_status_changes_request_only() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        user = User(telegram_id=778, username="analyst", first_name="Analyst", last_name=None)
        session.add(user)
        await session.flush()
        request = MatchAnalysisRequest(
            user_id=user.id,
            telegram_id=user.telegram_id,
            username=user.username,
            match_text="Player A - Player B",
            status="new",
        )
        session.add(request)
        await session.commit()

        updated = await update_web_request_status(session, "analysis", request.id, "in_progress")

        saved_request = await session.get(MatchAnalysisRequest, request.id)

    await engine.dispose()

    assert updated is True
    assert saved_request is not None
    assert saved_request.status == "in_progress"


@pytest.mark.asyncio
async def test_update_web_signal_result_creates_and_updates_manual_result() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        match = Match(
            external_match_id=701,
            external_tournament_id=9001,
            source_url="https://example.test/9001/701",
            tournament_date="24.07.2026",
            match_time="12:30",
            match_start_at=datetime(2026, 7, 24, 12, 30),
            player_1="Player A",
            player_2="Player B",
            raw_data={},
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
        )
        session.add(signal)
        await session.commit()

        created = await update_web_signal_result(session, signal.id, "won")
        result = await session.scalar(select(SignalResult).where(SignalResult.signal_id == signal.id))
        updated = await update_web_signal_result(session, signal.id, "lost")
        saved_results = list((await session.execute(select(SignalResult))).scalars())
        missing = await update_web_signal_result(session, 9999, "won")

    await engine.dispose()

    assert created is True
    assert result is not None
    assert result.status == "lost"
    assert result.source == "manual"
    assert result.fixed_by_telegram_id is None
    assert updated is True
    assert len(saved_results) == 1
    assert missing is False
