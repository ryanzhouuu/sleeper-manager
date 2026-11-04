"""Replans may be skipped only when nothing the decision reads has changed."""

import asyncio
from dataclasses import replace
from datetime import timedelta

from sleeper_manager.domain.lock_in import LockInOpportunityStatus
from sleeper_manager.domain.planning import PlanStatus
from sleeper_manager.domain.planning_actions import UndecidedScore
from sleeper_manager.persistence.lock_in_opportunities import LockInOpportunityKey
from sleeper_manager.workflows import coordinated_postgame
from sleeper_manager.workflows.decision_inputs import decision_input_fingerprint
from sleeper_manager.workflows.postgame_lock_in import run_postgame_lock_in
from tests.sleeper_manager.decisions.test_stable_advice import LIVE, _state_near_cutoff
from tests.sleeper_manager.workflows.postgame_lock_in_support import (
    POLICY,
    POSTGAME,
    SECOND_POLL,
    _box,
    _fetcher,
    _prepare,
    _summary_result,
)


def test_fingerprint_ignores_time_and_source_recency() -> None:
    state = _state_near_cutoff()
    later = replace(state, decision_time=state.decision_time + timedelta(minutes=5))
    assert decision_input_fingerprint(state, LIVE) == decision_input_fingerprint(later, LIVE)


def test_fingerprint_ignores_when_availability_was_last_reported() -> None:
    state = _state_near_cutoff()
    repolled = replace(
        state,
        opportunities=tuple(
            replace(row, availability_evidence_at=state.decision_time)
            for row in state.opportunities
        ),
    )
    earlier = replace(
        state,
        opportunities=tuple(
            replace(row, availability_evidence_at=state.decision_time - timedelta(hours=1))
            for row in state.opportunities
        ),
    )
    assert decision_input_fingerprint(repolled, LIVE) == decision_input_fingerprint(earlier, LIVE)


def test_fingerprint_changes_with_availability_status() -> None:
    state = _state_near_cutoff()
    injured = replace(
        state,
        opportunities=tuple(
            replace(row, availability_status="out") if row.sleeper_player_id == "swing" else row
            for row in state.opportunities
        ),
    )
    assert decision_input_fingerprint(state, LIVE) != decision_input_fingerprint(injured, LIVE)


def test_fingerprint_changes_with_projections() -> None:
    assert decision_input_fingerprint(_state_near_cutoff(0.7), LIVE) != decision_input_fingerprint(
        _state_near_cutoff(0.6), LIVE
    )


def test_fingerprint_changes_with_policy() -> None:
    state = _state_near_cutoff()
    assert decision_input_fingerprint(state, LIVE) != decision_input_fingerprint(
        state, replace(LIVE, minimum_lock_in_confidence=0.8)
    )


def test_fingerprint_changes_once_a_later_game_has_started() -> None:
    state = _state_near_cutoff()
    swing = next(row for row in state.opportunities if row.sleeper_player_id == "swing")
    after_tipoff = replace(state, decision_time=swing.scheduled_start + timedelta(minutes=1))
    assert decision_input_fingerprint(state, LIVE) != decision_input_fingerprint(after_tipoff, LIVE)


def test_second_poll_with_identical_inputs_skips_the_search(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[object] = []
    real = coordinated_postgame.build_team_recommendation

    def counting(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(coordinated_postgame, "build_team_recommendation", counting)

    async def exercise() -> None:
        repository, inputs, notifications, sender = await _prepare(tmp_path)
        fetch = _fetcher(
            _summary_result(_box("401", points=20), _box("402", points=8, did_play=False))
        )
        for poll, when in enumerate((POSTGAME, SECOND_POLL)):
            notifications._clock = lambda when=when: when
            result = await run_postgame_lock_in(
                "g1",
                inputs,
                decision_time=when,
                repository=repository,
                notifications=notifications,
                fetch_summary=fetch,
                runtime_policy=POLICY,
                open_sleeper_url="https://sleeper.com",
                poll_id=f"wake-{poll}",
                player_names={"p1": "Ann", "p2": "Ben"},
            )
        assert result.outcome == "duplicate"
        assert len(calls) == 1
        assert len(sender.messages) == 1

    asyncio.run(exercise())


def test_advice_kept_by_the_band_leaves_its_finals_actionable(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    real = coordinated_postgame.build_team_recommendation
    wobble = {"active": False}

    def scripted(*args, **kwargs):  # type: ignore[no-untyped-def]
        plan = real(*args, **kwargs)
        if not wobble["active"]:
            return plan
        return replace(
            plan,
            status=PlanStatus.NO_ACTION,
            score_actions=(),
            undecided_scores=(
                *plan.undecided_scores,
                UndecidedScore("p1", "g1", 0.3, 0.68),
            ),
        )

    monkeypatch.setattr(coordinated_postgame, "build_team_recommendation", scripted)
    monkeypatch.setattr(
        coordinated_postgame, "decision_input_fingerprint", lambda *args: str(wobble["active"])
    )

    async def exercise() -> None:
        repository, inputs, notifications, sender = await _prepare(tmp_path)
        fetch = _fetcher(
            _summary_result(_box("401", points=20), _box("402", points=8, did_play=False))
        )
        for poll, when in enumerate((POSTGAME, SECOND_POLL)):
            wobble["active"] = bool(poll)
            notifications._clock = lambda when=when: when
            await run_postgame_lock_in(
                "g1",
                inputs,
                decision_time=when,
                repository=repository,
                notifications=notifications,
                fetch_summary=fetch,
                runtime_policy=POLICY,
                open_sleeper_url="https://sleeper.com",
                poll_id=f"wake-{poll}",
                player_names={"p1": "Ann", "p2": "Ben"},
            )
        stored = await repository.get_lock_in_opportunity(
            LockInOpportunityKey("league-1", 1, 1, "p1", "g1")
        )
        assert stored is not None
        assert stored.status is LockInOpportunityStatus.ACTIONABLE
        assert len(sender.messages) == 1

    asyncio.run(exercise())
