"""Retention sweep for the large-payload offload store.

``large_payload.py`` offloads oversized payloads and keys them by content
hash. Neither the local driver nor the S3 driver ever deletes: the module's own
note says offloaded objects "just accumulate" and points at a bucket lifecycle
rule as the only cleanup. This adds an in-repo alternative, so a deployment that
cannot set a bucket policy (or uses the local driver) still has a way to reclaim
space.

The sweep deletes offloaded objects older than a window. Age is measured from
the object's last-modified time, which for these drivers is its FIRST-store time
(both dedupe by content hash and skip re-writing an existing object, so re-use
does not refresh the timestamp). Two consequences to size the window against:

- An offloaded payload is safe to delete only once nothing can still claim it.
  A claim lives in Event History, so keep the window LONGER than the namespace
  retention period, and longer than the oldest live workflow that might replay.
- Because re-use does not refresh the timestamp, a long-lived workflow that
  keeps referencing one old payload could have it swept. In practice the agent's
  offloaded payloads are per-turn conversation snapshots whose bytes change every
  turn, so each turn writes a fresh object and the old ones become unreferenced.
  Do not point this at a store whose objects are long-lived and re-referenced.

Run it from cron or a Temporal Schedule::

    python -m temporal_agent_harness.utils.large_payload_gc --days 7
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path

from temporal_agent_harness.utils.large_payload import _BASE_DIR


@dataclass
class SweepResult:
    scanned: int = 0
    deleted: int = 0
    freed_bytes: int = 0


def sweep_local(older_than_epoch: float, base_dir: Path | None = None) -> SweepResult:
    """Delete offloaded blobs (and stale temp files) under the local store whose
    mtime is older than ``older_than_epoch``. A missing store is not an error."""
    base = Path(base_dir) if base_dir is not None else _BASE_DIR
    result = SweepResult()
    if not base.exists():
        return result
    for path in base.iterdir():
        # The local driver writes `{sha256}.bin` and, briefly, `.tmp` files it
        # renames into place; a crashed store can strand a `.tmp`, so both age
        # out through here.
        if not path.is_file() or path.suffix not in (".bin", ".tmp"):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue  # deleted underneath us; a sweep can run beside live traffic
        result.scanned += 1
        if stat.st_mtime < older_than_epoch:
            try:
                path.unlink()
            except OSError:
                continue
            result.deleted += 1
            result.freed_bytes += stat.st_size
    return result


async def sweep_s3(
    older_than_epoch: float, bucket: str | None = None
) -> SweepResult:
    """Delete objects in the S3 offload bucket older than ``older_than_epoch``.

    Uses aioboto3 and the standard AWS credential/endpoint chain, matching
    ``large_payload._s3_storage_driver`` (so ``AWS_ENDPOINT_URL`` retargets it at
    MinIO or a mock the same way)."""
    import aioboto3

    bucket = bucket or os.environ.get("LARGE_PAYLOAD_S3_BUCKET")
    if not bucket:
        raise RuntimeError("sweep_s3 requires LARGE_PAYLOAD_S3_BUCKET (or an explicit bucket)")
    result = SweepResult()
    session = aioboto3.Session()
    async with session.client("s3") as s3:
        paginator = s3.get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=bucket):
            expired = []
            for obj in page.get("Contents", []):
                result.scanned += 1
                if obj["LastModified"].timestamp() < older_than_epoch:
                    expired.append({"Key": obj["Key"]})
                    result.deleted += 1
                    result.freed_bytes += obj.get("Size", 0)
            # DeleteObjects takes up to 1000 keys, the list page size, so one
            # delete per page covers it.
            if expired:
                await s3.delete_objects(Bucket=bucket, Delete={"Objects": expired, "Quiet": True})
    return result


async def sweep(older_than_epoch: float) -> SweepResult:
    """Sweep the store selected by ``LARGE_PAYLOAD_DRIVER`` (mirrors the driver
    selection in ``large_payload``)."""
    kind = os.environ.get("LARGE_PAYLOAD_DRIVER", "local").strip().lower()
    if kind == "local":
        return sweep_local(older_than_epoch)
    if kind == "s3":
        return await sweep_s3(older_than_epoch)
    raise ValueError(f"unknown LARGE_PAYLOAD_DRIVER={kind!r}; expected 'local' or 's3'")


def main() -> None:
    parser = argparse.ArgumentParser(description="Delete offloaded payloads older than a window.")
    parser.add_argument(
        "--days", type=float, default=7.0, help="delete objects older than this many days (default 7)"
    )
    args = parser.parse_args()
    if args.days <= 0:
        parser.error("--days must be positive")
    # No wall-clock inside a workflow here: this is an ops CLI, not workflow code.
    cutoff = time.time() - args.days * 86_400
    result = asyncio.run(sweep(cutoff))
    print(
        f"scanned {result.scanned}, deleted {result.deleted}, "
        f"freed {result.freed_bytes / 1_048_576:.1f} MB (older than {args.days} days)"
    )


if __name__ == "__main__":
    main()
