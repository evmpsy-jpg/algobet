from __future__ import annotations

import asyncio
import logging
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from app.database.session import SessionFactory
from app.services.admin_notifications import format_import_error_admin_text, format_import_success_admin_text, notify_admins
from app.services.bot_settings import get_bot_setting, set_bot_setting
from app.services.import_service import ImportSummary, calculate_sha256, import_tournaments
from app.settings import get_settings

logger = logging.getLogger(__name__)

GOOGLE_SHEETS_LAST_SHA_KEY = "google_sheets.last_sha256"
GOOGLE_SHEETS_UPLOADED_BY = 0
GOOGLE_SHEETS_EXPORT_SCOPE = "https://www.googleapis.com/auth/drive.readonly"


class GoogleSyncBot(Protocol):
    async def send_message(self, *, chat_id: int, text: str, **kwargs: object) -> object:
        ...


@dataclass(frozen=True)
class GoogleSheetsSyncResult:
    status: str
    file_hash: str | None = None
    summary: ImportSummary | None = None
    message: str = ""


def google_sheet_export_url(sheet_id: str) -> str:
    cleaned = sheet_id.strip()
    if not cleaned:
        raise ValueError("Google Sheet ID не указан.")
    return f"https://docs.google.com/spreadsheets/d/{cleaned}/export?format=xlsx"


def google_sheet_download_path(uploads_dir: Path, now: datetime | None = None) -> Path:
    now = now or datetime.utcnow()
    return uploads_dir / f"google-sheets-{now:%Y%m%d-%H%M%S}.xlsx"


def google_service_account_token(service_account_file: str) -> str:
    path = Path(service_account_file).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Файл сервисного аккаунта не найден: {path}")

    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_file(
        str(path),
        scopes=[GOOGLE_SHEETS_EXPORT_SCOPE],
    )
    credentials.refresh(Request())
    if not credentials.token:
        raise ValueError("Google не вернул access token для сервисного аккаунта.")
    return credentials.token


def download_google_sheet_xlsx(sheet_id: str, destination: Path, service_account_file: str = "") -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "AlgobetBot/1.0"}
    if service_account_file.strip():
        headers["Authorization"] = f"Bearer {google_service_account_token(service_account_file)}"
    request = urllib.request.Request(
        google_sheet_export_url(sheet_id),
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
    if not data.startswith(b"PK"):
        raise ValueError("Google Sheets не отдал XLSX. Проверьте доступ сервисного аккаунта к таблице.")
    destination.write_bytes(data)


def remove_temp_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("Не удалось удалить временный файл Google Sheets %s: %s", path, exc)


async def sync_google_sheet_once(bot: GoogleSyncBot | None = None) -> GoogleSheetsSyncResult:
    settings = get_settings()
    if not settings.google_sheet_id.strip():
        return GoogleSheetsSyncResult(status="disabled", message="Google Sheet ID не указан.")

    destination = google_sheet_download_path(settings.uploads_dir)
    service_account_file = str(getattr(settings, "google_service_account_file", "") or "")
    try:
        await asyncio.to_thread(download_google_sheet_xlsx, settings.google_sheet_id, destination, service_account_file)
        file_hash = calculate_sha256(destination)
        async with SessionFactory() as session:
            previous_hash = await get_bot_setting(session, GOOGLE_SHEETS_LAST_SHA_KEY, "")
            if previous_hash == file_hash:
                return GoogleSheetsSyncResult(status="skipped", file_hash=file_hash, message="Изменений нет.")

            summary = await import_tournaments(
                session,
                destination,
                "Google Sheets.xlsx",
                uploaded_by=GOOGLE_SHEETS_UPLOADED_BY,
            )
            await set_bot_setting(session, GOOGLE_SHEETS_LAST_SHA_KEY, file_hash, max_length=64)
            await session.commit()
    except Exception as exc:
        logger.exception("Ошибка синхронизации Google Sheets")
        if bot is not None:
            await notify_admins(
                bot,
                settings.admin_ids,
                format_import_error_admin_text("Google Sheets.xlsx", GOOGLE_SHEETS_UPLOADED_BY, exc),
            )
        return GoogleSheetsSyncResult(status="error", message=str(exc))
    finally:
        remove_temp_file(destination)

    if bot is not None:
        await notify_admins(
            bot,
            settings.admin_ids,
            format_import_success_admin_text(summary, "Google Sheets.xlsx", GOOGLE_SHEETS_UPLOADED_BY),
        )
    logger.info(
        "Google Sheets imported: rows=%s matches=%s signals=%s",
        summary.total_rows,
        summary.parsed_matches,
        summary.scheduled_signals,
    )
    return GoogleSheetsSyncResult(status="imported", file_hash=file_hash, summary=summary)


async def google_sheets_sync_loop(bot: GoogleSyncBot) -> None:
    settings = get_settings()
    if not settings.google_sheets_sync_enabled:
        logger.info("Google Sheets sync disabled")
        return
    if not settings.google_sheet_id.strip():
        logger.warning("Google Sheets sync enabled, but GOOGLE_SHEET_ID is empty")
        return

    interval_seconds = max(1, int(settings.google_sheets_sync_interval_minutes)) * 60
    auth_mode = "service_account" if getattr(settings, "google_service_account_file", "") else "public_link"
    logger.info("Google Sheets sync enabled: every %s seconds, auth=%s", interval_seconds, auth_mode)
    while True:
        await sync_google_sheet_once(bot)
        await asyncio.sleep(interval_seconds)
