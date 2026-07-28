from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import secrets
from dataclasses import dataclass
import re
from urllib.parse import parse_qs, urlencode
from io import StringIO
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from html import escape
from pathlib import Path
from typing import Annotated, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from aiogram import Bot
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import desc, select

from app.database.models import MatchAnalysisRequest, ScheduledSignal, SignalResult, SubscriptionRequest, User, WebAdminActionLog, WebAdminUser
from app.database.session import SessionFactory, init_db
from app.services.access import disable_access, grant_subscription_access, grant_subscription_plan_access, grant_trial_access
from app.services.signal_sender import process_delivery_now
from app.services.sqlite_backup import BackupVerification, create_sqlite_backup, verify_sqlite_backup
from app.services.bot_settings import (
    ANALYSIS_PAYMENT_DETAILS_KEY,
    ANALYSIS_SPECIALIST_CONTACT_KEY,
    SUBSCRIPTION_PAYMENT_DETAILS_KEY,
    SUBSCRIPTION_SPECIALIST_CONTACT_KEY,
    PaymentConfig,
    SystemRuntimeSettings,
    apply_system_runtime_settings,
    get_analysis_payment_config,
    get_subscription_payment_config,
    get_system_runtime_settings,
    set_bot_setting,
    set_system_runtime_settings,
)
from app.services.subscriptions import SUBSCRIPTION_PLANS, format_price, get_subscription_plan
from app.services.signal_results import AutoResultSummary, auto_update_signal_results, format_winrate, set_signal_result
from app.services.dashboard import (
    DashboardSummary,
    MaintenanceSummary,
    MonitoringSummary,
    ImportDetail,
    SystemHealthItem,
    QualityStatsItem,
    QualitySummary,
    RequestDetail,
    RequestListItem,
    SignalDetail,
    SignalListItem,
    UserDetail,
    UserListItem,
    collect_dashboard_summary,
    collect_delivery_list,
    collect_import_detail,
    collect_maintenance_summary,
    collect_monitoring_summary,
    collect_quality_summary,
    collect_request_detail,
    collect_request_list,
    collect_signal_detail,
    collect_signal_list,
    collect_user_detail,
    collect_user_list,
)
from app.settings import get_settings


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await init_db()
    yield


app = FastAPI(title="Algobet Admin", docs_url=None, redoc_url=None, lifespan=lifespan)
security = HTTPBasic(auto_error=False)
ADMIN_GUIDE_PATH = Path(__file__).resolve().parents[1] / "docs" / "admin-guide.md"


def _auth_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Basic"},
    )


WEB_ADMIN_PASSWORD_ITERATIONS = 210_000
WEB_ADMIN_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")


def hash_web_admin_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, WEB_ADMIN_PASSWORD_ITERATIONS)
    return "$".join([
        "pbkdf2_sha256",
        str(WEB_ADMIN_PASSWORD_ITERATIONS),
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    ])


def verify_web_admin_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations_text))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def verify_env_web_admin_credentials(credentials: HTTPBasicCredentials | None, configured_credentials: dict[str, str]) -> str | None:
    if credentials is None:
        return None
    expected_password = configured_credentials.get(credentials.username)
    if expected_password is None:
        return None
    if not hmac.compare_digest(credentials.password, expected_password):
        return None
    return credentials.username


async def authenticate_web_admin(session, credentials: HTTPBasicCredentials | None) -> str | None:
    if credentials is None:
        return None

    user = await session.scalar(select(WebAdminUser).where(WebAdminUser.username == credentials.username))
    if user is not None and user.is_active and verify_web_admin_password(credentials.password, user.password_hash):
        user.last_login_at = datetime.utcnow()
        user.updated_at = datetime.utcnow()
        await session.commit()
        return user.username

    return verify_env_web_admin_credentials(credentials, get_settings().web_admin_credentials)


async def has_any_web_admin_credentials(session) -> bool:
    db_username = await session.scalar(select(WebAdminUser.username).where(WebAdminUser.is_active.is_(True)).limit(1))
    return db_username is not None or bool(get_settings().web_admin_credentials)


async def is_web_admin_superuser(session, username: str) -> bool:
    user = await session.scalar(select(WebAdminUser).where(WebAdminUser.username == username))
    if user is not None:
        return bool(user.is_active and user.is_super_admin)
    return username in get_settings().web_admin_superusers


async def require_web_admin(credentials: Annotated[HTTPBasicCredentials | None, Depends(security)]) -> str:
    async with SessionFactory() as session:
        if not await has_any_web_admin_credentials(session):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="WEB_ADMIN_USERS or active database web admin is not configured",
            )
        username = await authenticate_web_admin(session, credentials)
    if username is None:
        raise _auth_error()
    return username


async def require_super_admin(actor_username: Annotated[str, Depends(require_web_admin)]) -> str:
    async with SessionFactory() as session:
        if not await is_web_admin_superuser(session, actor_username):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Super admin access required")
    return actor_username


def _token_href(path: str, token: str = "") -> str:
    return path


