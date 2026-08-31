import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from test_d1 import FakeD1
from test_daily_plan import (
    GAME_START,
    NOW,
    _game,
    _inputs,
    _profile,
    _projections,
    _quality,
    _snapshot,
    _workflow,
)
from test_notification_loop import RecordingSender
from test_postgame_lock_in import _box, _summary_result

from sleeper_manager.cloudflare.dispatcher import dispatch_due_work
from sleeper_manager.cloudflare.planning import CloudflarePlanningAssembly
from sleeper_manager.cloudflare.runtime import run_scheduled
from sleeper_manager.cloudflare.scheduler_types import FailureCategory, ScheduledRunStatus
from sleeper_manager.decisions.weekly_plan import WeeklyPlanPolicyConfig
from sleeper_manager.domain.nba import (
    DataQualityReport,
    DataQualityState,
    GameStatus,
    GameSummary,
    ProviderResult,
)
from sleeper_manager.domain.runtime_policy import default_runtime_policy
from sleeper_manager.notifications.dispatcher import NotificationDispatcher
from sleeper_manager.persistence.async_sqlite import AsyncSQLiteStateRepository
from sleeper_manager.persistence.base import (
    DueWorkKind,
    RuntimePolicyRecord,
    ScheduledWorkRecord,
    ScheduledWorkStatus,
)
from sleeper_manager.persistence.d1 import D1_SCHEMA, D1StateRepository
from sleeper_manager.workflows.notification_loop import NotificationLoop
from sleeper_manager.workflows.plan_rendering import WEEKLY_LINEUP_DECISION_TYPE
from sleeper_manager.workflows.planning_collection import CollectedLiveEvidence
from sleeper_manager.workflows.planning_inputs import LiveProjectionResult, ScheduleResourceResult

SEVEN_AM_CENTRAL = datetime(2026, 1, 7, 13, tzinfo=UTC)
SIX_AM_CENTRAL = datetime(2026, 1, 7, 12, tzinfo=UTC)
PLAN_POLICY = WeeklyPlanPolicyConfig(scenario_count=8)
POLICY = default_runtime_policy(history_version="history-v1")
LATER_GAME_START = GAME_START + timedelta(hours=6)


def _assembly(inputs=None, *, decision_time: datetime = NOW) -> CloudflarePlanningAssembly:
    later = replace(_game("g2"), start_time=LATER_GAME_START)
    extra = (
        LiveProjectionResult("p1", "g2", _snapshot("p1", "g2", expected=10.0), None),
        LiveProjectionResult("p2", "g2", _snapshot("p2", "g2", expected=8.0), None),
    )
    planning_inputs = inputs or _inputs(
        _profile(starter_ids=("p1", None)),
        projections=_projections(p1_expected=10.0, p2_expected=30.0) + extra,
    )
    planning_inputs = replace(
        planning_inputs,
        schedule_results=(
            ScheduleResourceResult(
                "espn:team-schedule:12",
                (planning_inputs.schedule_results[0].games[0], later),
                _quality(),
            ),
        ),
    )
    return CloudflarePlanningAssembly(
        evidence=CollectedLiveEvidence(planning_inputs, decision_time, ()),
        player_names={"p1": "Ann", "p2": "Ben"},
        local_timezone=ZoneInfo("America/Chicago"),
    )


async def _save_policy(repository: AsyncSQLiteStateRepository, *, at: datetime = NOW) -> None:
    await repository.save_runtime_policy(RuntimePolicyRecord(POLICY.version, POLICY.to_json(), at))


def _collector(assembly: CloudflarePlanningAssembly):
    calls: list[datetime] = []

    async def collect(*, repository, policy, scheduled_at):  # type: ignore[no-untyped-def]
        del repository, policy
        calls.append(scheduled_at)
        return assembly

    return collect, calls


async def _dispatch(
    repository: AsyncSQLiteStateRepository,
    notifications: NotificationLoop,
    *,
    scheduled_at: datetime,
    correlation_id: str = "run-1",
    collect=None,
    calls: list[datetime] | None = None,
):
    if collect is None:
        collect, calls = _collector(_assembly(decision_time=scheduled_at))
    return await dispatch_due_work(
        repository,
        notifications=notifications,
        collect=collect,
        scheduled_at=scheduled_at,
        correlation_id=correlation_id,
        open_sleeper_url="https://sleeper.com/league",
        plan_policy=PLAN_POLICY,
    ), calls


