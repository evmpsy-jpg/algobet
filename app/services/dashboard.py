from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.sqlite_backup import BackupInfo, latest_sqlite_backup, list_sqlite_backups, sqlite_database_path, verify_sqlite_backup
from app.services.rules_config import get_signal_rules
from app.services.signal_results import LEVEL_ORDER, ResultCounter, signal_stats_eligible, summarize_results
from app.services.signal_tariffs import SIGNAL_TARIFF_ORDER, signal_tariff_key, signal_tariff_title
from app.settings import get_settings
from app.services.bot_settings import apply_system_runtime_settings, get_system_runtime_settings
from app.database.models import (
    ImportBatch,
    Match,
    MatchAnalysisRequest,
    ScheduledSignal,
    SignalDelivery,
    SignalResult,
    SignalDecisionLog,
    SubscriptionRequest,
    User,
    UserAccess,
    WebAdminActionLog,
)




@dataclass(frozen=True)
class MaintenanceSummary:
    database_path: str | None
    database_size_bytes: int
    data_size_bytes: int
    uploads_size_bytes: int
    latest_backup_path: str | None
    latest_backup_size_bytes: int | None
    latest_backup_created_at: datetime | None
    sqlite_backup_enabled: bool
    sqlite_backup_interval_hours: int
    sqlite_backup_keep: int
    backups: tuple[BackupInfo, ...] = ()

@dataclass(frozen=True)
class LatestImportSummary:
    id: int
    file_name: str
    status: str
    parsed_matches: int
    inserted_matches: int
    updated_matches: int
    missing_matches: int
    created_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class RecentSignalSummary:
    id: int
    status: str
    send_at: datetime
    signal_group: str
    level: str | None
    side: int | None
    player_1: str
    player_2: str
    result_status: str | None
    match_start_at: datetime | None = None
    lead_minutes: int | None = None
    schedule_warning: str | None = None


@dataclass(frozen=True)
class SignalListItem:
    id: int
    status: str
    send_at: datetime
    signal_group: str
    level: str | None
    side: int | None
    player_1: str
    player_2: str
    result_status: str | None
    sent_deliveries: int = 0
    failed_deliveries: int = 0
    match_start_at: datetime | None = None
    lead_minutes: int | None = None
    schedule_warning: str | None = None


@dataclass(frozen=True)
class UserListItem:
    id: int
    telegram_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    is_active: bool
    access_type: str | None
    access_status: str | None
    free_signals_remaining: int | None
    signals_remaining: int | None
    active_until: datetime | None
    sent_deliveries: int = 0
    failed_deliveries: int = 0


@dataclass(frozen=True)
class RequestListItem:
    kind: str
    id: int
    status: str
    telegram_id: int
    username: str | None
    title: str
    created_at: datetime





@dataclass(frozen=True)
class DeliveryListItem:
    id: int
    signal_id: int
    status: str
    telegram_id: int
    username: str | None
    match_title: str
    signal_group: str
    sent_at: datetime | None
    created_at: datetime
    error_text: str | None
    user_id: int | None = None
    first_name: str | None = None
    last_name: str | None = None
    signal_status: str | None = None


@dataclass(frozen=True)
class SignalDeliveryListItem:
    id: int
    status: str
    telegram_id: int
    username: str | None
    error_text: str | None
    sent_at: datetime | None


@dataclass(frozen=True)
class SignalDetail:
    item: SignalListItem
    external_match_id: int
    external_tournament_id: int
    source_url: str
    match_start_at: datetime
    match_time: str
    tournament_date: str
    message_text: str | None
    cancel_reason: str | None
    decision_reason: str | None
    decision_trace: list[dict]
    deliveries: list[SignalDeliveryListItem]


@dataclass(frozen=True)
class UserDeliveryListItem:
    id: int
    signal_id: int
    status: str
    match_title: str
    signal_group: str
    sent_at: datetime | None
    error_text: str | None
    created_at: datetime | None = None
    signal_status: str | None = None
    result_status: str | None = None


@dataclass(frozen=True)
class UserDetail:
    item: UserListItem
    deliveries: list[UserDeliveryListItem]
    requests: list[RequestListItem]
    delivery_status_counts: dict[str, int] = field(default_factory=dict)
    signal_group_counts: dict[str, int] = field(default_factory=dict)
    result_status_counts: dict[str, int] = field(default_factory=dict)
    request_status_counts: dict[str, int] = field(default_factory=dict)
    request_kind_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class RequestDetail:
    item: RequestListItem
    payment_details: str | None
    specialist_contact: str | None
    description: str
    updated_at: datetime


@dataclass(frozen=True)
class QualityStatsItem:
    key: str
    title: str
    sent_total: int
    counter: ResultCounter

    @property
    def evaluated(self) -> int:
        return self.counter.total

    @property
    def unrated_sent(self) -> int:
        return max(self.sent_total - self.evaluated, 0)


@dataclass(frozen=True)
class QualitySummary:
    sent_total: int
    overall: QualityStatsItem
    by_group: list[QualityStatsItem] = field(default_factory=list)
    by_level: list[QualityStatsItem] = field(default_factory=list)

@dataclass(frozen=True)
class ImportListItem:
    id: int
    file_name: str
    status: str
    total_rows: int
    parsed_matches: int
    inserted_matches: int
    updated_matches: int
    missing_matches: int
    error_text: str | None
    created_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class ImportDecisionReason:
    reason: str
    count: int


