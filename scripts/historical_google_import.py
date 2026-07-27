from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.database.session import SessionFactory
from app.services.excel_parser import DATE_RE, ParseResult
from app.services.google_sheets_api import (
    fetch_first_sheet_rows_grid,
    fetch_sheet_values,
    first_sheet_title,
    parse_google_sheets_grid,
)
from app.services.google_sheets_sync import google_service_account_token, parse_result_hash
from app.services.historical_import import filter_parse_result_by_period, preview_historical_import
from app.services.import_service import import_parse_result
from app.services.sqlite_backup import create_sqlite_backup, verify_sqlite_backup
from app.settings import get_settings

HISTORICAL_UPLOADED_BY = 0


def parse_date(value: str, timezone_name: str, *, end_of_day: bool = False) -> datetime:
    date_value = datetime.strptime(value, "%Y-%m-%d")
    if end_of_day:
        date_value = date_value.replace(hour=23, minute=59, second=59)
    return date_value.replace(tzinfo=ZoneInfo(timezone_name))


def default_month_start(timezone_name: str) -> datetime:
    now = datetime.now(ZoneInfo(timezone_name))
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def parse_sheet_date(value: str) -> date | None:
    match = DATE_RE.search(str(value or ""))
    if not match:
        return None
    try:
        return datetime.strptime(match.group(0), "%d.%m.%Y").date()
    except ValueError:
        return None


def last_data_row_from_overview(values: list[list[str]]) -> int:
    for index in range(len(values) - 1, -1, -1):
        row = values[index]
        time_value = str(row[1]).strip() if len(row) > 1 else ""
        match_value = str(row[2]).strip() if len(row) > 2 else ""
        if time_value[:2].isdigit() and ":" in time_value and match_value:
            return index + 1
    return len(values)


def date_sections(values: list[list[str]], *, start: date, end: date) -> list[tuple[int, int]]:
    date_rows: list[tuple[int, date]] = []
    for index, row in enumerate(values, start=1):
        sheet_date = parse_sheet_date(str(row[0]) if row else "")
        if sheet_date is not None:
            date_rows.append((index, sheet_date))

    if not date_rows:
        return []

    last_row = last_data_row_from_overview(values)
    sections: list[tuple[int, int]] = []
    current_date: date | None = None
    current_start: int | None = None
    current_end: int | None = None

    for position, (row_number, sheet_date) in enumerate(date_rows):
        next_row = date_rows[position + 1][0] if position + 1 < len(date_rows) else last_row + 1
        section_end = min(next_row - 1, last_row)
        if sheet_date < start or sheet_date > end or section_end < row_number:
            continue
        if current_date == sheet_date and current_start is not None and current_end is not None:
            current_end = section_end
            continue
        if current_start is not None and current_end is not None:
            sections.append((current_start, current_end))
        current_date = sheet_date
        current_start = row_number
        current_end = section_end

    if current_start is not None and current_end is not None:
        sections.append((current_start, current_end))
    return sections


def load_historical_parse_result(sheet_id: str, token: str, timezone_name: str, *, start_at: datetime, end_at: datetime, overview_rows: int) -> ParseResult:
    sheet_name = first_sheet_title(sheet_id, token)
    overview_range = f"A1:C{overview_rows}" if overview_rows > 0 else "A:C"
    print(f"Читаю обзор листа: {overview_range}", flush=True)
    overview = fetch_sheet_values(sheet_id, token, sheet_name, overview_range)
    sections = date_sections(overview, start=start_at.date(), end=end_at.date())
    matches = []
    warnings = []
    total_rows = 0
    print(f"Найдено дневных блоков: {len(sections)}", flush=True)
    for index, (start_row, end_row) in enumerate(sections, start=1):
        print(f"Читаю блок {index}/{len(sections)}: строки {start_row}-{end_row}", flush=True)
        payload = fetch_first_sheet_rows_grid(
            sheet_id,
            token,
            sheet_name=sheet_name,
            start_row=start_row,
            end_row=end_row,
        )
        parsed = parse_google_sheets_grid(payload, timezone_name)
        filtered = filter_parse_result_by_period(parsed, start_at=start_at, end_at=end_at)
        matches.extend(filtered.matches)
        warnings.extend(filtered.warnings)
        total_rows += parsed.total_rows
    return ParseResult(sheet_name=sheet_name, total_rows=total_rows, matches=matches, warnings=warnings)


def format_signal(item) -> str:
    side = f"П{item.side}" if item.side in (1, 2) else "—"
    result = item.result_status or "unknown"
    score = item.score or "—"
    return (
        f"{item.send_at:%d.%m %H:%M} · {item.group.upper()} · {item.level or '—'} · "
        f"{side} · {item.player_1} — {item.player_2} · счет {score} · результат {result}"
    )


