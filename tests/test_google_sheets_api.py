from app.services.google_sheets_api import _bounded_range, parse_google_sheets_grid


def cell(value=None, *, formula=None, hyperlink=None, formatted=None):
    data = {}
    if value is not None:
        if isinstance(value, (int, float)):
            data["effectiveValue"] = {"numberValue": value}
        else:
            data["effectiveValue"] = {"stringValue": value}
    if formatted is not None:
        data["formattedValue"] = formatted
    if formula is not None:
        data["userEnteredValue"] = {"formulaValue": formula}
    if hyperlink is not None:
        data["hyperlink"] = hyperlink
    return data


def test_parse_google_sheets_grid_reads_first_sheet_hyperlinks_and_values() -> None:
    payload = {
        "sheets": [
            {
                "properties": {"title": "Июль 2026"},
                "data": [
                    {
                        "rowData": [
                            {"values": [cell("26.07.2026"), cell("Время"), cell("Матч"), cell("D"), cell("E"), cell("F"), cell("G"), cell("Счёт"), cell("I"), cell("J"), cell("K"), cell("L"), cell("M"), cell("N"), cell("O"), cell("P"), cell("Q")]},
                            {"values": [cell("Лига", hyperlink="https://example.test/tournaments/9001")]},
                            {"values": [
                                cell(),
                                cell("12:30"),
                                cell("(100) Игрок 1 vs (200) Игрок 2", hyperlink="https://example.test/tournaments/9001/501"),
                                cell(8), cell(), cell(), cell(), cell(""), cell(), cell(), cell(), cell(), cell(), cell(), cell(), cell(), cell(9),
                            ]},
                        ]
                    }
                ],
            }
        ]
    }

    result = parse_google_sheets_grid(payload)

    assert result.sheet_name == "Июль 2026"
    assert result.total_rows == 3
    assert len(result.matches) == 1
    match = result.matches[0]
    assert match.external_tournament_id == 9001
    assert match.external_match_id == 501
    assert match.player_1 == "Игрок 1"
    assert match.player_2 == "Игрок 2"
    assert match.match_time == "12:30"
    assert match.raw_data["Q"] == 9
    assert match.raw_data["_tournament_name"] == "Лига"

def test_bounded_range_limits_rows() -> None:
    assert _bounded_range("A:CT", 500) == "A1:CT500"
    assert _bounded_range("A:CT", 500, 2000) == "A1501:CT2000"
    assert _bounded_range("A:CT", 0) == "A:CT"