@dataclass(frozen=True)
class ImportDetail:
    batch: ImportListItem
    signals: list[SignalListItem] = field(default_factory=list)
    schedule_warnings: list[SignalListItem] = field(default_factory=list)
    decision_counts: dict[str, int] = field(default_factory=dict)
    rejection_reasons: list[ImportDecisionReason] = field(default_factory=list)
    total_signals: int = 0
    filtered_signals: int = 0
    status_filter: str | None = None
    group_filter: str | None = None
    schedule_filter: str | None = None
    limit: int = 100


@dataclass(frozen=True)
class SystemHealthItem:
    status: str
    title: str
    message: str
    details: str = ""


@dataclass(frozen=True)
class MonitoringSummary:
    dashboard: "DashboardSummary"
    failed_deliveries: list[DeliveryListItem] = field(default_factory=list)
    recent_imports: list[ImportListItem] = field(default_factory=list)
    recent_admin_actions: list[WebAdminActionLog] = field(default_factory=list)
    upcoming_signals: list[SignalListItem] = field(default_factory=list)
    system_checks: list[SystemHealthItem] = field(default_factory=list)
    overdue_signals: int = 0


@dataclass(frozen=True)
class DashboardSummary:
    users_total: int = 0
    users_active: int = 0
    access_trial_active: int = 0
    access_paid_active: int = 0
    matches_total: int = 0
    matches_active: int = 0
    imports_total: int = 0
    signals_by_status: dict[str, int] = field(default_factory=dict)
    deliveries_by_status: dict[str, int] = field(default_factory=dict)
    results_by_status: dict[str, int] = field(default_factory=dict)
    subscription_requests_by_status: dict[str, int] = field(default_factory=dict)
    analysis_requests_by_status: dict[str, int] = field(default_factory=dict)
    latest_import: LatestImportSummary | None = None
    recent_signals: list[RecentSignalSummary] = field(default_factory=list)

    @property
    def signals_total(self) -> int:
        return sum(self.signals_by_status.values())

    @property
    def deliveries_total(self) -> int:
        return sum(self.deliveries_by_status.values())

    @property
    def open_subscription_requests(self) -> int:
        return self.subscription_requests_by_status.get("new", 0)

    @property
    def open_analysis_requests(self) -> int:
        return self.analysis_requests_by_status.get("new", 0)


async def _count(session: AsyncSession, query) -> int:
    return int(await session.scalar(query) or 0)


async def _count_by(session: AsyncSession, column) -> dict[str, int]:
    rows = (await session.execute(select(column, func.count()).group_by(column))).all()
    return {str(key or "unknown"): int(value or 0) for key, value in rows}


async def _delivery_counts_by_signal(session: AsyncSession, signal_ids: list[int]) -> dict[int, dict[str, int]]:
    if not signal_ids:
        return {}
    rows = (
        await session.execute(
            select(SignalDelivery.signal_id, SignalDelivery.status, func.count(SignalDelivery.id))
            .where(SignalDelivery.signal_id.in_(signal_ids))
            .group_by(SignalDelivery.signal_id, SignalDelivery.status)
        )
    ).all()
    counts: dict[int, dict[str, int]] = {}
    for signal_id, status, value in rows:
        counts.setdefault(int(signal_id), {})[str(status or "unknown")] = int(value or 0)
    return counts


async def _delivery_counts_by_user(session: AsyncSession, user_ids: list[int]) -> dict[int, dict[str, int]]:
    if not user_ids:
        return {}
    rows = (
        await session.execute(
            select(SignalDelivery.user_id, SignalDelivery.status, func.count(SignalDelivery.id))
            .where(SignalDelivery.user_id.in_(user_ids))
            .group_by(SignalDelivery.user_id, SignalDelivery.status)
        )
    ).all()
    counts: dict[int, dict[str, int]] = {}
    for user_id, status, value in rows:
        counts.setdefault(int(user_id), {})[str(status or "unknown")] = int(value or 0)
    return counts


def import_list_item_from_batch(batch: ImportBatch) -> ImportListItem:
    return ImportListItem(
        id=batch.id,
        file_name=batch.file_name,
        status=batch.status,
        total_rows=batch.total_rows,
        parsed_matches=batch.parsed_matches,
        inserted_matches=batch.inserted_matches,
        updated_matches=batch.updated_matches,
        missing_matches=batch.missing_matches,
        error_text=batch.error_text,
        created_at=batch.created_at,
        finished_at=batch.finished_at,
    )


def expected_signal_lead_minutes(settings: Any | None = None) -> int:
    settings = settings or get_settings()
    return int(get_signal_rules()["signal"].get("lead_minutes", settings.signal_lead_minutes))


def signal_schedule_lead_minutes(signal: ScheduledSignal, match: Match) -> int | None:
    if signal.send_at is None or match.match_start_at is None:
        return None
    return int(round((match.match_start_at - signal.send_at).total_seconds() / 60))


def signal_schedule_warning(
    signal: ScheduledSignal,
    match: Match,
    *,
    expected_lead_minutes: int,
    now: datetime | None = None,
) -> str | None:
    lead_minutes = signal_schedule_lead_minutes(signal, match)
    if lead_minutes is None:
        return "нет времени матча"
    if lead_minutes <= 0:
        return "отправка позже или в момент матча"
    if signal.status in {"scheduled", "ready"} and signal.send_at < (now or datetime.utcnow()):
        return "просрочен"
    if abs(lead_minutes - expected_lead_minutes) > 1:
        return f"ожидалось {expected_lead_minutes} мин"
    return None


