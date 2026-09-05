from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import SubscriptionRequest, User
from app.services.bot_settings import get_subscription_payment_config


@dataclass(frozen=True)
class SubscriptionPlan:
    id: str
    group: str
    title: str
    description: str
    price_rub: int
    signals_limit: int | None = None
    duration_days: int | None = None
    duration_hours: int | None = None
    includes_vip: bool = False
    includes_all_signals: bool = False
    includes_analytics: bool = False
    note: str | None = None


SUBSCRIPTION_PLANS: tuple[SubscriptionPlan, ...] = (
    SubscriptionPlan("vip_10", "vip", "VIP 99%", "10 сигналов", 2500, signals_limit=10, includes_vip=True, note="250р - сигнал"),
    SubscriptionPlan("vip_50", "vip", "VIP 99%", "50 сигналов", 11000, signals_limit=50, includes_vip=True, note="220р - сигнал"),
    SubscriptionPlan("vip_100", "vip", "VIP 99%", "100 сигналов", 20000, signals_limit=100, includes_vip=True, note="200р - сигнал"),
    SubscriptionPlan("all_10", "all", "Все сигналы 95%", "10 сигналов", 1500, signals_limit=10, includes_all_signals=True, note="150р - сигнал"),
    SubscriptionPlan("all_50", "all", "Все сигналы 95%", "50 сигналов", 6250, signals_limit=50, includes_all_signals=True, note="125р - сигнал"),
    SubscriptionPlan("all_100", "all", "Все сигналы 95%", "100 сигналов", 10000, signals_limit=100, includes_all_signals=True, note="100р - сигнал"),
    SubscriptionPlan("included_48h", "included", "Всё включено", "48 часов", 4500, duration_hours=48, includes_vip=True, includes_all_signals=True, includes_analytics=True, note="от 10 до 30 сигналов + вся турнирная таблица / выгодно брать на субботу + воскресенье - режим выходного дня"),
    SubscriptionPlan("included_10d", "included", "Всё включено", "10 дней", 10000, duration_days=10, includes_vip=True, includes_all_signals=True, includes_analytics=True, note="от 100 сигналов + вся турнирная таблица / < 100р за сигнал"),
    SubscriptionPlan("included_20d", "included", "Всё включено", "20 дней", 17500, duration_days=20, includes_vip=True, includes_all_signals=True, includes_analytics=True, note="от 200 сигналов + вся турнирная таблица / < 90р за сигнал"),
    SubscriptionPlan("included_30d", "included", "Всё включено", "30 дней", 25000, duration_days=30, includes_vip=True, includes_all_signals=True, includes_analytics=True, note="от 300 сигналов + вся турнирная таблица / < 80р за сигнал"),
    SubscriptionPlan("included_2m", "included", "Всё включено", "абонемент 2 месяца", 40000, duration_days=60, includes_vip=True, includes_all_signals=True, includes_analytics=True, note="< 70р за сигнал"),
    SubscriptionPlan("included_3m", "included", "Всё включено", "абонемент 3 месяца", 50000, duration_days=90, includes_vip=True, includes_all_signals=True, includes_analytics=True, note="< 60р за сигнал"),
    SubscriptionPlan("included_6m", "included", "Всё включено", "абонемент 6 месяцев", 80000, duration_days=180, includes_vip=True, includes_all_signals=True, includes_analytics=True, note="< 45р за сигнал"),
    SubscriptionPlan("included_12m", "included", "Всё включено", "годовой абонемент", 100000, duration_days=365, includes_vip=True, includes_all_signals=True, includes_analytics=True, note="< 30р за сигнал"),
)

PLAN_GROUP_LABELS = {
    "vip": "VIP 99%",
    "all": "Все сигналы 95%",
    "included": "Всё включено",
}

SUBSCRIPTION_STATUS_LABELS = {
    "new": "🆕 Новые",
    "paid": "💳 Оплаченные",
    "done": "✅ Обработанные",
    "cancelled": "❌ Отменённые",
}


def get_subscription_plan(plan_id: str) -> SubscriptionPlan | None:
    return next((plan for plan in SUBSCRIPTION_PLANS if plan.id == plan_id), None)


def format_price(value: int) -> str:
    return f"{value:,}".replace(",", " ") + "р"


