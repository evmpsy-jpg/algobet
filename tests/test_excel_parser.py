from pathlib import Path

from openpyxl import Workbook

from app.services.excel_parser import parse_tournaments_file


def test_parser() -> None:
    source = Path("tests/sample.xlsx")
    if not source.exists():
        return
    result = parse_tournaments_file(source)
    assert result.sheet_name == "Турниры"
    assert result.matches
    assert result.matches[0].external_match_id > 0


def test_parser_uses_first_sheet_and_regular_hyperlinks(tmp_path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Июль 2026"
    sheet.append(["26.07.2026", "Время", "Матч", "D", "E", "F", "G", "Счёт"])
    sheet.append(["Турнир", None, None])
    sheet.append([None, "12:30", "(100) Игрок 1 vs (200) Игрок 2", None, None, None, None, ""])
    sheet["C3"].hyperlink = "https://example.test/tournaments/9001/501"
    path = tmp_path / "google.xlsx"
    workbook.save(path)

    result = parse_tournaments_file(path)

    assert result.sheet_name == "Июль 2026"
    assert result.matches[0].external_tournament_id == 9001
    assert result.matches[0].external_match_id == 501
    assert result.matches[0].player_1 == "Игрок 1"
    assert result.warnings == ["Лист 'Турниры' не найден, использован первый лист: Июль 2026."]
