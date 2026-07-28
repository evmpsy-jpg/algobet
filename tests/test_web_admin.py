from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials

from app.database.models import Base, Match, MatchAnalysisRequest, ScheduledSignal, SignalResult, SubscriptionRequest, User, UserAccess, WebAdminActionLog, WebAdminUser
from app.services.dashboard import (
    DashboardSummary,
    MaintenanceSummary,
    MonitoringSummary,
    SystemHealthItem,
    QualityStatsItem,
    QualitySummary,
    LatestImportSummary,
    RecentSignalSummary,
    DeliveryListItem,
    ImportListItem,
    ImportDecisionReason,
    ImportDetail,
    RequestDetail,
    RequestListItem,
    SignalDetail,
    SignalListItem,
    UserDetail,
    UserDeliveryListItem,
    UserListItem,
)
from app.services.bot_settings import PaymentConfig, SystemRuntimeSettings, get_analysis_payment_config, get_subscription_payment_config
from app.services.sqlite_backup import BackupInfo, BackupVerification
from app.services.signal_results import AutoResultSummary, ResultCounter
from app.web_admin import (
    SystemPageSummary,
    WebAdminActivityItem,
    authenticate_web_admin,
    _csv_response,
    collect_web_admin_activity,
    create_or_update_web_admin_user,
    deactivate_web_admin_user,
    hash_web_admin_password,
    is_web_admin_superuser,
    log_web_admin_action,
    render_audit_csv,
    render_audit_html,
    render_admin_guide_html,
    render_markdown_document,
    render_dashboard_html,
    render_import_detail_html,
    render_deliveries_csv,
    render_deliveries_html,
    render_maintenance_html,
    render_monitoring_html,
    render_quality_html,
    render_request_detail_html,
    render_requests_csv,
    render_requests_html,
    render_settings_html,
    render_signal_detail_html,
    render_system_html,
    render_subscriptions_csv,
    render_subscriptions_html,
    render_signals_csv,
    render_signals_html,
    render_user_detail_html,
    render_web_admins_html,
    render_users_html,
    update_web_admin_password,
    update_web_payment_settings,
    update_web_system_backup_settings,
    verify_env_web_admin_credentials,
    verify_web_admin_password,
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
        match_start_at=datetime(2026, 7, 24, 12, 0),
        lead_minutes=20,
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



def test_csv_response_adds_utf8_bom_for_excel() -> None:
    response = _csv_response("title\nВсё включено\n", "sample.csv")

    assert response.body.startswith(b"\xef\xbb\xbf")
    assert response.media_type == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == "attachment; filename=sample.csv"
    assert response.body.decode("utf-8-sig").splitlines()[1] == "Всё включено"


def test_admin_tables_scroll_inside_sections_on_mobile() -> None:
    html = render_dashboard_html(DashboardSummary())

    assert "overflow-x:auto" in html
    assert "min-width:680px" in html


def test_render_signals_html_shows_filters_and_delivery_counts() -> None:
    html = render_signals_html([make_signal()], token="secret", status_filter="sent", result_filter="won")

    assert "Сигналы: Отправлен" in html
    assert "Зашли" in html
    assert "/signals?status=ready&result=won" in html
    assert "/signals?status=sent&result=lost" in html
    assert "/signals?status=sent&result=unrated" in html
    assert "/signals/export.csv?status=sent&result=won" in html
    assert "/signals?status=sent&result=won&schedule=problem" in html
    assert "Все расписание" in html
    assert "Player &lt;One&gt;" in html
    assert "5 / 1" in html
    assert "/signals/11" in html


def test_render_signals_html_shows_schedule_problem_filter() -> None:
    signal = make_signal()
    problem = SignalListItem(
        id=12,
        status="scheduled",
        send_at=datetime(2026, 7, 24, 12, 0),
        signal_group="all",
        level="STANDARD",
        side=2,
        player_1="Problem Player",
        player_2="Opponent",
        result_status=None,
        match_start_at=datetime(2026, 7, 24, 13, 0),
        lead_minutes=60,
        schedule_warning="ожидалось 20 мин",
    )

    html = render_signals_html([signal, problem], token="secret", status_filter="scheduled", schedule_filter="problem")

    assert "Только проблемы" in html
    assert "/signals?status=scheduled" in html
    assert "/signals/export.csv?status=scheduled&schedule=problem" in html
    assert "Problem Player" in html
    assert "ожидалось 20 мин" in html


def test_render_signals_csv_exports_rows() -> None:
    csv_text = render_signals_csv([make_signal()])

    assert csv_text.splitlines()[0] == "id,status,send_at,match_start_at,lead_minutes,schedule_warning,signal_group,level,side,match,result,sent_deliveries,failed_deliveries"
    assert "11,sent,24.07.2026 14:40,24.07.2026 15:00,20,,vip,TOP,1,Player <One> - Player Two,won,5,1" in csv_text


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
                user_id=1,
                first_name="Admin",
                last_name="User",
                signal_status="sent",
            )
        ],
        token="secret",
        status_filter="failed",
        search="admin",
        signal_id=11,
        user_id=1,
    )

    assert "Доставки: Ошибка" in html
    assert "/deliveries?status=sent&q=admin&signal_id=11&user_id=1" in html
    assert "/deliveries/export.csv?status=failed&q=admin&signal_id=11&user_id=1" in html
    assert "/signals/11" in html
    assert "/users/1" in html
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
            user_id=1,
            first_name="Admin",
            last_name="User",
            signal_status="sent",
        )
    ])

    assert csv_text.splitlines()[0] == "id,signal_id,signal_status,status,telegram_id,username,name,signal_group,match,created_at,sent_at,error"
    assert "5,11,sent,failed,315715137,admin,Admin User,vip,Player One - Player Two,24.07.2026 14:45,-,telegram unavailable" in csv_text


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
                key="vip_99",
                title="VIP 99%",
                sent_total=1,
                counter=ResultCounter(won=1),
            ),
            QualityStatsItem(
                key="all_95",
                title="Все остальные сигналы",
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
    assert "По тарифам" in html
    assert "VIP 99%" in html
    assert "Все остальные сигналы" in html
    assert "По уровням" not in html
    assert "TOP" not in html
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
    assert "1,315715137,admin,Admin User,paid,active,9,0,30.07.2026 13:00,3,1" in csv_text


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
    assert "subscription,7,new,315715137,admin,VIP <99%>,24.07.2026 14:00" in csv_text

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
    delivery = UserDeliveryListItem(
        id=5,
        signal_id=11,
        status='sent',
        match_title='Player <One> - Player Two',
        signal_group='vip',
        sent_at=datetime(2026, 7, 24, 12, 10),
        error_text=None,
        created_at=datetime(2026, 7, 24, 12, 0),
        signal_status='sent',
        result_status='won',
    )
    detail = UserDetail(
        item=user,
        deliveries=[delivery],
        requests=[RequestListItem('analysis', 4, 'new', 315715137, 'admin', 'Match <A>', datetime(2026, 7, 24, 11, 0))],
        delivery_status_counts={'sent': 3, 'failed': 1},
        signal_group_counts={'vip': 1},
        result_status_counts={'won': 1},
        request_status_counts={'new': 1},
        request_kind_counts={'analysis': 1},
    )

    html = render_user_detail_html(detail, token="secret")

    assert "Пользователь #1" in html
    assert "Управление доступом" in html
    assert "/users/1/access/trial" in html
    assert "/users/1/access/disable" in html
    assert "/users/1/access/plan/vip_10" in html
    assert "VIP 99% · 10 сигналов · 2 500р" in html
    assert "Match &lt;A&gt;" in html
    assert "/requests/analysis/4" in html
    assert '/deliveries?user_id=1' in html
    assert '/deliveries?status=failed' in html
    assert 'user_id=1' in html
    assert '/deliveries/export.csv?user_id=1' in html
    assert 'Результаты сигналов' in html
    assert 'Player &lt;One&gt; - Player Two' in html
    assert 'Выиграл' in html
    assert '/signals/11' in html


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


def test_render_import_detail_html_shows_signals_and_rejections() -> None:
    signal = make_signal()
    problem = SignalListItem(
        id=12,
        status="scheduled",
        send_at=datetime(2026, 7, 24, 12, 0),
        signal_group="all",
        level="STANDARD",
        side=2,
        player_1="Problem Player",
        player_2="Opponent",
        result_status=None,
        match_start_at=datetime(2026, 7, 24, 13, 0),
        lead_minutes=60,
        schedule_warning="ожидалось 20 мин",
    )
    detail = ImportDetail(
        batch=ImportListItem(
            id=2,
            file_name="ЛЕТО.xlsx",
            status="completed",
            total_rows=10,
            parsed_matches=8,
            inserted_matches=7,
            updated_matches=1,
            missing_matches=2,
            error_text=None,
            created_at=datetime(2026, 7, 24, 10, 0),
            finished_at=datetime(2026, 7, 24, 10, 1),
        ),
        signals=[signal, problem],
        schedule_warnings=[problem],
        decision_counts={"accepted": 2, "rejected": 3},
        rejection_reasons=[ImportDecisionReason("Недостаточно игр", 3)],
        total_signals=2,
        filtered_signals=2,
        status_filter="scheduled",
        group_filter="all",
        schedule_filter="problem",
        limit=50,
    )

    html = render_import_detail_html(detail, token="secret")

    assert "Импорт #2" in html
    assert "ЛЕТО.xlsx" in html
    assert "Проблем расписания" in html
    assert "Показано" in html
    assert "/imports/2?status=scheduled&group=all&schedule=problem&limit=100" in html
    assert "/imports/2/signals.csv?status=scheduled&group=all&schedule=problem&limit=50" in html
    assert "Problem Player" in html
    assert "ожидалось 20 мин" in html
    assert "Недостаточно игр" in html
    assert "/signals/12" in html


def test_render_monitoring_html_shows_operational_summary() -> None:
    dashboard = make_summary()
    summary = MonitoringSummary(
        dashboard=dashboard,
        failed_deliveries=[
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
        recent_imports=[
            ImportListItem(
                id=2,
                file_name="ЛЕТО.xlsx",
                status="failed",
                total_rows=10,
                parsed_matches=8,
                inserted_matches=7,
                updated_matches=1,
                missing_matches=2,
                error_text="Excel error",
                created_at=datetime(2026, 7, 24, 10, 0),
                finished_at=datetime(2026, 7, 24, 10, 1),
            )
        ],
        recent_admin_actions=[
            WebAdminActionLog(
                actor_username="admin",
                action="settings_update",
                target_type="settings",
                target_id="analysis",
                details={"section": "analysis"},
                created_at=datetime(2026, 7, 24, 12, 0),
            )
        ],
        system_checks=[
            SystemHealthItem("problem", "Доставки", "Есть ошибки доставки: 1", "Откройте раздел Доставки"),
            SystemHealthItem("warning", "Заявки", "Новых заявок: 3", "Подписки: 2, анализ: 1"),
            SystemHealthItem("ok", "Backup", "Последний backup читается.", "algobet.db"),
        ],
        overdue_signals=2,
    )

    html = render_monitoring_html(summary)

    assert "Мониторинг" in html
    assert "Очередь сигналов" in html
    assert "Состояние системы" in html
    assert "Проблема" in html
    assert "Внимание" in html
    assert "Последний backup читается" in html
    assert "просроченных 2" in html
    assert "Последние ошибки доставки" in html
    assert "telegram &lt;unavailable&gt;" in html
    assert "ЛЕТО.xlsx" in html
    assert "Excel error" in html
    assert "/imports/2" in html
    assert "Изменение настроек" in html
    assert "/monitoring" in html



def test_render_web_admins_html_shows_configured_admins_without_passwords() -> None:
    html = render_web_admins_html([
        WebAdminActivityItem("admin", True, "settings_update", datetime(2026, 7, 25, 9, 0), 3, source="БД", is_super_admin=True),
        WebAdminActivityItem("env", True, source="Аварийный .env", is_super_admin=True),
        WebAdminActivityItem("old", False, "request_status_update", datetime(2026, 7, 24, 8, 0), 1, source="Журнал"),
    ], current_username="admin", message="admin_saved")

    assert "Админы" in html
    assert "Админ сохранен" in html
    assert "admin" in html
    assert "old" in html
    assert "Изменение настроек" in html
    assert "Нет, только в журнале" in html
    assert "БД" in html
    assert "Аварийный .env" in html
    assert "Super-admin" in html
    assert "техническим разделам" in html
    assert "Добавить админа" in html
    assert "Сменить пароль" in html
    assert "Свой доступ не отключаем" in html
    assert "Управляется вне админки" in html
    assert "WEB_ADMIN_USERS" in html
    assert "secret" not in html
    assert "/admins" in html


@pytest.mark.asyncio
async def test_collect_web_admin_activity_uses_configured_users_and_audit_logs() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        session.add_all([
            WebAdminActionLog(
                actor_username="admin",
                action="settings_update",
                target_type="settings",
                target_id="analysis",
                details={},
                created_at=datetime.utcnow(),
            ),
            WebAdminActionLog(
                actor_username="old",
                action="request_status_update",
                target_type="subscription_request",
                target_id="1",
                details={},
                created_at=datetime(2026, 1, 1, 12, 0),
            ),
        ])
        await session.commit()

        session.add(WebAdminUser(username="dbadmin", password_hash=hash_web_admin_password("very-long-password"), is_active=True, is_super_admin=True))
        session.add(WebAdminUser(username="disabled", password_hash=hash_web_admin_password("very-long-password"), is_active=False))
        await session.commit()

        items = await collect_web_admin_activity(session, ["admin", "manager"])

    await engine.dispose()

    by_name = {item.username: item for item in items}
    assert by_name["admin"].configured is True
    assert by_name["admin"].last_action == "settings_update"
    assert by_name["admin"].actions_7d == 1
    assert by_name["admin"].source == "Аварийный .env"
    assert by_name["manager"].configured is True
    assert by_name["manager"].last_action is None
    assert by_name["dbadmin"].configured is True
    assert by_name["dbadmin"].source == "БД"
    assert by_name["dbadmin"].is_super_admin is True
    assert by_name["disabled"].configured is False
    assert by_name["old"].configured is False


def test_render_audit_html_shows_action_rows() -> None:
    log = WebAdminActionLog(
        actor_username="admin",
        action="request_status_update",
        target_type="subscription_request",
        target_id="7",
        details={"old_status": "new", "new_status": "done"},
        created_at=datetime(2026, 7, 24, 11, 0),
    )

    html = render_audit_html([log])

    assert "Журнал действий" in html
    assert "admin" in html
    assert "Изменение статуса заявки" in html
    assert "Заявка на подписку" in html
    assert "new" in html
    assert "done" in html
    assert "/audit/export.csv" in html


def test_render_audit_csv_exports_rows() -> None:
    csv_text = render_audit_csv([
        WebAdminActionLog(
            actor_username="admin",
            action="request_status_update",
            target_type="subscription_request",
            target_id="7",
            details={"old_status": "new", "new_status": "done"},
            created_at=datetime(2026, 7, 24, 11, 0),
        )
    ])

    assert csv_text.splitlines()[0] == "created_at,actor_username,action,target_type,target_id,details"
    assert "24.07.2026 14:00,admin,request_status_update,subscription_request,7" in csv_text
    assert "old_status" in csv_text


@pytest.mark.asyncio
async def test_log_web_admin_action_creates_log_row() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        await log_web_admin_action(
            session,
            actor_username="manager",
            action="settings_update",
            target_type="settings",
            target_id="analysis",
            details={"section": "analysis"},
        )
        await session.commit()

        log = await session.scalar(select(WebAdminActionLog))

    await engine.dispose()

    assert log is not None
    assert log.actor_username == "manager"
    assert log.action == "settings_update"
    assert log.target_type == "settings"
    assert log.target_id == "analysis"
    assert log.details == {"section": "analysis"}


def test_render_system_html_shows_safe_settings_and_backup_form() -> None:
    html = render_system_html(
        SystemPageSummary(
            runtime=SystemRuntimeSettings(sqlite_backup_enabled=True, sqlite_backup_interval_hours=6, sqlite_backup_keep=7),
            database_url="sqlite+aiosqlite:///./data/algobet.db",
            database_path="data/algobet.db",
            data_dir="data",
            uploads_dir="uploads",
            timezone="Europe/Moscow",
            max_upload_mb=25,
            scheduler_interval_seconds=30,
            signal_lead_minutes=20,
            sqlite_backup_env_enabled=True,
            sqlite_backup_env_interval_hours=24,
            sqlite_backup_env_keep=10,
            bot_token_configured=True,
            web_admin_users_configured=True,
            web_admin_superusers=("root",),
            database_size_bytes=2048,
            data_size_bytes=4096,
            uploads_size_bytes=512,
        ),
        message="Сохранено",
    )

    assert "Система" in html
    assert "Сохранено" in html
    assert "Auto-backup SQLite" in html
    assert 'action="/system/backup"' in html
    assert 'name="sqlite_backup_interval_hours"' in html
    assert 'value="6"' in html
    assert "BOT_TOKEN" in html
    assert "настроен" in html
    assert "root" in html
    assert "sqlite+aiosqlite:///./data/algobet.db" not in html
    assert "/system" in html


@pytest.mark.asyncio
async def test_update_web_system_backup_settings_saves_values_and_logs() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        runtime = await update_web_system_backup_settings(
            session,
            sqlite_backup_enabled=False,
            sqlite_backup_interval_hours="4",
            sqlite_backup_keep="8",
            actor_username="root",
        )
        log = await session.scalar(select(WebAdminActionLog).where(WebAdminActionLog.action == "system_settings_update"))

    await engine.dispose()

    assert runtime.sqlite_backup_enabled is False
    assert runtime.sqlite_backup_interval_hours == 4
    assert runtime.sqlite_backup_keep == 8
    assert log is not None
    assert log.actor_username == "root"
    assert log.target_id == "system_backup"
    assert log.details == {
        "sqlite_backup_enabled": False,
        "sqlite_backup_interval_hours": 4,
        "sqlite_backup_keep": 8,
    }


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
        await update_web_payment_settings(session, "subscription", "Карта 5555", "@manager", actor_username="admin")

        config = await get_subscription_payment_config(session)
        log = await session.scalar(select(WebAdminActionLog).where(WebAdminActionLog.action == "settings_update"))

    await engine.dispose()

    assert config.payment_details == "Карта 5555"
    assert config.specialist_contact == "@manager"
    assert log is not None
    assert log.actor_username == "admin"
    assert log.target_type == "settings"
    assert log.target_id == "subscription"


@pytest.mark.asyncio
async def test_update_web_payment_settings_saves_analysis_values() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        await update_web_payment_settings(session, "analysis", "Карта анализа", "@spec")

        config = await get_analysis_payment_config(session)

    await engine.dispose()

    assert config.payment_details == "Карта анализа"
    assert config.specialist_contact == "@spec"


def test_render_admin_guide_html_converts_markdown_and_shows_navigation() -> None:
    html = render_admin_guide_html("# Инструкция\n\n## Сигналы\n\n1. Откройте `Мониторинг`.\n- Проверьте [README](README.md).")

    assert "Инструкция" in html
    assert "<h1>Инструкция</h1>" in html
    assert "<h2>Сигналы</h2>" in html
    assert "<ol>" in html
    assert "<ul>" in html
    assert "<code>Мониторинг</code>" in html
    assert '<a href="README.md">README</a>' in html
    assert "/docs" in html


def test_render_markdown_document_escapes_html() -> None:
    html = render_markdown_document("# <script>bad</script>\n\n- `safe <code>`")

    assert "&lt;script&gt;bad&lt;/script&gt;" in html
    assert "safe &lt;code&gt;" in html
    assert "<script>" not in html


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
            backups=(
                BackupInfo(path=Path("data/backups/algobet-20260725-080000.db"), size_bytes=4096, created_at=datetime(2026, 7, 25, 8, 0)),
            ),
        ),
        message="Backup создан",
        backup_check=BackupVerification(path=Path("data/backups/algobet-20260725-080000.db"), ok=True, message="ok", table_count=12),
    )

    assert "Обслуживание" in html
    assert "data/algobet.db" in html
    assert "2.0 KB" in html
    assert "Включен" in html
    assert "Backup SQLite" in html
    assert "Сделать backup сейчас" in html
    assert "Проверить последний backup" in html
    assert "algobet-20260725-080000.db" in html
    assert "Backup исправен" in html
    assert "Backup создан" in html
    assert "/maintenance/backup" in html


