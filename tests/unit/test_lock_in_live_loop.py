"""End-to-end live Lock-In capture, advice, acknowledgement, and score correction."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

from test_postgame_lock_in import (
    LATER_START,
    POLICY,
    POSTGAME,
    SECOND_POLL,
    _box,
    _fetcher,
    _prepare,
    _summary_result,
)

from sleeper_manager.domain.lock_in import LockInOpportunityStatus
from sleeper_manager.domain.planning import PlanningGameStatus
from sleeper_manager.persistence.base import AcknowledgementAction, AcknowledgementOutcome
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityKey
from sleeper_manager.persistence.tokens import hash_action_token
from sleeper_manager.workflows.lock_in_planning import refresh_terminal_lock_in_scores
from sleeper_manager.workflows.planning_inputs import build_live_team_week_state
from sleeper_manager.workflows.postgame_lock_in import run_postgame_lock_in


def test_lock_in_live_loop_ack_correction_and_later_planning(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Capture tipoff evidence, lock, acknowledge, then use a corrected fixed score."""

    async def exercise() -> None:
        repository, inputs, notifications, sender = await _prepare(tmp_path)
        summary = _summary_result(_box("401", points=20), _box("402", points=8))
        await run_postgame_lock_in(
            "g1",
            inputs,
            decision_time=POSTGAME,
            repository=repository,
            notifications=notifications,
            fetch_summary=_fetcher(summary),
            runtime_policy=POLICY,
            open_sleeper_url="https://sleeper.com",
            poll_id="wake-1",
            player_names={"p1": "Ann"},
        )
        notifications._clock = lambda: SECOND_POLL
        notified = await run_postgame_lock_in(
            "g1",
            inputs,
            decision_time=SECOND_POLL,
            repository=repository,
            notifications=notifications,
            fetch_summary=_fetcher(summary),
            runtime_policy=POLICY,
            open_sleeper_url="https://sleeper.com",
            poll_id="wake-2",
            player_names={"p1": "Ann"},
        )
        assert notified.outcome == "notified"
        assert len(sender.messages) == 1
        token = parse_qs(urlparse(sender.messages[0].actions[0].url).query)["token"][0]
        consumed = await repository.consume_action_token(
            hash_action_token(token),
            AcknowledgementAction.LOCKED,
            SECOND_POLL,
        )
        locked = await repository.get_lock_in_opportunity(
            LockInOpportunityKey("league-1", 1, 1, "p1", "g1")
        )
        assert consumed.outcome is AcknowledgementOutcome.APPLIED
        assert locked is not None
        assert locked.status is LockInOpportunityStatus.ACKNOWLEDGED_LOCKED
        assert locked.stable_score == 20.0

        corrected = _summary_result(_box("401", points=24), _box("402", points=8))
        day_one = SECOND_POLL + timedelta(hours=1)
        await refresh_terminal_lock_in_scores(
            inputs,
            repository=repository,
            fetch_summary=_fetcher(corrected),
            observed_at=day_one,
            poll_id="daily-1",
        )
        day_two = SECOND_POLL + timedelta(hours=2)
        refreshed = await refresh_terminal_lock_in_scores(
            inputs,
            repository=repository,
            fetch_summary=_fetcher(corrected),
            observed_at=day_two,
            poll_id="daily-2",
        )
        corrected_row = await repository.get_lock_in_opportunity(
            LockInOpportunityKey("league-1", 1, 1, "p1", "g1")
        )
        assert corrected_row is not None
        assert corrected_row.status is LockInOpportunityStatus.ACKNOWLEDGED_LOCKED
        assert corrected_row.stable_score == 24.0
        assert corrected_row.score_revision == 2
        state = build_live_team_week_state(refreshed, decision_time=day_two)
        assert not state.is_blocked
        fixed = next(item for item in state.fixed_slots if item.player_id == "p1")
        assert fixed.game_id == "g1"
        assert fixed.accepted_fantasy_score == 24.0
        later = next(
            item
            for item in state.opportunities
            if item.sleeper_player_id == "p1" and item.game_id == "g2"
        )
        assert later.status is PlanningGameStatus.SCHEDULED
        assert later.scheduled_start == LATER_START
        assert sender.messages[-1].title.startswith("Lock")
        assert len(sender.messages) == 1

    asyncio.run(exercise())