def build_signal_list_item(
    signal: ScheduledSignal,
    match: Match,
    result: SignalResult | None,
    *,
    delivery_counts: dict[int, dict[str, int]] | None = None,
    expected_lead_minutes: int | None = None,
    now: datetime | None = None,
) -> SignalListItem:
    delivery_counts = delivery_counts or {}
    expected = expected_lead_minutes if expected_lead_minutes is not None else expected_signal_lead_minutes()
    return SignalListItem(
        id=signal.id,
        status=signal.status,
        send_at=signal.send_at,
        signal_group=str((signal.signal_payload or {}).get("signal_group") or "unknown"),
        level=(signal.signal_payload or {}).get("level"),
        side=(signal.signal_payload or {}).get("side"),
        player_1=match.player_1,
        player_2=match.player_2,
        result_status=result.status if result is not None else None,
        sent_deliveries=delivery_counts.get(signal.id, {}).get("sent", 0),
        failed_deliveries=delivery_counts.get(signal.id, {}).get("failed", 0),
        match_start_at=match.match_start_at,
        lead_minutes=signal_schedule_lead_minutes(signal, match),
        schedule_warning=signal_schedule_warning(signal, match, expected_lead_minutes=expected, now=now),
    )


def directory_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


def collect_maintenance_summary(settings: Any) -> MaintenanceSummary:
    database_path = sqlite_database_path(settings.database_url)
    backups = tuple(list_sqlite_backups(settings.data_dir))
    latest_backup = backups[0] if backups else latest_sqlite_backup(settings.data_dir)
    return MaintenanceSummary(
        database_path=str(database_path) if database_path is not None else None,
        database_size_bytes=database_path.stat().st_size if database_path is not None and database_path.exists() else 0,
        data_size_bytes=directory_size_bytes(settings.data_dir),
        uploads_size_bytes=directory_size_bytes(settings.uploads_dir),
        latest_backup_path=str(latest_backup.path) if latest_backup is not None else None,
        latest_backup_size_bytes=latest_backup.size_bytes if latest_backup is not None else None,
        latest_backup_created_at=latest_backup.created_at if latest_backup is not None else None,
        sqlite_backup_enabled=bool(settings.sqlite_backup_enabled),
        sqlite_backup_interval_hours=int(settings.sqlite_backup_interval_hours),
        sqlite_backup_keep=int(settings.sqlite_backup_keep),
        backups=backups,
    )


def _payload_value(payload: dict[str, Any] | None, key: str, default: str = 'unknown') -> str:
    if not isinstance(payload, dict):
        return default
    value = payload.get(key)
    return str(value or default)


def _group_title(key: str) -> str:
    return {
        'vip': 'VIP',
        'all': 'Все сигналы',
        'unknown': '\u0411\u0435\u0437 \u0442\u0438\u043f\u0430',
    }.get(key.lower(), key.upper())


def _make_quality_item(key: str, title: str, sent_total: int, rows: list[tuple[dict[str, Any] | None, str | None]]) -> QualityStatsItem:
    return QualityStatsItem(
        key=key,
        title=title,
        sent_total=sent_total,
        counter=summarize_results(rows, total_sent=sent_total).overall if rows else ResultCounter(),
    )


async def collect_quality_summary(session: AsyncSession) -> QualitySummary:
    sent_payload_rows = (
        await session.execute(
            select(ScheduledSignal.signal_payload, Match, SignalDecisionLog.suitable)
            .join(Match, Match.id == ScheduledSignal.match_id)
            .outerjoin(
                SignalDecisionLog,
                (SignalDecisionLog.match_id == ScheduledSignal.match_id)
                & (SignalDecisionLog.import_batch_id == ScheduledSignal.source_import_id),
            )
            .where(ScheduledSignal.status == 'sent')
        )
    ).all()
    sent_payloads = [payload for payload, match, decision_suitable in sent_payload_rows if signal_stats_eligible(match, decision_suitable=decision_suitable)]

    result_rows = [
        (payload, status)
        for payload, status, match, decision_suitable in (
            await session.execute(
                select(ScheduledSignal.signal_payload, SignalResult.status, Match, SignalDecisionLog.suitable)
                .join(Match, Match.id == ScheduledSignal.match_id)
                .join(SignalResult, SignalResult.signal_id == ScheduledSignal.id)
                .outerjoin(
                    SignalDecisionLog,
                    (SignalDecisionLog.match_id == ScheduledSignal.match_id)
                    & (SignalDecisionLog.import_batch_id == ScheduledSignal.source_import_id),
                )
                .where(ScheduledSignal.status == 'sent')
            )
        ).all()
        if signal_stats_eligible(match, decision_suitable=decision_suitable)
    ]

    sent_total = len(sent_payloads)
    overall_summary = summarize_results(result_rows, total_sent=sent_total)
    overall = QualityStatsItem(
        key='overall',
        title='\u0412\u0441\u0435\u0433\u043e',
        sent_total=sent_total,
        counter=overall_summary.overall,
    )

    sent_groups: dict[str, int] = {}
    result_groups: dict[str, list[tuple[dict[str, Any] | None, str | None]]] = {}
    for payload in sent_payloads:
        group = signal_tariff_key(payload)
        sent_groups[group] = sent_groups.get(group, 0) + 1
    for payload, result_status in result_rows:
        group = signal_tariff_key(payload)
        result_groups.setdefault(group, []).append((payload, result_status))

    group_order = list(SIGNAL_TARIFF_ORDER) + sorted(key for key in set(sent_groups) | set(result_groups) if key not in set(SIGNAL_TARIFF_ORDER))
    by_group = [
        _make_quality_item(key, signal_tariff_title(key), sent_groups.get(key, 0), result_groups.get(key, []))
        for key in group_order
        if sent_groups.get(key, 0) or result_groups.get(key)
    ]

    sent_levels: dict[str, int] = {}
    result_levels: dict[str, list[tuple[dict[str, Any] | None, str | None]]] = {}
    for payload in sent_payloads:
        level = _payload_value(payload, 'level', '-')
        sent_levels[level] = sent_levels.get(level, 0) + 1
    for payload, result_status in result_rows:
        level = _payload_value(payload, 'level', '-')
        result_levels.setdefault(level, []).append((payload, result_status))

    known_levels = set(sent_levels) | set(result_levels)
    level_order = [level for level in LEVEL_ORDER if level in known_levels]
    level_order.extend(sorted(level for level in known_levels if level not in set(LEVEL_ORDER)))
    by_level = [
        _make_quality_item(level, level, sent_levels.get(level, 0), result_levels.get(level, []))
        for level in level_order
    ]

    return QualitySummary(sent_total=sent_total, overall=overall, by_group=by_group, by_level=by_level)


