from datetime import datetime

from app.database.models import MatchAnalysisRequest, User
from app.handlers.admin import (
    analysis_dashboard_keyboard,
    analysis_detail_keyboard,
    analysis_list_keyboard,
    format_analysis_request_detail,
)


def make_request() -> MatchAnalysisRequest:
    return MatchAnalysisRequest(
        id=12,
        user_id=1,
        telegram_id=315715137,
        username="tester",
        match_text="Игрок 1 — Игрок 2, 18:00",
        status="new",
        payment_details="Карта 0000",
        specialist_contact="@spec",
        created_at=datetime(2026, 7, 22, 7, 0),
        updated_at=datetime(2026, 7, 22, 7, 5),
    )


def make_user() -> User:
    return User(
        id=1,
        telegram_id=315715137,
        username="tester",
        first_name="Евгений",
        last_name="Мельников",
    )


def button_texts(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


def button_callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def test_analysis_dashboard_keyboard_links_statuses() -> None:
    markup = analysis_dashboard_keyboard({"new": 2, "done": 1})

    callbacks = button_callbacks(markup)

    assert "an:list:new:0" in callbacks
    assert "an:list:done:0" in callbacks
    assert "an:dashboard" in callbacks
    assert any("Новые (2)" in text for text in button_texts(markup))


def test_analysis_list_keyboard_links_request_detail() -> None:
    request = make_request()
    markup = analysis_list_keyboard([(request, make_user())], "new", page=0, total=1)

    callbacks = button_callbacks(markup)

    assert "an:view:12:new:0" in callbacks
    assert "an:dashboard" in callbacks


def test_analysis_detail_keyboard_changes_status_and_goes_back() -> None:
    markup = analysis_detail_keyboard(12, current_status="new", list_status="new", page=0)

    callbacks = button_callbacks(markup)

    assert "an:work:12:new:0" in callbacks
    assert "an:status:12:paid:new:0" in callbacks
    assert "an:status:12:in_progress:new:0" in callbacks
    assert "an:list:new:0" in callbacks
    assert "an:status:12:new:new:0" not in callbacks


def test_format_analysis_request_detail_contains_request_user_and_match() -> None:
    text = format_analysis_request_detail(make_request(), make_user())

    assert "Заявка на анализ #12" in text
    assert "Евгений Мельников" in text
    assert "Telegram ID: 315715137" in text
    assert "Игрок 1 — Игрок 2" in text
    assert "Карта 0000" in text
    assert "@spec" in text


def test_analysis_detail_keyboard_hides_work_button_for_done_request() -> None:
    markup = analysis_detail_keyboard(12, current_status="done", list_status="done", page=0)

    assert "an:work:12:done:0" not in button_callbacks(markup)
