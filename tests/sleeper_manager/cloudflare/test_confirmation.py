"""A link preview cannot acknowledge an action; only an explicit form submission can."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from sleeper_manager.cloudflare.confirmation import confirmation
from sleeper_manager.cloudflare.form_pages import json_page
from sleeper_manager.cloudflare.worker import Default
from sleeper_manager.persistence.base import (
    AcknowledgementAction,
    ActionTokenRecord,
    RecommendationRecord,
    RecommendationStatus,
)
from sleeper_manager.persistence.d1 import D1_SCHEMA
from sleeper_manager.persistence.manager_store import D1ManagerStore
from sleeper_manager.persistence.tokens import hash_action_token
from tests.sleeper_manager.persistence.test_d1 import FakeD1

NOW = datetime(2026, 10, 22, 21, tzinfo=UTC)
TOKEN = "scoped-test-token"
VALUES = {"token": TOKEN, "action": "locked"}


class Reader:
    def __init__(self, body: bytes):  # type: ignore[no-untyped-def]
        self.body = body

    async def read(self):  # type: ignore[no-untyped-def]
        body, self.body = self.body, b""
        return SimpleNamespace(done=not body, value=SimpleNamespace(to_py=lambda: body))

    async def cancel(self):  # type: ignore[no-untyped-def]
        pass

    def releaseLock(self):  # type: ignore[no-untyped-def]
        pass


def request(method="GET", origin="https://worker.test", values=None):  # type: ignore[no-untyped-def]
    body = urlencode(values or VALUES).encode()
    return SimpleNamespace(
        method=method,
        url="https://worker.test/ack?" + urlencode(VALUES),
        headers={"origin": origin, "content-type": "application/x-www-form-urlencoded"},
        body=SimpleNamespace(getReader=lambda: Reader(body)),
    )


@pytest.fixture
def store():  # type: ignore[no-untyped-def]
    database = FakeD1()
    asyncio.run(database.exec(D1_SCHEMA))
    repo = D1ManagerStore(database)
    asyncio.run(
        repo.create_recommendation(
            RecommendationRecord(
                recommendation_id="rec",
                idempotency_key="rec",
                league_id="league",
                fantasy_week=1,
                player_id="player",
                game_id="game",
                decision_type="live_lock_in",
                title="Pass <player>?",
                message="A <script> must be escaped",
                deadline=NOW + timedelta(hours=1),
                policy_version="v1",
                created_at=NOW,
                status=RecommendationStatus.PENDING,
            )
        )
    )
    asyncio.run(
        repo.create_action_token(
            ActionTokenRecord(
                token_hash=hash_action_token(TOKEN),
                recommendation_id="rec",
                action=AcknowledgementAction.LOCKED,
                created_at=NOW,
                expires_at=NOW + timedelta(hours=1),
            )
        )
    )
    return repo


def test_get_and_preview_never_consume_token(store):  # type: ignore[no-untyped-def]
    for _ in range(2):
        result = asyncio.run(confirmation(request(), store, VALUES, now=NOW))
        assert result.status == 200
        assert '<form method="post"' in result.body
        assert "&lt;script&gt;" in result.body
        assert result.headers["cache-control"] == "no-store"
        assert result.headers["referrer-policy"] == "same-origin"
        assert "Save only after locking this game in Sleeper." in result.body
    row = asyncio.run(store.preview_token(hash_action_token(TOKEN)))
    assert row["used_at"] is None


def test_pass_records_a_choice_without_claiming_a_sleeper_action(store):  # type: ignore[no-untyped-def]
    token = "pass-test-token"
    values = {"token": token, "action": "passed"}
    asyncio.run(
        store.create_action_token(
            ActionTokenRecord(
                token_hash=hash_action_token(token),
                recommendation_id="rec",
                action=AcknowledgementAction.PASSED,
                created_at=NOW,
                expires_at=NOW + timedelta(hours=1),
            )
        )
    )
    preview = asyncio.run(confirmation(request(values=values), store, values, now=NOW))
    assert "Choose not to lock this game" in preview.body
    assert "You may keep the player starting or substitute them." in preview.body
    assert "No action in Sleeper is required." in preview.body
    assert "Save only after" not in preview.body
    assert asyncio.run(store.preview_token(hash_action_token(token)))["used_at"] is None

    saved = asyncio.run(confirmation(request("POST", values=values), store, {}, now=NOW))
    assert saved.status == 200
    assert not asyncio.run(store.is_locked("rec"))
    assert (
        store._database.connection.execute(
            "SELECT action FROM acknowledgements WHERE recommendation_id='rec'"
        ).fetchone()[0]
        == "passed"
    )


def test_post_records_once(store):  # type: ignore[no-untyped-def]
    for _ in range(2):
        result = asyncio.run(confirmation(request("POST"), store, {}, now=NOW))
        assert result.status == 200
        assert "Decision saved" in result.body
    assert asyncio.run(store.is_locked("rec"))
    work = asyncio.run(store.list_scheduled_work())
    assert len(work) == 1
    assert work[0].dedupe_key == "manual:ack:rec"
    assert (
        store._database.connection.execute("SELECT COUNT(*) FROM acknowledgements").fetchone()[0]
        == 1
    )


@pytest.mark.parametrize(
    "origin", [None, "null", "https://evil.test", "https://worker.test.evil.test"]
)
def test_cross_site_post_does_not_consume(store, origin):  # type: ignore[no-untyped-def]
    result = asyncio.run(confirmation(request("POST", origin), store, {}, now=NOW))
    assert result.status == 403
    assert not asyncio.run(store.is_locked("rec"))


def test_expired_and_wrong_action_links(store):  # type: ignore[no-untyped-def]
    expired = asyncio.run(confirmation(request(), store, VALUES, now=NOW + timedelta(hours=1)))
    wrong = asyncio.run(confirmation(request(), store, {**VALUES, "action": "passed"}, now=NOW))
    assert expired.status == 410
    assert wrong.status == 404


def test_worker_get_is_non_mutating(store):  # type: ignore[no-untyped-def]
    worker = Default()
    worker.env = SimpleNamespace(sleeper_manager_state=store._database)
    result = asyncio.run(worker.fetch(request()))
    assert result.headers["content-type"].startswith("text/html")
    assert not asyncio.run(store.is_locked("rec"))


def test_corrected_confirmation_cannot_resurface_an_old_acknowledgement(store):  # type: ignore[no-untyped-def]
    asyncio.run(confirmation(request("POST"), store, {}, now=NOW))
    asyncio.run(
        store._run("UPDATE recommendations SET status='superseded' WHERE recommendation_id='rec'")
    )
    for method in ("GET", "POST"):
        result = asyncio.run(confirmation(request(method), store, VALUES, now=NOW))
        assert result.status == 409
        assert "Confirmation replaced" in result.body


def test_confirmation_document_preserves_explicit_submission_and_token_checks(store):  # type: ignore[no-untyped-def]
    preview = asyncio.run(confirmation(request(), store, VALUES, now=NOW))
    document = json.loads(json_page(preview).body)
    assert document["status"] == "confirmation"
    assert document["token"] == TOKEN and document["action"] == "locked"
    assert document["message"] == "A <script> must be escaped"
    assert "Save only after locking" in document["instruction"]
    assert not asyncio.run(store.is_locked("rec"))
    rejected = asyncio.run(confirmation(request("POST", "https://evil.test"), store, {}, now=NOW))
    assert rejected.status == 403
    assert json.loads(json_page(rejected).body)["title"] == "Confirmation rejected"
    assert not asyncio.run(store.is_locked("rec"))
