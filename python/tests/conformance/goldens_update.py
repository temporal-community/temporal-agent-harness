# ABOUTME: Rewrites conformance/goldens/ from a fresh recording of every scenario. Refuses when
# the scripted integration diverged from a script, since that recording would not be the core's.

from __future__ import annotations

import asyncio
import sys

from tests.conformance.driver import normalize, timestamps_never_decrease
from tests.conformance.goldens import (
    GOLDENS,
    events_golden,
    integration_mismatches,
    record_all,
    sends_golden,
)


def main() -> int:
    recordings = asyncio.run(record_all())
    problems = [
        f"{name}: {problem}"
        for name, recording in recordings.items()
        for problem in integration_mismatches(recording)
        + ([] if timestamps_never_decrease(recording) else ["a stream's timestamps decrease"])
    ]
    if problems:
        print("Not writing goldens:\n" + "\n".join(problems), file=sys.stderr)
        return 1
    GOLDENS.mkdir(exist_ok=True)
    for stale in GOLDENS.glob("*.jsonl"):
        stale.unlink()
    for name, recording in recordings.items():
        normalized = normalize(recording)
        events_golden(name).write_text(normalized.events_jsonl())
        sends_golden(name).write_text(normalized.sends_jsonl())
    print(f"Wrote {len(recordings)} scenarios' goldens to {GOLDENS}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
