from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials

from app.services.dashboard import (
    DashboardSummary,
    MaintenanceSummary,
    LatestImportSummary,
    RecentSignalSummary,
    RequestDetail,
    RequestListItem,
    SignalDetail,
    SignalListItem,
    UserDetail,
    UserListItem,
)
from app.web_admin import (
    render_dashboard_html,
    render_maintenance_html,
    render_request_detail_html,
    render_requests_html,
    render_signal_detail_html,
    render_signals_html,
    render_user_detail_html,
    render_users_html,
    require_web_admin,
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
    html = render_signals_html([make_signal()], token="secret", status_filter="sent")

    assert "Сигналы: Отправлен" in html
    assert "/signals?status=ready" in html
    assert "Player &lt;One&gt;" in html
    assert "5 / 1" in html
    assert "/signals/11" in html


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
    )

    assert "@admin" in html
    assert "Платный / Активен" in html
    assert "3 / 1" in html
    assert "/users/1" in html


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
    )

    assert "Подписка" in html
    assert "VIP &lt;99%&gt;" in html
    assert "@admin" in html
    assert "/requests/subscription/7" in html


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
