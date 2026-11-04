"""Copy shared API guidance into independently installable SDK skills."""
from pathlib import Path
import argparse

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--check", action="store_true")
args = parser.parse_args()
source = (root / "docs/api/README.md").read_bytes()
paths = [
    root / "python/temporal_agent_harness/skills/temporal-harness-python/references/api.md",
    root / "typescript/packages/core/skills/temporal-harness-typescript/references/api.md",
]
for path in paths:
    if args.check:
        if not path.exists() or path.read_bytes() != source:
            raise SystemExit(f"Stale skill reference: {path}; run python3 scripts/sync-agent-resources.py")
    else:
        path.write_bytes(source)