def test_wake_outside_due_windows_skips_provider_io(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository, sender, notifications = _workflow(tmp_path, clock=lambda: SIX_AM_CENTRAL)
        await repository.initialize()
        await _save_policy(repository, at=SIX_AM_CENTRAL)
        summary, calls = await _dispatch(
            repository,
            notifications,
            scheduled_at=SIX_AM_CENTRAL,
        )
        work = await repository.list_scheduled_work(kind=DueWorkKind.DAILY)
        assert summary.status is ScheduledRunStatus.NO_ACTION
        assert summary.claimed_count == 0
        assert calls == []
        assert len(sender.messages) == 0
        assert len(work) == 1
        assert work[0].status is ScheduledWorkStatus.PENDING
        assert work[0].due_at == SEVEN_AM_CENTRAL

    asyncio.run(exercise())


def test_missing_runtime_policy_fails_closed_without_provider_io(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository, sender, notifications = _workflow(tmp_path, clock=lambda: SEVEN_AM_CENTRAL)
        await repository.initialize()
        summary, calls = await _dispatch(
            repository,
            notifications,
            scheduled_at=SEVEN_AM_CENTRAL,
        )
        assert summary.status is ScheduledRunStatus.BLOCKED
        assert summary.failure_category is FailureCategory.CONFIGURATION
        assert summary.claimed_count == 0
        assert calls == []
        assert len(sender.messages) == 0
        assert await repository.list_scheduled_work() == ()

    asyncio.run(exercise())


def test_daily_due_work_collects_once_and_notifies(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository, sender, notifications = _workflow(tmp_path, clock=lambda: NOW)
        await repository.initialize()
        await _save_policy(repository)
        summary, calls = await _dispatch(
            repository,
            notifications,
            scheduled_at=NOW,
        )
        pending = await repository.list_pending_recommendations(
            "league-1",
            1,
            decision_type=WEEKLY_LINEUP_DECISION_TYPE,
        )
        daily = await repository.list_scheduled_work(kind=DueWorkKind.DAILY)
        pre_tipoff = await repository.list_scheduled_work(kind=DueWorkKind.PRE_TIPOFF)
        assert summary.status is ScheduledRunStatus.SUCCESS
        assert summary.claimed_count == 1
        assert calls == [NOW]
        assert len(sender.messages) == 1
        assert [action.label for action in sender.messages[0].actions] == ["Open Sleeper"]
        assert len(pending) == 1
        assert pending[0].decision_type == WEEKLY_LINEUP_DECISION_TYPE
        assert json.loads(pending[0].trace_json)["trigger"] == "daily"
        assert daily[0].status is ScheduledWorkStatus.COMPLETED
        later = next(item for item in pre_tipoff if item.game_id == "g2")
        assert later.status is ScheduledWorkStatus.PENDING
        assert later.due_at == LATER_GAME_START - POLICY.move_lead_time
        postgame = await repository.list_scheduled_work(kind=DueWorkKind.POSTGAME)
        assert {item.game_id for item in postgame} == {"g1", "g2"}

    asyncio.run(exercise())


def test_daily_and_pre_tipoff_coalesce_to_one_pre_tipoff_plan(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        scheduled_at = GAME_START - POLICY.move_lead_time - timedelta(seconds=1)
        repository, sender, notifications = _workflow(tmp_path, clock=lambda: scheduled_at)
        await repository.initialize()
        await _save_policy(repository, at=scheduled_at)
        await repository.upsert_scheduled_work(
            ScheduledWorkRecord(
                work_id="pre-g1",
                dedupe_key="pre_tipoff:g1",
                kind=DueWorkKind.PRE_TIPOFF,
                due_at=scheduled_at,
                status=ScheduledWorkStatus.PENDING,
                game_id="g1",
                deadline=GAME_START,
                created_at=scheduled_at,
                updated_at=scheduled_at,
            )
        )
        summary, calls = await _dispatch(
            repository,
            notifications,
            scheduled_at=scheduled_at,
        )
        pending = await repository.list_pending_recommendations(
            "league-1",
            1,
            decision_type=WEEKLY_LINEUP_DECISION_TYPE,
        )
        assert summary.status is ScheduledRunStatus.SUCCESS
        assert summary.claimed_count == 2
        assert calls == [scheduled_at]
        assert len(sender.messages) == 1
        assert len(pending) == 1
        assert json.loads(pending[0].trace_json)["trigger"] == "pre_tipoff"
        kinds = {attempt.kind for attempt in summary.attempts}
        assert kinds == {DueWorkKind.DAILY, DueWorkKind.PRE_TIPOFF}

    asyncio.run(exercise())


def test_overlapping_wakes_send_one_notification(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        await _save_policy(repository)
        sender = RecordingSender()
        notifications = NotificationLoop(
            repository,
            NotificationDispatcher(sender),
            acknowledgement_base_url="https://example.test/ack",
            clock=lambda: NOW,
        )
        collect, calls = _collector(_assembly())

        async def run(correlation_id: str):
            return await dispatch_due_work(
                repository,
                notifications=notifications,
                collect=collect,
                scheduled_at=NOW,
                correlation_id=correlation_id,
                open_sleeper_url="https://sleeper.com/league",
                plan_policy=PLAN_POLICY,
            )

        first, second = await asyncio.gather(run("run-a"), run("run-b"))
        claimed = sorted([first.claimed_count, second.claimed_count])
        statuses = {first.status, second.status}
        pending = await repository.list_pending_recommendations(
            "league-1",
            1,
            decision_type=WEEKLY_LINEUP_DECISION_TYPE,
        )
        assert claimed[0] == 0
        assert claimed[1] >= 1
        assert ScheduledRunStatus.SUCCESS in statuses
        assert ScheduledRunStatus.NO_ACTION in statuses
        assert len(calls) == 1
        assert len(sender.messages) == 1
        assert len(pending) == 1

    asyncio.run(exercise())


def test_failed_delivery_retries_the_same_recommendation(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        repository = AsyncSQLiteStateRepository(tmp_path / "state.db")
        await repository.initialize()
        await _save_policy(repository)
        failing = RecordingSender(fail=True)
        first_loop = NotificationLoop(
            repository,
            NotificationDispatcher(failing),
            acknowledgement_base_url="https://example.test/ack",
            clock=lambda: NOW,
        )
        collect, calls = _collector(_assembly())
        first = await dispatch_due_work(
            repository,
            notifications=first_loop,
            collect=collect,
            scheduled_at=NOW,
            correlation_id="run-1",
            open_sleeper_url="https://sleeper.com/league",
            plan_policy=PLAN_POLICY,
        )
        retries = await repository.list_scheduled_work(kind=DueWorkKind.DELIVERY_RETRY)
        pending = await repository.list_pending_recommendations(
            "league-1",
            1,
            decision_type=WEEKLY_LINEUP_DECISION_TYPE,
        )
        assert first.status is ScheduledRunStatus.DELIVERY_FAILED
        assert len(failing.messages) == 1
        assert len(retries) == 1
        assert retries[0].status is ScheduledWorkStatus.PENDING
        assert retries[0].recommendation_id == pending[0].recommendation_id

        succeeding = RecordingSender()
        retry_loop = NotificationLoop(
            repository,
            NotificationDispatcher(succeeding),
            acknowledgement_base_url="https://example.test/ack",
            clock=lambda: NOW,
        )
        second = await dispatch_due_work(
            repository,
            notifications=retry_loop,
            collect=collect,
            scheduled_at=NOW,
            correlation_id="run-2",
            open_sleeper_url="https://sleeper.com/league",
            plan_policy=PLAN_POLICY,
        )
        retried = await repository.list_scheduled_work(kind=DueWorkKind.DELIVERY_RETRY)
        assert second.status is ScheduledRunStatus.SUCCESS
        assert second.claimed_count == 1
        assert second.attempts[0].kind is DueWorkKind.DELIVERY_RETRY
        assert calls == [NOW]
        assert len(succeeding.messages) == 1
        assert succeeding.messages[0].title == failing.messages[0].title
        assert succeeding.messages[0].message == failing.messages[0].message
        assert retried[0].status is ScheduledWorkStatus.COMPLETED
        assert retried[0].recommendation_id == pending[0].recommendation_id

    asyncio.run(exercise())


def test_scheduled_runtime_never_sends_placeholder() -> None:
    async def exercise() -> None:
        database = FakeD1()
        await database.exec(D1_SCHEMA)
        env = SimpleNamespace(
            ACKNOWLEDGEMENT_BASE_URL="https://example.test/ack",
            NTFY_TOPIC="topic",
            NTFY_BASE_URL="https://ntfy.test",
            NTFY_ACCESS_TOKEN="",
            DISCORD_WEBHOOK_URL="",
            SLEEPER_LEAGUE_ID="league-1",
            SLEEPER_USER_ID="user-1",
            OPEN_SLEEPER_URL="https://sleeper.com",
            sleeper_manager_state=database,
        )
        paths: list[str] = []

        async def fetcher(url: str):
            paths.append(url)
            raise AssertionError("scheduled no-op must not call providers")

        summary = await run_scheduled(
            env,
            fetcher,
            scheduled_at=SIX_AM_CENTRAL,
            correlation_id="cron-1",
        )
        repository = D1StateRepository(database)
        pending = await repository.list_pending_recommendations(
            "league-1",
            1,
            decision_type="placeholder_lock_in",
        )
        weekly = await repository.list_pending_recommendations(
            "league-1",
            1,
            decision_type=WEEKLY_LINEUP_DECISION_TYPE,
        )
        assert summary["status"] == ScheduledRunStatus.BLOCKED.value
        assert summary["failure_category"] == FailureCategory.CONFIGURATION.value
        assert summary["claimed_count"] == 0
        assert paths == []
        assert pending == ()
        assert weekly == ()

    asyncio.run(exercise())


def test_due_postgame_watch_fetches_espn_summary_directly(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Claimed postgame work must poll ESPN once and keep the five-minute watch open."""

    async def exercise() -> None:
        repository, sender, notifications = _workflow(tmp_path, clock=lambda: NOW)
        await repository.initialize()
        await _save_policy(repository)
        await _dispatch(repository, notifications, scheduled_at=NOW)
        postgame_at = GAME_START + timedelta(hours=2)
        fetches: list[str] = []

        async def fetch(game_id: str) -> ProviderResult[GameSummary]:
            fetches.append(game_id)
            game = replace(_game(game_id), status=GameStatus.IN_PROGRESS, start_time=GAME_START)
            return ProviderResult(
                GameSummary(game, ()),
                DataQualityReport(
                    state=DataQualityState.PARTIAL,
                    resource=f"espn:game-summary:{game_id}",
                    record_count=0,
                    retrieved_at=postgame_at,
                    source_updated_at=None,
                    expires_at=None,
                ),
            )

        notifications._clock = lambda: postgame_at
        collect, calls = _collector(_assembly(decision_time=postgame_at))
        summary = await dispatch_due_work(
            repository,
            notifications=notifications,
            collect=collect,
            scheduled_at=postgame_at,
            correlation_id="postgame-1",
            open_sleeper_url="https://sleeper.com/league",
            plan_policy=PLAN_POLICY,
            fetch_game_summary=fetch,
        )
        postgame = await repository.list_scheduled_work(kind=DueWorkKind.POSTGAME)
        g1 = next(item for item in postgame if item.game_id == "g1")
        assert DueWorkKind.POSTGAME in {attempt.kind for attempt in summary.attempts}
        assert fetches == ["g1"]
        assert g1.status is ScheduledWorkStatus.RETRY
        assert g1.due_at == postgame_at + timedelta(minutes=5)
        lock_in = await repository.list_pending_recommendations(
            "league-1", 1, decision_type="live_lock_in"
        )
        assert lock_in == ()
        assert calls == [postgame_at]

    asyncio.run(exercise())


def test_postgame_delivery_failure_remains_visible_and_retryable(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Record failed action delivery instead of reporting a successful watch."""

    async def exercise() -> None:
        repository, sender, notifications = _workflow(tmp_path, clock=lambda: NOW)
        await repository.initialize()
        await _save_policy(repository)
        await _dispatch(repository, notifications, scheduled_at=NOW)
        summary_result = _summary_result(_box("401", points=20), _box("402", points=8))
        first_at = GAME_START + timedelta(hours=2)

        async def fetch(game_id: str) -> ProviderResult[GameSummary]:
            assert game_id == "g1"
            return summary_result

        collect, _ = _collector(_assembly(decision_time=first_at))
        notifications._clock = lambda: first_at
        await dispatch_due_work(
            repository,
            notifications=notifications,
            collect=collect,
            scheduled_at=first_at,
            correlation_id="postgame-1",
            open_sleeper_url="https://sleeper.com/league",
            plan_policy=PLAN_POLICY,
            fetch_game_summary=fetch,
        )
        second_at = first_at + timedelta(minutes=5)
        collect, _ = _collector(_assembly(decision_time=second_at))
        notifications._clock = lambda: second_at
        sender.fail = True

        result = await dispatch_due_work(
            repository,
            notifications=notifications,
            collect=collect,
            scheduled_at=second_at,
            correlation_id="postgame-2",
            open_sleeper_url="https://sleeper.com/league",
            plan_policy=PLAN_POLICY,
            fetch_game_summary=fetch,
        )

        attempt = next(item for item in result.attempts if item.kind is DueWorkKind.POSTGAME)
        work = await repository.list_scheduled_work(kind=DueWorkKind.POSTGAME)
        g1 = next(item for item in work if item.game_id == "g1")
        assert result.status is ScheduledRunStatus.DELIVERY_FAILED
        assert attempt.outcome is ScheduledRunStatus.DELIVERY_FAILED
        assert attempt.failure_category is FailureCategory.DELIVERY
        assert g1.status is ScheduledWorkStatus.RETRY
        assert g1.due_at == second_at + timedelta(minutes=5)

    asyncio.run(exercise())
