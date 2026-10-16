# ABOUTME: Hypothesis profiles for the state-layer property tests — `default` for the
# normal run, `thorough` for a CI sweep (uv run pytest --hypothesis-profile=thorough).

"""Hypothesis profiles: `default` for the normal run, `thorough` for CI sweeps."""

from __future__ import annotations

from hypothesis import HealthCheck, settings

settings.register_profile(
    "default",
    max_examples=250,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "thorough",
    max_examples=5000,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("default")
