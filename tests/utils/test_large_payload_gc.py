"""Tests for the large-payload retention sweep.

The local sweep is exercised directly against a temp store. The S3 sweep uses
the same threaded moto server as test_large_payload.py, driving the sweep's
list/delete/pagination through real HTTP.

Run with: `uv run pytest tests/utils/test_large_payload_gc.py -v`
"""

import os
import time
from pathlib import Path

import pytest

from temporal_agent_harness.utils import large_payload_gc as gc

BUCKET = "large-payload-gc-test-bucket"


def _age(path: Path, seconds: int) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


def test_local_sweep_deletes_old_keeps_fresh_and_cleans_tmp(tmp_path: Path):
    (tmp_path / "old.bin").write_bytes(b"x" * 1000)
    (tmp_path / "fresh.bin").write_bytes(b"y")
    (tmp_path / "crashed.bin.tmp").write_bytes(b"partial")
    _age(tmp_path / "old.bin", 60 * 86_400)
    _age(tmp_path / "crashed.bin.tmp", 60 * 86_400)

    result = gc.sweep_local(time.time() - 30 * 86_400, base_dir=tmp_path)

    assert result.scanned == 3
    assert result.deleted == 2
    assert result.freed_bytes >= 1000
    assert not (tmp_path / "old.bin").exists()
    assert not (tmp_path / "crashed.bin.tmp").exists()
    assert (tmp_path / "fresh.bin").read_bytes() == b"y"


def test_local_sweep_of_missing_store_reports_zero(tmp_path: Path):
    result = gc.sweep_local(time.time(), base_dir=tmp_path / "never-created")
    assert (result.scanned, result.deleted, result.freed_bytes) == (0, 0, 0)


def test_local_sweep_ignores_non_blob_files(tmp_path: Path):
    (tmp_path / "notes.txt").write_text("not a blob")
    _age(tmp_path / "notes.txt", 60 * 86_400)
    result = gc.sweep_local(time.time() - 30 * 86_400, base_dir=tmp_path)
    assert result.scanned == 0
    assert (tmp_path / "notes.txt").exists()


@pytest.fixture(autouse=True)
def _restore_env():
    keys = (
        "AWS_ENDPOINT_URL",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_DEFAULT_REGION",
        "LARGE_PAYLOAD_S3_BUCKET",
    )
    prev = {k: os.environ.get(k) for k in keys}
    try:
        yield
    finally:
        for k, v in prev.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


@pytest.fixture()
def s3_server():
    from moto.server import ThreadedMotoServer

    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0)
    server.start()
    _, port = server.get_host_and_port()
    endpoint = f"http://127.0.0.1:{port}"
    os.environ["AWS_ENDPOINT_URL"] = endpoint
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
    os.environ["LARGE_PAYLOAD_S3_BUCKET"] = BUCKET
    try:
        import boto3

        boto3.client("s3", endpoint_url=endpoint).create_bucket(Bucket=BUCKET)
        yield endpoint
    finally:
        server.stop()


async def test_s3_sweep_deletes_objects_older_than_cutoff(s3_server):
    import boto3

    client = boto3.client("s3", endpoint_url=s3_server)
    for key in ("a.bin", "b.bin", "c.bin"):
        client.put_object(Bucket=BUCKET, Key=key, Body=b"data")

    # moto stamps LastModified at put time; a cutoff in the future treats them
    # all as expired, which exercises list + batched delete + the counters.
    result = await gc.sweep_s3(time.time() + 3600)
    assert result.scanned == 3
    assert result.deleted == 3
    assert client.list_objects_v2(Bucket=BUCKET).get("KeyCount", 0) == 0


async def test_s3_sweep_keeps_objects_newer_than_cutoff(s3_server):
    import boto3

    client = boto3.client("s3", endpoint_url=s3_server)
    client.put_object(Bucket=BUCKET, Key="fresh.bin", Body=b"data")

    result = await gc.sweep_s3(time.time() - 3600)
    assert result.scanned == 1
    assert result.deleted == 0
    assert client.list_objects_v2(Bucket=BUCKET).get("KeyCount", 0) == 1


async def test_s3_sweep_requires_a_bucket(s3_server):
    os.environ.pop("LARGE_PAYLOAD_S3_BUCKET")
    with pytest.raises(RuntimeError, match="LARGE_PAYLOAD_S3_BUCKET"):
        await gc.sweep_s3(time.time())
