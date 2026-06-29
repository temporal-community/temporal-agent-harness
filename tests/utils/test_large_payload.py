"""Tests for the large-payload storage-driver selection.

The S3 round-trip uses a threaded moto server (the reliable way to exercise
aioboto3/aiobotocore, which talk real HTTP) reached via the standard AWS
endpoint env var — the same mechanism _s3_storage_driver() relies on in prod.

Run with: `uv run pytest tests/utils/test_large_payload.py -v`
"""

import importlib
import os

import pytest
from moto.server import ThreadedMotoServer
from temporalio.api.common.v1 import Payload

BUCKET = "large-payload-test-bucket"


def _reload_with_driver(kind: str):
    """Reimport large_payload so module-level _DRIVER_KIND picks up LARGE_PAYLOAD_DRIVER."""
    os.environ["LARGE_PAYLOAD_DRIVER"] = kind
    from temporal_agent_harness.utils import large_payload

    return importlib.reload(large_payload)


@pytest.fixture(autouse=True)
def _restore_env():
    """Snapshot/restore the env this module mutates, so the reload-based driver
    selection can't leak LARGE_PAYLOAD_DRIVER (etc.) into other test files."""
    keys = (
        "AWS_ENDPOINT_URL",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_DEFAULT_REGION",
        "LARGE_PAYLOAD_DRIVER",
        "LARGE_PAYLOAD_S3_BUCKET",
    )
    prev = {k: os.environ.get(k) for k in keys}
    try:
        yield
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture()
def s3_server():
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


async def test_s3_driver_round_trip(s3_server):
    import boto3

    large_payload = _reload_with_driver("s3")
    driver = await large_payload._s3_storage_driver()

    payload = Payload(metadata={"encoding": b"json/plain"}, data=b"x" * 3_000_000)

    # The store/retrieve context args are unused by S3StorageDriver beyond
    # `context.target` (None here -> no namespace segment).
    claims = await driver.store(_ctx(), [payload])
    assert len(claims) == 1

    objects = boto3.client("s3", endpoint_url=s3_server).list_objects_v2(Bucket=BUCKET)
    assert objects.get("KeyCount", 0) == 1, "payload should have landed in S3"

    [restored] = await driver.retrieve(_ctx(), claims)
    assert restored.data == payload.data
    assert restored.metadata["encoding"] == b"json/plain"


async def test_s3_driver_requires_bucket(s3_server):
    large_payload = _reload_with_driver("s3")
    os.environ.pop("LARGE_PAYLOAD_S3_BUCKET")
    with pytest.raises(RuntimeError, match="LARGE_PAYLOAD_S3_BUCKET"):
        await large_payload._s3_storage_driver()


async def test_default_local_driver():
    large_payload = _reload_with_driver("local")
    driver = await large_payload._build_storage_driver()
    assert isinstance(driver, large_payload.LocalFileStorageDriver)


async def test_unknown_driver_rejected():
    large_payload = _reload_with_driver("bogus")
    with pytest.raises(ValueError, match="unknown LARGE_PAYLOAD_DRIVER"):
        await large_payload._build_storage_driver()


def _ctx():
    """A minimal store/retrieve context. S3StorageDriver only reads
    `context.target`; a simple object with `target=None` suffices."""
    return type("Ctx", (), {"target": None})()
