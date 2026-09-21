"""Verify the forecast archive's Cloudflare D1 binding boundary."""

import asyncio
from types import SimpleNamespace

import sleeper_manager.persistence.forecast_d1 as forecast_d1
from sleeper_manager.persistence.forecast_d1 import D1ForecastArchiveRepository
from sleeper_manager.persistence.forecast_rows import (
    artifact_insert_params,
    receipt_insert_params,
    revision_insert_params,
)
from sleeper_manager.persistence.forecast_statements import (
    FORECAST_ARCHIVE_SCHEMA,
    INSERT_FORECAST_ARTIFACT_SQL,
    INSERT_FORECAST_RECEIPT_SQL,
    INSERT_FORECAST_REVISION_SQL,
)
from tests.sleeper_manager.persistence.forecast_sqlite_support import successful_capture
from tests.sleeper_manager.persistence.test_d1 import FakeD1


def test_d1_repository_loads_exact_archive_identities() -> None:
    """Decode exact artifact, revision, and receipt rows through the D1 boundary."""

    database = FakeD1()
    asyncio.run(database.exec(FORECAST_ARCHIVE_SCHEMA))
    capture = successful_capture()
    assert capture.artifact is not None
    assert capture.revision is not None
    database.connection.execute(
        INSERT_FORECAST_ARTIFACT_SQL,
        artifact_insert_params(capture.artifact),
    )
    database.connection.execute(
        INSERT_FORECAST_REVISION_SQL,
        revision_insert_params(capture.revision),
    )
    database.connection.execute(
        INSERT_FORECAST_RECEIPT_SQL,
        receipt_insert_params(capture.receipt),
    )
    database.connection.commit()
    archive = D1ForecastArchiveRepository(database)

    assert asyncio.run(archive.initialize()) is None
    assert asyncio.run(archive.load_artifact(capture.artifact.payload_hash)) == capture.artifact
    assert asyncio.run(archive.load_revision(capture.revision.revision_id)) == capture.revision
    assert asyncio.run(archive.load_receipt(capture.receipt.receipt_id)) == capture.receipt
    assert asyncio.run(archive.load_artifact("f" * 64)) is None
    assert asyncio.run(archive.load_revision("missing")) is None
    assert asyncio.run(archive.load_receipt("missing")) is None


def test_d1_statement_converts_python_bytes_inside_pyodide(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Bind Python buffers as the typed arrays accepted by the D1 Worker API."""

    database = FakeD1()
    archive = D1ForecastArchiveRepository(database)
    monkeypatch.setattr(
        forecast_d1,
        "import_module",
        lambda name: SimpleNamespace(to_js=lambda value: list(value)),
    )

    archive._statement("SELECT ?", (b"\x01\x02",))

    assert database.last_bound == ([1, 2],)
