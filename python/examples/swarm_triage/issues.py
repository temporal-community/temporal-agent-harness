"""A synthetic issue backlog for the swarm triage example."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

# Team names, in the order teams are assigned.
AREAS = [
    "auth", "billing", "search", "ui", "api", "mobile", "infra", "data", "docs", "growth",
    "payments", "notifications", "onboarding", "reporting", "security", "sync", "storage",
    "admin", "integrations", "editor", "media", "chat", "calendar", "export", "i18n", "perf",
]  # fmt: skip

_SYMPTOMS = [
    "crash on save", "slow first load", "wrong total", "error after login", "missing button",
    "stale cache", "timeout on upload", "double charge", "blank screen", "bad redirect",
    "flaky retry", "unclear copy", "memory leak", "broken link", "lost draft",
]  # fmt: skip


@dataclass(frozen=True)
class Issue:
    team: str
    title: str
    payload: dict[str, Any]


def synthetic_issues(count: int, teams: list[str], rng: random.Random) -> list[Issue]:
    """``count`` issues spread over ``teams``, drawn from ``rng`` (``workflow.random()`` in the
    dispatcher, so the backlog is the same on replay)."""
    issues = []
    for number in range(1, count + 1):
        team = teams[rng.randrange(len(teams))]
        symptom = _SYMPTOMS[rng.randrange(len(_SYMPTOMS))]
        issues.append(
            Issue(
                team=team,
                title=f"#{number} {team}: {symptom}",
                payload={"number": number, "priority": f"p{rng.randrange(4)}"},
            )
        )
    return issues
