from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import SignalDecisionLog
from app.domain.models import SignalDecision
from app.services.rules_config import get_signal_rules


def _algorithm_version(decision: SignalDecision) -> str:
    value = decision.payload.get("algorithm_version") if decision.payload else None
    if value:
        return str(value)
    return str(get_signal_rules().get("algorithm", {}).get("version", "v1"))


async def record_decision_log(
    session: AsyncSession,
    *,
    match_id: int,
    decision: SignalDecision,
    import_batch_id: int | None = None,
    source: str = "import",
) -> SignalDecisionLog:
    log = SignalDecisionLog(
        match_id=match_id,
        import_batch_id=import_batch_id,
        algorithm_version=_algorithm_version(decision),
        source=source,
        suitable=decision.suitable,
        side=decision.side,
        selected_player=decision.selected_player,
        probability=decision.probability,
        level=decision.level,
        signal_type=decision.signal_type,
        reason=decision.reason,
        decision_payload=decision.payload or {},
        decision_trace=[item.to_dict() for item in decision.traces],
    )
    session.add(log)
    return log