async def collect_dashboard_summary(session: AsyncSession, *, recent_limit: int = 8) -> DashboardSummary:
    latest_import = await session.scalar(select(ImportBatch).order_by(desc(ImportBatch.id)).limit(1))
    latest_import_summary = None
    if latest_import is not None:
        latest_import_summary = LatestImportSummary(
            id=latest_import.id,
            file_name=latest_import.file_name,
            status=latest_import.status,
            parsed_matches=latest_import.parsed_matches,
            inserted_matches=latest_import.inserted_matches,
            updated_matches=latest_import.updated_matches,
            missing_matches=latest_import.missing_matches,
            created_at=latest_import.created_at,
            finished_at=latest_import.finished_at,
        )

    recent_rows = (
        await session.execute(
            select(ScheduledSignal, Match, SignalResult)
            .join(Match, Match.id == ScheduledSignal.match_id)
            .outerjoin(SignalResult, SignalResult.signal_id == ScheduledSignal.id)
            .order_by(desc(ScheduledSignal.id))
            .limit(recent_limit)
        )
    ).all()
    expected_lead = expected_signal_lead_minutes()
    recent_signals = [
        RecentSignalSummary(
            id=signal.id,
            status=signal.status,
            send_at=signal.send_at,
            signal_group=str((signal.signal_payload or {}).get("signal_group") or "unknown"),
            level=(signal.signal_payload or {}).get("level"),
            side=(signal.signal_payload or {}).get("side"),
            player_1=match.player_1,
            player_2=match.player_2,
            result_status=result.status if result is not None else None,
            match_start_at=match.match_start_at,
            lead_minutes=signal_schedule_lead_minutes(signal, match),
            schedule_warning=signal_schedule_warning(signal, match, expected_lead_minutes=expected_lead),
        )
        for signal, match, result in recent_rows
    ]

    return DashboardSummary(
        users_total=await _count(session, select(func.count(User.id))),
        users_active=await _count(session, select(func.count(User.id)).where(User.is_active.is_(True))),
        access_trial_active=await _count(
            session,
            select(func.count(UserAccess.id)).where(
                UserAccess.status == "active",
                UserAccess.access_type == "trial",
            ),
        ),
        access_paid_active=await _count(
            session,
            select(func.count(UserAccess.id)).where(
                UserAccess.status == "active",
                UserAccess.access_type == "paid",
            ),
        ),
        matches_total=await _count(session, select(func.count(Match.id))),
        matches_active=await _count(
            session,
            select(func.count(Match.id)).where(Match.is_present_in_latest_import.is_(True)),
        ),
        imports_total=await _count(session, select(func.count(ImportBatch.id))),
        signals_by_status=await _count_by(session, ScheduledSignal.status),
        deliveries_by_status=await _count_by(session, SignalDelivery.status),
        results_by_status=await _count_by(session, SignalResult.status),
        subscription_requests_by_status=await _count_by(session, SubscriptionRequest.status),
        analysis_requests_by_status=await _count_by(session, MatchAnalysisRequest.status),
        latest_import=latest_import_summary,
        recent_signals=recent_signals,
    )


def build_system_health_checks(
    dashboard: DashboardSummary,
    recent_imports: list[ImportListItem],
    *,
    failed_delivery_count: int,
    overdue_signals: int,
    settings: Any,
) -> list[SystemHealthItem]:
    checks: list[SystemHealthItem] = []

    if failed_delivery_count:
        checks.append(SystemHealthItem("problem", "Доставки", f"Есть ошибки доставки: {failed_delivery_count}", "Откройте раздел Доставки -> Ошибка."))
    else:
        checks.append(SystemHealthItem("ok", "Доставки", "Ошибок доставки нет."))

    if overdue_signals:
        checks.append(SystemHealthItem("problem", "Очередь сигналов", f"Просроченных сигналов: {overdue_signals}", "Проверьте фонового отправителя и готовые сигналы."))
    else:
        checks.append(SystemHealthItem("ok", "Очередь сигналов", "Просроченных сигналов нет."))

    latest_import = recent_imports[0] if recent_imports else None
    if latest_import is None:
        checks.append(SystemHealthItem("warning", "Импорт", "Загрузок пока нет.", "Загрузите Excel через Telegram-админку."))
    elif latest_import.status == "failed" or latest_import.error_text:
        checks.append(SystemHealthItem("problem", "Импорт", f"Последняя загрузка требует внимания: {latest_import.file_name}", latest_import.error_text or latest_import.status))
    else:
        checks.append(SystemHealthItem("ok", "Импорт", f"Последняя загрузка успешна: {latest_import.file_name}"))

    open_requests = dashboard.open_subscription_requests + dashboard.open_analysis_requests
    if open_requests:
        checks.append(SystemHealthItem("warning", "Заявки", f"Новых заявок: {open_requests}", f"Подписки: {dashboard.open_subscription_requests}, анализ: {dashboard.open_analysis_requests}."))
    else:
        checks.append(SystemHealthItem("ok", "Заявки", "Новых заявок нет."))

    backups = list_sqlite_backups(settings.data_dir)
    if not getattr(settings, "sqlite_backup_enabled", False):
        checks.append(SystemHealthItem("warning", "Backup", "Авто-backup выключен."))
    elif not backups:
        checks.append(SystemHealthItem("warning", "Backup", "Backup-копий пока нет.", "Создайте backup в разделе Обслуживание."))
    else:
        latest_backup = backups[0]
        max_age = timedelta(hours=max(1, int(settings.sqlite_backup_interval_hours)) * 2)
        backup_age = datetime.now() - latest_backup.created_at
        verification = verify_sqlite_backup(latest_backup.path)
        if not verification.ok:
            checks.append(SystemHealthItem("problem", "Backup", "Последний backup не прошел проверку.", verification.message))
        elif backup_age > max_age:
            checks.append(SystemHealthItem("warning", "Backup", f"Последний backup старше {int(max_age.total_seconds() // 3600)} ч.", latest_backup.path.name))
        else:
            checks.append(SystemHealthItem("ok", "Backup", "Последний backup читается.", latest_backup.path.name))

    return checks