def test_verify_env_web_admin_credentials_accepts_multiple_admins() -> None:
    configured = {"admin": "secret", "manager": "second"}

    assert verify_env_web_admin_credentials(HTTPBasicCredentials(username="admin", password="secret"), configured) == "admin"
    assert verify_env_web_admin_credentials(HTTPBasicCredentials(username="manager", password="second"), configured) == "manager"
    assert verify_env_web_admin_credentials(HTTPBasicCredentials(username="admin", password="bad"), configured) is None
    assert verify_env_web_admin_credentials(None, configured) is None


def test_web_admin_password_hash_does_not_store_plain_password() -> None:
    stored_hash = hash_web_admin_password("very-secret-password", salt=b"1234567890123456")

    assert "very-secret-password" not in stored_hash
    assert verify_web_admin_password("very-secret-password", stored_hash) is True
    assert verify_web_admin_password("wrong-password", stored_hash) is False


@pytest.mark.asyncio
async def test_authenticate_web_admin_uses_database_user_and_updates_login(monkeypatch) -> None:
    monkeypatch.setattr("app.web_admin.get_settings", lambda: SimpleNamespace(web_admin_credentials={}))
    engine, factory = await make_session()
    async with factory() as session:
        session.add(WebAdminUser(username="admin", password_hash=hash_web_admin_password("very-secret-password"), is_active=True))
        await session.commit()

        username = await authenticate_web_admin(session, HTTPBasicCredentials(username="admin", password="very-secret-password"))
        user = await session.scalar(select(WebAdminUser).where(WebAdminUser.username == "admin"))

    await engine.dispose()

    assert username == "admin"
    assert user is not None
    assert user.last_login_at is not None


