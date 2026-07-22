from decimal import Decimal

import pytest

from app.handlers.user import calculator_result_keyboard, calculator_step_keyboard
from app.services.stake_calculator import (
    calculate_stakes,
    calculate_step_stakes,
    format_step_stake_calculator,
    format_stake_calculator,
    parse_bank,
    parse_step,
)


def by_coeff(bank: Decimal, coefficient: str):
    items = calculate_stakes(bank)
    return next(item for item in items if item.coefficient == Decimal(coefficient))


def test_parse_bank_accepts_spaces_and_comma() -> None:
    assert parse_bank("5 500,50 ₽") == Decimal("5500.50")


@pytest.mark.parametrize("value", ["", "abc", "0", "-100"])
def test_parse_bank_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        parse_bank(value)


def test_parse_step_accepts_button_values() -> None:
    assert parse_step("5") == 5
    assert parse_step("1/20") == 20


@pytest.mark.parametrize("value", ["1", "15x", "30"])
def test_parse_step_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        parse_step(value)


def test_calculate_step_stakes_matches_excel_left_table_for_bank_5500() -> None:
    item = calculate_step_stakes(Decimal("5500"), 5)

    assert item.signal_bank == Decimal("1100.00")
    assert item.set_1 == Decimal("88.00")
    assert item.set_2 == Decimal("242.00")
    assert item.set_3 == Decimal("770.00")
    assert item.total == Decimal("1100.00")


def test_format_step_stake_calculator_outputs_mini_table() -> None:
    text = format_step_stake_calculator(Decimal("5500"), 5)

    assert "Банк: 5 500" in text
    assert "Шаг: 1/5" in text
    assert "1 сет | 8% | 88" in text
    assert "2 сет | 22% | 242" in text
    assert "3 сет | 70% | 770" in text


def test_calculator_step_keyboard_contains_step_buttons() -> None:
    markup = calculator_step_keyboard()

    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert callbacks == ["calc:step:2", "calc:step:5", "calc:step:10", "calc:step:15", "calc:step:20"]


def test_calculate_stakes_matches_excel_examples_for_bank_5500() -> None:
    bank = Decimal("5500")

    coeff_150 = by_coeff(bank, "1.50")
    assert coeff_150.step_1 == Decimal("385.00")
    assert coeff_150.step_2 == Decimal("1265.00")
    assert coeff_150.step_3 == Decimal("3850.00")
    assert coeff_150.profit_1 == Decimal("192.50")
    assert coeff_150.profit_2 == Decimal("247.50")
    assert coeff_150.profit_3 == Decimal("275.00")

    coeff_200 = by_coeff(bank, "2.00")
    assert coeff_200.step_1 == Decimal("825.00")
    assert coeff_200.step_2 == Decimal("1650.00")
    assert coeff_200.step_3 == Decimal("3025.00")


def test_format_stake_calculator_contains_key_rows() -> None:
    text = format_stake_calculator(Decimal("5500"))

    assert "Банк: 5 500" in text
    assert "КФ 1,5: 1 сет 385 / 2 сет 1 265 / 3 сет 3 850" in text
    assert "КФ 2: 1 сет 825 / 2 сет 1 650 / 3 сет 3 025" in text

def test_calculator_result_keyboard_contains_followup_actions() -> None:
    markup = calculator_result_keyboard()

    assert markup.inline_keyboard[0][0].callback_data == "calc:again"
    assert markup.inline_keyboard[1][0].callback_data == "calc:menu"