def _fmt_bytes(value: int | None) -> str:
    if value is None:
        return "-"
    units = ["B", "KB", "MB", "GB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{value} B"


LABELS = {
    "all": "Все",
    "scheduled": "Запланирован",
    "ready": "Готов",
    "sent": "Отправлен",
    "cancelled": "Отменен",
    "failed": "Ошибка",
    "pending": "Ожидает",
    "delivered": "Доставлен",
    "blocked": "Заблокирован",
    "won": "Выиграл",
    "lost": "Проиграл",
    "void": "Возврат",
    "unknown": "Неизвестно",
    "new": "Новая",
    "processing": "В работе",
    "done": "Выполнена",
    "completed": "Завершена",
    "active": "Активен",
    "inactive": "Неактивен",
    "paid": "Платный",
    "trial": "Пробный",
    "vip": "VIP",
    "all_signals": "Все сигналы",
    "subscription": "Подписка",
    "analysis": "Анализ матча",
    "enabled": "Включен",
    "disabled": "Выключен",
    "signal_result_update": "Изменение результата сигнала",
    "request_status_update": "Изменение статуса заявки",
    "user_access_update": "Изменение доступа пользователя",
    "settings_update": "Изменение настроек",
    "maintenance_backup_create": "Создание backup",
    "maintenance_backup_check": "Проверка backup",
    "system_settings_update": "Изменение системных настроек",
    "web_admin_create": "Создание web-админа",
    "web_admin_password_update": "Смена пароля web-админа",
    "web_admin_deactivate": "Отключение web-админа",
    "web_admin_role_update": "Изменение роли web-админа",
    "web_admin": "Web-админ",
    "signal": "Сигнал",
    "subscription_request": "Заявка на подписку",
    "analysis_request": "Заявка на анализ",
    "settings": "Настройки",
}


def _label(value: str | None) -> str:
    if value is None:
        return "-"
    text = str(value)
    return LABELS.get(text.lower(), text)


def _bool_label(value) -> str:
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    return str(value)


SUBSCRIPTION_WEB_STATUSES = ("new", "paid", "done", "cancelled")
ANALYSIS_WEB_STATUSES = ("new", "in_progress", "done", "cancelled")


REQUEST_KIND_FILTER_LABELS = {
    "subscription": "Подписки",
    "analysis": "Анализы",
}
REQUEST_STATUS_FILTER_LABELS = {
    "new": "Новые",
    "paid": "Оплаченные",
    "in_progress": "В работе",
    "done": "Выполненные",
    "cancelled": "Отмененные",
}


def _requests_path(kind_filter: str | None = None, status_filter: str | None = None, *, base: str = "/requests") -> str:
    params = []
    if kind_filter:
        params.append(f"kind={kind_filter}")
    if status_filter:
        params.append(f"status={status_filter}")
    return base + ("?" + "&".join(params) if params else "")


ACCESS_TYPE_FILTER_LABELS = {
    "trial": "Пробные",
    "paid": "Платные",
}
ACCESS_STATUS_FILTER_LABELS = {
    "active": "Активные",
    "disabled": "Отключенные",
}


def _subscriptions_path(access_type_filter: str | None = None, access_status_filter: str | None = None, *, base: str = "/subscriptions") -> str:
    params = []
    if access_type_filter:
        params.append(f"type={access_type_filter}")
    if access_status_filter:
        params.append(f"status={access_status_filter}")
    return base + ("?" + "&".join(params) if params else "")


SIGNAL_RESULT_WEB_STATUSES = ("won", "lost", "void", "unknown")


SIGNAL_RESULT_FILTER_LABELS = {
    "unrated": "Без результата",
    "won": "Зашли",
    "lost": "Не зашли",
    "void": "Возврат",
}

SIGNAL_SCHEDULE_FILTER_LABELS = {
    "problem": "Только проблемы",
}


def _signals_path(
    status_filter: str | None = None,
    result_filter: str | None = None,
    schedule_filter: str | None = None,
    *,
    base: str = "/signals",
) -> str:
    params = []
    if status_filter:
        params.append(f"status={status_filter}")
    if result_filter:
        params.append(f"result={result_filter}")
    if schedule_filter:
        params.append(f"schedule={schedule_filter}")
    return base + ("?" + "&".join(params) if params else "")


def _deliveries_path(
    status_filter: str | None = None,
    search: str | None = None,
    signal_id: int | None = None,
    user_id: int | None = None,
    *,
    base: str = "/deliveries",
) -> str:
    params = []
    if status_filter:
        params.append(("status", status_filter))
    if search:
        params.append(("q", str(search)))
    if signal_id is not None:
        params.append(("signal_id", str(signal_id)))
    if user_id is not None:
        params.append(("user_id", str(user_id)))
    return base + ("?" + urlencode(params) if params else "")


def _import_path(
    import_id: int,
    *,
    status_filter: str | None = None,
    group_filter: str | None = None,
    schedule_filter: str | None = None,
    limit: int | None = None,
    base_suffix: str = "",
) -> str:
    params = []
    if status_filter:
        params.append(f"status={status_filter}")
    if group_filter:
        params.append(f"group={group_filter}")
    if schedule_filter:
        params.append(f"schedule={schedule_filter}")
    if limit:
        params.append(f"limit={limit}")
    base = f"/imports/{import_id}{base_suffix}"
    return base + ("?" + "&".join(params) if params else "")


def _signal_result_buttons(signal_id: int, current_status: str | None) -> str:
    buttons = []
    for next_status in SIGNAL_RESULT_WEB_STATUSES:
        classes = "action-button"
        if next_status == current_status:
            classes += " current"
        buttons.append(
            f'<form method="post" action="/signals/{signal_id}/result/{escape(next_status)}">'
            f'<button class="{classes}" type="submit">{escape(_label(next_status))}</button>'
            "</form>"
        )
    return "".join(buttons)


def _request_status_buttons(kind: str, request_id: int, current_status: str) -> str:
    statuses = SUBSCRIPTION_WEB_STATUSES if kind == "subscription" else ANALYSIS_WEB_STATUSES if kind == "analysis" else ()
    buttons = []
    for next_status in statuses:
        classes = "action-button"
        if next_status == current_status:
            classes += " current"
        if next_status == "cancelled":
            classes += " danger"
        buttons.append(
            f'<form method="post" action="/requests/{escape(kind)}/{request_id}/status/{escape(next_status)}">'
            f'<button class="{classes}" type="submit">{escape(_label(next_status))}</button>'
            "</form>"
        )
    return "".join(buttons)


def _user_access_buttons(user_id: int) -> str:
    quick_actions = "".join([
        f'<form method="post" action="/users/{user_id}/access/trial"><button class="action-button" type="submit">Выдать пробный доступ</button></form>',
        f'<form method="post" action="/users/{user_id}/access/disable"><button class="action-button danger" type="submit">Отключить доступ</button></form>',
    ])
    plan_actions = "".join(
        f'<form method="post" action="/users/{user_id}/access/plan/{escape(plan.id)}">'
        f'<button class="action-button" type="submit">{escape(plan.title)} · {escape(plan.description)} · {escape(format_price(plan.price_rub))}</button>'
        "</form>"
        for plan in SUBSCRIPTION_PLANS
    )
    return f'<div class="actions">{quick_actions}</div><div class="actions">{plan_actions}</div>'


async def log_web_admin_action(
    session,
    *,
    actor_username: str,
    action: str,
    target_type: str,
    target_id: str | int | None = None,
    details: dict | None = None,
) -> WebAdminActionLog:
    log = WebAdminActionLog(
        actor_username=actor_username,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        details=details or {},
    )
    session.add(log)
    await session.flush()
    return log


async def update_web_signal_result(session, signal_id: int, new_status: str, *, actor_username: str | None = None) -> bool:
    if new_status not in SIGNAL_RESULT_WEB_STATUSES:
        raise ValueError("Unknown signal result status")
    signal = await session.get(ScheduledSignal, signal_id)
    if signal is None:
        return False
    old_status = await session.scalar(select(SignalResult.status).where(SignalResult.signal_id == signal_id))
    await set_signal_result(session, signal, new_status, fixed_by_telegram_id=None)
    if actor_username:
        await log_web_admin_action(
            session,
            actor_username=actor_username,
            action="signal_result_update",
            target_type="signal",
            target_id=signal_id,
            details={"old_status": old_status, "new_status": new_status},
        )
        await session.commit()
    return True


async def update_web_request_status(session, kind: str, request_id: int, new_status: str, *, actor_username: str | None = None) -> bool:
    if kind == "subscription":
        if new_status not in SUBSCRIPTION_WEB_STATUSES:
            raise ValueError("Unknown subscription status")
        request = await session.get(SubscriptionRequest, request_id)
        if request is None:
            return False
        old_status = request.status
        request.status = new_status
        request.updated_at = datetime.utcnow()
        if new_status in {"paid", "done"}:
            user = await session.get(User, request.user_id)
            if user is not None:
                await grant_subscription_access(session, user, request)
        if actor_username:
            await log_web_admin_action(
                session,
                actor_username=actor_username,
                action="request_status_update",
                target_type="subscription_request",
                target_id=request_id,
                details={"old_status": old_status, "new_status": new_status},
            )
        await session.commit()
        return True
    if kind == "analysis":
        if new_status not in ANALYSIS_WEB_STATUSES:
            raise ValueError("Unknown analysis status")
        request = await session.get(MatchAnalysisRequest, request_id)
        if request is None:
            return False
        old_status = request.status
        request.status = new_status
        request.updated_at = datetime.utcnow()
        if actor_username:
            await log_web_admin_action(
                session,
                actor_username=actor_username,
                action="request_status_update",
                target_type="analysis_request",
                target_id=request_id,
                details={"old_status": old_status, "new_status": new_status},
            )
        await session.commit()
        return True
    raise ValueError("Unknown request kind")


async def update_web_user_access(session, user_id: int, action: str, plan_id: str | None = None, *, actor_username: str | None = None) -> bool:
    user = await session.get(User, user_id)
    if user is None:
        return False

    old_access = await collect_user_detail(session, user_id)
    old_item = old_access.item if old_access is not None else None

    if action == "trial":
        await grant_trial_access(session, user)
    elif action == "disable":
        await disable_access(session, user)
    elif action == "plan":
        if plan_id is None:
            raise ValueError("Subscription plan is required")
        plan = get_subscription_plan(plan_id)
        if plan is None:
            raise ValueError("Unknown subscription plan")
        await grant_subscription_plan_access(session, user, plan)
    else:
        raise ValueError("Unknown user access action")

    user.updated_at = datetime.utcnow()
    if actor_username:
        await log_web_admin_action(
            session,
            actor_username=actor_username,
            action="user_access_update",
            target_type="user",
            target_id=user_id,
            details={
                "action": action,
                "plan_id": plan_id,
                "old_access_type": old_item.access_type if old_item is not None else None,
                "old_access_status": old_item.access_status if old_item is not None else None,
            },
        )
    await session.commit()
    return True


def _fmt_counts(counts: dict[str, int]) -> str:
    if not counts:
        return '<span class="muted">нет данных</span>'
    return "".join(
        f'<span class="pill"><b>{escape(_label(str(key)))}</b> {value}</span>'
        for key, value in sorted(counts.items())
    )


def _local_dt(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo(get_settings().timezone))


def _fmt_dt(value) -> str:
    local = _local_dt(value)
    if local is None:
        return "-"
    return escape(local.strftime("%d.%m.%Y %H:%M"))


def _fmt_minutes(value: int | None) -> str:
    return "-" if value is None else f"{value} мин"


def _schedule_control_label(signal: SignalListItem) -> str:
    if signal.schedule_warning:
        return f'<span class="pill danger"><b>Внимание</b> {escape(signal.schedule_warning)}</span>'
    return '<span class="pill"><b>OK</b></span>'


def _user_name(user: UserListItem | RequestListItem) -> str:
    username = f"@{user.username}" if user.username else ""
    if isinstance(user, UserListItem):
        full_name = " ".join(part for part in [user.first_name, user.last_name] if part)
        return escape(username or full_name or str(user.telegram_id))
    return escape(username or str(user.telegram_id))


@dataclass(frozen=True)
class WebAdminActivityItem:
    username: str
    configured: bool
    last_action: str | None = None
    last_action_at: datetime | None = None
    actions_7d: int = 0
    source: str = "WEB_ADMIN_USERS"
    is_active: bool = True
    is_super_admin: bool = False
    last_login_at: datetime | None = None


async def collect_web_admin_activity(session, configured_usernames: list[str]) -> list[WebAdminActivityItem]:
    since = datetime.utcnow() - timedelta(days=7)
    logs = list(
        (
            await session.scalars(
                select(WebAdminActionLog).order_by(desc(WebAdminActionLog.created_at), desc(WebAdminActionLog.id)).limit(10000)
            )
        ).all()
    )
    db_users = list((await session.scalars(select(WebAdminUser).order_by(WebAdminUser.username))).all())
    db_by_name = {user.username: user for user in db_users}
    env_usernames = set(configured_usernames)
    usernames = list(dict.fromkeys([
        *(user.username for user in db_users),
        *configured_usernames,
        *(log.actor_username for log in logs if log.actor_username),
    ]))
    items: list[WebAdminActivityItem] = []
    for username in usernames:
        db_user = db_by_name.get(username)
        user_logs = [log for log in logs if log.actor_username == username]
        latest = user_logs[0] if user_logs else None
        actions_7d = sum(1 for log in user_logs if log.created_at and log.created_at >= since)
        if db_user is not None:
            source = "БД"
            configured = bool(db_user.is_active)
            is_active = bool(db_user.is_active)
            is_super_admin = bool(db_user.is_super_admin)
            last_login_at = db_user.last_login_at
        elif username in env_usernames:
            source = "Аварийный .env"
            configured = True
            is_active = True
            is_super_admin = username in get_settings().web_admin_superusers
            last_login_at = None
        else:
            source = "Журнал"
            configured = False
            is_active = False
            is_super_admin = False
            last_login_at = None
        items.append(
            WebAdminActivityItem(
                username=username,
                configured=configured,
                last_action=latest.action if latest is not None else None,
                last_action_at=latest.created_at if latest is not None else None,
                actions_7d=actions_7d,
                source=source,
                is_active=is_active,
                is_super_admin=is_super_admin,
                last_login_at=last_login_at,
            )
        )
    return items


def _base_html(title: str, body: str, *, token: str = "") -> str:
    nav = "".join(
        f'<a href="{_token_href(path, token)}">{label}</a>'
        for label, path in [
            ("Сводка", "/"),
            ("Сигналы", "/signals"),
            ("Доставки", "/deliveries"),
            ("Статистика", "/quality"),
            ("Мониторинг", "/monitoring"),
            ("Пользователи", "/users"),
            ("Подписки", "/subscriptions"),
            ("Заявки", "/requests"),
            ("Настройки", "/settings"),
            ("Журнал", "/audit"),
            ("Админы", "/admins"),
            ("Система", "/system"),
            ("Обслуживание", "/maintenance"),
            ("Инструкция", "/docs"),
        ]
    )
    refresh_href = _token_href("/", token)
    return f"""
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)} - Algobet Admin</title>
  <style>
    :root {{ color-scheme: light; --bg:#f5f7fb; --panel:#ffffff; --text:#172033; --muted:#687386; --line:#dce3ee; --accent:#1167b1; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:Arial, Helvetica, sans-serif; background:var(--bg); color:var(--text); }}
    header {{ padding:18px 28px 14px; border-bottom:1px solid var(--line); background:var(--panel); display:flex; align-items:center; justify-content:space-between; gap:16px; }}
    h1 {{ margin:0; font-size:24px; line-height:1.2; letter-spacing:0; }}
    h2 {{ margin:0 0 14px; font-size:16px; letter-spacing:0; }}
    main {{ max-width:1180px; margin:0 auto; padding:22px; }}
    nav {{ display:flex; gap:6px; flex-wrap:wrap; margin-top:10px; }}
    nav a, a.button {{ display:inline-flex; align-items:center; justify-content:center; min-height:34px; padding:0 12px; background:#eef4fb; color:var(--accent); text-decoration:none; border-radius:6px; font-weight:700; }}
    a.button {{ background:var(--accent); color:#fff; }}
    .actions {{ display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; }}
    .actions form {{ margin:0; }}
    .action-button {{ min-height:34px; padding:0 12px; border:0; border-radius:6px; background:var(--accent); color:#fff; cursor:pointer; font-weight:700; }}
    .action-button.current {{ background:#d9e4f2; color:var(--text); }}
    .action-button.danger {{ background:#b42318; color:#fff; }}
    .muted {{ color:var(--muted); }}
    .grid {{ display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:12px; margin-bottom:18px; }}
    .metric, section {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; }}
    .metric {{ padding:14px; min-height:94px; }}
    .metric span {{ display:block; color:var(--muted); font-size:13px; margin-bottom:8px; }}
    .metric strong {{ font-size:30px; line-height:1; }}
    .sections {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-bottom:18px; }}
    section {{ padding:16px; margin-bottom:18px; min-width:0; }}
    .pills {{ display:flex; flex-wrap:wrap; gap:8px; }}
    .pill {{ display:inline-flex; gap:7px; align-items:center; border:1px solid var(--line); border-radius:999px; padding:6px 10px; background:#fbfcfe; font-size:13px; }}
    .pill.danger {{ border-color:#fecaca; background:#fef2f2; color:#991b1b; }}
    .health-list {{ display:grid; grid-template-columns:repeat(2, minmax(0,1fr)); gap:10px; }}
    .health-item {{ border:1px solid var(--line); border-radius:8px; padding:12px; background:#fbfcfe; }}
    .health-item strong {{ display:block; margin-bottom:5px; }}
    .health-status {{ display:inline-flex; align-items:center; min-height:24px; padding:0 8px; border-radius:999px; font-size:12px; font-weight:700; margin-bottom:8px; }}
    .health-ok .health-status {{ background:#dcfce7; color:#166534; }}
    .health-warning .health-status {{ background:#fef3c7; color:#92400e; }}
    .health-problem .health-status {{ background:#fee2e2; color:#991b1b; }}
    .details {{ display:grid; grid-template-columns:140px 1fr; gap:8px 12px; margin:0; }}
    dt {{ color:var(--muted); }} dd {{ margin:0; }}
    table {{ width:100%; border-collapse:collapse; font-size:14px; }}
    th, td {{ border-bottom:1px solid var(--line); padding:10px 8px; text-align:left; vertical-align:top; }}
    .table-scroll {{ width:100%; max-width:100%; overflow-x:auto; -webkit-overflow-scrolling:touch; }}
    .table-scroll table {{ min-width:720px; }}
    th {{ color:var(--muted); font-size:12px; text-transform:uppercase; }}
    .filters {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:14px; }}
    .settings-form {{ display:grid; gap:8px; max-width:760px; }}
    .settings-form label {{ color:var(--muted); font-weight:700; font-size:13px; }}
    .settings-form textarea, .settings-form input {{ width:100%; border:1px solid var(--line); border-radius:6px; padding:10px; font:inherit; color:var(--text); background:#fff; }}
    .doc-page {{ display:block; max-width:920px; }}
    .doc-page h1 {{ margin:0 0 16px; font-size:26px; }}
    .doc-page h2 {{ margin:28px 0 12px; padding-top:16px; border-top:1px solid var(--line); font-size:18px; }}
    .doc-page h3 {{ margin:20px 0 10px; font-size:15px; }}
    .doc-page p {{ margin:0 0 12px; line-height:1.55; }}
    .doc-page ul, .doc-page ol {{ margin:0 0 14px 22px; padding:0; line-height:1.55; }}
    .doc-page li {{ margin:5px 0; }}
    .doc-page code {{ padding:2px 5px; background:#f1f5f9; border:1px solid #e2e8f0; border-radius:4px; }}
    .doc-page a {{ color:var(--accent); font-weight:700; }}
    @media (max-width:900px) {{ .grid,.sections {{ grid-template-columns:1fr 1fr; }} }}
    @media (max-width:620px) {{ header {{ align-items:flex-start; flex-direction:column; }} main {{ padding:14px; }} .grid,.sections,.health-list {{ grid-template-columns:1fr; }} .table-scroll table {{ min-width:680px; }} table {{ font-size:13px; }} th.optional,td.optional {{ display:none; }} }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Админка Algobet</h1>
      <div class="muted">Операционная панель</div>
      <nav>{nav}</nav>
    </div>
    <a class="button" href="{refresh_href}">Обновить</a>
  </header>
  <main>{body}</main>
</body>
</html>
"""


def _signal_rows(signals: list[SignalListItem], *, token: str = "") -> str:
    return "".join(
        f"""
        <tr>
          <td><a href="{_token_href(f'/signals/{signal.id}', token)}">#{signal.id}</a></td>
          <td>{escape(_label(signal.status))}</td>
          <td>{_fmt_dt(signal.send_at)}</td>
          <td>{_fmt_dt(signal.match_start_at)}</td>
          <td>{_fmt_minutes(signal.lead_minutes)}</td>
          <td>{_schedule_control_label(signal)}</td>
          <td>{escape(signal.signal_group.upper())}</td>
          <td class="optional">{escape(signal.level or '-')}</td>
          <td>P{signal.side or '-'}</td>
          <td>{escape(signal.player_1)} - {escape(signal.player_2)}</td>
          <td>{escape(_label(signal.result_status))}</td>
          <td class="optional">{signal.sent_deliveries} / {signal.failed_deliveries}</td>
        </tr>
        """
        for signal in signals
    ) or '<tr><td colspan="12" class="muted">Сигналов пока нет.</td></tr>'


def render_dashboard_html(summary: DashboardSummary, *, token: str = "") -> str:
    latest = summary.latest_import
    latest_html = (
        "<p class=\"muted\">Загрузок пока нет.</p>"
        if latest is None
        else f"""
        <dl class="details">
          <dt>Файл</dt><dd>{escape(latest.file_name)}</dd>
          <dt>Статус</dt><dd>{escape(_label(latest.status))}</dd>
          <dt>Обработано</dt><dd>{latest.parsed_matches}</dd>
          <dt>Добавлено / обновлено</dt><dd>{latest.inserted_matches} / {latest.updated_matches}</dd>
          <dt>Пропущено</dt><dd>{latest.missing_matches}</dd>
          <dt>Завершено</dt><dd>{_fmt_dt(latest.finished_at or latest.created_at)}</dd>
        </dl>
        """
    )
    recent_signals = [
        SignalListItem(
            id=signal.id,
            status=signal.status,
            send_at=signal.send_at,
            signal_group=signal.signal_group,
            level=signal.level,
            side=signal.side,
            player_1=signal.player_1,
            player_2=signal.player_2,
            result_status=signal.result_status,
            match_start_at=signal.match_start_at,
            lead_minutes=signal.lead_minutes,
            schedule_warning=signal.schedule_warning,
        )
        for signal in summary.recent_signals
    ]
    body = f"""
    <div class="grid">
      <div class="metric"><span>Пользователи</span><strong>{summary.users_total}</strong><div class="muted">активных {summary.users_active}</div></div>
      <div class="metric"><span>Доступы</span><strong>{summary.access_paid_active}</strong><div class="muted">платных, пробных {summary.access_trial_active}</div></div>
      <div class="metric"><span>Матчи</span><strong>{summary.matches_total}</strong><div class="muted">активных {summary.matches_active}</div></div>
      <div class="metric"><span>Сигналы</span><strong>{summary.signals_total}</strong><div class="muted">доставок {summary.deliveries_total}</div></div>
    </div>
    <div class="sections">
      <section><h2>Последняя загрузка</h2>{latest_html}</section>
      <section><h2>Заявки</h2><div class="pills">{_fmt_counts(summary.subscription_requests_by_status)}{_fmt_counts(summary.analysis_requests_by_status)}</div></section>
      <section><h2>Статусы сигналов</h2><div class="pills">{_fmt_counts(summary.signals_by_status)}</div></section>
      <section><h2>Доставки и результаты</h2><div class="pills">{_fmt_counts(summary.deliveries_by_status)}{_fmt_counts(summary.results_by_status)}</div></section>
    </div>
    <section>
      <h2>Последние сигналы</h2>
      <table><thead><tr><th>ID</th><th>Статус</th><th>Отправка</th><th>Матч</th><th>За сколько</th><th>Контроль</th><th>Группа</th><th class="optional">Уровень</th><th>Сторона</th><th>Игра</th><th>Результат</th><th class="optional">Отправлено / ошибки</th></tr></thead><tbody>{_signal_rows(recent_signals, token=token)}</tbody></table>
    </section>
    """
    return _base_html("Сводка", body, token=token)


def render_signals_html(
    signals: list[SignalListItem],
    *,
    token: str = "",
    status_filter: str | None = None,
    result_filter: str | None = None,
    schedule_filter: str | None = None,
) -> str:
    status_filters = "".join(
        f'<a class="button" href="{_token_href(_signals_path(status, result_filter, schedule_filter), token)}">{label}</a>'
        for label, status in [
            ("Все", None),
            ("Запланировано", "scheduled"),
            ("Готово", "ready"),
            ("Отправлено", "sent"),
            ("Отменено", "cancelled"),
        ]
    )
    result_filters = "".join(
        f'<a class="button" href="{_token_href(_signals_path(status_filter, result, schedule_filter), token)}">{label}</a>'
        for label, result in [
            ("Все результаты", None),
            ("Без результата", "unrated"),
            ("Зашли", "won"),
            ("Не зашли", "lost"),
            ("Возврат", "void"),
        ]
    )
    schedule_filters = "".join(
        f'<a class="button" href="{_token_href(_signals_path(status_filter, result_filter, schedule), token)}">{label}</a>'
        for label, schedule in [("Все расписание", None), ("Только проблемы", "problem")]
    )
    result_title = SIGNAL_RESULT_FILTER_LABELS.get(result_filter or "", "Все результаты")
    schedule_title = SIGNAL_SCHEDULE_FILTER_LABELS.get(schedule_filter or "", "Все расписание")
    body = f"""
    <section>
      <h2>Сигналы: {escape(_label(status_filter or 'all'))} · {escape(result_title)} · {escape(schedule_title)}</h2>
      <div class="filters">{status_filters}</div>
      <div class="filters">{result_filters}</div>
      <div class="filters">{schedule_filters}<a class="button" href="{_token_href(_signals_path(status_filter, result_filter, schedule_filter, base='/signals/export.csv'), token)}">CSV</a></div>
      <table><thead><tr><th>ID</th><th>Статус</th><th>Отправка</th><th>Матч</th><th>За сколько</th><th>Контроль</th><th>Группа</th><th class="optional">Уровень</th><th>Сторона</th><th>Игра</th><th>Результат</th><th class="optional">Доставлено / ошибок</th></tr></thead><tbody>{_signal_rows(signals, token=token)}</tbody></table>
    </section>
    """
    return _base_html("Сигналы", body, token=token)


def render_signals_csv(signals: list[SignalListItem]) -> str:
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "status", "send_at", "match_start_at", "lead_minutes", "schedule_warning", "signal_group", "level", "side", "match", "result", "sent_deliveries", "failed_deliveries"])
    for signal in signals:
        writer.writerow([
            signal.id,
            signal.status,
            _fmt_dt(signal.send_at),
            _fmt_dt(signal.match_start_at),
            signal.lead_minutes if signal.lead_minutes is not None else "",
            signal.schedule_warning or "",
            signal.signal_group,
            signal.level or "",
            signal.side or "",
            f"{signal.player_1} - {signal.player_2}",
            signal.result_status or "",
            signal.sent_deliveries,
            signal.failed_deliveries,
        ])
    return output.getvalue()


