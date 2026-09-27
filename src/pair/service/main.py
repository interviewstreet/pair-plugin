"""Process entrypoint for the container: ``uvicorn pair.service.main:app``.

``app.create_app`` takes its storage, runner, and agents directory as
arguments so the tests can inject fakes. Something still has to build those
from the environment for a real deployment, and that is all this module is.
Nothing here is imported by the tests' app fixtures.

Environment:

* ``PAIR_SERVICE_TOKEN``  — the bearer token every route requires. Required.
* ``PAIR_SERVICE_BUCKET`` — the GCS bucket holding ``reviews/`` and ``payloads/``.
  Required unless ``PAIR_SERVICE_STORAGE=memory``.
* ``PAIR_SERVICE_STORAGE`` — ``gcs`` (default) or ``memory``. ``memory`` keeps
  everything in the process and is only useful for a smoke test of the image.
* ``PAIR_SERVICE_AGENTS_DIR`` — where the four writer prompts live. Defaults to
  the ``agents/`` directory beside the installed source, which is what
  ``service/Dockerfile`` copies to ``/app/agents``.
* ``PAIR_SERVICE_MAX_CONCURRENT`` — jobs allowed to run at once. Defaults to 2.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from fastapi import FastAPI

from pair.service.app import ServiceConfig, create_app
from pair.service.claude import SubprocessRunner, WRITERS, default_login_check
from pair.service.jobs import JobManager
from pair.service.storage import InMemoryStorage


class ConfigError(RuntimeError):
    """The environment doesn't describe a runnable service."""


def default_agents_dir() -> Path:
    """The ``agents/`` directory that ships next to the source tree.

    ``src/pair/service/main.py`` -> ``<root>/agents``. The Dockerfile copies
    both ``src`` and ``agents`` into ``/app``, so this resolves to
    ``/app/agents`` in the image.
    """
    return Path(__file__).resolve().parents[3] / "agents"


def resolve_agents_dir(env: Mapping[str, str]) -> Path:
    configured = env.get("PAIR_SERVICE_AGENTS_DIR")
    candidates = [Path(configured)] if configured else [default_agents_dir(), Path.cwd() / "agents"]
    for path in candidates:
        if all((path / f"{writer}.md").is_file() for writer in WRITERS.values()):
            return path
    wanted = ", ".join(f"{w}.md" for w in sorted(WRITERS.values()))
    raise ConfigError(
        "no agents directory holds the writer prompts "
        f"({wanted}); looked in {', '.join(str(p) for p in candidates)}. "
        "Set PAIR_SERVICE_AGENTS_DIR."
    )


def build_storage(env: Mapping[str, str]):
    kind = (env.get("PAIR_SERVICE_STORAGE") or "gcs").lower()
    if kind == "memory":
        return InMemoryStorage()
    if kind != "gcs":
        raise ConfigError(f"PAIR_SERVICE_STORAGE must be 'gcs' or 'memory', got {kind!r}")
    bucket = env.get("PAIR_SERVICE_BUCKET")
    if not bucket:
        raise ConfigError("PAIR_SERVICE_BUCKET is required when PAIR_SERVICE_STORAGE is gcs")
    # Imported here so the CLI and the tests never need google-cloud-storage.
    from google.cloud import storage as gcs

    from pair.service.storage import GCSStorage

    return GCSStorage(gcs.Client().bucket(bucket))


def create_app_from_env(env: Mapping[str, str] | None = None) -> FastAPI:
    env = os.environ if env is None else env
    token = env.get("PAIR_SERVICE_TOKEN") or ""
    if not token:
        raise ConfigError("PAIR_SERVICE_TOKEN is required")
    try:
        max_concurrent = int(env.get("PAIR_SERVICE_MAX_CONCURRENT") or 2)
    except ValueError as exc:
        raise ConfigError(f"PAIR_SERVICE_MAX_CONCURRENT must be an integer: {exc}") from exc
    manager = JobManager(
        storage=build_storage(env),
        runner=SubprocessRunner(),
        agents_dir=resolve_agents_dir(env),
        login_check=default_login_check,
        max_concurrent=max_concurrent,
    )
    return create_app(manager=manager, config=ServiceConfig(bearer_token=token))


def __getattr__(name: str):
    """Build ``app`` only when something asks for it.

    ``uvicorn pair.service.main:app`` imports the module and then reads the
    attribute, so this is enough for the container, while the helpers above
    stay importable without a configured environment.
    """
    if name == "app":
        built = create_app_from_env()
        globals()["app"] = built
        return built
    raise AttributeError(name)
