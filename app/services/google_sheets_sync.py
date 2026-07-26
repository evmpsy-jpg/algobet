from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.database.session import SessionFactory
from app.services.admin_notifications import format_import_error_admin_text, format_import_success_admin_text, notify_admins
from app.services.bot_settings import get_bot_setting, set_bot_setting
from app.services.google_sheets_api import parse_google_sheet
from app.services.import_service import ImportSummary, calculate_text_sha256, import_parse_result
from app.settings import get_settings

logger = logging.getLogger(__name__)

GOOGLE_SHEETS_LAST_SHA_KEY = "google_sheets.last_sha256"
GOOGLE_SHEETS_UPLOADED_BY = 0
GOOGLE_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
SYNC_LOCK = asyncio.Lock()


class GoogleSyncBot(Protocol):
    async def send_message(self, *, chat_id: int, text: str, **kwargs: object) -> object:
        ...


@dataclass(frozen=True)
class GoogleSheetsSyncResult:
    status: str
    file_hash: str | None = None
    summary: ImportSummary | None = None
    message: str = ""


def google_service_account_token(service_account_file: str) -> str:
    path = Path(service_account_file).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Файл сервисного аккаунта не найден: {path}")

    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_file(
        str(path),
        scopes=[GOOGLE_SHEETS_SCOPE],
    )
    credentials.refresh(Request())
    if not credentials.token:
        raise ValueError("Google не вернул access token для сервисного аккаунта.")
    return credentials.token


def parse_google_sheet_with_service_account(sheet_id: str, service_account_file: str, timezone: str):
    token = google_service_account_token(service_account_file)
    return parse_google_sheet(sheet_id, token, timezone)


def parse_result_hash(result) -> str:
    payload = {
        "sheet_name": result.sheet_name,
        "total_rows": result.total_rows,
        "matches": [
            {
                "external_match_id": match.external_match_id,
                "external_tournament_id": match.external_tournament_id,
                "source_url": match.source_url,
                "date": match.tournament_date,
                "time": match.match_time,
                "player_1": match.player_1,
                "player_2": match.player_2,
                "score": match.score,
                "raw_data": match.raw_data,
            }
            for match in result.matches
        ],
        "warnings": result.warnings,
    }
    return calculate_text_sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))


async def sync_google_sheet_once(bot: GoogleSyncBot | None = None) -> GoogleSheetsSyncResult:
    settings = get_settings()
    if not settings.google_sheet_id.strip():
        return GoogleSheetsSyncResult(status="disabled", message="Google Sheet ID не указан.")
    service_account_file = str(getattr(settings, "google_service_account_file", "") or "")
    if not service_account_file.strip():
        return GoogleSheetsSyncResult(status="disabled", message="Файл сервисного аккаунта не указан.")
    if SYNC_LOCK.locked():
        return GoogleSheetsSyncResult(status="skipped", message="Синхронизация уже выполняется.")

    async with SYNC_LOCK:
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    parse_google_sheet_with_service_account,
                    settings.google_sheet_id,
                    service_account_file,
                    settings.timezone,
                ),
                timeout=90,
            )
            file_hash = parse_result_hash(result)
            async with SessionFactory() as session:
                previous_hash = await get_bot_setting(session, GOOGLE_SHEETS_LAST_SHA_KEY, "")
                if previous_hash == file_hash:
                    return GoogleSheetsSyncResult(status="skipped", file_hash=file_hash, message="Изменений нет.")

                summary = await import_parse_result(
                    session,
                    result,
                    original_name="Google Sheets API",
                    stored_path=f"google-sheets://{settings.google_sheet_id}/{result.sheet_name}",
                    file_hash=file_hash,
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
                    format_import_error_admin_text("Google Sheets API", GOOGLE_SHEETS_UPLOADED_BY, exc),
                )
            return GoogleSheetsSyncResult(status="error", message=str(exc))

    if bot is not None:
        await notify_admins(
            bot,
            settings.admin_ids,
            format_import_success_admin_text(summary, "Google Sheets API", GOOGLE_SHEETS_UPLOADED_BY),
        )
    logger.info(
        "Google Sheets API imported: sheet=%s rows=%s matches=%s signals=%s",
        result.sheet_name,
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
    logger.info("Google Sheets sync enabled: every %s seconds, mode=sheets_api", interval_seconds)
    while True:
        await sync_google_sheet_once(bot)
        await asyncio.sleep(interval_seconds)