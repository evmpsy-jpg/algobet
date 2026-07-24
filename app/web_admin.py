from __future__ import annotations

import csv
import hmac
from io import StringIO
from contextlib import asynccontextmanager
from datetime import datetime
from html import escape
from typing import Annotated, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from aiogram import Bot
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.database.models import MatchAnalysisRequest, ScheduledSignal, SubscriptionRequest, User
from app.database.session import SessionFactory, init_db
from app.services.access import grant_subscription_access
from app.services.signal_sender import process_delivery_now
from app.services.signal_results import AutoResultSummary, auto_update_signal_results, format_winrate, set_signal_result
from app.services.dashboard import (
    DashboardSummary,
    MaintenanceSummary,
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
    collect_maintenance_summary,
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


def _auth_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Basic"},
    )


def require_web_admin(credentials: Annotated[HTTPBasicCredentials | None, Depends(security)]) -> None:
    configured_credentials = get_settings().web_admin_credentials
    if not configured_credentials:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WEB_ADMIN_USERS or WEB_ADMIN_USERNAME/WEB_ADMIN_PASSWORD is not configured",
        )

    if credentials is None:
        raise _auth_error()

    expected_password = configured_credentials.get(credentials.username)
    if expected_password is None:
        raise _auth_error()

    if not hmac.compare_digest(credentials.password, expected_password):
        raise _auth_error()


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


SIGNAL_RESULT_WEB_STATUSES = ("won", "lost", "void", "unknown")


SIGNAL_RESULT_FILTER_LABELS = {
    "unrated": "Без результата",
    "won": "Зашли",
    "lost": "Не зашли",
    "void": "Возврат",
}


def _signals_path(status_filter: str | None = None, result_filter: str | None = None, *, base: str = "/signals") -> str:
    params = []
    if status_filter:
        params.append(f"status={status_filter}")
    if result_filter:
        params.append(f"result={result_filter}")
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


async def update_web_signal_result(session, signal_id: int, new_status: str) -> bool:
    if new_status not in SIGNAL_RESULT_WEB_STATUSES:
        raise ValueError("Unknown signal result status")
    signal = await session.get(ScheduledSignal, signal_id)
    if signal is None:
        return False
    await set_signal_result(session, signal, new_status, fixed_by_telegram_id=None)
    return True


async def update_web_request_status(session, kind: str, request_id: int, new_status: str) -> bool:
    if kind == "subscription":
        if new_status not in SUBSCRIPTION_WEB_STATUSES:
            raise ValueError("Unknown subscription status")
        request = await session.get(SubscriptionRequest, request_id)
        if request is None:
            return False
        request.status = new_status
        request.updated_at = datetime.utcnow()
        if new_status in {"paid", "done"}:
            user = await session.get(User, request.user_id)
            if user is not None:
                await grant_subscription_access(session, user, request)
        await session.commit()
        return True
    if kind == "analysis":
        if new_status not in ANALYSIS_WEB_STATUSES:
            raise ValueError("Unknown analysis status")
        request = await session.get(MatchAnalysisRequest, request_id)
        if request is None:
            return False
        request.status = new_status
        request.updated_at = datetime.utcnow()
        await session.commit()
        return True
    raise ValueError("Unknown request kind")


def _fmt_counts(counts: dict[str, int]) -> str:
    if not counts:
        return '<span class="muted">нет данных</span>'
    return "".join(
        f'<span class="pill"><b>{escape(_label(str(key)))}</b> {value}</span>'
        for key, value in sorted(counts.items())
    )


def _fmt_dt(value) -> str:
    if value is None:
        return "-"
    return escape(value.strftime("%d.%m.%Y %H:%M"))


def _user_name(user: UserListItem | RequestListItem) -> str:
    username = f"@{user.username}" if user.username else ""
    if isinstance(user, UserListItem):
        full_name = " ".join(part for part in [user.first_name, user.last_name] if part)
        return escape(username or full_name or str(user.telegram_id))
    return escape(username or str(user.telegram_id))