def _delivery_retry_action(delivery_id: int, delivery_status: str, status_filter: str | None = None) -> str:
    if delivery_status == "sent":
        return '<span class="muted">-</span>'
    suffix = f"?status={status_filter}" if status_filter else ""
    return (
        f'<form method="post" action="/deliveries/{delivery_id}/retry{suffix}">'
        '<button class="action-button" type="submit">Повторить</button>'
        "</form>"
    )


def _delivery_user_label(delivery: DeliveryListItem) -> str:
    username = f"@{delivery.username}" if delivery.username else ""
    full_name = " ".join(part for part in [delivery.first_name, delivery.last_name] if part)
    return username or full_name or str(delivery.telegram_id)


def _delivery_user_link(delivery: DeliveryListItem, token: str) -> str:
    label = escape(_delivery_user_label(delivery))
    details = f'<div class="muted">{delivery.telegram_id}</div>'
    if delivery.user_id is None:
        return label + details
    href = _token_href(f"/users/{delivery.user_id}", token)
    return f'<a href="{href}">{label}</a>{details}'


def render_deliveries_csv(deliveries) -> str:
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "signal_id", "signal_status", "status", "telegram_id", "username", "name", "signal_group", "match", "created_at", "sent_at", "error"])
    for delivery in deliveries:
        full_name = " ".join(part for part in [delivery.first_name, delivery.last_name] if part)
        writer.writerow([
            delivery.id,
            delivery.signal_id,
            delivery.signal_status or "",
            delivery.status,
            delivery.telegram_id,
            delivery.username or "",
            full_name,
            delivery.signal_group,
            delivery.match_title,
            _fmt_dt(delivery.created_at),
            _fmt_dt(delivery.sent_at),
            delivery.error_text or "",
        ])
    return output.getvalue()


def render_deliveries_html(
    deliveries,
    *,
    token: str = "",
    status_filter: str | None = None,
    search: str | None = None,
    signal_id: int | None = None,
    user_id: int | None = None,
) -> str:
    filters = "".join(
        f'<a class="button" href="{_token_href(_deliveries_path(status, search, signal_id, user_id), token)}">{label}</a>'
        for label, status in [
            ("Все", None),
            ("Отправлено", "sent"),
            ("Ошибка", "failed"),
            ("Ожидает", "pending"),
        ]
    )
    rows = "".join(
        f"""
        <tr>
          <td>#{delivery.id}</td>
          <td><a href="{_token_href(f'/signals/{delivery.signal_id}', token)}">#{delivery.signal_id}</a><div class="muted">{escape(_label(delivery.signal_status))}</div></td>
          <td>{escape(_label(delivery.status))}</td>
          <td>{_delivery_user_link(delivery, token)}</td>
          <td>{escape(delivery.signal_group.upper())}</td>
          <td>{escape(delivery.match_title)}</td>
          <td>{_fmt_dt(delivery.created_at)}</td>
          <td>{_fmt_dt(delivery.sent_at)}</td>
          <td>{escape((delivery.error_text or '-')[:180])}</td>
          <td>{_delivery_retry_action(delivery.id, delivery.status, status_filter)}</td>
        </tr>
        """
        for delivery in deliveries
    ) or '<tr><td colspan="10" class="muted">Доставок пока нет.</td></tr>'
    csv_href = _token_href(_deliveries_path(status_filter, search, signal_id, user_id, base="/deliveries/export.csv"), token)
    search_value = escape(search or "", quote=True)
    signal_value = "" if signal_id is None else str(signal_id)
    user_value = "" if user_id is None else str(user_id)
    body = f"""
    <section><h2>Доставки: {escape(_label(status_filter or 'all'))}</h2>
      <form method="get" action="/deliveries" class="inline-form">
        <input name="q" value="{search_value}" placeholder="Поиск: пользователь, Telegram ID, матч, сигнал">
        <input name="signal_id" value="{signal_value}" inputmode="numeric" placeholder="ID сигнала">
        <input name="user_id" value="{user_value}" inputmode="numeric" placeholder="ID пользователя">
        {f'<input type="hidden" name="status" value="{escape(status_filter, quote=True)}">' if status_filter else ''}
        <button class="action-button" type="submit">Найти</button>
        <a class="button" href="{_token_href('/deliveries', token)}">Сбросить</a>
      </form>
      <div class="filters">{filters}<a class="button" href="{csv_href}">CSV</a></div>
      <table><thead><tr><th>ID</th><th>Сигнал</th><th>Статус</th><th>Пользователь</th><th>Группа</th><th>Матч</th><th>Создано</th><th>Отправлено</th><th>Ошибка</th><th>Действие</th></tr></thead><tbody>{rows}</tbody></table>
    </section>
    """
    return _base_html("Доставки", body, token=token)


def _quality_rows(items: list[QualityStatsItem]) -> str:
    return "".join(
        f"""
        <tr>
          <td>{escape(item.title)}</td>
          <td>{item.sent_total}</td>
          <td>{item.evaluated}</td>
          <td>{item.unrated_sent}</td>
          <td>{item.counter.won}</td>
          <td>{item.counter.lost}</td>
          <td>{item.counter.void}</td>
          <td>{item.counter.unknown}</td>
          <td>{format_winrate(item.counter.winrate)}</td>
        </tr>
        """
        for item in items
    ) or '<tr><td colspan="9" class="muted">Данных пока нет.</td></tr>'


def _quality_table(title: str, items: list[QualityStatsItem]) -> str:
    rows = _quality_rows(items)
    return f"""
    <section><h2>{escape(title)}</h2>
      <table><thead><tr><th>Срез</th><th>Отправлено</th><th>Оценено</th><th>Без результата</th><th>Зашло</th><th>Не зашло</th><th>Возврат</th><th>Неизвестно</th><th>Процент захода</th></tr></thead><tbody>{rows}</tbody></table>
    </section>
    """


def render_quality_html(summary: QualitySummary, *, token: str = "", auto_result: AutoResultSummary | None = None) -> str:
    overall = summary.overall
    auto_message = ""
    if auto_result is not None:
        auto_message = (
            '<p class="muted">Автообновление: '
            f'проверено {auto_result.scanned}, обновлено {auto_result.updated}, '
            f'без изменений {auto_result.unchanged}, без счета {auto_result.no_score}, '
            f'ручных пропущено {auto_result.skipped_manual}.</p>'
        )
    body = f"""
    <section><h2>Статистика качества</h2>
      <div class="stats">
        <div class="metric"><span>Отправлено</span><strong>{overall.sent_total}</strong></div>
        <div class="metric"><span>Оценено</span><strong>{overall.evaluated}</strong></div>
        <div class="metric"><span>Без результата</span><strong>{overall.unrated_sent}</strong></div>
        <div class="metric"><span>Процент захода</span><strong>{format_winrate(overall.counter.winrate)}</strong></div>
      </div>
      <div class="actions"><form method="post" action="/quality/auto-update"><button class="action-button" type="submit">Обновить результаты по счету</button></form></div>
      {auto_message}
    </section>
    {_quality_table('По тарифам', summary.by_group)}
    """
    return _base_html("Статистика", body, token=token)


def render_users_html(users: list[UserListItem], *, token: str = "", search: str = "") -> str:
    search_value = escape(search)
    rows = "".join(
        f"""
        <tr>
          <td><a href="{_token_href(f'/users/{user.id}', token)}">#{user.id}</a></td>
          <td>{user.telegram_id}</td>
          <td>{_user_name(user)}</td>
          <td>{_label('active' if user.is_active else 'inactive')}</td>
          <td>{escape(_label(user.access_type))} / {escape(_label(user.access_status))}</td>
          <td>{user.signals_remaining if user.signals_remaining is not None else '-'}</td>
          <td>{user.free_signals_remaining if user.free_signals_remaining is not None else '-'}</td>
          <td class="optional">{_fmt_dt(user.active_until)}</td>
          <td class="optional">{user.sent_deliveries} / {user.failed_deliveries}</td>
        </tr>
        """
        for user in users
    ) or '<tr><td colspan="9" class="muted">Пользователей пока нет.</td></tr>'
    body = f"""
    <section><h2>Пользователи</h2>
      <form class="actions" method="get" action="/users">
        <input name="search" value="{search_value}" placeholder="ID Telegram, имя пользователя или имя" autocomplete="off">
        <button class="action-button" type="submit">Найти</button>
        <a class="button" href="/users">Сбросить</a>
      </form>
      <table><thead><tr><th>ID</th><th>ID Telegram</th><th>Имя</th><th>Статус</th><th>Доступ</th><th>Платных</th><th>Пробных</th><th class="optional">До</th><th class="optional">Отправлено / ошибки</th></tr></thead><tbody>{rows}</tbody></table>
    </section>
    """
    return _base_html("Пользователи", body, token=token)


