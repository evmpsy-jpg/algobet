from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.google_sheets_sync import (
    GoogleSheetsSyncResult,
    download_google_sheet_xlsx,
    google_sheet_download_path,
    google_sheet_export_url,
    remove_temp_file,
    sync_google_sheet_once,
)


def test_google_sheet_export_url_uses_xlsx_export() -> None:
    assert google_sheet_export_url("abc123") == "https://docs.google.com/spreadsheets/d/abc123/export?format=xlsx"


@pytest.mark.parametrize("sheet_id", ["", "   "])
def test_google_sheet_export_url_rejects_empty_id(sheet_id: str) -> None:
    with pytest.raises(ValueError):
        google_sheet_export_url(sheet_id)


def test_google_sheet_download_path_uses_uploads_dir(tmp_path: Path) -> None:
    path = google_sheet_download_path(tmp_path)

    assert path.parent == tmp_path
    assert path.name.startswith("google-sheets-")
    assert path.suffix == ".xlsx"


def test_remove_temp_file_ignores_missing_file(tmp_path: Path) -> None:
    remove_temp_file(tmp_path / "missing.xlsx")


@pytest.mark.asyncio
async def test_sync_google_sheet_once_skips_when_id_is_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "app.services.google_sheets_sync.get_settings",
        lambda: SimpleNamespace(google_sheet_id="", uploads_dir=tmp_path, admin_ids=[]),
    )

    result = await sync_google_sheet_once()

    assert result.status == "disabled"


@pytest.mark.asyncio
async def test_sync_google_sheet_once_skips_unchanged_hash(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    destination = tmp_path / "sheet.xlsx"
    content = b"PK fake xlsx"
    destination.write_bytes(content)

    async def fake_get_bot_setting(session, key: str, default: str) -> str:
        return "same-sha"

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(
        "app.services.google_sheets_sync.get_settings",
        lambda: SimpleNamespace(google_sheet_id="sheet", uploads_dir=tmp_path, admin_ids=[]),
    )
    monkeypatch.setattr("app.services.google_sheets_sync.google_sheet_download_path", lambda uploads_dir: destination)
    monkeypatch.setattr("app.services.google_sheets_sync.download_google_sheet_xlsx", lambda sheet_id, path, service_account_file="": None)
    monkeypatch.setattr("app.services.google_sheets_sync.calculate_sha256", lambda path: "same-sha")
    monkeypatch.setattr("app.services.google_sheets_sync.SessionFactory", lambda: FakeSession())
    monkeypatch.setattr("app.services.google_sheets_sync.get_bot_setting", fake_get_bot_setting)

    result = await sync_google_sheet_once()

    assert result == GoogleSheetsSyncResult(status="skipped", file_hash="same-sha", message="Изменений нет.")
    assert not destination.exists()

def test_download_google_sheet_xlsx_uses_service_account_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self) -> bytes:
            return b"PK xlsx"

    def fake_urlopen(request, timeout: int):
        captured["authorization"] = request.headers.get("Authorization")
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("app.services.google_sheets_sync.google_service_account_token", lambda path: "token-123")
    monkeypatch.setattr("app.services.google_sheets_sync.urllib.request.urlopen", fake_urlopen)

    destination = tmp_path / "sheet.xlsx"
    download_google_sheet_xlsx("sheet-id", destination, "service.json")

    assert captured == {"authorization": "Bearer token-123", "timeout": 240}
    assert destination.read_bytes() == b"PK xlsx"
