from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from html import escape
from typing import Annotated, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.database.session import SessionFactory, init_db
from app.services.dashboard import (
    DashboardSummary,
    MaintenanceSummary,
    RequestDetail,
    RequestListItem,
    SignalDetail,
    SignalListItem,
    UserDetail,
    UserListItem,
    collect_dashboard_summary,
    collect_maintenance_summary,
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


def _fmt_counts(counts: dict[str, int]) -> str:
    if not counts:
        return '<span class="muted">none</span>'
    return "".join(
        f'<span class="pill"><b>{escape(key)}</b> {value}</span>'
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
            ("Dashboard", "/"),
            ("Signals", "/signals"),
            ("Users", "/users"),
            ("Requests", "/requests"),
            ("Maintenance", "/maintenance"),
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
      <h1>Algobet Admin</h1>
      <div class="muted">Read-only operational dashboard</div>
      <nav>{nav}</nav>
    </div>
    <a class="button" href="{refresh_href}">Refresh</a>
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
          <td>{escape(signal.status)}</td>
          <td>{_fmt_dt(signal.send_at)}</td>
          <td>{escape(signal.signal_group.upper())}</td>
          <td class="optional">{escape(signal.level or '-')}</td>
          <td>P{signal.side or '-'}</td>
          <td>{escape(signal.player_1)} - {escape(signal.player_2)}</td>
          <td>{escape(signal.result_status or '-')}</td>
          <td class="optional">{signal.sent_deliveries} / {signal.failed_deliveries}</td>
        </tr>
        """
        for signal in signals
    ) or '<tr><td colspan="9" class="muted">No signals yet.</td></tr>'


def render_dashboard_html(summary: DashboardSummary, *, token: str = "") -> str:
    latest = summary.latest_import
    latest_html = (
        "<p class=\"muted\">No imports yet.</p>"
        if latest is None
        else f"""
        <dl class="details">
          <dt>File</dt><dd>{escape(latest.file_name)}</dd>
          <dt>Status</dt><dd>{escape(latest.status)}</dd>
          <dt>Parsed</dt><dd>{latest.parsed_matches}</dd>
          <dt>Inserted / Updated</dt><dd>{latest.inserted_matches} / {latest.updated_matches}</dd>
          <dt>Missing</dt><dd>{latest.missing_matches}</dd>
          <dt>Finished</dt><dd>{_fmt_dt(latest.finished_at or latest.created_at)}</dd>
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
      <div class="metric"><span>Users</span><strong>{summary.users_total}</strong><div class="muted">active {summary.users_active}</div></div>
      <div class="metric"><span>Access</span><strong>{summary.access_paid_active}</strong><div class="muted">paid, trial {summary.access_trial_active}</div></div>
      <div class="metric"><span>Matches</span><strong>{summary.matches_total}</strong><div class="muted">active {summary.matches_active}</div></div>
      <div class="metric"><span>Signals</span><strong>{summary.signals_total}</strong><div class="muted">deliveries {summary.deliveries_total}</div></div>
    </div>
    <div class="sections">
      <section><h2>Latest Import</h2>{latest_html}</section>
      <section><h2>Requests</h2><div class="pills">{_fmt_counts(summary.subscription_requests_by_status)}{_fmt_counts(summary.analysis_requests_by_status)}</div></section>
      <section><h2>Signal Statuses</h2><div class="pills">{_fmt_counts(summary.signals_by_status)}</div></section>
      <section><h2>Delivery Results</h2><div class="pills">{_fmt_counts(summary.deliveries_by_status)}{_fmt_counts(summary.results_by_status)}</div></section>
    </div>
    <section>
      <h2>Recent Signals</h2>
      <table><thead><tr><th>ID</th><th>Status</th><th>Send At</th><th>Group</th><th class="optional">Level</th><th>Side</th><th>Match</th><th>Result</th><th class="optional">Sent / Failed</th></tr></thead><tbody>{_signal_rows(recent_signals, token=token)}</tbody></table>
    </section>
    """
    return _base_html("Dashboard", body, token=token)


def render_signals_html(signals: list[SignalListItem], *, token: str = "", status_filter: str | None = None) -> str:
    filters = "".join(
        f'<a class="button" href="{_token_href(path, token)}">{label}</a>'
        for label, path in [
            ("All", "/signals"),
            ("Scheduled", "/signals?status=scheduled"),
            ("Ready", "/signals?status=ready"),
            ("Sent", "/signals?status=sent"),
            ("Cancelled", "/signals?status=cancelled"),
        ]
    )
    body = f"""
    <section>
      <h2>Signals {escape(status_filter or 'all')}</h2>
      <div class="filters">{filters}</div>
      <table><thead><tr><th>ID</th><th>Status</th><th>Send At</th><th>Group</th><th class="optional">Level</th><th>Side</th><th>Match</th><th>Result</th><th class="optional">Sent / Failed</th></tr></thead><tbody>{_signal_rows(signals, token=token)}</tbody></table>
    </section>
    """
    return _base_html("Signals", body, token=token)


def render_users_html(users: list[UserListItem], *, token: str = "") -> str:
    rows = "".join(
        f"""
        <tr>
          <td><a href="{_token_href(f'/users/{user.id}', token)}">#{user.id}</a></td>
          <td>{user.telegram_id}</td>
          <td>{_user_name(user)}</td>
          <td>{'active' if user.is_active else 'inactive'}</td>
          <td>{escape(user.access_type or '-')} / {escape(user.access_status or '-')}</td>
          <td>{user.signals_remaining if user.signals_remaining is not None else '-'}</td>
          <td>{user.free_signals_remaining if user.free_signals_remaining is not None else '-'}</td>
          <td class="optional">{_fmt_dt(user.active_until)}</td>
          <td class="optional">{user.sent_deliveries} / {user.failed_deliveries}</td>
        </tr>
        """
        for user in users
    ) or '<tr><td colspan="9" class="muted">No users yet.</td></tr>'
    body = f"""
    <section>
      <h2>Users</h2>
      <table><thead><tr><th>ID</th><th>Telegram</th><th>Name</th><th>Status</th><th>Access</th><th>Paid Left</th><th>Trial Left</th><th class="optional">Until</th><th class="optional">Sent / Failed</th></tr></thead><tbody>{rows}</tbody></table>
    </section>
    """
    return _base_html("Users", body, token=token)


def render_requests_html(requests: list[RequestListItem], *, token: str = "") -> str:
    rows = "".join(
        f"""
        <tr>
          <td>{escape(request.kind)}</td>
          <td><a href="{_token_href(f'/requests/{request.kind}/{request.id}', token)}">#{request.id}</a></td>
          <td>{escape(request.status)}</td>
          <td>{request.telegram_id}</td>
          <td>{_user_name(request)}</td>
          <td>{escape(request.title[:160])}</td>
          <td>{_fmt_dt(request.created_at)}</td>
        </tr>
        """
        for request in requests
    ) or '<tr><td colspan="7" class="muted">No requests yet.</td></tr>'
    body = f"""
    <section>
      <h2>Requests</h2>
      <table><thead><tr><th>Type</th><th>ID</th><th>Status</th><th>Telegram</th><th>User</th><th>Title</th><th>Created</th></tr></thead><tbody>{rows}</tbody></table>
    </section>
    """
    return _base_html("Requests", body, token=token)




def render_signal_detail_html(detail: SignalDetail, *, token: str = "") -> str:
    delivery_rows = "".join(
        f"""
        <tr><td>#{delivery.id}</td><td>{escape(delivery.status)}</td><td>{delivery.telegram_id}</td><td>{escape('@' + delivery.username if delivery.username else '-')}</td><td>{_fmt_dt(delivery.sent_at)}</td><td>{escape(delivery.error_text or '-')}</td></tr>
        """
        for delivery in detail.deliveries
    ) or '<tr><td colspan="6" class="muted">No deliveries yet.</td></tr>'
    trace_rows = "".join(
        f"<tr><td>{escape(str(trace.get('code', '-')))}</td><td>{escape(str(trace.get('label', '-')))}</td><td>{escape(str(trace.get('passed', '-')))}</td><td>{escape(str(trace.get('actual', '-'))[:180])}</td></tr>"
        for trace in detail.decision_trace
    ) or '<tr><td colspan="4" class="muted">No decision trace.</td></tr>'
    body = f"""
    <section><h2>Signal #{detail.item.id}</h2>
      <dl class="details">
        <dt>Status</dt><dd>{escape(detail.item.status)}</dd>
        <dt>Group / Level</dt><dd>{escape(detail.item.signal_group.upper())} / {escape(detail.item.level or '-')}</dd>
        <dt>Side</dt><dd>P{detail.item.side or '-'}</dd>
        <dt>Match</dt><dd>{escape(detail.item.player_1)} - {escape(detail.item.player_2)}</dd>
        <dt>Match start</dt><dd>{_fmt_dt(detail.match_start_at)}</dd>
        <dt>External IDs</dt><dd>match {detail.external_match_id}, tournament {detail.external_tournament_id}</dd>
        <dt>Result</dt><dd>{escape(detail.item.result_status or '-')}</dd>
        <dt>Deliveries</dt><dd>{detail.item.sent_deliveries} sent / {detail.item.failed_deliveries} failed</dd>
        <dt>Cancel reason</dt><dd>{escape(detail.cancel_reason or '-')}</dd>
        <dt>Decision</dt><dd>{escape(detail.decision_reason or '-')}</dd>
      </dl>
    </section>
    <section><h2>Message</h2><pre>{escape(detail.message_text or '')}</pre></section>
    <section><h2>Deliveries</h2><table><thead><tr><th>ID</th><th>Status</th><th>Telegram</th><th>User</th><th>Sent</th><th>Error</th></tr></thead><tbody>{delivery_rows}</tbody></table></section>
    <section><h2>Decision Trace</h2><table><thead><tr><th>Code</th><th>Rule</th><th>Passed</th><th>Actual</th></tr></thead><tbody>{trace_rows}</tbody></table></section>
    """
    return _base_html(f"Signal #{detail.item.id}", body, token=token)


def render_user_detail_html(detail: UserDetail, *, token: str = "") -> str:
    delivery_rows = "".join(
        f"""
        <tr><td>#{delivery.id}</td><td><a href="{_token_href(f'/signals/{delivery.signal_id}', token)}">#{delivery.signal_id}</a></td><td>{escape(delivery.status)}</td><td>{escape(delivery.signal_group.upper())}</td><td>{escape(delivery.match_title)}</td><td>{_fmt_dt(delivery.sent_at)}</td><td>{escape(delivery.error_text or '-')}</td></tr>
        """
        for delivery in detail.deliveries
    ) or '<tr><td colspan="7" class="muted">No deliveries yet.</td></tr>'
    request_rows = "".join(
        f"""
        <tr><td>{escape(request.kind)}</td><td><a href="{_token_href(f'/requests/{request.kind}/{request.id}', token)}">#{request.id}</a></td><td>{escape(request.status)}</td><td>{escape(request.title[:160])}</td><td>{_fmt_dt(request.created_at)}</td></tr>
        """
        for request in detail.requests
    ) or '<tr><td colspan="5" class="muted">No requests yet.</td></tr>'
    item = detail.item
    body = f"""
    <section><h2>User #{item.id}</h2>
      <dl class="details">
        <dt>Telegram ID</dt><dd>{item.telegram_id}</dd>
        <dt>Name</dt><dd>{_user_name(item)}</dd>
        <dt>Status</dt><dd>{'active' if item.is_active else 'inactive'}</dd>
        <dt>Access</dt><dd>{escape(item.access_type or '-')} / {escape(item.access_status or '-')}</dd>
        <dt>Paid left</dt><dd>{item.signals_remaining if item.signals_remaining is not None else '-'}</dd>
        <dt>Trial left</dt><dd>{item.free_signals_remaining if item.free_signals_remaining is not None else '-'}</dd>
        <dt>Active until</dt><dd>{_fmt_dt(item.active_until)}</dd>
        <dt>Deliveries</dt><dd>{item.sent_deliveries} sent / {item.failed_deliveries} failed</dd>
      </dl>
    </section>
    <section><h2>Recent Deliveries</h2><table><thead><tr><th>ID</th><th>Signal</th><th>Status</th><th>Group</th><th>Match</th><th>Sent</th><th>Error</th></tr></thead><tbody>{delivery_rows}</tbody></table></section>
    <section><h2>Requests</h2><table><thead><tr><th>Type</th><th>ID</th><th>Status</th><th>Title</th><th>Created</th></tr></thead><tbody>{request_rows}</tbody></table></section>
    """
    return _base_html(f"User #{item.id}", body, token=token)


def render_request_detail_html(detail: RequestDetail, *, token: str = "") -> str:
    item = detail.item
    body = f"""
    <section><h2>{escape(item.kind.title())} Request #{item.id}</h2>
      <dl class="details">
        <dt>Status</dt><dd>{escape(item.status)}</dd>
        <dt>User</dt><dd>{_user_name(item)} / {item.telegram_id}</dd>
        <dt>Title</dt><dd>{escape(item.title)}</dd>
        <dt>Description</dt><dd>{escape(detail.description)}</dd>
        <dt>Payment</dt><dd>{escape(detail.payment_details or '-')}</dd>
        <dt>Contact</dt><dd>{escape(detail.specialist_contact or '-')}</dd>
        <dt>Created</dt><dd>{_fmt_dt(item.created_at)}</dd>
        <dt>Updated</dt><dd>{_fmt_dt(detail.updated_at)}</dd>
      </dl>
    </section>
    """
    return _base_html(f"Request #{item.id}", body, token=token)


def render_maintenance_html(summary: MaintenanceSummary, *, token: str = "") -> str:
    enabled = "enabled" if summary.sqlite_backup_enabled else "disabled"
    body = f"""
    <section><h2>Maintenance</h2>
      <dl class="details">
        <dt>Database</dt><dd>{escape(summary.database_path or '-')}</dd>
        <dt>Database size</dt><dd>{_fmt_bytes(summary.database_size_bytes)}</dd>
        <dt>Data directory</dt><dd>{_fmt_bytes(summary.data_size_bytes)}</dd>
        <dt>Uploads directory</dt><dd>{_fmt_bytes(summary.uploads_size_bytes)}</dd>
        <dt>Auto backup</dt><dd>{enabled}</dd>
        <dt>Backup interval</dt><dd>{summary.sqlite_backup_interval_hours} h</dd>
        <dt>Keep backups</dt><dd>{summary.sqlite_backup_keep}</dd>
        <dt>Latest backup</dt><dd>{escape(summary.latest_backup_path or '-')}</dd>
        <dt>Latest backup size</dt><dd>{_fmt_bytes(summary.latest_backup_size_bytes)}</dd>
        <dt>Latest backup date</dt><dd>{_fmt_dt(summary.latest_backup_created_at)}</dd>
      </dl>
    </section>
    """
    return _base_html("Maintenance", body, token=token)


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
    async with SessionFactory() as session:
        rows = await collect_signal_list(session, status_filter=status_filter)
    return HTMLResponse(render_signals_html(rows, token="", status_filter=status_filter))


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