def render_subscriptions_html(
    users: list[UserListItem],
    *,
    token: str = "",
    access_type_filter: str | None = None,
    access_status_filter: str | None = None,
) -> str:
    type_filters = "".join(
        f'<a class="button" href="{_token_href(_subscriptions_path(type_value, access_status_filter), token)}">{label}</a>'
        for label, type_value in [("Все типы", None), ("Пробные", "trial"), ("Платные", "paid")]
    )
    status_filters = "".join(
        f'<a class="button" href="{_token_href(_subscriptions_path(access_type_filter, status_value), token)}">{label}</a>'
        for label, status_value in [("Все статусы", None), ("Активные", "active"), ("Отключенные", "disabled")]
    )
    type_title = ACCESS_TYPE_FILTER_LABELS.get(access_type_filter or "", "Все типы")
    status_title = ACCESS_STATUS_FILTER_LABELS.get(access_status_filter or "", "Все статусы")
    rows = "".join(
        f"""
        <tr>
          <td><a href="{_token_href(f'/users/{user.id}', token)}">#{user.id}</a></td>
          <td>{user.telegram_id}</td>
          <td>{_user_name(user)}</td>
          <td>{escape(_label(user.access_type))}</td>
          <td>{escape(_label(user.access_status))}</td>
          <td>{user.signals_remaining if user.signals_remaining is not None else '-'}</td>
          <td>{user.free_signals_remaining if user.free_signals_remaining is not None else '-'}</td>
          <td>{_fmt_dt(user.active_until)}</td>
          <td class="optional">{user.sent_deliveries} / {user.failed_deliveries}</td>
        </tr>
        """
        for user in users
    ) or '<tr><td colspan="9" class="muted">Подписок пока нет.</td></tr>'
    body = f"""
    <section><h2>Подписки: {escape(type_title)} · {escape(status_title)}</h2>
      <div class="filters">{type_filters}</div>
      <div class="filters">{status_filters}<a class="button" href="{_token_href(_subscriptions_path(access_type_filter, access_status_filter, base='/subscriptions/export.csv'), token)}">CSV</a></div>
      <table><thead><tr><th>ID</th><th>ID Telegram</th><th>Имя</th><th>Тип</th><th>Статус</th><th>Платных</th><th>Пробных</th><th>До</th><th class="optional">Отправлено / ошибки</th></tr></thead><tbody>{rows}</tbody></table>
    </section>
    """
    return _base_html("Подписки", body, token=token)


def render_subscriptions_csv(users: list[UserListItem]) -> str:
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id",
        "telegram_id",
        "username",
        "name",
        "access_type",
        "access_status",
        "signals_remaining",
        "free_signals_remaining",
        "active_until",
        "sent_deliveries",
        "failed_deliveries",
    ])
    for user in users:
        full_name = " ".join(part for part in [user.first_name, user.last_name] if part)
        writer.writerow([
            user.id,
            user.telegram_id,
            user.username or "",
            full_name,
            user.access_type or "",
            user.access_status or "",
            user.signals_remaining if user.signals_remaining is not None else "",
            user.free_signals_remaining if user.free_signals_remaining is not None else "",
            _fmt_dt(user.active_until),
            user.sent_deliveries,
            user.failed_deliveries,
        ])
    return output.getvalue()

def render_requests_html(
    requests: list[RequestListItem],
    *,
    token: str = "",
    kind_filter: str | None = None,
    status_filter: str | None = None,
) -> str:
    kind_filters = "".join(
        f'<a class="button" href="{_token_href(_requests_path(kind, status_filter), token)}">{label}</a>'
        for label, kind in [
            ("Все типы", None),
            ("Подписки", "subscription"),
            ("Анализы", "analysis"),
        ]
    )
    status_filters = "".join(
        f'<a class="button" href="{_token_href(_requests_path(kind_filter, status_value), token)}">{label}</a>'
        for label, status_value in [
            ("Все статусы", None),
            ("Новые", "new"),
            ("Оплаченные", "paid"),
            ("В работе", "in_progress"),
            ("Выполненные", "done"),
            ("Отмененные", "cancelled"),
        ]
    )
    kind_title = REQUEST_KIND_FILTER_LABELS.get(kind_filter or "", "Все типы")
    status_title = REQUEST_STATUS_FILTER_LABELS.get(status_filter or "", "Все статусы")
    rows = "".join(
        f"""
        <tr>
          <td>{escape(_label(request.kind))}</td>
          <td><a href="{_token_href(f'/requests/{request.kind}/{request.id}', token)}">#{request.id}</a></td>
          <td>{escape(_label(request.status))}</td>
          <td>{request.telegram_id}</td>
          <td>{_user_name(request)}</td>
          <td>{escape(request.title[:160])}</td>
          <td>{_fmt_dt(request.created_at)}</td>
        </tr>
        """
        for request in requests
    ) or '<tr><td colspan="7" class="muted">Заявок пока нет.</td></tr>'
    body = f"""
    <section><h2>Заявки: {escape(kind_title)} · {escape(status_title)}</h2>
      <div class="filters">{kind_filters}</div>
      <div class="filters">{status_filters}<a class="button" href="{_token_href(_requests_path(kind_filter, status_filter, base='/requests/export.csv'), token)}">CSV</a></div>
      <table><thead><tr><th>Тип</th><th>ID</th><th>Статус</th><th>ID Telegram</th><th>Пользователь</th><th>Название</th><th>Создано</th></tr></thead><tbody>{rows}</tbody></table>
    </section>
    """
    return _base_html("Заявки", body, token=token)


def render_requests_csv(requests: list[RequestListItem]) -> str:
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["kind", "id", "status", "telegram_id", "username", "title", "created_at"])
    for request in requests:
        writer.writerow([
            request.kind,
            request.id,
            request.status,
            request.telegram_id,
            request.username or "",
            request.title,
            _fmt_dt(request.created_at),
        ])
    return output.getvalue()


def render_signal_detail_html(detail: SignalDetail, *, token: str = "") -> str:
    result_actions = _signal_result_buttons(detail.item.id, detail.item.result_status)
    delivery_rows = "".join(
        f"""
        <tr><td>#{delivery.id}</td><td>{escape(_label(delivery.status))}</td><td>{delivery.telegram_id}</td><td>{escape('@' + delivery.username if delivery.username else '-')}</td><td>{_fmt_dt(delivery.sent_at)}</td><td>{escape(delivery.error_text or '-')}</td></tr>
        """
        for delivery in detail.deliveries
    ) or '<tr><td colspan="6" class="muted">Доставок пока нет.</td></tr>'
    trace_rows = "".join(
        f"<tr><td>{escape(str(trace.get('code', '-')))}</td><td>{escape(str(trace.get('label', '-')))}</td><td>{escape(_bool_label(trace.get('passed', '-')))}</td><td>{escape(str(trace.get('actual', '-'))[:180])}</td></tr>"
        for trace in detail.decision_trace
    ) or '<tr><td colspan="4" class="muted">Трассировки решения пока нет.</td></tr>'
    body = f"""
    <section><h2>Сигнал #{detail.item.id}</h2>
      <dl class="details">
        <dt>Статус</dt><dd>{escape(_label(detail.item.status))}</dd>
        <dt>Группа / уровень</dt><dd>{escape(detail.item.signal_group.upper())} / {escape(detail.item.level or '-')}</dd>
        <dt>Сторона</dt><dd>P{detail.item.side or '-'}</dd>
        <dt>Матч</dt><dd>{escape(detail.item.player_1)} - {escape(detail.item.player_2)}</dd>
        <dt>Начало матча</dt><dd>{_fmt_dt(detail.match_start_at)}</dd>
        <dt>Внешние ID</dt><dd>match {detail.external_match_id}, tournament {detail.external_tournament_id}</dd>
        <dt>Результат</dt><dd>{escape(_label(detail.item.result_status))}</dd>
        <dt>Доставки</dt><dd>{detail.item.sent_deliveries} отправлено / {detail.item.failed_deliveries} ошибок</dd>
        <dt>Причина отмены</dt><dd>{escape(detail.cancel_reason or '-')}</dd>
        <dt>Решение</dt><dd>{escape(detail.decision_reason or '-')}</dd>
      </dl>
      <div class="actions">{result_actions}</div>
    </section>
    <section><h2>Текст сигнала</h2><pre>{escape(detail.message_text or '')}</pre></section>
    <section><h2>Доставки</h2><table><thead><tr><th>ID</th><th>Статус</th><th>Telegram</th><th>Пользователь</th><th>Отправлено</th><th>Ошибка</th></tr></thead><tbody>{delivery_rows}</tbody></table></section>
    <section><h2>Трассировка решения</h2><table><thead><tr><th>Код</th><th>Правило</th><th>Пройдено</th><th>Факт</th></tr></thead><tbody>{trace_rows}</tbody></table></section>
    """
    return _base_html(f"Сигнал #{detail.item.id}", body, token=token)


def render_user_detail_html(detail: UserDetail, *, token: str = "") -> str:
    item = detail.item
    deliveries_href = _token_href(_deliveries_path(user_id=item.id), token)
    deliveries_csv_href = _token_href(_deliveries_path(user_id=item.id, base="/deliveries/export.csv"), token)
    failed_href = _token_href(_deliveries_path("failed", user_id=item.id), token)
    delivery_rows = "".join(
        f"""
        <tr>
          <td>#{delivery.id}</td>
          <td><a href="{_token_href(f'/signals/{delivery.signal_id}', token)}">#{delivery.signal_id}</a><div class="muted">{escape(_label(delivery.signal_status))}</div></td>
          <td>{escape(_label(delivery.status))}</td>
          <td>{escape(delivery.signal_group.upper())}</td>
          <td>{escape(delivery.match_title)}</td>
          <td>{escape(_label(delivery.result_status))}</td>
          <td>{_fmt_dt(delivery.created_at)}</td>
          <td>{_fmt_dt(delivery.sent_at)}</td>
          <td>{escape((delivery.error_text or '-')[:180])}</td>
        </tr>
        """
        for delivery in detail.deliveries
    ) or '<tr><td colspan="9" class="muted">Доставок пока нет.</td></tr>'
    request_rows = "".join(
        f"""
        <tr><td>{escape(_label(request.kind))}</td><td><a href="{_token_href(f'/requests/{request.kind}/{request.id}', token)}">#{request.id}</a></td><td>{escape(_label(request.status))}</td><td>{escape(request.title[:160])}</td><td>{_fmt_dt(request.created_at)}</td></tr>
        """
        for request in detail.requests
    ) or '<tr><td colspan="5" class="muted">Заявок пока нет.</td></tr>'
    body = f"""
    <section><h2>Пользователь #{item.id}</h2>
      <dl class="details">
        <dt>ID Telegram</dt><dd>{item.telegram_id}</dd>
        <dt>Имя</dt><dd>{_user_name(item)}</dd>
        <dt>Статус</dt><dd>{_label('active' if item.is_active else 'inactive')}</dd>
        <dt>Доступ</dt><dd>{escape(_label(item.access_type))} / {escape(_label(item.access_status))}</dd>
        <dt>Платных осталось</dt><dd>{item.signals_remaining if item.signals_remaining is not None else '-'}</dd>
        <dt>Пробных осталось</dt><dd>{item.free_signals_remaining if item.free_signals_remaining is not None else '-'}</dd>
        <dt>Доступ до</dt><dd>{_fmt_dt(item.active_until)}</dd>
        <dt>Доставки</dt><dd>{item.sent_deliveries} отправлено / {item.failed_deliveries} ошибок</dd>
      </dl>
      <div class="actions">
        <a class="button" href="{deliveries_href}">Все доставки</a>
        <a class="button" href="{failed_href}">Ошибки доставки</a>
        <a class="button" href="{deliveries_csv_href}">CSV доставок</a>
      </div>
    </section>
    <div class="sections">
      <section><h2>Доставки</h2><div class="pills">{_fmt_counts(detail.delivery_status_counts)}{_fmt_counts(detail.signal_group_counts)}</div></section>
      <section><h2>Результаты сигналов</h2><div class="pills">{_fmt_counts(detail.result_status_counts)}</div></section>
      <section><h2>Заявки</h2><div class="pills">{_fmt_counts(detail.request_kind_counts)}{_fmt_counts(detail.request_status_counts)}</div></section>
      <section><h2>Управление доступом</h2>{_user_access_buttons(item.id)}</section>
    </div>
    <section><h2>Последние доставки</h2><div class="table-scroll"><table><thead><tr><th>ID</th><th>Сигнал</th><th>Доставка</th><th>Группа</th><th>Матч</th><th>Результат</th><th>Создано</th><th>Отправлено</th><th>Ошибка</th></tr></thead><tbody>{delivery_rows}</tbody></table></div></section>
    <section><h2>Заявки</h2><table><thead><tr><th>Тип</th><th>ID</th><th>Статус</th><th>Название</th><th>Создано</th></tr></thead><tbody>{request_rows}</tbody></table></section>
    """
    return _base_html(f"Пользователь #{item.id}", body, token=token)