async def collect_monitoring_summary(session: AsyncSession, *, limit: int = 10, settings: Any | None = None) -> MonitoringSummary:
    settings = settings or get_settings()
    runtime = await get_system_runtime_settings(session, settings)
    settings = apply_system_runtime_settings(settings, runtime)
    dashboard = await collect_dashboard_summary(session, recent_limit=limit)
    failed_deliveries = await collect_delivery_list(session, status_filter="failed", limit=limit)
    now = datetime.utcnow()
    overdue_signals = await _count(
        session,
        select(func.count(ScheduledSignal.id))
        .where(ScheduledSignal.status.in_(["scheduled", "ready"]))
        .where(ScheduledSignal.send_at < now),
    )
    import_rows = (
        await session.scalars(select(ImportBatch).order_by(desc(ImportBatch.id)).limit(limit))
    ).all()
    recent_imports = [import_list_item_from_batch(item) for item in import_rows]
    recent_admin_actions = list(
        (
            await session.scalars(
                select(WebAdminActionLog).order_by(desc(WebAdminActionLog.id)).limit(limit)
            )
        ).all()
    )
    upcoming_signals = await collect_upcoming_signal_list(session, limit=limit, settings=settings)
    system_checks = build_system_health_checks(
        dashboard,
        recent_imports,
        failed_delivery_count=dashboard.deliveries_by_status.get("failed", 0),
        overdue_signals=overdue_signals,
        settings=settings,
    )
    return MonitoringSummary(
        dashboard=dashboard,
        failed_deliveries=failed_deliveries,
        recent_imports=recent_imports,
        recent_admin_actions=recent_admin_actions,
        upcoming_signals=upcoming_signals,
        system_checks=system_checks,
        overdue_signals=overdue_signals,
    )


async def collect_signal_list(
    session: AsyncSession,
    *,
    status_filter: str | None = None,
    result_filter: str | None = None,
    schedule_filter: str | None = None,
    limit: int = 50,
) -> list[SignalListItem]:
    query = (
        select(ScheduledSignal, Match, SignalResult)
        .join(Match, Match.id == ScheduledSignal.match_id)
        .outerjoin(SignalResult, SignalResult.signal_id == ScheduledSignal.id)
        .order_by(desc(ScheduledSignal.id))
        .limit(limit)
    )
    if status_filter:
        query = query.where(ScheduledSignal.status == status_filter)
    if result_filter == "unrated":
        query = query.where(or_(SignalResult.id.is_(None), SignalResult.status == "unknown"))
    elif result_filter:
        query = query.where(SignalResult.status == result_filter)
    rows = (await session.execute(query)).all()
    signal_ids = [signal.id for signal, _, _ in rows]
    delivery_counts = await _delivery_counts_by_signal(session, signal_ids)

    expected_lead = expected_signal_lead_minutes()
    now = datetime.utcnow()
    items = [
        build_signal_list_item(
            signal,
            match,
            result,
            delivery_counts=delivery_counts,
            expected_lead_minutes=expected_lead,
            now=now,
        )
        for signal, match, result in rows
    ]
    if schedule_filter == "problem":
        return [item for item in items if item.schedule_warning]
    return items


async def collect_import_signal_schedule_warnings(
    session: AsyncSession,
    import_batch_id: int,
    *,
    limit: int = 10,
    settings: Any | None = None,
) -> list[SignalListItem]:
    now = datetime.utcnow()
    rows = (
        await session.execute(
            select(ScheduledSignal, Match, SignalResult)
            .join(Match, Match.id == ScheduledSignal.match_id)
            .outerjoin(SignalResult, SignalResult.signal_id == ScheduledSignal.id)
            .where(ScheduledSignal.source_import_id == import_batch_id)
            .where(ScheduledSignal.status.in_(["scheduled", "ready"]))
            .order_by(ScheduledSignal.send_at.asc())
        )
    ).all()
    signal_ids = [signal.id for signal, _, _ in rows]
    delivery_counts = await _delivery_counts_by_signal(session, signal_ids)
    expected_lead = expected_signal_lead_minutes(settings)
    items = [
        build_signal_list_item(
            signal,
            match,
            result,
            delivery_counts=delivery_counts,
            expected_lead_minutes=expected_lead,
            now=now,
        )
        for signal, match, result in rows
    ]
    return [item for item in items if item.schedule_warning][:limit]