@pytest.mark.asyncio
async def test_authenticate_web_admin_falls_back_to_env_credentials(monkeypatch) -> None:
    monkeypatch.setattr("app.web_admin.get_settings", lambda: SimpleNamespace(web_admin_credentials={"root": "env-password"}, web_admin_superusers={"root"}))
    engine, factory = await make_session()
    async with factory() as session:
        username = await authenticate_web_admin(session, HTTPBasicCredentials(username="root", password="env-password"))

    await engine.dispose()

    assert username == "root"


@pytest.mark.asyncio
async def test_manage_web_admin_users_create_password_role_and_deactivate() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        user = await create_or_update_web_admin_user(
            session,
            "manager",
            "very-secret-password",
            actor_username="root",
            is_super_admin=True,
        )
        created_hash = user.password_hash

        assert user.username == "manager"
        assert user.is_active is True
        assert user.is_super_admin is True
        assert verify_web_admin_password("very-secret-password", user.password_hash) is True

        updated = await update_web_admin_password(session, "manager", "another-secret-password", actor_username="root")
        refreshed = await session.scalar(select(WebAdminUser).where(WebAdminUser.username == "manager"))
        disabled = await deactivate_web_admin_user(session, "manager", actor_username="root")
        logs = list((await session.scalars(select(WebAdminActionLog).order_by(WebAdminActionLog.id))).all())

    await engine.dispose()

    assert updated is True
    assert disabled is True
    assert refreshed is not None
    assert refreshed.password_hash != created_hash
    assert refreshed.is_active is False
    assert [log.action for log in logs] == ["web_admin_create", "web_admin_password_update", "web_admin_deactivate"]
    assert logs[0].details["is_super_admin"] is True