def _base_html(title: str, body: str, *, token: str = "") -> str:
    nav = "".join(
        f'<a href="{_token_href(path, token)}">{label}</a>'
        for label, path in [
            ("Сводка", "/"),
            ("Сигналы", "/signals"),
            ("Доставки", "/deliveries"),
            ("Статистика", "/quality"),
            ("Пользователи", "/users"),
            ("Заявки", "/requests"),
            ("Обслуживание", "/maintenance"),
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
    section {{ padding:16px; margin-bottom:18px; }}
    .pills {{ display:flex; flex-wrap:wrap; gap:8px; }}
    .pill {{ display:inline-flex; gap:7px; align-items:center; border:1px solid var(--line); border-radius:999px; padding:6px 10px; background:#fbfcfe; font-size:13px; }}
    .details {{ display:grid; grid-template-columns:140px 1fr; gap:8px 12px; margin:0; }}
    dt {{ color:var(--muted); }} dd {{ margin:0; }}
    table {{ width:100%; border-collapse:collapse; font-size:14px; }}
    th, td {{ border-bottom:1px solid var(--line); padding:10px 8px; text-align:left; vertical-align:top; }}
    th {{ color:var(--muted); font-size:12px; text-transform:uppercase; }}
    .filters {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:14px; }}
    @media (max-width:900px) {{ .grid,.sections {{ grid-template-columns:1fr 1fr; }} }}
    @media (max-width:620px) {{ header {{ align-items:flex-start; flex-direction:column; }} main {{ padding:14px; }} .grid,.sections {{ grid-template-columns:1fr; }} table {{ font-size:13px; }} th.optional,td.optional {{ display:none; }} }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Админка Algobet</h1>
      <div class="muted">Операционная панель без правки данных</div>
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
          <td>{escape(signal.signal_group.upper())}</td>
          <td class="optional">{escape(signal.level or '-')}</td>
          <td>P{signal.side or '-'}</td>
          <td>{escape(signal.player_1)} - {escape(signal.player_2)}</td>
          <td>{escape(_label(signal.result_status))}</td>
          <td class="optional">{signal.sent_deliveries} / {signal.failed_deliveries}</td>
        </tr>
        """
        for signal in signals
    ) or '<tr><td colspan="9" class="muted">Сигналов пока нет.</td></tr>'


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
      <table><thead><tr><th>ID</th><th>Статус</th><th>Отправка</th><th>Группа</th><th class="optional">Уровень</th><th>Сторона</th><th>Матч</th><th>Результат</th><th class="optional">Отправлено / ошибки</th></tr></thead><tbody>{_signal_rows(recent_signals, token=token)}</tbody></table>
    </section>
    """
    return _base_html("Сводка", body, token=token)


def render_signals_html(
    signals: list[SignalListItem],
    *,
    token: str = "",
    status_filter: str | None = None,
    result_filter: str | None = None,
) -> str:
    status_filters = "".join(
        f'<a class="button" href="{_token_href(_signals_path(status, result_filter), token)}">{label}</a>'
        for label, status in [
            ("Все", None),
            ("Запланировано", "scheduled"),
            ("Готово", "ready"),
            ("Отправлено", "sent"),
            ("Отменено", "cancelled"),
        ]
    )
    result_filters = "".join(
        f'<a class="button" href="{_token_href(_signals_path(status_filter, result), token)}">{label}</a>'
        for label, result in [
            ("Все результаты", None),
            ("Без результата", "unrated"),
            ("Зашли", "won"),
            ("Не зашли", "lost"),
            ("Возврат", "void"),
        ]
    )
    result_title = SIGNAL_RESULT_FILTER_LABELS.get(result_filter or "", "Все результаты")
    body = f"""
    <section>
      <h2>Сигналы: {escape(_label(status_filter or 'all'))} · {escape(result_title)}</h2>
      <div class="filters">{status_filters}</div>
      <div class="filters">{result_filters}<a class="button" href="{_token_href(_signals_path(status_filter, result_filter, base='/signals/export.csv'), token)}">CSV</a></div>
      <table><thead><tr><th>ID</th><th>Статус</th><th>Отправка</th><th>Группа</th><th class="optional">Уровень</th><th>Сторона</th><th>Матч</th><th>Результат</th><th class="optional">Доставлено / ошибок</th></tr></thead><tbody>{_signal_rows(signals, token=token)}</tbody></table>
    </section>
    """
    return _base_html("Сигналы", body, token=token)


def render_signals_csv(signals: list[SignalListItem]) -> str:
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "status", "send_at", "signal_group", "level", "side", "match", "result", "sent_deliveries", "failed_deliveries"])
    for signal in signals:
        writer.writerow([
            signal.id,
            signal.status,
            _fmt_dt(signal.send_at),
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


def render_deliveries_csv(deliveries) -> str:
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "signal_id", "status", "telegram_id", "username", "signal_group", "match", "time", "error"])
    for delivery in deliveries:
        writer.writerow([
            delivery.id,
            delivery.signal_id,
            delivery.status,
            delivery.telegram_id,
            delivery.username or "",
            delivery.signal_group,
            delivery.match_title,
            _fmt_dt(delivery.sent_at or delivery.created_at),
            delivery.error_text or "",
        ])
    return output.getvalue()


def render_deliveries_html(deliveries, *, token: str = "", status_filter: str | None = None) -> str:
    filters = "".join(
        f'<a class="button" href="{_token_href(path, token)}">{label}</a>'
        for label, path in [
            ("Все", "/deliveries"),
            ("Отправлено", "/deliveries?status=sent"),
            ("Ошибка", "/deliveries?status=failed"),
            ("Ожидает", "/deliveries?status=pending"),
        ]
    )
    rows = "".join(
        f"""
        <tr>
          <td>#{delivery.id}</td>
          <td><a href="{_token_href(f'/signals/{delivery.signal_id}', token)}">#{delivery.signal_id}</a></td>
          <td>{escape(_label(delivery.status))}</td>
          <td>{delivery.telegram_id}</td>
          <td>{escape('@' + delivery.username if delivery.username else '-')}</td>
          <td>{escape(delivery.signal_group.upper())}</td>
          <td>{escape(delivery.match_title)}</td>
          <td>{_fmt_dt(delivery.sent_at or delivery.created_at)}</td>
          <td class="optional">{escape((delivery.error_text or '-')[:180])}</td>
          <td>{_delivery_retry_action(delivery.id, delivery.status, status_filter)}</td>
        </tr>
        """
        for delivery in deliveries
    ) or '<tr><td colspan="10" class="muted">Доставок пока нет.</td></tr>'
    body = f"""
    <section>
      <h2>Доставки: {escape(_label(status_filter or 'all'))}</h2>
      <div class="filters">{filters}<a class="button" href="{_token_href('/deliveries/export.csv' + ('?status=' + status_filter if status_filter else ''), token)}">CSV</a></div>
      <table><thead><tr><th>ID</th><th>Сигнал</th><th>Статус</th><th>Telegram</th><th>Пользователь</th><th>Группа</th><th>Матч</th><th>Время</th><th class="optional">Ошибка</th></tr></thead><tbody>{rows}</tbody></table>
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
    {_quality_table('По группам', summary.by_group)}
    {_quality_table('По уровням', summary.by_level)}
    """
    return _base_html("Статистика", body, token=token)


def render_users_html(users: list[UserListItem], *, token: str = "") -> str:
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
    <section>
      <h2>Пользователи</h2>
      <table><thead><tr><th>ID</th><th>Telegram</th><th>Имя</th><th>Статус</th><th>Доступ</th><th>Платных</th><th>Пробных</th><th class="optional">До</th><th class="optional">Отправлено / ошибки</th></tr></thead><tbody>{rows}</tbody></table>
    </section>
    """
    return _base_html("Пользователи", body, token=token)


def render_requests_html(requests: list[RequestListItem], *, token: str = "") -> str:
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
    <section>
      <h2>Заявки</h2>
      <table><thead><tr><th>Тип</th><th>ID</th><th>Статус</th><th>Telegram</th><th>Пользователь</th><th>Название</th><th>Создано</th></tr></thead><tbody>{rows}</tbody></table>
    </section>
    """
    return _base_html("Заявки", body, token=token)




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
    delivery_rows = "".join(
        f"""
        <tr><td>#{delivery.id}</td><td><a href="{_token_href(f'/signals/{delivery.signal_id}', token)}">#{delivery.signal_id}</a></td><td>{escape(_label(delivery.status))}</td><td>{escape(delivery.signal_group.upper())}</td><td>{escape(delivery.match_title)}</td><td>{_fmt_dt(delivery.sent_at)}</td><td>{escape(delivery.error_text or '-')}</td></tr>
        """
        for delivery in detail.deliveries
    ) or '<tr><td colspan="7" class="muted">Доставок пока нет.</td></tr>'
    request_rows = "".join(
        f"""
        <tr><td>{escape(_label(request.kind))}</td><td><a href="{_token_href(f'/requests/{request.kind}/{request.id}', token)}">#{request.id}</a></td><td>{escape(_label(request.status))}</td><td>{escape(request.title[:160])}</td><td>{_fmt_dt(request.created_at)}</td></tr>
        """
        for request in detail.requests
    ) or '<tr><td colspan="5" class="muted">Заявок пока нет.</td></tr>'
    item = detail.item
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
    </section>
    <section><h2>Последние доставки</h2><table><thead><tr><th>ID</th><th>Сигнал</th><th>Статус</th><th>Группа</th><th>Матч</th><th>Отправлено</th><th>Ошибка</th></tr></thead><tbody>{delivery_rows}</tbody></table></section>
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


def render_maintenance_html(summary: MaintenanceSummary, *, token: str = "") -> str:
    enabled = _label("enabled" if summary.sqlite_backup_enabled else "disabled")
    body = f"""
    <section><h2>Обслуживание</h2>
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
    </section>
    """
    return _base_html("Обслуживание", body, token=token)


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
    if result_filter not in SIGNAL_RESULT_FILTER_LABELS:
        result_filter = None
    async with SessionFactory() as session:
        rows = await collect_signal_list(session, status_filter=status_filter, result_filter=result_filter)
    return HTMLResponse(render_signals_html(rows, token="", status_filter=status_filter, result_filter=result_filter))


@app.get("/signals/export.csv")
async def signals_export(_: Annotated[None, Depends(require_web_admin)], request: Request) -> Response:
    status_filter = request.query_params.get("status") or None
    result_filter = request.query_params.get("result") or None
    if result_filter not in SIGNAL_RESULT_FILTER_LABELS:
        result_filter = None
    async with SessionFactory() as session:
        rows = await collect_signal_list(session, status_filter=status_filter, result_filter=result_filter, limit=10000)
    content = render_signals_csv(rows)
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=algobet-signals.csv"},
    )


@app.get("/deliveries", response_class=HTMLResponse)
async def deliveries(_: Annotated[None, Depends(require_web_admin)], request: Request) -> HTMLResponse:
    status_filter = request.query_params.get("status") or None
    async with SessionFactory() as session:
        rows = await collect_delivery_list(session, status_filter=status_filter)
    return HTMLResponse(render_deliveries_html(rows, token="", status_filter=status_filter))


@app.get("/deliveries/export.csv")
async def deliveries_export(_: Annotated[None, Depends(require_web_admin)], request: Request) -> Response:
    status_filter = request.query_params.get("status") or None
    async with SessionFactory() as session:
        rows = await collect_delivery_list(session, status_filter=status_filter, limit=10000)
    content = render_deliveries_csv(rows)
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=algobet-deliveries.csv"},
    )


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
    async with SessionFactory() as session:
        rows = await collect_user_list(session)
    return HTMLResponse(render_users_html(rows, token=""))


@app.get("/requests", response_class=HTMLResponse)
async def requests(_: Annotated[None, Depends(require_web_admin)], request: Request) -> HTMLResponse:
    async with SessionFactory() as session:
        rows = await collect_request_list(session)
    return HTMLResponse(render_requests_html(rows, token=""))


@app.get("/maintenance", response_class=HTMLResponse)
async def maintenance(_: Annotated[None, Depends(require_web_admin)]) -> HTMLResponse:
    summary = collect_maintenance_summary(get_settings())
    return HTMLResponse(render_maintenance_html(summary))


@app.post("/signals/{signal_id}/result/{result_status}")
async def signal_result_update(
    signal_id: int,
    result_status: str,
    _: Annotated[None, Depends(require_web_admin)],
) -> RedirectResponse:
    async with SessionFactory() as session:
        updated = await update_web_signal_result(session, signal_id, result_status)
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
    _: Annotated[None, Depends(require_web_admin)],
) -> RedirectResponse:
    try:
        async with SessionFactory() as session:
            updated = await update_web_request_status(session, kind, request_id, new_status)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    return RedirectResponse(url=f"/requests/{kind}/{request_id}", status_code=status.HTTP_303_SEE_OTHER)