async def collect_upcoming_signal_list(
    session: AsyncSession,
    *,
    limit: int = 10,
    settings: Any | None = None,
) -> list[SignalListItem]:
    now = datetime.utcnow()
    rows = (
        await session.execute(
            select(ScheduledSignal, Match, SignalResult)
            .join(Match, Match.id == ScheduledSignal.match_id)
            .outerjoin(SignalResult, SignalResult.signal_id == ScheduledSignal.id)
            .where(ScheduledSignal.status.in_(["scheduled", "ready"]))
            .order_by(ScheduledSignal.send_at.asc())
            .limit(limit)
        )
    ).all()
    signal_ids = [signal.id for signal, _, _ in rows]
    delivery_counts = await _delivery_counts_by_signal(session, signal_ids)
    expected_lead = expected_signal_lead_minutes(settings)
    return [
        build_signal_list_item(
            signal,
            match,
            result,
            delivery_counts=delivery_counts,
            expected_lead_minutes=expected_lead,
            now=now,
        )
        for signal, match, result in rows
    ]


async def collect_import_detail(
    session: AsyncSession,
    import_batch_id: int,
    *,
    status_filter: str | None = None,
    group_filter: str | None = None,
    schedule_filter: str | None = None,
    limit: int = 100,
) -> ImportDetail | None:
    batch = await session.get(ImportBatch, import_batch_id)
    if batch is None:
        return None

    base_query = (
        select(ScheduledSignal, Match, SignalResult)
        .join(Match, Match.id == ScheduledSignal.match_id)
        .outerjoin(SignalResult, SignalResult.signal_id == ScheduledSignal.id)
        .where(ScheduledSignal.source_import_id == import_batch_id)
        .order_by(ScheduledSignal.send_at.asc())
    )
    if status_filter:
        base_query = base_query.where(ScheduledSignal.status == status_filter)
    rows = (await session.execute(base_query)).all()
    signal_ids = [signal.id for signal, _, _ in rows]
    delivery_counts = await _delivery_counts_by_signal(session, signal_ids)
    expected_lead = expected_signal_lead_minutes()
    now = datetime.utcnow()
    all_signals = [
        build_signal_list_item(
            signal,
            match,
            result,
            delivery_counts=delivery_counts,
            expected_lead_minutes=expected_lead,
            now=now,
        )
        for signal, match, result in rows
    ]
    signals = all_signals
    if group_filter:
        signals = [item for item in signals if item.signal_group == group_filter]
    if schedule_filter == "problem":
        signals = [item for item in signals if item.schedule_warning]
    filtered_signals = len(signals)
    signals = signals[:max(1, int(limit))]

    decision_rows = (
        await session.execute(
            select(SignalDecisionLog.suitable, SignalDecisionLog.reason, func.count(SignalDecisionLog.id))
            .where(SignalDecisionLog.import_batch_id == import_batch_id)
            .group_by(SignalDecisionLog.suitable, SignalDecisionLog.reason)
        )
    ).all()
    decision_counts: Counter[str] = Counter()
    rejection_counter: Counter[str] = Counter()
    for suitable, reason, count in decision_rows:
        value = int(count or 0)
        if suitable:
            decision_counts["accepted"] += value
        else:
            decision_counts["rejected"] += value
            rejection_counter[str(reason or "Без причины")] += value

    rejection_reasons = [
        ImportDecisionReason(reason=reason, count=count)
        for reason, count in sorted(rejection_counter.items(), key=lambda item: (-item[1], item[0]))[:10]
    ]

    return ImportDetail(
        batch=import_list_item_from_batch(batch),
        signals=signals,
        schedule_warnings=[item for item in all_signals if item.schedule_warning],
        decision_counts=dict(decision_counts),
        rejection_reasons=rejection_reasons,
        total_signals=len(all_signals),
        filtered_signals=filtered_signals,
        status_filter=status_filter,
        group_filter=group_filter,
        schedule_filter=schedule_filter,
        limit=max(1, int(limit)),
    )


async def collect_user_list(
    session: AsyncSession,
    *,
    search: str | None = None,
    access_type_filter: str | None = None,
    access_status_filter: str | None = None,
    limit: int = 50,
) -> list[UserListItem]:
    query = (
        select(User, UserAccess)
        .outerjoin(UserAccess, UserAccess.user_id == User.id)
        .order_by(desc(User.id))
        .limit(limit)
    )
    search_text = (search or "").strip()
    if search_text:
        like = f"%{search_text.lower()}%"
        conditions = [
            func.lower(User.username).like(like),
            func.lower(User.first_name).like(like),
            func.lower(User.last_name).like(like),
        ]
        if search_text.isdigit():
            conditions.append(User.telegram_id == int(search_text))
        query = query.where(or_(*conditions))
    if access_type_filter:
        query = query.where(UserAccess.access_type == access_type_filter)
    if access_status_filter:
        query = query.where(UserAccess.status == access_status_filter)
    rows = (await session.execute(query)).all()
    user_ids = [user.id for user, _ in rows]
    delivery_counts = await _delivery_counts_by_user(session, user_ids)

    return [
        UserListItem(
            id=user.id,
            telegram_id=user.telegram_id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            is_active=user.is_active,
            access_type=access.access_type if access is not None else None,
            access_status=access.status if access is not None else None,
            free_signals_remaining=access.free_signals_remaining if access is not None else None,
            signals_remaining=access.signals_remaining if access is not None else None,
            active_until=access.active_until if access is not None else None,
            sent_deliveries=delivery_counts.get(user.id, {}).get("sent", 0),
            failed_deliveries=delivery_counts.get(user.id, {}).get("failed", 0),
        )
        for user, access in rows
    ]