def render_request_detail_html(detail: RequestDetail, *, token: str = "") -> str:
    item = detail.item
    actions = _request_status_buttons(item.kind, item.id, item.status)
    body = f"""
    <section><h2>Заявка: {escape(_label(item.kind))} #{item.id}</h2>
      <dl class="details">
        <dt>Статус</dt><dd>{escape(_label(item.status))}</dd>
        <dt>Пользователь</dt><dd>{_user_name(item)} / {item.telegram_id}</dd>
        <dt>Название</dt><dd>{escape(item.title)}</dd>
        <dt>Описание</dt><dd>{escape(detail.description)}</dd>
        <dt>Оплата</dt><dd>{escape(detail.payment_details or '-')}</dd>
        <dt>Контакт</dt><dd>{escape(detail.specialist_contact or '-')}</dd>
        <dt>Создано</dt><dd>{_fmt_dt(item.created_at)}</dd>
        <dt>Обновлено</dt><dd>{_fmt_dt(detail.updated_at)}</dd>
      </dl>
      <div class="actions">{actions}</div>
    </section>
    """
    return _base_html(f"Заявка #{item.id}", body, token=token)


def _health_label(status_value: str) -> str:
    return {"ok": "OK", "warning": "Внимание", "problem": "Проблема"}.get(status_value, status_value)


def _health_rows(items: list[SystemHealthItem]) -> str:
    return "".join(
        f"""
        <div class="health-item health-{escape(item.status)}">
          <span class="health-status">{escape(_health_label(item.status))}</span>
          <strong>{escape(item.title)}</strong>
          <div>{escape(item.message)}</div>
          <div class="muted">{escape(item.details or '-')}</div>
        </div>
        """
        for item in items
    ) or '<p class="muted">Проверок пока нет.</p>'


def _decision_reason_rows(reasons) -> str:
    return "".join(
        f"""
        <tr>
          <td>{escape(item.reason)}</td>
          <td>{item.count}</td>
        </tr>
        """
        for item in reasons
    ) or '<tr><td colspan="2" class="muted">Причин отклонения нет.</td></tr>'


def render_import_detail_html(detail: ImportDetail, *, token: str = "") -> str:
    batch = detail.batch
    signal_counts: dict[str, int] = {}
    group_counts: dict[str, int] = {}
    for signal in detail.signals:
        signal_counts[signal.status] = signal_counts.get(signal.status, 0) + 1
        group_counts[signal.signal_group] = group_counts.get(signal.signal_group, 0) + 1
    warning_rows = _signal_rows(detail.schedule_warnings, token=token)
    status_filters = "".join(
        f'<a class="button" href="{_token_href(_import_path(batch.id, status_filter=status, group_filter=detail.group_filter, schedule_filter=detail.schedule_filter, limit=detail.limit), token)}">{label}</a>'
        for label, status in [("Все", None), ("Запланировано", "scheduled"), ("Готово", "ready"), ("Отправлено", "sent"), ("Отменено", "cancelled")]
    )
    group_filters = "".join(
        f'<a class="button" href="{_token_href(_import_path(batch.id, status_filter=detail.status_filter, group_filter=group, schedule_filter=detail.schedule_filter, limit=detail.limit), token)}">{label}</a>'
        for label, group in [("Все группы", None), ("VIP", "vip"), ("ALL", "all"), ("Без типа", "unknown")]
    )
    schedule_filters = "".join(
        f'<a class="button" href="{_token_href(_import_path(batch.id, status_filter=detail.status_filter, group_filter=detail.group_filter, schedule_filter=schedule, limit=detail.limit), token)}">{label}</a>'
        for label, schedule in [("Все расписание", None), ("Только проблемы", "problem")]
    )
    limit_filters = "".join(
        f'<a class="button" href="{_token_href(_import_path(batch.id, status_filter=detail.status_filter, group_filter=detail.group_filter, schedule_filter=detail.schedule_filter, limit=value), token)}">{value}</a>'
        for value in [50, 100, 250, 1000]
    )
    csv_href = _token_href(_import_path(batch.id, status_filter=detail.status_filter, group_filter=detail.group_filter, schedule_filter=detail.schedule_filter, limit=detail.limit, base_suffix="/signals.csv"), token)
    body = f"""
    <section><h2>Импорт #{batch.id}</h2>
      <dl class="details">
        <dt>Файл</dt><dd>{escape(batch.file_name)}</dd>
        <dt>Статус</dt><dd>{escape(_label(batch.status))}</dd>
        <dt>Строк / матчей</dt><dd>{batch.total_rows} / {batch.parsed_matches}</dd>
        <dt>Новых / обновлено / отсутствуют</dt><dd>{batch.inserted_matches} / {batch.updated_matches} / {batch.missing_matches}</dd>
        <dt>Создано</dt><dd>{_fmt_dt(batch.created_at)}</dd>
        <dt>Завершено</dt><dd>{_fmt_dt(batch.finished_at)}</dd>
        <dt>Ошибка</dt><dd>{escape(batch.error_text or '-')}</dd>
      </dl>
      <div class="pills">
        <span class="pill"><b>Сигналов всего</b> {detail.total_signals}</span>
        <span class="pill"><b>Показано</b> {len(detail.signals)} из {detail.filtered_signals}</span>
        <span class="pill"><b>Проблем расписания</b> {len(detail.schedule_warnings)}</span>
        <span class="pill"><b>Принято правилом</b> {detail.decision_counts.get('accepted', 0)}</span>
        <span class="pill"><b>Отклонено правилом</b> {detail.decision_counts.get('rejected', 0)}</span>
      </div>
    </section>
    <section><h2>Фильтры сигналов</h2><div class="filters">{status_filters}</div><div class="filters">{group_filters}</div><div class="filters">{schedule_filters}</div><div class="filters">Показать: {limit_filters}<a class="button" href="{csv_href}">CSV</a></div></section>
    <section><h2>Статусы сигналов</h2><div class="pills">{_fmt_counts(signal_counts)}{_fmt_counts(group_counts)}</div></section>
    <section><h2>Проблемы расписания</h2><table><thead><tr><th>ID</th><th>Статус</th><th>Отправка</th><th>Матч</th><th>За сколько</th><th>Контроль</th><th>Группа</th><th class="optional">Уровень</th><th>Сторона</th><th>Игра</th><th>Результат</th><th class="optional">Доставлено / ошибок</th></tr></thead><tbody>{warning_rows}</tbody></table></section>
    <section><h2>Сигналы загрузки</h2><table><thead><tr><th>ID</th><th>Статус</th><th>Отправка</th><th>Матч</th><th>За сколько</th><th>Контроль</th><th>Группа</th><th class="optional">Уровень</th><th>Сторона</th><th>Игра</th><th>Результат</th><th class="optional">Доставлено / ошибок</th></tr></thead><tbody>{_signal_rows(detail.signals, token=token)}</tbody></table></section>
    <section><h2>Причины отклонения</h2><table><thead><tr><th>Причина</th><th>Количество</th></tr></thead><tbody>{_decision_reason_rows(detail.rejection_reasons)}</tbody></table></section>
    """
    return _base_html(f"Импорт #{batch.id}", body, token=token)


def render_monitoring_html(summary: MonitoringSummary, *, token: str = "") -> str:
    dashboard = summary.dashboard
    latest = dashboard.latest_import
    latest_import_html = (
        '<span class="muted">загрузок пока нет</span>'
        if latest is None
        else f'{escape(latest.file_name)} · {escape(_label(latest.status))} · {_fmt_dt(latest.finished_at or latest.created_at)}'
    )
    failed_rows = "".join(
        f"""
        <tr>
          <td>#{delivery.id}</td>
          <td><a href="{_token_href(f'/signals/{delivery.signal_id}', token)}">#{delivery.signal_id}</a></td>
          <td>{delivery.telegram_id}</td>
          <td>{escape(delivery.match_title)}</td>
          <td>{_fmt_dt(delivery.sent_at or delivery.created_at)}</td>
          <td>{escape((delivery.error_text or '-')[:220])}</td>
        </tr>
        """
        for delivery in summary.failed_deliveries
    ) or '<tr><td colspan="6" class="muted">Ошибок доставки нет.</td></tr>'
    import_rows = "".join(
        f"""
        <tr>
          <td>#{item.id}</td>
          <td><a href="{_token_href(f'/imports/{item.id}', token)}">{escape(item.file_name)}</a></td>
          <td>{escape(_label(item.status))}</td>
          <td>{item.parsed_matches} / {item.total_rows}</td>
          <td>{item.inserted_matches} / {item.updated_matches} / {item.missing_matches}</td>
          <td>{_fmt_dt(item.finished_at or item.created_at)}</td>
          <td>{escape((item.error_text or '-')[:180])}</td>
        </tr>
        """
        for item in summary.recent_imports
    ) or '<tr><td colspan="7" class="muted">Загрузок пока нет.</td></tr>'
    action_rows = "".join(
        f"""
        <tr>
          <td>{_fmt_dt(log.created_at)}</td>
          <td>{escape(log.actor_username)}</td>
          <td>{escape(_label(log.action))}</td>
          <td>{escape(_label(log.target_type))}</td>
          <td>{escape(log.target_id or '-')}</td>
        </tr>
        """
        for log in summary.recent_admin_actions
    ) or '<tr><td colspan="5" class="muted">Действий пока нет.</td></tr>'
    upcoming_rows = _signal_rows(summary.upcoming_signals, token=token)
    body = f"""
    <div class="grid">
      <div class="metric"><span>Очередь сигналов</span><strong>{dashboard.signals_by_status.get('scheduled', 0) + dashboard.signals_by_status.get('ready', 0)}</strong><div class="muted">готовых {dashboard.signals_by_status.get('ready', 0)}, просроченных {summary.overdue_signals}</div></div>
      <div class="metric"><span>Ошибки доставки</span><strong>{dashboard.deliveries_by_status.get('failed', 0)}</strong><div class="muted">всего доставок {dashboard.deliveries_total}</div></div>
      <div class="metric"><span>Открытые заявки</span><strong>{dashboard.open_subscription_requests + dashboard.open_analysis_requests}</strong><div class="muted">подписки {dashboard.open_subscription_requests}, анализ {dashboard.open_analysis_requests}</div></div>
      <div class="metric"><span>Платные доступы</span><strong>{dashboard.access_paid_active}</strong><div class="muted">пробных {dashboard.access_trial_active}</div></div>
    </div>
    <section><h2>Состояние системы</h2><div class="health-list">{_health_rows(summary.system_checks)}</div></section>
    <section><h2>Последняя загрузка</h2><p>{latest_import_html}</p></section>
    <section><h2>Ближайшие отправки</h2><table><thead><tr><th>ID</th><th>Статус</th><th>Отправка</th><th>Матч</th><th>За сколько</th><th>Контроль</th><th>Группа</th><th class="optional">Уровень</th><th>Сторона</th><th>Игра</th><th>Результат</th><th class="optional">Доставлено / ошибок</th></tr></thead><tbody>{upcoming_rows}</tbody></table></section>
    <section><h2>Последние ошибки доставки</h2><table><thead><tr><th>ID</th><th>Сигнал</th><th>ID Telegram</th><th>Матч</th><th>Время</th><th>Ошибка</th></tr></thead><tbody>{failed_rows}</tbody></table></section>
    <section><h2>Последние загрузки Excel</h2><table><thead><tr><th>ID</th><th>Файл</th><th>Статус</th><th>Разобрано / строк</th><th>Добавлено / обновлено / пропущено</th><th>Время</th><th>Ошибка</th></tr></thead><tbody>{import_rows}</tbody></table></section>
    <section><h2>Последние действия админов</h2><table><thead><tr><th>Время</th><th>Админ</th><th>Действие</th><th>Объект</th><th>ID</th></tr></thead><tbody>{action_rows}</tbody></table></section>
    """
    return _base_html("Мониторинг", body, token=token)


def _web_admin_row_actions(item: WebAdminActivityItem, current_username: str | None) -> str:
    if item.source != "БД":
        return '<span class="muted">Управляется вне админки</span>'
    password_form = (
        f'<form method="post" action="/admins/{escape(item.username)}/password" class="inline-form">'
        '<input name="password" type="password" placeholder="Новый пароль" autocomplete="new-password" required minlength="12">'
        '<button class="action-button" type="submit">Сменить пароль</button>'
        '</form>'
    )
    if item.is_active and item.username != current_username:
        deactivate_form = (
            f'<form method="post" action="/admins/{escape(item.username)}/deactivate">'
            '<button class="action-button danger" type="submit">Отключить</button>'
            '</form>'
        )
    elif item.username == current_username:
        deactivate_form = '<span class="muted">Свой доступ не отключаем</span>'
    else:
        deactivate_form = '<span class="muted">Отключен. Задайте новый пароль, чтобы включить.</span>'
    return f'<div class="actions compact">{password_form}{deactivate_form}</div>'


