"""The container entrypoint: `uvicorn pair.service.main:app`.

These tests build the app the way the image does, from the environment, with
``PAIR_SERVICE_STORAGE=memory`` so no GCS client is constructed. They exist
because the app itself is a factory: a wrong module path or a missing
``agents/`` directory in the image would only show up at run time otherwise.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pair.service.claude import WRITERS
from pair.service.main import (
    ConfigError,
    create_app_from_env,
    default_agents_dir,
    resolve_agents_dir,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_default_agents_dir_holds_the_four_writer_prompts():
    """This is the directory service/Dockerfile copies to /app/agents."""
    agents = default_agents_dir()
    assert agents == REPO_ROOT / "agents"
    for writer in WRITERS.values():
        assert (agents / f"{writer}.md").is_file(), writer


def test_resolve_agents_dir_prefers_the_configured_path(tmp_path):
    for writer in WRITERS.values():
        (tmp_path / f"{writer}.md").write_text("stub\n")
    assert resolve_agents_dir({"PAIR_SERVICE_AGENTS_DIR": str(tmp_path)}) == tmp_path


def test_resolve_agents_dir_falls_back_to_the_shipped_directory():
    assert resolve_agents_dir({}) == default_agents_dir()


def test_resolve_agents_dir_errors_when_a_writer_prompt_is_missing(tmp_path):
    (tmp_path / "pair-summary.md").write_text("stub\n")
    with pytest.raises(ConfigError, match="pair-pushback.md"):
        resolve_agents_dir({"PAIR_SERVICE_AGENTS_DIR": str(tmp_path)})


def test_create_app_from_env_requires_a_bearer_token():
    with pytest.raises(ConfigError, match="PAIR_SERVICE_TOKEN"):
        create_app_from_env({"PAIR_SERVICE_STORAGE": "memory"})


def test_create_app_from_env_requires_a_bucket_for_gcs():
    with pytest.raises(ConfigError, match="PAIR_SERVICE_BUCKET"):
        create_app_from_env({"PAIR_SERVICE_TOKEN": "t"})


def test_create_app_from_env_rejects_an_unknown_storage_kind():
    with pytest.raises(ConfigError, match="gcs"):
        create_app_from_env({"PAIR_SERVICE_TOKEN": "t", "PAIR_SERVICE_STORAGE": "s3"})


def test_app_from_env_serves_the_authenticated_routes():
    app = create_app_from_env({
        "PAIR_SERVICE_TOKEN": "env-token",
        "PAIR_SERVICE_STORAGE": "memory",
    })
    with TestClient(app) as client:
        assert client.get("/health").status_code == 401
        # 404 rather than 401: the env token was accepted, and the key is
        # simply unknown to the fresh in-memory storage. /health is left alone
        # because it runs the real `claude --version` login check.
        assert client.get(
            "/reviews/acme__demo__1__abcdef012345",
            headers={"Authorization": "Bearer env-token"},
        ).status_code == 404