@pytest.mark.asyncio
async def test_is_web_admin_superuser_checks_db_role_and_env_fallback(monkeypatch) -> None:
    monkeypatch.setattr("app.web_admin.get_settings", lambda: SimpleNamespace(web_admin_credentials={"root": "env-password"}, web_admin_superusers={"root"}))
    engine, factory = await make_session()
    async with factory() as session:
        session.add_all([
            WebAdminUser(username="super", password_hash=hash_web_admin_password("very-secret-password"), is_active=True, is_super_admin=True),
            WebAdminUser(username="manager", password_hash=hash_web_admin_password("very-secret-password"), is_active=True, is_super_admin=False),
            WebAdminUser(username="disabled", password_hash=hash_web_admin_password("very-secret-password"), is_active=False, is_super_admin=True),
        ])
        await session.commit()

        assert await is_web_admin_superuser(session, "super") is True
        assert await is_web_admin_superuser(session, "manager") is False
        assert await is_web_admin_superuser(session, "disabled") is False
        assert await is_web_admin_superuser(session, "root") is True

    await engine.dispose()


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

        updated = await update_web_request_status(session, "subscription", request.id, "done", actor_username="admin")

        saved_request = await session.get(SubscriptionRequest, request.id)
        access = await session.scalar(select(UserAccess).where(UserAccess.user_id == user.id))
        log = await session.scalar(select(WebAdminActionLog).where(WebAdminActionLog.action == "request_status_update"))

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
    assert log is not None
    assert log.actor_username == "admin"
    assert log.target_type == "subscription_request"
    assert log.target_id == str(request.id)
    assert log.details == {"old_status": "new", "new_status": "done"}


