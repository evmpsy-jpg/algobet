from pathlib import Path

from app.services.excel_parser import parse_tournaments_file


def test_parser() -> None:
    source = Path("tests/sample.xlsx")
    if not source.exists():
        return
    result = parse_tournaments_file(source)
    assert result.sheet_name == "Турниры"
    assert result.matches
    assert result.matches[0].external_match_id > 0
