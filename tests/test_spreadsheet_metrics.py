from app.services.spreadsheet_metrics import calculate_signal_columns


def test_calculate_signal_columns_fills_all_signal_columns() -> None:
    data = {
        "M": 3,
        "N": 1,
        "O": 11,
        "P": 9,
        "Q": 10,
        "R": 4,
        "T": 5,
        "U": 1,
        "V": 7,
        "W": 4,
        "X": 7,
        "Y": 5,
        "AA": 9,
        "AB": 5,
        "AC": 15,
        "AD": 5,
        "AE": 1,
        "AF": 6,
        "CM": 26,
        "CN": 4,
        "DL": 16,
        "DM": 2,
        "DN": 10,
        "DO": 5,
        "DQ": 15,
        "DR": 8,
        "DT": 30,
        "DU": 3,
        "DW": 11,
        "DX": 2,
    }

    calculate_signal_columns(data)

    assert data["DG"] == 8
    assert data["DH"] == 0
    assert data["EG"] == 5
    assert data["CS"] == 9