@pytest.mark.asyncio
async def test_update_web_user_access_grants_selected_plan() -> None:
    engine, factory = await make_session()
    async with factory() as session:
        user = User(telegram_id=779, username="vip", first_name="Vip", last_name=None)
        session.add(user)
        await session.commit()

        updated = await update_web_user_access(session, user.id, "plan", plan_id="included_48h", actor_username="manager")

        access = await session.scalar(select(UserAccess).where(UserAccess.user_id == user.id))
        log = await session.scalar(select(WebAdminActionLog).where(WebAdminActionLog.action == "user_access_update"))

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
    assert log is not None
    assert log.actor_username == "manager"
    assert log.target_type == "user"
    assert log.target_id == str(user.id)
    assert log.details["action"] == "plan"
    assert log.details["plan_id"] == "included_48h"


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
        updated = await update_web_signal_result(session, signal.id, "lost", actor_username="admin")
        saved_results = list((await session.execute(select(SignalResult))).scalars())
        log = await session.scalar(select(WebAdminActionLog).where(WebAdminActionLog.action == "signal_result_update"))
        missing = await update_web_signal_result(session, 9999, "won")

    await engine.dispose()

    assert created is True
    assert result is not None
    assert result.status == "lost"
    assert result.source == "manual"
    assert result.fixed_by_telegram_id is None
    assert updated is True
    assert len(saved_results) == 1
    assert log is not None
    assert log.actor_username == "admin"
    assert log.target_type == "signal"
    assert log.target_id == str(signal.id)
    assert log.details == {"old_status": "won", "new_status": "lost"}
    assert missing is False
