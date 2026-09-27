"""Bundle key grammar.

Per the contract, a key is ``<owner>__<repo>__<n>__<head_sha[:12]>``: exactly
four ``__``-separated parts. Owner and repo may themselves contain a single
underscore, so parsing is done by splitting on ``__`` rather than a regex over
underscores.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEX12 = re.compile(r"^[0-9a-f]{12}$")
_SIMPLE = re.compile(r"^[A-Za-z0-9._-]+$")
_NUMBER = re.compile(r"^(0|[1-9][0-9]*)$")


class BadKey(ValueError):
    """Raised when a string does not match the key grammar."""


@dataclass(frozen=True)
class ReviewKey:
    owner: str
    repo: str
    pr: int
    head12: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    def __str__(self) -> str:
        return f"{self.owner}__{self.repo}__{self.pr}__{self.head12}"


def parse_key(key: str) -> ReviewKey:
    if not isinstance(key, str) or not key:
        raise BadKey("empty key")
    parts = key.split("__")
    if len(parts) != 4:
        raise BadKey(f"expected 4 __-parts, got {len(parts)}")
    owner, repo, n, head = parts
    if not (_SIMPLE.match(owner) and _SIMPLE.match(repo)):
        raise BadKey("owner or repo has invalid characters")
    if not _NUMBER.match(n):
        raise BadKey("pr number is not a positive decimal without leading zeros")
    if not _HEX12.match(head):
        raise BadKey("head part is not exactly 12 lowercase hex chars")
    return ReviewKey(owner=owner, repo=repo, pr=int(n), head12=head)