async def collect_delivery_list(
    session: AsyncSession,
    *,
    status_filter: str | None = None,
    search: str | None = None,
    signal_id: int | None = None,
    user_id: int | None = None,
    limit: int = 100,
) -> list[DeliveryListItem]:
    query = (
        select(SignalDelivery, ScheduledSignal, Match, User)
        .join(ScheduledSignal, ScheduledSignal.id == SignalDelivery.signal_id)
        .join(Match, Match.id == ScheduledSignal.match_id)
        .outerjoin(User, User.id == SignalDelivery.user_id)
        .order_by(desc(SignalDelivery.id))
        .limit(limit)
    )
    if status_filter:
        query = query.where(SignalDelivery.status == status_filter)
    if signal_id is not None:
        query = query.where(SignalDelivery.signal_id == signal_id)
    if user_id is not None:
        query = query.where(SignalDelivery.user_id == user_id)
    search_text = (search or "").strip()
    if search_text:
        like = f"%{search_text.lower()}%"
        conditions = [
            func.lower(User.username).like(like),
            func.lower(User.first_name).like(like),
            func.lower(User.last_name).like(like),
            func.lower(Match.player_1).like(like),
            func.lower(Match.player_2).like(like),
        ]
        if search_text.isdigit():
            value = int(search_text)
            conditions.extend([
                SignalDelivery.telegram_id == value,
                SignalDelivery.signal_id == value,
                SignalDelivery.id == value,
            ])
        query = query.where(or_(*conditions))
    rows = (await session.execute(query)).all()
    return [
        DeliveryListItem(
            id=delivery.id,
            signal_id=signal.id,
            status=delivery.status,
            telegram_id=delivery.telegram_id,
            username=user.username if user is not None else None,
            match_title=f"{match.player_1} - {match.player_2}",
            signal_group=str((signal.signal_payload or {}).get("signal_group") or "-"),
            sent_at=delivery.sent_at,
            created_at=delivery.created_at,
            error_text=delivery.error_text,
            user_id=user.id if user is not None else None,
            first_name=user.first_name if user is not None else None,
            last_name=user.last_name if user is not None else None,
            signal_status=signal.status,
        )
        for delivery, signal, match, user in rows
    ]


async def collect_request_list(
    session: AsyncSession,
    *,
    kind_filter: str | None = None,
    status_filter: str | None = None,
    limit: int = 50,
) -> list[RequestListItem]:
    subscription_query = select(SubscriptionRequest).order_by(desc(SubscriptionRequest.id)).limit(limit)
    analysis_query = select(MatchAnalysisRequest).order_by(desc(MatchAnalysisRequest.id)).limit(limit)
    if status_filter:
        subscription_query = subscription_query.where(SubscriptionRequest.status == status_filter)
        analysis_query = analysis_query.where(MatchAnalysisRequest.status == status_filter)

    subscription_rows = []
    analysis_rows = []
    if kind_filter in (None, "subscription"):
        subscription_rows = (await session.scalars(subscription_query)).all()
    if kind_filter in (None, "analysis"):
        analysis_rows = (await session.scalars(analysis_query)).all()

    items = [
        RequestListItem(
            kind="subscription",
            id=request.id,
            status=request.status,
            telegram_id=request.telegram_id,
            username=request.username,
            title=f"{request.plan_title} / {request.plan_description}",
            created_at=request.created_at,
        )
        for request in subscription_rows
    ]
    items.extend(
        RequestListItem(
            kind="analysis",
            id=request.id,
            status=request.status,
            telegram_id=request.telegram_id,
            username=request.username,
            title=request.match_text,
            created_at=request.created_at,
        )
        for request in analysis_rows
    )
    return sorted(items, key=lambda item: item.created_at, reverse=True)[:limit]

async def collect_signal_detail(session: AsyncSession, signal_id: int) -> SignalDetail | None:
    row = (
        await session.execute(
            select(ScheduledSignal, Match, SignalResult)
            .join(Match, Match.id == ScheduledSignal.match_id)
            .outerjoin(SignalResult, SignalResult.signal_id == ScheduledSignal.id)
            .where(ScheduledSignal.id == signal_id)
        )
    ).first()
    if row is None:
        return None

    signal, match, result = row
    delivery_counts = await _delivery_counts_by_signal(session, [signal.id])
    item = build_signal_list_item(signal, match, result, delivery_counts=delivery_counts)
    delivery_rows = (
        await session.execute(
            select(SignalDelivery, User)
            .outerjoin(User, User.id == SignalDelivery.user_id)
            .where(SignalDelivery.signal_id == signal.id)
            .order_by(desc(SignalDelivery.id))
        )
    ).all()
    deliveries = [
        SignalDeliveryListItem(
            id=delivery.id,
            status=delivery.status,
            telegram_id=delivery.telegram_id,
            username=user.username if user is not None else None,
            error_text=delivery.error_text,
            sent_at=delivery.sent_at,
        )
        for delivery, user in delivery_rows
    ]
    decision = await session.scalar(
        select(SignalDecisionLog)
        .where(SignalDecisionLog.match_id == match.id)
        .order_by(desc(SignalDecisionLog.id))
        .limit(1)
    )
    return SignalDetail(
        item=item,
        external_match_id=match.external_match_id,
        external_tournament_id=match.external_tournament_id,
        source_url=match.source_url,
        match_start_at=match.match_start_at,
        match_time=match.match_time,
        tournament_date=match.tournament_date,
        message_text=signal.message_text,
        cancel_reason=signal.cancel_reason,
        decision_reason=decision.reason if decision is not None else None,
        decision_trace=decision.decision_trace if decision is not None else [],
        deliveries=deliveries,
    )


