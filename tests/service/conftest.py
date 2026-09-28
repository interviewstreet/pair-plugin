"""Pytest fixtures for tests/service/*.

Shared, plain (non-fixture) helpers live in ``helpers.py`` so other test
files can import them directly instead of relying on conftest attribute
lookup.
"""

from __future__ import annotations

import pytest

from pair.service.jobs import JobManager
from pair.service.storage import InMemoryStorage

from .helpers import (
    AGENTS_DIR,
    TEST_TOKEN,
    FakeRunner,
    default_writer_replies,
)


@pytest.fixture
def storage() -> InMemoryStorage:
    return InMemoryStorage()


@pytest.fixture
def fake_runner() -> FakeRunner:
    return FakeRunner(replies=default_writer_replies())


@pytest.fixture
def manager(storage: InMemoryStorage, fake_runner: FakeRunner) -> JobManager:
    return JobManager(
        storage=storage,
        runner=fake_runner,
        agents_dir=AGENTS_DIR,
        login_check=lambda: True,
        max_concurrent=2,
    )


@pytest.fixture
def app_client(manager):
    from fastapi.testclient import TestClient

    from pair.service.app import ServiceConfig, create_app

    app = create_app(
        manager=manager,
        config=ServiceConfig(bearer_token=TEST_TOKEN),
        login_check=manager.login_check,
        access_token=lambda: "ya29.test-token",
    )
    with TestClient(app) as client:
        yield client
