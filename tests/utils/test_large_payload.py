"""Tests for the large-payload storage factories.

The S3 round-trip uses a threaded moto server (the reliable way to exercise
aioboto3/aiobotocore, which talk real HTTP) reached via the standard AWS
endpoint env var — the same mechanism s3_payload_storage() relies on in prod.

Run with: `uv run pytest tests/utils/test_large_payload.py -v`
"""

import os

import pytest
from moto.server import ThreadedMotoServer
from temporalio.api.common.v1 import Payload
from temporalio.contrib.pydantic import pydantic_data_converter

from temporal_agent_harness.utils.large_payload import (
    DEFAULT_PAYLOAD_SIZE_THRESHOLD,
    DEFAULT_PAYLOAD_STORAGE,
    LocalFileStorageDriver,
    local_payload_storage,
    s3_payload_storage,
    with_large_payload_offload,
)

BUCKET = "large-payload-test-bucket"


@pytest.fixture(autouse=True)
def _restore_env():
    """Snapshot/restore the AWS env the S3 fixture sets, so it can't leak between files."""
    keys = (
        "AWS_ENDPOINT_URL",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_DEFAULT_REGION",
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
    try:
        import boto3

        boto3.client("s3", endpoint_url=endpoint).create_bucket(Bucket=BUCKET)
        yield endpoint
    finally:
        server.stop()


# ---------------------------------------------------------------- local


def test_local_storage_defaults():
    storage = local_payload_storage()
    [driver] = storage.drivers
    assert isinstance(driver, LocalFileStorageDriver)
    assert driver.name() == "local-file"
    assert storage.payload_size_threshold == DEFAULT_PAYLOAD_SIZE_THRESHOLD


def test_local_storage_takes_its_config_as_arguments(tmp_path):
    """No env vars: the directory and threshold are passed in by the caller."""
    storage = local_payload_storage(base_dir=tmp_path, payload_size_threshold=10)
    assert storage.payload_size_threshold == 10
    assert storage.drivers[0]._base == tmp_path


async def test_local_driver_round_trip(tmp_path):
    [driver] = local_payload_storage(base_dir=tmp_path).drivers

    payload = Payload(metadata={"encoding": b"json/plain"}, data=b"y" * 2_000_000)
    claims = await driver.store(_ctx(), [payload])
    assert len(claims) == 1
    assert list(tmp_path.glob("*.bin")), "payload should have landed on disk"

    [restored] = await driver.retrieve(_ctx(), claims)
    assert restored.data == payload.data
    assert restored.metadata["encoding"] == b"json/plain"


async def test_local_driver_rejects_a_traversing_claim_key(tmp_path):
    [driver] = local_payload_storage(base_dir=tmp_path).drivers
    from temporalio.converter import StorageDriverClaim

    with pytest.raises(ValueError, match="invalid storage claim key"):
        await driver.retrieve(_ctx(), [StorageDriverClaim(claim_data={"key": "../x"})])


def test_the_shared_default_is_local_storage():
    """Everything that doesn't configure a backend must land on the SAME instance.

    The plugin, the packaged web app, and any hand-built converter all default to this one
    object; an offloaded payload is only readable by a process using the matching backend.
    """
    [driver] = DEFAULT_PAYLOAD_STORAGE.drivers
    assert isinstance(driver, LocalFileStorageDriver)
    assert DEFAULT_PAYLOAD_STORAGE.payload_size_threshold == DEFAULT_PAYLOAD_SIZE_THRESHOLD


# ---------------------------------------------------------------- s3


async def test_s3_storage_round_trip(s3_server):
    import boto3

    storage = await s3_payload_storage(bucket=BUCKET)
    assert storage.payload_size_threshold == DEFAULT_PAYLOAD_SIZE_THRESHOLD
    [driver] = storage.drivers

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


# ---------------------------------------------------------------- converter helper


def test_with_large_payload_offload_applies_the_given_storage(tmp_path):
    storage = local_payload_storage(base_dir=tmp_path)
    converted = with_large_payload_offload(pydantic_data_converter, storage)
    assert converted.external_storage is storage
    assert pydantic_data_converter.external_storage is None, "must not mutate the input"


def test_with_large_payload_offload_refuses_to_clobber_existing_storage(tmp_path):
    already = with_large_payload_offload(
        pydantic_data_converter, local_payload_storage(base_dir=tmp_path)
    )
    with pytest.raises(ValueError, match="already has external_storage"):
        with_large_payload_offload(already, local_payload_storage(base_dir=tmp_path))


def _ctx():
    """A minimal store/retrieve context. S3StorageDriver only reads
    `context.target`; a simple object with `target=None` suffices."""
    return type("Ctx", (), {"target": None})()