async def collect_user_detail(session: AsyncSession, user_id: int) -> UserDetail | None:
    row = (
        await session.execute(
            select(User, UserAccess)
            .outerjoin(UserAccess, UserAccess.user_id == User.id)
            .where(User.id == user_id)
        )
    ).first()
    if row is None:
        return None

    user, access = row
    delivery_counts = await _delivery_counts_by_user(session, [user.id])
    item = UserListItem(
        id=user.id,
        telegram_id=user.telegram_id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        is_active=user.is_active,
        access_type=access.access_type if access is not None else None,
        access_status=access.status if access is not None else None,
        free_signals_remaining=access.free_signals_remaining if access is not None else None,
        signals_remaining=access.signals_remaining if access is not None else None,
        active_until=access.active_until if access is not None else None,
        sent_deliveries=delivery_counts.get(user.id, {}).get("sent", 0),
        failed_deliveries=delivery_counts.get(user.id, {}).get("failed", 0),
    )
    delivery_rows = (
        await session.execute(
            select(SignalDelivery, ScheduledSignal, Match, SignalResult)
            .join(ScheduledSignal, ScheduledSignal.id == SignalDelivery.signal_id)
            .join(Match, Match.id == ScheduledSignal.match_id)
            .outerjoin(SignalResult, SignalResult.signal_id == ScheduledSignal.id)
            .where(SignalDelivery.user_id == user.id)
            .order_by(desc(SignalDelivery.id))
            .limit(20)
        )
    ).all()
    deliveries = [
        UserDeliveryListItem(
            id=delivery.id,
            signal_id=signal.id,
            status=delivery.status,
            match_title=f"{match.player_1} - {match.player_2}",
            signal_group=str((signal.signal_payload or {}).get("signal_group") or "unknown"),
            sent_at=delivery.sent_at,
            error_text=delivery.error_text,
            created_at=delivery.created_at,
            signal_status=signal.status,
            result_status=result.status if result is not None else None,
        )
        for delivery, signal, match, result in delivery_rows
    ]
    subscriptions = (
        await session.scalars(
            select(SubscriptionRequest).where(SubscriptionRequest.user_id == user.id).order_by(desc(SubscriptionRequest.id)).limit(10)
        )
    ).all()
    analyses = (
        await session.scalars(
            select(MatchAnalysisRequest).where(MatchAnalysisRequest.user_id == user.id).order_by(desc(MatchAnalysisRequest.id)).limit(10)
        )
    ).all()
    requests = [
        RequestListItem(
            kind="subscription",
            id=request.id,
            status=request.status,
            telegram_id=request.telegram_id,
            username=request.username,
            title=f"{request.plan_title} / {request.plan_description}",
            created_at=request.created_at,
        )
        for request in subscriptions
    ]
    requests.extend(
        RequestListItem(
            kind="analysis",
            id=request.id,
            status=request.status,
            telegram_id=request.telegram_id,
            username=request.username,
            title=request.match_text,
            created_at=request.created_at,
        )
        for request in analyses
    )
    sorted_requests = sorted(requests, key=lambda value: value.created_at, reverse=True)
    delivery_status_counts = dict(Counter(delivery.status for delivery in deliveries))
    signal_group_counts = dict(Counter(delivery.signal_group for delivery in deliveries))
    result_status_counts = dict(Counter(delivery.result_status or "unknown" for delivery in deliveries))
    request_status_counts = dict(Counter(request.status for request in sorted_requests))
    request_kind_counts = dict(Counter(request.kind for request in sorted_requests))
    return UserDetail(
        item=item,
        deliveries=deliveries,
        requests=sorted_requests,
        delivery_status_counts=delivery_status_counts,
        signal_group_counts=signal_group_counts,
        result_status_counts=result_status_counts,
        request_status_counts=request_status_counts,
        request_kind_counts=request_kind_counts,
    )


async def collect_request_detail(session: AsyncSession, kind: str, request_id: int) -> RequestDetail | None:
    if kind == "subscription":
        request = await session.get(SubscriptionRequest, request_id)
        if request is None:
            return None
        item = RequestListItem(
            kind="subscription",
            id=request.id,
            status=request.status,
            telegram_id=request.telegram_id,
            username=request.username,
            title=f"{request.plan_title} / {request.plan_description}",
            created_at=request.created_at,
        )
        return RequestDetail(
            item=item,
            payment_details=request.payment_details,
            specialist_contact=request.specialist_contact,
            description=f"{request.plan_id}; price={request.price_rub}; signals={request.signals_limit}; duration_days={request.duration_days}; duration_hours={request.duration_hours}",
            updated_at=request.updated_at,
        )
    if kind == "analysis":
        request = await session.get(MatchAnalysisRequest, request_id)
        if request is None:
            return None
        item = RequestListItem(
            kind="analysis",
            id=request.id,
            status=request.status,
            telegram_id=request.telegram_id,
            username=request.username,
            title=request.match_text,
            created_at=request.created_at,
        )
        return RequestDetail(
            item=item,
            payment_details=request.payment_details,
            specialist_contact=request.specialist_contact,
            description=request.match_text,
            updated_at=request.updated_at,
        )
    return None