def render_web_admins_html(
    items: list[WebAdminActivityItem],
    *,
    token: str = "",
    current_username: str | None = None,
    message: str = "",
) -> str:
    message_labels = {
        "admin_saved": "Админ сохранен",
        "password_saved": "Пароль обновлен",
        "admin_disabled": "Админ отключен",
    }
    message_text = message_labels.get(message, message)
    message_html = f'<p class="pill"><b>{escape(message_text)}</b></p>' if message_text else ""
    rows = "".join(
        f"""
        <tr>
          <td>{escape(item.username)}</td>
          <td>{'Активен' if item.configured else 'Нет, только в журнале'}</td>
          <td>{escape(item.source)}</td>
          <td>{'Super-admin' if item.is_super_admin else 'Админ'}</td>
          <td>{_fmt_dt(item.last_login_at)}</td>
          <td>{escape(_label(item.last_action))}</td>
          <td>{_fmt_dt(item.last_action_at)}</td>
          <td>{item.actions_7d}</td>
          <td>{_web_admin_row_actions(item, current_username)}</td>
        </tr>
        """
        for item in items
    ) or '<tr><td colspan="9" class="muted">Web-админы не настроены.</td></tr>'
    body = f"""
    <section><h2>Админы</h2>{message_html}
      <p class="muted">Пароли здесь не показываются. Основные web-админы хранятся в базе данных, а WEB_ADMIN_USERS остается аварийным super-admin доступом через .env.</p>
      <table><thead><tr><th>Логин</th><th>Статус</th><th>Источник</th><th>Роль</th><th>Последний вход</th><th>Последнее действие</th><th>Когда</th><th>Действий за 7 дней</th><th>Доступ</th></tr></thead><tbody>{rows}</tbody></table>
    </section>
    <section><h2>Добавить админа</h2>
      <form method="post" action="/admins" class="settings-form">
        <label>Логин</label>
        <input name="username" placeholder="manager" autocomplete="username" required minlength="3" maxlength="64">
        <label>Пароль</label>
        <input name="password" type="password" placeholder="Минимум 12 символов" autocomplete="new-password" required minlength="12">
        <label><input name="is_super_admin" type="checkbox" value="1"> Super-admin: доступ к техническим разделам и управлению админами</label>
        <div class="actions"><button class="action-button" type="submit">Добавить админа</button></div>
      </form>
    </section>
    <section><h2>Аварийный доступ</h2>
      <p>Переменная <code>WEB_ADMIN_USERS</code> в <code>/opt/algobet/.env</code> остается запасным входом на случай проблем с БД.</p>
      <p class="muted">Изменения админов из этой страницы пишутся в <a href="{_token_href('/audit', token)}">Журнал</a>.</p>
    </section>
    """
    return _base_html("Админы", body, token=token)


def render_audit_html(logs: list[WebAdminActionLog], *, token: str = "") -> str:
    rows = "".join(
        f"""
        <tr>
          <td>{_fmt_dt(log.created_at)}</td>
          <td>{escape(log.actor_username)}</td>
          <td>{escape(_label(log.action))}</td>
          <td>{escape(_label(log.target_type))}</td>
          <td>{escape(log.target_id or '-')}</td>
          <td><code>{escape(str(log.details or {}))}</code></td>
        </tr>
        """
        for log in logs
    ) or '<tr><td colspan="6" class="muted">Записей пока нет.</td></tr>'
    body = f"""
    <section><h2>Журнал действий</h2>
      <div class="filters"><a class="button" href="{_token_href('/audit/export.csv', token)}">CSV</a></div>
      <table><thead><tr><th>Время</th><th>Админ</th><th>Действие</th><th>Объект</th><th>ID</th><th>Детали</th></tr></thead><tbody>{rows}</tbody></table>
    </section>
    """
    return _base_html("Журнал", body, token=token)


def render_audit_csv(logs: list[WebAdminActionLog]) -> str:
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["created_at", "actor_username", "action", "target_type", "target_id", "details"])
    for log in logs:
        writer.writerow([
            _fmt_dt(log.created_at),
            log.actor_username,
            log.action,
            log.target_type,
            log.target_id or "",
            str(log.details or {}),
        ])
    return output.getvalue()


@dataclass(frozen=True)
class SystemPageSummary:
    runtime: SystemRuntimeSettings
    database_url: str
    database_path: str | None
    data_dir: str
    uploads_dir: str
    timezone: str
    max_upload_mb: int
    scheduler_interval_seconds: int
    signal_lead_minutes: int
    sqlite_backup_env_enabled: bool
    sqlite_backup_env_interval_hours: int
    sqlite_backup_env_keep: int
    bot_token_configured: bool
    web_admin_users_configured: bool
    web_admin_superusers: tuple[str, ...]
    database_size_bytes: int
    data_size_bytes: int
    uploads_size_bytes: int

def _csv_response(content: str, filename: str) -> Response:
    return Response(
        content=content.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )

def collect_system_page_summary(settings, runtime: SystemRuntimeSettings) -> SystemPageSummary:
    effective = apply_system_runtime_settings(settings, runtime)
    maintenance_summary = collect_maintenance_summary(effective)
    return SystemPageSummary(
        runtime=runtime,
        database_url=settings.database_url,
        database_path=maintenance_summary.database_path,
        data_dir=str(settings.data_dir),
        uploads_dir=str(settings.uploads_dir),
        timezone=settings.timezone,
        max_upload_mb=int(settings.max_upload_mb),
        scheduler_interval_seconds=int(settings.scheduler_interval_seconds),
        signal_lead_minutes=int(settings.signal_lead_minutes),
        sqlite_backup_env_enabled=bool(settings.sqlite_backup_enabled),
        sqlite_backup_env_interval_hours=int(settings.sqlite_backup_interval_hours),
        sqlite_backup_env_keep=int(settings.sqlite_backup_keep),
        bot_token_configured=bool(settings.bot_token),
        web_admin_users_configured=bool(settings.web_admin_credentials),
        web_admin_superusers=tuple(sorted(settings.web_admin_superusers)),
        database_size_bytes=maintenance_summary.database_size_bytes,
        data_size_bytes=maintenance_summary.data_size_bytes,
        uploads_size_bytes=maintenance_summary.uploads_size_bytes,
    )


def _checked(value: bool) -> str:
    return " checked" if value else ""


def render_system_html(summary: SystemPageSummary, *, token: str = "", message: str = "") -> str:
    message_html = f'<p class="pill"><b>{escape(message)}</b></p>' if message else ""
    runtime = summary.runtime
    superusers = ", ".join(summary.web_admin_superusers) or "-"
    body = f"""
    <section><h2>Система</h2>{message_html}
      <p class="muted">Технические параметры доступны только super-admin. Секреты не показываются.</p>
      <dl class="details">
        <dt>Часовой пояс</dt><dd>{escape(summary.timezone)}</dd>
        <dt>БД</dt><dd>{escape(summary.database_path or 'внешняя БД')}</dd>
        <dt>Размер БД</dt><dd>{_fmt_bytes(summary.database_size_bytes)}</dd>
        <dt>Папка data</dt><dd>{escape(summary.data_dir)} · {_fmt_bytes(summary.data_size_bytes)}</dd>
        <dt>Папка uploads</dt><dd>{escape(summary.uploads_dir)} · {_fmt_bytes(summary.uploads_size_bytes)}</dd>
        <dt>Лимит Excel</dt><dd>{summary.max_upload_mb} МБ</dd>
        <dt>Отправка до матча</dt><dd>{summary.signal_lead_minutes} мин</dd>
        <dt>Проверка очереди</dt><dd>{summary.scheduler_interval_seconds} сек</dd>
        <dt>BOT_TOKEN</dt><dd>{'настроен' if summary.bot_token_configured else 'не настроен'}</dd>
        <dt>WEB_ADMIN_USERS</dt><dd>{'настроен' if summary.web_admin_users_configured else 'не настроен'}</dd>
        <dt>Super-admin .env</dt><dd>{escape(superusers)}</dd>
      </dl>
    </section>
    <section><h2>Auto-backup SQLite</h2>
      <form method="post" action="/system/backup" class="settings-form">
        <label><input name="sqlite_backup_enabled" type="checkbox" value="1"{_checked(runtime.sqlite_backup_enabled)}> Включить авто-backup</label>
        <label>Интервал, часов</label>
        <input name="sqlite_backup_interval_hours" type="number" min="1" max="168" value="{runtime.sqlite_backup_interval_hours}">
        <label>Хранить копий</label>
        <input name="sqlite_backup_keep" type="number" min="1" max="60" value="{runtime.sqlite_backup_keep}">
        <div class="actions"><button class="action-button" type="submit">Сохранить backup</button></div>
      </form>
      <p class="muted">Значения из .env: backup {'включен' if summary.sqlite_backup_env_enabled else 'выключен'}, интервал {summary.sqlite_backup_env_interval_hours} ч, хранить {summary.sqlite_backup_env_keep}. Значения выше применяются из БД без редактирования .env.</p>
    </section>
    <section><h2>Только просмотр</h2>
      <p>Критичные параметры вроде <code>BOT_TOKEN</code>, <code>DATABASE_URL</code>, <code>WEB_ADMIN_USERS</code>, порта админки и путей хранения не редактируются из web-интерфейса.</p>
    </section>
    """
    return _base_html("Система", body, token=token)


async def update_web_system_backup_settings(
    session,
    *,
    sqlite_backup_enabled: bool,
    sqlite_backup_interval_hours: str,
    sqlite_backup_keep: str,
    actor_username: str,
) -> SystemRuntimeSettings:
    try:
        interval = int(sqlite_backup_interval_hours)
        keep = int(sqlite_backup_keep)
    except ValueError as exc:
        raise ValueError("Интервал и количество копий должны быть числами") from exc
    runtime = await set_system_runtime_settings(
        session,
        sqlite_backup_enabled=sqlite_backup_enabled,
        sqlite_backup_interval_hours=interval,
        sqlite_backup_keep=keep,
    )
    await log_web_admin_action(
        session,
        actor_username=actor_username,
        action="system_settings_update",
        target_type="settings",
        target_id="system_backup",
        details={
            "sqlite_backup_enabled": runtime.sqlite_backup_enabled,
            "sqlite_backup_interval_hours": runtime.sqlite_backup_interval_hours,
            "sqlite_backup_keep": runtime.sqlite_backup_keep,
        },
    )
    await session.commit()
    return runtime


def render_settings_html(analysis_config: PaymentConfig, subscription_config: PaymentConfig, *, token: str = "", message: str = "") -> str:
    message_html = f'<p class="pill">{escape(message)}</p>' if message else ""
    body = f"""
    <section><h2>Настройки</h2>{message_html}
      <p class="muted">Реквизиты и контакты используются в сообщениях пользователям после создания заявки.</p>
    </section>
    <section><h2>Анализ матча</h2>
      <form method="post" action="/settings/analysis" class="settings-form">
        <label>Реквизиты для оплаты</label>
        <textarea name="payment_details" rows="6">{escape(analysis_config.payment_details)}</textarea>
        <label>Контакт специалиста</label>
        <input name="specialist_contact" value="{escape(analysis_config.specialist_contact)}">
        <div class="actions"><button class="action-button" type="submit">Сохранить анализ</button></div>
      </form>
    </section>
    <section><h2>Подписка</h2>
      <form method="post" action="/settings/subscription" class="settings-form">
        <label>Реквизиты для оплаты</label>
        <textarea name="payment_details" rows="6">{escape(subscription_config.payment_details)}</textarea>
        <label>Контакт специалиста</label>
        <input name="specialist_contact" value="{escape(subscription_config.specialist_contact)}">
        <div class="actions"><button class="action-button" type="submit">Сохранить подписку</button></div>
      </form>
    </section>
    """
    return _base_html("Настройки", body, token=token)


def validate_web_admin_username(username: str) -> str:
    username = username.strip()
    if not WEB_ADMIN_USERNAME_RE.fullmatch(username):
        raise ValueError("Логин должен быть 3-64 символа: латиница, цифры, точка, дефис или подчеркивание")
    return username


def validate_web_admin_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Пароль должен быть не короче 12 символов")
    return password


async def create_or_update_web_admin_user(
    session,
    username: str,
    password: str,
    *,
    actor_username: str,
    is_super_admin: bool = False,
) -> WebAdminUser:
    username = validate_web_admin_username(username)
    password = validate_web_admin_password(password)
    user = await session.scalar(select(WebAdminUser).where(WebAdminUser.username == username))
    now = datetime.utcnow()
    action = "web_admin_create"
    if user is None:
        user = WebAdminUser(
            username=username,
            password_hash=hash_web_admin_password(password),
            is_active=True,
            is_super_admin=is_super_admin,
            created_by=actor_username,
            updated_by=actor_username,
        )
        session.add(user)
    else:
        user.password_hash = hash_web_admin_password(password)
        user.is_active = True
        user.is_super_admin = is_super_admin
        user.updated_by = actor_username
        user.updated_at = now
        action = "web_admin_password_update"
    await log_web_admin_action(
        session,
        actor_username=actor_username,
        action=action,
        target_type="web_admin",
        target_id=username,
        details={"username": username, "is_super_admin": is_super_admin},
    )
    await session.commit()
    return user


async def update_web_admin_password(session, username: str, password: str, *, actor_username: str) -> bool:
    username = validate_web_admin_username(username)
    password = validate_web_admin_password(password)
    user = await session.scalar(select(WebAdminUser).where(WebAdminUser.username == username))
    if user is None:
        return False
    user.password_hash = hash_web_admin_password(password)
    user.is_active = True
    user.updated_by = actor_username
    user.updated_at = datetime.utcnow()
    await log_web_admin_action(
        session,
        actor_username=actor_username,
        action="web_admin_password_update",
        target_type="web_admin",
        target_id=username,
        details={"username": username},
    )
    await session.commit()
    return True


async def deactivate_web_admin_user(session, username: str, *, actor_username: str) -> bool:
    username = validate_web_admin_username(username)
    if username == actor_username:
        raise ValueError("Нельзя отключить свой текущий доступ")
    user = await session.scalar(select(WebAdminUser).where(WebAdminUser.username == username))
    if user is None:
        return False
    user.is_active = False
    user.updated_by = actor_username
    user.updated_at = datetime.utcnow()
    await log_web_admin_action(
        session,
        actor_username=actor_username,
        action="web_admin_deactivate",
        target_type="web_admin",
        target_id=username,
        details={"username": username},
    )
    await session.commit()
    return True