def format_subscription_plans_text() -> str:
    return "\n".join([
        "💳 Подписка",
        "",
        "VIP 99%:",
        "• 10 сигналов — 2 500р / 250р - сигнал",
        "• 50 сигналов — 11 000р / 220р - сигнал",
        "• 100 сигналов — 20 000р / 200р - сигнал",
        "",
        "Все сигналы 95%:",
        "• 10 сигналов — 1 500р / 150р - сигнал",
        "• 50 сигналов — 6 250р / 125р - сигнал",
        "• 100 сигналов — 10 000р / 100р - сигнал",
        "",
        "Всё включено:",
        "VIP, все сигналы и аналитика турнирной таблицы.",
        "",
        "• 48 часов — 4 500р ~ от 10 до 30 сигналов + вся турнирная таблица / выгодно брать на субботу + воскресенье - режим выходного дня.",
        "",
        "• 10 дней — 10 000р ~ от 100 сигналов + вся турнирная таблица / < 100р за сигнал",
        "• 20 дней — 17 500р ~ от 200 сигналов + вся турнирная таблица / < 90р за сигнал",
        "• 30 дней — 25 000р ~ от 300 сигналов + вся турнирная таблица / < 80р за сигнал",
        "• абонемент 2 месяца — 40 000р / < 70р за сигнал",
        "• абонемент 3 месяца — 50 000р / < 60р за сигнал",
        "• абонемент 6 месяцев — 80 000р / < 45р за сигнал",
        "• годовой абонемент — 100 000р / < 30р за сигнал",
        "",
        "Выберите тариф кнопкой ниже, чтобы оставить заявку.",
    ])

def format_subscription_plan_line(plan: SubscriptionPlan) -> str:
    note = f" · {plan.note}" if plan.note else ""
    return f"{plan.title} · {plan.description} · {format_price(plan.price_rub)}{note}"


def format_subscription_request_user_text(request: SubscriptionRequest) -> str:
    lines = [
        f"✅ Заявка на подписку #{request.id} создана.",
        "",
        f"Тариф: {request.plan_title}",
        f"Условия: {request.plan_description}",
        f"Стоимость: {format_price(request.price_rub)}",
        "",
        "Реквизиты для оплаты:",
        request.payment_details or "Реквизиты уточните у специалиста.",
        "",
        f"Контакт: {request.specialist_contact or '—'}",
        "",
        "После оплаты напишите специалисту номер заявки.",
    ]
    return "\n".join(lines)


def format_subscription_request_admin_text(request: SubscriptionRequest, user: User) -> str:
    username = f"@{user.username}" if user.username else "—"
    name = " ".join(part for part in [user.first_name, user.last_name] if part).strip() or "—"
    return "\n".join([
        f"💳 Новая заявка на подписку #{request.id}",
        "",
        f"Пользователь: {name}",
        f"ID Telegram: {user.telegram_id}",
        f"Имя пользователя: {username}",
        "",
        f"Тариф: {request.plan_title}",
        f"Условия: {request.plan_description}",
        f"Стоимость: {format_price(request.price_rub)}",
    ])


async def create_subscription_request(session: AsyncSession, user: User, plan_id: str) -> SubscriptionRequest:
    plan = get_subscription_plan(plan_id)
    if plan is None:
        raise ValueError("Тариф не найден.")
    payment_config = await get_subscription_payment_config(session)
    request = SubscriptionRequest(
        user_id=user.id,
        telegram_id=user.telegram_id,
        username=user.username,
        plan_id=plan.id,
        plan_group=plan.group,
        plan_title=plan.title,
        plan_description=plan.description,
        price_rub=plan.price_rub,
        signals_limit=plan.signals_limit,
        duration_days=plan.duration_days,
        duration_hours=plan.duration_hours,
        includes_vip=plan.includes_vip,
        includes_all_signals=plan.includes_all_signals,
        includes_analytics=plan.includes_analytics,
        payment_details=payment_config.payment_details,
        specialist_contact=payment_config.specialist_contact,
    )
    session.add(request)
    await session.flush()
    return request


def format_subscription_activation_user_text(request: SubscriptionRequest) -> str:
    if request.signals_limit is not None:
        access_line = f"Осталось сигналов: {request.signals_limit}"
    elif request.duration_hours is not None:
        access_line = f"Срок доступа: {request.duration_hours} часов"
    elif request.duration_days is not None:
        access_line = f"Срок доступа: {request.duration_days} дней"
    else:
        access_line = "Доступ активирован без ограничения по сроку."

    return "\n".join([
        "✅ Подписка активирована.",
        "",
        f"Заявка: #{request.id}",
        f"Тариф: {request.plan_title}",
        f"Условия: {request.plan_description}",
        access_line,
        "",
        "Теперь сигналы будут приходить по условиям выбранного тарифа.",
    ])