def print_preview(preview) -> None:
    print("Исторический импорт Google Sheets")
    print(f"Строк в выбранных дневных блоках: {preview.total_rows}")
    print(f"Матчей в выбранном периоде: {preview.selected_matches}")
    print(f"Кандидатов в сигналы: {preview.candidate_signals}")
    print(f"По группам: {preview.signals_by_group}")
    print(f"Результаты: {preview.results_by_status}, без результата: {preview.without_result}")
    rejection_items = sorted(preview.rejection_reasons.items(), key=lambda item: item[1], reverse=True)
    print(f"Причин отказа всего: {len(rejection_items)}")
    for reason, count in rejection_items[:10]:
        compact_reason = reason.replace("\n", " ")
        if len(compact_reason) > 220:
            compact_reason = compact_reason[:217] + "..."
        print(f"- {count}: {compact_reason}")
    if preview.first_signals:
        print("\nПервые сигналы:")
        for item in preview.first_signals:
            print("- " + format_signal(item))
    if preview.last_signals:
        print("\nПоследние сигналы:")
        for item in preview.last_signals:
            print("- " + format_signal(item))


async def main() -> None:
    parser = argparse.ArgumentParser(description="Preview/apply historical Google Sheets import for monthly statistics.")
    parser.add_argument("--apply", action="store_true", help="Записать исторические сигналы в базу. Без флага только dry-run.")
    parser.add_argument("--from-date", default=None, help="Начало периода YYYY-MM-DD. По умолчанию 1 число текущего месяца.")
    parser.add_argument("--to-date", default=None, help="Конец периода YYYY-MM-DD. По умолчанию текущий момент.")
    parser.add_argument("--overview-rows", type=int, default=5000, help="Сколько строк читать для поиска дневных блоков. 0 = весь столбец A:C.")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.google_sheet_id.strip():
        raise SystemExit("GOOGLE_SHEET_ID не указан.")
    if not settings.google_service_account_file.strip():
        raise SystemExit("GOOGLE_SERVICE_ACCOUNT_FILE не указан.")

    start_at = parse_date(args.from_date, settings.timezone) if args.from_date else default_month_start(settings.timezone)
    end_at = parse_date(args.to_date, settings.timezone, end_of_day=True) if args.to_date else datetime.now(ZoneInfo(settings.timezone))

    token = google_service_account_token(settings.google_service_account_file)
    filtered = load_historical_parse_result(settings.google_sheet_id, token, settings.timezone, start_at=start_at, end_at=end_at, overview_rows=args.overview_rows)
    preview = preview_historical_import(filtered, signal_lead_minutes=settings.signal_lead_minutes)
    print_preview(preview)

    if not args.apply:
        print("\nDRY-RUN: база не изменена. Для записи добавьте --apply.")
        return

    backup = create_sqlite_backup(settings.database_url, settings.data_dir, keep=settings.sqlite_backup_keep)
    verification = verify_sqlite_backup(backup.created.path)
    if not verification.ok:
        raise SystemExit(f"Backup создан, но проверка не прошла: {verification.message}")
    print(f"\nBackup: {backup.created.path} ({backup.created.size_bytes} bytes)")

    matches_by_day = defaultdict(list)
    for match in filtered.matches:
        matches_by_day[match.match_start_at.date()].append(match)

    total_inserted = 0
    total_updated = 0
    total_signals = 0
    total_cancelled = 0
    group_totals: dict[str, int] = {}
    batch_ids: list[int] = []

    print("\nНачинаю пакетную запись по дням", flush=True)
    for day in sorted(matches_by_day):
        day_result = ParseResult(
            sheet_name=filtered.sheet_name,
            total_rows=len(matches_by_day[day]),
            matches=matches_by_day[day],
            warnings=filtered.warnings,
        )
        file_hash = parse_result_hash(day_result)
        print(f"Пишу день {day}: матчей {len(day_result.matches)}", flush=True)
        async with SessionFactory() as session:
            summary = await import_parse_result(
                session,
                day_result,
                original_name=f"Google Sheets historical import {day}",
                stored_path=f"google-sheets://{settings.google_sheet_id}/{filtered.sheet_name}?historical=1&date={day}",
                file_hash=file_hash,
                uploaded_by=HISTORICAL_UPLOADED_BY,
                mark_missing=False,
                past_due_signal_action="store_sent",
            )
        batch_ids.append(summary.batch_id)
        total_inserted += summary.inserted_matches
        total_updated += summary.updated_matches
        total_signals += summary.scheduled_signals
        total_cancelled += summary.cancelled_signals
        for group, count in summary.scheduled_by_group.items():
            group_totals[group] = group_totals.get(group, 0) + count
        print(
            f"Готово {day}: batch={summary.batch_id}, +{summary.inserted_matches}, "
            f"обновлено={summary.updated_matches}, сигналов={summary.scheduled_signals}",
            flush=True,
        )

    print("\nЗапись завершена")
    print(f"Import batch IDs: {batch_ids}")
    print(f"Матчей добавлено: {total_inserted}")
    print(f"Матчей обновлено: {total_updated}")
    print(f"Исторических сигналов сохранено как sent: {total_signals}")
    print(f"Отменено сигналов: {total_cancelled}")
    print(f"По группам: {group_totals}")


if __name__ == "__main__":
    asyncio.run(main())