async def update_web_payment_settings(session, section: str, payment_details: str, specialist_contact: str, *, actor_username: str | None = None) -> None:
    if section == "analysis":
        payment_key = ANALYSIS_PAYMENT_DETAILS_KEY
        contact_key = ANALYSIS_SPECIALIST_CONTACT_KEY
    elif section == "subscription":
        payment_key = SUBSCRIPTION_PAYMENT_DETAILS_KEY
        contact_key = SUBSCRIPTION_SPECIALIST_CONTACT_KEY
    else:
        raise ValueError("Unknown settings section")

    await set_bot_setting(session, payment_key, payment_details, max_length=2000)
    await set_bot_setting(session, contact_key, specialist_contact, max_length=255)
    if actor_username:
        await log_web_admin_action(
            session,
            actor_username=actor_username,
            action="settings_update",
            target_type="settings",
            target_id=section,
            details={"section": section, "payment_key": payment_key, "contact_key": contact_key},
        )
    await session.commit()


async def _read_form_fields(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8", errors="replace")
    values = parse_qs(body, keep_blank_values=True)
    return {key: items[-1] if items else "" for key, items in values.items()}


def _backup_rows(summary: MaintenanceSummary) -> str:
    rows = []
    for backup in summary.backups[:10]:
        rows.append(
            f"""
            <tr>
              <td>{escape(backup.path.name)}</td>
              <td>{escape(str(backup.path))}</td>
              <td>{_fmt_bytes(backup.size_bytes)}</td>
              <td>{_fmt_dt(backup.created_at)}</td>
            </tr>
            """
        )
    return "".join(rows) or '<tr><td colspan="4" class="muted">Backup-копий пока нет.</td></tr>'


def _backup_check_message(check: BackupVerification | None) -> str:
    if check is None:
        return ""
    status_text = "Backup исправен" if check.ok else "Backup не прошел проверку"
    return (
        f'<p class="pill"><b>{status_text}</b>: {escape(check.path.name)} · '
        f'{escape(check.message)} · таблиц: {check.table_count}</p>'
    )


def render_maintenance_html(
    summary: MaintenanceSummary,
    *,
    token: str = "",
    message: str = "",
    backup_check: BackupVerification | None = None,
) -> str:
    enabled = _label("enabled" if summary.sqlite_backup_enabled else "disabled")
    message_html = f'<p class="pill"><b>{escape(message)}</b></p>' if message else ""
    check_html = _backup_check_message(backup_check)
    backup_actions = ""
    if summary.database_path:
        backup_actions = """
        <div class="actions">
          <form method="post" action="/maintenance/backup"><button class="action-button" type="submit">Сделать backup сейчас</button></form>
          <form method="post" action="/maintenance/backup/check"><button class="action-button" type="submit">Проверить последний backup</button></form>
        </div>
        """
    body = f"""
    <section><h2>Обслуживание</h2>{message_html}{check_html}
      <dl class="details">
        <dt>База данных</dt><dd>{escape(summary.database_path or '-')}</dd>
        <dt>Размер БД</dt><dd>{_fmt_bytes(summary.database_size_bytes)}</dd>
        <dt>Папка данных</dt><dd>{_fmt_bytes(summary.data_size_bytes)}</dd>
        <dt>Папка загрузок</dt><dd>{_fmt_bytes(summary.uploads_size_bytes)}</dd>
        <dt>Авто-backup</dt><dd>{enabled}</dd>
        <dt>Интервал backup</dt><dd>{summary.sqlite_backup_interval_hours} h</dd>
        <dt>Хранить копий</dt><dd>{summary.sqlite_backup_keep}</dd>
        <dt>Последний backup</dt><dd>{escape(summary.latest_backup_path or '-')}</dd>
        <dt>Размер backup</dt><dd>{_fmt_bytes(summary.latest_backup_size_bytes)}</dd>
        <dt>Дата backup</dt><dd>{_fmt_dt(summary.latest_backup_created_at)}</dd>
      </dl>
      {backup_actions}
    </section>
    <section><h2>Backup SQLite</h2>
      <table><thead><tr><th>Файл</th><th>Путь</th><th>Размер</th><th>Дата</th></tr></thead><tbody>{_backup_rows(summary)}</tbody></table>
    </section>
    """
    return _base_html("Обслуживание", body, token=token)


def _render_inline_markdown(text: str) -> str:
    rendered = escape(text)
    rendered = re.sub(r"`([^`]+)`", r"<code>\1</code>", rendered)
    rendered = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', rendered)
    return rendered


def render_markdown_document(markdown_text: str) -> str:
    html: list[str] = []
    list_type: str | None = None

    def close_list() -> None:
        nonlocal list_type
        if list_type is not None:
            html.append(f"</{list_type}>")
            list_type = None

    for raw_line in markdown_text.splitlines():
        line = raw_line.strip()
        if not line:
            close_list()
            continue

        if line.startswith("### "):
            close_list()
            html.append(f"<h3>{_render_inline_markdown(line[4:])}</h3>")
            continue
        if line.startswith("## "):
            close_list()
            html.append(f"<h2>{_render_inline_markdown(line[3:])}</h2>")
            continue
        if line.startswith("# "):
            close_list()
            html.append(f"<h1>{_render_inline_markdown(line[2:])}</h1>")
            continue

        ordered_match = re.match(r"^\d+\.\s+(.+)$", line)
        if ordered_match:
            if list_type != "ol":
                close_list()
                html.append("<ol>")
                list_type = "ol"
            html.append(f"<li>{_render_inline_markdown(ordered_match.group(1))}</li>")
            continue

        if line.startswith("- "):
            if list_type != "ul":
                close_list()
                html.append("<ul>")
                list_type = "ul"
            html.append(f"<li>{_render_inline_markdown(line[2:])}</li>")
            continue

        close_list()
        html.append(f"<p>{_render_inline_markdown(line)}</p>")

    close_list()
    return "\n".join(html)


def read_admin_guide(path: Path = ADMIN_GUIDE_PATH) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "# Инструкция недоступна\n\nФайл `docs/admin-guide.md` не найден на сервере."


def render_admin_guide_html(markdown_text: str, *, token: str = "") -> str:
    body = f'<section class="doc-page">{render_markdown_document(markdown_text)}</section>'
    return _base_html("Инструкция", body, token=token)


@app.get("/monitoring", response_class=HTMLResponse)
async def monitoring(_: Annotated[None, Depends(require_web_admin)], request: Request) -> HTMLResponse:
    async with SessionFactory() as session:
        summary = await collect_monitoring_summary(session)
    return HTMLResponse(render_monitoring_html(summary, token=""))


@app.get("/system", response_class=HTMLResponse)
async def system_page(_: Annotated[str, Depends(require_super_admin)], request: Request) -> HTMLResponse:
    settings = get_settings()
    async with SessionFactory() as session:
        runtime = await get_system_runtime_settings(session, settings)
    message = "Системные настройки сохранены." if request.query_params.get("saved") else ""
    return HTMLResponse(render_system_html(collect_system_page_summary(settings, runtime), token="", message=message))


@app.post("/system/backup")
async def system_backup_update(request: Request, actor_username: Annotated[str, Depends(require_super_admin)]) -> RedirectResponse:
    fields = await _read_form_fields(request)
    try:
        async with SessionFactory() as session:
            await update_web_system_backup_settings(
                session,
                sqlite_backup_enabled=fields.get("sqlite_backup_enabled") == "1",
                sqlite_backup_interval_hours=fields.get("sqlite_backup_interval_hours", ""),
                sqlite_backup_keep=fields.get("sqlite_backup_keep", ""),
                actor_username=actor_username,
            )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return RedirectResponse(url="/system?saved=1", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admins", response_class=HTMLResponse)
async def web_admins(actor_username: Annotated[str, Depends(require_super_admin)], request: Request) -> HTMLResponse:
    settings = get_settings()
    async with SessionFactory() as session:
        items = await collect_web_admin_activity(session, list(settings.web_admin_credentials.keys()))
    message = request.query_params.get("message") or ""
    return HTMLResponse(render_web_admins_html(items, token="", current_username=actor_username, message=message))


@app.post("/admins")
async def web_admin_create(request: Request, actor_username: Annotated[str, Depends(require_super_admin)]) -> RedirectResponse:
    fields = await _read_form_fields(request)
    try:
        async with SessionFactory() as session:
            await create_or_update_web_admin_user(
                session,
                fields.get("username", ""),
                fields.get("password", ""),
                actor_username=actor_username,
                is_super_admin=fields.get("is_super_admin") == "1",
            )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return RedirectResponse(url="/admins?message=admin_saved", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admins/{username}/password")
async def web_admin_password_update(username: str, request: Request, actor_username: Annotated[str, Depends(require_super_admin)]) -> RedirectResponse:
    fields = await _read_form_fields(request)
    try:
        async with SessionFactory() as session:
            updated = await update_web_admin_password(session, username, fields.get("password", ""), actor_username=actor_username)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Web admin not found")
    return RedirectResponse(url="/admins?message=password_saved", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admins/{username}/deactivate")
async def web_admin_deactivate(username: str, actor_username: Annotated[str, Depends(require_super_admin)]) -> RedirectResponse:
    try:
        async with SessionFactory() as session:
            updated = await deactivate_web_admin_user(session, username, actor_username=actor_username)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Web admin not found")
    return RedirectResponse(url="/admins?message=admin_disabled", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/audit", response_class=HTMLResponse)
async def audit(_: Annotated[str, Depends(require_web_admin)], request: Request) -> HTMLResponse:
    async with SessionFactory() as session:
        logs = (
            await session.scalars(
                select(WebAdminActionLog).order_by(desc(WebAdminActionLog.id)).limit(200)
            )
        ).all()
    return HTMLResponse(render_audit_html(list(logs), token=""))


@app.get("/audit/export.csv")
async def audit_export(_: Annotated[str, Depends(require_web_admin)], request: Request) -> Response:
    async with SessionFactory() as session:
        logs = (
            await session.scalars(
                select(WebAdminActionLog).order_by(desc(WebAdminActionLog.id)).limit(10000)
            )
        ).all()
    content = render_audit_csv(list(logs))
    return _csv_response(content, "algobet-audit.csv")


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(_: Annotated[None, Depends(require_web_admin)], request: Request) -> HTMLResponse:
    async with SessionFactory() as session:
        analysis_config = await get_analysis_payment_config(session)
        subscription_config = await get_subscription_payment_config(session)
    message = request.query_params.get("saved") or ""
    message_text = "Настройки сохранены." if message else ""
    return HTMLResponse(render_settings_html(analysis_config, subscription_config, token="", message=message_text))


@app.post("/settings/{section}")
async def settings_update(
    section: str,
    request: Request,
    actor_username: Annotated[str, Depends(require_web_admin)],
) -> RedirectResponse:
    fields = await _read_form_fields(request)
    try:
        async with SessionFactory() as session:
            await update_web_payment_settings(
                session,
                section,
                fields.get("payment_details", ""),
                fields.get("specialist_contact", ""),
                actor_username=actor_username,
            )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return RedirectResponse(url="/settings?saved=1", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/", response_class=HTMLResponse)
async def dashboard(_: Annotated[None, Depends(require_web_admin)], request: Request) -> HTMLResponse:
    async with SessionFactory() as session:
        summary = await collect_dashboard_summary(session)
    return HTMLResponse(render_dashboard_html(summary, token=""))


@app.get("/signals", response_class=HTMLResponse)
async def signals(_: Annotated[None, Depends(require_web_admin)], request: Request) -> HTMLResponse:
    status_filter = request.query_params.get("status") or None
    result_filter = request.query_params.get("result") or None
    schedule_filter = request.query_params.get("schedule") or None
    if result_filter not in SIGNAL_RESULT_FILTER_LABELS:
        result_filter = None
    if schedule_filter not in SIGNAL_SCHEDULE_FILTER_LABELS:
        schedule_filter = None
    async with SessionFactory() as session:
        rows = await collect_signal_list(session, status_filter=status_filter, result_filter=result_filter, schedule_filter=schedule_filter)
    return HTMLResponse(render_signals_html(rows, token="", status_filter=status_filter, result_filter=result_filter, schedule_filter=schedule_filter))


@app.get("/signals/export.csv")
async def signals_export(_: Annotated[None, Depends(require_web_admin)], request: Request) -> Response:
    status_filter = request.query_params.get("status") or None
    result_filter = request.query_params.get("result") or None
    schedule_filter = request.query_params.get("schedule") or None
    if result_filter not in SIGNAL_RESULT_FILTER_LABELS:
        result_filter = None
    if schedule_filter not in SIGNAL_SCHEDULE_FILTER_LABELS:
        schedule_filter = None
    async with SessionFactory() as session:
        rows = await collect_signal_list(session, status_filter=status_filter, result_filter=result_filter, schedule_filter=schedule_filter, limit=10000)
    content = render_signals_csv(rows)
    return _csv_response(content, "algobet-signals.csv")


def _import_query_filters(request: Request) -> tuple[str | None, str | None, str | None, int]:
    status_filter = request.query_params.get("status") or None
    group_filter = request.query_params.get("group") or None
    schedule_filter = request.query_params.get("schedule") or None
    if status_filter not in {"scheduled", "ready", "sent", "cancelled"}:
        status_filter = None
    if group_filter not in {"vip", "all", "unknown"}:
        group_filter = None
    if schedule_filter not in SIGNAL_SCHEDULE_FILTER_LABELS:
        schedule_filter = None
    try:
        limit = int(request.query_params.get("limit") or 100)
    except ValueError:
        limit = 100
    limit = min(max(limit, 1), 10000)
    return status_filter, group_filter, schedule_filter, limit


@app.get("/imports/{import_id}", response_class=HTMLResponse)
async def import_detail(_: Annotated[None, Depends(require_web_admin)], request: Request, import_id: int) -> HTMLResponse:
    status_filter, group_filter, schedule_filter, limit = _import_query_filters(request)
    async with SessionFactory() as session:
        detail = await collect_import_detail(
            session,
            import_id,
            status_filter=status_filter,
            group_filter=group_filter,
            schedule_filter=schedule_filter,
            limit=limit,
        )
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Import not found")
    return HTMLResponse(render_import_detail_html(detail, token=""))


@app.get("/imports/{import_id}/signals.csv")
async def import_signals_export(_: Annotated[None, Depends(require_web_admin)], request: Request, import_id: int) -> Response:
    status_filter, group_filter, schedule_filter, limit = _import_query_filters(request)
    async with SessionFactory() as session:
        detail = await collect_import_detail(
            session,
            import_id,
            status_filter=status_filter,
            group_filter=group_filter,
            schedule_filter=schedule_filter,
            limit=limit,
        )
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Import not found")
    content = render_signals_csv(detail.signals)
    return _csv_response(content, f"algobet-import-{import_id}-signals.csv")


def _delivery_query_filters(request: Request) -> tuple[str | None, str | None, int | None, int | None]:
    status_filter = request.query_params.get("status") or None
    if status_filter not in {"sent", "failed", "pending"}:
        status_filter = None
    search = request.query_params.get("q") or None
    signal_id = _optional_int(request.query_params.get("signal_id"))
    user_id = _optional_int(request.query_params.get("user_id"))
    return status_filter, search, signal_id, user_id


def _optional_int(value: str | None) -> int | None:
    if value is None or not str(value).strip():
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


@app.get("/deliveries", response_class=HTMLResponse)
async def deliveries(_: Annotated[None, Depends(require_web_admin)], request: Request) -> HTMLResponse:
    status_filter, search, signal_id, user_id = _delivery_query_filters(request)
    async with SessionFactory() as session:
        rows = await collect_delivery_list(session, status_filter=status_filter, search=search, signal_id=signal_id, user_id=user_id)
    return HTMLResponse(render_deliveries_html(rows, token="", status_filter=status_filter, search=search, signal_id=signal_id, user_id=user_id))


@app.get("/deliveries/export.csv")
async def deliveries_export(_: Annotated[None, Depends(require_web_admin)], request: Request) -> Response:
    status_filter, search, signal_id, user_id = _delivery_query_filters(request)
    async with SessionFactory() as session:
        rows = await collect_delivery_list(session, status_filter=status_filter, search=search, signal_id=signal_id, user_id=user_id, limit=10000)
    content = render_deliveries_csv(rows)
    return _csv_response(content, "algobet-deliveries.csv")


@app.post("/deliveries/{delivery_id}/retry")
async def delivery_retry(
    delivery_id: int,
    _: Annotated[None, Depends(require_web_admin)],
    request: Request,
) -> RedirectResponse:
    settings = get_settings()
    bot = Bot(token=settings.bot_token)
    try:
        async with SessionFactory() as session:
            await process_delivery_now(bot, session, delivery_id, admin_ids=settings.admin_ids)
    finally:
        await bot.session.close()
    status_filter = request.query_params.get("status")
    suffix = f"?status={status_filter}" if status_filter else ""
    return RedirectResponse(url=f"/deliveries{suffix}", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/quality", response_class=HTMLResponse)
async def quality(_: Annotated[None, Depends(require_web_admin)], request: Request) -> HTMLResponse:
    async with SessionFactory() as session:
        summary = await collect_quality_summary(session)
    auto_result = None
    if "auto_scanned" in request.query_params:
        auto_result = AutoResultSummary(
            scanned=int(request.query_params.get("auto_scanned") or 0),
            updated=int(request.query_params.get("auto_updated") or 0),
            unchanged=int(request.query_params.get("auto_unchanged") or 0),
            skipped_manual=int(request.query_params.get("auto_skipped_manual") or 0),
            no_score=int(request.query_params.get("auto_no_score") or 0),
        )
    return HTMLResponse(render_quality_html(summary, token="", auto_result=auto_result))


@app.post("/quality/auto-update")
async def quality_auto_update(_: Annotated[None, Depends(require_web_admin)]) -> RedirectResponse:
    async with SessionFactory() as session:
        result = await auto_update_signal_results(session)
        await session.commit()
    params = (
        f"auto_scanned={result.scanned}&auto_updated={result.updated}"
        f"&auto_unchanged={result.unchanged}&auto_skipped_manual={result.skipped_manual}"
        f"&auto_no_score={result.no_score}"
    )
    return RedirectResponse(url=f"/quality?{params}", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/users", response_class=HTMLResponse)
async def users(_: Annotated[None, Depends(require_web_admin)], request: Request) -> HTMLResponse:
    search = (request.query_params.get("search") or "").strip()
    async with SessionFactory() as session:
        rows = await collect_user_list(session, search=search)
    return HTMLResponse(render_users_html(rows, token="", search=search))


@app.get("/subscriptions", response_class=HTMLResponse)
async def subscriptions(_: Annotated[None, Depends(require_web_admin)], request: Request) -> HTMLResponse:
    access_type_filter = request.query_params.get("type") or None
    if access_type_filter not in ACCESS_TYPE_FILTER_LABELS:
        access_type_filter = None
    access_status_filter = request.query_params.get("status") or None
    if access_status_filter not in ACCESS_STATUS_FILTER_LABELS:
        access_status_filter = None
    async with SessionFactory() as session:
        rows = await collect_user_list(
            session,
            access_type_filter=access_type_filter,
            access_status_filter=access_status_filter,
            limit=200,
        )
    return HTMLResponse(
        render_subscriptions_html(
            rows,
            token="",
            access_type_filter=access_type_filter,
            access_status_filter=access_status_filter,
        )
    )


@app.get("/subscriptions/export.csv")
async def subscriptions_export(_: Annotated[None, Depends(require_web_admin)], request: Request) -> Response:
    access_type_filter = request.query_params.get("type") or None
    if access_type_filter not in ACCESS_TYPE_FILTER_LABELS:
        access_type_filter = None
    access_status_filter = request.query_params.get("status") or None
    if access_status_filter not in ACCESS_STATUS_FILTER_LABELS:
        access_status_filter = None
    async with SessionFactory() as session:
        rows = await collect_user_list(
            session,
            access_type_filter=access_type_filter,
            access_status_filter=access_status_filter,
            limit=10000,
        )
    content = render_subscriptions_csv(rows)
    return _csv_response(content, "algobet-subscriptions.csv")

@app.get("/requests", response_class=HTMLResponse)
async def requests(_: Annotated[None, Depends(require_web_admin)], request: Request) -> HTMLResponse:
    kind_filter = request.query_params.get("kind") or None
    if kind_filter not in REQUEST_KIND_FILTER_LABELS:
        kind_filter = None
    status_filter = request.query_params.get("status") or None
    if status_filter not in REQUEST_STATUS_FILTER_LABELS:
        status_filter = None
    async with SessionFactory() as session:
        rows = await collect_request_list(session, kind_filter=kind_filter, status_filter=status_filter)
    return HTMLResponse(render_requests_html(rows, token="", kind_filter=kind_filter, status_filter=status_filter))


@app.get("/requests/export.csv")
async def requests_export(_: Annotated[None, Depends(require_web_admin)], request: Request) -> Response:
    kind_filter = request.query_params.get("kind") or None
    if kind_filter not in REQUEST_KIND_FILTER_LABELS:
        kind_filter = None
    status_filter = request.query_params.get("status") or None
    if status_filter not in REQUEST_STATUS_FILTER_LABELS:
        status_filter = None
    async with SessionFactory() as session:
        rows = await collect_request_list(session, kind_filter=kind_filter, status_filter=status_filter, limit=10000)
    content = render_requests_csv(rows)
    return _csv_response(content, "algobet-requests.csv")


@app.get("/maintenance", response_class=HTMLResponse)
async def maintenance(_: Annotated[None, Depends(require_super_admin)]) -> HTMLResponse:
    base_settings = get_settings()
    async with SessionFactory() as session:
        runtime = await get_system_runtime_settings(session, base_settings)
    summary = collect_maintenance_summary(apply_system_runtime_settings(base_settings, runtime))
    return HTMLResponse(render_maintenance_html(summary))


@app.post("/maintenance/backup", response_class=HTMLResponse)
async def maintenance_backup(actor_username: Annotated[str, Depends(require_super_admin)]) -> HTMLResponse:
    base_settings = get_settings()
    async with SessionFactory() as session:
        runtime = await get_system_runtime_settings(session, base_settings)
    settings = apply_system_runtime_settings(base_settings, runtime)
    try:
        result = create_sqlite_backup(settings.database_url, settings.data_dir, keep=settings.sqlite_backup_keep)
    except (FileNotFoundError, ValueError) as exc:
        summary = collect_maintenance_summary(settings)
        return HTMLResponse(render_maintenance_html(summary, message=f"Ошибка backup: {exc}"), status_code=status.HTTP_400_BAD_REQUEST)
    async with SessionFactory() as session:
        await log_web_admin_action(
            session,
            actor_username=actor_username,
            action="maintenance_backup_create",
            target_type="maintenance",
            target_id=result.created.path.name,
            details={"path": str(result.created.path), "size_bytes": result.created.size_bytes, "deleted": len(result.deleted)},
        )
        await session.commit()
    summary = collect_maintenance_summary(settings)
    message = f"Backup создан: {result.created.path.name} ({_fmt_bytes(result.created.size_bytes)})"
    return HTMLResponse(render_maintenance_html(summary, message=message))


@app.post("/maintenance/backup/check", response_class=HTMLResponse)
async def maintenance_backup_check(actor_username: Annotated[str, Depends(require_super_admin)]) -> HTMLResponse:
    base_settings = get_settings()
    async with SessionFactory() as session:
        runtime = await get_system_runtime_settings(session, base_settings)
    settings = apply_system_runtime_settings(base_settings, runtime)
    backups = list(summary_backup for summary_backup in collect_maintenance_summary(settings).backups)
    if not backups:
        summary = collect_maintenance_summary(settings)
        return HTMLResponse(render_maintenance_html(summary, message="Backup-копий пока нет."), status_code=status.HTTP_400_BAD_REQUEST)
    check = verify_sqlite_backup(backups[0].path)
    async with SessionFactory() as session:
        await log_web_admin_action(
            session,
            actor_username=actor_username,
            action="maintenance_backup_check",
            target_type="maintenance",
            target_id=backups[0].path.name,
            details={"path": str(backups[0].path), "ok": check.ok, "message": check.message, "table_count": check.table_count},
        )
        await session.commit()
    summary = collect_maintenance_summary(settings)
    return HTMLResponse(render_maintenance_html(summary, backup_check=check), status_code=status.HTTP_200_OK if check.ok else status.HTTP_400_BAD_REQUEST)


@app.get("/docs", response_class=HTMLResponse)
async def admin_docs(_: Annotated[None, Depends(require_web_admin)]) -> HTMLResponse:
    return HTMLResponse(render_admin_guide_html(read_admin_guide()))


@app.post("/signals/{signal_id}/result/{result_status}")
async def signal_result_update(
    signal_id: int,
    result_status: str,
    actor_username: Annotated[str, Depends(require_web_admin)],
) -> RedirectResponse:
    async with SessionFactory() as session:
        updated = await update_web_signal_result(session, signal_id, result_status, actor_username=actor_username)
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Signal not found")
    return RedirectResponse(url=f"/signals/{signal_id}", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/signals/{signal_id}", response_class=HTMLResponse)
async def signal_detail(signal_id: int, _: Annotated[None, Depends(require_web_admin)], request: Request) -> HTMLResponse:
    async with SessionFactory() as session:
        detail = await collect_signal_detail(session, signal_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Signal not found")
    return HTMLResponse(render_signal_detail_html(detail, token=""))


@app.get("/users/{user_id}", response_class=HTMLResponse)
async def user_detail(user_id: int, _: Annotated[None, Depends(require_web_admin)], request: Request) -> HTMLResponse:
    async with SessionFactory() as session:
        detail = await collect_user_detail(session, user_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return HTMLResponse(render_user_detail_html(detail, token=""))


@app.post("/users/{user_id}/access/plan/{plan_id}")
async def user_access_plan_update(
    user_id: int,
    plan_id: str,
    actor_username: Annotated[str, Depends(require_web_admin)],
) -> RedirectResponse:
    try:
        async with SessionFactory() as session:
            updated = await update_web_user_access(session, user_id, "plan", plan_id=plan_id, actor_username=actor_username)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return RedirectResponse(url=f"/users/{user_id}", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/users/{user_id}/access/{action}")
async def user_access_update(
    user_id: int,
    action: str,
    actor_username: Annotated[str, Depends(require_web_admin)],
) -> RedirectResponse:
    try:
        async with SessionFactory() as session:
            updated = await update_web_user_access(session, user_id, action, actor_username=actor_username)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return RedirectResponse(url=f"/users/{user_id}", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/requests/{kind}/{request_id}", response_class=HTMLResponse)
async def request_detail(kind: str, request_id: int, _: Annotated[None, Depends(require_web_admin)], request: Request) -> HTMLResponse:
    async with SessionFactory() as session:
        detail = await collect_request_detail(session, kind, request_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    return HTMLResponse(render_request_detail_html(detail, token=""))


@app.post("/requests/{kind}/{request_id}/status/{new_status}")
async def request_status_update(
    kind: str,
    request_id: int,
    new_status: str,
    actor_username: Annotated[str, Depends(require_web_admin)],
) -> RedirectResponse:
    try:
        async with SessionFactory() as session:
            updated = await update_web_request_status(session, kind, request_id, new_status, actor_username=actor_username)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    return RedirectResponse(url=f"/requests/{kind}/{request_id}", status_code=status.HTTP_303_SEE_OTHER)
