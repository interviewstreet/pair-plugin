"""Manifest construction for reviews/<key>/manifest.json.

The five section names are fixed by the contract and are present from the
initial ``queued`` manifest onward. Sections carry ``{status, updated_at,
error}`` with ``error`` == ``None`` when the section did not fail — never an
empty string.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

SCHEMA_VERSION = 1

SECTION_NAMES: tuple[str, ...] = (
    "summary",
    "look_first",
    "plan_vs_result",
    "pushback",
    "side_effects",
)

STATUSES = frozenset({"queued", "running", "done", "failed"})


def iso_now(now: datetime | None = None) -> str:
    """RFC3339 with a ``Z`` offset, second precision."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class SectionState:
    status: str = "queued"
    updated_at: str = ""
    error: str | None = None

    def to_dict(self) -> dict:
        # ``error`` is None (not "") when the section is fine — see contract.
        return {
            "status": self.status,
            "updated_at": self.updated_at,
            "error": self.error,
        }


@dataclass
class ManifestState:
    """In-memory manifest that job code mutates as sections progress."""

    key: str
    repo: str
    pr: int
    title: str
    base_sha: str
    head_sha: str
    sections: dict[str, SectionState] = field(default_factory=dict)

    @classmethod
    def initial(
        cls,
        *,
        key: str,
        repo: str,
        pr: int,
        title: str,
        base_sha: str,
        head_sha: str,
        now: datetime | None = None,
    ) -> "ManifestState":
        """All five sections queued at the same ``updated_at``."""
        stamp = iso_now(now)
        sections = {
            name: SectionState(status="queued", updated_at=stamp, error=None)
            for name in SECTION_NAMES
        }
        return cls(
            key=key,
            repo=repo,
            pr=pr,
            title=title,
            base_sha=base_sha,
            head_sha=head_sha,
            sections=sections,
        )

    def mark(
        self,
        section: str,
        status: str,
        *,
        error: str | None = None,
        now: datetime | None = None,
    ) -> None:
        if section not in self.sections:
            raise KeyError(section)
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        if error == "":
            # Contract forbids empty strings; normalise to null.
            error = None
        self.sections[section] = SectionState(
            status=status, updated_at=iso_now(now), error=error
        )

    @property
    def top_status(self) -> str:
        vals = [s.status for s in self.sections.values()]
        if all(v == "done" for v in vals):
            return "done"
        if any(v == "failed" for v in vals):
            return "failed"
        if any(v in ("running", "done", "failed") for v in vals):
            return "running"
        return "queued"

    def to_dict(self, *, status: str | None = None) -> dict:
        st = status if status is not None else self.top_status
        if st not in STATUSES:
            raise ValueError(f"unknown status {st!r}")
        return {
            "schema_version": SCHEMA_VERSION,
            "key": self.key,
            "repo": self.repo,
            "pr": self.pr,
            "title": self.title,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "status": st,
            "sections": {name: self.sections[name].to_dict() for name in SECTION_NAMES},
        }

    def to_json(self, *, status: str | None = None, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(status=status), indent=indent, ensure_ascii=False)


def sections_from_iter(states: Iterable[tuple[str, SectionState]]) -> dict[str, SectionState]:
    """Small helper for tests that want to build a manifest from a list."""
    return {name: state for name, state in states}
