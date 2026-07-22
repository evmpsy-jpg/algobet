from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


@dataclass(frozen=True)
class StakePlan:
    coefficient: Decimal
    step_1_ratio: Decimal
    step_2_ratio: Decimal
    step_3_ratio: Decimal


@dataclass(frozen=True)
class StakeCalculation:
    coefficient: Decimal
    step_1: Decimal
    step_2: Decimal
    step_3: Decimal
    profit_1: Decimal
    profit_2: Decimal
    profit_3: Decimal


@dataclass(frozen=True)
class StepStakeCalculation:
    bank: Decimal
    step_divisor: int
    signal_bank: Decimal
    set_1: Decimal
    set_2: Decimal
    set_3: Decimal
    total: Decimal


STAKE_PLANS = [
    StakePlan(Decimal("1.40"), Decimal("0.06"), Decimal("0.21"), Decimal("0.73")),
    StakePlan(Decimal("1.45"), Decimal("0.07"), Decimal("0.22"), Decimal("0.71")),
    StakePlan(Decimal("1.50"), Decimal("0.07"), Decimal("0.23"), Decimal("0.70")),
    StakePlan(Decimal("1.55"), Decimal("0.08"), Decimal("0.24"), Decimal("0.68")),
    StakePlan(Decimal("1.60"), Decimal("0.10"), Decimal("0.25"), Decimal("0.65")),
    StakePlan(Decimal("1.65"), Decimal("0.10"), Decimal("0.25"), Decimal("0.65")),
    StakePlan(Decimal("1.70"), Decimal("0.10"), Decimal("0.25"), Decimal("0.65")),
    StakePlan(Decimal("1.75"), Decimal("0.11"), Decimal("0.26"), Decimal("0.63")),
    StakePlan(Decimal("1.80"), Decimal("0.12"), Decimal("0.27"), Decimal("0.61")),
    StakePlan(Decimal("1.85"), Decimal("0.12"), Decimal("0.27"), Decimal("0.61")),
    StakePlan(Decimal("1.90"), Decimal("0.13"), Decimal("0.27"), Decimal("0.60")),
    StakePlan(Decimal("1.95"), Decimal("0.14"), Decimal("0.28"), Decimal("0.58")),
    StakePlan(Decimal("2.00"), Decimal("0.15"), Decimal("0.30"), Decimal("0.55")),
]
STEP_OPTIONS = (2, 5, 10, 15, 20)
SET_RATIOS = (Decimal("0.08"), Decimal("0.22"), Decimal("0.70"))
TWOPLACES = Decimal("0.01")


def parse_bank(value: str) -> Decimal:
    normalized = (
        value.replace("₽", "")
        .replace("руб", "")
        .replace(" ", "")
        .replace("\u00a0", "")
        .replace(",", ".")
        .strip()
    )
    try:
        bank = Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError("Введите банк числом, например 5500 или 5 500") from exc
    if bank <= 0:
        raise ValueError("Банк должен быть больше нуля")
    return bank.quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def parse_step(value: str) -> int:
    normalized = value.strip().replace("1/", "").replace("/", "")
    try:
        step = int(normalized)
    except ValueError as exc:
        raise ValueError("Выберите шаг кнопкой") from exc
    if step not in STEP_OPTIONS:
        raise ValueError("Выберите один из доступных шагов")
    return step


def _money(value: Decimal) -> Decimal:
    return value.quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def calculate_step_stakes(bank: Decimal, step_divisor: int) -> StepStakeCalculation:
    if step_divisor <= 0:
        raise ValueError("Шаг должен быть больше нуля")
    signal_bank = _money(bank / Decimal(step_divisor))
    set_1 = _money(signal_bank * SET_RATIOS[0])
    set_2 = _money(signal_bank * SET_RATIOS[1])
    set_3 = _money(signal_bank * SET_RATIOS[2])
    return StepStakeCalculation(
        bank=bank,
        step_divisor=step_divisor,
        signal_bank=signal_bank,
        set_1=set_1,
        set_2=set_2,
        set_3=set_3,
        total=_money(set_1 + set_2 + set_3),
    )


def calculate_stakes(bank: Decimal) -> list[StakeCalculation]:
    calculations: list[StakeCalculation] = []
    for plan in STAKE_PLANS:
        step_1 = _money(bank * plan.step_1_ratio)
        step_2 = _money(bank * plan.step_2_ratio)
        step_3 = _money(bank * plan.step_3_ratio)
        profit_1 = _money(step_1 * plan.coefficient - step_1)
        profit_2 = _money(step_2 * plan.coefficient - (step_1 + step_2))
        profit_3 = _money(step_3 * plan.coefficient - (step_1 + step_2 + step_3))
        calculations.append(StakeCalculation(plan.coefficient, step_1, step_2, step_3, profit_1, profit_2, profit_3))
    return calculations


def format_amount(value: Decimal) -> str:
    if value == value.to_integral_value():
        return f"{int(value):,}".replace(",", " ")
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def format_step_stake_calculator(bank: Decimal, step_divisor: int) -> str:
    item = calculate_step_stakes(bank, step_divisor)
    lines = [
        "🧮 Калькулятор ставок",
        "",
        f"Банк: {format_amount(item.bank)}",
        f"Шаг: 1/{item.step_divisor}",
        f"Сумма на сигнал: {format_amount(item.signal_bank)}",
        "",
        "Сет | Доля | Ставка",
        f"1 сет | 8% | {format_amount(item.set_1)}",
        f"2 сет | 22% | {format_amount(item.set_2)}",
        f"3 сет | 70% | {format_amount(item.set_3)}",
        f"Итого | 100% | {format_amount(item.total)}",
    ]
    return "\n".join(lines)[:3900]


def format_stake_calculator(bank: Decimal) -> str:
    lines = [
        "🧮 Калькулятор ставок",
        "",
        f"Банк: {format_amount(bank)}",
        "",
        "Ставки по трёхшаговой стратегии:",
    ]
    for item in calculate_stakes(bank):
        coefficient = str(item.coefficient.normalize()).replace(".", ",")
        lines.append(
            f"КФ {coefficient}: "
            f"1 сет {format_amount(item.step_1)} / "
            f"2 сет {format_amount(item.step_2)} / "
            f"3 сет {format_amount(item.step_3)}"
        )
    lines.extend([
        "",
        "Расчёт повторяет лист «рассчет ставки»: доли банка зависят от коэффициента.",
    ])
    return "\n".join(lines)[:3900]
