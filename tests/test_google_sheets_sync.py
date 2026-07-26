from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.excel_parser import ParseResult
from app.services.google_sheets_sync import GoogleSheetsSyncResult, parse_result_hash, sync_google_sheet_once


def test_parse_result_hash_is_stable() -> None:
    result = ParseResult(sheet_name="Лист", total_rows=1, matches=[], warnings=[])

    assert parse_result_hash(result) == parse_result_hash(result)


@pytest.mark.asyncio
async def test_sync_google_sheet_once_skips_when_id_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.google_sheets_sync.get_settings",
        lambda: SimpleNamespace(google_sheet_id="", google_service_account_file="", admin_ids=[]),
    )

    result = await sync_google_sheet_once()

    assert result.status == "disabled"


@pytest.mark.asyncio
async def test_sync_google_sheet_once_skips_when_service_account_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.google_sheets_sync.get_settings",
        lambda: SimpleNamespace(google_sheet_id="sheet", google_service_account_file="", admin_ids=[]),
    )

    result = await sync_google_sheet_once()

    assert result.status == "disabled"


@pytest.mark.asyncio
async def test_sync_google_sheet_once_skips_unchanged_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    parsed = ParseResult(sheet_name="Лист", total_rows=1, matches=[], warnings=[])
    file_hash = parse_result_hash(parsed)

    async def fake_get_bot_setting(session, key: str, default: str) -> str:
        return file_hash

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(
        "app.services.google_sheets_sync.get_settings",
        lambda: SimpleNamespace(
            google_sheet_id="sheet",
            google_service_account_file="service.json",
            timezone="Europe/Moscow",
            admin_ids=[],
        ),
    )
    monkeypatch.setattr("app.services.google_sheets_sync.parse_google_sheet_with_service_account", lambda sheet_id, path, timezone: parsed)
    monkeypatch.setattr("app.services.google_sheets_sync.SessionFactory", lambda: FakeSession())
    monkeypatch.setattr("app.services.google_sheets_sync.get_bot_setting", fake_get_bot_setting)

    result = await sync_google_sheet_once()

    assert result == GoogleSheetsSyncResult(status="skipped", file_hash=file_hash, message="Изменений нет.")