"""Check a Playpen policy without running an agent: Dogwood's validation, plus the annotations.

``dogwood validate`` checks the rules against the policy's schema and Playpen's event kinds;
this also reads every rule's Playpen annotations, so a mistyped ``@on_deny`` fails here rather
than on the first call it would have decided::

    uv run python -m temporal_agent_harness.playpen.check examples/playpen/policies/support \\
        --dogwood "$DOGWOOD_CLI"
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from .activity import EVENT_SCHEMA, POLICY_FILE, SCHEMA_FILE
from .policy_file import parse_rules


def check(policy_dir: Path, *, dogwood_cli: str) -> str:
    """Validate the policy in ``policy_dir``; return a one-line-per-rule summary.

    Raises ``ValueError`` if Dogwood rejects the policy or an annotation is invalid."""
    rules = parse_rules((policy_dir / POLICY_FILE).read_text())
    with tempfile.TemporaryDirectory(prefix="dogwood-") as tmp:
        events = Path(tmp) / "events.dwschema"
        events.write_text(EVENT_SCHEMA)
        result = subprocess.run(
            [
                dogwood_cli,
                "validate",
                str(policy_dir / POLICY_FILE),
                "--policy-schema",
                str(policy_dir / SCHEMA_FILE),
                "--event-schema",
                str(events),
            ],
            capture_output=True,
            text=True,
        )
    if result.returncode != 0:
        raise ValueError((result.stdout + result.stderr).strip())
    lines = [result.stdout.strip()]
    for rule in rules:
        actions = ", ".join(sorted(rule.actions)) or "any action"
        escalates = " (escalates on deny)" if rule.escalates_on_deny else ""
        lines.append(f"  {rule.index}: {rule.effect} {rule.id} [{actions}]{escalates}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("policy_dir", type=Path, help="a policy directory in the store")
    parser.add_argument("--dogwood", required=True, help="path to the dogwood CLI")
    args = parser.parse_args(argv)
    try:
        print(check(args.policy_dir, dogwood_cli=args.dogwood))
    except ValueError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
