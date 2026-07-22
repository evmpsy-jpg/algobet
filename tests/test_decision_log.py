from __future__ import annotations

import pytest

from app.domain.models import RuleTrace, SignalDecision
from app.services.decision_log import record_decision_log
from app.services.rules_config import get_signal_rules


class DummySession:
    def __init__(self) -> None:
        self.added = []

    def add(self, item: object) -> None:
        self.added.append(item)


@pytest.fixture(autouse=True)
def clear_rules_cache() -> None:
    get_signal_rules.cache_clear()
    yield
    get_signal_rules.cache_clear()


@pytest.mark.asyncio
async def test_record_decision_log_stores_decision_trace() -> None:
    session = DummySession()
    decision = SignalDecision(
        suitable=True,
        side=1,
        selected_player="Игрок 1",
        probability=91,
        level="TOP",
        signal_type="SET_TOP",
        payload={"algorithm_version": "v-test"},
        traces=[
            RuleTrace(
                code="CP_MIN",
                label="Количество H2H",
                passed=True,
                actual=5,
                expected=">= 5",
                message="ok",
            )
        ],
    )

    log = await record_decision_log(
        session,  # type: ignore[arg-type]
        match_id=10,
        import_batch_id=20,
        decision=decision,
        source="test",
    )

    assert session.added == [log]
    assert log.match_id == 10
    assert log.import_batch_id == 20
    assert log.algorithm_version == "v-test"
    assert log.suitable is True
    assert log.side == 1
    assert log.probability == 91
    assert log.level == "TOP"
    assert log.decision_trace[0]["code"] == "CP_MIN"
    assert log.decision_trace[0]["passed"] is True
