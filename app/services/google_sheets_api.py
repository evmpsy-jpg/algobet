from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from openpyxl.utils import get_column_letter

from app.services.excel_parser import DATE_RE, HYPERLINK_RE, PLAYER_RE, URL_IDS_RE, ParsedMatch, ParseResult
from app.services.spreadsheet_metrics import calculate_signal_columns

GOOGLE_SHEETS_API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
DEFAULT_RANGE_COLUMNS = "A:CT"


def _request_json(url: str, token: str, *, timeout: int = 45) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "AlgobetBot/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _quote_sheet_name(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def first_sheet_title(sheet_id: str, token: str) -> str:
    url = f"{GOOGLE_SHEETS_API_BASE}/{urllib.parse.quote(sheet_id)}?fields=sheets.properties(title,index)"
    payload = _request_json(url, token)
    sheets = payload.get("sheets") or []
    if not sheets:
        raise ValueError("В Google Sheets нет листов.")
    sheets.sort(key=lambda item: item.get("properties", {}).get("index", 0))
    title = sheets[0].get("properties", {}).get("title")
    if not title:
        raise ValueError("Не удалось определить первый лист Google Sheets.")
    return str(title)


def fetch_first_sheet_grid(sheet_id: str, token: str, *, columns: str = DEFAULT_RANGE_COLUMNS) -> dict[str, Any]:
    title = first_sheet_title(sheet_id, token)
    range_name = f"{_quote_sheet_name(title)}!{columns}"
    params = urllib.parse.urlencode({
        "includeGridData": "true",
        "ranges": range_name,
        "fields": "sheets(properties(title),data(rowData(values(userEnteredValue,effectiveValue,formattedValue,hyperlink))))",
    })
    url = f"{GOOGLE_SHEETS_API_BASE}/{urllib.parse.quote(sheet_id)}?{params}"
    return _request_json(url, token, timeout=90)


def _typed_value(value: dict[str, Any] | None) -> Any:
    if not value:
        return None
    for key in ("numberValue", "stringValue", "boolValue", "formulaValue"):
        if key in value:
            return value[key]
    return None


def _display_value(cell: dict[str, Any] | None) -> str:
    if not cell:
        return ""
    formatted = cell.get("formattedValue")
    if formatted is not None:
        return str(formatted).strip()
    value = _typed_value(cell.get("effectiveValue"))
    return str(value).strip() if value is not None else ""


def _raw_value(cell: dict[str, Any] | None) -> Any:
    if not cell:
        return None
    effective = _typed_value(cell.get("effectiveValue"))
    if effective is not None:
        return effective
    formatted = cell.get("formattedValue")
    return formatted if formatted is not None else None


def _formula_value(cell: dict[str, Any] | None) -> str | None:
    if not cell:
        return None
    user_entered = cell.get("userEnteredValue") or {}
    formula = user_entered.get("formulaValue")
    return str(formula) if formula else None


def _extract_link_and_label(cell: dict[str, Any] | None) -> tuple[str, str] | None:
    if not cell:
        return None
    formula = _formula_value(cell)
    if formula and formula.upper().startswith("=HYPERLINK"):
        match = HYPERLINK_RE.match(formula.strip())
        if match:
            return match.group("url"), match.group("label")
    hyperlink = cell.get("hyperlink")
    if hyperlink:
        return str(hyperlink).strip(), _display_value(cell)
    return None


def _row_values(row: dict[str, Any]) -> list[dict[str, Any]]:
    return list((row.get("values") or []))


def parse_google_sheets_grid(payload: dict[str, Any], timezone: str = "Europe/Moscow") -> ParseResult:
    sheets = payload.get("sheets") or []
    if not sheets:
        raise ValueError("Google Sheets API не вернул листы.")
    sheet = sheets[0]
    sheet_name = str(sheet.get("properties", {}).get("title") or "Лист1")
    data = sheet.get("data") or []
    rows = list((data[0].get("rowData") if data else []) or [])

    current_date: str | None = None
    current_headers: list[str] = []
    current_tournament_name = "Турнир"
    parsed: list[ParsedMatch] = []
    warnings: list[str] = []
    tz = ZoneInfo(timezone)

    for row_number, row in enumerate(rows, start=1):
        cells = _row_values(row)
        first_text = _display_value(cells[0] if cells else None)
        date_match = DATE_RE.search(first_text) if first_text else None
        if date_match:
            current_date = date_match.group(0)
            current_headers = [
                _display_value(cell) or f"COL_{index + 1}"
                for index, cell in enumerate(cells)
            ]
            continue

        tournament_link = _extract_link_and_label(cells[0]) if cells else None
        if tournament_link is not None:
            current_tournament_name = tournament_link[1].strip()

        if not current_date or len(cells) < 3:
            continue

        time_value = _display_value(cells[1])
        hyperlink = _extract_link_and_label(cells[2])
        if not time_value or hyperlink is None:
            continue
        source_url, label = hyperlink

        ids_match = URL_IDS_RE.search(source_url)
        players_match = PLAYER_RE.match(label)
        if ids_match is None or players_match is None:
            warnings.append(f"Строка {row_number}: не удалось определить ID или игроков.")
            continue

        match_time = time_value[:5]
        try:
            start_at = datetime.strptime(f"{current_date} {match_time}", "%d.%m.%Y %H:%M").replace(tzinfo=tz)
        except ValueError:
            warnings.append(f"Строка {row_number}: неверные дата/время {current_date} {match_time}.")
            continue

        raw_data: dict[str, Any] = {
            "_row_number": row_number,
            "_tournament_name": current_tournament_name,
        }
        for index, cell in enumerate(cells):
            column = get_column_letter(index + 1)
            header = current_headers[index] if index < len(current_headers) else f"COL_{index + 1}"
            raw_data[column] = _raw_value(cell)
            raw_data[f"{header}__{index + 1}"] = _raw_value(cell)
            formula = _formula_value(cell)
            if formula:
                raw_data[f"_{column}_formula"] = formula

        calculate_signal_columns(raw_data)

        parsed.append(
            ParsedMatch(
                external_match_id=int(ids_match.group("match_id")),
                external_tournament_id=int(ids_match.group("tournament_id")),
                source_url=source_url,
                tournament_date=current_date,
                tournament_name=current_tournament_name,
                match_time=match_time,
                match_start_at=start_at,
                player_1=players_match.group("player1").strip(),
                player_2=players_match.group("player2").strip(),
                player_1_rating=int(players_match.group("rating1")) if players_match.group("rating1") else None,
                player_2_rating=int(players_match.group("rating2")) if players_match.group("rating2") else None,
                score=_display_value(cells[7]) if len(cells) > 7 and _display_value(cells[7]) else None,
                raw_data=raw_data,
            )
        )

    return ParseResult(
        sheet_name=sheet_name,
        total_rows=len(rows),
        matches=parsed,
        warnings=warnings,
    )


def parse_google_sheet(sheet_id: str, token: str, timezone: str = "Europe/Moscow") -> ParseResult:
    return parse_google_sheets_grid(fetch_first_sheet_grid(sheet_id, token), timezone)